"""Small formatting helpers shared by the TUI and the report mode."""

from __future__ import annotations

from .i18n import _
from .probes import Snapshot

SEVERITY_COLORS = {
    "ok": "green",
    "warn": "yellow",
    "crit": "red",
    "error": "red",
}

SEVERITY_MARKS = {
    "ok": "\u25cf",
    "warn": "\u25b2",
    "crit": "\u25a0",
    "error": "\u2716",
}


def human_bytes(value: float, precision: int = 1) -> str:
    # IEC units stay as they are in both languages (Ukrainian docs use them too).
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return f"{size:.{precision}f} PiB"


def human_rate(value: float) -> str:
    return _("{size}/s", size=human_bytes(value, 1))


def human_uptime(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _seconds = divmod(rest, 60)
    if days:
        return _("{d}d {h}h", d=days, h=hours)
    if hours:
        return _("{h}h {m}m", h=hours, m=minutes)
    return _("{m}m", m=minutes)


def human_ms(seconds: float) -> str:
    return _("{ms} ms", ms=f"{seconds * 1000:.0f}")


def net_total_rate(snapshot: Snapshot, metrics) -> float:
    total = 0.0
    for name, (rx, tx) in metrics.net_rates.items():
        if name == "lo":
            continue
        total += rx + tx
    return total


def worst_disk_percent(snapshot: Snapshot) -> float:
    disk = snapshot.worst_disk
    return disk.percent if disk else 0.0
