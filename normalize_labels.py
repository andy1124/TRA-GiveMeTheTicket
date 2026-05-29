"""
將 captcha_dataset/labeled/ 裡所有檔名的 label 部分統一轉為小寫。

背景：台鐵驗證碼伺服器驗證為 case-insensitive，ddddocr 猜對字母但大小寫不一定吻合實際圖片。
統一 lowercase 後可將 character set 從 62 縮減為 36（a-z0-9），簡化訓練。

命名規則：{label}_{timestamp}.png → label 部分 lowercase，timestamp 不動。

用法：
    python normalize_labels.py                     # 預設路徑 captcha_dataset/labeled/
    python normalize_labels.py --dry-run           # 只印出改動，不實際 rename
    python normalize_labels.py --dir path/to/dir   # 指定目錄
"""

import argparse
import re
from pathlib import Path


FILENAME_PATTERN = re.compile(r"^([A-Za-z0-9]+)_(\d+)\.png$")


def normalize_dir(labeled_dir: Path, dry_run: bool) -> None:
    files = sorted(labeled_dir.glob("*.png"))
    if not files:
        print(f"目錄 {labeled_dir} 中沒有 .png 檔案。")
        return

    renamed = skipped = already_ok = 0

    for src in files:
        m = FILENAME_PATTERN.match(src.name)
        if not m:
            print(f"  [SKIP] 檔名格式不符：{src.name}")
            skipped += 1
            continue

        label, ts = m.group(1), m.group(2)
        new_label = label.lower()

        if new_label == label:
            already_ok += 1
            continue

        dst = src.parent / f"{new_label}_{ts}.png"
        # Windows 檔案系統 case-insensitive：dst.exists() 對 case-only rename 會誤判為衝突
        # 只有真正不同檔案（非自身 case 變體）才算衝突
        if dst.exists() and dst.resolve() != src.resolve():
            print(f"  [CONFLICT] 目標已存在，略過：{src.name} → {dst.name}")
            skipped += 1
            continue

        print(f"  rename: {src.name}  →  {dst.name}")
        if not dry_run:
            # Windows case-only rename 需透過暫時名稱中轉
            tmp = src.parent / f"__tmp_{src.name}"
            src.rename(tmp)
            tmp.rename(dst)
        renamed += 1

    print(f"\n完成：rename {renamed} 筆，已是小寫 {already_ok} 筆，略過 {skipped} 筆。")
    if dry_run:
        print("（dry-run 模式：未實際改名）")


def main():
    parser = argparse.ArgumentParser(description="Lowercase normalize captcha labels")
    parser.add_argument("--dir", default="captcha_dataset/labeled",
                        help="labeled 目錄路徑（預設：captcha_dataset/labeled）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出改動，不實際 rename")
    args = parser.parse_args()

    labeled_dir = Path(args.dir)
    if not labeled_dir.is_dir():
        print(f"找不到目錄：{labeled_dir}")
        return

    print(f"目錄：{labeled_dir.resolve()}")
    print(f"模式：{'dry-run' if args.dry_run else '實際 rename'}\n")
    normalize_dir(labeled_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
