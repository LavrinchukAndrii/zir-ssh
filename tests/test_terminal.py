"""Built-in terminal and host tab tools (terminal sessions, snippets, disabling a host)."""

import asyncio
import sys

import pytest

from zir import clipboard
from zir.prefs import load_prefs
from zir.widgets.terminal import SUPPORTED, TerminalView
from test_app import make_app

pytestmark = pytest.mark.skipif(not SUPPORTED, reason="the built-in terminal needs a POSIX pty")


def _lines(view: TerminalView) -> list[str]:
    buf = view.screen_buf
    return ["".join(c.data for c in buf.buffer[y].values()).rstrip() for y in range(buf.lines)]


async def _wait_for(predicate, timeout=6.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        try:
            if predicate():
                return True
        except (AttributeError, LookupError):  # not built yet, e.g. the pty screen of a fresh session
            pass
        await asyncio.sleep(0.1)
    return False


def test_terminal_sessions_and_keys():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("alt+3")
            view = app._active_view()
            view.show_section("terminal")
            await pilot.pause(0.5)
            term = app.focused
            assert isinstance(term, TerminalView) and term.running
            await pilot.press(*"echo hi-$((40+2))", "enter")
            assert await _wait_for(lambda: any("hi-42" in line for line in _lines(term)))

            await pilot.press("ctrl+k")  # goes to the shell, not to the host switcher
            await pilot.pause(0.2)
            assert len(app.screen_stack) == 1

            await pilot.press("q")  # typed into the shell, does not quit
            await pilot.pause(0.2)
            assert app.is_running

            await pilot.press("alt+t")
            await pilot.pause(0.8)
            assert len(view.terminal().sessions()) == 2
            await pilot.press("alt+w")
            await pilot.pause(0.3)
            assert len(view.terminal().sessions()) == 1

            await pilot.press("f4")  # leave the terminal: next section
            assert view.section == "snippets"

    asyncio.run(scenario())


def test_local_tab_runs_a_shell_on_this_computer(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))

    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("alt+1")
            await pilot.pause(0.5)
            term = app.focused
            assert isinstance(term, TerminalView) and term.running
            assert term.command == ["/bin/sh", "-l"]
            await pilot.press(*'[ "$PWD" = "$HOME" ] && echo home-$((40+2))', "enter")  # starts in ~
            assert await _wait_for(lambda: any(line == "home-42" for line in _lines(term)))

            await pilot.press("alt+t")
            await pilot.pause(0.8)
            assert len(app.local.sessions()) == 2
            await pilot.press("alt+w")
            await pilot.pause(0.3)
            assert len(app.local.sessions()) == 1

            await pilot.press("ctrl+w")  # typed into the shell, the tab stays
            await pilot.press("ctrl+pagedown")
            assert app.servers.active == "fleet-pane"
            await pilot.press("ctrl+w")
            assert app.servers.tab_count == 2 + 7

    asyncio.run(scenario())


def test_snippet_check_and_insert():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("alt+3")
            view = app._active_view()
            view.show_section("snippets")
            await pilot.pause(0.3)
            section = view.query_one("#snippets")
            section.check()
            assert await _wait_for(lambda: bool(section.commands))
            index = section.commands.index("nginx -t")
            assert await _wait_for(lambda: bool(section.query(f"#snip-{index}")))  # buttons mount async
            section.query_one(f"#snip-{index}").press()
            assert await _wait_for(lambda: view.section == "terminal")
            assert await _wait_for(lambda: isinstance(app.focused, TerminalView)
                                   and any("nginx -t" in line for line in _lines(app.focused)))

    asyncio.run(scenario())


def test_disable_host_stops_polling_and_is_remembered():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("alt+3")
            await pilot.pause(1.0)
            state = app.store["web-1"]
            await pilot.press("d")
            assert state.disabled and state.level == "off"
            version = state.version
            await pilot.pause(2.0)  # interval is 0.5 s: no polls while disabled
            assert state.version == version
            assert "⊘" in app.servers.get_tab(app.pane_ids["web-1"]).label.plain
            assert load_prefs()["disabled_hosts"] == ["web-1"]
            assert make_app().store["web-1"].disabled  # remembered for the next start
            await pilot.press("d")
            await pilot.pause(1.5)
            assert not state.disabled and state.version > version
            assert load_prefs()["disabled_hosts"] == []

    asyncio.run(scenario())


async def _open_terminal(app, pilot):
    await pilot.press("alt+3")
    view = app._active_view()
    view.show_section("terminal")
    await pilot.pause(0.5)
    term = app.focused
    assert isinstance(term, TerminalView)
    await _wait_for(lambda: term._got_output)
    return term


def _find(term: TerminalView, needle: str) -> tuple[int, int] | None:
    for y, line in enumerate(_lines(term)):
        x = line.find(needle)
        if x >= 0:
            return y, x
    return None


def test_mouse_select_copy_and_right_click_paste(monkeypatch):
    monkeypatch.setattr(clipboard, "_clipboard_tools", lambda: (None, None))  # never touch the real clipboard

    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            term = await _open_terminal(app, pilot)
            await pilot.press(*"clear; echo copy-me-please", "enter")
            assert await _wait_for(lambda: _find(term, "copy-me-please") and _find(term, "copy-me-please")[1] == 0)
            y, x = _find(term, "copy-me-please")
            await pilot.mouse_down(term, offset=(x, y))
            await pilot.hover(term, offset=(x + 6, y))
            await pilot.mouse_up(term, offset=(x + 6, y))
            assert app._clipboard == "copy-me"
            # Right click types the clipboard at the prompt.
            await pilot.click(term, offset=(1, 1), button=3)
            assert await _wait_for(lambda: sum(line.count("copy-me") for line in _lines(term)) >= 2)

    asyncio.run(scenario())


