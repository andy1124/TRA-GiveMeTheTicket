"""
booking_runner.py — 背景訂票任務管理
負責：
  - 以 asyncio.Task 管理 run_booking_multi 的生命週期
  - 提供 start() / stop() 介面供 server.py 呼叫
  - 維護全域狀態機（idle / waiting / running / success / failed）
  - 支援 scheduled_time：等待期間每秒更新倒數計時
  - 支援多 job 狀態追蹤（job_index、total_jobs、job_results）
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

from booker import BookingConfig, run_booking_multi  # noqa: E402

logger = logging.getLogger(__name__)

# ── 全域狀態 ─────────────────────────────────────────────────────────
_current_task: asyncio.Task | None = None
_state: str = "idle"          # idle | waiting | running | success | failed
_countdown_secs: int = 0       # 排程倒數剩餘秒數（waiting 狀態時有效）
_retry_count: int = 0          # 目前重試次數（running 狀態時有效）
_last_attempt: str = ""        # 上次嘗試時間（ISO 格式字串）

# ── 多 job 狀態 ──────────────────────────────────────────────────────
_jobs: list[BookingConfig] = []
_current_job_index: int = 0    # 0-based，目前執行到第幾個 job
_total_jobs: int = 0
_job_results: list[str] = []   # "success" | "failed" | "" (尚未執行)

# 廣播函式：由 server.py 在啟動時注入
_broadcast_fn: Callable[[str], Awaitable[None]] | None = None


def set_broadcast(fn: Callable[[str], Awaitable[None]]) -> None:
    global _broadcast_fn
    _broadcast_fn = fn


def get_state() -> dict:
    current_label = ""
    if _jobs and 0 <= _current_job_index < len(_jobs):
        j = _jobs[_current_job_index]
        current_label = j.label or f"Train {j.train_number} | {j.id_number[:3]}***"
    return {
        "state": _state,
        "countdown_secs": _countdown_secs,
        "retry_count": _retry_count,
        "last_attempt": _last_attempt,
        "job_index": _current_job_index,
        "total_jobs": _total_jobs,
        "current_job_label": current_label,
        "job_results": list(_job_results),
    }


# ── WebSocket Log Handler ────────────────────────────────────────────
class WebSocketLogHandler(logging.Handler):
    """把 logging 輸出即時廣播到所有 WebSocket 連線。"""

    def emit(self, record: logging.LogRecord) -> None:
        if _broadcast_fn is None:
            return
        try:
            msg = self.format(record)
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


# ── 重試計數鉤子 ─────────────────────────────────────────────────────
def _on_retry(count: int) -> None:
    """由 run_booking_session 每次重試時回呼，更新 _retry_count 與 _last_attempt。"""
    global _retry_count, _last_attempt
    _retry_count = count
    _last_attempt = datetime.now().strftime("%H:%M:%S")


# ── 內部執行函式 ─────────────────────────────────────────────────────
async def _run(jobs: list[BookingConfig], scheduled_time: datetime | None = None) -> None:
    global _state, _countdown_secs, _retry_count, _last_attempt
    global _jobs, _current_job_index, _total_jobs, _job_results

    _jobs = jobs
    _total_jobs = len(jobs)
    _current_job_index = 0
    _job_results = [""] * _total_jobs

    # ── 階段 1：等待排程時間 ──────────────────────────────────────────
    if scheduled_time:
        wait_secs = (scheduled_time - datetime.now()).total_seconds()
        if wait_secs > 0:
            _state = "waiting"
            _countdown_secs = int(wait_secs)
            logger.info(f"⏰ 排程等待中，將於 {scheduled_time.strftime('%Y/%m/%d %H:%M')} 自動開始")
            try:
                while _countdown_secs > 0:
                    await asyncio.sleep(1)
                    _countdown_secs = max(0, _countdown_secs - 1)
            except asyncio.CancelledError:
                _state = "idle"
                _countdown_secs = 0
                logger.info("⛔ 排程已取消。")
                raise

    _countdown_secs = 0

    # ── 階段 2：執行搶票 ──────────────────────────────────────────────
    _state = "running"
    _retry_count = 0
    logger.info(f"▶  開始搶票任務（共 {_total_jobs} 個）")

    def _on_job_done(index: int, success: bool, cfg: BookingConfig) -> None:
        global _current_job_index, _job_results
        _job_results[index] = "success" if success else "failed"
        _current_job_index = index + 1

    try:
        results = await run_booking_multi(
            jobs,
            on_job_done=_on_job_done,
            on_retry=_on_retry,
        )
        succeeded = sum(1 for r in results if r)
        _state = "success" if succeeded > 0 else "failed"
        if succeeded > 0:
            logger.info(f"🎉 搶票完成！成功：{succeeded}/{_total_jobs}")
        else:
            logger.warning(f"❌ 搶票任務結束，{_total_jobs} 個任務均未成功。")
    except asyncio.CancelledError:
        _state = "idle"
        _retry_count = 0
        logger.info("⛔ 搶票任務已取消。")
        raise
    except Exception as e:
        _state = "failed"
        logger.error(f"搶票發生例外：{e}")


# ── 公開介面 ─────────────────────────────────────────────────────────
async def start(jobs: list[BookingConfig], scheduled_time: datetime | None = None) -> None:
    """啟動搶票任務（若已在執行中則直接返回）。"""
    global _current_task, _state
    if _current_task and not _current_task.done():
        logger.warning("⚠️  任務已在執行中，請先停止再重新開始。")
        return
    _current_task = asyncio.create_task(_run(jobs, scheduled_time))


async def stop() -> None:
    """取消正在執行或等待中的任務。"""
    global _state, _countdown_secs, _retry_count
    if _current_task and not _current_task.done():
        _current_task.cancel()
        try:
            await _current_task
        except asyncio.CancelledError:
            pass
    _state = "idle"
    _countdown_secs = 0
    _retry_count = 0
    logger.info("⏹  任務已停止。")


def load_jobs_from_yaml() -> list[BookingConfig]:
    """從 config.yaml 讀取設定並回傳 list[BookingConfig]。
    支援新格式（jobs: 列表）及舊格式（booking: 單鍵），向後相容。
    """
    config_path = ROOT / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    a = raw.get("automation", {})
    automation_kwargs = dict(
        headless             = bool(a.get("headless", False)),
        retry_interval       = float(a.get("retry_interval", 3)),
        max_retries          = int(a.get("max_retries", 200)),
        slow_mo              = int(a.get("slow_mo", 300)),
        use_real_chrome      = bool(a.get("use_real_chrome", True)),
        chrome_profile_path  = str(a.get("chrome_profile_path", "") or ""),
        kill_chrome_on_start = bool(a.get("kill_chrome_on_start", False)),
        collect_captcha      = bool(a.get("collect_captcha", False)),
        captcha_dataset_dir  = str(a.get("captcha_dataset_dir", "captcha_dataset")),
        on_job_exhaust       = str(a.get("on_job_exhaust", "skip")),
    )

    def _make(b: dict) -> BookingConfig:
        return BookingConfig(
            id_number            = b["id_number"],
            departure_station    = b["departure_station"],
            arrival_station      = b["arrival_station"],
            date                 = b["date"],
            train_number         = str(b["train_number"]),
            ticket_count         = int(b.get("ticket_count", 1)),
            seat_preference      = b.get("seat_preference", "none"),
            accept_seat_exchange = bool(b.get("accept_seat_exchange", True)),
            label                = str(b.get("label", "") or ""),
            **automation_kwargs,
        )

    if "jobs" in raw:
        return [_make(j) for j in (raw["jobs"] or [])]

    return [_make(raw.get("booking", {}))]


def load_config_from_yaml() -> BookingConfig:
    """向後相容：回傳第一個 job 的 BookingConfig。"""
    return load_jobs_from_yaml()[0]
