"""
train.py — TRA CAPTCHA CRNN Training Script

Dataset  : 50,000 synthetic (train) + 1,077 real labeled (90% train / 10% val)
Target   : Character Accuracy > 98%, Sequence Accuracy > 90%
Model    : saved to models/tra_captcha_crnn.pt

Prerequisites (run once):
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
    pip install tensorboard

Usage:
    python train.py                        # default: 50 epochs, batch=64
    python train.py --epochs 10            # quick smoke test
    python train.py --batch-size 32        # reduce if CUDA OOM on 4GB GPU
    python train.py --no-augment           # disable data augmentation
    tensorboard --logdir runs/             # monitor training (separate terminal)
"""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, ConcatDataset
import torchvision.transforms as T
from PIL import Image

from captcha_model import (
    CRNN, NUM_CLASSES, BLANK_IDX, SEQ_LEN,
    encode, decode_label, ctc_greedy_decode, is_valid_label,
    CRNN as CRNNModel,
)

# ── Defaults (overridable via CLI) ────────────────────────────────────────────
IMG_H        = 64
IMG_W        = 200
BATCH_SIZE   = 64
EPOCHS       = 50
LR           = 1e-3
DATASET_ROOT = Path("captcha_dataset")
MODEL_PATH   = Path("models/tra_captcha_crnn.pt")

# ── Dataset ───────────────────────────────────────────────────────────────────
class CaptchaDataset(Dataset):
    """Loads captcha images from a list of paths.

    Label is parsed from the filename stem: "ab3c9z_000001.png" → "ab3c9z"
    Images with invalid labels (wrong length or unknown chars) are skipped.
    """

    def __init__(self, paths: list[Path], transform=None):
        self.transform = transform
        self.paths: list[Path] = []
        skipped = 0
        for p in paths:
            label = p.stem.split("_")[0].lower()
            if is_valid_label(label):
                self.paths.append(p)
            else:
                skipped += 1
        if skipped:
            print(f"  [Dataset] Skipped {skipped} images with invalid labels in {paths[0].parent.name}/")

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, idx: int):
        path  = self.paths[idx]
        label = path.stem.split("_")[0].lower()
        img   = Image.open(path).convert("RGB")
        if self.transform:
            img = self.transform(img)
        return img, torch.tensor(encode(label), dtype=torch.long)


# ── Transforms ────────────────────────────────────────────────────────────────
def make_transforms(augment: bool = True):
    base = [
        T.Resize((IMG_H, IMG_W)),
        T.ToTensor(),
        T.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
    ]
    if augment:
        aug = [
            T.RandomAffine(degrees=5, translate=(0.05, 0.05)),
            T.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2),
            T.GaussianBlur(kernel_size=3, sigma=(0.1, 1.0)),
        ]
        # Augmentations applied before ToTensor
        return T.Compose([T.Resize((IMG_H, IMG_W))] + aug + [T.ToTensor(), base[-1]])
    return T.Compose(base)


# ── Evaluation ────────────────────────────────────────────────────────────────
@torch.no_grad()
def evaluate(model: CRNN, loader: DataLoader, device: str) -> tuple[float, float]:
    """Returns (char_accuracy, sequence_accuracy)."""
    model.eval()
    char_correct = char_total = seq_correct = seq_total = 0

    for imgs, labels in loader:
        imgs = imgs.to(device)
        log_probs = model(imgs)                    # (T, B, C)
        preds     = ctc_greedy_decode(log_probs)   # list[str]

        for pred, label_t in zip(preds, labels):
            target  = decode_label(label_t)
            seq_total  += 1
            if pred == target:
                seq_correct += 1
            # Char accuracy: per-char comparison when lengths match
            n = len(target)
            char_total += n
            if len(pred) == n:
                char_correct += sum(a == b for a, b in zip(pred, target))
            # else: all chars wrong (pred length mismatch)

    char_acc = char_correct / max(char_total, 1)
    seq_acc  = seq_correct  / max(seq_total,  1)
    return char_acc, seq_acc


# ── Training ──────────────────────────────────────────────────────────────────
def build_dataloaders(args):
    """Build train/val DataLoaders from synthetic/ and labeled/ directories."""
    synth_dir  = DATASET_ROOT / "synthetic"
    labeled_dir = DATASET_ROOT / "labeled"

    synth_paths  = sorted(synth_dir.glob("*.png"))
    labeled_paths = sorted(labeled_dir.glob("*.png"))

    # 90/10 split on real labeled images (reproducible)
    random.seed(42)
    random.shuffle(labeled_paths)
    split = int(0.9 * len(labeled_paths))
    real_train_paths = labeled_paths[:split]
    real_val_paths   = labeled_paths[split:]

    train_tfm = make_transforms(augment=not args.no_augment)
    val_tfm   = make_transforms(augment=False)

    train_ds = ConcatDataset([
        CaptchaDataset(synth_paths,      train_tfm),
        CaptchaDataset(real_train_paths, train_tfm),
    ])
    val_ds = CaptchaDataset(real_val_paths, val_tfm)

    # num_workers=0 avoids Windows DataLoader multiprocessing issues
    nw = 0 if args.workers == 0 else args.workers
    pin = torch.cuda.is_available()

    train_loader = DataLoader(train_ds, batch_size=args.batch_size,
                              shuffle=True,  num_workers=nw, pin_memory=pin)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch_size,
                              shuffle=False, num_workers=nw, pin_memory=pin)

    print(f"[Data] train={len(train_ds):,}  val={len(val_ds)}  "
          f"(synthetic={len(synth_paths):,}, real_train={len(real_train_paths)}, real_val={len(real_val_paths)})")
    return train_loader, val_loader