def test_mouse_is_reported_to_programs_that_ask_for_it():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            term = await _open_terminal(app, pilot)
            # Like htop/mc/vim: switch on click reporting (1000) in SGR format (1006), then show raw input.
            await pilot.press(*"clear; printf '\\033[?1000h\\033[?1006h'; cat -v", "enter")
            assert await _wait_for(lambda: term.mouse_reporting)
            await pilot.click(term, offset=(4, 2))
            await pilot.pause(0.3)
            await pilot.press("enter")
            assert await _wait_for(lambda: _find(term, "^[[<0;5;3M") is not None)
            await pilot.press("ctrl+c")

    asyncio.run(scenario())


def test_shift_drag_selects_inside_mouse_programs_and_double_click_copies_a_word(monkeypatch):
    monkeypatch.setattr(clipboard, "_clipboard_tools", lambda: (None, None))

    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            term = await _open_terminal(app, pilot)
            await pilot.press(*"clear; echo grab-this word; printf '\\033[?1000h\\033[?1006h'; cat -v", "enter")
            assert await _wait_for(lambda: term.mouse_reporting and _find(term, "grab-this word") is not None)
            y, x = _find(term, "grab-this word")
            await pilot.mouse_down(term, offset=(x, y), shift=True)
            await pilot.hover(term, offset=(x + 8, y))
            await pilot.mouse_up(term, offset=(x + 8, y), shift=True)
            assert app._clipboard == "grab-this"
            assert term.mouse_reporting  # the program keeps the mouse afterwards
            await pilot.pause(0.3)
            assert _find(term, "^[[<") is None  # nothing was reported to it
            await pilot.press("ctrl+c")  # still an interrupt for the shell, not a copy
            await pilot.press(*"printf '\\033[?1000l'; clear; echo first second", "enter")
            assert await _wait_for(lambda: not term.program_mouse and _find(term, "first second") is not None)
            y, x = _find(term, "first second")
            await pilot.click(term, offset=(x + 8, y), times=2)
            assert app._clipboard == "second"
            await pilot.click(term, offset=(x + 2, y), times=3)
            assert app._clipboard == "first second"

    asyncio.run(scenario())


def test_password_prompt_offers_identity_cards(tmp_path, monkeypatch):
    from zir import credentials
    from zir.credentials import Credential, Vault
    from zir.widgets.credentials import CredentialPicker

    (tmp_path / "id_ed25519").write_bytes(b"fake private key")
    monkeypatch.setattr(credentials, "ssh_dir", lambda: tmp_path)

    async def scenario():
        app = make_app()
        app.vault = Vault(tmp_path / "credentials.enc")
        app.vault.cards = [Credential("root on demo", "root", "hunter-42")]
        app.vault.save()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("alt+3")
            view = app._active_view()
            view.show_section("terminal")
            await pilot.pause(0.5)
            term = app.focused
            term.write("printf 'Пароль: '; stty -echo; read x; stty echo; echo; echo got-$x\r")
            assert await _wait_for(lambda: isinstance(app.screen, CredentialPicker))
            await pilot.press("enter")  # the first card
            assert await _wait_for(lambda: any("got-hunter-42" in line for line in _lines(term)))
            assert app.focused is term
            assert not any("hunter-42" in line and "got-" not in line for line in _lines(term))

    asyncio.run(scenario())


def test_picker_opens_the_manager():
    from zir.widgets.credentials import CredentialManager, CredentialPicker

    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("alt+3")
            view = app._active_view()
            view.show_section("terminal")
            await pilot.pause(0.5)
            view.query_one("#term-passwords").press()
            assert await _wait_for(lambda: isinstance(app.screen, CredentialPicker))
            await pilot.press("enter")  # no cards: the only item is "Manage identity cards…"
            assert await _wait_for(lambda: isinstance(app.screen, CredentialManager))

    asyncio.run(scenario())


def test_scrollback_keeps_lines_compactly():
    from zir.widgets.terminal import _pty, freeze

    pyte, _pty_process, ScrollbackScreen = _pty()
    screen = ScrollbackScreen(20, 3, history=100)
    pyte.ByteStream(screen).feed(b"".join(b"\x1b[31mred\x1b[0m %d\r\n" % n for n in range(10)))
    assert len(screen.scrollback) == 8
    cells, runs = screen.scrollback[0]
    assert cells == "red 0" and [n for n, _attrs in runs] == [3, 2] and runs[0][1][0] == "red"
    assert freeze(screen.buffer[0])[0] == "red 8"  # live lines freeze the same way
    pyte.ByteStream(screen).feed(b"\x1b[3J")  # `clear` drops the history too
    assert not screen.scrollback


def test_shift_pageup_shows_the_history():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            term = await _open_terminal(app, pilot)
            await pilot.press(*"clear; seq 1 300", "enter")
            assert await _wait_for(lambda: _find(term, "300") is not None)
            await pilot.pause(0.3)
            view = lambda: ["".join(term._cells(y)).rstrip() for y in range(term.screen_buf.lines)]  # noqa: E731
            await pilot.press("shift+pageup")
            assert term._offset > 0
            assert "300" not in view() and str(300 - term.screen_buf.lines) in view()
            await pilot.press("shift+pagedown")
            assert term._offset == 0
            await pilot.press("shift+pageup", "x")  # typing goes back to the live screen
            assert term._offset == 0

    asyncio.run(scenario())
