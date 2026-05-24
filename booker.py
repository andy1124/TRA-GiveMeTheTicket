"""
Taiwan Railway Auto-Booking Core Logic (Phase 1)
Selectors verified from live page JS dump 2026-05-24

Key anti-captcha strategy: persistent Chrome profile stored in .chrome_profile/
reCAPTCHA v3 scores improve as the profile accumulates cookies and history.
"""

import asyncio
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

logger = logging.getLogger(__name__)

BOOKING_FORM_URL    = "https://www.railway.gov.tw/tra-tip-web/tip/tip001/tip121/query"
SUCCESS_MARKER      = "訂票成功"
FAIL_MARKER_SEAT    = "00089"
FAIL_MARKER_NONE    = "均無符合條件車次"
FAIL_MARKER_CAPTCHA = "驗證碼驗證失敗"
RESET_BTN_TEXT      = "返回，重設訂票條件"
MAX_CAPTCHA_RETRIES = 15   # 單次送出最多重試幾次驗證碼

RESULT_JS = (
    "document.body.innerText.includes('訂票成功') || "
    "document.body.innerText.includes('00089') || "
    "document.body.innerText.includes('均無符合條件車次') || "
    "document.body.innerText.includes('驗證碼驗證失敗')"
)

# Persistent profile dir lives next to this script
PROFILE_DIR = Path(__file__).parent / ".chrome_profile"


@dataclass
class BookingConfig:
    id_number: str
    departure_station: str
    arrival_station: str
    date: str
    train_number: str
    ticket_count: int
    seat_preference: str
    accept_seat_exchange: bool
    headless: bool
    retry_interval: float
    max_retries: int
    slow_mo: int
    use_real_chrome: bool
    chrome_profile_path: str = ""    # empty = use default .chrome_profile/
    kill_chrome_on_start: bool = True


# ===================================================================
#  1. Navigate
# ===================================================================

async def navigate_to_booking(page: Page) -> None:
    logger.info(f"Navigating to: {BOOKING_FORM_URL}")
    await page.goto(BOOKING_FORM_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle", timeout=15000)
    try:
        cookie_btn = page.locator("button.btn-cookie")
        if await cookie_btn.is_visible(timeout=2000):
            await cookie_btn.click()
    except Exception:
        pass
    logger.info(f"Page loaded: {page.url}")


# ===================================================================
#  2. Fill form helpers
# ===================================================================

async def _select_station(page: Page, field_id: str, station: str) -> None:
    inp = page.locator(f"#{field_id}")

    # Skip if already showing the correct station
    try:
        current = (await inp.input_value()).strip()
        if current == station:
            logger.info(f"  Station already '{station}', skipping")
            return
    except Exception:
        pass

    await inp.click()
    await asyncio.sleep(0.2)
    await inp.fill("")
    await inp.type(station, delay=80)
    await asyncio.sleep(0.7)

    try:
        item = page.locator(
            f"ul.ui-autocomplete li.ui-menu-item:has-text('{station}')"
        ).first
        await item.wait_for(state="visible", timeout=3000)
        await item.click()
        # Wait for dropdown to fully close before continuing
        try:
            await page.locator("ul.ui-autocomplete").wait_for(state="hidden", timeout=2000)
        except Exception:
            pass
        logger.info(f"  Station selected (autocomplete): {station}")
        await asyncio.sleep(0.2)
        return
    except PlaywrightTimeout:
        logger.debug("  Autocomplete not shown, trying station picker")

    await inp.press("Escape")
    await asyncio.sleep(0.2)
    icon_buttons = page.locator("button.icon.icon-list")
    btn_index = 0 if field_id == "startStation" else 1
    try:
        await icon_buttons.nth(btn_index).click()
        await asyncio.sleep(0.4)
        station_btn = page.locator(f"button.tipStation:has-text('{station}')").first
        await station_btn.wait_for(state="visible", timeout=3000)
        await station_btn.click()
        logger.info(f"  Station selected (picker): {station}")
        await asyncio.sleep(0.3)
    except Exception as e:
        logger.warning(f"  Station selection failed ({station}): {e}")


# ── ddddocr singleton（懶載入，避免每次重試都重新載入模型）──────────
_ocr_instance = None

def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        try:
            import ddddocr
            _ocr_instance = ddddocr.DdddOcr(show_ad=False)
            logger.info("ddddocr loaded")
        except ImportError:
            logger.warning("ddddocr not installed — captcha will require manual input")
    return _ocr_instance


# 嘗試多個 selector 截取驗證碼圖片
# 最後一個 fallback：找 #verifyCode input 的父層容器內任意 img
_CAPTCHA_IMG_SELECTORS = [
    "#codeimg",                        # 台鐵實際 id（2026-05 確認）
    "img[alt*='驗證碼']",               # 以 alt 文字辨識（最可靠）
    "img[src*='player/picture']",       # 台鐵驗證碼圖片 src 路徑
    "#verifyCodeImg",
    "img[src*='verifyCode']",
    "img[src*='captcha']",
    "img[onclick*='verifyCode']",
]

async def _screenshot_captcha_image(page: Page) -> bytes | None:
    # 先嘗試已知 selector
    for sel in _CAPTCHA_IMG_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=500):
                logger.debug(f"  [captcha img] hit selector: {sel}")
                return await el.screenshot()
        except Exception:
            continue

    # Fallback：找頁面上所有可見的 <img>，取第一個非 logo/icon 的小圖
    logger.debug("  [captcha img] known selectors failed, trying visible img fallback...")
    try:
        all_imgs = page.locator("img")
        count = await all_imgs.count()
        for i in range(count):
            img = all_imgs.nth(i)
            try:
                if not await img.is_visible(timeout=300):
                    continue
                src = (await img.get_attribute("src") or "").lower()
                # 排除明顯的 logo / icon / static 圖片
                if any(kw in src for kw in ("logo", "icon", "banner", "static", ".svg", "app.png", "store", "google-play", "play-store")):
                    continue
                box = await img.bounding_box()
                if box is None:
                    continue
                # 驗證碼圖片通常寬 60-200px、高 20-80px
                if 40 <= box["width"] <= 300 and 15 <= box["height"] <= 120:
                    logger.debug(f"  [captcha img] fallback hit: src={src!r} box={box}")
                    return await img.screenshot()
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"  [captcha img] fallback error: {e}")

    logger.warning("  [captcha img] 無法截取驗證碼圖片，所有 selector 均未命中")
    return None


