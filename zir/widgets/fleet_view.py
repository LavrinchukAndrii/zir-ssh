"""The `Fleet` tab: every host in one table, Enter jumps to the host tab."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Static

from ..formatting import human_ms, human_rate, human_uptime, net_total_rate
from ..i18n import _, _n
from ..store import OFF, PENDING, HostStore
from .badges import BADGE_COLORS, badge
from .tables import cell_key

DASH = "—"

COLUMNS = (
    ("mark", ""),
    ("host", "HOST"),
    ("state", "STATE"),
    ("cpu", "CPU"),
    ("mem", "MEM"),
    ("load", "LOAD"),
    ("disk", "DISK"),
    ("failed", "FAILED"),
    ("net", "NET"),
    ("services", "SERVICES"),
    ("rps", "REQ/S"),
    ("qps", "QPS"),
    ("uptime", "UPTIME"),
    ("latency", "LATENCY"),
    ("tags", "TAGS"),
)


def _host_cell(state) -> Text:
    """Human name in bold, the technical one dimmed next to it."""
    if state.title and state.title != state.spec.name:
        return Text.assemble((state.title, "bold"), ("  " + state.spec.name, "grey50"))
    return Text(state.spec.name, style="bold")


def _services_cell(services) -> Text:
    """✓ 9 when all run, otherwise the first broken one: ■ php8.2-fpm +2."""
    if not services:
        return Text(DASH, style="grey42")
    bad = [s for s in services if s.state in ("crit", "warn")]
    running = sum(1 for s in services if s.state == "ok")
    if not bad:
        return Text(f"✓ {running}", style="green")
    first = bad[0]
    text = Text(("■ " if first.state == "crit" else "▲ ") + first.unit,
                style="bold red" if first.state == "crit" else "yellow")
    if len(bad) > 1:
        text.append(f" +{len(bad) - 1}", style="grey58")
    return text


def _rate_cell(value: float | None, baseline: float | None) -> Text:
    if value is None:
        return Text(DASH, style="grey42")
    hot = baseline is not None and baseline >= 1 and value >= max(baseline * 5, baseline + 50)
    return Text(f"{value:.0f}", style="bold red" if hot else "")


class FleetView(Vertical):
    DEFAULT_CSS = """
    FleetView {
        width: 1fr;
        height: 1fr;
    }
    FleetView > DataTable {
        width: 1fr;
        height: 1fr;
    }
    FleetView > #fleet-summary {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    class OpenHost(Message):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    def __init__(self, store: HostStore, interval_of, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        self.interval_of = interval_of
        self._signatures: dict[str, tuple] = {}  # per host: what its row was last built from
        self._shown: dict[str, list[Text]] = {}  # per host: the cells on screen

    def compose(self) -> ComposeResult:
        yield DataTable(id="fleet-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="fleet-summary")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for key, label in COLUMNS:
            table.add_column(_(label) if label else "", key=key)
        for state in self.store:
            self.add_host(state)

    def add_host(self, state) -> None:
        cells = self._cells(state, time.time())
        self.query_one(DataTable).add_row(*cells, key=state.spec.name)
        self._shown[state.spec.name] = cells
        self._signatures.pop(state.spec.name, None)

    def remove_host(self, name: str) -> None:
        table = self.query_one(DataTable)
        if name in table.rows:
            table.remove_row(name)
        self._shown.pop(name, None)
        self._signatures.pop(name, None)
        self._render_summary()

    def focus_table(self) -> None:
        self.query_one(DataTable).focus()

    def selected_host(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
        return str(key.value) if key.value is not None else None

    def _cells(self, state, now: float) -> list[Text]:
        reading = state.reading
        level = state.level
        color = BADGE_COLORS[level]
        tags = Text(", ".join(state.spec.tags), style="grey50")
        if reading is None or reading.snapshot is None or level == OFF:
            note = DASH if reading is None or level == OFF else (reading.error or DASH)
            latency = human_ms(reading.latency) if reading else DASH
            return [
                badge(state, now, self.interval_of(state.spec.name)),
                _host_cell(state),
                Text({PENDING: _("pending"), OFF: _("disabled")}.get(level, _("down")), style=color),
                *(Text(DASH) for _column in range(10)),
                Text(latency),
                Text(note, style=color if reading else "grey50"),
            ]

        snapshot = reading.snapshot
        metrics = reading.metrics
        disk = snapshot.worst_disk
        failed = len(snapshot.failed_services)
        return [
            badge(state, now, self.interval_of(state.spec.name)),
            _host_cell(state),
            Text(_("stale") if reading.error else _(level), style=color),
            Text(f"{metrics.cpu_pct:.0f}%" if metrics.cpu_pct is not None else DASH),
            Text(f"{snapshot.mem_percent:.0f}%"),
            Text(f"{snapshot.load[0]:.2f}"),
            Text(f"{disk.percent:.0f}% {disk.mount}" if disk else DASH),
            Text(str(failed), style="red" if failed else ""),
            Text(human_rate(net_total_rate(snapshot, metrics))),
            _services_cell(snapshot.stack.services),
            _rate_cell(metrics.stack.rps, metrics.stack.rps_baseline),
            _rate_cell(metrics.stack.qps, None),
            Text(human_uptime(snapshot.uptime)),
            Text(human_ms(reading.latency)),
            tags,
        ]

    def refresh_data(self, force: bool = False) -> None:
        if not self.is_mounted or not self.query(DataTable):
            return
        now = time.time()
        table = self.query_one(DataTable)
        changed = False
        for state in self.store:
            name = state.spec.name
            signature = (state.version, state.paused, state.disabled, state.title,
                         state.is_stale(now, self.interval_of(name)))
            if signature == self._signatures.get(name) and not force:
                continue
            self._signatures[name] = signature
            changed = True
            # Only the cells whose value changed: every update makes the table redraw
            # and may re-measure the whole column.
            old = self._shown.get(name)
            cells = self._cells(state, now)
            for index, ((key, _label), value) in enumerate(zip(COLUMNS, cells)):
                if old is None or cell_key(old[index]) != cell_key(value):
                    table.update_cell(name, key, value, update_width=True)
            self._shown[name] = cells
        if changed or force:
            self._render_summary()

    def _render_summary(self) -> None:
        counts = self.store.counts()
        parts = [_n("{n} host", "{n} hosts", len(self.store))]
        for key, label in (("ok", "ok"), ("warn", "warn"), ("crit", "crit"), ("error", "down"), (PENDING, "pending"), (OFF, "disabled")):
            if counts[key]:
                parts.append(f"{counts[key]} {_(label)}")
        paused = sum(1 for state in self.store if state.paused)
        if paused:
            parts.append(f"{paused} {_('paused')}")
        parts.append(time.strftime("%H:%M:%S"))
        parts.append(_("Enter → open host tab"))
        self.query_one("#fleet-summary", Static).update(" · ".join(parts))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        key = getattr(event.row_key, "value", None)
        if key:
            self.post_message(self.OpenHost(str(key)))
