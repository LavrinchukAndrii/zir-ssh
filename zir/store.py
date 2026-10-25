"""In-memory state of every host and the persisted tab layout.

Workers write here, the UI only reads: a slow host never blocks rendering.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .collector import HostReading
from .config import HostSpec
from .paths import state_dir
from .probes import Finding, evaluate, level_of

PENDING = "pending"
OFF = "off"  # disabled by the user: never polled
BAD_LEVELS = {"crit", "error"}
BACKOFF = (5.0, 10.0, 30.0, 60.0)
STALE_FACTOR = 3
HISTORY = 300  # points per chart: 10 minutes at the default 2 s interval
SERIES = ("cpu", "mem", "load", "rx", "tx", "rps", "qps", "php", "dbconn")


def backoff_delay(failures: int) -> float:
    """Delay before the next attempt after `failures` errors in a row."""
    return BACKOFF[min(max(failures, 1), len(BACKOFF)) - 1]


@dataclass
class HostState:
    spec: HostSpec
    reading: HostReading | None = None
    history: dict[str, deque[float]] = field(
        default_factory=lambda: {name: deque(maxlen=HISTORY) for name in SERIES}
    )
    last_ok_at: float | None = None
    failures: int = 0
    paused: bool = False
    disabled: bool = False
    title: str | None = None  # human name from F2 settings or hosts.toml
    version: int = 0
    level: str = PENDING
    findings: list[Finding] = field(default_factory=list)  # of the current reading

    @property
    def label(self) -> str:
        return self.title or self.spec.name

    def is_stale(self, now: float, interval: float) -> bool:
        """SSH still answers (or is paused), but the data is too old to trust."""
        if self.last_ok_at is None or self.level in ("error", OFF):
            return False
        return now - self.last_ok_at > STALE_FACTOR * interval


def _record(state: HostState, reading: HostReading) -> None:
    snapshot, metrics, history = reading.snapshot, reading.metrics, state.history
    if metrics.cpu_pct is None:
        return  # first sample: no rates yet
    history["cpu"].append(metrics.cpu_pct)
    history["mem"].append(snapshot.mem_percent)
    history["load"].append(snapshot.load[0])
    rx = sum(r for name, (r, _t) in metrics.net_rates.items() if name != "lo")
    tx = sum(t for name, (_r, t) in metrics.net_rates.items() if name != "lo")
    history["rx"].append(rx)
    history["tx"].append(tx)
    stack, rates = snapshot.stack, metrics.stack
    if rates.rps is not None:
        history["rps"].append(rates.rps)
    if rates.qps is not None:
        history["qps"].append(rates.qps)
        history["dbconn"].append(float(stack.mysql.status.get("Threads_connected", 0)))
    workers = [g for g in stack.of("php-fpm") if g.group != "master"]
    if workers:
        history["php"].append(float(sum(g.running for g in workers)))


def set_disabled(state: HostState, disabled: bool) -> None:
    state.disabled = disabled
    state.level = OFF if disabled else PENDING
    state.findings = []
    state.version += 1


class HostStore:
    def __init__(self, specs: list[HostSpec]) -> None:
        self.hosts: dict[str, HostState] = {spec.name: HostState(spec) for spec in specs}

    def __getitem__(self, name: str) -> HostState:
        return self.hosts[name]

    def __contains__(self, name: str) -> bool:
        return name in self.hosts

    def add(self, spec: HostSpec) -> HostState:
        state = self.hosts[spec.name] = HostState(spec)
        return state

    def remove(self, name: str) -> None:
        self.hosts.pop(name, None)

    def __iter__(self):
        return iter(self.hosts.values())

    def __len__(self) -> int:
        return len(self.hosts)

    def update(self, name: str, reading: HostReading) -> bool:
        """Store a reading; True when the host has just become crit/down."""
        state = self.hosts[name]
        if state.disabled:
            return False  # a poll that was already running when the host got disabled
        previous = state.level
        state.reading = reading
        state.version += 1

        if reading.ok:
            state.failures = 0
            state.last_ok_at = reading.at
            _record(state, reading)
        else:
            state.failures += 1

        # Evaluated once per reading; the tab badge, Attention and Attacks reuse it.
        if reading.snapshot is not None and not reading.error:
            state.findings = evaluate(reading.snapshot, reading.metrics)
            state.level = level_of(state.findings)
        else:
            state.findings = []
            state.level = "error"
        return state.level in BAD_LEVELS and previous not in BAD_LEVELS

    def counts(self) -> dict[str, int]:
        result = {key: 0 for key in ("ok", "warn", "crit", "error", PENDING, OFF)}
        for state in self:
            result[state.level] += 1
        return result


# --------------------------------------------------------------------------
# Session (open tabs, order, active tab, active section per host)
# --------------------------------------------------------------------------


def session_path() -> Path:
    return state_dir() / "session.json"


@dataclass
class Session:
    open: list[str] = field(default_factory=list)
    active: str | None = None
    sections: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Session":
        path = path or session_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        def _strings(value) -> list[str]:
            return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

        sections = data.get("sections")
        return cls(
            open=_strings(data.get("open")),
            active=data.get("active") if isinstance(data.get("active"), str) else None,
            sections={
                key: value
                for key, value in (sections.items() if isinstance(sections, dict) else ())
                if isinstance(key, str) and isinstance(value, str)
            },
        )

    def save(self, path: Path | None = None) -> None:
        path = path or session_path()
        payload = {
            "open": self.open,
            "active": self.active,
            "sections": self.sections,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # losing the layout is not worth crashing the UI
