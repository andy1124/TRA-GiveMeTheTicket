"""
診斷腳本：填好訂票表單並送出，在出現驗證碼的頁面上
列出所有 img 元素並嘗試用 ddddocr 辨識。
執行：  venv/Scripts/python.exe debug_captcha.py
"""
import asyncio
import yaml
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BOOKING_FORM_URL = "https://www.railway.gov.tw/tra-tip-web/tip/tip001/tip121/query"

_CAPTCHA_IMG_SELECTORS = [
    "#verifyCodeImg",
    "img[src*='verifyCode']",
    "img[src*='captcha']",
    "img[onclick*='verifyCode']",
]

# ── 讀 config.yaml ────────────────────────────────────────────
cfg_path = Path(__file__).parent / "config.yaml"
with open(cfg_path, encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)
booking = _cfg["booking"]

ID_NUMBER         = booking["id_number"]
DEPARTURE         = booking["departure_station"]
ARRIVAL           = booking["arrival_station"]
DATE              = booking["date"]
TRAIN_NUMBER      = str(booking["train_number"])


async def fill_and_submit(page):
    """最簡化的填表 + 送出，只要能進到驗證碼頁就好。"""
    await page.wait_for_selector("#pid", timeout=15000)
    await asyncio.sleep(0.5)

    # ID
    await page.locator("#personlType").check()
    await page.locator("#pid").fill(ID_NUMBER)

    # 出發站
    inp = page.locator("#startStation")
    await inp.fill("")
    await inp.type(DEPARTURE, delay=80)
    await asyncio.sleep(0.8)
    try:
        item = page.locator(f"ul.ui-autocomplete li.ui-menu-item:has-text('{DEPARTURE}')").first
        await item.wait_for(state="visible", timeout=3000)
        await item.click()
        await asyncio.sleep(0.3)
    except PWTimeout:
        pass

    # 抵達站
    inp2 = page.locator("#endStation")
    await inp2.fill("")
    await inp2.type(ARRIVAL, delay=80)
    await asyncio.sleep(0.8)
    try:
        item2 = page.locator(f"ul.ui-autocomplete li.ui-menu-item:has-text('{ARRIVAL}')").first
        await item2.wait_for(state="visible", timeout=3000)
        await item2.click()
        await asyncio.sleep(0.3)
    except PWTimeout:
        pass

    # 單程
    await page.locator("input[name='tripType'][value='ONEWAY']").check()

    # 依車次訂票
    await page.locator("#orderType1").check()
    await asyncio.sleep(0.2)

    # 日期
    date_field = page.locator("#rideDate1")
    await date_field.click(click_count=3)
    await date_field.fill(DATE)
    await date_field.press("Escape")
    await asyncio.sleep(0.3)

    # 車次
    train = page.locator("#trainNoList1")
    await train.fill(TRAIN_NUMBER)
    await asyncio.sleep(0.2)

    # 送出
    submit = page.locator("input[type='submit'].btn-3d")
    await submit.wait_for(state="visible", timeout=10000)
    await submit.click(force=True)
    print("  [送出] 等待頁面變化（最多 20 秒）...")

    # 等任何一個結果出現
    WAIT_JS = (
        "document.querySelector('#verifyCode') !== null || "
        "document.querySelector('#verifyCodeImg') !== null || "
        "document.body.innerText.includes('訂票成功') || "
        "document.body.innerText.includes('00089') || "
        "document.body.innerText.includes('均無符合條件車次') || "
        "document.body.innerText.includes('驗證碼驗證失敗')"
    )
    try:
        await page.wait_for_function(WAIT_JS, timeout=20000)
    except PWTimeout:
        print("  [timeout] 20 秒內頁面未出現預期內容，繼續診斷...")


async def diagnose_captcha_page(page):
    print("\n=== 目前頁面 URL ===")
    print(" ", page.url)

    print("\n=== 所有 <img> 元素 ===")
    imgs = await page.locator("img").all()
    for img in imgs:
        try:
            id_     = await img.get_attribute("id") or ""
            src     = await img.get_attribute("src") or ""
            onclick = await img.get_attribute("onclick") or ""
            vis     = await img.is_visible()
            print(f"  id={id_!r:20}  visible={vis}  src={src[:70]!r}  onclick={onclick!r}")
        except Exception as e:
            print(f"  (error: {e})")

    print("\n=== Selector 測試 + ddddocr ===")
    for sel in _CAPTCHA_IMG_SELECTORS:
        try:
            el = page.locator(sel).first
            vis = await el.is_visible(timeout=2000)
            print(f"  {sel!r:38} visible={vis}", end="")
            if vis:
                shot = await el.screenshot()
                print(f"  screenshot={len(shot)}B", end="")
                try:
                    import ddddocr
                    ocr = ddddocr.DdddOcr(show_ad=False)
                    text = ocr.classification(shot).strip()
                    print(f"  ddddocr={text!r}", end="")
                except Exception as oe:
                    print(f"  ddddocr_err={oe}", end="")
            print()
        except Exception as e:
            print(f"  {sel!r:38} ERROR: {e}")

    print("\n=== #verifyCode input ===")
    inp = page.locator("#verifyCode")
    try:
        vis = await inp.is_visible(timeout=2000)
        print(f"  visible={vis}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n=== 頁面文字片段（前 500 字）===")
    try:
        txt = await page.locator("body").inner_text()
        print(txt[:500])
    except Exception:
        pass


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=150)
        page = await browser.new_page()

        print(f"前往訂票頁...")
        await page.goto(BOOKING_FORM_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=15000)

        # 關閉 cookie banner
        try:
            btn = page.locator("button.btn-cookie")
            if await btn.is_visible(timeout=2000):
                await btn.click()
        except Exception:
            pass

        print(f"填表並送出（ID={ID_NUMBER[:3]}***, 車次={TRAIN_NUMBER}）...")
        await fill_and_submit(page)

        await diagnose_captcha_page(page)

        input("\n[診斷完成，按 Enter 關閉瀏覽器]")
        await browser.close()

asyncio.run(main())
