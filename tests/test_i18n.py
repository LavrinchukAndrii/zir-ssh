import ast
import asyncio
import re
from pathlib import Path

import pytest

from zir import i18n
from zir.cli import pick_language
from zir.i18n import _, _n, set_language
from zir.locale_uk import UK, UK_PLURAL
from zir.prefs import load_prefs, save_prefs

SOURCE = Path(__file__).resolve().parent.parent / "zir"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _source_strings() -> tuple[set[str], set[str]]:
    singles, plurals = set(), set()
    for path in SOURCE.rglob("*.py"):
        if path.name == "locale_uk.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            first = node.args[0] if node.args else None
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            if node.func.id == "_":
                singles.add(first.value)
            elif node.func.id == "_n":
                plurals.add(first.value)
            elif node.func.id == "Binding" and len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                singles.add(node.args[2].value)
    return singles, plurals


def _dynamic_strings() -> set[str]:
    """Strings translated through a variable: columns, states, help lines."""
    from zir.app import KEYS
    from zir.widgets.fleet_view import COLUMNS
    from zir.widgets.host_view import SECTIONS
    from zir.widgets.stack_sections import GROUP_LABELS, STACK_COLUMNS
    from zir.widgets.terminal_section import HINTS

    host_columns = (
        "PID USER CPU% MEM% RSS TIME STAT COMMAND FILESYSTEM MOUNT SIZE USED FREE USE INODE "
        "IFACE RX TX PROTO LISTEN PROCESS UNIT STATE DESCRIPTION"
    ).split() + ["RX TOTAL", "TX TOTAL"]
    report_columns = ["CPU", "MEM", "LOAD", "DISK", "FAILED", "NET", "UPTIME", "TIME"]
    levels = ["ok", "warn", "crit", "error", "pending"]
    return (
        {label for _key, label in COLUMNS if label}
        | {what for _key, what in KEYS} | {"Mouse drag", "Double / triple click"}
        | {text for pair in HINTS for text in pair} - {"Alt+T", "Alt+W", "F6", "F3/F4"}
        | set(host_columns) | set(report_columns) | set(levels)
        | {label for tables in STACK_COLUMNS.values() for labels in tables.values() for label in labels}
        | set(GROUP_LABELS.values()) - {"PHP", "FTP", "DNS"}
        | {"SERVICE", "RUNNING FOR", "MEMORY", "RESTARTS", "AUTOSTART"}
        | {title for _key, title in SECTIONS if title != "PHP"}
    )


def test_every_string_has_a_ukrainian_translation():
    singles, plurals = _source_strings()
    missing = sorted((singles | _dynamic_strings()) - UK.keys())
    assert not missing, "add to zir/locale_uk.py: " + repr(missing)
    missing_plural = sorted(plurals - UK_PLURAL.keys())
    assert not missing_plural, "add to UK_PLURAL: " + repr(missing_plural)


def test_placeholders_match():
    for source, target in UK.items():
        assert set(PLACEHOLDER.findall(source)) == set(PLACEHOLDER.findall(target)), source
    for source, forms in UK_PLURAL.items():
        for form in forms:
            assert set(PLACEHOLDER.findall(source)) == set(PLACEHOLDER.findall(form)), source


def test_no_unused_catalog_entries():
    singles, plurals = _source_strings()
    unused = sorted(UK.keys() - singles - _dynamic_strings())
    assert not unused, "remove from zir/locale_uk.py: " + repr(unused)
    assert not (UK_PLURAL.keys() - plurals)


@pytest.mark.parametrize(
    "n, expected",
    [(1, "1 сервер"), (2, "2 сервери"), (4, "4 сервери"), (5, "5 серверів"), (11, "11 серверів"),
     (12, "12 серверів"), (21, "21 сервер"), (22, "22 сервери"), (25, "25 серверів"), (111, "111 серверів")],
)
def test_ukrainian_plurals(n, expected):
    set_language("uk")
    assert _n("{n} host", "{n} hosts", n) == expected


