"""One host tab: Overview, Web, Database, PHP, Attacks, Processes, Disks, Network, Services, Logs.

Everything is sized in fr/% so the view fills the terminal and follows resizes;
bars and truncation are computed from the current width.
"""

from __future__ import annotations

import asyncio
import time

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ..collector import HostReading
from ..formatting import human_bytes, human_ms, human_rate, human_uptime
from ..i18n import _, _n
from ..probes import Finding
from ..store import HostState
from .chart import Chart
from .snippets_section import SnippetsSection
from .stack_sections import STACK_COLUMNS, STACK_CSS, StackSections, service_state_text, services_panel, short_rate
from .tables import fill
from .terminal_section import TerminalSection

SECTIONS = (
    ("overview", "Overview"),
    ("web", "Web"),
    ("db", "Database"),
    ("php", "PHP"),
    ("attacks", "Attacks"),
    ("procs", "Processes"),
    ("disks", "Disks"),
    ("network", "Network"),
    ("services", "Services"),
    ("logs", "Logs"),
    ("terminal", "Terminal"),
    ("snippets", "Snippets"),
)
# Sections that work without monitoring data (host down, disabled, still connecting).
TOOL_SECTIONS = {"terminal", "snippets"}
NARROW = 100
TABLE_COLUMNS = {  # section -> table -> column labels
    "procs": {"#procs-table": ("PID", "USER", "CPU%", "MEM%", "RSS", "TIME", "STAT", "COMMAND")},
    "disks": {"#disks-table": ("FILESYSTEM", "MOUNT", "SIZE", "USED", "FREE", "USE", "INODE")},
    "network": {"#iface-table": ("IFACE", "RX", "TX", "RX TOTAL", "TX TOTAL"),
                "#ports-table": ("PROTO", "LISTEN", "PROCESS")},
    "services": {"#failed-table": ("UNIT", "STATE", "DESCRIPTION"),
                 "#key-services": ("SERVICE", "UNIT", "STATE", "RUNNING FOR", "MEMORY", "RESTARTS", "AUTOSTART")},
}


def _pct_style(percent: float) -> str:
    if percent >= 85:
        return "red"
    if percent >= 70:
        return "yellow"
    return "green"


