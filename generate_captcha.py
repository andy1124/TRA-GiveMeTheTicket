"""
generate_captcha.py - 台鐵驗證碼合成數據生成器 v3

基於真實台鐵驗證碼深度分析重新設計：
  - 尺寸：181×61 px
  - 背景：HSV 淡彩色（薰衣草、薄荷、水藍等）
  - 干擾：RGBA 半透明斜線，覆蓋在字元上方，密度與角度高度隨機
          部分圖有兩組不同角度的交叉斜線
  - 文字：6 個大寫英數字，無旋轉，斜體字型（ariali/verdanai/calibrii），各字顏色不同
  - 輕微高斯模糊

命名格式：{6-char-lowercase}_{index:06d}.png（與 labeled/ 一致）

用法：
    python generate_captcha.py                     # 生成 50,000 張
    python generate_captcha.py --count 1000
    python generate_captcha.py --preview           # 儲存 20 張樣本至 captcha_dataset/_preview/
    python generate_captcha.py --preview-count 40
"""

import argparse
import colorsys
import random
import string
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Constants ──────────────────────────────────────────────────────────────────
# TRA captcha confirmed character set (30 chars, excludes visually ambiguous ones)
# Excluded digits: 0 (like O), 1 (like I/l)
# Excluded letters: e, i (like 1/l), o (like 0), z (ambiguous in italic)
# Source: empirical analysis of 1,077 labeled real captcha images (0 occurrences of excluded chars)
LABEL_CHARS = "23456789abcdfghjklmnopqrstuvwxy"  # exactly 31 chars
N_CHARS = 6
IMG_W, IMG_H = 181, 61

FONT_SIZE_MIN = 28
FONT_SIZE_MAX = 36

