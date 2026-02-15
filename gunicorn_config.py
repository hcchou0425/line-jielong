"""
Gunicorn 設定檔
標記 worker process，讓 app.py 的排程器只在 worker 裡啟動一次
"""
import os


def post_fork(server, worker):
    """每個 worker process 啟動後，設定標記讓排程器知道自己在 worker 裡"""
    os.environ["GUNICORN_WORKER"] = "1"
