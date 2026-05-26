"""
booking_runner.py — 背景訂票任務管理
負責：
  - 以 asyncio.Task 管理 run_booking 的生命週期
  - 提供 start() / stop() 介面供 server.py 呼叫
  - 維護全域狀態（idle / running / success / failed）
  - 收集 WebSocket 廣播函式（由 server.py 注入）
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

import yaml

# 將專案根目錄加入 sys.path，讓 import booker 能正常運作
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from booker import BookingConfig, run_booking  # noqa: E402

logger = logging.getLogger(__name__)

# ── 全域狀態 ─────────────────────────────────────────────────────────
_current_task: asyncio.Task | None = None
_state: str = "idle"          # idle | running | success | failed

# 廣播函式：由 server.py 在啟動時注入
_broadcast_fn: Callable[[str], Awaitable[None]] | None = None


def set_broadcast(fn: Callable[[str], Awaitable[None]]) -> None:
    global _broadcast_fn
    _broadcast_fn = fn


def get_state() -> dict:
    return {"state": _state}


# ── WebSocket Log Handler ────────────────────────────────────────────
class WebSocketLogHandler(logging.Handler):
    """把 logging 輸出即時廣播到所有 WebSocket 連線。"""

    def emit(self, record: logging.LogRecord) -> None:
        if _broadcast_fn is None:
            return
        try:
            msg = self.format(record)
            # 在現有 event loop 上建立 task（FastAPI 執行中時必有 loop）
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_broadcast_fn(msg))
        except Exception:
            pass


# 全域 handler 實例（server.py 啟動時掛入 root logger）
ws_log_handler = WebSocketLogHandler()
ws_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
)


# ── 內部執行函式 ─────────────────────────────────────────────────────
async def _run(cfg: BookingConfig) -> None:
    global _state
    _state = "running"
    logger.info("▶  開始搶票任務")
    try:
        success = await run_booking(cfg)
        _state = "success" if success else "failed"
        if success:
            logger.info("🎉 搶票成功！")
        else:
            logger.warning("❌ 搶票任務結束，未能取得票券。")
    except asyncio.CancelledError:
        _state = "idle"
        logger.info("⛔ 搶票任務已取消。")
        raise
    except Exception as e:
        _state = "failed"
        logger.error(f"搶票發生例外：{e}")


# ── 公開介面 ─────────────────────────────────────────────────────────
async def start(cfg: BookingConfig) -> None:
    """啟動搶票任務（若已在執行中則直接返回）。"""
    global _current_task, _state
    if _current_task and not _current_task.done():
        logger.warning("⚠️  任務已在執行中，請先停止再重新開始。")
        return
    _current_task = asyncio.create_task(_run(cfg))


async def stop() -> None:
    """取消正在執行的任務。"""
    global _state
    if _current_task and not _current_task.done():
        _current_task.cancel()
        try:
            await _current_task
        except asyncio.CancelledError:
            pass
    _state = "idle"
    logger.info("⏹  任務已停止。")


def load_config_from_yaml() -> BookingConfig:
    """從 config.yaml 讀取設定並回傳 BookingConfig。"""
    config_path = ROOT / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    b = raw.get("booking", {})
    a = raw.get("automation", {})

    return BookingConfig(
        id_number            = b["id_number"],
        departure_station    = b["departure_station"],
        arrival_station      = b["arrival_station"],
        date                 = b["date"],
        train_number         = str(b["train_number"]),
        ticket_count         = int(b.get("ticket_count", 1)),
        seat_preference      = b.get("seat_preference", "none"),
        accept_seat_exchange = bool(b.get("accept_seat_exchange", True)),
        headless             = bool(a.get("headless", False)),
        retry_interval       = float(a.get("retry_interval", 3)),
        max_retries          = int(a.get("max_retries", 200)),
        slow_mo              = int(a.get("slow_mo", 100)),
        use_real_chrome      = bool(a.get("use_real_chrome", True)),
        chrome_profile_path  = str(a.get("chrome_profile_path", "") or ""),
        kill_chrome_on_start = bool(a.get("kill_chrome_on_start", False)),
        collect_captcha      = bool(a.get("collect_captcha", False)),
        captcha_dataset_dir  = str(a.get("captcha_dataset_dir", "captcha_dataset")),
    )
