"""
Taiwan Railway Auto-Booking Core Logic (Phase 1)
Selectors verified from live page JS dump 2026-05-24

Key anti-captcha strategy: persistent Chrome profile stored in .chrome_profile/
reCAPTCHA v3 scores improve as the profile accumulates cookies and history.
"""

import asyncio
import logging
import re
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

BOOKING_FORM_URL    = "https://www.railway.gov.tw/tra-tip-web/tip/tip001/tip123/query"
SUCCESS_MARKER      = "訂票成功"
FAIL_MARKER_SEAT    = "00089"
FAIL_MARKER_NONE    = "均無符合條件車次"
FAIL_MARKER_NO_SEAT = "均沒有空位"          # 新版失敗頁（2026-05 更新）
FAIL_MARKER_NO_SEAT2 = "目前查無可售座位"   # 新版失敗頁（2026-05 更新）
FAIL_MARKER_CAPTCHA = "驗證碼驗證失敗"
TRAIN_LIST_MARKER   = "目前可預訂的車次"    # queryTrain 頁面：顯示可選班次
RESET_BTN_TEXT      = "返回，重設訂票條件"
MAX_CAPTCHA_RETRIES = 15   # 單次送出最多重試幾次驗證碼

# 完整訂票頁（tip123）座位偏好 radio value 對應表
# DOM 確認（2026-05）：
#   #seatPref1 value="NONE"   label="不指定"
#   #seatPref2 value="WINDOW" label="靠窗"
#   #seatPref3 value="AISLE"  label="靠走道"
#   #seatPref4 value="TABLE"  label="桌型座優先"
# seat_preference 可用值：
#   "none" / "window" / "aisle" / "table"
#   （舊值 "window_seat" / "no_preference" 仍可使用，自動對應）
SEAT_PREF_VALUES: dict[str, str] = {
    "none":          "NONE",
    "window":        "WINDOW",
    "aisle":         "AISLE",
    "table":         "TABLE",
    # backward compat
    "window_seat":   "WINDOW",
    "no_preference": "NONE",
}