async def _refresh_captcha_image(page: Page) -> None:
    """點擊「重新產生驗證碼」按鈕，讓伺服器換一張新圖。"""
    # 優先：點台鐵的重新產生按鈕（2026-05 確認 id=changeVoice）
    for btn_sel in ["#changeVoice", "button[title*='重新產生驗證碼']", "button:has-text('重新產生驗證碼')"]:
        try:
            btn = page.locator(btn_sel).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                await asyncio.sleep(1.0)   # 等新圖載入
                logger.debug(f"  [captcha refresh] 點擊 {btn_sel}")
                return
        except Exception:
            continue
    # Fallback：點驗證碼圖片本身（部分版本點圖也能換圖）
    for sel in _CAPTCHA_IMG_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=500):
                await el.click()
                await asyncio.sleep(1.0)
                logger.debug(f"  [captcha refresh] fallback 點圖 {sel}")
                return
        except Exception:
            continue

def _is_valid_captcha(text: str) -> bool:
    """
    驗證 ddddocr 辨識結果是否符合台鐵驗證碼規則：
      - 不可為空
      - 只能包含英文字母與數字（無中文、符號、空白）
      - 長度 = 6 個英文字數字元
    """
    import re
    if not text:
        return False
    if not re.fullmatch(r'[A-Za-z0-9]+', text):
        return False
    if not (len(text) == 6):
        return False
    return True


