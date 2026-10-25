"""Area charts for the terminal: 1/8-block resolution, gradient fill, y-axis.

`mirror` draws a second series downwards from the middle (network rx/tx).
Rendering is O(width × height) and only happens when data or size changes.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Callable, Sequence

from rich.style import Style
from rich.text import Text
from textual.widget import Widget

# The classic Windows console (conhost) fonts have only the half blocks ▄ ▀ █
# and draw ▁▂▃▅▆▇▔ as "?" boxes; Windows Terminal (WT_SESSION) has them all.
LIMITED_GLYPHS = sys.platform == "win32" and "WT_SESSION" not in os.environ

if LIMITED_GLYPHS:
    EIGHTHS = " ▄▄▄▄▄▄██"
    UPPER = {0: " ", 1: "▀", 2: "▀", 3: "▀", 4: "▀", 5: "▀", 6: "▀", 7: "█", 8: "█"}
    AVERAGE = "avg"
else:
    EIGHTHS = " ▁▂▃▄▅▆▇█"
    UPPER = {0: " ", 1: "▔", 2: "▔", 3: "▔", 4: "▀", 5: "▀", 6: "▀", 7: "▀", 8: "█"}
    AVERAGE = "⌀"

PALETTES = {
    "cpu": ("#1f6f5c", "#34d399"),
    "mem": ("#3b3f8f", "#818cf8"),
    "load": ("#6b4e16", "#fbbf24"),
    "rx": ("#155e75", "#38bdf8"),
    "tx": ("#6b2160", "#f472b6"),
    "rps": ("#14532d", "#4ade80"),
    "qps": ("#7c2d12", "#fb923c"),
    "php": ("#4c1d95", "#a78bfa"),
    "conn": ("#713f12", "#facc15"),
}
ALERT = ("#7f1d1d", "#f87171")


def _mix(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


@lru_cache(maxsize=512)
def _shade(palette: str, alert: bool, level: int, total: int) -> Style:
    """Colour of one chart row: the gradient step of its palette (red above the alert line)."""
    low, high = ALERT if alert else PALETTES.get(palette, PALETTES["cpu"])
    return Style(color=_mix(low, high, (level + 1) / max(total, 1)))


_DOT = Style.parse("grey23")


class _Runs:
    """Builds one chart line, appending runs of cells with the same style at once."""

    __slots__ = ("line", "chars", "style")

    def __init__(self, line: Text) -> None:
        self.line, self.chars, self.style = line, [], None

    def add(self, char: str, style: Style | None) -> None:
        if style is not self.style:
            self.flush()
            self.style = style
        self.chars.append(char)

    def flush(self) -> Text:
        if self.chars:
            self.line.append("".join(self.chars), style=self.style)
            self.chars = []
        return self.line


def _plain(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


class Chart(Widget):
    DEFAULT_CSS = """
    Chart {
        height: 8;
        min-height: 4;
        width: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        title: str = "",
        palette: str = "cpu",
        maximum: float | None = None,
        fmt: Callable[[float], str] = _plain,
        mirror_palette: str | None = None,
        alert_at: float | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.title = title
        self.palette = palette
        self.maximum = maximum
        self.fmt = fmt
        self.mirror_palette = mirror_palette
        self.alert_at = alert_at  # values at/above this are drawn in red
        self.values: list[float] = []
        self.mirror: list[float] = []
        self.note = ""
        self._key: tuple | None = None

    def set_data(
        self,
        values: Sequence[float],
        mirror: Sequence[float] = (),
        title: str | None = None,
        note: str = "",
    ) -> None:
        values = values if isinstance(values, list) else list(values)
        mirror = mirror if isinstance(mirror, list) else list(mirror)
        title = self.title if title is None else title
        key = (tuple(values[-400:]), tuple(mirror[-400:]), title, note, self.maximum, self.alert_at)
        if key == self._key:
            return
        self._key = key
        self.values, self.mirror, self.title, self.note = values, mirror, title, note
        self.refresh()

    # ------------------------------------------------------------------

    def _summary(self, values: list[float]) -> str:
        if not values:
            return ""
        now, avg, top = values[-1], sum(values) / len(values), max(values)
        return f"{self.fmt(now)} · {AVERAGE} {self.fmt(avg)} · ▲ {self.fmt(top)}"

    def render(self) -> Text:
        width, height = self.content_size.width, self.content_size.height
        text = Text(no_wrap=True, overflow="crop")
        if width < 8 or height < 2:
            return text

        # Header: title left, now / average / max right.
        summary = self._summary(self.values)
        if self.mirror:
            summary = f"↓ {self.fmt(self.values[-1]) if self.values else '—'}  ↑ {self.fmt(self.mirror[-1])}"
        # Keep the header on one line: shorten the title first, then drop the summary.
        title = self.title
        if len(title) + len(summary) + 1 > width:
            room = width - len(summary) - 2
            if room >= 8:
                title = title[: room - 1] + "…" if len(title) > room else title
            else:
                summary = ""
                title = title[: width - 1] + "…" if len(title) > width else title
        text.append(title, style="bold")
        text.append(" " * max(1, width - len(title) - len(summary)))
        text.append(summary, style="grey70")

        rows = height - 1
        if not self.values and not self.mirror:
            text.append("\n" + (self.note or "…").center(width)[:width], style="grey50")
            return text

        top_value = self.maximum or max([*self.values, *self.mirror, 0.0001])
        axis_top = self.fmt(top_value)
        axis_w = max(len(axis_top), len(self.fmt(top_value / 2)), 1) + 1
        plot_w = max(1, width - axis_w)

        def align(series: list[float]) -> list[float | None]:
            tail = series[-plot_w:]
            return [None] * (plot_w - len(tail)) + tail

        up, down = align(self.values), align(self.mirror) if self.mirror else []
        up_rows = rows // 2 if self.mirror else rows
        down_rows = rows - up_rows if self.mirror else 0

        alert_at = self.alert_at
        # Heights in eighths of a row, once per value instead of once per cell.
        up_eighths = [None if v is None else round(max(v, 0) / top_value * up_rows * 8) for v in up]
        down_eighths = [None if v is None else round(max(v, 0) / top_value * down_rows * 8) for v in down]

        lines: list[Text] = []
        for row in range(up_rows):  # row 0 is the top line
            level = up_rows - 1 - row
            label = axis_top if row == 0 else ""
            if not self.mirror and row == up_rows // 2 and up_rows >= 4:
                label = self.fmt(top_value / 2)
            if row == up_rows - 1:
                label = "0"
            runs = _Runs(Text(label.rjust(axis_w - 1) + " ", style="grey42", no_wrap=True))
            normal = _shade(self.palette, False, level, up_rows)
            alert = _shade(self.palette, True, level, up_rows)
            empty = "·" if level == 0 else " "
            for value, eighths in zip(up, up_eighths):
                if eighths is None:
                    runs.add(" ", None)
                    continue
                cell = max(0, min(8, eighths - level * 8))
                if cell:
                    runs.add(EIGHTHS[cell], alert if alert_at is not None and value >= alert_at else normal)
                else:
                    runs.add(empty, _DOT)
            lines.append(runs.flush())

        mirror_palette = self.mirror_palette or "tx"
        for row in range(down_rows):  # mirrored series grows downwards from the middle
            label = axis_top if row == down_rows - 1 else ""
            runs = _Runs(Text(label.rjust(axis_w - 1) + " ", style="grey42", no_wrap=True))
            normal = _shade(mirror_palette, False, row, down_rows)
            alert = _shade(mirror_palette, True, row, down_rows)
            for value, eighths in zip(down, down_eighths):
                if eighths is None:
                    runs.add(" ", None)
                    continue
                cell = max(0, min(8, eighths - row * 8))
                runs.add(UPPER[cell], alert if alert_at is not None and value >= alert_at else normal)
            lines.append(runs.flush())

        for line in lines:
            text.append("\n")
            text.append_text(line)
        return text
