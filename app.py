"""
LINE 接龍機器人
支援兩種模式：
1. 簡易接龍：接龍 [名稱] → 大家依序報名
2. 工作認養排班：直接貼入排班表 → Bot 自動解析並編號，成員用 +編號 姓名 報名
"""

import os
import re
import json
import sqlite3
import logging
import threading
from datetime import datetime

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TZ_TAIPEI = pytz.timezone("Asia/Taipei")

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "jielong.db")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ── 排班表解析用正規表示式
DATE_RE      = re.compile(r'(\d{1,2}/\d{1,2})\s*[（(]([一二三四五六日ㄧ零][一二三四五六日ㄧ零]?)[）)]')
COUNT_RE     = re.compile(r'(\d+)\s*人')
TIME_RE      = re.compile(r'\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}:\d{2})?')
SESSION_RE   = re.compile(r'^\s*(上午|下午)\s*[：:](.*)')

HELP_TEXT = """📖 接龍助理使用說明
─────────────────
【工作認養排班模式】
直接將排班表貼到群組
→ Bot 自動解析並編號

+[編號] 你的名字  — 報名特定工作
+3 小明           — 報名第3項
+3               — 報名第3項（用LINE暱稱）
退出 [編號]       — 取消特定項目報名
列表              — 查看目前報名狀況
結束接龍          — 封存最終名單

─────────────────
【簡易接龍模式】
接龍 [名稱]  — 開始新的接龍
+1 [姓名] [項目] [備註] — 依序加入
列表         — 查看名單
退出         — 移除自己
結束接龍     — 封存最終名單

─────────────────
📌 每天早上 07:00 自動公布最新名單"""


# ══════════════════════════════════════════
# 資料庫
# ══════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS lists (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     TEXT    NOT NULL,
            title        TEXT    NOT NULL,
            creator_id   TEXT    NOT NULL,
            creator_name TEXT,
            status       TEXT    DEFAULT 'open',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            list_type    TEXT    DEFAULT 'simple'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id    INTEGER NOT NULL,
            user_id    TEXT    NOT NULL,
            user_name  TEXT,
            item       TEXT,
            quantity   TEXT,
            seq        INTEGER,
            slot_num   INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (list_id) REFERENCES lists (id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id        INTEGER NOT NULL,
            slot_num       INTEGER NOT NULL,
            date_str       TEXT,
            day_str        TEXT,
            activity       TEXT,
            time_str       TEXT,
            session        TEXT,
            required_count INTEGER DEFAULT 1,
            note           TEXT,
            FOREIGN KEY (list_id) REFERENCES lists (id)
        )
    """)
    # 相容舊資料庫：補欄位（已存在時靜默忽略）
    for sql in [
        "ALTER TABLE lists   ADD COLUMN list_type TEXT DEFAULT 'simple'",
        "ALTER TABLE entries ADD COLUMN slot_num  INTEGER",
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass

    conn.commit()
    conn.close()


# ══════════════════════════════════════════
# 資料庫輔助函式
# ══════════════════════════════════════════

def get_active_list(group_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'SELECT * FROM lists WHERE group_id=? AND status="open" ORDER BY id DESC LIMIT 1',
        (group_id,),
    )
    row = c.fetchone()
    conn.close()
    return row  # cols: id,group_id,title,creator_id,creator_name,status,created_at,list_type

def _list_type(active):
    return active[7] if active and len(active) > 7 else "simple"

def get_entries(list_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM entries WHERE list_id=? ORDER BY seq", (list_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_slots(list_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM slots WHERE list_id=? ORDER BY slot_num", (list_id,))
    rows = c.fetchall()
    conn.close()
    return rows  # id,list_id,slot_num,date_str,day_str,activity,time_str,session,required_count,note

def get_slot_signups(list_id):
    """回傳 {slot_num: [name, ...]} 的 dict"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT slot_num, user_name FROM entries WHERE list_id=? AND slot_num IS NOT NULL ORDER BY id",
        (list_id,),
    )
    rows = c.fetchall()
    conn.close()
    result = {}
    for snum, uname in rows:
        result.setdefault(snum, []).append(uname or "（未知）")
    return result

