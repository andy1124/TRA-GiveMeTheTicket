"""
run_ui.py — TRA-GiveMeTheTicket UI 啟動入口
用法：
    python run_ui.py

執行後自動開啟瀏覽器 http://localhost:8787
"""

import asyncio
import sys
import time
import webbrowser
from pathlib import Path

import uvicorn

HOST = "127.0.0.1"
PORT = 8787
URL  = f"http://{HOST}:{PORT}"


def main():
    print("=" * 50)
    print("  TRA-GiveMeTheTicket — Web UI")
    print(f"  開啟瀏覽器：{URL}")
    print("  按 Ctrl+C 可停止伺服器")
    print("=" * 50)

    # 延遲 1 秒再開啟瀏覽器，讓 uvicorn 先啟動
    def _open_browser():
        time.sleep(1.2)
        webbrowser.open(URL)

    import threading
    threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(
        "ui.server:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="warning",   # 只顯示 uvicorn 警告以上；應用層 log 透過 WebSocket 推送
    )


if __name__ == "__main__":
    main()
