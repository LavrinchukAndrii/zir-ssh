"""Symbols the terminal font can draw.

The classic Windows console (conhost, used when Windows Terminal is not
installed) has Consolas / Lucida Console: no 1/8 blocks (▔ ▁ ▊ ▎, the "tall"
borders of Textual's buttons, inputs and lists), no rounded corners, no ✓ ✖ ⓘ
and no emoji. There they are replaced with symbols those fonts have.
Windows Terminal (WT_SESSION), macOS and Linux terminals keep the originals.
ZIR_PLAIN_GLYPHS=1 / 0 forces either way.
"""

from __future__ import annotations

import os
import sys


def _limited() -> bool:
    forced = os.environ.get("ZIR_PLAIN_GLYPHS")
    if forced is not None:
        return forced.strip().lower() not in ("", "0", "no", "false")
    return sys.platform == "win32" and "WT_SESSION" not in os.environ


LIMITED = _limited()

# Text symbols: replacement in the classic console ("" drops the symbol and its space).
SYMBOLS = {
    "✓": "√", "✔": "√", "✖": "×", "✕": "×", "⊘": "Ø", "ⓘ": "i", "⌀": "Ø",
    "✎": "", "📋": "", "🔑": "", "👁": "*",
}
# Border pieces: horizontal 1/8 lines become blank (the widget's own colour),
# vertical ones half blocks, rounded and heavy lines thin square ones.
BORDER_PIECES = {
    "▔": " ", "▁": " ", "▊": "▐", "▎": "▌", "▏": "│", "▕": "│",
    "╭": "┌", "╮": "┐", "╰": "└", "╯": "┘",
    "━": "─", "┃": "│", "┏": "┌", "┓": "┐", "┗": "└", "┛": "┘", "╍": "─", "╏": "│",
    "▖": " ", "▗": " ", "▘": " ", "▝": " ", "▙": "█", "▛": "█", "▜": "█", "▟": "█",
}
HEADER_ICON = "≡" if LIMITED else None  # Textual's default is ⭘


def plain(text: str, limited: bool | None = None) -> str:
    """`text` with the symbols the console font lacks replaced."""
    if not (LIMITED if limited is None else limited) or text.isascii():
        return text
    for symbol, replacement in SYMBOLS.items():
        if symbol in text:
            text = text.replace(symbol + " ", replacement + " " if replacement else "")
            text = text.replace(symbol, replacement)
    return text


def adapt_textual() -> None:
    """Swap Textual's border characters for ones the classic console can draw."""
    if not LIMITED:
        return
    from textual import _border

    table = str.maketrans(BORDER_PIECES)
    for name, rows in list(_border.BORDER_CHARS.items()):
        _border.BORDER_CHARS[name] = tuple(tuple(c.translate(table) for c in row) for row in rows)
    _border.get_box.cache_clear()
