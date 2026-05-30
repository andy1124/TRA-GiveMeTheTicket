"""
benchmark_ocr.py — 比較 CRNN 自訓練模型 vs ddddocr 的推論速度與準確率

Usage:
    python benchmark_ocr.py              # 預設：從 labeled/ 隨機抽 100 張
    python benchmark_ocr.py --n 200      # 抽 200 張
    python benchmark_ocr.py --n 50 --no-ddddocr   # 只測 CRNN（沒裝 ddddocr 時用）
"""

import argparse
import random
import time
from pathlib import Path
from io import BytesIO

import torch
from PIL import Image
import torchvision.transforms as T

from captcha_model import CRNN, ctc_greedy_decode, is_valid_label

MODEL_PATH   = Path("models/tra_captcha_crnn.pt")
LABELED_DIR  = Path("captcha_dataset/labeled")
IMG_H, IMG_W = 64, 200

_transform = T.Compose([
    T.Resize((IMG_H, IMG_W)),
    T.ToTensor(),
    T.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
])


def load_samples(n: int) -> list[tuple[Path, str]]:
    paths = sorted(LABELED_DIR.glob("*.png"))
    valid = [(p, p.stem.split("_")[0].lower()) for p in paths if is_valid_label(p.stem.split("_")[0].lower())]

    # 重現 train.py 的 split（seed=42），只取驗證集（後 10%），避免測到訓練資料
    random.seed(42)
    random.shuffle(valid)
    split = int(0.9 * len(valid))
    val_only = valid[split:]   # 108 張，模型從未見過

    if len(val_only) < n:
        print(f"  [警告] 驗證集只有 {len(val_only)} 張，已全部使用（要求 {n} 張）")
        return val_only
    random.seed(0)
    return random.sample(val_only, n)


# ── CRNN inference ────────────────────────────────────────────────────────────

def bench_crnn(samples: list[tuple[Path, str]], device: str) -> dict:
    model = CRNN.load(MODEL_PATH, device=device)
    model.eval()

    correct = 0
    t0 = time.perf_counter()
    with torch.no_grad():
        for path, label in samples:
            img = Image.open(path).convert("RGB")
            x   = _transform(img).unsqueeze(0).to(device)
            log_probs = model(x)
            pred = ctc_greedy_decode(log_probs)[0]
            if pred == label:
                correct += 1
    elapsed = time.perf_counter() - t0

    return {
        "total":    len(samples),
        "correct":  correct,
        "seq_acc":  correct / len(samples),
        "total_ms": elapsed * 1000,
        "avg_ms":   elapsed * 1000 / len(samples),
    }


# ── ddddocr inference ─────────────────────────────────────────────────────────

def bench_ddddocr(samples: list[tuple[Path, str]]) -> dict:
    import ddddocr
    ocr = ddddocr.DdddOcr(show_ad=False)

    correct = 0
    t0 = time.perf_counter()
    for path, label in samples:
        img_bytes = path.read_bytes()
        try:
            pred = ocr.classification(img_bytes).strip().lower()
        except Exception:
            pred = ""
        if pred == label:
            correct += 1
    elapsed = time.perf_counter() - t0

    return {
        "total":    len(samples),
        "correct":  correct,
        "seq_acc":  correct / len(samples),
        "total_ms": elapsed * 1000,
        "avg_ms":   elapsed * 1000 / len(samples),
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_result(name: str, r: dict) -> None:
    print(f"\n{'─'*40}")
    print(f"  {name}")
    print(f"{'─'*40}")
    print(f"  樣本數     : {r['total']}")
    print(f"  正確數     : {r['correct']}")
    print(f"  序列準確率 : {r['seq_acc']:.2%}")
    print(f"  總時間     : {r['total_ms']:.1f} ms")
    print(f"  每張平均   : {r['avg_ms']:.2f} ms")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n",           type=int,  default=100)
    parser.add_argument("--no-ddddocr",  action="store_true")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device.upper()}")

    samples = load_samples(args.n)
    print(f"抽樣 {len(samples)} 張（來自 {LABELED_DIR}）")

    # warm-up（排除第一次載入模型、CUDA init 的時間）
    print("\n[warm-up] CRNN ...")
    model = CRNN.load(MODEL_PATH, device=device)
    model.eval()
    with torch.no_grad():
        dummy = _transform(Image.open(samples[0][0]).convert("RGB")).unsqueeze(0).to(device)
        _ = model(dummy)
    del model
    torch.cuda.empty_cache() if device == "cuda" else None

    crnn_result = bench_crnn(samples, device)
    print_result(f"CRNN (自訓練, {device.upper()})", crnn_result)

    if not args.no_ddddocr:
        try:
            ddddocr_result = bench_ddddocr(samples)
            print_result("ddddocr", ddddocr_result)

            print(f"\n{'═'*40}")
            print("  速度比較")
            print(f"{'═'*40}")
            ratio = ddddocr_result["avg_ms"] / crnn_result["avg_ms"]
            faster = "CRNN" if ratio > 1 else "ddddocr"
            print(f"  CRNN avg    : {crnn_result['avg_ms']:.2f} ms/張")
            print(f"  ddddocr avg : {ddddocr_result['avg_ms']:.2f} ms/張")
            print(f"  → {faster} 快 {abs(ratio - 1) * 100:.0f}% ({ratio:.2f}×)")
            print(f"\n  準確率比較")
            print(f"  CRNN    : {crnn_result['seq_acc']:.2%}")
            print(f"  ddddocr : {ddddocr_result['seq_acc']:.2%}")
        except ImportError:
            print("\n[skip] ddddocr 未安裝，只顯示 CRNN 結果")


if __name__ == "__main__":
    main()