def get_all_active_lists():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM lists WHERE status="open"')
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_name(event, group_id, user_id):
    try:
        if event.source.type == "group":
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
        else:
            profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return None

def source_id(event):
    src = event.source
    if src.type == "group":
        return src.group_id
    if src.type == "room":
        return src.room_id
    return src.user_id

def normalize(text):
    """全形英數符號 → 半形（處理中文輸入法輸入的 ＋、１２３ 等）"""
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:   # 全形 ！～ → 半形 !~
            result.append(chr(code - 0xFEE0))
        elif ch == '\u3000':            # 全形空格 → 半形空格
            result.append(' ')
        else:
            result.append(ch)
    return ''.join(result)


# ══════════════════════════════════════════
# 排班表解析
# ══════════════════════════════════════════

def is_schedule_post(text):
    """含有至少 2 個日期行（3/1（日）格式）視為排班表"""
    return len(DATE_RE.findall(text)) >= 2


def parse_schedule_slots(text):
    """
    解析工作認養排班表，回傳 slot list。
    每個 slot：{slot_num, date_str, day_str, activity, time_str, session, required_count, note}
    有「上午：/ 下午：」的工作項目會拆成兩個 slot。
    """
    slots = []
    slot_num = 1
    lines = text.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        date_match = DATE_RE.search(line)

        if not date_match:
            i += 1
            continue

        date_str = date_match.group(1)
        day_str  = date_match.group(2)
        after    = line[date_match.end():].strip()

        # 萃取人數
        count_match = COUNT_RE.search(after)
        required = int(count_match.group(1)) if count_match else 1
        if count_match:
            after = (after[:count_match.start()] + after[count_match.end():]).strip()

        # 萃取同行的時間
        time_str   = ""
        time_match = TIME_RE.search(after)
        if time_match:
            time_str = time_match.group().strip()
            after = (after[:time_match.start()] + after[time_match.end():]).strip()

        activity     = after.strip()
        sessions     = []   # 收集到的 ['上午','下午']
        note_parts   = []

        # 掃描後續行，直到空行或下一個日期
        j = i + 1
        while j < len(lines):
            nl = lines[j].strip()
            if not nl:
                j += 1
                break
            if DATE_RE.search(nl):
                break

            sm = SESSION_RE.match(nl)
            if sm:
                sess = sm.group(1)
                if sess not in sessions:
                    sessions.append(sess)
            elif TIME_RE.search(nl) and not time_str:
                time_str = nl.strip()
            else:
                note_parts.append(nl)
            j += 1

        note = " ".join(note_parts).strip()

        if sessions:
            # 有上午/下午 → 各建一個 slot（確保兩個都有）
            for sess in ["上午", "下午"]:
                slots.append({
                    "slot_num":      slot_num,
                    "date_str":      date_str,
                    "day_str":       day_str,
                    "activity":      activity,
                    "time_str":      time_str,
                    "session":       sess,
                    "required_count": required,
                    "note":          note,
                })
                slot_num += 1
        else:
            slots.append({
                "slot_num":      slot_num,
                "date_str":      date_str,
                "day_str":       day_str,
                "activity":      activity,
                "time_str":      time_str,
                "session":       None,
                "required_count": required,
                "note":          note,
            })
            slot_num += 1

        i = j

    return slots


# ══════════════════════════════════════════
# 格式化顯示
# ══════════════════════════════════════════

def _slot_label(slot):
    """slot tuple → 單行文字，如「3/18（三）苓雅共修處值班 上午」"""
    date_str = slot[3]
    day_str  = slot[4]
    activity = slot[5]
    time_str = slot[6]
    session  = slot[7]
    label = f"{date_str}（{day_str}）{activity}"
    if session:
        label += f" {session}"
    if time_str:
        label += f" {time_str}"
    return label


