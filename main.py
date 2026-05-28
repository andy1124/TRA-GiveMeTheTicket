"""
台鐵自動訂票 — 主程式入口
用法：
    python main.py                      # 使用預設 config.yaml
    python main.py --config my.yaml     # 指定設定檔
    python main.py --inspect            # 只開啟瀏覽器到訂票頁面（用於檢查 selector）
    python main.py --collect-captcha    # 開啟驗證碼蒐集模式（覆蓋 config.yaml 設定）
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

from booker import BookingConfig, run_booking_multi, navigate_to_booking
from playwright.async_api import async_playwright

# ── 設定 logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def _build_booking_config(b: dict, automation_kwargs: dict) -> BookingConfig:
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


def load_jobs(config_path: str) -> list[BookingConfig]:
    """讀取 YAML 設定檔，回傳 list[BookingConfig]。
    支援新格式（jobs: 列表）及舊格式（booking: 單鍵），向後相容。
    """
    path = Path(config_path)
    if not path.exists():
        logger.error(f"找不到設定檔: {config_path}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
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

    # 新格式：jobs: 列表
    if "jobs" in raw:
        return [_build_booking_config(j, automation_kwargs) for j in (raw["jobs"] or [])]

    # 舊格式：booking: 單鍵 → 包成 1-element list
    b = raw.get("booking", {})
    return [_build_booking_config(b, automation_kwargs)]


async def inspect_mode():
    """
    開啟瀏覽器並導覽到訂票頁面，然後暫停。
    讓使用者可以在 DevTools 裡手動檢查 HTML 結構與 selector。
    """
    logger.info("🔍 Inspect 模式：開啟瀏覽器並導覽到訂票頁面")
    logger.info("   請在瀏覽器 DevTools (F12) 中檢查元素，按 Enter 關閉。")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False, slow_mo=500,
            args=["--disable-blink-features=AutomationControlled", "--disable-infobars"],
        )
        context = await browser.new_context(
            locale="zh-TW",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        await navigate_to_booking(page)
        input("\n📌 瀏覽器已開啟，按 Enter 關閉...")
        await browser.close()


def main():
    parser = argparse.ArgumentParser(description="台鐵自動訂票工具")
    parser.add_argument(
        "--config", default="config.yaml",
        help="設定檔路徑（預設：config.yaml）"
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="只開啟瀏覽器到訂票頁面（用於檢查 selector，不執行訂票）"
    )
    parser.add_argument(
        "--collect-captcha", action="store_true", default=None,
        dest="collect_captcha",
        help="開啟驗證碼蒐集模式：每次 ddddocr 預測後，依結果自動存圖至 captcha_dataset/"
    )
    args = parser.parse_args()

    if args.inspect:
        asyncio.run(inspect_mode())
        return

    jobs = load_jobs(args.config)
    if not jobs:
        logger.error("設定檔中沒有任何搶票任務，請確認 jobs: 列表或 booking: 設定。")
        sys.exit(1)

    # --collect-captcha 旗標套用到所有 jobs
    if args.collect_captcha:
        for j in jobs:
            j.collect_captcha = True

    logger.info("=" * 50)
    logger.info("  台鐵自動訂票工具 TRA-GiveMeTheTicket")
    logger.info("=" * 50)
    logger.info(f"  共 {len(jobs)} 個搶票任務")
    for i, j in enumerate(jobs, 1):
        label = j.label or f"Train {j.train_number}"
        logger.info(
            f"  [{i}] {label}  "
            f"{j.departure_station}→{j.arrival_station}  "
            f"{j.date}  {j.id_number[:3]}*******"
        )
    logger.info(f"  最大重試：{jobs[0].max_retries} 次，間隔 {jobs[0].retry_interval} 秒")
    if jobs[0].chrome_profile_path:
        logger.info(f"  Chrome  ：使用指定 profile → {jobs[0].chrome_profile_path}")
    else:
        logger.info("  Chrome  ：使用專案 .chrome_profile/（建議設定 chrome_profile_path）")
    logger.info("=" * 50)
    logger.info("▶  開始執行，按 Ctrl+C 可隨時中止\n")

    results: list[bool] = []
    try:
        results = asyncio.run(run_booking_multi(jobs))
    except KeyboardInterrupt:
        logger.info("\n⛔ 使用者手動中止")
        sys.exit(130)

    # 彙整摘要
    succeeded = sum(1 for r in results if r)
    failed = len(results) - succeeded
    logger.info("\n" + "=" * 50)
    logger.info("  搶票完成摘要")
    logger.info("=" * 50)
    for i, (job, result) in enumerate(zip(jobs, results), 1):
        label = job.label or f"Train {job.train_number}"
        status = "✅ 成功" if result else "❌ 失敗"
        logger.info(f"  [{i}] {label}: {status}")
    logger.info(f"\n  成功：{succeeded} 筆　失敗：{failed} 筆")
    logger.info("=" * 50)

    sys.exit(0 if succeeded > 0 else 1)


if __name__ == "__main__":
    main()
