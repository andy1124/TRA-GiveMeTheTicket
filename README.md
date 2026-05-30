# 🚆 TRA-GiveMeTheTicket

> 台鐵自動搶票工具 — 自動填表、自動辨識驗證碼、持續刷票直到搶到為止。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Playwright](https://img.shields.io/badge/Playwright-1.44%2B-green?logo=microsoft)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

---

## ✨ 功能特色

- **自動填表**：根據 `config.yaml` 自動填寫身分證、起訖站、日期、車次、票數、座位偏好
- **自訓練 CRNN 驗證碼辨識**：使用自製深度學習模型（CRNN + CTC）辨識台鐵圖形驗證碼，可持續蒐集資料再訓練提升準確率
- **持續刷票**：票售完或找不到車次時自動重設表單並重試，支援自訂最大重試次數與間隔秒數
- **多工搶票**：`jobs:` 列表格式，一次搶多人 / 多車次的票，全程共用同一個 Chrome 視窗
- **Web UI**：網頁操作介面，無需修改 YAML 即可設定訂票工作、即時查看 log、排程自動開搶
- **reCAPTCHA v3 規避**：使用真實 Chrome + 持久化 Profile + Stealth JS，讓 Google reCAPTCHA 信任你的瀏覽環境

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
playwright install chromium
```

### 2. 編輯設定檔

複製範例設定並填入你的資料：

```bash
copy config.example.yaml config.yaml
```

最簡設定（單張票）：

```yaml
booking:
  id_number: "A123456789"
  departure_station: "台北"
  arrival_station: "台中"
  date: "2026/06/01"
  train_number: "139"
  ticket_count: 1

automation:
  use_real_chrome: true
  retry_interval: 3
  max_retries: 200
```

### 3. 執行

**Web UI（推薦）**

```bash
python run_ui.py
# 自動開啟 http://localhost:8787
```

**命令列**

```bash
python main.py
```

---

## ⚙️ 設定檔參考

### 多工格式（推薦）

```yaml
jobs:
  - label: "爸爸-0622太魯閣"     # 自訂名稱（選填，顯示於 log 與 UI）
    id_number: "A123456789"
    departure_station: "台北"
    arrival_station: "台中"
    date: "2026/06/22"           # 格式：YYYY/MM/DD
    train_number: "102"
    ticket_count: 1
    seat_preference: "window"    # none / window / aisle / table
    accept_seat_exchange: true

  - label: "媽媽-0622太魯閣"
    id_number: "B987654321"
    departure_station: "台北"
    arrival_station: "台中"
    date: "2026/06/22"
    train_number: "102"
    ticket_count: 1
    seat_preference: "window"
    accept_seat_exchange: true

automation:
  headless: false
  retry_interval: 3
  max_retries: 200
  slow_mo: 300
  use_real_chrome: true
  chrome_profile_path: ""
  kill_chrome_on_start: false
  collect_captcha: false
  captcha_dataset_dir: "captcha_dataset"
  captcha_engine: "crnn"         # crnn（預設）或 ddddocr
  on_job_exhaust: "skip"         # skip（略過繼續）或 stop（立即停止）
```

> 舊格式 `booking:` 仍向後相容，只需要搶一張票時可繼續沿用。

### 座位偏好說明

| 值 | 說明 |
|---|---|
| `none` | 不指定 |
| `window` | 靠窗 |
| `aisle` | 靠走道 |
| `table` | 桌型座優先 |

### Chrome Profile 設定（強烈建議）

指定你日常使用的 Chrome 使用者資料目錄，可大幅提升 reCAPTCHA v3 信任分數，幾乎不再出現驗證碼。

> ⚠️ 執行前必須**完全關閉 Chrome**（含背景程序），或設定 `kill_chrome_on_start: true` 自動強制關閉。

```yaml
automation:
  use_real_chrome: true
  kill_chrome_on_start: true
  chrome_profile_path: "C:\\Users\\你的使用者名稱\\AppData\\Local\\Google\\Chrome\\User Data"
```

| OS | 預設 Chrome Profile 路徑 |
|---|---|
| Windows | `C:\Users\<使用者名稱>\AppData\Local\Google\Chrome\User Data` |
| macOS | `/Users/<使用者名稱>/Library/Application Support/Google/Chrome` |
| Linux | `~/.config/google-chrome` |

---

## 🖱️ Web UI

```bash
python run_ui.py
```

瀏覽器開啟 `http://localhost:8787` 後可以：

- 新增 / 刪除 / 排序搶票工作，無需手動編輯 YAML
- 即時查看搶票 log（WebSocket 串流）
- 設定排程開搶時間（例如票務開放前預先設定好時間）
- 一鍵啟動 / 停止

---

## 🔧 命令列參數

```bash
python main.py                        # 使用預設 config.yaml
python main.py --config other.yaml    # 指定其他設定檔
python main.py --inspect              # 只開啟瀏覽器（除錯用）
python main.py --collect-captcha      # 臨時啟用驗證碼蒐集模式
```

---

## 🤖 驗證碼辨識引擎

本工具支援兩種驗證碼辨識引擎，透過 `captcha_engine` 設定切換：

### CRNN（預設，自訓練模型）

基於 CNN + BiLSTM + CTC 的深度學習模型，針對台鐵驗證碼字型、配色、干擾線訓練。

**優點**：辨識準確率高（訓練完成後 >90% 序列準確率）、可持續用真實資料再訓練

需要先訓練模型，訓練後模型存放於 `models/tra_captcha_crnn.pt`。

### ddddocr（備用）

```bash
pip install ddddocr
```

```yaml
automation:
  captcha_engine: "ddddocr"
```

不需訓練即可使用，但準確率相對較低。適合在 CRNN 模型尚未訓練完成時使用。

---

## 🏋️ 訓練驗證碼模型

### 流程概覽

```
蒐集真實圖片  →  產生合成圖片  →  訓練模型  →  評估效能  →  部署使用
```

### Step 1：蒐集真實驗證碼圖片

啟用蒐集模式後，程式會在每次 OCR 預測後依結果自動存圖：

```bash
python main.py --collect-captcha
```

存放位置：

```
captcha_dataset/
├── labeled/    ← 驗證碼答對，直接作為訓練資料（檔名即 label）
├── errors/     ← 驗證碼答錯，需人工重新標記
└── uncertain/  ← 結果不明
```

人工標記工具：

```bash
python label_tool.py    # Tkinter 圖形介面，用鍵盤輸入正確答案
```

### Step 2：產生合成訓練資料

```bash
python generate_captcha.py              # 預設產生 50,000 張
python generate_captcha.py --count 10000
python generate_captcha.py --preview    # 輸出 20 張預覽至 _preview/
```

合成圖片存放於 `captcha_dataset/synthetic/`，模擬台鐵驗證碼的字型、配色與干擾線。

### Step 3：訓練模型

```bash
python train.py                         # 預設 50 個 epoch
python train.py --epochs 30
python train.py --batch-size 32

tensorboard --logdir runs/              # 監看訓練進度
```

訓練完成後模型存至 `models/tra_captcha_crnn.pt`。

### Step 4：評估效能

```bash
python benchmark_ocr.py                 # 在驗證集上比較 CRNN 與 ddddocr
python benchmark_ocr.py --n 200
python benchmark_ocr.py --no-ddddocr    # 僅測試 CRNN
```

---

## 📋 運作流程

```
啟動程式
    │
    ▼
讀取 config.yaml（支援 jobs: 多工列表 或 booking: 單筆）
    │
    ▼
預載 OCR 模型（CRNN / ddddocr）
    │
    ▼
啟動 Chrome（真實 Chrome + 持久 Profile）
    │
    ▼
對每個 job 依序執行：
    │
    ▼
開啟台鐵訂票頁面
    │
    ▼
自動填寫表單（身分證、起訖站、日期、車次、座位偏好）
    │
    ▼
自動辨識驗證碼（CRNN 或 ddddocr，最多重試 15 次）
    │
    ▼
送出訂單
    │
    ├──► 訂票成功 ✅ → 顯示訂單號碼 → 繼續下一個 job
    │
    ├──► 驗證碼錯誤 → 刷新驗證碼重新辨識
    │
    └──► 無票 / 找不到車次 → 等待 retry_interval 秒 → 重設表單 → 繼續刷票
```

---

## 📁 專案結構

```
TRA-GiveMeTheTicket/
├── main.py                # CLI 程式入口
├── run_ui.py              # Web UI 啟動器
├── booker.py              # 核心訂票邏輯（Playwright 填表、驗證碼、刷票迴圈）
├── captcha_model.py       # CRNN 模型架構定義
├── train.py               # 模型訓練腳本
├── generate_captcha.py    # 合成驗證碼產生器
├── benchmark_ocr.py       # OCR 引擎效能比較
├── label_tool.py          # 驗證碼人工標記工具（Tkinter GUI）
├── config.example.yaml    # 設定檔範例
├── requirements.txt       # Python 依賴清單
│
├── ui/
│   ├── server.py          # FastAPI 後端（REST API + WebSocket）
│   ├── booking_runner.py  # 訂票任務非同步管理器
│   └── static/
│       └── index.html     # Web UI 前端（單頁應用）
│
├── models/
│   └── tra_captcha_crnn.pt  # 訓練好的 CRNN 模型（訓練後自動生成）
│
├── captcha_dataset/         # 驗證碼訓練資料（自動建立）
│   ├── synthetic/           # 合成圖片（generate_captcha.py 輸出）
│   ├── labeled/             # 標記正確的真實圖片
│   ├── errors/              # OCR 預測錯誤的圖片
│   └── uncertain/           # 結果不明的圖片
│
└── .chrome_profile/         # 持久化 Chrome Profile（自動建立，已 .gitignore）
```

---

## 🛡️ reCAPTCHA 應對策略

台鐵使用 **reCAPTCHA v3**（不可見驗證），本工具採用以下策略：

1. **真實 Chrome + 持久 Profile**：複用你日常使用的瀏覽紀錄與 Cookie，讓 Google 給出高信任分數
2. **Stealth JS 注入**：隱藏自動化特徵（`navigator.webdriver`、`window.chrome` 等）
3. **CRNN 圖形驗證碼辨識**：應對偶發出現的圖形驗證碼
4. **手動退路**：OCR 失敗超過 15 次時，暫停並提示使用者手動輸入

---

## ❗ 注意事項

- 請遵守台鐵訂票系統規章，勿以此工具進行黃牛行為
- `config.yaml` 含有身分證字號等個人資料，**請勿上傳至公開 GitHub Repo**
- `chrome_profile_path` 使用真實 Chrome Profile 時，執行前必須完全關閉 Chrome
- 成功搶到票後請務必在期限內完成付款，否則訂單將自動取消

---

## 🤝 貢獻

歡迎 Issue 回報問題或提交 Pull Request。

---

## 📄 License

MIT License
