"""
server.py — FastAPI 主 app
路由：
  GET  /api/config   → 讀取 config.yaml，回傳 JSON
  POST /api/config   → 接收 JSON，寫入 config.yaml
  POST /api/start    → 啟動搶票任務
  POST /api/stop     → 停止搶票任務
  GET  /api/status   → 回傳目前狀態
  WS   /ws/logs      → 即時 log 廣播
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from . import booking_runner
from .booking_runner import ws_log_handler

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TRA-GiveMeTheTicket UI")

# ── WebSocket 連線管理 ────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: str):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()


# ── 啟動事件：掛載 WebSocket log handler ────────────────────────────
@app.on_event("startup")
async def startup_event():
    booking_runner.set_broadcast(manager.broadcast)
    root_logger = logging.getLogger()
    root_logger.addHandler(ws_log_handler)
    root_logger.setLevel(logging.INFO)
    logging.info("🚀 TRA-GiveMeTheTicket UI 已啟動")


# ── REST API ─────────────────────────────────────────────────────────
@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    """讀取 config.yaml，回傳 JSON。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data or {}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="config.yaml 不存在")


@app.post("/api/config")
async def post_config(body: dict[str, Any]) -> dict[str, str]:
    """接收 JSON，寫入 config.yaml（Phase 1 使用 yaml.dump，註解會被移除）。"""
    try:
        CONFIG_PATH.write_text(
            yaml.dump(body, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/start")
async def api_start() -> dict[str, str]:
    """從 config.yaml 讀取設定，啟動搶票任務。"""
    try:
        cfg = booking_runner.load_config_from_yaml()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"讀取設定失敗：{e}")
    await booking_runner.start(cfg)
    return {"status": "started"}


@app.post("/api/stop")
async def api_stop() -> dict[str, str]:
    """停止正在執行的搶票任務。"""
    await booking_runner.stop()
    return {"status": "stopped"}


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    """回傳目前狀態。"""
    return booking_runner.get_state()


# ── WebSocket ─────────────────────────────────────────────────────────
@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # 保持連線：等待客戶端訊息（ping/pong 或關閉）
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ── 靜態檔案（最後掛載，避免路由衝突）──────────────────────────────
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
