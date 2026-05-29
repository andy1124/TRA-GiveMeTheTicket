"""
台鐵驗證碼人工標記工具

功能：
  - 從 errors/ 載入圖片，顯示 ddddocr 上次預測（檔名推斷）作為參考
  - 使用者輸入正確答案（自動 lowercase），按 Enter 確認 → 移至 labeled/
  - 按 's' / [略過] → 移至 uncertain/
  - 按 'd' / [刪除] → 刪除（品質太差的圖）
  - 即時顯示 ddddocr 重新預測作為輔助提示
  - 進度計數

用法：
    python label_tool.py                        # 預設處理 captcha_dataset/errors/
    python label_tool.py --source errors        # 也可指定 labeled 來校正現有標記
    python label_tool.py --dir path/to/dataset  # 指定 dataset 根目錄
"""

import argparse
import re
import shutil
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox

from PIL import Image, ImageTk

try:
    import ddddocr
    _ocr = ddddocr.DdddOcr(show_ad=False)
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

VALID_PATTERN = re.compile(r"^[a-z0-9]{6}$")
ERRORS_FILENAME_PATTERN = re.compile(r"^(\d+)_([A-Za-z0-9]*)\.png$")
LABELED_FILENAME_PATTERN = re.compile(r"^([A-Za-z0-9]+)_(\d+)\.png$")

ZOOM = 4  # 放大倍數（原圖約 160×60，放大後 640×240）


def ddddocr_predict(img_path: Path) -> str:
    if not OCR_AVAILABLE:
        return ""
    try:
        with open(img_path, "rb") as f:
            result = _ocr.classification(f.read()).strip().lower()
        return result
    except Exception:
        return ""


def extract_prev_prediction(filename: str) -> str:
    """從 errors/ 的檔名取出 ddddocr 上次的預測（參考用）。"""
    m = ERRORS_FILENAME_PATTERN.match(filename)
    if m:
        return m.group(2).lower()
    m = LABELED_FILENAME_PATTERN.match(filename)
    if m:
        return m.group(1).lower()
    return ""