def format_schedule_list(list_row, slots, signups, *, show_time=False):
    title   = list_row[2]
    creator = list_row[4] or "負責人"
    lines   = [f"📋 {title}", f"（負責人：{creator}）"]
    if show_time:
        now = datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d %H:%M")
        lines.append(f"🕖 更新：{now}")
    lines.append("─" * 16)

    for s in slots:
        slot_num = s[2]
        required = s[8]
        header   = f"{slot_num}. {_slot_label(s)}"
        if required > 1:
            header += f"（共{required}人）"
        lines.append(header)
        names = signups.get(slot_num, [])
        lines.append("   👤 " + ("、".join(names) if names else "（尚無人報名）"))

    return "\n".join(lines)


def format_list(list_row, entries, *, show_time=False):
    title   = list_row[2]
    creator = list_row[4] or "開團者"
    lines   = [f"📋 {title}", f"（開團：{creator}）"]
    if show_time:
        now = datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d %H:%M")
        lines.append(f"🕖 更新時間：{now}")
    lines.append("─" * 16)

    if not entries:
        lines.append("（尚無人加入）")
    else:
        for e in entries:
            seq       = e[6]
            disp_name = e[3] or "匿名"
            item      = e[4] or ""
            quantity  = e[5] or ""
            parts = [f"{seq}. {disp_name}"]
            if item:
                parts.append(item)
            if quantity:
                parts.append(quantity)
            lines.append(" ".join(parts))

    return "\n".join(lines)


# ══════════════════════════════════════════
# 每日推播
# ══════════════════════════════════════════

def daily_broadcast():
    active_lists = get_all_active_lists()
    if not active_lists:
        logger.info("[排程] 目前沒有進行中的接龍，跳過推播")
        return

    now_str = datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d")
    logger.info(f"[排程] 開始推播 {len(active_lists)} 個接龍")

    for lst in active_lists:
        group_id  = lst[1]
        ltype     = _list_type(lst)

        if ltype == "schedule":
            slots   = get_slots(lst[0])
            signups = get_slot_signups(lst[0])
            body    = format_schedule_list(lst, slots, signups, show_time=True)
        else:
            entries = get_entries(lst[0])
            body    = format_list(lst, entries, show_time=True)

        message = f"📣 早安！以下是今日工作認養名單（{now_str}）\n\n{body}"
        try:
            line_bot_api.push_message(group_id, TextSendMessage(text=message))
            logger.info(f"[排程] 已推播至 {group_id}：{lst[2]}")
        except Exception as e:
            logger.error(f"[排程] 推播失敗 {group_id}：{e}")


# ══════════════════════════════════════════
# 指令處理
# ══════════════════════════════════════════

def cmd_post_schedule(group_id, user_id, user_name, text):
    """解析排班表並建立排班型接龍"""
    slots = parse_schedule_slots(text)
    if not slots:
        return "找不到日期資料，無法建立排班表。請確認格式如：3/1（日）活動名稱"

    # 標題：取第一行若非日期行，否則用預設
    first_line = text.strip().split("\n")[0].strip()
    title = first_line if not DATE_RE.search(first_line) else "工作認養排班"
    title = re.sub(r"[：:如下]+$", "", title).strip() or "工作認養排班"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE lists SET status="closed" WHERE group_id=? AND status="open"', (group_id,))
    c.execute(
        "INSERT INTO lists (group_id, title, creator_id, creator_name, list_type) VALUES (?, ?, ?, ?, 'schedule')",
        (group_id, title, user_id, user_name),
    )
    list_id = c.lastrowid
    for s in slots:
        c.execute(
            "INSERT INTO slots (list_id,slot_num,date_str,day_str,activity,time_str,session,required_count,note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (list_id, s["slot_num"], s["date_str"], s["day_str"], s["activity"],
             s["time_str"], s["session"], s["required_count"], s["note"]),
        )
    conn.commit()
    conn.close()

    lines = [f"✅ 排班表已建立！\n📋 {title}\n共 {len(slots)} 個工作項目\n─────────────────"]
    for s in slots:
        label = f"{s['slot_num']}. {s['date_str']}（{s['day_str']}）{s['activity']}"
        if s["session"]:
            label += f" {s['session']}"
        if s["time_str"]:
            label += f" {s['time_str']}"
        if s["required_count"] > 1:
            label += f" {s['required_count']}人"
        lines.append(label)
    lines.append("\n報名方式：\n+[編號] 你的名字\n例：+3 小明\n（或只輸入 +3，用LINE暱稱報名）")
    return "\n".join(lines)


