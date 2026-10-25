import asyncio
from pathlib import Path

import pytest

from zir.app import FLEET_PANE, LOCAL_PANE, ZirApp
from zir.collector import build_collectors, demo_specs
from zir.config import Settings
from zir.store import Session
from zir.widgets.host_view import HostView
from textual.widgets import TabbedContent


def make_app(count=6, session=None, open_hosts=None):
    specs = demo_specs(count)
    collectors = build_collectors(specs, 8.0, 5, demo=True)
    return ZirApp(
        specs,
        collectors,
        Settings(interval=0.5),
        session or Session(),
        open_hosts,
        persist_session=False,
    )


def run(coro):
    return asyncio.run(coro)


def test_tabs_navigation_and_close():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(140, 40)) as pilot:
            servers = app.query_one("#servers", TabbedContent)
            assert servers.active == FLEET_PANE
            assert servers.tab_count == 2 + 7  # Local + Fleet + 6 demo + edge-slow

            await pilot.press("ctrl+pagedown")
            assert app.active_host == "web-1"
            await pilot.press("alt+4")
            assert app.active_host == "web-2"
            # What a real macOS terminal delivers for Option+3 / ESC 3 (not "alt+3").
            await pilot.press("pound_sign")
            assert app.active_host == "web-1"
            await pilot.press("alt+4")
            await pilot.press("ctrl+pageup")
            assert app.active_host == "web-1"

            await pilot.press("right_square_bracket")
            view = app._active_view()
            assert view.section == "web"

            await pilot.press("ctrl+w")
            await pilot.pause()
            assert servers.tab_count == 8
            assert "web-1" not in app.open_hosts

            await app.open_host("web-1")
            await pilot.pause()
            assert app.active_host == "web-1"
            assert servers.tab_count == 9

            await pilot.press("alt+2")
            await pilot.press("ctrl+w")  # Fleet stays
            assert servers.tab_count == 9

            await pilot.press("ctrl+pageup")  # Local is the first tab, before Fleet
            assert servers.active == LOCAL_PANE and app.active_host is None

    run(scenario())


def test_badges_and_pause():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(1.5)
            servers = app.query_one("#servers", TabbedContent)
            backup = servers.get_tab(app.pane_ids["backup-1"]).label.plain
            worker = servers.get_tab(app.pane_ids["worker-1"]).label.plain
            assert "×" in backup
            assert "■" in worker

            await pilot.press("alt+3")
            await pilot.press("p")
            assert app.store["web-1"].paused
            await pilot.pause(0.3)
            assert "‖" in servers.get_tab(app.pane_ids["web-1"]).label.plain
            await pilot.press("P")
            assert all(state.paused for state in app.store)

    run(scenario())


def test_modals_switcher_and_quit_confirm():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("question_mark")
            await pilot.pause(0.5)  # render loop keeps running under the modal
            assert type(app.screen).__name__ == "HelpScreen"
            await pilot.press("escape")

            await app.open_host("db-1")  # ensure it's open, then go back to Fleet
            await pilot.press("alt+2")
            await pilot.press("ctrl+k")
            await pilot.pause(0.3)
            await pilot.press(*"cache")
            await pilot.pause(0.5)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.active_host == "cache-1"

            await pilot.pause(1.0)  # worker-1 / backup-1 are crit
            await pilot.press("q")
            await pilot.pause(0.2)
            assert type(app.screen).__name__ == "ConfirmQuit"
            await pilot.press("n")
            assert app.is_running

    run(scenario())


def test_quit_always_asks():
    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("q")
            await pilot.pause(0.2)
            assert type(app.screen).__name__ == "ConfirmQuit"
            await pilot.press("enter")  # focus is on "Stay"
            await pilot.pause(0.2)
            assert app.is_running
            assert type(app.screen).__name__ != "ConfirmQuit"

            await pilot.press("ctrl+q")
            await pilot.pause(0.2)
            assert type(app.screen).__name__ == "ConfirmQuit"
            await pilot.click("#quit-yes")
            await pilot.pause(0.2)
        assert app.return_code == 0

    run(scenario())


def test_session_restored():
    async def scenario():
        session = Session(open=["db-1", "web-1"], active="web-1", sections={"web-1": "disks"})
        app = make_app(session=session, open_hosts=session.open)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app.open_hosts == ["db-1", "web-1"]
            assert app.active_host == "web-1"
            assert app._active_view().section == "disks"

    run(scenario())


@pytest.mark.parametrize("size", [(60, 20), (100, 30), (220, 60)])
def test_fills_terminal_and_follows_resize(size):
    async def scenario():
        app = make_app()
        async with app.run_test(size=size) as pilot:
            await pilot.press("alt+3")
            await pilot.pause(1.0)
            servers = app.query_one("#servers", TabbedContent)
            view = app._active_view()
            assert servers.size.width == size[0]
            assert view.size.width == size[0]
            assert view.has_class("narrow") == (size[0] < 100)

            new_size = (size[0] + 40, size[1] + 10)
            await pilot.resize_terminal(*new_size)
            await pilot.pause(0.3)
            assert servers.size.width == new_size[0]
            assert view.size.width == new_size[0]
            assert view.region.bottom >= new_size[1] - 2  # only the footer below
            assert view.has_class("narrow") == (new_size[0] < 100)

    run(scenario())