async def _handle_captcha(page: Page, force: bool = False) -> None:
    """
    自動辨識驗證碼（ddddocr），失敗則退回手動輸入。
    force=True：即使欄位已有值也重新辨識（驗證碼錯誤後使用）。
    """
    captcha_input = page.locator("#verifyCode")
    try:
        visible = await captcha_input.is_visible(timeout=1000)
    except Exception:
        visible = False
    if not visible:
        logger.debug("  [captcha] #verifyCode 不可見，跳過")
        return

    logger.info("  [captcha] 偵測到驗證碼輸入欄")

    if not force:
        val = await captcha_input.input_value()
        if val:
            logger.debug(f"  [captcha] 已有值 {val!r}，跳過")
            return  # 已填且非強制，跳過

    # 重新整理驗證碼圖片（force 模式 = 上次答錯，需要新圖）
    if force:
        logger.info("  [captcha] 強制刷新驗證碼圖片...")
        await _refresh_captcha_image(page)

    # ddddocr 截圖 + 辨識，結果不合規則或截圖失敗時最多重試 15 次
    OCR_RETRIES = 15
    ocr = _get_ocr()
    for ocr_attempt in range(1, OCR_RETRIES + 1):
        img_bytes = await _screenshot_captcha_image(page)
        if not img_bytes:
            logger.warning(f"  [captcha] 截圖失敗（attempt {ocr_attempt}/{OCR_RETRIES}），稍後重試...")
            await asyncio.sleep(0.8)
            await _refresh_captcha_image(page)
            continue

        logger.info(f"  [captcha] 截圖成功 ({len(img_bytes)} bytes)，送入 ddddocr... (attempt {ocr_attempt}/{OCR_RETRIES})")
        if ocr is not None:
            try:
                result = ocr.classification(img_bytes).strip()
                logger.info(f"  [captcha] ddddocr 結果: {result!r}")
                if _is_valid_captcha(result):
                    await captcha_input.fill("")
                    await captcha_input.fill(result)
                    logger.info(f"  [captcha] 自動填入: {result}")
                    return
                else:
                    reason = "空字串" if not result else (
                        f"長度 {len(result)} 不等於 6" if not (len(result) == 6) else
                        "含非英數字元"
                    )
                    logger.warning(
                        f"  [captcha] 結果 {result!r} 不合規則（{reason}）"
                        f"（attempt {ocr_attempt}/{OCR_RETRIES}），刷新圖片重試..."
                    )
                    await _refresh_captcha_image(page)
                    await asyncio.sleep(0.6)
            except Exception as e:
                logger.warning(f"  [captcha] ddddocr error: {e}")
                break  # 辨識異常不重試，直接退到手動

    logger.warning("  [captcha] OCR 重試耗盡，退回手動輸入")
    # 退回手動輸入
    logger.warning("CAPTCHA detected! Look at the browser and enter the code:")
    captcha_text = input("   captcha -> ").strip()
    await captcha_input.fill(captcha_text)
    logger.info("  Captcha entered.")


async def fill_booking_form(page: Page, cfg: BookingConfig) -> None:
    logger.info("Waiting for form...")
    await page.wait_for_selector("#pid", timeout=15000)
    await asyncio.sleep(0.5)

    # --- ID ---
    pid = page.locator("#pid")
    current_id = (await pid.input_value()).strip()
    if current_id != cfg.id_number:
        logger.info("Selecting ID radio")
        await page.locator("#personlType").check()
        logger.info(f"Filling ID: {cfg.id_number[:3]}*******")
        await pid.click()
        await pid.fill(cfg.id_number)
    else:
        logger.info(f"ID already filled: {cfg.id_number[:3]}******* (skip)")

    # --- Stations ---
    logger.info(f"Departure: {cfg.departure_station}")
    await _select_station(page, "startStation", cfg.departure_station)

    logger.info(f"Arrival: {cfg.arrival_station}")
    await _select_station(page, "endStation", cfg.arrival_station)

    # --- Trip type ---
    trip_radio = page.locator("input[name='tripType'][value='ONEWAY']")
    if not await trip_radio.is_checked():
        logger.info("Trip type: one-way")
        await trip_radio.check()
    else:
        logger.info("Trip type: one-way (already set, skip)")

    # --- Order type ---
    order_radio = page.locator("#orderType1")
    if not await order_radio.is_checked():
        logger.info("Order type: by train number")
        await order_radio.check()
        await asyncio.sleep(0.2)
    else:
        logger.info("Order type: by train number (already set, skip)")

    # --- Ticket count ---
    if cfg.ticket_count != 1:
        current_qty = int(await page.locator("#normalQty").input_value())
        if current_qty != cfg.ticket_count:
            logger.info(f"Setting ticket count: {cfg.ticket_count}")
            diff = cfg.ticket_count - current_qty
            btn_class = "button.add" if diff > 0 else "button.cut"
            for _ in range(abs(diff)):
                await page.locator(btn_class).first.click()
                await asyncio.sleep(0.15)
        else:
            logger.info(f"Ticket count: {cfg.ticket_count} (already set, skip)")

    # --- Date ---
    date_field = page.locator("#rideDate1")
    current_date = (await date_field.input_value()).strip()
    if current_date != cfg.date:
        logger.info(f"Date: {cfg.date}")
        await date_field.click(click_count=3)
        await date_field.fill(cfg.date)
        await date_field.press("Escape")   # 關閉 datepicker，避免 overlay 殘留
        await asyncio.sleep(0.3)
    else:
        logger.info(f"Date: {cfg.date} (already set, skip)")

    # --- Train number ---
    train = page.locator("#trainNoList1")
    current_train = (await train.input_value()).strip()
    if current_train != cfg.train_number:
        logger.info(f"Train: {cfg.train_number}")
        await train.click()
        await train.fill(cfg.train_number)
        await train.press("Escape")        # 關閉任何 autocomplete dropdown
        await asyncio.sleep(0.2)
    else:
        logger.info(f"Train: {cfg.train_number} (already set, skip)")

    # --- Seat preference ---
    if cfg.seat_preference == "window_seat":
        if not await page.locator("#seatPref2").is_checked():
            logger.info("Seat: table preferred")
            await page.locator("#seatPref2").check()
        else:
            logger.info("Seat: table preferred (already set, skip)")
    else:
        if not await page.locator("#seatPref1").is_checked():
            logger.info("Seat: no preference")
            await page.locator("#seatPref1").check()
        else:
            logger.info("Seat: no preference (already set, skip)")

    # --- Seat exchange ---
    chg = page.locator("#chgSeat1")
    is_checked = await chg.is_checked()
    if cfg.accept_seat_exchange and not is_checked:
        await chg.check()
    elif not cfg.accept_seat_exchange and is_checked:
        await chg.uncheck()

    await _handle_captcha(page)
    logger.info("Form filled.")