class LabelTool:
    def __init__(self, root: tk.Tk, source_dir: Path, labeled_dir: Path, uncertain_dir: Path):
        self.root = root
        self.source_dir = source_dir
        self.labeled_dir = labeled_dir
        self.uncertain_dir = uncertain_dir

        self.files: list[Path] = []
        self.idx = 0
        self.done = self.skipped = self.deleted = 0

        self._load_files()
        self._build_ui()
        self._show_current()

    def _load_files(self):
        self.files = sorted(self.source_dir.glob("*.png"))

    def _build_ui(self):
        self.root.title("台鐵驗證碼標記工具")
        self.root.resizable(False, False)
        self.root.bind("<Return>", self._on_confirm)
        self.root.bind("<Escape>", lambda e: self._on_skip())
        self.root.bind("s", lambda e: self._on_skip() if self.root.focus_get() != self.entry else None)
        self.root.bind("d", lambda e: self._on_delete() if self.root.focus_get() != self.entry else None)

        big_font = tkfont.Font(family="Consolas", size=18, weight="bold")
        hint_font = tkfont.Font(family="Consolas", size=12)
        small_font = tkfont.Font(family="Consolas", size=10)

        # 進度列
        self.progress_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.progress_var, font=small_font,
                 fg="#666").pack(pady=(8, 0))

        # 圖片區
        self.img_label = tk.Label(self.root, bd=2, relief="sunken", bg="#f0f0f0")
        self.img_label.pack(padx=16, pady=8)

        # 上次預測 & ddddocr 建議
        self.hint_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.hint_var, font=hint_font,
                 fg="#1a6bb5").pack()

        # 輸入框
        input_frame = tk.Frame(self.root)
        input_frame.pack(pady=6)
        tk.Label(input_frame, text="答案：", font=big_font).pack(side="left")
        self.entry_var = tk.StringVar()
        self.entry_var.trace_add("write", self._on_entry_change)
        self.entry = tk.Entry(input_frame, textvariable=self.entry_var,
                              font=big_font, width=8, justify="center")
        self.entry.pack(side="left")
        self.entry.focus_set()

        self.entry_status = tk.Label(self.root, text="", font=small_font)
        self.entry_status.pack()

        # 按鈕列
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=(4, 12))
        tk.Button(btn_frame, text="確認 (Enter)", command=self._on_confirm,
                  font=hint_font, bg="#4caf50", fg="white", width=12).pack(side="left", padx=4)
        tk.Button(btn_frame, text="略過 (S)", command=self._on_skip,
                  font=hint_font, width=10).pack(side="left", padx=4)
        tk.Button(btn_frame, text="刪除 (D)", command=self._on_delete,
                  font=hint_font, bg="#e53935", fg="white", width=10).pack(side="left", padx=4)

        # 狀態列
        self.status_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.status_var, font=small_font,
                 fg="#555").pack(pady=(0, 4))

    def _show_current(self):
        if self.idx >= len(self.files):
            self._finish()
            return

        path = self.files[self.idx]
        total = len(self.files)
        self.progress_var.set(
            f"進度：{self.idx + 1} / {total}  "
            f"（已標記 {self.done}，略過 {self.skipped}，刪除 {self.deleted}）"
        )

        # 顯示放大圖
        try:
            img = Image.open(path)
            w, h = img.size
            img = img.resize((w * ZOOM, h * ZOOM), Image.NEAREST)
            self._photo = ImageTk.PhotoImage(img)
            self.img_label.config(image=self._photo)
        except Exception as e:
            self.img_label.config(image="", text=f"[圖片讀取失敗: {e}]")

        # 提示
        prev = extract_prev_prediction(path.name)
        ocr_hint = ddddocr_predict(path)
        hint_parts = []
        if prev:
            hint_parts.append(f"上次預測：{prev}")
        if ocr_hint and ocr_hint != prev:
            hint_parts.append(f"ddddocr：{ocr_hint}")
        elif ocr_hint:
            hint_parts.append(f"ddddocr（同上）：{ocr_hint}")
        self.hint_var.set("  |  ".join(hint_parts) if hint_parts else "（無預測）")

        # 預填 ddddocr 建議到輸入框
        best_guess = ocr_hint or prev or ""
        if VALID_PATTERN.match(best_guess):
            self.entry_var.set(best_guess)
            self.entry.select_range(0, "end")
        else:
            self.entry_var.set("")

        self.entry_status.config(text="")
        self.status_var.set(f"檔案：{path.name}")
        self.entry.focus_set()

    def _on_entry_change(self, *_):
        val = self.entry_var.get().lower()
        # 自動 lowercase（不干擾游標）
        if val != self.entry_var.get():
            self.entry_var.set(val)
            self.entry.icursor("end")

        if len(val) == 0:
            self.entry_status.config(text="", fg="black")
        elif len(val) < 6:
            self.entry_status.config(text=f"{len(val)}/6 字元", fg="#f57c00")
        elif len(val) == 6 and VALID_PATTERN.match(val):
            self.entry_status.config(text="✓ 格式正確", fg="#388e3c")
        else:
            self.entry_status.config(text="✗ 需 6 個英數字元", fg="#e53935")

    def _on_confirm(self, _event=None):
        if self.idx >= len(self.files):
            return
        val = self.entry_var.get().lower().strip()
        if not VALID_PATTERN.match(val):
            self.entry_status.config(text="✗ 需要剛好 6 個英數字元（a-z0-9）", fg="#e53935")
            return

        src = self.files[self.idx]
        ts = int(time.time())
        dst = self.labeled_dir / f"{val}_{ts}.png"
        # 若目標已存在（同標記同秒），加後綴
        counter = 0
        while dst.exists():
            counter += 1
            dst = self.labeled_dir / f"{val}_{ts}_{counter}.png"

        shutil.move(str(src), str(dst))
        self.done += 1
        self.status_var.set(f"✓ 已存為 {dst.name}")
        self._next()

    def _on_skip(self):
        if self.idx >= len(self.files):
            return
        src = self.files[self.idx]
        dst = self.uncertain_dir / src.name
        counter = 0
        while dst.exists():
            counter += 1
            dst = self.uncertain_dir / f"{src.stem}_{counter}.png"
        shutil.move(str(src), str(dst))
        self.skipped += 1
        self.status_var.set(f"略過 → {dst.name}")
        self._next()

    def _on_delete(self):
        if self.idx >= len(self.files):
            return
        src = self.files[self.idx]
        if messagebox.askyesno("確認刪除", f"確定刪除 {src.name}？"):
            src.unlink()
            self.deleted += 1
            self.status_var.set("已刪除")
            self._next()

    def _next(self):
        self.idx += 1
        self._show_current()

    def _finish(self):
        self.img_label.config(image="", text="🎉 所有圖片處理完畢！", font=tkfont.Font(size=20))
        self.hint_var.set("")
        self.entry.config(state="disabled")
        self.status_var.set(
            f"完成：標記 {self.done} 張，略過 {self.skipped} 張，刪除 {self.deleted} 張"
        )
        labeled_count = sum(1 for _ in self.labeled_dir.glob("*.png"))
        self.progress_var.set(f"labeled/ 目前共 {labeled_count} 張")


def main():
    parser = argparse.ArgumentParser(description="台鐵驗證碼人工標記工具")
    parser.add_argument("--dir", default="captcha_dataset",
                        help="dataset 根目錄（預設：captcha_dataset）")
    parser.add_argument("--source", default="errors",
                        choices=["errors", "labeled", "uncertain"],
                        help="要標記的來源子目錄（預設：errors）")
    args = parser.parse_args()

    dataset_root = Path(args.dir)
    source_dir   = dataset_root / args.source
    labeled_dir  = dataset_root / "labeled"
    uncertain_dir = dataset_root / "uncertain"

    for d in (source_dir, labeled_dir, uncertain_dir):
        d.mkdir(parents=True, exist_ok=True)

    files = list(source_dir.glob("*.png"))
    if not files:
        print(f"來源目錄 {source_dir} 中沒有 .png 檔案。")
        return

    print(f"來源：{source_dir}  （{len(files)} 張待標記）")
    print(f"目標：{labeled_dir}")

    root = tk.Tk()
    app = LabelTool(root, source_dir, labeled_dir, uncertain_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
