"""
LINE 接龍機器人
支援團購、認養、報名等接龍功能

設計原則：
- 每次加入/退出只回覆簡短確認，保持版面清爽
- 完整名單存於「記事本」（資料庫），不即時發送
- 每天早上 07:00（台灣時間）自動將最新名單推播到群組
- 需要隨時查看可輸入「列表」
"""

import os
import re
import sqlite3
import logging
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

HELP_TEXT = """📖 接龍助理使用說明
─────────────────
接龍 [名稱]  — 開始新的工作接龍
+1 [姓名] [工作項目] [備註] — 報名加入
列表       — 隨時查看目前名單
退出       — 從名單中移除自己
結束接龍   — 公布並封存最終名單
─────────────────
📌 每天早上 07:00 自動公布最新名單
（也可隨時輸入「列表」查看）
─────────────────
💡 範例（工作分派）：
  接龍 2/15 志工值班分配
  +1 小明 早班 8:00-12:00
  +1 小華 午班
  +1 小李
  列表
  結束接龍"""


# ──────────────────────────────────────────
# 資料庫初始化
# ──────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS lists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            creator_id  TEXT    NOT NULL,
            creator_name TEXT,
            status      TEXT    DEFAULT 'open',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id     INTEGER NOT NULL,
            user_id     TEXT    NOT NULL,
            user_name   TEXT,
            item        TEXT,
            quantity    TEXT,
            seq         INTEGER,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (list_id) REFERENCES lists (id)
        )
    """)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────
# 資料庫輔助函式
# ──────────────────────────────────────────

def get_active_list(group_id):
    """取得指定群組目前進行中的接龍（最新一筆）"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'SELECT * FROM lists WHERE group_id=? AND status="open" ORDER BY id DESC LIMIT 1',
        (group_id,),
    )
    row = c.fetchone()
    conn.close()
    return row  # (id, group_id, title, creator_id, creator_name, status, created_at)


def get_entries(list_id):
    """取得接龍的所有項目，依照 seq 排序"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM entries WHERE list_id=? ORDER BY seq", (list_id,))
    rows = c.fetchall()
    conn.close()
    return rows  # (id, list_id, user_id, user_name, item, quantity, seq, created_at)


def format_list(list_row, entries, *, show_time=False):
    """將接龍列表格式化成可讀文字"""
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


def get_user_name(event, group_id, user_id):
    """嘗試取得使用者顯示名稱"""
    try:
        if event.source.type == "group":
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
        else:
            profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return None


def source_id(event):
    """依來源類型回傳對話 ID（群組、聊天室或個人）"""
    src = event.source
    if src.type == "group":
        return src.group_id
    if src.type == "room":
        return src.room_id
    return src.user_id


# ──────────────────────────────────────────
# 指令處理函式
# ──────────────────────────────────────────

def get_all_active_lists():
    """取得所有進行中的接龍（排程推播用）"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM lists WHERE status="open"')
    rows = c.fetchall()
    conn.close()
    return rows


def daily_broadcast():
    """每天 07:00 自動將最新名單推播到各群組（記事本公告）"""
    active_lists = get_all_active_lists()
    if not active_lists:
        logger.info("[排程] 目前沒有進行中的接龍，跳過推播")
        return

    now_str = datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d")
    logger.info(f"[排程] 開始推播 {len(active_lists)} 個接龍")

    for lst in active_lists:
        group_id = lst[1]
        entries  = get_entries(lst[0])
        body     = format_list(lst, entries, show_time=True)
        message  = f"📣 早安！以下是今日接龍名單（{now_str}）\n\n{body}"
        try:
            line_bot_api.push_message(group_id, TextSendMessage(text=message))
            logger.info(f"[排程] 已推播至 {group_id}：{lst[2]}")
        except Exception as e:
            logger.error(f"[排程] 推播失敗 {group_id}：{e}")


def cmd_open(group_id, user_id, user_name, text):
    """開始接龍（支援「接龍」和「開團」兩種觸發詞）"""
    m = re.match(r"[/]?(?:接龍|開團)\s*(.*)", text)
    title = (m.group(1).strip() if m else "").strip() or "工作接龍"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 關閉現有進行中的接龍
    c.execute(
        'UPDATE lists SET status="closed" WHERE group_id=? AND status="open"',
        (group_id,),
    )
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
        f"例：+1 小明 早班 8:00-12:00\n"
        f"例：+1 小華\n\n"
        f"📌 名單每天早上 07:00 自動公布\n"
        f"隨時輸入「列表」也可查看"
    )


