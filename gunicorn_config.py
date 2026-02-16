"""
Gunicorn 設定檔
- post_fork：標記 worker process
- post_worker_init：worker 完全啟動、app 載入後才啟動排程器
  （避免排程器在模組載入時阻塞 port 綁定）
"""
import os


def post_fork(server, worker):
    """fork 後立刻標記此 process 為 worker"""
    os.environ["GUNICORN_WORKER"] = "1"


def post_worker_init(worker):
    """worker 完全初始化（app 已載入）後啟動排程器"""
    try:
        import app as _app
        _app._start_scheduler_once()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[gunicorn] 排程器啟動失敗: {e}")
