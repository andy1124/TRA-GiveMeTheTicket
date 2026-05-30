"""
captcha_model.py - TRA CAPTCHA CRNN Model

Architecture: CNN → BiLSTM × 2 → Linear → CTC
  - CNN:    (B,3,64,200) → (B,512,1,50)  [height halved 6×, width stable at 50]
  - BiLSTM: (50, B, 512) → (50, B, 512)
  - Linear: (50, B, 512) → (50, B, 32)   [31 chars + 1 CTC blank]

Training: nn.CTCLoss(blank=0)
Inference: ctc_greedy_decode()

Character set: 31 chars verified from 1,077 real TRA labeled images.
  Excluded: 0 (≈O), 1 (≈I/l), e, i (≈1), z  |  O included (confirmed present)
"""

from __future__ import annotations
from pathlib import Path

import torch
import torch.nn as nn

# ── Character set ─────────────────────────────────────────────────────────────
CHARS       = "23456789abcdfghjklmnopqrstuvwxy"  # 31 chars
NUM_CHARS   = len(CHARS)        # 31
NUM_CLASSES = NUM_CHARS + 1     # 32  (index 0 = CTC blank)
BLANK_IDX   = 0
SEQ_LEN     = 6                 # TRA captcha is always 6 characters

_char_to_idx: dict[str, int] = {c: i + 1 for i, c in enumerate(CHARS)}  # 1..31
_idx_to_char: dict[int, str] = {i + 1: c for i, c in enumerate(CHARS)}
CHAR_SET: frozenset[str]     = frozenset(CHARS)


def is_valid_label(label: str) -> bool:
    return len(label) == SEQ_LEN and all(c in CHAR_SET for c in label)


def encode(label: str) -> list[int]:
    """Lowercase 6-char label → index list (1-indexed, no blank)."""
    return [_char_to_idx[c] for c in label]


def decode(indices: list[int]) -> str:
    """Index list → character string (skips unknown indices)."""
    return "".join(_idx_to_char[i] for i in indices if i in _idx_to_char)


def decode_label(label_tensor: torch.Tensor) -> str:
    """Label tensor (SEQ_LEN,) → string."""
    return decode(label_tensor.tolist())


def ctc_greedy_decode(log_probs: torch.Tensor) -> list[str]:
    """Greedy CTC decode.

    Args:
        log_probs: (T, B, num_classes) — log-softmax output from CRNN.forward()
    Returns:
        List of B decoded strings (may be shorter than SEQ_LEN if model uncertain).
    """
    best = log_probs.argmax(dim=-1)  # (T, B)
    results: list[str] = []
    for b in range(best.shape[1]):
        seq = best[:, b].tolist()
        collapsed: list[int] = []
        prev = BLANK_IDX
        for idx in seq:
            if idx != BLANK_IDX and idx != prev:
                collapsed.append(idx)
            prev = idx
        results.append(decode(collapsed))
    return results


# ── Model ─────────────────────────────────────────────────────────────────────
class CRNN(nn.Module):
    """Convolutional Recurrent Neural Network for TRA CAPTCHA recognition."""

    # CNN output sequence length for input (64, 200)
    # Width: 200 → MaxPool(2,2)→100 → MaxPool(2,2)→50 → MaxPool(2,1)×4 → 50
    CNN_SEQ_LEN = 50

    def __init__(self, num_classes: int = NUM_CLASSES, rnn_hidden: int = 256):
        super().__init__()
        self.num_classes = num_classes
        self.rnn_hidden  = rnn_hidden

        def _block(in_ch: int, out_ch: int) -> nn.Sequential:
            return nn.Sequential(
                nn.Conv2d(in_ch, out_ch, kernel_size=3, padding=1, bias=False),
                nn.BatchNorm2d(out_ch),
                nn.ReLU(inplace=True),
            )

        # Input : (B,  3, 64, 200)
        # Output: (B, 512,  1,  50)
        self.cnn = nn.Sequential(
            _block(3,   32),  nn.MaxPool2d(2, 2),    # → (B, 32,  32, 100)
            _block(32,  64),  nn.MaxPool2d(2, 2),    # → (B, 64,  16,  50)
            _block(64,  128), nn.MaxPool2d((2, 1)),  # → (B, 128,  8,  50)
            _block(128, 256), nn.MaxPool2d((2, 1)),  # → (B, 256,  4,  50)
            _block(256, 512), nn.MaxPool2d((2, 1)),  # → (B, 512,  2,  50)
            _block(512, 512), nn.MaxPool2d((2, 1)),  # → (B, 512,  1,  50)
        )

        # BiLSTM: reads the 50-step sequence left-to-right and right-to-left
        self.rnn = nn.LSTM(
            input_size=512,
            hidden_size=rnn_hidden,
            num_layers=2,
            bidirectional=True,
            batch_first=False,
            dropout=0.25,
        )

        # Project each time step to class logits
        self.fc = nn.Linear(rnn_hidden * 2, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: (B, 3, H, W)  — normalised image tensor
        Returns:
            log_probs: (T, B, num_classes)  — ready for nn.CTCLoss
        """
        feat = self.cnn(x)            # (B, 512, 1, T)
        feat = feat.squeeze(2)        # (B, 512, T)
        feat = feat.permute(2, 0, 1)  # (T, B, 512)
        out, _ = self.rnn(feat)       # (T, B, hidden*2)
        out = self.fc(out)            # (T, B, num_classes)
        return torch.log_softmax(out, dim=-1)

    # ── Persistence ───────────────────────────────────────────────────────────
    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "state_dict":  self.state_dict(),
            "num_classes": self.num_classes,
            "rnn_hidden":  self.rnn_hidden,
            "chars":       CHARS,
        }, path)
        print(f"[Model] Saved → {path}")

    @classmethod
    def load(cls, path: str | Path, device: str = "cpu") -> "CRNN":
        ckpt  = torch.load(path, map_location=device)
        model = cls(num_classes=ckpt["num_classes"], rnn_hidden=ckpt["rnn_hidden"])
        model.load_state_dict(ckpt["state_dict"])
        model.to(device)
        model.eval()
        return model
