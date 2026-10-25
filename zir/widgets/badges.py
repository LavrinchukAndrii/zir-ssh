"""Status badges shown in tab titles and the Fleet table."""

from __future__ import annotations

from rich.text import Text
from textual.content import Content

from ..formatting import SEVERITY_COLORS
from ..store import OFF, PENDING, HostState

BADGE_MARKS = {
    "ok": "●",       # ●
    "warn": "▲",     # ▲
    "crit": "■",     # ■
    "error": "×",    # ×
    PENDING: "·",    # ·
    OFF: "⊘",        # disabled
}
BADGE_COLORS = {**SEVERITY_COLORS, PENDING: "grey50", OFF: "grey42"}
STALE_MARK = "~"
PAUSED_MARK = "‖"   # ‖


def badge(state: HostState, now: float, interval: float) -> Text:
    """`●`, `▲3`, `■1`, `×`, `·`, plus `~` when stale and `‖` when paused."""
    level = state.level
    text = Text(BADGE_MARKS[level], style=BADGE_COLORS[level])
    if state.findings and level in ("warn", "crit"):
        text.append(str(len(state.findings)), style=BADGE_COLORS[level])
    if state.is_stale(now, interval):
        text.append(STALE_MARK, style="bold yellow")
    if state.paused:
        text.append(PAUSED_MARK, style="grey70")
    return text


def tab_label(state: HostState, now: float, interval: float) -> Content:
    """Tab titles are Textual Content, not Rich Text."""
    return Content.from_rich_text(Text.assemble(state.label, " ", badge(state, now, interval)))