# ===================================================================
#  3. Submit & check result
# ===================================================================

async def submit_booking(page: Page) -> None:
    logger.info("Submitting...")
    submit = page.locator("input[type='submit'].btn-3d")
    # 等按鈕出現在 DOM 且可見
    await submit.wait_for(state="visible", timeout=10000)
    await submit.scroll_into_view_if_needed()
    await asyncio.sleep(0.2)
    # force=True：略過 actionability 檢查（overlay、disabled 等），確保一定能點到
    await submit.click(force=True)
    try:
        await page.wait_for_function(RESULT_JS, timeout=20000)
        logger.info("Result page detected.")
    except PlaywrightTimeout:
        logger.warning("Result not detected in 20s, reading page anyway...")
        await asyncio.sleep(2)


async def check_result(page: Page) -> str:
    content = await page.content()
    if SUCCESS_MARKER in content:
        try:
            order_text = await page.locator(".order-no, .bookingNo").first.inner_text()
            logger.info(f"SUCCESS! Order: {order_text.strip()}")
        except Exception:
            logger.info("SUCCESS!")
        return "success"
    if FAIL_MARKER_CAPTCHA in content:
        logger.warning("CAPTCHA verification failed")
        return "captcha_fail"
    if FAIL_MARKER_SEAT in content or FAIL_MARKER_NONE in content:
        logger.info("FAIL: no seats or no matching train")
        return "fail"
    logger.warning("UNKNOWN result - check browser")
    return "unknown"