def test_permission_denied_asks_password_and_installs_key(monkeypatch):
    from zir import app as app_module
    from zir.collector import HostReading
    from zir.config import HostSpec

    spec = HostSpec(name="vps", target="root@203.0.113.10", port=2345)
    installed = []

    class LockedCollector:
        """Rejects the key until the fake install_key 'adds' it."""

        def __init__(self, inner):
            self.inner = inner

        async def collect(self):
            if not installed:
                return HostReading(spec=spec, error="root@203.0.113.10: Permission denied (publickey,password).")
            reading = await self.inner.collect()
            reading.spec = spec
            return reading

    async def fake_install(spec_, password, public_key, **kwargs):
        if password != "s3cret":
            raise app_module.KeySetupError("wrong password (or password login is disabled on the server)")
        installed.append((spec_.target, password))

    async def fake_key(spec_):
        return Path("/tmp/id_ed25519.pub"), False

    monkeypatch.setattr(app_module, "install_key", fake_install)
    monkeypatch.setattr(app_module, "ensure_public_key", fake_key)

    async def scenario():
        inner = build_collectors([HostSpec(name="web-1", target="web-1")], 8.0, 5, demo=True)["web-1"]
        app = ZirApp(
            [spec], {"vps": LockedCollector(inner)}, Settings(interval=0.5), Session(), None,
            persist_session=False,
        )
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.5)
            assert type(app.screen).__name__ == "PasswordScreen"

            await pilot.press(*"wrong", "enter")
            await pilot.pause(0.5)
            assert type(app.screen).__name__ == "PasswordScreen"  # asked again
            assert "wrong password" in str(app.screen.query_one("#pw-error").render())

            await pilot.press(*"s3cret", "enter")
            await pilot.pause(1.5)
            assert installed == [("root@203.0.113.10", "s3cret")]
            assert len(app.screen_stack) == 1
            assert app.store["vps"].level in ("ok", "warn", "crit")

    run(scenario())


def test_add_and_remove_server_from_the_app(tmp_path):
    from zir.config import load_config

    config = tmp_path / "hosts.toml"
    config.write_text('[settings]\ninterval = 2.0\n\n[hosts."keep-me"]\ntarget = "root@192.0.2.1"\n')

    async def scenario():
        specs = demo_specs(2)
        app = ZirApp(specs, build_collectors(specs, 8.0, 5, demo=True), Settings(interval=0.5),
                            Session(), None, persist_session=False, config_path=config)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("ctrl+n")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "AddHostScreen"
            await pilot.press(*"root@203.0.113.10:2345")
            app.screen.query_one("#host-title").value = "Shop (production)"
            assert app.screen.query_one("#host-name").value == "203.0.113.10"  # suggested from the address
            await pilot.click("#add-ok")
            await pilot.pause(0.8)
            assert len(app.screen_stack) == 1
            assert app.active_host == "203.0.113.10"
            assert app.servers.get_tab(app.pane_ids["203.0.113.10"]).label.plain.startswith("Shop (production)")
            assert "203.0.113.10" in app.workers_by_host
            saved = {h.name: h for h in load_config(config)[0]}
            assert saved["203.0.113.10"].port == 2345 and saved["203.0.113.10"].title == "Shop (production)"
            assert "keep-me" in saved

            await pilot.press("delete")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "ConfirmRemove"
            await pilot.click("#rm-yes")
            await pilot.pause(0.8)
            assert "203.0.113.10" not in app.store and "203.0.113.10" not in app.open_hosts
            assert [h.name for h in load_config(config)[0]] == ["keep-me"]

    run(scenario())


def test_add_server_validation():
    async def scenario():
        app = make_app(2)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("ctrl+n")
            await pilot.pause(0.3)
            await pilot.click("#add-ok")  # empty address
            await pilot.pause(0.2)
            assert type(app.screen).__name__ == "AddHostScreen"
            assert "address" in str(app.screen.query_one("#add-status").render()).lower()
            app.screen.query_one("#host-address").value = "web-1"
            await pilot.pause(0.2)
            assert app.screen.query_one("#host-name").value == "web-1-2"  # free name suggested
            app.screen.query_one("#host-name").value = "web-1"  # already taken
            await pilot.click("#add-ok")
            await pilot.pause(0.2)
            assert "already exists" in str(app.screen.query_one("#add-status").render())

    run(scenario())


def test_copy_table_row_and_mouse_selection(monkeypatch):
    from textual.widgets import DataTable, Static

    from zir import clipboard

    monkeypatch.setattr(clipboard, "_clipboard_tools", lambda: (None, None))

    async def scenario():
        app = make_app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("alt+3")
            view = app._active_view()
            view.show_section("procs")
            table = view.query_one("#procs-table", DataTable)
            for _attempt in range(50):
                if table.row_count:
                    break
                await pilot.pause(0.1)
            table.focus()
            await pilot.press("ctrl+c")
            first = "\t".join(str(getattr(c, "plain", c)).strip() for c in table.get_row_at(0))
            assert app._clipboard == first and "\t" in first
            await pilot.press("down", "y")
            assert app._clipboard != first and "\t" in app._clipboard

            # Dragging over plain text anywhere in the UI copies it on release.
            summary = view.query_one("#procs-summary", Static)
            await pilot.mouse_down(summary, offset=(0, 0))
            await pilot.hover(summary, offset=(5, 0))
            await pilot.mouse_up(summary, offset=(5, 0))
            await pilot.pause(0.1)
            assert app._clipboard and "\t" not in app._clipboard
            assert app._clipboard in summary.render().plain

    asyncio.run(scenario())


def test_ctrl_v_pastes_the_system_clipboard(monkeypatch):
    from textual.widgets import Input

    from zir import clipboard

    monkeypatch.setattr(clipboard, "paste_text", lambda app: "10.0.0.7")

    async def scenario():
        app = make_app()
        async with app.run_test(size=(140, 40)) as pilot:
            box = Input()
            await app.screen.mount(box)
            box.focus()
            await pilot.press("ctrl+v")
            assert box.value == "10.0.0.7"

    asyncio.run(scenario())