def _gauge(label: str, percent: float, detail: str, width: int = 22) -> Text:
    percent = max(0.0, min(100.0, percent))
    filled = round(width * percent / 100)
    text = Text()
    text.append(f"{label:<7} ", style="bold")  # fits ПАМ'ЯТЬ
    text.append("[", style="grey50")
    text.append("█" * filled, style=_pct_style(percent))
    text.append("░" * (width - filled), style="grey23")
    text.append("] ")
    text.append(f"{percent:5.1f}%", style="bold")
    text.append(f"  {detail}", style="grey70")
    return text


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def _load(value: float) -> str:
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def _truncate(value: str, length: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= length else value[: length - 1] + "…"


class HostView(StackSections, Vertical):
    DEFAULT_CSS = STACK_CSS + """
    HostView {
        width: 1fr;
        height: 1fr;
    }
    HostView #sections, HostView #ov-switch {
        width: 1fr;
        height: 1fr;
    }
    HostView #host-msg {
        width: 1fr;
        height: 1fr;
        padding: 2 3;
    }
    HostView TabPane {
        padding: 0 1;
        height: 1fr;
    }
    HostView .section {
        width: 1fr;
        height: 1fr;
    }
    HostView #overview {
        overflow-y: auto;
    }
    HostView #gauges {
        height: auto;
        padding: 0 1;
        border: round $panel;
    }
    HostView #overview-cols {
        height: auto;
    }
    HostView.narrow #overview-cols {
        layout: vertical;
    }
    HostView #hostinfo, HostView #attention {
        width: 1fr;
        height: auto;
        border: round $panel;
        padding: 0 1;
    }
    HostView #svc-panel {
        height: auto;
        border: round $panel;
        padding: 0 1;
    }
    HostView #charts {
        grid-size: 3 2;
        grid-gutter: 0 1;
        height: 1fr;
        min-height: 14;
    }
    HostView.narrow #charts {
        grid-size: 2 3;
        min-height: 21;
    }
    HostView #charts Chart {
        height: 1fr;
    }
    HostView .tab-head {
        height: auto;
        color: $text-muted;
    }
    HostView #procs-table, HostView #disks-table,
    HostView #ports-table, HostView #failed-table {
        width: 1fr;
        height: 1fr;
    }
    HostView #iface-table {
        width: 1fr;
        height: auto;
        max-height: 40%;
    }
    HostView .note {
        height: auto;
    }
    HostView #log-view {
        width: 1fr;
        height: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    """

    class SectionChanged(Message):
        def __init__(self, host: str, section: str) -> None:
            super().__init__()
            self.host = host
            self.section = section

    def __init__(
        self,
        state: HostState,
        section: str | None = None,
        terminal_command: list[str] | None = None,
        check_runner=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.terminal_command = terminal_command or ["ssh", state.spec.target]
        self.check_runner = check_runner
        self.initial_section = section if section in dict(SECTIONS) else "overview"
        self._rendered: tuple | None = None
        # Sections are built the first time they are shown: a host tab costs a few
        # widgets instead of ~120 until the user looks inside. Overview always exists
        # (it holds the connecting / down message).
        self._ready = {"overview", self.initial_section}
        self._building: dict[str, asyncio.Future] = {}
        self._widgets: dict = {}  # see StackSections._q
        self._logs_shown: tuple | None = None

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with TabbedContent(id="sections", initial=f"sec-{self.initial_section}"):
            with TabPane(_("Overview"), id="sec-overview"):
                yield from self._content("overview")
            with TabPane(_("Web"), id="sec-web"):
                yield from self._initial("web")
            with TabPane(_("Database"), id="sec-db"):
                yield from self._initial("db")
            with TabPane("PHP", id="sec-php"):
                yield from self._initial("php")
            with TabPane(_("Attacks"), id="sec-attacks"):
                yield from self._initial("attacks")
            with TabPane(_("Processes"), id="sec-procs"):
                yield from self._initial("procs")
            with TabPane(_("Disks"), id="sec-disks"):
                yield from self._initial("disks")
            with TabPane(_("Network"), id="sec-network"):
                yield from self._initial("network")
            with TabPane(_("Services"), id="sec-services"):
                yield from self._initial("services")
            with TabPane(_("Logs"), id="sec-logs"):
                yield from self._initial("logs")
            with TabPane(_("Terminal"), id="sec-terminal"):
                yield from self._initial("terminal")
            with TabPane(_("Snippets"), id="sec-snippets"):
                yield from self._initial("snippets")

    def _initial(self, section: str) -> ComposeResult:
        return self._content(section) if section == self.initial_section else iter(())

    def _content(self, section: str) -> ComposeResult:
        """The widgets inside one section's tab."""
        if section == "overview":
            with ContentSwitcher(id="ov-switch", initial="host-msg"):
                yield Static(_("connecting…"), id="host-msg")
                with Vertical(id="overview", classes="section"):
                    yield Static(id="gauges")
                    with Horizontal(id="overview-cols"):
                        yield Static(id="hostinfo")
                        yield Static(id="attention")
                    yield Static(id="svc-panel")
                    with Grid(id="charts"):
                        yield Chart(_("CPU"), "cpu", maximum=100, fmt=_pct, alert_at=85, id="chart-cpu")
                        yield Chart(_("Memory"), "mem", maximum=100, fmt=_pct, alert_at=90, id="chart-mem")
                        yield Chart(_("Load average"), "load", fmt=_load, id="chart-load")
                        yield Chart(_("Network ↓ in / ↑ out"), "rx", mirror_palette="tx", fmt=short_rate, id="chart-net")
                        yield Chart(_("Requests per second"), "rps", fmt=short_rate, id="chart-rps")
                        yield Chart(_("Database queries per second"), "qps", fmt=short_rate, id="chart-qps")
        elif section == "web":
            yield from self.compose_web()
        elif section == "db":
            yield from self.compose_db()
        elif section == "php":
            yield from self.compose_php()
        elif section == "attacks":
            yield from self.compose_attacks()
        elif section == "procs":
            yield Static(id="procs-summary", classes="tab-head")
            yield DataTable(id="procs-table", zebra_stripes=True, cursor_type="row")
        elif section == "disks":
            yield Static(id="disks-summary", classes="tab-head")
            yield DataTable(id="disks-table", zebra_stripes=True, cursor_type="row")
        elif section == "network":
            yield Static(id="network-summary", classes="tab-head")
            yield DataTable(id="iface-table", zebra_stripes=True, cursor_type="row")
            yield Static(id="ports-summary", classes="tab-head")
            yield DataTable(id="ports-table", zebra_stripes=True, cursor_type="row")
            yield Static("", id="network-note", classes="note")
        elif section == "services":
            yield Static(id="services-summary", classes="tab-head")
            yield DataTable(id="key-services", zebra_stripes=True, cursor_type="row")
            yield Static(_("Failed units"), classes="tab-head")
            yield DataTable(id="failed-table", zebra_stripes=True, cursor_type="row")
            yield Static("", id="services-note", classes="note")
        elif section == "logs":
            yield Static(id="logs-summary", classes="tab-head")
            yield RichLog(id="log-view", markup=False, wrap=True)
        elif section == "terminal":
            yield TerminalSection(self.terminal_command, id="terminal")
        elif section == "snippets":
            yield SnippetsSection(self.state.spec.name, self.check_runner, id="snippets")

    def _add_columns(self, section: str) -> None:
        for selector, labels in {**TABLE_COLUMNS.get(section, {}), **STACK_COLUMNS.get(section, {})}.items():
            self._q(selector, DataTable).add_columns(*(_(label) for label in labels))

    def on_mount(self) -> None:
        self._add_columns(self.initial_section)

    def ensure_section(self, section: str) -> asyncio.Future | None:
        """Build a section's widgets if it was never shown; await the result to use them."""
        if section in self._ready or section not in dict(SECTIONS):
            return None
        building = self._building.get(section)
        if building is None:
            # mount_compose registers the widgets right away; mounting finishes in the task.
            mounting = self._q(f"#sec-{section}", TabPane).mount_compose(self._content(section))
            building = self._building[section] = asyncio.ensure_future(self._finish_section(section, mounting))
        return building

    async def _finish_section(self, section: str, mounting) -> None:
        await mounting
        self._add_columns(section)
        self._ready.add(section)
        self._building.pop(section, None)
        if section == self.section:
            self.refresh_data(force=True)

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < NARROW, "narrow")
        self.refresh_data(force=True)

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    @property
    def section(self) -> str:
        try:
            active = self._q("#sections", TabbedContent).active
        except NoMatches:  # not composed yet
            return self.initial_section
        return (active or "sec-overview").removeprefix("sec-")

    def show_section(self, section: str) -> None:
        if section in dict(SECTIONS):
            self.ensure_section(section)
            self._q("#sections", TabbedContent).active = f"sec-{section}"

    def cycle_section(self, delta: int) -> None:
        keys = [key for key, _title in SECTIONS]
        index = keys.index(self.section) if self.section in keys else 0
        self.show_section(keys[(index + delta) % len(keys)])

    async def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id != "sections":
            return
        event.stop()  # keep the outer server tabs from seeing section switches
        section = self.section
        self.post_message(self.SectionChanged(self.state.spec.name, section))
        building = self.ensure_section(section)
        if building is not None:
            await building
        if section == "terminal" and self.section == "terminal":
            self.call_later(self._q(TerminalSection).ensure_session)
        self.refresh_data(force=True)  # only the visible section is kept up to date

    async def on_snippets_section_insert(self, event: SnippetsSection.Insert) -> None:
        event.stop()
        self.show_section("terminal")
        building = self.ensure_section("terminal")
        if building is not None:
            await building
        await self._q(TerminalSection).paste(event.command)

    def terminal(self) -> TerminalSection | None:
        """The Terminal section, None until it was first shown."""
        if "terminal" not in self._ready:
            return None
        return self._q(TerminalSection)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def refresh_data(self, force: bool = False) -> None:
        """Re-render if the host has new data (or the size changed)."""
        if not self.is_mounted:
            return
        key = (self.state.version, self.state.paused, self.state.disabled, self.size.width, self.section)
        if key == self._rendered and not force:
            return
        try:
            switcher = self._q("#ov-switch", ContentSwitcher)
            message = self._q("#host-msg", Static)
        except NoMatches:  # children not mounted yet; the next frame renders
            return
        self._rendered = key

        reading = self.state.reading
        name = self.state.spec.name

        if self.state.disabled:
            message.update(Text.assemble(
                ("⊘ " + name, "bold grey70"), "\n\n",
                _("This host is disabled: Zir SSH does not connect to it at all."), "\n",
                (_("Press d to enable it again."), "bold cyan"),
            ))
            switcher.current = "host-msg"
            return

        if reading is None:
            message.update(Text.assemble((name, "bold"), "\n\n", _("connecting…")))
            switcher.current = "host-msg"
            return

        if reading.snapshot is None:
            text = Text()
            text.append(f"✖ {name}\n\n", style="bold red")
            text.append(reading.error or _("no data"), style="red")
            if reading.hint:
                text.append("\n\n" + reading.hint, style="grey70")
            port = f" -p {reading.spec.port}" if reading.spec.port else ""
            text.append(
                "\n\n" + _("Check:") + f" ssh -o BatchMode=yes{port} {reading.spec.target} true",
                style="grey50",
            )
            if "permission denied" in (reading.error or "").lower():
                text.append(
                    "\n\n" + _("Press a to enter the server password once: Zir SSH adds your SSH key "
                                 "and logs in without a password from then on."),
                    style="bold cyan",
                )
            if self.state.failures:
                text.append(
                    "\n\n" + _n("{n} failed attempt, retrying with backoff",
                                 "{n} failed attempts, retrying with backoff", self.state.failures),
                    style="grey50",
                )
            message.update(text)
            switcher.current = "host-msg"
            return

        switcher.current = "overview"
        if self.section not in self._ready:
            self._rendered = None  # still being built: render when it is ready
            return
        # Rendering hidden sections would only cost time: each switch renders the new one.
        renderer = {
            "overview": self._render_overview,
            "web": self.render_web,
            "db": self.render_db,
            "php": self.render_php,
            "attacks": self.render_attacks,
            "procs": self._render_processes,
            "disks": self._render_disks,
            "network": self._render_network,
            "services": self._render_services,
            "logs": self._render_logs,
        }.get(self.section)
        if renderer is not None:
            renderer(reading)

    @property
    def _bar_width(self) -> int:
        return max(10, min(50, self.size.width // 4))

    def _render_overview(self, reading: HostReading) -> None:
        snapshot = reading.snapshot
        assert snapshot is not None
        metrics = reading.metrics

        cpu_pct = metrics.cpu_pct if metrics.cpu_pct is not None else 0.0
        cpu_detail = _("warming up…")
        if metrics.cpu_pct is not None:
            cpu_detail = (
                _("{cpus} vCPU · load {l1} {l5} {l15}", cpus=snapshot.cpus,
                  l1=f"{snapshot.load[0]:.2f}", l5=f"{snapshot.load[1]:.2f}", l15=f"{snapshot.load[2]:.2f}")
            )

        mem_detail = (
            _("{used} / {total} · cache {cache}", used=human_bytes(snapshot.mem_used),
              total=human_bytes(snapshot.mem_total), cache=human_bytes(snapshot.cached))
            if snapshot.mem_total
            else _("not available")
        )

        if snapshot.swap_total:
            swap_pct = snapshot.swap_percent
            swap_detail = f"{human_bytes(snapshot.swap_used)} / {human_bytes(snapshot.swap_total)}"
        else:
            swap_pct = 0.0
            swap_detail = _("not configured")

        disk = snapshot.worst_disk
        disk_pct = disk.percent if disk else 0.0
        disk_detail = (
            _n("{mount} · {used} / {size} · {n} mount", "{mount} · {used} / {size} · {n} mounts",
               len(snapshot.disks), mount=disk.mount, used=human_bytes(disk.used), size=human_bytes(disk.size))
            if disk
            else _("no data")
        )

        width = self._bar_width
        gauges = Text()
        gauges.append_text(_gauge(_("CPU"), cpu_pct, cpu_detail, width))
        gauges.append("\n")
        gauges.append_text(_gauge(_("MEM"), snapshot.mem_percent, mem_detail, width))
        gauges.append("\n")
        gauges.append_text(_gauge(_("SWAP"), swap_pct, swap_detail, width))
        gauges.append("\n")
        gauges.append_text(_gauge(_("DISK"), disk_pct, disk_detail, width))
        self._q("#gauges", Static).update(gauges)

        info = Text()
        info.append(f"{_('host'):<12}", style="grey50")
        info.append(snapshot.hostname, style="bold")
        if snapshot.ip:
            info.append(f"  {snapshot.ip}", style="grey70")
        info.append("\n")
        info.append(f"{_('os'):<12}", style="grey50")
        info.append(snapshot.os)
        info.append("\n")
        info.append(f"{_('kernel'):<12}", style="grey50")
        info.append(snapshot.kernel)
        info.append("\n")
        info.append(f"{_('uptime'):<12}", style="grey50")
        info.append(human_uptime(snapshot.uptime))
        info.append(
            "  " + _("(boot {at})", at=time.strftime('%Y-%m-%d %H:%M', time.localtime(snapshot.boot_time))),
            style="grey70",
        )
        info.append("\n")
        info.append(f"{_('activity'):<12}", style="grey50")
        info.append(
            _n("{n} process", "{n} processes", snapshot.procs_total) + " · "
            + _n("{n} service", "{n} services", snapshot.running_services) + " · "
            + _n("{n} logged in", "{n} logged in", len(snapshot.who))
        )
        info.append("\n")
        info.append(f"{_('docker'):<12}", style="grey50")
        info.append(
            _n("{n} running", "{n} running", len(snapshot.docker)) if snapshot.docker
            else _("not installed / not running")
        )
        info.append("\n")
        info.append(f"{_('sampled'):<12}", style="grey50")
        info.append(f"{time.strftime('%H:%M:%S', time.localtime(reading.at))}")
        info.append(f" · {human_ms(reading.latency)}", style="grey70")
        if self.state.paused:
            info.append("  " + _("PAUSED"), style="bold yellow")
        self._q("#hostinfo", Static).update(info)

        self._q("#attention", Static).update(self._attention_text(reading))
        panel = self._q("#svc-panel", Static)
        panel.border_title = _("Services")
        panel.update(services_panel(snapshot.stack.services, max(self.size.width - 6, 40)))

        history = self.state.history
        waiting = _("collecting data…")
        self._q("#chart-cpu", Chart).set_data(list(history["cpu"]), note=waiting)
        self._q("#chart-mem", Chart).set_data(list(history["mem"]), note=waiting)
        load = self._q("#chart-load", Chart)
        load.alert_at = float(snapshot.cpus * 2) if snapshot.cpus else None
        load.set_data(list(history["load"]), title=_("Load average") + f" · {snapshot.cpus} vCPU", note=waiting)
        self._q("#chart-net", Chart).set_data(list(history["rx"]), list(history["tx"]), note=waiting)
        stack = snapshot.stack
        self._q("#chart-rps", Chart).set_data(
            list(history["rps"]),
            note=_("no web server") if not stack.web_server else
            (waiting if stack.web.files else
             _("no permission to read access logs") if stack.web.unreadable else _("no access logs found")),
        )
        self._q("#chart-qps", Chart).set_data(
            list(history["qps"]),
            note=_("no MySQL / MariaDB") if stack.mysql.state == "absent" else
            (_("no access to the database") if stack.mysql.state != "ok" else waiting),
        )

    def _attention_text(self, reading: HostReading) -> Text:
        snapshot = reading.snapshot
        assert snapshot is not None
        findings: list[Finding] = self.findings_of(reading)
        if reading.error:
            findings.insert(0, Finding("crit", _("stale data — {error}", error=reading.error)))

        text = Text()
        if not findings:
            text.append("✓ " + _("all good") + "\n", style="bold green")
            text.append(_("No failed units, disks and memory below thresholds."), style="grey70")
            return text

        for finding in findings[:6]:
            color = "red" if finding.level == "crit" else "yellow"
            mark = "✖" if finding.level == "crit" else "▲"
            text.append(f"{mark} ", style=color)
            text.append(finding.message, style=f"bold {color}")
            text.append("\n")

        if len(findings) > 6:
            text.append(_("+{n} more", n=len(findings) - 6) + "\n", style="grey50")

        if reading.hint:
            text.append("\n" + reading.hint, style="grey70")
        return text

    def _render_processes(self, reading: HostReading) -> None:
        snapshot = reading.snapshot
        assert snapshot is not None
        command_width = max(20, self.size.width - 70)
        rows = []
        for process in snapshot.procs[:30]:
            command = process.args
            if command == process.comm:
                command = f"[{process.comm}]"
            rows.append((
                str(process.pid),
                process.user,
                Text(f"{process.cpu:.1f}", style=_pct_style(process.cpu)),
                f"{process.mem:.1f}",
                human_bytes(process.rss),
                process.etime,
                process.state,
                _truncate(command, command_width),
            ))
        fill(self._q("#procs-table", DataTable), rows)
        self._q("#procs-summary", Static).update(
            Text(
                _("Top {top} of {total} processes · sorted by CPU", top=len(snapshot.procs), total=snapshot.procs_total),
                style="grey70",
            )
        )

    def _render_disks(self, reading: HostReading) -> None:
        snapshot = reading.snapshot
        assert snapshot is not None
        disks = sorted(snapshot.disks, key=lambda disk: disk.percent, reverse=True)
        fs_width = max(12, self.size.width // 4)
        fill(self._q("#disks-table", DataTable), [
            (
                _truncate(disk.filesystem, fs_width),
                disk.mount,
                human_bytes(disk.size),
                human_bytes(disk.used),
                human_bytes(disk.available),
                Text(f"{disk.percent:.0f}%", style=_pct_style(disk.percent)),
                Text(f"{disk.inode_percent:.0f}%", style=_pct_style(disk.inode_percent)),
            )
            for disk in disks
        ])
        self._q("#disks-summary", Static).update(
            Text(
                _n("{n} mount", "{n} mounts", len(disks)) + " · " + _("sorted by usage · inode usage in the last column"),
                style="grey70",
            )
        )

    def _render_network(self, reading: HostReading) -> None:
        snapshot = reading.snapshot
        assert snapshot is not None
        rates = reading.metrics.net_rates

        ordered = sorted(
            snapshot.net.items(),
            key=lambda item: sum(rates.get(item[0], (0.0, 0.0))),
            reverse=True,
        )
        rows = []
        for name, counters in ordered:
            rx_rate, tx_rate = rates.get(name, (0.0, 0.0))
            rows.append((
                name,
                human_rate(rx_rate) if name in rates else "—",
                human_rate(tx_rate) if name in rates else "—",
                human_bytes(counters.rx),
                human_bytes(counters.tx),
            ))
        fill(self._q("#iface-table", DataTable), rows)

        total_rx = sum(rate[0] for rate in rates.values())
        total_tx = sum(rate[1] for rate in rates.values())
        self._q("#network-summary", Static).update(
            Text(
                _n("{n} interface", "{n} interfaces", len(snapshot.net))
                + f" · rx {human_rate(total_rx)} · tx {human_rate(total_tx)}"
                + (" · " + _("warming up…") if not rates else ""),
                style="grey70",
            )
        )

        fill(self._q("#ports-table", DataTable),
             [(socket.proto, socket.local, socket.process or "—") for socket in snapshot.listening[:30]])
        self._q("#ports-summary", Static).update(
            Text(_("Listening sockets · {n}", n=len(snapshot.listening)), style="grey70")
        )
        note = ""
        if not snapshot.listening:
            note = _("ss is not available (or returned nothing). Process names are hidden for non-root users.")
        self._q("#network-note", Static).update(Text(note, style="grey50"))

    def _render_services(self, reading: HostReading) -> None:
        snapshot = reading.snapshot
        assert snapshot is not None
        rows = []
        for service in snapshot.stack.services:
            mark, style = {"ok": ("●", "green"), "warn": ("▲", "yellow"), "crit": ("■", "bold red"),
                           "off": ("○", "grey42")}[service.state]
            rows.append((
                Text(f"{mark} {service.name}", style=style if service.state != "ok" else ""),
                service.unit,
                Text(service_state_text(service), style=style),
                human_uptime(service.since) if service.since is not None else "—",
                human_bytes(service.memory) if service.memory else "—",
                Text(str(service.restarts), style="yellow" if service.restarts else ""),
                {"enabled": _("yes"), "disabled": _("no")}.get(service.enabled, service.enabled or "—"),
            ))
        fill(self._q("#key-services", DataTable), rows)

        failed = snapshot.failed_services
        fill(self._q("#failed-table", DataTable),
             [(Text(service.unit, style="red"), service.state, service.description) for service in failed])

        if not snapshot.init:
            summary = _("systemd not detected — service checks are unavailable")
        else:
            summary = _("{running} running · {failed} failed", running=_n("{n} service", "{n} services", snapshot.running_services), failed=len(failed))
        self._q("#services-summary", Static).update(Text(summary, style="grey70"))
        self._q("#services-note", Static).update(
            Text(_("Everything is running.") if snapshot.init and not failed else "", style="green")
        )

    def _render_logs(self, reading: HostReading) -> None:
        snapshot = reading.snapshot
        assert snapshot is not None
        log = self._q("#log-view", RichLog)
        # The journal is read on full polls only: most polls bring the same lines.
        shown = (tuple(snapshot.logs[-60:]), self.size.width)
        if shown != self._logs_shown:
            self._logs_shown = shown
            self._write_logs(log, snapshot.logs)

        self._q("#logs-summary", Static).update(
            Text(
                "journalctl -p warning -b · "
                + _n("last {n} line", "last {n} lines", len(snapshot.logs)) + " · "
                + _n("{n} user logged in", "{n} users logged in", len(snapshot.who)),
                style="grey70",
            )
        )

    @staticmethod
    def _write_logs(log: RichLog, logs: list[str]) -> None:
        log.clear()
        if not logs:
            log.write(
                Text(_("No warnings in this boot — or journalctl is not accessible.") + "\n", style="grey50")
            )
        for line in logs[-60:]:
            lowered = line.lower()
            if any(word in lowered for word in ("error", "crit", "fatal", "failure")):
                style = "red"
            elif "warn" in lowered:
                style = "yellow"
            else:
                style = "grey70"
            log.write(Text(line, style=style))