async def click_reset(page: Page) -> None:
    logger.info("Clicking reset...")
    reset_btn = page.get_by_text(RESET_BTN_TEXT, exact=False).first
    await reset_btn.click()
    await page.wait_for_load_state("domcontentloaded", timeout=10000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightTimeout:
        pass  # networkidle 超時不是致命錯誤，繼續執行
    await asyncio.sleep(0.5)


# ===================================================================
#  4. Main booking loop
# ===================================================================

BROWSER_ARGS = [
    "--lang=zh-TW",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-sandbox",
]

STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
"""

COMMON_CONTEXT_OPTS = dict(
    locale="zh-TW",
    viewport={"width": 1280, "height": 900},
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
)


def _kill_chrome() -> None:
    """Force-kill all Chrome processes so the profile lock is released."""
    logger.info("正在關閉所有 Chrome 視窗...")
    result = subprocess.run(
        ["taskkill", "/F", "/IM", "chrome.exe"],
        capture_output=True, text=True
    )
    if "找不到" in (result.stderr or "") or "not found" in (result.stderr or "").lower():
        logger.info("  Chrome 本來就沒在執行，繼續。")
    else:
        logger.info("  Chrome 已關閉，等待 1 秒讓 profile lock 釋放...")
        time.sleep(1)


async def run_booking(cfg: BookingConfig) -> bool:
    if cfg.chrome_profile_path:
        profile_path = cfg.chrome_profile_path
        logger.info(f"Using custom Chrome profile: {profile_path}")
        if cfg.kill_chrome_on_start:
            _kill_chrome()
    else:
        PROFILE_DIR.mkdir(exist_ok=True)
        profile_path = str(PROFILE_DIR)
        logger.info(f"Using project Chrome profile: {profile_path}")

    async with async_playwright() as pw:
        channel = "chrome" if cfg.use_real_chrome else None

        # Use launch_persistent_context so cookies/history are reused across runs.
        # reCAPTCHA v3 score improves as the profile accumulates real browsing data.
        logger.info(f"Launching persistent Chrome profile: {profile_path}")
        try:
            context: BrowserContext = await pw.chromium.launch_persistent_context(
                profile_path,
                channel=channel,
                headless=cfg.headless,
                slow_mo=cfg.slow_mo,
                args=BROWSER_ARGS,
                **COMMON_CONTEXT_OPTS,
            )
        except PlaywrightError as e:
            err = str(e)
            if "TargetClosedError" in err or "Target page" in err or "has been closed" in err:
                logger.error(
                    "\n"
                    "❌ Chrome profile 被鎖住，無法啟動！\n"
                    "\n"
                    "原因：Chrome 仍在背景執行，佔用了 profile 目錄。\n"
                    "\n"
                    "解法（擇一）：\n"
                    "  1. 開啟「工作管理員」→ 找到所有 chrome.exe → 全部結束工作，再重新執行本程式。\n"
                    "  2. 在 config.yaml 中設定 kill_chrome_on_start: true，讓程式自動處理。\n"
                    "  3. 不填 chrome_profile_path，改用空白 profile（驗證碼可能仍會出現）。\n"
                )
                sys.exit(1)
            raise
        await context.add_init_script(STEALTH_JS)

        # Reuse existing tab or open a new one
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await navigate_to_booking(page)
            await fill_booking_form(page, cfg)

            for attempt in range(1, cfg.max_retries + 1):
                logger.info(f"\n-- Attempt {attempt} --")
                await submit_booking(page)
                result = await check_result(page)

                if result == "success":
                    logger.info("Done! Remember to pay within the deadline.")
                    input("\nPress Enter to close browser...")
                    return True

                if result == "unknown":
                    logger.warning("Cannot determine result. Check browser, press Enter...")
                    input()
                    break

                # ── 驗證碼失敗：頁面仍在表單，不需要 reset ──────────────
                # 直接在原頁面刷新驗證碼圖片、重新辨識後再送出，
                # 最多重試 MAX_CAPTCHA_RETRIES 次，超過才算此輪失敗。
                if result == "captcha_fail":
                    captcha_success = False
                    for cap_retry in range(1, MAX_CAPTCHA_RETRIES + 1):
                        logger.warning(
                            f"  Captcha fail — retrying ({cap_retry}/{MAX_CAPTCHA_RETRIES})..."
                        )
                        # force=True：強制刷新圖片並重新辨識
                        await _handle_captcha(page, force=True)
                        await submit_booking(page)
                        result = await check_result(page)

                        if result == "success":
                            logger.info("Done! Remember to pay within the deadline.")
                            input("\nPress Enter to close browser...")
                            return True

                        if result != "captcha_fail":
                            # 非驗證碼問題（fail / unknown），跳出內層迴圈走正常流程
                            break

                    if result == "captcha_fail":
                        logger.error(
                            f"Captcha retry exhausted ({MAX_CAPTCHA_RETRIES} times). "
                            "Giving up this attempt."
                        )
                    captcha_success = (result == "success")
                    if captcha_success:
                        return True  # 已在內層處理，保險再檢查一次

                # ── 一般失敗或驗證碼耗盡後：等待 → reset → 下一輪 ───────
                if attempt < cfg.max_retries:
                    logger.info(f"Waiting {cfg.retry_interval}s...")
                    await asyncio.sleep(cfg.retry_interval)
                    await click_reset(page)
                    # 重設後台鐵網站會保留表單資料，不需要重新填表
                    # 等表單就緒後直接進入下一輪 submit
                    await page.wait_for_selector("input[type='submit'].btn-3d",
                                                  state="visible", timeout=15000)
                    await _handle_captcha(page)   # 驗證碼重設後可能再次出現
                    logger.info("Form ready, retrying...")

            logger.warning(f"Max retries ({cfg.max_retries}) reached.")
            return False

        except Exception as e:
            logger.exception(f"Error: {e}")
            input("\nException. Press Enter to close browser...")
            raise
        finally:
            await context.close()