def test_english_and_ukrainian():
    set_language("en")
    assert _("{ms} ms", ms=5) == "5 ms"
    assert _n("{n} host", "{n} hosts", 1) == "1 host"
    set_language("uk")
    assert _("{ms} ms", ms=5) == "5 мс"
    assert _("unknown string stays") == "unknown string stays"


def test_language_priority(monkeypatch):
    assert pick_language() == "uk"  # default
    assert pick_language(config="en") == "en"
    save_prefs({"language": "en"})
    assert load_prefs()["language"] == "en"
    assert pick_language(config="uk") == "en"  # UI choice beats hosts.toml
    monkeypatch.setenv("ZIR_LANG", "uk_UA.UTF-8")
    assert pick_language() == "uk"  # env beats prefs
    assert pick_language("en") == "en"  # --lang beats everything
    assert pick_language(ui_first=True) == "en"  # right after F2 the saved choice wins


def test_bindings_follow_language():
    from zir.app import ZirApp

    set_language("uk")
    labels = {b.description for b in ZirApp.BINDINGS}
    assert {"Вихід", "Налаштування", "Довідка"} <= labels
    set_language("en")
    assert "Quit" in {b.description for b in ZirApp.BINDINGS}


def _demo_app():
    from zir.app import ZirApp
    from zir.collector import build_collectors, demo_specs
    from zir.config import Settings
    from zir.store import Session

    specs = demo_specs(6)
    return ZirApp(
        specs, build_collectors(specs, 8.0, 5, demo=True), Settings(interval=0.5), Session(), None,
        persist_session=False,
    )


def _screen_text(app) -> str:
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


def test_ukrainian_ui_renders():
    set_language("uk")
    app = _demo_app()

    async def scenario():
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(1.2)
            text = _screen_text(app)
            assert "Локальний" in text and "Усі(7)" in text and "СЕРВЕР" in text and "Вихід" in text
            assert "7 серверів" in text
            await pilot.press("alt+3")
            await pilot.pause(0.8)
            text = _screen_text(app)
            assert "Огляд" in text and "Процеси" in text and "ПАМ'ЯТЬ" in text

    asyncio.run(scenario())


def test_f2_switches_language_and_restarts():
    from zir.widgets.settings import RESTART

    set_language("uk")
    app = _demo_app()

    async def scenario():
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("f2")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "SettingsScreen"
            assert "Мова інтерфейсу" in _screen_text(app)
            await pilot.press("down", "enter")  # select English
            await pilot.click("#settings-save")
            await pilot.pause(0.3)

    asyncio.run(scenario())
    assert app.return_value == RESTART
    assert load_prefs()["language"] == "en"


def test_underscore_is_never_assigned():
    """`x, _ = ...` in a function makes `_` local and breaks `_()` calls there."""
    offenders = []
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "use a named throwaway instead of _: " + ", ".join(offenders)


def test_help_screen_is_readable():
    set_language("uk")
    app = _demo_app()

    async def scenario():
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("question_mark")
            await pilot.pause(0.3)
            text = _screen_text(app)
            assert "наступна вкладка сервера" in text and "Позначки:" in text

    asyncio.run(scenario())


def test_human_host_names_from_settings():
    from zir.widgets.settings import TITLES

    set_language("en")
    app = _demo_app()

    async def scenario():
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("f2")
            await pilot.pause(0.3)
            field = app.screen.query_one("#title-0")
            field.value = "Shop (production)"
            await pilot.click("#settings-save")
            await pilot.pause(0.5)
            assert app.store["web-1"].title == "Shop (production)"
            assert app.servers.get_tab(app.pane_ids["web-1"]).label.plain.startswith("Shop (production)")
            text = _screen_text(app)
            assert "Shop (production)" in text and "web-1" in text  # technical name stays visible in Fleet
            assert app.is_running  # names apply without restarting the UI

    asyncio.run(scenario())
    assert load_prefs()["titles"] == {"web-1": "Shop (production)"}