# Italic fonts first — matching the real TRA captcha's slight slant style
FONT_PATHS = [
    "C:/Windows/Fonts/ariali.ttf",       # Arial Italic
    "C:/Windows/Fonts/verdanai.ttf",     # Verdana Italic
    "C:/Windows/Fonts/calibrii.ttf",     # Calibri Italic
    "C:/Windows/Fonts/timesi.ttf",       # Times New Roman Italic
    "C:/Windows/Fonts/couri.ttf",        # Courier New Italic
    "C:/Windows/Fonts/georgiai.ttf",     # Georgia Italic
    "C:/Windows/Fonts/trebucit.ttf",     # Trebuchet MS Italic
    # Regular fallbacks (in case italic variants not installed)
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/verdana.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    # Linux / Mac fallbacks
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# ── Font management ────────────────────────────────────────────────────────────
_fonts_by_size: dict[int, list] = {}
_default_font = None


def _init_fonts() -> None:
    """Pre-load italic (or regular fallback) font variants at startup."""
    global _default_font
    valid_paths = []
    for path in FONT_PATHS:
        try:
            ImageFont.truetype(path, 30)
            valid_paths.append(path)
        except Exception:
            continue

    if not valid_paths:
        print("[警告] 找不到系統字型，將使用內建點陣字型")
        _default_font = ImageFont.load_default()
        return

    italic_count = sum(
        1 for p in valid_paths
        if any(tag in p.lower() for tag in ("i.ttf", "italic", "oblique", "it.ttf"))
    )
    total = 0
    for size in range(FONT_SIZE_MIN, FONT_SIZE_MAX + 1):
        _fonts_by_size[size] = []
        for path in valid_paths:
            try:
                _fonts_by_size[size].append(ImageFont.truetype(path, size))
                total += 1
            except Exception:
                continue

    print(
        f"[字型] {len(valid_paths)} 個字型檔（斜體 {italic_count} 個），"
        f"預載 {total} 個變體"
    )


def _get_image_font():
    """Return ONE font object to use for ALL characters in a single image."""
    if not _fonts_by_size and _default_font is None:
        _init_fonts()
    if _default_font is not None:
        return _default_font
    size = random.randint(FONT_SIZE_MIN, FONT_SIZE_MAX)
    fonts = _fonts_by_size.get(size) or list(_fonts_by_size.values())[0]
    return random.choice(fonts)


# ── Color helpers ──────────────────────────────────────────────────────────────
def _clamp(v: int) -> int:
    return max(0, min(255, v))


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def _random_pastel_bg() -> tuple[tuple, float]:
    """Return (rgb_tuple, hue) for a light pastel background."""
    h = random.random()
    s = random.uniform(0.15, 0.35)
    v = random.uniform(0.82, 0.94)
    return _hsv_to_rgb(h, s, v), h


def _random_char_color(bg_hue: float) -> tuple:
    """Colorful mid-tone character color, hue-separated from background."""
    for _ in range(8):
        h = random.random()
        diff = abs(h - bg_hue)
        if min(diff, 1.0 - diff) > 0.08:
            break
    s = random.uniform(0.55, 0.90)
    v = random.uniform(0.35, 0.65)
    return _hsv_to_rgb(h, s, v)


# ── Interference lines (RGBA overlay) ────────────────────────────────────────
def _draw_line_pass(overlay_draw: ImageDraw.ImageDraw, bg: tuple, w: int, h: int) -> None:
    """Draw one pass of parallel diagonal lines onto an RGBA overlay canvas."""
    # Dense vs sparse mode — matches real captcha variance
    dense = random.random() < 0.55
    if dense:
        spacing = random.randint(3, 7)
        alpha = random.randint(45, 95)    # semi-transparent, visible
    else:
        spacing = random.randint(10, 22)
        alpha = random.randint(95, 165)   # less dense → stronger per line

    # Angle: horizontal displacement per 1 vertical pixel
    # Range covers nearly flat (5.0) to steep diagonal (0.3)
    angle = random.choice([-1, 1]) * random.uniform(0.3, 5.0)

    # Line base color: either background-shifted (subtle) or independent hue
    if random.random() < 0.6:
        shift = random.randint(20, 65)
        d = random.choice([-1, 1])
        r = _clamp(bg[0] + d * shift)
        g = _clamp(bg[1] + d * shift)
        b = _clamp(bg[2] + d * shift)
    else:
        hue = random.random()
        r, g, b = _hsv_to_rgb(hue, random.uniform(0.5, 0.9), random.uniform(0.4, 0.8))

    n_cols = w // spacing + 30
    for i in range(-10, n_cols):
        x0 = i * spacing
        y0 = 0
        x1 = x0 + int(h * angle)
        y1 = h
        # Slight per-line color jitter for natural look
        line_color = (
            _clamp(r + random.randint(-6, 6)),
            _clamp(g + random.randint(-6, 6)),
            _clamp(b + random.randint(-6, 6)),
            alpha,
        )
        overlay_draw.line([(x0, y0), (x1, y1)], fill=line_color, width=1)


def _apply_interference(img: Image.Image, bg: tuple) -> Image.Image:
    """Composite semi-transparent diagonal lines over the image (incl. over characters)."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 30% chance: two line passes at different angles (crosshatch, as seen in 2KNTTG)
    n_passes = 2 if random.random() < 0.30 else 1
    for _ in range(n_passes):
        _draw_line_pass(draw, bg, img.width, img.height)

    base = img.convert("RGBA")
    result = Image.alpha_composite(base, overlay)
    return result.convert("RGB")


# ── Character rendering ───────────────────────────────────────────────────────
def _draw_characters(base_img: Image.Image, text: str, font, bg_hue: float) -> Image.Image:
    """Render each character: NO rotation, same font size, individual colors."""
    slot_w = IMG_W // N_CHARS  # ~30 px per slot

    for i, ch in enumerate(text):
        color = _random_char_color(bg_hue)
        canvas = Image.new("RGBA", (slot_w, IMG_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)

        bbox = font.getbbox(ch)
        glyph_w = bbox[2] - bbox[0]
        glyph_h = bbox[3] - bbox[1]
        x = max(0, (slot_w - glyph_w) // 2) - bbox[0]
        y = max(0, (IMG_H - glyph_h) // 2) - bbox[1]
        d.text((x, y), ch, font=font, fill=(*color, 255))

        # No rotation — characters always upright like real TRA captcha
        base_img.paste(canvas, (i * slot_w, 0), canvas)

    return base_img


# ── Public API ────────────────────────────────────────────────────────────────
def generate_one(label: str) -> Image.Image:
    """Generate a single captcha image for the given lowercase label string."""
    font = _get_image_font()
    bg, h_bg = _random_pastel_bg()

    img = Image.new("RGB", (IMG_W, IMG_H), bg)
    img = _draw_characters(img, label.upper(), font, bg_hue=h_bg)
    img = _apply_interference(img, bg)
    img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.15, 0.45)))

    return img


def generate_batch(out_dir: Path, count: int, start_index: int = 0) -> None:
    """Generate `count` images into `out_dir`. Prints progress every 1,000 images."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _init_fonts()

    print(f"開始生成 {count:,} 張合成驗證碼 → {out_dir}")
    t0 = time.time()

    for i in range(count):
        label = "".join(random.choices(LABEL_CHARS, k=N_CHARS))
        img = generate_one(label)
        img.save(out_dir / f"{label}_{start_index + i:06d}.png")

        if (i + 1) % 1000 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (count - i - 1) / rate
            print(f"  [{i + 1:>6,}/{count:,}]  {rate:.0f} img/s  ETA {eta:.0f}s")

    elapsed = time.time() - t0
    total = sum(1 for _ in out_dir.glob("*.png"))
    print(f"\n完成！本次生成 {count:,} 張 / {elapsed:.1f}s（{count / elapsed:.0f} img/s）")
    print(f"synthetic/ 目前共 {total:,} 張")


def save_preview(out_dir: Path, count: int = 20) -> None:
    """Save sample images to out_dir for visual inspection."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _init_fonts()
    for i in range(count):
        label = "".join(random.choices(LABEL_CHARS, k=N_CHARS))
        img = generate_one(label)
        img.save(out_dir / f"preview_{i:03d}_{label}.png")
    print(f"預覽圖已儲存至 {out_dir}（共 {count} 張）")


def preview_window(count: int = 20) -> None:
    """Open a Tk window showing randomly generated samples for visual QA."""
    import tkinter as tk
    from PIL import ImageTk

    _init_fonts()
    root = tk.Tk()
    root.title(f"合成驗證碼預覽（{count} 張，2×放大）")
    cols, zoom = 5, 2

    for idx in range(count):
        label = "".join(random.choices(LABEL_CHARS, k=N_CHARS))
        img = generate_one(label)
        img_z = img.resize((IMG_W * zoom, IMG_H * zoom), Image.NEAREST)
        photo = ImageTk.PhotoImage(img_z)
        row, col = divmod(idx, cols)
        frame = tk.Frame(root, padx=4, pady=4)
        frame.grid(row=row, column=col)
        tk.Label(frame, image=photo).pack()
        tk.Label(frame, text=label, font=("Consolas", 10)).pack()
        frame.photo = photo

    root.mainloop()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="台鐵驗證碼合成數據生成器 v3")
    parser.add_argument("--count", type=int, default=50_000)
    parser.add_argument("--out-dir", default="captcha_dataset/synthetic")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--preview", action="store_true",
                        help="儲存樣本圖至 captcha_dataset/_preview/，不大量生成")
    parser.add_argument("--preview-count", type=int, default=20)
    parser.add_argument("--preview-window", action="store_true",
                        help="在 Tk 視窗中即時預覽（需有圖形介面）")
    args = parser.parse_args()

    if args.preview_window:
        preview_window(args.preview_count)
    elif args.preview:
        save_preview(out_dir=Path("captcha_dataset/_preview"), count=args.preview_count)
    else:
        generate_batch(out_dir=Path(args.out_dir), count=args.count, start_index=args.start_index)


if __name__ == "__main__":
    main()