def cmd_open(group_id, user_id, user_name, text):
    """簡易接龍"""
    m = re.match(r"[/]?(?:接龍|開團)\s*(.*)", text)
    title = (m.group(1).strip() if m else "").strip() or "工作接龍"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE lists SET status="closed" WHERE group_id=? AND status="open"', (group_id,))
    c.execute(
        "INSERT INTO lists (group_id, title, creator_id, creator_name) VALUES (?, ?, ?, ?)",
        (group_id, title, user_id, user_name),
    )
    conn.commit()
    conn.close()

    return (
        f"✅ 接龍已開始！\n"
        f"📋 {title}\n\n"
        f"群組成員直接輸入：\n"
        f"+1 姓名 工作項目 備註\n"
        f"（工作項目和備註可省略）\n\n"
        f"例：+1 小明 早班 8:00-12:00\n\n"
        f"📌 名單每天早上 07:00 自動公布\n"
        f"隨時輸入「列表」也可查看"
    )


def cmd_join(group_id, user_id, user_name, text):
    """加入接龍（自動依 list_type 切換模式）"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。\n請貼上排班表，或輸入「接龍 [名稱]」開始簡易接龍。"

    if _list_type(active) == "schedule":
        return _join_slot(group_id, user_id, user_name, text, active)
    else:
        return _join_simple(group_id, user_id, user_name, text, active)


def _join_slot(group_id, user_id, user_name, text, active):
    """排班模式：+3 小明 → 報名第 3 號工作"""
    list_id = active[0]

    m = re.match(r"\+(\d+)\s*(.*)", text)
    if not m:
        return "格式：+[編號] 你的名字\n例：+3 小明\n（輸入「列表」查看可報名項目）"

    slot_num = int(m.group(1))
    name     = m.group(2).strip() or user_name or "（未知）"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 確認 slot 存在
    c.execute("SELECT * FROM slots WHERE list_id=? AND slot_num=?", (list_id, slot_num))
    slot = c.fetchone()
    if not slot:
        conn.close()
        return f"找不到第 {slot_num} 號工作項目。\n輸入「列表」查看可報名的項目。"

    required = slot[8]

    # 同一人重複報名同一項目 → 更新姓名
    c.execute(
        "SELECT id FROM entries WHERE list_id=? AND user_id=? AND slot_num=?",
        (list_id, user_id, slot_num),
    )
    existing = c.fetchone()
    if existing:
        c.execute("UPDATE entries SET user_name=? WHERE id=?", (name, existing[0]))
        conn.commit()
        conn.close()
        return f"✏️ 已更新！\n{slot_num}. {_slot_label(slot)} → {name}"

    # 檢查額滿（required > 1 才限制名額）
    if required > 1:
        c.execute(
            "SELECT COUNT(*) FROM entries WHERE list_id=? AND slot_num=?",
            (list_id, slot_num),
        )
        if c.fetchone()[0] >= required:
            conn.close()
            return f"❌ 第 {slot_num} 號已額滿（{required} 人）！"

    c.execute(
        "INSERT INTO entries (list_id, user_id, user_name, slot_num, seq) VALUES (?, ?, ?, ?, ?)",
        (list_id, user_id, name, slot_num, slot_num),
    )
    conn.commit()
    conn.close()
    return f"✅ 報名成功！\n{slot_num}. {_slot_label(slot)} → {name}\n（輸入「列表」查看完整名單）"


def _join_simple(group_id, user_id, user_name, text, active):
    """簡易接龍模式：+1 名字 項目 數量"""
    list_id = active[0]

    m    = re.match(r"\+\d*\s*(.*)", text)
    rest = m.group(1).strip() if m else text[1:].strip()
    parts = rest.split(None, 2)
    if not parts:
        return "格式：+1 [名字] [項目] [備註]\n例：+1 小明 早班"

    entry_name = parts[0]
    item       = parts[1] if len(parts) > 1 else ""
    quantity   = parts[2] if len(parts) > 2 else ""

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, seq FROM entries WHERE list_id=? AND user_id=?", (list_id, user_id))
    existing = c.fetchone()

    if existing:
        c.execute(
            "UPDATE entries SET user_name=?, item=?, quantity=? WHERE id=?",
            (entry_name, item, quantity, existing[0]),
        )
        seq   = existing[1]
        reply = f"✏️ 已更新！（第 {seq} 號）"
    else:
        c.execute("SELECT MAX(seq) FROM entries WHERE list_id=?", (list_id,))
        seq = (c.fetchone()[0] or 0) + 1
        c.execute(
            "INSERT INTO entries (list_id, user_id, user_name, item, quantity, seq) VALUES (?, ?, ?, ?, ?, ?)",
            (list_id, user_id, entry_name, item, quantity, seq),
        )
        reply = f"✅ 已加入！你是第 {seq} 號"

    conn.commit()
    conn.close()
    return reply + "\n（名單每天 07:00 公布，或輸入「列表」隨時查看）"


def cmd_list(group_id):
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    ltype = _list_type(active)
    logger.info(f"[cmd_list] list_id={active[0]} list_type={ltype}")

    if ltype == "schedule":
        slots   = get_slots(active[0])
        signups = get_slot_signups(active[0])
        logger.info(f"[cmd_list] slots={len(slots)} signups={signups}")
        return format_schedule_list(active, slots, signups)
    else:
        entries = get_entries(active[0])
        logger.info(f"[cmd_list] entries={len(entries)}")
        return format_list(active, entries)


def cmd_close(group_id, user_id):
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE lists SET status="closed" WHERE id=?', (active[0],))
    conn.commit()
    conn.close()

    if _list_type(active) == "schedule":
        slots   = get_slots(active[0])
        signups = get_slot_signups(active[0])
        body    = format_schedule_list(active, slots, signups, show_time=True)
        total   = sum(len(v) for v in signups.values())
        return f"🔒 工作認養已結束！\n\n{body}\n\n共 {total} 人報名"
    else:
        entries  = get_entries(active[0])
        body     = format_list(active, entries, show_time=True)
        return f"🔒 接龍已結束，以下為最終名單：\n\n{body}\n\n共 {len(entries)} 人報名"


def cmd_leave(group_id, user_id, text=""):
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    list_id = active[0]

    # 排班模式支援「退出 3」取消特定項目
    slot_match = re.match(r"(?:退出|取消)\s+(\d+)", text)
    if _list_type(active) == "schedule" and slot_match:
        slot_num = int(slot_match.group(1))
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "DELETE FROM entries WHERE list_id=? AND user_id=? AND slot_num=?",
            (list_id, user_id, slot_num),
        )
        affected = c.rowcount
        conn.commit()
        conn.close()
        if affected:
            return f"✅ 已取消第 {slot_num} 號工作的報名。"
        else:
            return f"你沒有報名第 {slot_num} 號工作。"

    # 預設：移除該用戶所有報名
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if _list_type(active) == "schedule":
        c.execute(
            "SELECT DISTINCT slot_num FROM entries WHERE list_id=? AND user_id=?",
            (list_id, user_id),
        )
        slot_nums = [r[0] for r in c.fetchall()]
        if not slot_nums:
            conn.close()
            return "你目前沒有報名任何工作項目。"
        c.execute("DELETE FROM entries WHERE list_id=? AND user_id=?", (list_id, user_id))
        conn.commit()
        conn.close()
        return f"✅ 已取消你在第 {', '.join(str(s) for s in slot_nums)} 號的報名。"
    else:
        c.execute("SELECT id, seq FROM entries WHERE list_id=? AND user_id=?", (list_id, user_id))
        existing = c.fetchone()
        if not existing:
            conn.close()
            return "你不在目前的接龍名單中。"
        c.execute("DELETE FROM entries WHERE id=?", (existing[0],))
        conn.commit()
        conn.close()
        return f"✅ 已將你（第 {existing[1]} 號）從名單中移除。"


# ══════════════════════════════════════════
# LINE Webhook
# ══════════════════════════════════════════

@app.route("/", methods=["GET"])
def health():
    return str({
        "status":    "ok",
        "scheduler": _scheduler_started,
        "token_set": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "secret_set": bool(LINE_CHANNEL_SECRET),
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = json.loads(body).get("events", [])
        for ev in events:
            logger.info(f"[webhook] type={ev.get('type')} source={ev.get('source',{}).get('type')}")
    except Exception:
        logger.info(f"[webhook] raw: {body[:200]}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("[webhook] Invalid signature")
        abort(400)
    except Exception as e:
        logger.error(f"[webhook] 處理失敗: {e}")
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = normalize(event.message.text.strip())
    gid  = source_id(event)
    uid  = event.source.user_id

    logger.info(f"[msg] text={repr(text[:60])}")

    def lazy_name():
        return get_user_name(event, gid, uid)

    reply = None

    # ── 排班表：多行且含日期格式（優先偵測）
    if "\n" in text and is_schedule_post(text):
        reply = cmd_post_schedule(gid, uid, lazy_name(), text)

    # ── 簡易接龍開始
    elif re.match(r"[/]?(?:接龍|開團)\s+\S", text):
        reply = cmd_open(gid, uid, lazy_name(), text)

    # ── 加入（+N 或 +N 姓名）
    elif re.match(r"\+\d+(\s|$)", text):
        reply = cmd_join(gid, uid, lazy_name(), text)

    # ── 查看名單
    elif text in ("列表", "/列表", "查看", "名單"):
        reply = cmd_list(gid)

    # ── 結束
    elif text in ("結束接龍", "結團", "/結束接龍", "/結團", "關閉接龍"):
        reply = cmd_close(gid, uid)

    # ── 退出（支援「退出 3」取消特定項目）
    elif re.match(r"(退出|取消)(\s+\d+)?$", text):
        reply = cmd_leave(gid, uid, text)

    # ── 說明
    elif text in ("說明", "/說明", "help", "/help", "幫助"):
        reply = HELP_TEXT

    logger.info(f"[msg] reply={'（無）' if reply is None else repr(reply[:40])}")

    if reply:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e:
            logger.error(f"[reply] 失敗: {e}")


@handler.add(JoinEvent)
def handle_join(event):
    msg = (
        "👋 大家好！我是接龍助理\n\n"
        "📋 工作認養排班：\n"
        "直接將排班表貼到群組，我會自動解析並編號，大家用 +編號 姓名 報名\n\n"
        "📝 簡易接龍：\n"
        "輸入「接龍 [名稱]」開始\n\n"
        "輸入「說明」查看完整指令\n"
        "📌 每天早上 07:00 自動公布名單"
    )
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
    except Exception as e:
        logger.error(f"[Join] 失敗: {e}")


# ══════════════════════════════════════════
# 排程器
# ══════════════════════════════════════════

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=TZ_TAIPEI)
    scheduler.add_job(
        daily_broadcast, trigger="cron", hour=7, minute=0,
        id="daily_broadcast", replace_existing=True,
    )
    scheduler.start()
    logger.info("[排程] 已啟動，每天 07:00（台灣時間）自動推播")
    return scheduler


# ══════════════════════════════════════════
# 啟動初始化（模組層級，gunicorn 和直接執行都適用）
# ══════════════════════════════════════════

_startup_lock     = threading.Lock()
_scheduler_started = False


def _startup():
    global _scheduler_started
    with _startup_lock:
        try:
            init_db()
            logger.info("[startup] 資料庫初始化完成")
        except Exception as e:
            logger.error(f"[startup] 資料庫初始化失敗: {e}")

        in_gunicorn = "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")
        is_worker   = os.environ.get("GUNICORN_WORKER", "") == "1"

        if not _scheduler_started and (not in_gunicorn or is_worker):
            try:
                start_scheduler()
                _scheduler_started = True
            except Exception as e:
                logger.error(f"[startup] 排程器啟動失敗: {e}")


_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
