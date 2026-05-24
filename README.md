# 🚆 TRA-GiveMeTheTicket

> 台鐵自動搶票工具 — 自動填表、自動辨識驗證碼、持續刷票直到搶到為止。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Playwright](https://img.shields.io/badge/Playwright-1.44%2B-green?logo=microsoft)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

---

## ✨ 功能特色

- **自動填表**：根據 `config.yaml` 自動填寫身分證、起訖站、日期、車次、票數、座位偏好
- **自動辨識驗證碼**：整合 [ddddocr](https://github.com/sml2h3/ddddocr) 自動識別台鐵圖形驗證碼，失敗時退回手動輸入
- **持續刷票**：票售完時自動重設表單並重試，支援自訂最大重試次數與間隔秒數
- **reCAPTCHA v3 規避**：使用真實 Chrome + 持久化 Profile，讓 Google reCAPTCHA 信任你的瀏覽環境
- **防偵測**：注入 Stealth JS、隱藏 `navigator.webdriver` 旗標

---

## 🖥️ 系統需求

| 項目 | 需求 |
|---|---|
| OS | Windows 10/11（主要測試環境） |
| Python | 3.10 以上 |
| Chrome | 已安裝 Google Chrome（建議使用真實 Chrome 以規避驗證碼） |

---

## 🚀 快速開始

### 1. 建立虛擬環境並安裝依賴

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
pip install ddddocr          # 選用：自動辨識驗證碼
playwright install chromium  # 安裝 Playwright 瀏覽器核心
```

### 2. 編輯設定檔

複製並修改 `config.yaml`：

```bash
# 直接編輯專案內的 config.yaml
```

```yaml
booking:
  id_number: "A123456789"        # 你的身分證字號
  departure_station: "台北"       # 出發站（中文站名）
  arrival_station: "台中"         # 抵達站
  date: "2026/06/01"             # 乘車日期（YYYY/MM/DD）
  train_number: "139"            # 車次號碼
  ticket_count: 1                # 購票張數

automation:
  headless: false                # false = 可看到瀏覽器視窗（建議）
  retry_interval: 3              # 搶票失敗後等待秒數
  max_retries: 200               # 最大重試次數
  use_real_chrome: true          # 使用真實 Chrome（強烈建議）
  chrome_profile_path: ""        # 留空 = 使用專案內的 .chrome_profile/
```

### 3. 執行

```bash
python main.py
```

---

## ⚙️ 進階設定

### 使用你的真實 Chrome Profile（強烈建議）

指定你電腦上的 Chrome 使用者資料目錄，可大幅提升 reCAPTCHA v3 信任分數，幾乎不再出現驗證碼。

> ⚠️ 執行前必須**完全關閉 Chrome**（含背景程序）。

```yaml
automation:
  use_real_chrome: true
  chrome_profile_path: "C:\\Users\\你的使用者名稱\\AppData\\Local\\Google\\Chrome\\User Data"
  kill_chrome_on_start: true     # 自動強制關閉 Chrome，免手動
```

**各系統預設 Profile 路徑：**

| OS | 路徑 |
|---|---|
| Windows | `C:\Users\<使用者名稱>\AppData\Local\Google\Chrome\User Data` |
| macOS | `/Users/<使用者名稱>/Library/Application Support/Google/Chrome` |
| Linux | `~/.config/google-chrome` |

### 座位偏好

```yaml
booking:
  seat_preference: "no_preference"  # 不指定
  # seat_preference: "window_seat"  # 桌型座優先
  accept_seat_exchange: true        # 接受同班車換座
```

---

## 🔧 命令列參數

```bash
# 使用預設 config.yaml
python main.py

# 指定其他設定檔
python main.py --config my_config.yaml

# Inspect 模式：只開啟瀏覽器到訂票頁面（用於除錯 CSS selector）
python main.py --inspect
```

---

## 📁 專案結構

```
TRA-GiveMeTheTicket/
├── main.py            # 程式入口，命令列介面
├── booker.py          # 核心訂票邏輯（填表、驗證碼、刷票迴圈）
├── debug_captcha.py   # 驗證碼辨識除錯工具
├── config.yaml        # 訂票設定檔（請勿上傳個人資料）
├── requirements.txt   # Python 依賴清單
└── .chrome_profile/   # 持久化 Chrome Profile（自動建立，已 .gitignore）
```

---

## 📋 運作流程

```
啟動程式
    │
    ▼
讀取 config.yaml
    │
    ▼
啟動 Chrome（真實 Chrome + 持久 Profile）
    │
    ▼
開啟台鐵訂票頁面
    │
    ▼
自動填寫表單（身分證、路線、日期、車次...）
    │
    ▼
自動辨識驗證碼（ddddocr）
    │
    ▼
送出訂單
    │
    ├──► 訂票成功 ✅ → 顯示訂單號碼，等待使用者按 Enter
    │
    ├──► 驗證碼錯誤 → 刷新驗證碼重新辨識，最多重試 15 次
    │
    └──► 無票 / 找不到車次 → 等待 retry_interval 秒 → 重設表單 → 繼續刷票
```

---

## 🛡️ reCAPTCHA 應對策略

台鐵使用 **reCAPTCHA v3**（不可見驗證），本工具採用以下策略：

1. **真實 Chrome + 持久 Profile**：複用你日常使用的瀏覽紀錄與 Cookie，讓 Google 給出高信任分數
2. **Stealth JS 注入**：隱藏自動化特徵（`navigator.webdriver`、`window.chrome` 等）
3. **ddddocr 圖形驗證碼辨識**：應對偶發出現的圖形驗證碼
4. **手動退路**：OCR 失敗超過 15 次時，暫停並請使用者手動輸入

---

## 📱 手機遠端使用

人在外面只有手機時，可透過以下方式遠端觸發電腦執行：

### 推薦：Chrome Remote Desktop（最簡單）

1. 電腦安裝並設定 [Chrome Remote Desktop](https://remotedesktop.google.com/access)
2. 手機安裝 **Chrome Remote Desktop** App（[iOS](https://apps.apple.com/app/chrome-remote-desktop/id944025693) / [Android](https://play.google.com/store/apps/details?id=com.google.chromeremotedesktop)）
3. 用 Google 帳號登入，連回電腦後直接執行程式

> 💡 電腦需保持開機並連網。不需修改任何程式碼，完全相容。

---

## ❗ 注意事項

- 請遵守台鐵訂票系統規章，勿以此工具進行黃牛行為
- `config.yaml` 含有身分證字號等個人資料，**請勿上傳至公開 GitHub Repo**
- 成功搶到票後請務必在期限內完成付款，否則訂單將自動取消

---

## 🤝 貢獻

歡迎 Issue 回報問題或提交 Pull Request。

---

## 📄 License

MIT License
