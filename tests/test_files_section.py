"""The Files section: two panels, keys like mc, copying with overwrite questions."""

import asyncio
import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from zir.filesystems import LocalFS, Runner, ShellFS
from zir.widgets.file_dialogs import AskText, Confirm, OverwriteScreen, ViewerScreen
from zir.widgets.files_section import FilePanel, FilesSection, FileTable


def _server_fs():
    if sys.platform == "win32":
        return LocalFS("server")
    return ShellFS(Runner(), "server")


class FilesApp(App):
    def __init__(self, remote) -> None:
        super().__init__()
        self.remote = remote

    def compose(self) -> ComposeResult:
        yield FilesSection(LocalFS(), self.remote, id="files")


async def _wait_for(predicate, timeout: float = 8.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


async def _wait_screen(app, kind, timeout: float = 8.0):
    await _wait_for(lambda: isinstance(app.screen, kind), timeout)
    return app.screen


def _names(panel: FilePanel) -> list[str]:
    return [entry.name for entry in panel.rows]


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    local, server = tmp_path / "local", tmp_path / "server"
    (local / "site" / "img").mkdir(parents=True)
    (local / "site" / "index.html").write_text("new index")
    (local / "site" / "img" / "logo.png").write_bytes(b"\x89PNG" + bytes(2000))
    (local / "readme.txt").write_text("read me")
    (local / "zeta.log").write_text("log")
    (server / "site").mkdir(parents=True)
    (server / "site" / "index.html").write_text("old index")
    (server / "readme.txt").write_text("old readme")
    return local, server


def test_copy_both_ways_with_overwrite_questions(tmp_path):
    local, server = _setup(tmp_path)
    app = FilesApp(_server_fs())

    async def scenario():
        async with app.run_test(size=(160, 40)) as pilot:
            section = app.query_one(FilesSection)
            left, right = section.panel("left"), section.panel("right")
            await left.load(str(local))
            await right.load(str(server))
            left.focus_table()
            await pilot.pause()
            assert _names(left)[:3] == ["..", "site", "readme.txt"]  # folders first

            # Mark two items with Insert and copy them with F5.
            left.table.move_cursor(row=1)
            await pilot.press("insert", "insert")
            assert left.marked == {"site", "readme.txt"}
            await pilot.press("f5")
            ask = await _wait_screen(app, AskText)
            assert ask.query_one("Input").value == right.path
            await pilot.press("enter")

            # Two conflicts: keep index.html, overwrite readme.txt.
            first = await _wait_screen(app, OverwriteScreen)
            assert first.conflict.source.rel == "site/index.html"
            await pilot.press("n")
            await _wait_for(lambda: isinstance(app.screen, OverwriteScreen) and app.screen is not first)
            assert app.screen.conflict.source.rel == "readme.txt"
            await pilot.press("y")
            await _wait_for(lambda: (server / "site" / "img" / "logo.png").exists())
            await _wait_for(lambda: not left.marked and "img" not in _names(right))
            assert (server / "site" / "index.html").read_text() == "old index"
            assert (server / "readme.txt").read_text() == "read me"
            assert (server / "site" / "img" / "logo.png").read_bytes() == (local / "site" / "img" / "logo.png").read_bytes()

            # Tab goes to the server panel; F7 makes a folder there.
            await pilot.press("tab")
            assert app.focused is right.table
            await pilot.press("f7")
            await _wait_screen(app, AskText)
            await pilot.press(*"backup", "enter")
            await _wait_for(lambda: "backup" in _names(right))
            assert (server / "backup").is_dir()
            await _wait_for(lambda: right.cursor_entry().name == "backup", 3)

            # Copy server -> this computer with "overwrite all".
            right.table.move_cursor(row=_names(right).index("site"))
            await pilot.press("f5")
            await _wait_screen(app, AskText)
            await pilot.press("enter")
            await _wait_screen(app, OverwriteScreen)
            await pilot.press("a")
            await _wait_for(lambda: (local / "site" / "index.html").read_text() == "old index")

            # Quick search: typing jumps to a name and does not trigger app keys (q = quit).
            left.focus_table()
            await _wait_for(lambda: "site" in _names(left))
            await pilot.press("z", "e")
            assert left.cursor_entry().name == "zeta.log"
            await pilot.press("q")  # nothing starts with "zeq": a bell, but no quit dialog
            await pilot.pause()
            assert app.screen is app.screen_stack[0]

            # F8 deletes after a confirmation; the cursor stays nearby.
            await pilot.press("escape", "f8")
            await _wait_screen(app, Confirm)
            await pilot.press("y")
            await _wait_for(lambda: not (local / "zeta.log").exists())
            await _wait_for(lambda: "zeta.log" not in _names(left))

            # F3 views a file, F4 edits and saves it.
            left.table.move_cursor(row=_names(left).index("readme.txt"))
            await pilot.press("f3")
            viewer = await _wait_screen(app, ViewerScreen)
            assert "read me" in viewer.query_one("TextArea").text
            await pilot.press("escape")
            await _wait_for(lambda: not isinstance(app.screen, ViewerScreen))
            await pilot.press("f4")
            editor = await _wait_screen(app, ViewerScreen)
            editor.query_one("TextArea").text = "edited"
            await pilot.press("ctrl+s")
            await _wait_for(lambda: (local / "readme.txt").read_text() == "edited")
            await pilot.press("escape")
            await _wait_for(lambda: app.screen is app.screen_stack[0])

            # Backspace goes up and puts the cursor on the folder we came from.
            left.table.move_cursor(row=_names(left).index("site"))
            await pilot.press("enter")
            await _wait_for(lambda: left.path == str(local / "site"))
            await pilot.press("backspace")
            await _wait_for(lambda: left.path == str(local))
            assert left.cursor_entry().name == "site"

    asyncio.run(scenario())


def test_rename_and_masks(tmp_path):
    local, server = _setup(tmp_path)
    app = FilesApp(_server_fs())

    async def scenario():
        async with app.run_test(size=(120, 30)) as pilot:
            section = app.query_one(FilesSection)
            left = section.panel("left")
            await left.load(str(local))
            left.focus_table()
            await pilot.press("plus")
            await _wait_screen(app, AskText)
            app.screen.query_one("Input").value = "*.txt,*.LOG"
            await pilot.press("enter")
            await _wait_for(lambda: left.marked == {"readme.txt", "zeta.log"})
            await pilot.press("asterisk")
            assert left.marked == set()  # the only other entry is a folder
            left.table.move_cursor(row=_names(left).index("readme.txt"))
            await pilot.press("f2")
            ask = await _wait_screen(app, AskText)
            ask.query_one("Input").value = "README.md"
            await pilot.press("enter")
            await _wait_for(lambda: (local / "README.md").exists())
            await _wait_for(lambda: left.cursor_entry() is not None and left.cursor_entry().name == "README.md")

    asyncio.run(scenario())


def test_files_tab_in_the_app_keeps_f_keys_for_files():
    from test_app import make_app

    app = make_app()

    async def scenario():
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("alt+3")
            view = app._active_view()
            view.show_section("files")
            await _wait_for(lambda: view.section == "files")
            await _wait_for(lambda: isinstance(app.focused, FileTable))
            section = view.query_one(FilesSection)
            await _wait_for(lambda: section.panel("right").path is not None)
            await pilot.press("f4")  # Edit in the files panel, not "next section"
            await pilot.pause(0.3)
            assert view.section == "files"
            while not isinstance(app.focused, FileTable):
                await pilot.press("escape")
                await pilot.pause(0.1)
            await pilot.press("f6")  # Move, not the identity cards
            await pilot.pause(0.3)
            assert type(app.screen).__name__ != "CredentialManager"

    asyncio.run(scenario())
