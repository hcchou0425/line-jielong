"""
Gunicorn 設定檔
確保在 worker 啟動時初始化資料庫與排程器
"""


def post_fork(server, worker):
    """每個 worker process 啟動後執行"""
    from app import init_db, start_scheduler
    init_db()
    start_scheduler()