def train(args):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[Device] {device.upper()}", end="")
    if device == "cuda":
        print(f" — {torch.cuda.get_device_name(0)}  "
              f"({torch.cuda.get_device_properties(0).total_memory // 1024**2} MB)")
    else:
        print(" (no GPU found, training will be slow)")

    train_loader, val_loader = build_dataloaders(args)

    # ── Model ─────────────────────────────────────────────────────────────────
    model = CRNN(num_classes=NUM_CLASSES).to(device)
    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[Model] Parameters: {n_params:,}")

    # Compute actual CNN sequence length (T) from a dummy forward
    with torch.no_grad():
        dummy = torch.zeros(1, 3, IMG_H, IMG_W, device=device)
        T_seq = model.cnn(dummy).shape[-1]   # width of feature map
    print(f"[Model] CNN sequence length T={T_seq}  (need T ≥ {2*SEQ_LEN+1} for CTC ✓)")

    # ── Optimizer & scheduler ─────────────────────────────────────────────────
    criterion = nn.CTCLoss(blank=BLANK_IDX, reduction="mean", zero_infinity=True)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=1e-5)

    # ── TensorBoard (optional) ────────────────────────────────────────────────
    writer = None
    try:
        from torch.utils.tensorboard import SummaryWriter
        writer = SummaryWriter("runs/crnn")
        print("[TensorBoard] Logging to runs/crnn  →  run: tensorboard --logdir runs/")
    except ImportError:
        print("[TensorBoard] Not installed — pip install tensorboard  (skipping logging)")

    # ── Training loop ─────────────────────────────────────────────────────────
    best_seq_acc = 0.0
    input_lengths  = torch.full((args.batch_size,), T_seq,   dtype=torch.long)
    target_lengths = torch.full((args.batch_size,), SEQ_LEN, dtype=torch.long)

    print(f"\n{'Epoch':>6}  {'Loss':>8}  {'CharAcc':>8}  {'SeqAcc':>8}  {'LR':>9}  {'Time':>6}")
    print("─" * 60)

    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss  = 0.0
        n_batches   = 0
        t0 = time.time()

        for imgs, labels in train_loader:
            imgs   = imgs.to(device)
            labels = labels.to(device)
            B      = imgs.size(0)

            log_probs = model(imgs)  # (T, B, num_classes) — on GPU

            # CTCLoss: move to CPU (safe across all PyTorch versions)
            # Gradients flow back correctly through .cpu() to GPU tensors
            il = input_lengths[:B]  if B == args.batch_size else torch.full((B,), T_seq,   dtype=torch.long)
            tl = target_lengths[:B] if B == args.batch_size else torch.full((B,), SEQ_LEN, dtype=torch.long)
            loss = criterion(log_probs.cpu(), labels.cpu(), il, tl)

            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()

            total_loss += loss.item()
            n_batches  += 1

        scheduler.step()

        avg_loss            = total_loss / n_batches
        char_acc, seq_acc   = evaluate(model, val_loader, device)
        lr_now              = scheduler.get_last_lr()[0]
        elapsed             = time.time() - t0

        print(f"{epoch:>6d}  {avg_loss:>8.4f}  {char_acc:>8.4f}  {seq_acc:>8.4f}  {lr_now:>9.2e}  {elapsed:>5.0f}s")

        if writer:
            writer.add_scalar("Loss/train",       avg_loss, epoch)
            writer.add_scalar("Accuracy/char",    char_acc, epoch)
            writer.add_scalar("Accuracy/seq",     seq_acc,  epoch)
            writer.add_scalar("LR",               lr_now,   epoch)

        if seq_acc > best_seq_acc:
            best_seq_acc = seq_acc
            model.save(MODEL_PATH)
            print(f"  ★ New best seq_acc={seq_acc:.4f} — model saved")

    if writer:
        writer.close()

    print(f"\n{'─'*60}")
    print(f"[完成] Best sequence accuracy : {best_seq_acc:.4f}")
    print(f"       Character accuracy      : depends on best epoch (check logs)")
    print(f"       Model saved to          : {MODEL_PATH}")
    if best_seq_acc >= 0.90:
        print("       ✓ 目標達成 (seq_acc ≥ 90%)")
    else:
        print("       ⚠ 目標未達 — 考慮增加 epochs 或調整 lr")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TRA CAPTCHA CRNN Training")
    parser.add_argument("--epochs",      type=int,   default=EPOCHS,     help=f"訓練週期數 (預設 {EPOCHS})")
    parser.add_argument("--batch-size",  type=int,   default=BATCH_SIZE, help=f"批次大小 (預設 {BATCH_SIZE}，OOM 時改 32)")
    parser.add_argument("--lr",          type=float, default=LR,         help=f"初始學習率 (預設 {LR})")
    parser.add_argument("--workers",     type=int,   default=0,          help="DataLoader workers (預設 0，Windows 安全)")
    parser.add_argument("--no-augment",  action="store_true",            help="停用資料增強")
    args = parser.parse_args()
    train(args)