def cmd_join(group_id, user_id, user_name, text):
    """加入接龍"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍，請先輸入「開團 [名稱]」開始。"

    list_id = active[0]

    # 解析輸入：+1 名字 品項 數量
    m = re.match(r"\+\d*\s*(.*)", text)
    rest = m.group(1).strip() if m else text[1:].strip()

    parts = rest.split(None, 2)  # 最多切成 3 份
    if not parts:
        return "格式：+1 [名字] [品項] [數量]\n例：+1 小明 草莓 2盒"

    entry_name = parts[0]
    item       = parts[1] if len(parts) > 1 else ""
    quantity   = parts[2] if len(parts) > 2 else ""

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, seq FROM entries WHERE list_id=? AND user_id=?",
        (list_id, user_id),
    )
    existing = c.fetchone()

    if existing:
        c.execute(
            "UPDATE entries SET user_name=?, item=?, quantity=? WHERE id=?",
            (entry_name, item, quantity, existing[0]),
        )
        seq = existing[1]
        reply = f"✏️ 已更新！（第 {seq} 號）\n（名單每天 07:00 公布，或輸入「列表」隨時查看）"
    else:
        c.execute(
            "SELECT MAX(seq) FROM entries WHERE list_id=?",
            (list_id,),
        )
        max_seq = c.fetchone()[0] or 0
        seq = max_seq + 1
        c.execute(
            "INSERT INTO entries (list_id, user_id, user_name, item, quantity, seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (list_id, user_id, entry_name, item, quantity, seq),
        )
        reply = f"✅ 已加入！你是第 {seq} 號\n（名單每天 07:00 公布，或輸入「列表」隨時查看）"

    conn.commit()
    conn.close()
    return reply


def cmd_list(group_id):
    """查看名單"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"
    entries = get_entries(active[0])
    return format_list(active, entries)


def cmd_close(group_id, user_id):
    """結團"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE lists SET status="closed" WHERE id=?', (active[0],))
    conn.commit()
    conn.close()

    entries = get_entries(active[0])
    list_text = format_list(active, entries, show_time=True)
    return f"🔒 接龍已結束，以下為最終名單：\n\n{list_text}\n\n共 {len(entries)} 人報名"


def cmd_leave(group_id, user_id):
    """退出接龍"""
    active = get_active_list(group_id)
    if not active:
        return "目前沒有進行中的接龍。"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, seq FROM entries WHERE list_id=? AND user_id=?",
        (active[0], user_id),
    )
    existing = c.fetchone()
    if not existing:
        conn.close()
        return "你不在目前的接龍名單中。"

    c.execute("DELETE FROM entries WHERE id=?", (existing[0],))
    conn.commit()
    conn.close()
    return f"✅ 已將你（第 {existing[1]} 號）從名單中移除。"


# ──────────────────────────────────────────
# LINE Webhook 路由
# ──────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    """Health check — 確認伺服器正常運作"""
    return "LINE 接龍助理運作中 ✅", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    gid  = source_id(event)
    uid  = event.source.user_id

    # 懶惰取得使用者名稱（只在需要時才查詢）
    def lazy_name():
        return get_user_name(event, gid, uid)

    reply = None

    # ── 開始接龍：支援「接龍 xxx」和「開團 xxx」
    if re.match(r"[/]?(?:接龍|開團)\s+\S", text):
        reply = cmd_open(gid, uid, lazy_name(), text)

    # ── 加入：+1 / +2 / + 姓名...
    elif re.match(r"\+\d*(\s|$)", text) or text == "+":
        reply = cmd_join(gid, uid, lazy_name(), text)

    # ── 查看名單
    elif text in ("列表", "/列表", "查看", "/查看", "名單", "/名單"):
        reply = cmd_list(gid)

    # ── 結束接龍：支援「結束接龍」和舊版「結團」
    elif text in ("結束接龍", "/結束接龍", "結團", "/結團", "關閉接龍"):
        reply = cmd_close(gid, uid)

    # ── 退出名單
    elif text in ("退出", "/退出", "刪除", "/刪除", "取消"):
        reply = cmd_leave(gid, uid)

    # ── 說明
    elif text in ("說明", "/說明", "help", "/help", "幫助"):
        reply = HELP_TEXT

    if reply:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply),
        )


@handler.add(JoinEvent)
def handle_join(event):
    """當機器人加入群組時發送歡迎訊息"""
    welcome_msg = (
        "👋 大家好！我是接龍助理 RBOT\n\n"
        "我可以幫大家管理工作分派、團購、活動報名等接龍事項。\n\n"
        "📝 快速開始：\n"
        "• 輸入「接龍 [名稱]」開始接龍\n"
        "• 輸入「說明」查看完整指令\n\n"
        "📌 每天早上 07:00 會自動公布最新名單\n\n"
        "讓我們開始吧！ 🎉"
    )
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=welcome_msg)
        )
        logger.info(f"[Join] 機器人已加入群組: {source_id(event)}")
    except Exception as e:
        logger.error(f"[Join] 發送歡迎訊息失敗: {e}")


# ──────────────────────────────────────────
# 排程器設定（每天 07:00 台灣時間推播）
# ──────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=TZ_TAIPEI)
    scheduler.add_job(
        daily_broadcast,
        trigger="cron",
        hour=7,
        minute=0,
        id="daily_broadcast",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("[排程] 已啟動，每天 07:00（台灣時間）自動推播接龍名單")
    return scheduler


# ──────────────────────────────────────────
# 啟動初始化
# 放在模組層級，gunicorn 和 python app.py 都會執行
# ──────────────────────────────────────────

# 初始化資料庫（idempotent，重複呼叫安全）
init_db()

# 啟動每日推播排程器
# 用 threading.Lock 防止多次 import 時重複啟動
import threading
_startup_lock = threading.Lock()
_scheduler_started = False


def _ensure_scheduler():
    global _scheduler_started
    with _startup_lock:
        if not _scheduler_started:
            start_scheduler()
            _scheduler_started = True


_ensure_scheduler()


# ──────────────────────────────────────────
# 主程式（直接執行時）
# ──────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
