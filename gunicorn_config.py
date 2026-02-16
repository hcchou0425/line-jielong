"""
Gunicorn 設定檔
標記 worker process，讓 _start_scheduler_once 知道自己在 worker 裡
（排程器由 app.py 的 background thread 延遲啟動，不在 hook 裡呼叫）
"""
import os


def post_fork(server, worker):
    """每個 worker process fork 後設定標記"""
    os.environ["GUNICORN_WORKER"] = "1"
