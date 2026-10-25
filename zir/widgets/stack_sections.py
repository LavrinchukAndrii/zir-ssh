"""Host sections for the software stack: Web, Database, PHP, Attacks.

A mixin for HostView: compose_* yield the widgets of each section, render_*
fill them from the latest reading. Only the visible section is rendered.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, DataTable, Static

from ..collector import HostReading
from ..formatting import human_bytes
from ..i18n import _, _n
from ..probes import Finding, evaluate
from ..services import Stack, StackMetrics
from .chart import Chart
from .tables import fill

GROUP_LABELS = {
    "web": "Web", "php": "PHP", "db": "Databases", "ftp": "FTP", "mail": "Mail",
    "dns": "DNS", "panel": "Panels", "system": "System",
}
SERVICE_STATE = {  # state -> (mark, style)
    "ok": ("●", "green"), "warn": ("▲", "yellow"), "crit": ("■", "bold red"), "off": ("○", "grey42"),
}


def service_state_text(service) -> str:
    if service.state == "crit":
        return _("failed") if service.active == "failed" else _("stopped")
    if service.state == "warn":
        return _("starting") if service.active == "activating" else service.active
    if service.state == "off":
        return _("disabled")
    return _("running")


def services_panel(services, width: int) -> Text:
    """One line per group: ● nginx  ● PHP-FPM 8.2  ■ Pure-FTPd stopped …"""
    from ..formatting import human_uptime

    text = Text()
    if not services:
        return Text(_("No known services found (systemd units or processes)."), style="grey58")
    by_group: dict[str, list] = {}
    for service in services:
        by_group.setdefault(service.group, []).append(service)
    label_w = max(len(_(GROUP_LABELS[g])) for g in by_group) + 2
    for group in GROUP_LABELS:
        items = by_group.get(group)
        if not items:
            continue
        if text:
            text.append("\n")
        text.append(f"{_(GROUP_LABELS[group]):<{label_w}}", style="grey58")
        line_len = label_w
        for service in items:
            mark, style = SERVICE_STATE[service.state]
            # PHP-FPM / PM2 can run several units: show which one (php8.2-fpm).
            name = service.unit if service.name in ("PHP-FPM", "PM2") else service.name
            chip = Text.assemble((mark + " ", style), (name, "bold" if service.state != "off" else "grey50"))
            if service.state != "ok":
                chip.append(" " + service_state_text(service), style=style)
            elif service.since is not None:
                chip.append(" " + human_uptime(service.since), style="grey42")
            if line_len + chip.cell_len + 3 > width and line_len > label_w:
                text.append("\n" + " " * label_w)
                line_len = label_w
            text.append_text(chip)
            text.append("   ")
            line_len += chip.cell_len + 3
    return text


KIND_NAMES = {"nginx": "nginx", "apache": "Apache", "litespeed": "LiteSpeed", "caddy": "Caddy"}


def short_rate(value: float) -> str:
    """Compact numbers for chart axes: 950, 1.2k, 3.4M."""
    for limit, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return f"{value:.0f}" if value >= 10 else f"{value:.1f}".rstrip("0").rstrip(".") or "0"


def _bar(fraction: float, width: int = 12) -> Text:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    color = "red" if fraction >= 0.9 else "yellow" if fraction >= 0.7 else "green"
    return Text.assemble(("█" * filled, color), ("░" * (width - filled), "grey23"))


def _kv(label: str, value: str, style: str = "") -> Text:
    return Text.assemble((f"{label}: ", "grey58"), (value, style or "bold"))


def _join(parts: list[Text], sep: str = "   ") -> Text:
    text = Text()
    for index, part in enumerate(parts):
        if index:
            text.append(sep)
        text.append_text(part)
    return text


STACK_COLUMNS = {  # section -> table -> column labels
    "web": {"#web-ips": ("IP", "REQ/S", "SHARE"), "#web-paths": ("URL", "REQ/S"), "#web-sites": ("SITE", "REQ/S")},
    "db": {"#db-queries": ("SEC", "USER", "DB", "STATE", "QUERY")},
    "php": {"#php-pools": ("POOL", "PM", "WORKERS", "BUSY", "MAX", "LOAD", "CPU", "RAM")},
    "attacks": {"#atk-peers": ("IP", "CONNECTIONS"), "#atk-ips": ("IP", "REQ/S", "SHARE")},
}

STACK_CSS = """
HostView .stack { width: 1fr; height: 1fr; }
HostView .stack-head { height: auto; padding: 0 1; }
HostView .stack-note { height: auto; padding: 0 1; color: $text-muted; }
HostView .stack-row { height: auto; }
HostView .stack-tables { height: 1fr; min-height: 8; }
HostView .stack-tables DataTable { width: 1fr; height: 1fr; margin-right: 1; }
HostView .stack-row Chart { margin-right: 1; }
HostView #atk-verdict { height: auto; border: round $panel; padding: 0 1; }
HostView #db-setup { margin: 0 1 1 1; display: none; }
HostView.narrow .stack-tables { layout: vertical; }
"""


class DbAccessRequested(Message):
    """The user wants to enter database credentials for this host."""


class StackSections:
    # -- composition -----------------------------------------------------

    def compose_web(self) -> ComposeResult:
        with VerticalScroll(classes="stack"):
            yield Static(id="web-head", classes="stack-head")
            yield Chart(_("Requests per second"), "rps", fmt=short_rate, id="chart-web-rps")
            yield Static(id="web-status", classes="stack-head")
            with Horizontal(classes="stack-tables"):
                yield DataTable(id="web-ips", zebra_stripes=True, cursor_type="row")
                yield DataTable(id="web-paths", zebra_stripes=True, cursor_type="row")
                yield DataTable(id="web-sites", zebra_stripes=True, cursor_type="row")
            yield Static(id="web-note", classes="stack-note")

    def compose_db(self) -> ComposeResult:
        with VerticalScroll(classes="stack"):
            yield Static(id="db-head", classes="stack-head")
            yield Button(_("Set up database access"), id="db-setup", variant="warning")
            with Horizontal(classes="stack-row"):
                yield Chart(_("Queries per second"), "qps", fmt=short_rate, id="chart-db-qps")
                yield Chart(_("Connections"), "conn", fmt=short_rate, id="chart-db-conn")
            yield Static(id="db-stats", classes="stack-head")
            with Horizontal(classes="stack-tables"):
                yield DataTable(id="db-queries", zebra_stripes=True, cursor_type="row")
            yield Static(id="db-note", classes="stack-note")

    def compose_php(self) -> ComposeResult:
        with VerticalScroll(classes="stack"):
            yield Static(id="php-head", classes="stack-head")
            yield Chart(_("Busy PHP workers"), "php", fmt=short_rate, id="chart-php")
            with Horizontal(classes="stack-tables"):
                yield DataTable(id="php-pools", zebra_stripes=True, cursor_type="row")
            yield Static(id="php-note", classes="stack-note")

    def compose_attacks(self) -> ComposeResult:
        with VerticalScroll(classes="stack"):
            yield Static(id="atk-verdict")
            yield Static(id="atk-stats", classes="stack-head")
            with Horizontal(classes="stack-tables"):
                yield DataTable(id="atk-peers", zebra_stripes=True, cursor_type="row")
                yield DataTable(id="atk-ips", zebra_stripes=True, cursor_type="row")
            yield Static(id="atk-note", classes="stack-note")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "db-setup":
            event.stop()
            self.post_message(DbAccessRequested())

    # -- helpers -------------------------------------------------------------

    def _q(self, selector, expect_type=None):
        """query_one, remembered: render code looks up the same widgets on every poll."""
        widget = self._widgets.get(selector)
        if widget is None:
            widget = self.query_one(selector, expect_type) if expect_type else self.query_one(selector)
            self._widgets[selector] = widget
        return widget

    def _fill(self, selector: str, rows: list[tuple]) -> None:
        fill(self._q(selector, DataTable), rows)

    def findings_of(self, reading: HostReading) -> list[Finding]:
        """Findings of `reading`: the store evaluated the current one already."""
        if reading is self.state.reading and not reading.error:
            return list(self.state.findings)
        return evaluate(reading.snapshot, reading.metrics)

    def _chart(self, selector: str, series: str, **kwargs) -> None:
        self._q(selector, Chart).set_data(list(self.state.history[series]), **kwargs)

    # -- Web -----------------------------------------------------------------

    def render_web(self, reading: HostReading) -> None:
        stack: Stack = reading.snapshot.stack
        rates: StackMetrics = reading.metrics.stack
        servers = stack.web_kinds()
        head = Text()
        if not servers:
            head.append(_("No web server found (nginx, Apache, LiteSpeed, Caddy)."), style="grey58")
        for kind in servers:
            groups = stack.of(kind)
            if not groups:
                # Known only from systemd: its processes are hidden from this SSH user.
                service = stack.web_service(kind)
                memory = service.memory if service else None
                head.append_text(_join([
                    Text(KIND_NAMES[kind], style="bold cyan"),
                    _kv(_("RAM"), human_bytes(memory) if memory else "—"),
                    Text(_("processes are not visible to this user"), style="grey58"),
                ]))
                head.append("\n")
                continue
            cpu = sum(rates.group_cpu.get(f"{g.kind}|{g.group}", 0.0) for g in groups)
            head.append_text(_join([
                Text(KIND_NAMES[kind], style="bold cyan"),
                _kv(_("processes"), str(sum(g.count for g in groups))),
                _kv(_("CPU"), f"{cpu:.0f}%"),
                _kv(_("RAM"), human_bytes(sum(g.rss for g in groups))),
            ]))
            head.append("\n")
        if servers:
            head.append_text(_join([
                _kv(_("connections to :80/:443"), str(stack.web_connections)),
                _kv(_("all TCP connections"), str(stack.established)),
            ]))
        self._q("#web-head", Static).update(head)

        rps = rates.rps
        baseline = rates.rps_baseline
        title = _("Requests per second")
        if baseline:
            title += " · " + _("normal ≈ {n}", n=short_rate(baseline))
        note = (_("Waiting for the second poll…") if stack.web.files else
                _("Access logs cannot be read.") if stack.web.unreadable else _("No access logs found."))
        self._chart("#chart-web-rps", "rps", title=title, note=note)

        status = Text()
        if rps:
            labels = (("2", "2xx", "green"), ("3", "3xx", "cyan"), ("4", "4xx", "yellow"), ("5", "5xx", "red"))
            parts = []
            for key, label, color in labels:
                value = rates.status_rps.get(key, 0.0)
                share = value / rps * 100 if rps else 0
                parts.append(Text.assemble((f"{label} ", color), (f"{share:.0f}%", "bold"), (f" ({short_rate(value)}/s)", "grey58")))
            status.append_text(_join(parts))
            if rates.bruteforce_rps:
                status.append("   ")
                status.append(_("wp-login/xmlrpc: {n}/s", n=f"{rates.bruteforce_rps:.1f}"), style="bold red")
        self._q("#web-status", Static).update(status)

        total = rps or 0.0
        self._fill("#web-ips", [
            (ip, f"{rate:.1f}", f"{rate / total * 100:.0f}%" if total else "—") for rate, ip in rates.ip_rps
        ])
        self._fill("#web-paths", [(path[:60], f"{rate:.1f}") for rate, path in rates.path_rps])
        sites = sorted(rates.file_rps.items(), key=lambda item: -item[1])[:10]
        self._fill("#web-sites", [(_site(path), f"{rate:.1f}") for path, rate in sites])

        files = len(stack.web.files)
        if files:
            note = _n("Reading {n} access log", "Reading {n} access logs", files)
            if stack.web.unreadable:
                note += " · " + _("no permission to read {n} more", n=len(stack.web.unreadable))
        elif stack.web.unreadable:
            folder = stack.web.unreadable[0].rsplit("/", 1)[0] or "/"
            note = _(
                "Access logs found: {n}, but this SSH user may not read them ({path}). "
                "Debian/Ubuntu: sudo usermod -aG adm $USER, then reconnect. "
                "Other systems: sudo setfacl -m u:$USER:rx,d:u:$USER:r {dir} && sudo setfacl -m u:$USER:r {dir}/*.log",
                n=len(stack.web.unreadable), path=stack.web.unreadable[0], dir=folder,
            )
        else:
            note = _(
                "Access logs were not found in the usual places. Add their paths as web_logs = [...] in hosts.toml."
            )
        if stack.web.scale > 1:
            note += " · " + _("heavy traffic: counted from a sample of the newest lines")
        self._q("#web-note", Static).update(note)

    # -- Database ------------------------------------------------------------

    def render_db(self, reading: HostReading) -> None:
        mysql = reading.snapshot.stack.mysql
        rates = reading.metrics.stack
        head = self._q("#db-head", Static)
        notes = {
            "absent": _("MySQL / MariaDB is not running on this server."),
            "no_client": _("The database runs, but the mysql client is not installed."),
            "no_access": _("No access to the database for this SSH user. As root it usually works through the socket; "
                           "otherwise create ~/.my.cnf with a read-only user."),
            "error": _("Database error: {error}", error=mysql.message[:200]),
        }
        setup = self._q("#db-setup", Button)
        setup.display = mysql.state == "no_access"
        if mysql.state == "no_access" and reading.spec.target.split("@")[0] == "root":
            notes["no_access"] = _("MySQL / MariaDB asks for a password even for root here. Press the button: "
                                   "enter it once and Zir SSH saves it to ~/.my.cnf on the server.")
        if mysql.state != "ok":
            head.update(Text(notes.get(mysql.state, mysql.state), style="grey58"))
            self._q("#db-stats", Static).update("")
            self._fill("#db-queries", [])
            self._chart("#chart-db-qps", "qps", note="—")
            self._chart("#chart-db-conn", "dbconn", note="—")
            return

        status = mysql.status
        connected = status.get("Threads_connected", 0)
        limit = mysql.max_connections
        auth = {"default": "", "": ""}.get(mysql.auth, mysql.auth)
        head.update(_join([
            Text(mysql.product + (f"  ({_('access')}: {auth})" if auth else ""), style="bold cyan"),
            _kv(_("connections"), f"{connected} / {limit}" if limit else str(connected)),
            _kv(_("running now"), str(status.get("Threads_running", 0))),
            _kv(_("peak connections"), str(status.get("Max_used_connections", 0))),
        ]))
        self._chart("#chart-db-qps", "qps", note=_("Waiting for the second poll…"))
        self._q("#chart-db-conn", Chart).maximum = float(limit) if limit else None
        self._chart("#chart-db-conn", "dbconn")

        reads = status.get("Innodb_buffer_pool_read_requests", 0)
        misses = status.get("Innodb_buffer_pool_reads", 0)
        hit = (1 - misses / reads) * 100 if reads else None
        db_rates = rates.db_rates
        parts = [
            _kv("SELECT", f"{db_rates.get('Com_select', 0):.0f}/s"),
            _kv("INSERT", f"{db_rates.get('Com_insert', 0):.0f}/s"),
            _kv("UPDATE", f"{db_rates.get('Com_update', 0):.0f}/s"),
            _kv("DELETE", f"{db_rates.get('Com_delete', 0):.0f}/s"),
            _kv(_("slow / min"), f"{rates.slow_per_min:.0f}", "bold yellow" if rates.slow_per_min else ""),
            _kv(_("failed logins / min"), f"{rates.aborted_per_min:.0f}", "bold red" if rates.aborted_per_min >= 10 else ""),
        ]
        if hit is not None:
            parts.append(_kv(_("buffer pool hits"), f"{hit:.1f}%", "bold green" if hit >= 99 else "bold yellow"))
        self._q("#db-stats", Static).update(_join(parts))

        width = max(30, self.size.width - 60)
        self._fill("#db-queries", [
            (Text(str(q.seconds), style="red" if q.seconds >= 10 else "yellow" if q.seconds >= 2 else ""),
             q.user, q.db, q.state[:24], q.sql[:width])
            for q in mysql.queries
        ])
        self._q("#db-note", Static).update(
            "" if mysql.queries else _("No running queries right now.")
        )

    # -- PHP -----------------------------------------------------------------

    def render_php(self, reading: HostReading) -> None:
        stack = reading.snapshot.stack
        rates = reading.metrics.stack
        groups = stack.of("php-fpm")
        workers = {g.group: g for g in groups if g.group != "master"}
        pools = {pool.name: pool for pool in stack.php_pools}
        head = self._q("#php-head", Static)
        if not groups:
            head.update(Text(_("PHP-FPM is not running (PHP may run inside Apache as mod_php)."), style="grey58"))
        else:
            names = set(workers) | set(pools)
            limit = sum(pools[n].max_children for n in names if n in pools)
            version = stack.php_version_label()
            masters = len(stack.php_masters) or sum(1 for g in groups if g.group == "master")
            head.update(_join([
                Text("PHP-FPM" + (f" {version}" if version else ""), style="bold cyan"),
                _kv(_("masters"), str(masters)),
                _kv(_("pools"), str(len(names))),
                _kv(_("workers"), f"{sum(g.count for g in workers.values())}" + (f" / {limit}" if limit else "")),
                _kv(_("busy now"), str(sum(g.running for g in workers.values()))),
            ]))
            self._q("#chart-php", Chart).maximum = float(limit) if limit else None
        self._chart("#chart-php", "php", note="—")

        rows = []
        for name in sorted(set(workers) | set(pools)):
            group, pool = workers.get(name), pools.get(name)
            limit = pool.max_children if pool else 0
            count = group.count if group else 0
            rows.append((
                name,
                pool.pm if pool and pool.pm else "—",
                str(count),
                str(group.running if group else 0),
                str(limit) if limit else "—",
                _bar(count / limit) if limit else Text("—"),
                f"{rates.group_cpu.get(f'php-fpm|{name}', 0.0):.0f}%",
                human_bytes(group.rss) if group else "—",
            ))
        self._fill("#php-pools", rows)
        note = Text()
        if any(pool.pm == "ondemand" and pool.name not in workers for pool in pools.values()):
            note.append(_("pm = ondemand: workers start on the first request and stop when idle, "
                          "so 0 workers is normal without traffic.") + "\n", style="grey58")
        for master in stack.php_masters:
            note.append(f"{master.exe or '?'}  ·  {master.config}\n", style="grey42")
        for line in stack.php_warnings[-3:]:
            note.append(line[:160] + "\n", style="yellow")
        self._q("#php-note", Static).update(note)

    # -- Attacks -------------------------------------------------------------

    def render_attacks(self, reading: HostReading) -> None:
        stack = reading.snapshot.stack
        rates = reading.metrics.stack
        findings = [f for f in self.findings_of(reading) if f.topic == "ddos"]
        verdict = Text()
        if findings:
            verdict.append(_("Signs of an attack or abuse:") + "\n", style="bold red")
            for finding in findings:
                color = "red" if finding.level == "crit" else "yellow"
                verdict.append(("✖ " if finding.level == "crit" else "▲ ") + finding.message + "\n", style=color)
        else:
            verdict.append("✓ " + _("No signs of DDoS or brute force right now."), style="bold green")
        self._q("#atk-verdict", Static).update(verdict)

        used, limit = stack.conntrack
        parts = [
            _kv(_("requests/s"), short_rate(rates.rps) if rates.rps is not None else "—"),
            _kv(_("normal"), short_rate(rates.rps_baseline) if rates.rps_baseline else "—"),
            _kv(_("established"), str(stack.established)),
            _kv("SYN-RECV", str(stack.syn_recv), "bold red" if stack.syn_recv >= 100 else ""),
            _kv(_("TIME-WAIT"), str(stack.tcp_states.get("TIME-WAIT", 0))),
        ]
        if limit:
            parts.append(_kv("conntrack", f"{used}/{limit} ({used / limit * 100:.0f}%)"))
        self._q("#atk-stats", Static).update(_join(parts))

        self._fill("#atk-peers", [(ip, str(count)) for count, ip in stack.peers])
        total = rates.rps or 0.0
        self._fill("#atk-ips", [
            (ip, f"{rate:.1f}", f"{rate / total * 100:.0f}%" if total else "—") for rate, ip in rates.ip_rps
        ])
        self._q("#atk-note", Static).update(
            _("Zir SSH only watches. To block an IP, use the Snippets tab (firewall commands) or fail2ban.")
        )


def _site(path: str) -> str:
    """/var/log/nginx/example.com.access.log -> example.com, access-wms01.log -> wms01,
    /home/domains/example.com/logs/access.log -> example.com"""
    parts = path.split("/")
    name = parts[-1]
    for suffix in (".access.log", "-access.log", "_access.log", ".access_log", "access.log", ".log"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)].rstrip("._-") or name
            break
    for prefix in ("access-", "access_", "access."):
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    if name == "access" and len(parts) >= 3 and parts[-2] == "logs" and parts[-3]:
        return parts[-3]
    return name
