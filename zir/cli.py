"""Command line entry point: TUI by default, report mode for cron/scripts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .collector import HostReading, build_collectors, demo_specs
from .config import (
    DEFAULT_CONFIG,
    ConfigError,
    HostSpec,
    Settings,
    find_config,
    hosts_from_args,
    load_config,
)
from .formatting import (
    human_ms,
    human_rate,
    human_uptime,
    SEVERITY_COLORS,
    SEVERITY_MARKS,
)
from .i18n import DEFAULT_LANGUAGE, LANGUAGES, _, _n, env_language, normalize, set_language
from .paths import migrate_legacy
from .prefs import load_prefs
from .probes import evaluate, severity


def pick_language(cli: str | None = None, config: str | None = None, ui_first: bool = False) -> str:
    """--lang > ZIR_LANG > prefs.json (F2 in the UI) > hosts.toml > Ukrainian.

    After a switch in the UI (`ui_first`) the saved choice wins over flags/env.
    """
    saved = normalize(load_prefs().get("language"))
    if ui_first and saved:
        return saved
    return normalize(cli) or env_language() or saved or normalize(config) or DEFAULT_LANGUAGE


def _early_lang(argv: list[str]) -> str | None:
    """--lang before argparse runs, so --help is already translated."""
    for index, arg in enumerate(argv):
        if arg == "--lang" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
    return None


def _argparse_text(text: str) -> str:
    from .i18n import get_language
    from .locale_uk import ARGPARSE_UK

    return ARGPARSE_UK.get(text, text) if get_language() == "uk" else text


def build_parser() -> argparse.ArgumentParser:
    # argparse looks up its headings and errors through its module-level `_`.
    argparse._ = _argparse_text
    parser = argparse.ArgumentParser(
        prog="zir",
        description=_(
            "Read-only overview of one or many servers over SSH. "
            "Run without arguments to use ~/.config/zir/hosts.toml."
        ),
        epilog=_(
            "Examples:\n"
            "  zir                 # all hosts from the config\n"
            "  zir web-1 db-1      # ad-hoc hosts (SSH aliases)\n"
            "  zir --demo          # fake fleet, no SSH needed\n"
            "  zir --report web-1  # one-shot health report\n"
            "  zir --report --json web-1\n"
            "  zir --local         # probe this machine (Linux)\n"
            "  zir --lang en       # English interface\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "hosts",
        nargs="*",
        metavar="HOST",
        help=_("SSH destination (alias from ~/.ssh/config, or user@ip[:port])"),
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        metavar="PATH",
        help=_("hosts.toml to use (default: ~/.config/zir/hosts.toml)"),
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        metavar="SEC",
        help=_("refresh interval in seconds (default: 2)"),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="SEC",
        help=_("per-host SSH timeout in seconds (default: 8)"),
    )
    parser.add_argument(
        "--lang",
        choices=sorted(LANGUAGES),
        help=_("interface language: uk (default) or en; also ZIR_LANG or F2 in the app"),
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help=_("also probe the local machine (Linux, no SSH)"),
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=_("run with generated demo hosts (no network access)"),
    )
    parser.add_argument(
        "--demo-hosts",
        type=int,
        default=6,
        metavar="N",
        help=_("with --demo: number of demo hosts (default: 6, plus one slow host)"),
    )
    parser.add_argument(
        "--reset-layout",
        action="store_true",
        help=_("forget the saved tab layout (open tabs, active tab and sections)"),
    )
    parser.add_argument(
        "--install-desktop",
        action="store_true",
        help=_("Linux: add Zir SSH with its icon to the applications menu"),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=_("print a one-shot report and exit (no TUI)"),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=_("with --report: print machine-readable JSON"),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Zir SSH {__version__}",
    )
    return parser


def _resolve(
    args: argparse.Namespace,
) -> tuple[list[HostSpec], Settings, bool, Path | None]:
    config_path = find_config(args.config)
    config_hosts, settings = load_config(config_path)

    if args.interval is not None:
        settings = replace(settings, interval=args.interval)
    if args.timeout is not None:
        settings = replace(settings, timeout=args.timeout)
    if settings.interval < 0.5:
        raise ConfigError(_("--interval must be >= 0.5 seconds"))
    if settings.timeout < 1:
        raise ConfigError(_("--timeout must be >= 1 second"))

    if args.demo:
        if not 1 <= args.demo_hosts <= 200:
            raise ConfigError(_("--demo-hosts must be between 1 and 200"))
        return demo_specs(args.demo_hosts), settings, True, config_path

    specs = hosts_from_args(args.hosts) if args.hosts else config_hosts
    if args.local:
        if sys.platform != "linux":
            raise ConfigError(_("--local reads /proc and works on Linux only"))
        specs = [HostSpec(name="local", target="local", local=True), *specs]

    # No hosts: the TUI shows the first-run screen, --report fails in main().
    return specs, settings, False, config_path


def _no_hosts() -> str:
    return _(
        "no hosts configured.\n"
        "Pass SSH aliases (zir web-1 db-1), use --demo, or create {path} (see hosts.example.toml).",
        path=DEFAULT_CONFIG,
    )


def _fail(message: str, interactive: bool) -> int:
    print(f"zir: {message}", file=sys.stderr)
    if interactive:
        # Started by double-click: keep the window open long enough to read it.
        try:
            input("\n" + _("Press Enter to close…"))
        except (EOFError, KeyboardInterrupt):
            pass
    return 2


def _first_run(settings: Settings, demo_hosts: int) -> tuple[list[HostSpec], bool] | None:
    from .sshconfig import ssh_aliases
    from .welcome import WelcomeApp, save_hosts

    while True:
        result = WelcomeApp(ssh_aliases(), DEFAULT_CONFIG).run()
        if result is None or not result.restart:
            break
        set_language(pick_language(config=settings.language, ui_first=True))
    if result is None:
        return None
    if result.demo:
        return demo_specs(demo_hosts), True
    if result.save and not DEFAULT_CONFIG.exists():
        try:
            save_hosts(DEFAULT_CONFIG, result.specs)
        except OSError as exc:
            print(f"zir: " + _("could not save {path}: {error}", path=DEFAULT_CONFIG, error=exc), file=sys.stderr)
    return result.specs, False


# --------------------------------------------------------------------------
# Report mode
# --------------------------------------------------------------------------


async def _sample(collectors) -> list[HostReading]:
    names = list(collectors)
    results = await asyncio.gather(
        *(collectors[name].collect() for name in names), return_exceptions=True
    )
    readings: list[HostReading] = []
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            readings.append(
                HostReading(
                    spec=collectors[name].spec,
                    error=f"{type(result).__name__}: {result}",
                )
            )
        else:
            readings.append(result)
    return readings


async def _collect_for_report(collectors) -> list[HostReading]:
    await _sample(collectors)
    await asyncio.sleep(0.7)  # second sample gives CPU% and network rates
    return await _sample(collectors)


def _print_report(readings: list[HostReading], console: Console) -> int:
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column(_("HOST"), no_wrap=True)
    table.add_column(_("STATE"), no_wrap=True)
    for column in ("CPU", "MEM", "LOAD", "DISK", "FAILED", "NET", "UPTIME", "TIME"):
        table.add_column(_(column), justify="right", no_wrap=True)

    worst = "ok"
    for reading in readings:
        state = severity(reading.snapshot, reading.metrics, reading.error)
        color = SEVERITY_COLORS[state]
        mark = SEVERITY_MARKS[state]

        if state == "crit" or state == "error":
            worst = "crit"
        elif state == "warn" and worst == "ok":
            worst = "warn"

        snapshot = reading.snapshot
        if snapshot is None:
            table.add_row(
                reading.spec.name,
                f"[{color}]{mark} {_(state)}[/]",
                *("—" for _column in range(7)),
                human_ms(reading.latency),
            )
            continue

        cpu = (
            f"{reading.metrics.cpu_pct:.0f}%"
            if reading.metrics.cpu_pct is not None
            else "—"
        )
        disk = snapshot.worst_disk
        net = sum(
            rx + tx
            for name, (rx, tx) in reading.metrics.net_rates.items()
            if name != "lo"
        )
        table.add_row(
            reading.spec.name,
            f"[{color}]{mark} {_(state)}[/]",
            cpu,
            f"{snapshot.mem_percent:.0f}%",
            f"{snapshot.load[0]:.2f}",
            f"{disk.percent:.0f}% {disk.mount}" if disk else "—",
            str(len(snapshot.failed_services)),
            human_rate(net),
            human_uptime(snapshot.uptime),
            human_ms(reading.latency),
        )

    console.print(table)

    for reading in readings:
        findings = evaluate(reading.snapshot, reading.metrics) if reading.snapshot else []
        for finding in findings:
            color = "red" if finding.level == "crit" else "yellow"
            console.print(
                f"  [{color}]{reading.spec.name}[/]  {finding.message}"
            )
        if reading.snapshot is None:
            console.print(f"  [red]{reading.spec.name}[/]  {reading.error}")
            if reading.hint:
                console.print(f"           [grey50]{reading.hint}[/]")
        elif reading.error:
            console.print(
                f"  [yellow]{reading.spec.name}[/]  " + _("stale: {error}", error=reading.error)
            )

    console.print()
    state_color = SEVERITY_COLORS[worst]
    console.print(
        f"[{state_color}]{SEVERITY_MARKS[worst]} {_(worst)}[/] "
        f"[grey50]· {_n('{n} host', '{n} hosts', len(readings))}[/]"
    )
    return 0 if worst == "ok" else 1


def _json_report(readings: list[HostReading]) -> str:
    # Keys and states stay English: this output is for scripts.
    payload = []
    for reading in readings:
        state = severity(reading.snapshot, reading.metrics, reading.error)
        payload.append(
            {
                "host": reading.spec.name,
                "target": reading.spec.target,
                "state": state,
                "error": reading.error,
                "hint": reading.hint,
                "latency_ms": round(reading.latency * 1000),
                "metrics": {
                    "cpu_pct": reading.metrics.cpu_pct,
                    "net_rates": reading.metrics.net_rates,
                    "stack": asdict(reading.metrics.stack),
                },
                "findings": [
                    asdict(finding)
                    for finding in evaluate(
                        reading.snapshot, reading.metrics
                    )
                ]
                if reading.snapshot
                else [],
                "snapshot": asdict(reading.snapshot)
                if reading.snapshot is not None
                else None,
            }
        )
    return json.dumps(payload, indent=2, default=str)


def run_report(specs, settings, demo: bool) -> int:
    collectors = build_collectors(
        specs, settings.timeout, settings.connect_timeout, demo
    )
    readings = asyncio.run(_collect_for_report(collectors))
    console = Console()
    if not console.is_terminal:
        console = Console(width=200)  # cron / pipes: Rich assumes 80 columns and truncates
    return _print_report(readings, console)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from .keysetup import answer_askpass

    if argv is None and answer_askpass():
        return 0
    migrate_legacy()
    raw_args = argv if argv is not None else sys.argv[1:]
    set_language(pick_language(_early_lang(raw_args)))
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.install_desktop:
        if sys.platform != "linux":
            print("zir: " + _("--install-desktop is for Linux (macOS: zir.app, Windows: zir.exe)"), file=sys.stderr)
            return 2
        from .desktop import install_desktop_entry

        for path in install_desktop_entry():
            print(_("installed {path}", path=path))
        return 0

    interactive = not raw_args and sys.stdin.isatty()
    try:
        specs, settings, demo, config_path = _resolve(args)
    except ConfigError as exc:
        return _fail(str(exc), interactive)
    set_language(pick_language(args.lang, settings.language))

    if not specs:
        if args.report or not sys.stdout.isatty():
            return _fail(_no_hosts(), interactive)
        chosen = _first_run(settings, args.demo_hosts)
        if chosen is None:
            return 0
        specs, demo = chosen

    if args.report:
        disabled = set(load_prefs().get("disabled_hosts", []))
        specs = [spec for spec in specs if spec.enabled and spec.name not in disabled]
        if not specs:
            return _fail(_("all hosts are disabled"), interactive)
        if args.json:
            collectors = build_collectors(
                specs, settings.timeout, settings.connect_timeout, demo
            )
            readings = asyncio.run(_collect_for_report(collectors))
            print(_json_report(readings))
            return 0
        return run_report(specs, settings, demo)

    from .app import ZirApp
    from .store import Session
    from .widgets.settings import RESTART

    session = Session() if args.reset_layout else Session.load()
    names = [spec.name for spec in specs]
    if args.hosts and not args.demo:
        open_hosts = names  # explicit hosts on the command line win
    else:
        open_hosts = [name for name in session.open if name in names] or names

    collectors = build_collectors(
        specs, settings.timeout, settings.connect_timeout, demo
    )
    store = None
    while True:
        app = ZirApp(
            specs, collectors, settings, session, open_hosts, store=store, config_path=config_path
        )
        if app.run() != RESTART:
            return 0
        # Language switched in F2: rebuild the UI, keep data, history and layout.
        set_language(pick_language(config=settings.language, ui_first=True))
        store, session, open_hosts = app.store, app.session, list(app.open_hosts)
