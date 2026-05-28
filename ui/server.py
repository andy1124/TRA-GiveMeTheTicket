"""
server.py — FastAPI 主 app
路由：
  GET  /api/config   → 讀取 config.yaml，回傳 JSON（ruamel.yaml，保留中文註解）
  POST /api/config   → 接收 JSON，寫入 config.yaml（ruamel.yaml，保留中文註解）
  POST /api/start    → 啟動搶票任務（可帶 scheduled_time 參數）
  POST /api/stop     → 停止搶票任務（含等待中的排程）
  GET  /api/status   → 回傳 { state, countdown_secs, retry_count, last_attempt }
  WS   /ws/logs      → 即時 log 廣播
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ruamel.yaml import YAML

from . import booking_runner
from .booking_runner import ws_log_handler

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TRA-GiveMeTheTicket UI")

# ruamel.yaml 實例（保留中文註解、格式）
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False


# ── YAML 讀寫工具 ─────────────────────────────────────────────────────
def _read_yaml() -> dict:
    """讀取 config.yaml，回傳 CommentedMap（保留註解結構）。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return _yaml.load(f) or {}


def _write_yaml(data: dict) -> None:
    """將 data 合併更新到既有 config.yaml，保留原有中文註解。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            existing = _yaml.load(f) or {}
    except FileNotFoundError:
        existing = {}

    # 深度合併：只更新有值的鍵，保留結構與註解
    for section, values in data.items():
        if section not in existing:
            existing[section] = {}
        if isinstance(values, dict):
            for k, v in values.items():
                existing[section][k] = v
        else:
            existing[section] = values

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        _yaml.dump(existing, f)


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
    """讀取 config.yaml，回傳 { jobs: [...], automation: {...} }。
    若 yaml 只有舊格式 booking: 單鍵，自動轉為 jobs: [booking] 再回傳。
    """
    try:
        data = _read_yaml()
        result: dict[str, Any] = {}
        result["automation"] = dict(data.get("automation") or {})
        if "jobs" in data and data["jobs"]:
            result["jobs"] = [dict(j) for j in data["jobs"]]
        elif "booking" in data and data["booking"]:
            result["jobs"] = [dict(data["booking"])]
        else:
            result["jobs"] = []
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="config.yaml 不存在")


@app.post("/api/config")
async def post_config(body: dict[str, Any]) -> dict[str, str]:
    """接收 JSON，寫入 config.yaml（ruamel.yaml，保留中文註解）。"""
    try:
        _write_yaml(body)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /api/start Request Body ──────────────────────────────────────────
class StartRequest(BaseModel):
    scheduled_time: Optional[str] = None   # ISO 格式：2026-06-01T08:00


@app.post("/api/start")
async def api_start(body: StartRequest = StartRequest()) -> dict[str, str]:
    """從 config.yaml 讀取設定，啟動搶票任務。
    可選帶入 scheduled_time（ISO 格式字串），到時間才自動開始。
    """
    try:
        jobs = booking_runner.load_jobs_from_yaml()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"讀取設定失敗：{e}")

    scheduled = None
    if body.scheduled_time:
        try:
            scheduled = datetime.fromisoformat(body.scheduled_time)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"scheduled_time 格式錯誤：{body.scheduled_time}")

    await booking_runner.start(jobs, scheduled)
    return {"status": "started"}


@app.post("/api/stop")
async def api_stop() -> dict[str, str]:
    """停止正在執行或等待中的搶票任務。"""
    await booking_runner.stop()
    return {"status": "stopped"}


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    """回傳目前狀態：{ state, countdown_secs, retry_count, last_attempt }。"""
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