RESULT_JS = (
    "document.body.innerText.includes('訂票成功') || "
    "document.body.innerText.includes('00089') || "
    "document.body.innerText.includes('均無符合條件車次') || "
    "document.body.innerText.includes('均沒有空位') || "
    "document.body.innerText.includes('目前查無可售座位') || "
    "document.body.innerText.includes('驗證碼驗證失敗') || "
    "document.body.innerText.includes('目前可預訂的車次')"   # 列車清單頁
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
    # ── 驗證碼蒐集模式 ──────────────────────────────────────────────
    collect_captcha: bool = False          # True = 每次 ddddocr 預測後存圖
    captcha_dataset_dir: str = "captcha_dataset"  # 蒐集根目錄（相對於專案根目錄）
    # ── 多工搶票 ────────────────────────────────────────────────────
    label: str = ""              # 自訂名稱（選填，顯示用，如 "爸爸-0622太魯閣"）
    on_job_exhaust: str = "skip" # skip | stop（耗盡 max_retries 仍失敗時的行為）


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
        # 用 regex 精確比對結尾，避免「新竹」誤選「北新竹」
        # 台鐵下拉選單格式為「代碼-站名」，比對「-站名$」或「^站名$」
        exact_pattern = re.compile(rf'(^|-){re.escape(station)}$')
        item = page.locator("ul.ui-autocomplete li.ui-menu-item").filter(
            has_text=exact_pattern
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
    btn_index = 0 if "start" in field_id else 1
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


# ── 驗證碼蒐集：紀錄上一次 ddddocr 的截圖與預測，供結果回傳後分類存檔 ──
# 使用 dict 而非物件，方便在 async 函式間傳遞（不需要 thread-safe，單執行緒）
_last_captcha_attempt: dict = {}
# keys:
#   "img_bytes"  : bytes  — 截圖原始資料
#   "predicted"  : str    — ddddocr 輸出（空字串表示 OCR 失敗或手動輸入）
#   "is_auto"    : bool   — True = ddddocr 自動，False = 手動輸入（不存檔）

# 蒐集統計（整個執行期間累計）
_collect_stats: dict = {"labeled": 0, "errors": 0, "uncertain": 0}


def _safe_filename(text: str) -> str:
    """將字串中 Windows/Unix 不合法的檔名字元替換為 '_'。"""
    import re
    return re.sub(r'[\\/:*?"<>|\s]', '_', text)


def _save_captcha_sample(result: str, cfg: "BookingConfig") -> None:
    """
    依訂票結果將上一次 ddddocr 的截圖分類存檔。

    分類邏輯：
      success / fail  → captcha 被伺服器接受 → labeled/（可直接用於訓練）
      captcha_fail    → captcha 答錯          → errors/ （需人工修正）
      unknown / other → 無法判斷              → uncertain/

    檔名規則：
      labeled/   {predicted}_{timestamp}.png   ← 預測即正解，filename 就是 label
      errors/    {timestamp}_{predicted}.png   ← 錯誤樣本，預測僅供參考
      uncertain/ {timestamp}_{predicted}.png
    """
    if not cfg.collect_captcha:
        return

    attempt = _last_captcha_attempt
    if not attempt:
        return

    img_bytes: bytes = attempt.get("img_bytes", b"")
    predicted: str   = attempt.get("predicted", "")
    is_auto: bool    = attempt.get("is_auto", False)

    # 只蒐集 ddddocr 自動預測（手動輸入答案的不算，因為 label 是人打的不是 OCR）
    if not is_auto or not img_bytes or not predicted:
        _last_captcha_attempt.clear()
        return

    ts = int(time.time())
    dataset_root = Path(__file__).parent / cfg.captcha_dataset_dir

    if result in ("success", "fail"):
        # captcha 被伺服器接受 → 預測正確
        save_dir  = dataset_root / "labeled"
        filename  = f"{_safe_filename(predicted)}_{ts}.png"
        stat_key  = "labeled"
    elif result == "captcha_fail":
        # 伺服器拒絕 → 預測錯誤（留著 ddddocr 的猜測以便人工比對）
        save_dir  = dataset_root / "errors"
        filename  = f"{ts}_{_safe_filename(predicted)}.png"
        stat_key  = "errors"
    else:
        save_dir  = dataset_root / "uncertain"
        filename  = f"{ts}_{_safe_filename(predicted)}.png"
        stat_key  = "uncertain"

    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / filename).write_bytes(img_bytes)
    _collect_stats[stat_key] += 1
    logger.info(
        f"  [collect] {stat_key:9s} ← {filename}  "
        f"(total labeled={_collect_stats['labeled']} "
        f"errors={_collect_stats['errors']} "
        f"uncertain={_collect_stats['uncertain']})"
    )
    _last_captcha_attempt.clear()


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


async def _handle_captcha(page: Page, cfg: "BookingConfig | None" = None, force: bool = False) -> None:
    """
    自動辨識驗證碼（ddddocr），失敗則退回手動輸入。
    force=True：即使欄位已有值也重新辨識（驗證碼錯誤後使用）。
    cfg：傳入可啟用蒐集模式，ddddocr 結果不合規則時也會存入 errors/。
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
                    # 記錄此次截圖與預測，供送出後 _save_captcha_sample 分類存檔
                    _last_captcha_attempt.clear()
                    _last_captcha_attempt["img_bytes"] = img_bytes
                    _last_captcha_attempt["predicted"] = result
                    _last_captcha_attempt["is_auto"] = True
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
                    # 格式不合規則 = ddddocr 預測失敗，直接存入 errors/（不需等伺服器驗證）
                    if cfg and cfg.collect_captcha and img_bytes and result:
                        ts = int(time.time())
                        save_dir = Path(__file__).parent / cfg.captcha_dataset_dir / "errors"
                        save_dir.mkdir(parents=True, exist_ok=True)
                        filename = f"{ts}_{_safe_filename(result)}.png"
                        (save_dir / filename).write_bytes(img_bytes)
                        _collect_stats["errors"] += 1
                        logger.info(
                            f"  [collect] errors     ← {filename}  (invalid format: {reason})"
                            f"  (total labeled={_collect_stats['labeled']} "
                            f"errors={_collect_stats['errors']} "
                            f"uncertain={_collect_stats['uncertain']})"
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
    await _select_station(page, "startStation1", cfg.departure_station)

    logger.info(f"Arrival: {cfg.arrival_station}")
    await _select_station(page, "endStation1", cfg.arrival_station)

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
        current_qty = int(await page.locator("#normalQty1").input_value())
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

    # --- Seat preference（tip123 DOM 確認：radio value = NONE/WINDOW/AISLE/TABLE）---
    # 注意：radio 本身 pointer-events:none + position:absolute，
    #       .check() 無效，必須點對應的 <label> 才能切換選項。
    seat_value = SEAT_PREF_VALUES.get(cfg.seat_preference, "NONE")
    logger.info(f"Seat preference: {cfg.seat_preference!r} → value='{seat_value}'")
    seat_radio = page.locator(
        f"input[name='ticketOrderParamList[0].seatPref'][value='{seat_value}']"
    ).first
    if not await seat_radio.is_checked():
        radio_id = await seat_radio.get_attribute("id")
        seat_label_el = page.locator(f"label[for='{radio_id}']").first
        await seat_label_el.click()
        logger.info(f"  Seat pref label clicked: #{radio_id} ({seat_value})")
    else:
        logger.info(f"  Seat pref already set: {seat_value} (skip)")

    # --- Seat exchange（tip123 DOM 確認：#pref1，舊頁為 #chgSeat1）---
    chg = page.locator("#pref1")
    is_checked = await chg.is_checked()
    if cfg.accept_seat_exchange and not is_checked:
        await chg.check()
    elif not cfg.accept_seat_exchange and is_checked:
        await chg.uncheck()

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
    if (FAIL_MARKER_SEAT in content or FAIL_MARKER_NONE in content
            or FAIL_MARKER_NO_SEAT in content or FAIL_MARKER_NO_SEAT2 in content):
        logger.info("FAIL: no seats or no matching train")
        return "fail"
    if TRAIN_LIST_MARKER in content:
        logger.info("TRAIN LIST page detected — need to select train and proceed")
        return "train_list"
    logger.warning("UNKNOWN result - check browser")
    return "unknown"


async def select_first_train_and_next(page: Page, cfg: "BookingConfig | None" = None) -> None:
    """
    在 queryTrain 列車清單頁面，選取第一可用班次（右側 radio），
    處理驗證碼（如出現），點擊「下一步：選擇票種」後等待確認頁面載入。

    2026-05 新版流程：form submit → queryTrain → select radio → 填驗證碼 → 下一步 → 訂票成功
    驗證碼失敗時會在此頁面內自動重試（最多 MAX_CAPTCHA_RETRIES 次）。
    每次重試都會重新確認 radio 選擇，避免驗證碼失敗後選擇被取消。
    """

    async def _ensure_radio_selected() -> bool:
        """點選第一個可用班次的 radio，回傳是否成功。"""
        radio_selectors = [
            "td input[type='radio']",   # 表格列內的 radio（最具體）
            "input[type='radio']",      # 頁面上任意 radio（fallback）
        ]
        for sel in radio_selectors:
            try:
                radio = page.locator(sel).first
                if await radio.is_visible(timeout=2000):
                    await radio.click(force=True)
                    logger.info(f"  [train list] Radio 已點選（selector: {sel}）")
                    return True
                # radio 不可見時嘗試點 label（TRA 常見：radio hidden + label clickable）
                rid = await radio.get_attribute("id")
                if rid:
                    lbl = page.locator(f"label[for='{rid}']").first
                    if await lbl.is_visible(timeout=1000):
                        await lbl.click()
                        logger.info(f"  [train list] Radio label 已點選（id={rid}）")
                        return True
            except Exception as e:
                logger.debug(f"  [train list] Radio selector {sel!r} failed: {e}")
                continue
        # Fallback：直接點「選擇」欄格
        logger.warning("  [train list] 無法點選 radio，嘗試直接點擊「選擇」欄格")
        try:
            await page.locator("table tr:nth-child(2) td:last-child").first.click(force=True)
            return True
        except Exception as e:
            logger.warning(f"  [train list] 點擊「選擇」欄格失敗: {e}")
            return False

    logger.info("  [train list] 選擇第一可用班次...")
    await _ensure_radio_selected()
    await asyncio.sleep(0.5)

    # 「下一步：選擇票種」按鈕 selector 清單（共用於每次重試）
    next_btn_selectors = [
        "button:has-text('下一步：選擇票種')",
        "a:has-text('下一步：選擇票種')",
        "button:has-text('下一步')",
        "a.btn:has-text('下一步')",
    ]

    # ── 驗證碼 + 送出「下一步」迴圈 ────────────────────────────────────
    # 2026-05 新版：驗證碼出現在選車後、下一步前；驗證失敗時在此頁面重試。
    for cap_attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        # 第二次起：驗證碼答錯後，radio 選擇可能被清空，需要重新點選
        force_refresh = cap_attempt > 1
        if force_refresh:
            logger.info("  [train list] 重試前重新確認班次 radio 選擇...")
            await _ensure_radio_selected()
            await asyncio.sleep(0.3)

        # 強制刷新驗證碼圖片（上一次答錯需要新圖）並填入
        await _handle_captcha(page, cfg, force=force_refresh)

        # 點擊「下一步：選擇票種」按鈕（右下角）
        next_clicked = False
        for sel in next_btn_selectors:
            try:
                btn = page.locator(sel).last   # 用 .last 確保是頁面底部的按鈕
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    logger.info(f"  [train list] 「下一步」按鈕已點選（selector: {sel}）")
                    next_clicked = True
                    break
            except Exception as e:
                logger.debug(f"  [train list] Next btn selector {sel!r} failed: {e}")
                continue

        if not next_clicked:
            logger.warning("  [train list] 找不到「下一步」按鈕，嘗試 get_by_role...")
            try:
                btn = page.get_by_role("button", name=re.compile(r"下一步")).last
                await btn.click()
                next_clicked = True
            except Exception as e:
                logger.error(f"  [train list] 所有「下一步」嘗試均失敗: {e}")
                break   # 按鈕消失，跳出迴圈交由外層處理

        # 等待結果頁面（成功 / 付款 / 或驗證碼錯誤訊息）
        logger.info(
            f"  [train list] 等待結果頁面（captcha attempt {cap_attempt}/{MAX_CAPTCHA_RETRIES}）..."
        )
        try:
            await page.wait_for_function(
                "document.body.innerText.includes('訂票成功') || "
                "document.body.innerText.includes('票種') || "
                "document.body.innerText.includes('付款') || "
                "document.body.innerText.includes('驗證碼驗證失敗') || "
                "document.body.innerText.includes('請輸入驗證碼')",
                timeout=20000,
            )
        except PlaywrightTimeout:
            logger.warning("  [train list] 等待結果頁超時，繼續執行...")
            break

        # 判斷是驗證碼錯誤還是正常進入確認頁
        try:
            page_text = await page.inner_text("body")
        except Exception:
            page_text = ""

        if "驗證碼驗證失敗" in page_text or "請輸入驗證碼" in page_text:
            logger.warning(
                f"  [train list] 驗證碼錯誤（attempt {cap_attempt}/{MAX_CAPTCHA_RETRIES}），重試..."
            )
            if cfg is not None:
                _save_captcha_sample("captcha_fail", cfg)
            await asyncio.sleep(1.0)
            # 繼續迴圈，force_refresh 將在下一輪設為 True
        else:
            logger.info("  [train list] 訂票確認頁面已載入")
            break
    else:
        logger.error(f"  [train list] 驗證碼重試耗盡（{MAX_CAPTCHA_RETRIES} 次），放棄此輪...")


async def click_reset(page: Page) -> None:
    logger.info("Clicking reset...")
    # 以多種 selector 嘗試找「返回，重設訂票條件」按鈕（新舊版頁面通用）
    reset_selectors = [
        "button:has-text('返回，重設訂票條件')",
        "a:has-text('返回，重設訂票條件')",
        "input[value*='返回']",
        "button:has-text('重設訂票條件')",
        "a:has-text('重設訂票條件')",
    ]
    for sel in reset_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                logger.info(f"  Reset button clicked（selector: {sel}）")
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass
                await asyncio.sleep(0.5)
                return
        except Exception as e:
            logger.debug(f"  Reset selector {sel!r} failed: {e}")
            continue

    # get_by_text fallback（原始邏輯）
    try:
        reset_btn = page.get_by_text(RESET_BTN_TEXT, exact=False).first
        await reset_btn.wait_for(state="visible", timeout=3000)
        await reset_btn.click()
        logger.info("  Reset button clicked（get_by_text）")
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass
    except (PlaywrightTimeout, PlaywrightError) as e:
        # 找不到重設按鈕（頁面處於未知狀態），直接導回訂票頁
        logger.warning(f"  Reset button not found ({e.__class__.__name__}), navigating back to booking page...")
        await page.goto(BOOKING_FORM_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeout:
            pass
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
                _save_captcha_sample(result, cfg)

                if result == "success":
                    logger.info("Done! Remember to pay within the deadline.")
                    input("\nPress Enter to close browser...")
                    return True

                # ── 新版流程：列車清單頁 → 選車 → 填驗證碼 → 進入訂票確認頁 ──
                if result == "train_list":
                    await select_first_train_and_next(page, cfg)
                    result = await check_result(page)
                    _save_captcha_sample(result, cfg)
                    if result == "success":
                        logger.info("Done! Remember to pay within the deadline.")
                        input("\nPress Enter to close browser...")
                        return True
                    if result == "unknown":
                        logger.warning("Cannot determine result after train selection. Check browser...")
                        input()
                        break

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
                        # 連續失敗後稍作等待，避免觸發伺服器 throttling
                        await asyncio.sleep(1.5)
                        # force=True：強制刷新圖片並重新辨識
                        await _handle_captcha(page, cfg, force=True)
                        await submit_booking(page)
                        result = await check_result(page)
                        _save_captcha_sample(result, cfg)

                        if result == "success":
                            logger.info("Done! Remember to pay within the deadline.")
                            input("\nPress Enter to close browser...")
                            return True

                        # 驗證碼通過後出現列車清單 → 繼續選車流程
                        if result == "train_list":
                            await select_first_train_and_next(page, cfg)
                            result = await check_result(page)
                            _save_captcha_sample(result, cfg)
                            if result == "success":
                                logger.info("Done! Remember to pay within the deadline.")
                                input("\nPress Enter to close browser...")
                                return True
                            break  # 非成功則跳出內層走正常流程

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
                    # 等表單就緒
                    await page.wait_for_selector("input[type='submit'].btn-3d",
                                                  state="visible", timeout=15000)
                    # 用身分證欄位快速判斷表單資料是否還在
                    pid_val = ""
                    try:
                        pid_val = (await page.locator("#pid").input_value()).strip()
                    except Exception:
                        pass
                    if not pid_val:
                        logger.info("表單資料已消失，重新填寫...")
                        await fill_booking_form(page, cfg)
                    else:
                        logger.info(f"表單資料保留（{pid_val[:3]}*******），僅處理驗證碼")
                        await _handle_captcha(page, cfg)
                    logger.info("Form ready, retrying...")

            logger.warning(f"Max retries ({cfg.max_retries}) reached.")
            return False

        except Exception as e:
            logger.exception(f"Error: {e}")
            input("\nException. Press Enter to close browser...")
            raise
        finally:
            await context.close()


# ===================================================================
#  5. Multi-job helpers
# ===================================================================

async def run_booking_session(
    page: Page,
    cfg: BookingConfig,
    on_retry=None,
) -> bool:
    """
    Execute one booking job on an already-open page.
    Returns True on success, False on failure or max_retries exhausted.
    Does NOT manage browser lifecycle — caller opens/closes the context.
    Does NOT call input() — safe for UI/async use.
    on_retry(attempt): optional callback called at the start of each attempt.
    """
    await navigate_to_booking(page)
    await fill_booking_form(page, cfg)

    for attempt in range(1, cfg.max_retries + 1):
        if on_retry:
            on_retry(attempt)
        logger.info(f"\n-- Attempt {attempt} --")
        await submit_booking(page)
        result = await check_result(page)
        _save_captcha_sample(result, cfg)

        if result == "success":
            logger.info("Done! Remember to pay within the deadline.")
            return True

        if result == "train_list":
            await select_first_train_and_next(page, cfg)
            result = await check_result(page)
            _save_captcha_sample(result, cfg)
            if result == "success":
                logger.info("Done! Remember to pay within the deadline.")
                return True
            if result == "unknown":
                logger.warning("Cannot determine result after train selection.")
                return False

        if result == "unknown":
            logger.warning("Cannot determine result. Aborting this job.")
            return False

        if result == "captcha_fail":
            for cap_retry in range(1, MAX_CAPTCHA_RETRIES + 1):
                logger.warning(
                    f"  Captcha fail — retrying ({cap_retry}/{MAX_CAPTCHA_RETRIES})..."
                )
                await asyncio.sleep(1.5)
                await _handle_captcha(page, cfg, force=True)
                await submit_booking(page)
                result = await check_result(page)
                _save_captcha_sample(result, cfg)

                if result == "success":
                    logger.info("Done! Remember to pay within the deadline.")
                    return True

                if result == "train_list":
                    await select_first_train_and_next(page, cfg)
                    result = await check_result(page)
                    _save_captcha_sample(result, cfg)
                    if result == "success":
                        logger.info("Done! Remember to pay within the deadline.")
                        return True
                    break

                if result != "captcha_fail":
                    break

            if result == "captcha_fail":
                logger.error(
                    f"Captcha retry exhausted ({MAX_CAPTCHA_RETRIES} times). "
                    "Giving up this attempt."
                )

        if attempt < cfg.max_retries:
            logger.info(f"Waiting {cfg.retry_interval}s...")
            await asyncio.sleep(cfg.retry_interval)
            await click_reset(page)
            await page.wait_for_selector(
                "input[type='submit'].btn-3d", state="visible", timeout=15000
            )
            pid_val = ""
            try:
                pid_val = (await page.locator("#pid").input_value()).strip()
            except Exception:
                pass
            if not pid_val:
                logger.info("表單資料已消失，重新填寫...")
                await fill_booking_form(page, cfg)
            else:
                logger.info(f"表單資料保留（{pid_val[:3]}*******），僅處理驗證碼")
                await _handle_captcha(page, cfg)
            logger.info("Form ready, retrying...")

    logger.warning(f"Max retries ({cfg.max_retries}) reached.")
    return False


async def run_booking_multi(
    configs: list[BookingConfig],
    on_job_done=None,
    on_retry=None,
) -> list[bool]:
    """
    Run multiple booking jobs sequentially, sharing one Chrome context.
    on_job_done(index, success, cfg): optional callback after each job.
    Returns list of bool results in job order.
    Browser settings (profile, headless, slow_mo) taken from configs[0].
    on_job_exhaust is read from each cfg: "skip" continues, "stop" halts.
    """
    if not configs:
        return []

    first = configs[0]
    if first.chrome_profile_path:
        profile_path = first.chrome_profile_path
        logger.info(f"Using custom Chrome profile: {profile_path}")
        if first.kill_chrome_on_start:
            _kill_chrome()
    else:
        PROFILE_DIR.mkdir(exist_ok=True)
        profile_path = str(PROFILE_DIR)
        logger.info(f"Using project Chrome profile: {profile_path}")

    results: list[bool] = []

    async with async_playwright() as pw:
        channel = "chrome" if first.use_real_chrome else None
        logger.info(f"Launching persistent Chrome profile: {profile_path}")
        try:
            context: BrowserContext = await pw.chromium.launch_persistent_context(
                profile_path,
                channel=channel,
                headless=first.headless,
                slow_mo=first.slow_mo,
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
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            for i, cfg in enumerate(configs):
                job_label = cfg.label or f"Train {cfg.train_number} | {cfg.id_number[:3]}******"
                logger.info(f"\n{'='*60}")
                logger.info(f"[Job {i+1}/{len(configs)}] 開始：{job_label}")
                logger.info("=" * 60)

                try:
                    success = await run_booking_session(page, cfg, on_retry=on_retry)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[Job {i+1}] 發生例外：{e}")
                    success = False

                results.append(success)
                logger.info(
                    f"[Job {i+1}/{len(configs)}] 結果：{'✅ 成功' if success else '❌ 失敗'}"
                )

                if on_job_done:
                    on_job_done(i, success, cfg)

                if not success and cfg.on_job_exhaust == "stop":
                    logger.warning("[Job %d] on_job_exhaust=stop — 停止後續任務。", i + 1)
                    results.extend([False] * (len(configs) - len(results)))
                    break

        finally:
            await context.close()

    return results
