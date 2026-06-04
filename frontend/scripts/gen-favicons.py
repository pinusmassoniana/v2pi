#!/usr/bin/env python3
"""Generate raster favicons from the v2pi brand mark.

Source of truth is the in-app brand mark (App.svelte .brand-mark):
a 8/27-radius rounded square filled with a 150deg gradient from the
accent #0b6bd8 to its 58%-black mix #063e7d, with white bold "v2".
public/favicon.svg is the hand-authored vector twin; these PNG/ICO
files are the legacy + home-screen + PWA fallbacks.

Run from the frontend/ dir:  python3 scripts/gen-favicons.py
Outputs into public/. Requires Pillow (already a dev-machine dep).
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

ACCENT = (11, 107, 216)   # #0b6bd8  (light-theme --accent, the brand blue)
DARK = (6, 62, 125)       # #063e7d  (color-mix #0b6bd8 58% / #000)
RADIUS_RATIO = 8 / 27     # .brand-mark: 27px box, 8px radius
SS = 4                    # supersample factor for anti-aliasing

OUT = os.path.join(os.path.dirname(__file__), "..", "public")

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _gradient(side: int) -> Image.Image:
    """150deg-ish diagonal gradient, accent (top-left) -> dark (bottom-right)."""
    img = Image.new("RGB", (side, side))
    px = img.load()
    dx, dy = 0.5, 0.866                      # CSS 150deg direction vector
    maxd = dx * (side - 1) + dy * (side - 1)
    for y in range(side):
        base = dy * y
        for x in range(side):
            t = (dx * x + base) / maxd
            px[x, y] = (
                round(ACCENT[0] + (DARK[0] - ACCENT[0]) * t),
                round(ACCENT[1] + (DARK[1] - ACCENT[1]) * t),
                round(ACCENT[2] + (DARK[2] - ACCENT[2]) * t),
            )
    return img


def _sheen(side: int) -> Image.Image:
    """White top->transparent overlay, matching the SVG #sheen gradient."""
    ov = Image.new("L", (1, side), 0)
    for y in range(side):
        t = y / (side - 1)
        a = max(0.0, 0.30 * (1 - t / 0.55)) if t < 0.55 else 0.0
        ov.putpixel((0, y), round(a * 255))
    return ov.resize((side, side))


def _mark(side: int, *, rounded: bool, text_ratio: float) -> Image.Image:
    """Render the brand mark at `side` px. rounded=True clips to the brand
    radius (tab/legacy icons); rounded=False is full-bleed for maskable /
    apple-touch (the platform applies its own mask)."""
    S = side * SS
    base = _gradient(S).convert("RGBA")

    sheen = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    sheen.putalpha(_sheen(S))
    base = Image.alpha_composite(base, sheen)

    draw = ImageDraw.Draw(base)
    font = _font(round(S * text_ratio))
    draw.text((S / 2, S / 2 + S * 0.012), "v2", font=font, fill=(255, 255, 255, 255), anchor="mm")

    if rounded:
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, S - 1, S - 1], radius=round(S * RADIUS_RATIO), fill=255
        )
        base.putalpha(mask)

    return base.resize((side, side), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    # PWA "any" icons + legacy ICO: the standalone rounded mark.
    for size in (192, 512):
        _mark(size, rounded=True, text_ratio=0.50).save(os.path.join(OUT, f"icon-{size}.png"))

    ico = _mark(64, rounded=True, text_ratio=0.52)
    ico.save(os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)])

    # Full-bleed icons: platform rounds/masks these itself.
    _mark(512, rounded=False, text_ratio=0.42).save(os.path.join(OUT, "icon-maskable-512.png"))
    _mark(180, rounded=False, text_ratio=0.44).save(os.path.join(OUT, "apple-touch-icon.png"))

    print("wrote: icon-192.png icon-512.png favicon.ico icon-maskable-512.png apple-touch-icon.png")


if __name__ == "__main__":
    main()
