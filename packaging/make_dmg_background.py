#!/usr/bin/env python3
"""Draw the background of the macOS installer window (zir.dmg).

  python packaging/make_dmg_background.py  ->  packaging/dmg/background.tiff

The window is 660x440 pt; the image is drawn at 1x and 2x and merged into one
Retina TIFF with `tiffutil` (macOS only). The output is committed, so builds on
CI do not need Pillow. Icon positions must match packaging/dmg_settings.py.

The layout sits in the top ~300 pt: newer Finder keeps its toolbar (and the
user's path / status bars) even when .DS_Store hides them, which eats ~125 pt.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent / "dmg"
WIDTH, HEIGHT = 660, 440
APP_X, APPS_X, ICON_Y = 170, 490, 140  # icon centres, see dmg_settings.py
HINT_Y = 262
SS = 4  # supersampling for smooth edges

NAVY = (38, 52, 86)
TEAL = (45, 212, 191)
HINT = "Перетягніть zir у Applications"
FONTS = ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Helvetica.ttc"]


def vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    width, height = size
    column = Image.new("RGBA", (1, height))
    for y in range(height):
        t = y / max(height - 1, 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return column.resize((width, height))


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def arrow_points(scale: float) -> list[tuple[float, float]]:
    """A gentle upward arc between the two icons (quadratic Bezier)."""
    x0, x1, y = APP_X + 92, APPS_X - 98, ICON_Y - 4
    cx, cy = (x0 + x1) / 2, y - 34
    points = []
    for i in range(65):
        t = i / 64
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t**2 * x1
        yy = (1 - t) ** 2 * y + 2 * (1 - t) * t * cy + t**2 * y
        points.append((x * scale, yy * scale))
    return points


def draw(scale: int) -> Image.Image:
    s = scale * SS
    size = (WIDTH * s, HEIGHT * s)
    canvas = vertical_gradient(size, (248, 249, 252, 255), (232, 236, 243, 255))

    # Soft spotlights behind the two icons so they sit on the background.
    # Blur a mask, not an RGBA layer: blurring into transparent black leaves grey halos.
    mask = Image.new("L", size, 0)
    md = ImageDraw.Draw(mask)
    for x in (APP_X, APPS_X):
        r = 88 * s
        md.ellipse((x * s - r, ICON_Y * s - r, x * s + r, ICON_Y * s + r), fill=210)
    canvas.paste((255, 255, 255, 255), (0, 0), mask.filter(ImageFilter.GaussianBlur(30 * s)))

    # Arrow: navy shaft fading into a teal head.
    points = arrow_points(s)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    width = round(5 * s)
    for i in range(len(points) - 1):
        t = i / (len(points) - 2)
        color = tuple(round(a + (b - a) * t) for a, b in zip(NAVY, TEAL)) + (round(90 + 165 * t),)
        d.line((points[i], points[i + 1]), fill=color, width=width)
        d.ellipse((points[i][0] - width / 2, points[i][1] - width / 2,
                   points[i][0] + width / 2, points[i][1] + width / 2), fill=color)
    (ex, ey), (px_, py_) = points[-1], points[-4]
    dx, dy = ex - px_, ey - py_
    length = (dx * dx + dy * dy) ** 0.5
    ux, uy = dx / length, dy / length
    head, half = 22 * s, 13 * s
    tip = (ex + ux * head * 0.55, ey + uy * head * 0.55)
    base = (tip[0] - ux * head, tip[1] - uy * head)
    d.polygon(
        [tip, (base[0] - uy * half, base[1] + ux * half), (base[0] + uy * half, base[1] - ux * half)],
        fill=(*TEAL, 255),
    )
    canvas.alpha_composite(layer)

    # Hint under the icons.
    text = ImageDraw.Draw(canvas)
    hint_font = font(15 * s)
    box = text.textbbox((0, 0), HINT, font=hint_font)
    text.text(((size[0] - (box[2] - box[0])) / 2, HINT_Y * s), HINT, font=hint_font, fill=(90, 100, 122, 255))

    return canvas.resize((WIDTH * scale, HEIGHT * scale), Image.Resampling.LANCZOS).convert("RGB")


def export() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "background.tiff"
    with tempfile.TemporaryDirectory() as tmp:
        one, two = Path(tmp) / "background.png", Path(tmp) / "background@2x.png"
        draw(1).save(one, dpi=(72, 72))
        draw(2).save(two, dpi=(144, 144))
        subprocess.run(["tiffutil", "-cathidpicheck", str(one), str(two), "-out", str(target)], check=True)
    return target


if __name__ == "__main__":
    print(export())
