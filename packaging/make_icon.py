#!/usr/bin/env python3
"""Draw the zir app icon and export it for every platform.

  python packaging/make_icon.py   ->  packaging/icons/zir.png  (1024 px, README)
                                      zir/assets/zir.png     (512 px, Linux menu entry)
                                      packaging/icons/zir.ico  (Windows, 16..256 px)
                                      packaging/icons/zir.icns (macOS, needs iconutil)

The outputs are committed, so builds on CI do not need Pillow.
Design: a dark rounded "deck" with a tab strip (one server = one tab) and three
server rows with green / amber / red status lights and load meters.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "icons"
SIZE = 1024
SS = 4  # supersampling for smooth edges
S = SIZE * SS


def px(value: float) -> int:
    return round(value * SS)


def vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    width, height = size
    column = Image.new("RGBA", (1, height))
    for y in range(height):
        t = y / max(height - 1, 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return column.resize((width, height))


def horizontal_gradient(size: tuple[int, int], left: tuple, right: tuple) -> Image.Image:
    return vertical_gradient((size[1], size[0]), left, right).rotate(90, expand=True).transpose(
        Image.Transpose.FLIP_TOP_BOTTOM
    )


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def paste_rounded(canvas: Image.Image, fill: Image.Image, box: tuple[int, int, int, int], radius: int) -> None:
    x0, y0, x1, y1 = box
    size = (x1 - x0, y1 - y0)
    canvas.paste(fill.resize(size), (x0, y0), rounded_mask(size, radius))


def over(canvas: Image.Image, paint) -> None:
    """Draw on a transparent layer and blend it in (ImageDraw on RGBA overwrites alpha)."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    paint(ImageDraw.Draw(layer))
    canvas.alpha_composite(layer)


def draw_icon() -> Image.Image:
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # macOS icon grid: 824 px body centred in 1024, soft shadow under it.
    body = (px(100), px(92), px(924), px(916))
    radius = px(186)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (body[0], body[1] + px(18), body[2], body[3] + px(18)), radius, fill=(0, 0, 0, 120)
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(px(22))))

    paste_rounded(
        canvas,
        vertical_gradient((body[2] - body[0], body[3] - body[1]), (38, 52, 86, 255), (11, 16, 32, 255)),
        body,
        radius,
    )
    over(canvas, lambda d: d.rounded_rectangle(body, radius, outline=(255, 255, 255, 34), width=px(4)))

    # Tab strip: Fleet + servers, the active one in teal.
    tab_y0, tab_y1 = px(214), px(282)
    tabs = [(196, 352, False), (366, 540, True), (554, 700, False), (714, 828, False)]
    for x0, x1, active in tabs:
        color = (45, 212, 191, 255) if active else (255, 255, 255, 36)
        over(canvas, lambda d: d.rounded_rectangle((px(x0), tab_y0, px(x1), tab_y1), px(20), fill=color))
    over(canvas, lambda d: d.rounded_rectangle((px(196), px(296), px(828), px(304)), px(4), fill=(45, 212, 191, 150)))

    # Three server rows with a status light and a load meter each.
    rows = [
        ((52, 211, 153), 0.46),   # ok
        ((251, 191, 36), 0.72),   # warn
        ((248, 113, 113), 0.90),  # crit
    ]
    for index, (light, load) in enumerate(rows):
        y0 = px(352 + index * 168)
        y1 = y0 + px(132)
        paste_rounded(
            canvas,
            vertical_gradient((px(632), px(132)), (52, 69, 107, 255), (33, 45, 74, 255)),
            (px(196), y0, px(828), y1),
            px(34),
        )
        over(canvas, lambda d: d.rounded_rectangle((px(196), y0, px(828), y1), px(34), outline=(255, 255, 255, 26), width=px(3)))

        cx, cy, r = px(262), (y0 + y1) // 2, px(26)
        glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse((cx - r * 2, cy - r * 2, cx + r * 2, cy + r * 2), fill=(*light, 110))
        canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(px(14))))
        over(canvas, lambda d: d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*light, 255)))
        over(canvas, lambda d: d.ellipse((cx - r // 2.6, cy - r // 1.6, cx + r // 8, cy - r // 6), fill=(255, 255, 255, 120)))

        track = (px(326), cy - px(16), px(786), cy + px(16))
        over(canvas, lambda d: d.rounded_rectangle(track, px(16), fill=(11, 16, 32, 170)))
        filled = track[0] + round((track[2] - track[0]) * load)
        paste_rounded(
            canvas,
            horizontal_gradient((filled - track[0], track[3] - track[1]), (56, 189, 248, 255), (*light, 255)),
            (track[0], track[1], filled, track[3]),
            px(16),
        )

    return canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def export(icon: Image.Image) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon.save(OUT / "zir.png")
    # Bundled inside the package for `zir --install-desktop` (Linux menu entry).
    icon.resize((512, 512), Image.Resampling.LANCZOS).save(
        Path(__file__).resolve().parent.parent / "zir" / "assets" / "zir.png"
    )
    icon.save(OUT / "zir.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

    iconutil = shutil.which("iconutil")
    if iconutil is None:
        print("iconutil not found (not macOS): keeping the existing zir.icns", file=sys.stderr)
        return
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "zir.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            icon.resize((size, size), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            icon.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(
                iconset / f"icon_{size}x{size}@2x.png"
            )
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(OUT / "zir.icns")], check=True)


if __name__ == "__main__":
    export(draw_icon())
    print("\n".join(str(path) for path in sorted(OUT.iterdir())))
