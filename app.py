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
from datetime import datetime, timedelta

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
DB_PATH = os.environ.get("DB_PATH", "/data/jielong.db")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ── 排班表解析用正規表示式
DATE_RE      = re.compile(r'(\d{1,2}/\d{1,2})\s*[（(]([一二三四五六日ㄧ零][一二三四五六日ㄧ零]?)[）)]')
COUNT_RE     = re.compile(r'(\d+)\s*人')
TIME_RE      = re.compile(r'\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}:\d{2})?')
SESSION_RE   = re.compile(r'^\s*(上午|下午)\s*[：:](.*)')
PREFILL_RE   = re.compile(r'^\s*\d+[.．、]\s*(.+\S)')  # 「1. 小白」式預填

HELP_TEXT = """📖 接龍指令說明
─────────────
報名：+編號 名字
　例：+3 王小明
　多人：+3 小明 小華
　多項：+1 +3 +5 小明
退出：退出 編號
列表：查看報名狀況
空缺：查看缺人項目

【負責人】
重新開團／結束接龍
清除 編號／移除 編號 姓名
更改 編號 舊名 新名
設定提醒 HH:MM／關閉"""


# ══════════════════════════════════════════
# 資料庫
# ══════════════════════════════════════════

def init_db():
    global DB_PATH
    # 確保資料庫目錄存在
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"[startup] 建立資料庫目錄: {db_dir}")
        except OSError as e:
            logger.warning(f"[startup] 無法建立 {db_dir}: {e}，改用當前目錄")
            DB_PATH = "jielong.db"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS lists (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id             TEXT    NOT NULL,
            title                TEXT    NOT NULL,
            creator_id           TEXT    NOT NULL,
            creator_name         TEXT,
            status               TEXT    DEFAULT 'open',
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            list_type            TEXT    DEFAULT 'simple',
            last_broadcast_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_broadcast_count INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id         INTEGER NOT NULL,
            user_id         TEXT    NOT NULL,
            user_name       TEXT,
            item            TEXT,
            quantity        TEXT,
            seq             INTEGER,
            slot_num        INTEGER,
            registered_by   TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # 預設推播設定（第一次建立時寫入，之後不覆蓋）
    defaults = [
        ("broadcast_hour",      "7"),   # 早安推播小時（0–23）
        ("broadcast_minute",    "0"),   # 早安推播分鐘
        ("allow_start",         "7"),   # 允許推播開始（含）
        ("allow_end",           "22"),  # 允許推播結束（不含）→ 22:00 後靜音
        ("activity_threshold",  "6"),   # 新增幾筆觸發即時推播
        ("interval_hours",      "6"),   # 定時推播間隔小時
        ("reminder_hour",       "12"),  # 空缺提醒小時（預設 12:00）
        ("reminder_minute",     "0"),   # 空缺提醒分鐘
        ("reminder_enabled",    "1"),   # 空缺提醒開關（1=開, 0=關）
    ]
    c.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", defaults
    )

    # 相容舊資料庫：補欄位（已存在時靜默忽略）
    for sql in [
        "ALTER TABLE lists   ADD COLUMN list_type            TEXT      DEFAULT 'simple'",
        "ALTER TABLE lists   ADD COLUMN last_broadcast_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE lists   ADD COLUMN last_broadcast_count INTEGER   DEFAULT 0",
        "ALTER TABLE entries ADD COLUMN slot_num             INTEGER",
        "ALTER TABLE entries ADD COLUMN registered_by        TEXT",
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

def get_setting(key, default=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_entry_count(list_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM entries WHERE list_id=?", (list_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def update_broadcast_state(list_id):
    """推播完成後，更新 last_broadcast_at 及 last_broadcast_count"""
    count = get_entry_count(list_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE lists SET last_broadcast_at=CURRENT_TIMESTAMP, last_broadcast_count=? WHERE id=?",
        (count, list_id),
    )
    conn.commit()
    conn.close()

def is_broadcast_allowed():
    """台灣時間在允許時段內才推播（預設 07:00–22:00）"""
    hour        = datetime.now(TZ_TAIPEI).hour
    allow_start = int(get_setting("allow_start", "7"))
    allow_end   = int(get_setting("allow_end",   "22"))
    return allow_start <= hour < allow_end

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


_TITLE_SKIP = re.compile(r'^[/]?(?:接龍|開團)\s*$|^親愛的|^大家好|^平安|^各位|^Hello|^嗨')

def _extract_title(text):
    """從排班表文字中萃取有意義的標題，跳過問候語和接龍關鍵字"""
    for line in text.strip().split("\n")[:12]:
        line = line.strip()
        if not line or DATE_RE.search(line):
            continue
        if _TITLE_SKIP.search(line):
            continue
        title = re.sub(r'[：:如下\s]+$', '', line).strip()
        if title:
            return title
    return "工作認養排班"


def parse_schedule_slots(text):
    """
    解析工作認養排班表，回傳 (slots, prefilled)。
    - slots:     list of slot dicts
    - prefilled: {slot_num: [name, ...]}  ← 排班表中已填寫的姓名
    支援兩種預填格式：
      「上午 : 小珍」→ session 預填
      「1. 小白」    → 編號列表預填
    """
    slots     = []
    prefilled = {}   # slot_num → [name, ...]
    slot_num  = 1
    lines     = text.split("\n")

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

        activity      = after.strip()
        sessions      = []   # 收集到的 session 名稱 ['上午','下午']
        session_names = {}   # {'上午': '小珍', '下午': '小明'}
        note_parts    = []
        prefill_names = []   # 編號列表預填：['小白']

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
                sess      = sm.group(1)
                name_part = sm.group(2).strip().lstrip(':：').strip()
                if sess not in sessions:
                    sessions.append(sess)
                if name_part:
                    session_names[sess] = name_part
            elif TIME_RE.search(nl) and not time_str:
                time_str = nl.strip()
            else:
                pm = PREFILL_RE.match(nl)
                if pm:
                    name = pm.group(1).strip()
                    if name:
                        prefill_names.append(name)
                else:
                    note_parts.append(nl)
            j += 1

        note = " ".join(note_parts).strip()

        if sessions:
            # 有上午/下午 → 只建出現在文字中的 session slot
            for sess in ["上午", "下午"]:
                if sess not in sessions:
                    continue
                sn = slot_num
                slots.append({
                    "slot_num":       sn,
                    "date_str":       date_str,
                    "day_str":        day_str,
                    "activity":       activity,
                    "time_str":       time_str,
                    "session":        sess,
                    "required_count": required,
                    "note":           note,
                })
                if sess in session_names:
                    prefilled[sn] = [session_names[sess]]
                slot_num += 1
        else:
            sn = slot_num
            slots.append({
                "slot_num":       sn,
                "date_str":       date_str,
                "day_str":        day_str,
                "activity":       activity,
                "time_str":       time_str,
                "session":        None,
                "required_count": required,
                "note":           note,
            })
            if prefill_names:
                prefilled[sn] = prefill_names
            slot_num += 1

        i = j

    return slots, prefilled


# ══════════════════════════════════════════
# 格式化顯示
# ══════════════════════════════════════════

def _is_strict_slot(slot):
    """判斷此項目是否嚴格限制人數（只有「值班」類工作才限額）"""
    activity = (slot[5] or "").lower()
    return "值班" in activity


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
        header   = f"【{slot_num}】{_slot_label(s)}"
        names = signups.get(slot_num, [])
        current = len(names)
        if required > 1:
            header += f"（{current}/{required}人）"
        lines.append(header)
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
# 推播核心
# ══════════════════════════════════════════

def _is_all_filled(lst):
    """判斷接龍是否所有工作都已認領完畢（不需要再推播）"""
    if _list_type(lst) != "schedule":
        return False  # 簡易接龍無法判斷，持續推播
    slots   = get_slots(lst[0])
    signups = get_slot_signups(lst[0])
    for s in slots:
        sn       = s[2]
        required = s[8]
        current  = len(signups.get(sn, []))
        if _is_strict_slot(s) and current < required:
            return False
        if not _is_strict_slot(s) and current == 0:
            return False
    return True


def _push_list(lst, prefix=""):
    """對單一接龍推播名單，成功後更新推播狀態"""
    group_id = lst[1]
    ltype    = _list_type(lst)

    if ltype == "schedule":
        slots   = get_slots(lst[0])
        signups = get_slot_signups(lst[0])
        body    = format_schedule_list(lst, slots, signups, show_time=True)
    else:
        entries = get_entries(lst[0])
        body    = format_list(lst, entries, show_time=True)

    message = f"{prefix}\n\n{body}".strip() if prefix else body
    try:
        line_bot_api.push_message(group_id, TextSendMessage(text=message))
        logger.info(f"[broadcast] 推播至 {group_id}：{lst[2]}")
        update_broadcast_state(lst[0])
    except Exception as e:
        logger.error(f"[broadcast] 推播失敗 {group_id}：{e}")


def daily_broadcast():
    """每天 07:00 早安推播"""
    active_lists = get_all_active_lists()
    if not active_lists:
        logger.info("[排程] 目前沒有進行中的接龍，跳過推播")
        return

    now_str = datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d")
    logger.info(f"[排程] 早安推播 {len(active_lists)} 個接龍")
    prefix = f"📣 早安！以下是今日工作認養名單（{now_str}）"
    for lst in active_lists:
        if _is_all_filled(lst):
            logger.info(f"[排程] 全部認領完畢，跳過推播：{lst[2]}")
            continue
        _push_list(lst, prefix)


def check_timed_broadcast():
    """每小時執行：距上次推播已超過 6 小時且在允許時段內，則推播"""
    if not is_broadcast_allowed():
        return

    active_lists = get_all_active_lists()
    now = datetime.now(TZ_TAIPEI)

    for lst in active_lists:
        if _is_all_filled(lst):
            continue
        last_at_str = lst[8]  # last_broadcast_at
        if last_at_str:
            try:
                last_at = datetime.strptime(last_at_str, "%Y-%m-%d %H:%M:%S")
                last_at = pytz.utc.localize(last_at).astimezone(TZ_TAIPEI)
            except Exception:
                last_at = now  # 解析失敗則跳過
        else:
            last_at = now - timedelta(hours=7)  # None → 視為很久以前

        interval = float(get_setting("interval_hours", "6"))
        elapsed_hours = (now - last_at).total_seconds() / 3600
        if elapsed_hours >= interval:
            logger.info(f"[排程] 6 小時定時推播：{lst[2]}")
            _push_list(lst, "📋 定時更新")


def _check_all_filled_notify(list_id, group_id, lst=None):
    """報名後檢查：全部認領完畢時推播通知"""
    if lst is None:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM lists WHERE id=?", (list_id,))
        lst = c.fetchone()
        conn.close()
    if not lst or lst[5] != "open":
        return
    if not _is_all_filled(lst):
        return

    logger.info(f"[通知] 全部認領完畢：{lst[2]}")
    slots   = get_slots(list_id)
    signups = get_slot_signups(list_id)
    body    = format_schedule_list(lst, slots, signups, show_time=True)
    total   = sum(len(v) for v in signups.values())
    message = f"🎉 所有工作都已認領完畢！\n\n{body}\n\n共 {total} 人報名"
    try:
        line_bot_api.push_message(group_id, TextSendMessage(text=message))
    except Exception as e:
        logger.error(f"[通知] 推播失敗 {group_id}: {e}")


def vacancy_reminder():
    """定時推播：各群組尚未額滿的工作項目"""
    if not is_broadcast_allowed():
        return
    if get_setting("reminder_enabled", "1") != "1":
        return

    active_lists = get_all_active_lists()
    if not active_lists:
        return

    logger.info("[提醒] 開始推播空缺項目")
    for lst in active_lists:
        if _list_type(lst) != "schedule":
            continue

        list_id = lst[0]
        slots   = get_slots(list_id)
        signups = get_slot_signups(list_id)

        unfilled = []
        for s in slots:
            sn       = s[2]
            required = s[8]
            current  = len(signups.get(sn, []))
            if _is_strict_slot(s) and current < required:
                unfilled.append((s, current, required))
            elif not _is_strict_slot(s) and current == 0:
                unfilled.append((s, current, required))

        if not unfilled:
            continue

        lines = [f"📢 {lst[2]}", "以下項目尚有空缺，歡迎認養！", "─" * 16]
        for s, current, required in unfilled:
            sn    = s[2]
            label = f"【{sn}】{_slot_label(s)}"
            if required > 1:
                label += f"  （{current}/{required}人）"
            lines.append(label)
        lines.append("─" * 16)
        # (說明已移至「接龍說明」指令)

        try:
            line_bot_api.push_message(lst[1], TextSendMessage(text="\n".join(lines)))
            logger.info(f"[提醒] 已推播至 {lst[1]}：{len(unfilled)} 項空缺")
        except Exception as e:
            logger.error(f"[提醒] 推播失敗 {lst[1]}: {e}")


def _parse_slot_date(date_str):
    """將 slot 的 date_str（如 '3/1'）解析為 date 物件（自動判斷年份）"""
    try:
        now = datetime.now(TZ_TAIPEI)
        m, d = date_str.split("/")
        dt = now.replace(month=int(m), day=int(d)).date()
        # 如果日期已過超過半年，推測為明年
        if dt < now.date() - timedelta(days=180):
            dt = dt.replace(year=now.year + 1)
        return dt
    except Exception:
        return None


def weekly_reminder():
    """每週日推播：下週的工作排班總覽"""
    active_lists = get_all_active_lists()
    if not active_lists:
        return

    now = datetime.now(TZ_TAIPEI).date()
    # 計算下週一到下週日
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 1  # 今天是週日，下週一是明天
    next_monday = now + timedelta(days=days_until_monday)
    next_sunday = next_monday + timedelta(days=6)

    logger.info(f"[週報] 下週範圍: {next_monday} ~ {next_sunday}")

    for lst in active_lists:
        if _list_type(lst) != "schedule":
            continue

        list_id = lst[0]
        slots   = get_slots(list_id)
        signups = get_slot_signups(list_id)

        # 篩選下週的工作項目
        next_week_slots = []
        for s in slots:
            dt = _parse_slot_date(s[3])
            if dt and next_monday <= dt <= next_sunday:
                next_week_slots.append(s)

        if not next_week_slots:
            continue

        lines = [
            f"📅 下週工作預告（{next_monday.strftime('%m/%d')}–{next_sunday.strftime('%m/%d')}）",
            f"📋 {lst[2]}",
            "─" * 16,
        ]
        for s in next_week_slots:
            sn       = s[2]
            required = s[8]
            names    = signups.get(sn, [])
            current  = len(names)
            label    = f"【{sn}】{_slot_label(s)}"
            if required > 1:
                label += f"（{current}/{required}人）"

            if names:
                label += f"\n   👤 {'、'.join(names)}"
            else:
                label += "\n   ⚠️ 尚無人報名"

            lines.append(label)

        lines.append("─" * 16)
        # (說明已移至「接龍說明」指令)

        try:
            line_bot_api.push_message(lst[1], TextSendMessage(text="\n".join(lines)))
            logger.info(f"[週報] 已推播至 {lst[1]}：{len(next_week_slots)} 項")
        except Exception as e:
            logger.error(f"[週報] 推播失敗 {lst[1]}: {e}")


# ══════════════════════════════════════════
# 指令處理
# ══════════════════════════════════════════

def cmd_post_schedule(group_id, user_id, user_name, text):
    """解析排班表並建立排班型接龍（有進行中的接龍時，僅負責人可重建）"""
    # 檢查是否有進行中的接龍
    existing = get_active_list(group_id)
    if existing and existing[3] != user_id:
        creator_name = existing[4] or "負責人"
        return f"⚠️ 目前已有進行中的接龍「{existing[2]}」\n只有負責人（{creator_name}）可以重建排班表。"

    slots, prefilled = parse_schedule_slots(text)
    if not slots:
        return "找不到日期資料，無法建立排班表。請確認格式如：3/1（日）活動名稱"

    title = _extract_title(text)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 讀取舊的報名資料（用於重建時保留）
    old_signups = {}  # {slot_num: [(user_id, user_name, registered_by), ...]}
    carried_count = 0
    if existing and _list_type(existing) == "schedule":
        old_list_id = existing[0]
        c.execute(
            "SELECT slot_num, user_id, user_name, registered_by FROM entries WHERE list_id=? AND slot_num IS NOT NULL",
            (old_list_id,),
        )
        for row in c.fetchall():
            old_signups.setdefault(row[0], []).append((row[1], row[2], row[3]))

    c.execute('UPDATE lists SET status="closed" WHERE group_id=? AND status="open"', (group_id,))
    c.execute(
        "INSERT INTO lists (group_id, title, creator_id, creator_name, list_type, last_broadcast_at, last_broadcast_count)"
        " VALUES (?, ?, ?, ?, 'schedule', CURRENT_TIMESTAMP, 0)",
        (group_id, title, user_id, user_name),
    )
    list_id = c.lastrowid

    new_slot_nums = set()
    for s in slots:
        new_slot_nums.add(s["slot_num"])
        c.execute(
            "INSERT INTO slots (list_id,slot_num,date_str,day_str,activity,time_str,session,required_count,note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (list_id, s["slot_num"], s["date_str"], s["day_str"], s["activity"],
             s["time_str"], s["session"], s["required_count"], s["note"]),
        )

    # 將排班表中已填寫的姓名預先寫入 entries
    for sn, names in prefilled.items():
        for name in names:
            proxy_uid = f"__prefill__{sn}__{name}"
            c.execute(
                "INSERT INTO entries (list_id, user_id, user_name, slot_num, seq, registered_by)"
                " VALUES (?, ?, ?, ?, ?, '__prefilled__')",
                (list_id, proxy_uid, name, sn, sn),
            )

    # 保留舊報名（同編號的項目，且不是預填的重複姓名）
    for sn, entries in old_signups.items():
        if sn not in new_slot_nums:
            continue
        for uid, uname, reg_by in entries:
            # 避免與新預填資料重複
            c.execute(
                "SELECT id FROM entries WHERE list_id=? AND user_name=? AND slot_num=?",
                (list_id, uname, sn),
            )
            if c.fetchone():
                continue
            c.execute(
                "INSERT INTO entries (list_id, user_id, user_name, slot_num, seq, registered_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (list_id, uid, uname, sn, sn, reg_by),
            )
            carried_count += 1

    conn.commit()
    conn.close()

    is_rebuild = bool(old_signups)
    header = "🔄 排班表已重建！" if is_rebuild else "✅ 排班表已建立！"
    lines = [f"{header}\n📋 {title}\n共 {len(slots)} 個工作項目"]
    if carried_count > 0:
        lines.append(f"📌 已保留 {carried_count} 筆報名紀錄")
    lines.append("─────────────────")
    for s in slots:
        sn    = s["slot_num"]
        label = f"【{sn}】{s['date_str']}（{s['day_str']}）{s['activity']}"
        if s["session"]:
            label += f" {s['session']}"
        if s["time_str"]:
            label += f" {s['time_str']}"
        if s["required_count"] > 1:
            label += f" {s['required_count']}人"
        # 顯示預填姓名
        if sn in prefilled:
            label += f"  ✓ {'、'.join(prefilled[sn])}"
        lines.append(label)
    lines.append("")
    return "\n".join(lines)


def cmd_open(group_id, user_id, user_name, text):
    """簡易接龍"""
    m = re.match(r"[/]?(?:接龍|開團)\s*(.*)", text)
    title = (m.group(1).strip() if m else "").strip() or "工作接龍"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE lists SET status="closed" WHERE group_id=? AND status="open"', (group_id,))
    c.execute(
        "INSERT INTO lists (group_id, title, creator_id, creator_name, last_broadcast_at, last_broadcast_count)"
        " VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 0)",
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
    """排班模式：+3 小明 → 報名第 3 號工作（支援 +3 小明 小華 家和 多人報名）"""
    list_id = active[0]

    m = re.match(r"\+(\d+)\s*(.*)", text)
    if not m:
        return "請輸入 + 編號 空格 你的名字\n\n例如：+3 王小明\n\n先輸入「列表」看有哪些工作可以報名"

    slot_num  = int(m.group(1))
    name_part = m.group(2).strip()
    names     = name_part.split() if name_part else [user_name or "（未知）"]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 確認 slot 存在
    c.execute("SELECT * FROM slots WHERE list_id=? AND slot_num=?", (list_id, slot_num))
    slot = c.fetchone()
    if not slot:
        conn.close()
        return f"找不到第 {slot_num} 號工作項目。\n\n請先輸入「列表」查看有哪些工作可以報名。"

    required = slot[8]

    # 單人報名走簡化流程
    if len(names) == 1:
        name = names[0]
        c.execute(
            "SELECT id FROM entries WHERE list_id=? AND user_name=? AND slot_num=?",
            (list_id, name, slot_num),
        )
        existing = c.fetchone()
        if existing:
            conn.close()
            return f"⚠️ {name} 已報名 {slot_num}. {_slot_label(slot)}"

        if _is_strict_slot(slot):
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
        _check_all_filled_notify(list_id, group_id, active)
        return f"✅ 報名成功！\n【{slot_num}】{_slot_label(slot)} → {name}\n\n輸入「列表」可查看完整名單"

    # 多人報名
    results = []
    any_inserted = False
    for name in names:
        c.execute(
            "SELECT id FROM entries WHERE list_id=? AND user_name=? AND slot_num=?",
            (list_id, name, slot_num),
        )
        if c.fetchone():
            results.append(f"⚠️ {name}（已報名）")
            continue

        if _is_strict_slot(slot):
            c.execute(
                "SELECT COUNT(*) FROM entries WHERE list_id=? AND slot_num=?",
                (list_id, slot_num),
            )
            if c.fetchone()[0] >= required:
                results.append(f"❌ {name}（已額滿）")
                continue

        c.execute(
            "INSERT INTO entries (list_id, user_id, user_name, slot_num, seq) VALUES (?, ?, ?, ?, ?)",
            (list_id, user_id, name, slot_num, slot_num),
        )
        results.append(f"✅ {name}")
        any_inserted = True

    conn.commit()
    conn.close()
    if any_inserted:
        _check_all_filled_notify(list_id, group_id, active)

    header = f"📋 【{slot_num}】{_slot_label(slot)} 報名結果："
    return header + "\n" + "\n".join(results)


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
    return reply + "\n（輸入「列表」隨時查看）"


def cmd_join_multi(group_id, user_id, user_name, text):
    """多項報名：+1 +3 +5 小明 小華 — 多人一次報名多個工作"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"
    if _list_type(active) != "schedule":
        return "多項報名只適用於排班模式。\n格式：+1 +3 +5 你的名字"

    slot_nums = [int(x) for x in re.findall(r'\+(\d+)', text)]
    name_part = re.sub(r'\+\d+', '', text).strip()
    names = name_part.split() if name_part else [user_name or "（未知）"]

    list_id = active[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    results = []
    any_inserted = False

    for name in names:
        for slot_num in slot_nums:
            c.execute("SELECT * FROM slots WHERE list_id=? AND slot_num=?", (list_id, slot_num))
            slot = c.fetchone()
            if not slot:
                results.append(f"❌ {name}：第 {slot_num} 號不存在")
                continue

            required = slot[8]

            # 同一姓名重複報名 → 跳過
            c.execute(
                "SELECT id FROM entries WHERE list_id=? AND user_name=? AND slot_num=?",
                (list_id, name, slot_num),
            )
            if c.fetchone():
                results.append(f"⚠️ {name}：【{slot_num}】已報名")
                continue

            # 額滿檢查（僅值班類工作限額）
            if _is_strict_slot(slot):
                c.execute(
                    "SELECT COUNT(*) FROM entries WHERE list_id=? AND slot_num=?",
                    (list_id, slot_num),
                )
                if c.fetchone()[0] >= required:
                    results.append(f"❌ {name}：【{slot_num}】已額滿（{required}人）")
                    continue

            c.execute(
                "INSERT INTO entries (list_id, user_id, user_name, slot_num, seq) VALUES (?, ?, ?, ?, ?)",
                (list_id, user_id, name, slot_num, slot_num),
            )
            results.append(f"✅ {name}：【{slot_num}】{_slot_label(slot)}")
            any_inserted = True

    conn.commit()
    conn.close()

    if any_inserted:
        _check_all_filled_notify(list_id, group_id, active)

    name_display = "、".join(names)
    return f"📋 {name_display} 報名結果：\n" + "\n".join(results)


def cmd_proxy_join(group_id, user_id, user_name, text):
    """幫報 [編號] [姓名] — 代替他人報名（排班模式）"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"
    if _list_type(active) != "schedule":
        return "幫報功能只適用於排班模式。"

    m = re.match(r"幫報\s+(\d+)\s+(.+)", text)
    if not m:
        return "格式：幫報 [編號] [姓名]\n例：幫報 3 小明"

    list_id  = active[0]
    slot_num = int(m.group(1))
    name     = m.group(2).strip()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT * FROM slots WHERE list_id=? AND slot_num=?", (list_id, slot_num))
    slot = c.fetchone()
    if not slot:
        conn.close()
        return f"找不到第 {slot_num} 號工作項目。\n\n請先輸入「列表」查看有哪些工作可以報名。"

    required = slot[8]

    # 同一姓名已在此 slot → 提示重複
    c.execute(
        "SELECT id FROM entries WHERE list_id=? AND slot_num=? AND user_name=?",
        (list_id, slot_num, name),
    )
    if c.fetchone():
        conn.close()
        return f"❌ {name} 已在第 {slot_num} 號工作中了。"

    # 檢查額滿（僅值班類工作限額）
    if _is_strict_slot(slot):
        c.execute("SELECT COUNT(*) FROM entries WHERE list_id=? AND slot_num=?", (list_id, slot_num))
        if c.fetchone()[0] >= required:
            conn.close()
            return f"❌ 第 {slot_num} 號已額滿（{required} 人）！"

    # 用特殊 user_id 避免跟操作者自己的報名衝突
    proxy_uid = f"__proxy__{slot_num}__{name}"
    c.execute(
        "INSERT INTO entries (list_id, user_id, user_name, slot_num, seq, registered_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (list_id, proxy_uid, name, slot_num, slot_num, user_id),
    )
    conn.commit()
    conn.close()
    _check_all_filled_notify(list_id, group_id)
    operator = user_name or "代報者"
    return f"✅ 已代替 {name} 報名！\n【{slot_num}】{_slot_label(slot)} → {name}\n（由 {operator} 代報）"


def cmd_show_settings():
    """推播設定 — 顯示目前所有推播設定"""
    h   = get_setting("broadcast_hour",    "7")
    m   = get_setting("broadcast_minute",  "0")
    a1  = get_setting("allow_start",       "7")
    a2  = get_setting("allow_end",         "22")
    th  = get_setting("activity_threshold","6")
    iv  = get_setting("interval_hours",    "6")
    rh  = get_setting("reminder_hour",     "12")
    rm  = get_setting("reminder_minute",   "0")
    ren = get_setting("reminder_enabled",  "1")
    reminder_status = f"每天 {int(rh):02d}:{int(rm):02d}" if ren == "1" else "已關閉"
    return (
        f"📋 目前推播設定\n"
        f"─────────────────\n"
        f"⏰ 早安推播：每天 {int(h):02d}:{int(m):02d}\n"
        f"🔔 空缺提醒：{reminder_status}\n"
        f"🔇 靜音時段：{int(a2):02d}:00 – {int(a1):02d}:00\n"
        f"📊 活動門檻：新增 {th} 筆報名即推播\n"
        f"🕐 定時間隔：每 {iv} 小時推播一次\n"
        f"─────────────────\n"
        f"修改指令：\n"
        f"設定推播 08:00      — 改早安時間\n"
        f"設定提醒 12:00      — 改空缺提醒時間\n"
        f"設定提醒 關閉       — 關閉空缺提醒\n"
        f"設定靜音 23 7       — 改靜音時段\n"
        f"設定推播門檻 10     — 改活動觸發門檻\n"
        f"設定推播間隔 4      — 改定時間隔（小時）"
    )


def cmd_set_broadcast_time(text):
    """設定推播 HH:MM — 修改早安推播時間並即時生效"""
    m = re.match(r"設定推播\s+(\d{1,2})(?:[：:](\d{2}))?$", text)
    if not m:
        return "格式：設定推播 HH:MM\n例：設定推播 08:00\n例：設定推播 7"
    hour   = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "時間格式錯誤，小時 0–23，分鐘 0–59"

    set_setting("broadcast_hour",   hour)
    set_setting("broadcast_minute", minute)

    # 即時更新 scheduler
    if _scheduler:
        try:
            _scheduler.reschedule_job(
                "daily_broadcast",
                trigger="cron", hour=hour, minute=minute,
            )
            logger.info(f"[設定] 早安推播已更新為 {hour:02d}:{minute:02d}")
        except Exception as e:
            logger.error(f"[設定] reschedule 失敗: {e}")

    return f"✅ 早安推播已更新為 每天 {hour:02d}:{minute:02d}（台灣時間）\n無需重新部署，立即生效。"


def cmd_set_quiet(text):
    """設定靜音 HH HH — 修改靜音時段（靜音開始 靜音結束）"""
    m = re.match(r"設定靜音\s+(\d{1,2})\s+(\d{1,2})$", text)
    if not m:
        return "格式：設定靜音 [靜音開始小時] [靜音結束小時]\n例：設定靜音 22 7\n（表示 22:00 至隔天 07:00 靜音）"
    end_quiet   = int(m.group(1))  # allow_end（靜音開始）
    start_allow = int(m.group(2))  # allow_start（靜音結束 = 推播開始）
    if not (0 <= end_quiet <= 23 and 0 <= start_allow <= 23):
        return "小時需在 0–23 之間"

    set_setting("allow_end",   end_quiet)
    set_setting("allow_start", start_allow)
    return f"✅ 靜音時段已更新：{end_quiet:02d}:00 – {start_allow:02d}:00（台灣時間）\n立即生效。"


def cmd_set_threshold(text):
    """設定推播門檻 N — 修改活動觸發推播的新增筆數"""
    m = re.match(r"設定推播門檻\s+(\d+)$", text)
    if not m:
        return "格式：設定推播門檻 [筆數]\n例：設定推播門檻 10"
    n = int(m.group(1))
    if n < 1:
        return "門檻至少為 1"
    set_setting("activity_threshold", n)
    return f"✅ 活動觸發門檻已更新為 {n} 筆新增報名。\n立即生效。"


def cmd_set_interval(text):
    """設定推播間隔 N — 修改定時推播間隔小時"""
    m = re.match(r"設定推播間隔\s+(\d+(?:\.\d+)?)$", text)
    if not m:
        return "格式：設定推播間隔 [小時]\n例：設定推播間隔 4"
    n = float(m.group(1))
    if n < 1:
        return "間隔至少為 1 小時"
    set_setting("interval_hours", n)
    return f"✅ 定時推播間隔已更新為 {n} 小時。\n立即生效。"


def cmd_set_reminder(text):
    """設定提醒 HH:MM 或 設定提醒 關閉 — 修改空缺提醒時間"""
    # 關閉
    if re.match(r"設定提醒\s*(關閉|停用|off)$", text, re.IGNORECASE):
        set_setting("reminder_enabled", "0")
        if _scheduler:
            try:
                _scheduler.pause_job("vacancy_reminder")
            except Exception:
                pass
        return "✅ 空缺提醒已關閉。\n輸入「設定提醒 12:00」可重新開啟。"

    m = re.match(r"設定提醒\s+(\d{1,2})(?:[：:](\d{2}))?$", text)
    if not m:
        return "格式：設定提醒 HH:MM\n例：設定提醒 12:00\n或：設定提醒 關閉"

    hour   = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "時間格式錯誤，小時 0–23，分鐘 0–59"

    set_setting("reminder_hour",    hour)
    set_setting("reminder_minute",  minute)
    set_setting("reminder_enabled", "1")

    if _scheduler:
        try:
            _scheduler.reschedule_job(
                "vacancy_reminder",
                trigger="cron", hour=hour, minute=minute,
            )
        except Exception as e:
            logger.error(f"[設定] reminder reschedule 失敗: {e}")

    return (
        f"✅ 空缺提醒已設定為每天 {hour:02d}:{minute:02d}（台灣時間）\n"
        f"只推播尚未額滿的項目。立即生效。"
    )


def cmd_clear_slot(group_id, user_id, text):
    """清除 [編號] — 負責人清除某項目的所有報名"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"
    if active[3] != user_id:
        creator_name = active[4] or "負責人"
        return f"⚠️ 只有負責人（{creator_name}）才能清除項目。"
    if _list_type(active) != "schedule":
        return "此功能僅適用於排班模式。"

    m = re.match(r"清除\s+(\d+)", text)
    slot_num = int(m.group(1))
    list_id  = active[0]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT * FROM slots WHERE list_id=? AND slot_num=?", (list_id, slot_num))
    slot = c.fetchone()
    if not slot:
        conn.close()
        return f"找不到第 {slot_num} 號工作項目。"

    c.execute(
        "SELECT user_name FROM entries WHERE list_id=? AND slot_num=?",
        (list_id, slot_num),
    )
    names = [r[0] for r in c.fetchall()]

    if not names:
        conn.close()
        return f"【{slot_num}】{_slot_label(slot)} 目前沒有人報名。"

    c.execute("DELETE FROM entries WHERE list_id=? AND slot_num=?", (list_id, slot_num))
    conn.commit()
    conn.close()

    return (
        f"🗑️ 已清除【{slot_num}】{_slot_label(slot)} 的所有報名\n"
        f"移除 {len(names)} 人：{'、'.join(names)}\n\n"
        f"現在可以重新報名此項目。"
    )


def cmd_admin_remove(group_id, user_id, text):
    """移除 [編號] [姓名] — 開團者移除指定人員"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"
    if active[3] != user_id:
        return "❌ 只有開團者可以使用此指令。"

    m = re.match(r"移除\s+(\d+)\s+(.+)", text)
    if not m:
        return "格式：移除 [編號] [姓名]\n例：移除 3 小明"

    list_id  = active[0]
    slot_num = int(m.group(1))
    name     = m.group(2).strip()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM entries WHERE list_id=? AND slot_num=? AND user_name=?",
        (list_id, slot_num, name),
    )
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected:
        return f"✅ 已移除：第 {slot_num} 號 {name}"
    else:
        return f"找不到第 {slot_num} 號中的「{name}」。"


def cmd_admin_rename(group_id, user_id, text):
    """更改 [編號] [舊名] [新名] — 開團者修改報名者姓名"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"
    if active[3] != user_id:
        return "❌ 只有開團者可以使用此指令。"

    m = re.match(r"更改\s+(\d+)\s+(\S+)\s+(\S+)", text)
    if not m:
        return "格式：更改 [編號] [舊名] [新名]\n例：更改 3 小明 小美"

    list_id  = active[0]
    slot_num = int(m.group(1))
    old_name = m.group(2).strip()
    new_name = m.group(3).strip()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE entries SET user_name=? WHERE list_id=? AND slot_num=? AND user_name=?",
        (new_name, list_id, slot_num, old_name),
    )
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected:
        return f"✅ 已修改：第 {slot_num} 號 {old_name} → {new_name}"
    else:
        return f"找不到第 {slot_num} 號中的「{old_name}」。"


def cmd_vacancy(group_id):
    """手動查詢尚未認領的工作項目"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    if _list_type(active) != "schedule":
        return "此功能僅適用於排班模式的接龍。"

    list_id = active[0]
    slots   = get_slots(list_id)
    signups = get_slot_signups(list_id)

    unfilled = []
    for s in slots:
        sn       = s[2]
        required = s[8]
        current  = len(signups.get(sn, []))
        if _is_strict_slot(s) and current < required:
            unfilled.append((s, current, required))
        elif not _is_strict_slot(s) and current == 0:
            unfilled.append((s, current, required))

    if not unfilled:
        return f"🎉 {active[2]}\n\n所有工作都已認領完畢！"

    lines = [f"📋 {active[2]}", "以下項目尚未認領，歡迎報名！", "─" * 16]
    for s, current, required in unfilled:
        sn    = s[2]
        label = f"【{sn}】{_slot_label(s)}"
        if required > 1:
            label += f"  （{current}/{required}人）"
        lines.append(label)
    lines.append("─" * 16)
    lines.append(f"共 {len(unfilled)} 項空缺")
    return "\n".join(lines)


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

    # 只有發起人才能結束接龍
    creator_id = active[3]
    if user_id != creator_id:
        creator_name = active[4] or "發起人"
        return f"⚠️ 只有發起人（{creator_name}）才能結束接龍。"

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


def cmd_restart(group_id, user_id):
    """重新開團 — 負責人結束目前接龍，用相同排班表重新開團（清除所有報名）"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    if active[3] != user_id:
        creator_name = active[4] or "負責人"
        return f"⚠️ 只有負責人（{creator_name}）才能重新開團。"

    if _list_type(active) != "schedule":
        return "此功能僅適用於排班模式的接龍。"

    old_list_id = active[0]
    title       = active[2]
    creator_id  = active[3]
    creator_name = active[4]

    # 讀取舊的 slots
    old_slots = get_slots(old_list_id)
    if not old_slots:
        return "找不到排班資料，無法重新開團。"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 關閉舊的
    c.execute('UPDATE lists SET status="closed" WHERE id=?', (old_list_id,))

    # 建立新的（相同排班，不帶報名）
    c.execute(
        "INSERT INTO lists (group_id, title, creator_id, creator_name, list_type, last_broadcast_at, last_broadcast_count)"
        " VALUES (?, ?, ?, ?, 'schedule', CURRENT_TIMESTAMP, 0)",
        (group_id, title, creator_id, creator_name),
    )
    new_list_id = c.lastrowid

    for s in old_slots:
        c.execute(
            "INSERT INTO slots (list_id,slot_num,date_str,day_str,activity,time_str,session,required_count,note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (new_list_id, s[2], s[3], s[4], s[5], s[6], s[7], s[8], s[9]),
        )

    conn.commit()
    conn.close()

    lines = [f"🔄 已重新開團！\n📋 {title}\n共 {len(old_slots)} 個工作項目（報名已清除）", "─" * 16]
    for s in old_slots:
        sn = s[2]
        label = f"【{sn}】{_slot_label(s)}"
        if s[8] > 1:
            label += f"（共{s[8]}人）"
        lines.append(label)
    lines.append("─" * 16)
    return "\n".join(lines)


def cmd_leave(group_id, user_id, user_name, text=""):
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    list_id = active[0]

    # 排班模式支援「退出 3」或「退出 3 小明」取消特定項目
    slot_match = re.match(r"(?:退出|取消)\s+(\d+)\s*(.*)", text)
    if _list_type(active) == "schedule" and slot_match:
        slot_num = int(slot_match.group(1))
        name     = slot_match.group(2).strip() or user_name
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # 先用姓名找，找不到再用 user_id
        c.execute(
            "DELETE FROM entries WHERE list_id=? AND slot_num=? AND user_name=?",
            (list_id, slot_num, name),
        )
        if c.rowcount == 0:
            c.execute(
                "DELETE FROM entries WHERE list_id=? AND user_id=? AND slot_num=?",
                (list_id, user_id, slot_num),
            )
        affected = c.rowcount
        conn.commit()
        conn.close()
        if affected:
            return f"✅ 已取消 {name} 在第 {slot_num} 號工作的報名。"
        else:
            return f"找不到 {name} 在第 {slot_num} 號的報名紀錄。"

    # 預設：移除該用戶所有報名（用 user_name 或 user_id）
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if _list_type(active) == "schedule":
        # 用姓名找
        c.execute(
            "SELECT DISTINCT slot_num FROM entries WHERE list_id=? AND user_name=?",
            (list_id, user_name),
        )
        slot_nums = [r[0] for r in c.fetchall()]
        if slot_nums:
            c.execute("DELETE FROM entries WHERE list_id=? AND user_name=?", (list_id, user_name))
        else:
            # fallback 用 user_id
            c.execute(
                "SELECT DISTINCT slot_num FROM entries WHERE list_id=? AND user_id=?",
                (list_id, user_id),
            )
            slot_nums = [r[0] for r in c.fetchall()]
            if slot_nums:
                c.execute("DELETE FROM entries WHERE list_id=? AND user_id=?", (list_id, user_id))
        conn.commit()
        conn.close()
        if not slot_nums:
            return "找不到你的報名紀錄。"
        return f"✅ 已取消 {user_name} 在第 {', '.join(str(s) for s in slot_nums)} 號的報名。"
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

    # ── 多項報名（+1 +3 +5 姓名）
    elif len(re.findall(r'\+\d+', text)) > 1:
        reply = cmd_join_multi(gid, uid, lazy_name(), text)

    # ── 加入（+N 或 +N 姓名）
    elif re.match(r"\+\d+(\s|$)", text):
        reply = cmd_join(gid, uid, lazy_name(), text)

    # ── 加入（N. 姓名 格式，與列表顯示一致）
    elif re.match(r"^\d+[\.．]\s*\S", text):
        m_dot = re.match(r"^(\d+)[\.．]\s*(.*)", text)
        reply = cmd_join(gid, uid, lazy_name(), f"+{m_dot.group(1)} {m_dot.group(2).strip()}")

    # ── 查看名單
    elif text in ("列表", "/列表", "查看", "名單"):
        reply = cmd_list(gid)

    # ── 查看空缺
    elif text in ("空缺", "缺人", "未認領", "誰沒報"):
        reply = cmd_vacancy(gid)

    # ── 重新開團（負責人清除報名重來）
    elif text in ("重新開團", "重開", "/重新開團"):
        try:
            reply = cmd_restart(gid, uid)
        except Exception as e:
            logger.error(f"[cmd_restart] 錯誤: {e}")
            reply = "⚠️ 重新開團失敗，請稍後再試。"

    # ── 結束
    elif text in ("結束接龍", "結團", "/結束接龍", "/結團", "關閉接龍"):
        reply = cmd_close(gid, uid)

    # ── 退出（支援「退出 3」或「退出 3 小明」取消特定項目）
    elif re.match(r"(退出|取消)(\s+\d+.*)?$", text):
        reply = cmd_leave(gid, uid, lazy_name(), text)

    # ── 負責人清除整個項目（清除 3）
    elif re.match(r"清除\s+\d+$", text):
        reply = cmd_clear_slot(gid, uid, text)

    # ── 幫報（代替他人報名）
    elif re.match(r"幫報\s+\d+\s+\S", text):
        reply = cmd_proxy_join(gid, uid, lazy_name(), text)

    # ── 開團者：移除指定人員
    elif re.match(r"移除\s+\d+\s+\S", text):
        reply = cmd_admin_remove(gid, uid, text)

    # ── 開團者：修改姓名
    elif re.match(r"更改\s+\d+\s+\S+\s+\S", text):
        reply = cmd_admin_rename(gid, uid, text)

    # ── 推播設定
    elif text in ("推播設定", "/推播設定"):
        reply = cmd_show_settings()

    elif re.match(r"設定推播\s+\d", text):
        reply = cmd_set_broadcast_time(text)

    elif re.match(r"設定靜音\s+\d+\s+\d+$", text):
        reply = cmd_set_quiet(text)

    elif re.match(r"設定推播門檻\s+\d+$", text):
        reply = cmd_set_threshold(text)

    elif re.match(r"設定推播間隔\s+\d", text):
        reply = cmd_set_interval(text)

    elif re.match(r"設定提醒(\s|$)", text):
        reply = cmd_set_reminder(text)

    # ── 說明（只有負責人輸入「接龍說明」才顯示）
    elif text in ("接龍說明",):
        active = get_active_list(group_id)
        if active and active[3] == user_id:
            reply = HELP_TEXT
        elif not active:
            reply = HELP_TEXT
        else:
            reply = "此指令僅限負責人使用。"

    logger.info(f"[msg] reply={'（無）' if reply is None else repr(reply[:40])}")

    if reply:
        # LINE 文字訊息上限 5000 字
        if len(reply) > 5000:
            reply = reply[:4950] + "\n\n⋯（訊息過長已截斷，請輸入「列表」查看完整內容）"
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e:
            logger.error(f"[reply] 失敗: {e}")


@handler.add(JoinEvent)
def handle_join(event):
    msg = (
        "👋 大家好！我是接龍助理\n\n"
        "📋 將排班表貼到群組，我會自動編號\n"
        "📝 輸入「接龍 名稱」開始簡易接龍\n\n"
        "報名方式：+編號 名字\n"
        "例如：+3 王小明"
    )
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
    except Exception as e:
        logger.error(f"[Join] 失敗: {e}")


# ══════════════════════════════════════════
# 排程器
# ══════════════════════════════════════════

def start_scheduler():
    """啟動排程器"""
    scheduler = BackgroundScheduler(timezone=TZ_TAIPEI)
    # 空缺提醒（預設 12:00）
    scheduler.add_job(
        vacancy_reminder, trigger="cron", hour=12, minute=0,
        id="vacancy_reminder", replace_existing=True,
    )
    # 每週日 20:00 推播下週工作預告
    scheduler.add_job(
        weekly_reminder, trigger="cron", day_of_week="sun", hour=20, minute=0,
        id="weekly_reminder", replace_existing=True,
    )

    scheduler.start()
    logger.info("[排程] 已啟動（空缺提醒 12:00 + 週日 20:00 週報）")

    # 啟動後再讀 DB 設定，若與預設不同則更新
    try:
        r_hour    = int(get_setting("reminder_hour",    "12"))
        r_minute  = int(get_setting("reminder_minute",  "0"))
        r_enabled = get_setting("reminder_enabled", "1") == "1"
        if not r_enabled:
            scheduler.pause_job("vacancy_reminder")
        elif (r_hour, r_minute) != (12, 0):
            scheduler.reschedule_job(
                "vacancy_reminder", trigger="cron", hour=r_hour, minute=r_minute,
            )
            logger.info(f"[排程] 更新空缺提醒為 {r_hour:02d}:{r_minute:02d}")
    except Exception as e:
        logger.warning(f"[排程] 讀取設定失敗（使用預設值）: {e}")

    return scheduler


def _start_scheduler_once():
    """安全地啟動一次排程器（可從 gunicorn post_worker_init 或直接執行呼叫）"""
    global _scheduler_started, _scheduler
    with _startup_lock:
        if not _scheduler_started:
            try:
                _scheduler = start_scheduler()
                _scheduler_started = True
            except Exception as e:
                logger.error(f"[startup] 排程器啟動失敗: {e}")


# ══════════════════════════════════════════
# 啟動初始化（模組層級）
# ══════════════════════════════════════════

_startup_lock      = threading.Lock()
_scheduler_started = False
_scheduler         = None   # 全域 scheduler，供動態調整使用


def _startup():
    """模組載入時：在背景執行緒初始化 DB 並延遲啟動排程器（避免阻塞 port 綁定）"""

    def _delayed_init():
        import time
        # 先初始化 DB
        try:
            init_db()
            logger.info("[startup] 資料庫初始化完成")
        except Exception as e:
            logger.error(f"[startup] 資料庫初始化失敗: {e}")
        # 等 gunicorn 完成 port 綁定後再啟動排程器
        time.sleep(3)
        _start_scheduler_once()

    t = threading.Thread(target=_delayed_init, daemon=True)
    t.start()
    logger.info("[startup] 背景初始化執行緒已啟動")


_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
