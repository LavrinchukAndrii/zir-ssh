"""The Files section: two panels like Midnight Commander, this computer on the left,
the server on the right. F5 copies, F6 moves, F7 makes a folder, F8 deletes."""

from __future__ import annotations

import asyncio
import fnmatch
import threading
import time
from typing import Callable

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, Static

from ..credentials import Credential, Vault
from ..filesystems import (
    EDIT_LIMIT,
    VIEW_LIMIT,
    Cancelled,
    Entry,
    FileSystem,
    FsError,
    ShellFS,
    check_name,
    copy_items,
    is_inside,
    plan_copy,
    removable,
)
from ..formatting import human_bytes
from ..i18n import _, _n, localized
from .credentials import MANAGE
from .file_dialogs import SELF, AskText, Confirm, OverwriteScreen, ProgressScreen, RunAsScreen, ViewerScreen, hex_dump

UP = ".."
SEARCH_RESET = 1.5  # seconds without typing start a new quick search
SORTS = ("name", "size", "mtime")
# Key bar: (key shown, label, action of the section)
KEY_BAR = (
    ("F3", "View", "view"),
    ("F4", "Edit", "edit"),
    ("F5", "Copy", "copy"),
    ("F6", "Move", "move"),
    ("F2", "Rename", "rename"),
    ("F7", "Folder", "mkdir"),
    ("F8", "Delete", "delete"),
    ("F9", "Run as", "run_as"),
)


def _short_size(size: int) -> str:
    if size < 100_000:
        return str(size)
    return human_bytes(size, 1).replace(" ", "").replace("iB", "")


@localized
class FileTable(DataTable):
    """The list of one panel. Keys work like in mc; typing letters jumps to a name."""

    BINDINGS = [
        Binding("backspace", "back", "Up", show=False),
        Binding("left", "up_dir", "Up", show=False),
        Binding("right", "enter_dir", "Open", show=False),
        Binding("insert,space", "mark", "Mark", show=False),
        Binding("plus", "mask(True)", "Select", show=False),
        Binding("minus", "mask(False)", "Unselect", show=False),
        Binding("asterisk", "invert", "Invert", show=False),
        Binding("tab", "other", "Other panel", show=False),
        Binding("ctrl+r", "reload", "Refresh", show=False),
        Binding("ctrl+g", "goto", "Go to", show=False),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("f3", "op('view')", "View", show=False),
        Binding("f4", "op('edit')", "Edit", show=False),
        Binding("f5", "op('copy')", "Copy", show=False),
        Binding("f6", "op('move')", "Move", show=False),
        Binding("f2,shift+f6", "op('rename')", "Rename", show=False),
        Binding("f7", "op('mkdir')", "Folder", show=False),
        Binding("f8,delete", "op('delete')", "Delete", show=False),
        Binding("f9", "op('run_as')", "Run as", show=False),
    ]

    @property
    def panel(self) -> "FilePanel":
        return self.parent  # type: ignore[return-value]

    def on_key(self, event: events.Key) -> None:
        char = event.character
        if char and len(char) == 1 and (char.isalnum() or char in "._-~@#") and event.key not in ("minus",):
            event.stop()
            event.prevent_default()
            self.panel.search(char)

    def action_back(self) -> None:
        if self.panel.query_text:
            self.panel.search_back()
        else:
            self.panel.go_up()

    def action_up_dir(self) -> None:
        self.panel.go_up()

    def action_enter_dir(self) -> None:
        entry = self.panel.cursor_entry()
        if entry is not None and (entry.name == UP or entry.is_dir):
            self.panel.open_entry(entry)

    def action_mark(self) -> None:
        self.panel.toggle_mark()

    def action_mask(self, select: bool) -> None:
        self.panel.post_message(FilePanel.Operation(self.panel, "select" if select else "unselect"))

    def action_invert(self) -> None:
        self.panel.invert()

    def action_other(self) -> None:
        self.panel.post_message(FilePanel.Operation(self.panel, "other"))

    def action_reload(self) -> None:
        self.panel.reload()

    def action_goto(self) -> None:
        self.panel.post_message(FilePanel.Operation(self.panel, "goto"))

    def action_clear_search(self) -> None:
        if self.panel.query_text:
            self.panel.clear_search()
        elif self.panel.marked:
            self.panel.marked.clear()
            self.panel.render_rows()

    def action_op(self, name: str) -> None:
        self.panel.post_message(FilePanel.Operation(self.panel, name))


class FilePanel(Vertical):
    """One side: the folder path, its files and a status line."""

    DEFAULT_CSS = """
    FilePanel {
        width: 1fr;
        height: 1fr;
        border: round $panel;
        padding: 0;
    }
    FilePanel:focus-within { border: round $accent; }
    FilePanel .fp-head { height: 1; width: 1fr; }
    FilePanel .fp-path { width: 1fr; height: 1; padding: 0 1; color: $text-muted; }
    FilePanel:focus-within .fp-path { color: $text; background: $accent 30%; text-style: bold; }
    FilePanel .fp-user { width: auto; height: 1; padding: 0 1; }
    FilePanel FileTable { height: 1fr; width: 1fr; scrollbar-size-vertical: 1; }
    FilePanel FileTable > .datatable--cursor { background: $panel; }
    FilePanel FileTable:focus > .datatable--cursor { background: $accent 60%; }
    FilePanel .fp-status { height: 1; width: 1fr; padding: 0 1; color: $text-muted; border-top: none; }
    """

    class Operation(Message):
        def __init__(self, panel: "FilePanel", name: str) -> None:
            super().__init__()
            self.panel = panel
            self.name = name

    class Loaded(Message):
        def __init__(self, panel: "FilePanel") -> None:
            super().__init__()
            self.panel = panel

    def __init__(self, fs: FileSystem, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fs = fs
        self.path: str | None = None
        self.entries: list[Entry] = []
        self.rows: list[Entry] = []
        self.marked: set[str] = set()
        self.sort = "name"
        self.reverse = False
        self.query_text = ""
        self._typed_at = 0.0
        self.error = ""
        self.loading = False
        self.user = ""
        self._load_seq = 0
        self._columns_for: tuple | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="fp-head"):
            yield Static(classes="fp-path")
            yield Static(classes="fp-user")
        yield FileTable(cursor_type="row", zebra_stripes=False, show_cursor=True)
        yield Static(classes="fp-status")

    @property
    def table(self) -> FileTable:
        return self.query_one(FileTable)

    def focus_table(self) -> None:
        self.table.focus()

    # -- loading -------------------------------------------------------------

    async def load(self, path: str | None, select: str | None = None, keep_marks: bool = False) -> bool:
        """Show `path` ("" is home); the cursor goes to `select`. False when it cannot be opened."""
        self._load_seq += 1
        seq = self._load_seq
        self.loading = True
        self.render_status()
        fs = self.fs
        target = fs.home() if path is None else path

        def read():
            real, entries = fs.list(target)
            user = fs.user() if isinstance(fs, ShellFS) else ""
            return real, entries, user

        try:
            real, entries, user = await asyncio.to_thread(read)
        except FsError as exc:
            if seq == self._load_seq:
                self.loading = False
                if self.path is None:  # nothing shown yet (server unreachable)
                    self.error = str(exc)
                    self.render_rows()
                else:
                    self.render_status()
                self.app.notify(str(exc), title=_("Cannot open the folder"), severity="error", timeout=6)
            return False
        if seq != self._load_seq:
            return False
        changed = real != self.path
        self.path, self.entries, self.user = real, entries, user
        self.loading = False
        self.error = ""
        if changed and not keep_marks:
            self.marked.clear()
        else:
            self.marked &= {entry.name for entry in entries}
        current = self.cursor_entry()
        self.clear_search(render=False)
        self.render_rows(select or (current.name if current is not None and not changed else None))
        self.post_message(self.Loaded(self))
        return True

    @work(group="panel-load", exclusive=False)
    async def reload(self, select: str | None = None) -> None:
        if self.path is None:
            await self.load(None, select)
        else:
            await self.load(self.path, select, keep_marks=True)

    @work(group="panel-load", exclusive=False)
    async def go(self, path: str, select: str | None = None) -> None:
        await self.load(path, select)

    def go_up(self) -> None:
        if self.path is None:
            return
        parent = self.fs.parent(self.path)
        if parent is not None:
            self.go(parent, select=self.fs.basename(self.path) if self.path else None)

    def open_entry(self, entry: Entry) -> None:
        if entry.name == UP:
            self.go_up()
        elif entry.is_dir and self.path is not None:
            self.go(self.fs.join(self.path, entry.name))
        else:
            self.post_message(self.Operation(self, "view"))

    # -- rendering -------------------------------------------------------------

    def _sorted(self) -> list[Entry]:
        def key(entry: Entry):
            if self.sort == "size":
                value = entry.size
            elif self.sort == "mtime":
                value = entry.mtime
            else:
                value = entry.name.lower()
            return value

        folders = sorted((e for e in self.entries if e.is_dir), key=key, reverse=self.reverse)
        files = sorted((e for e in self.entries if not e.is_dir), key=key, reverse=self.reverse)
        return folders + files

    def _widths(self) -> tuple[int, int, int, int]:
        """Name, size, date and rights column widths for the panel's current width."""
        inner = max(self.size.width - 2, 20)
        size_w, date_w = 7, 16
        rights_w = 18 if inner >= 66 else 0
        cells = 3 + (1 if rights_w else 0)
        name_w = inner - size_w - date_w - rights_w - cells * 2 - 1
        if name_w < 14:  # very narrow: no date
            date_w = 0
            name_w = inner - size_w - 2 * 2 - 1
        return max(name_w, 8), size_w, date_w, rights_w

    def _ensure_columns(self) -> tuple[int, int, int, int]:
        widths = self._widths()
        marker = {"name": "", "size": "", "mtime": ""}
        marker[self.sort] = " ↓" if self.reverse else " ↑"
        key = (widths, self.sort, self.reverse)
        if key != self._columns_for:
            self._columns_for = key
            table = self.table
            table.clear(columns=True)
            name_w, size_w, date_w, rights_w = widths
            table.add_column(_("Name") + marker["name"], width=name_w, key="name")
            table.add_column(_("Size") + marker["size"], width=size_w, key="size")
            if date_w:
                table.add_column(_("Modified") + marker["mtime"], width=date_w, key="mtime")
            if rights_w:
                table.add_column(_("Rights"), width=rights_w, key="rights")
        return widths

    def _name_cell(self, entry: Entry, width: int) -> Text:
        if entry.name == UP:
            return Text("/..", style="bold")
        if entry.kind == "dir":
            label, style = "/" + entry.name, "bold"
        elif entry.link_dir:
            label, style = "~" + entry.name, "bold cyan"
        elif entry.kind == "link":
            label, style = "@" + entry.name, "cyan"
        elif entry.executable:
            label, style = "*" + entry.name, "green"
        elif entry.kind == "other":
            label, style = "=" + entry.name, "magenta"
        else:
            label, style = " " + entry.name, ""
        if entry.name.startswith(".") and not style:
            style = "grey62"
        if entry.name in self.marked:
            style = "bold yellow"
        if len(label) > width:
            half = (width - 1) // 2
            label = label[:half] + "~" + label[-(width - 1 - half):]
        return Text(label, style=style, no_wrap=True, overflow="ellipsis")

    def render_rows(self, select: str | None = None) -> None:
        if not self.is_mounted:
            return
        table = self.table
        cursor = self.cursor_entry()  # before the columns are rebuilt (that resets the cursor)
        wanted = select if select is not None else (cursor.name if cursor is not None else None)
        name_w, size_w, date_w, rights_w = self._ensure_columns()
        table.clear()
        rows: list[Entry] = []
        if self.path is not None and self.fs.parent(self.path) is not None:
            rows.append(Entry(UP, "dir"))
        rows += self._sorted()
        self.rows = rows
        for entry in rows:
            marked = entry.name in self.marked
            style = "bold yellow" if marked else ""
            if entry.name == UP:
                size = Text(_("UP"), style="grey50", justify="right")
            elif entry.is_dir:
                size = Text(_("DIR"), style=style or "grey62", justify="right")
            else:
                size = Text(_short_size(entry.size), style=style, justify="right")
            cells = [self._name_cell(entry, name_w), size]
            if date_w:
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime)) if entry.mtime else ""
                cells.append(Text(stamp, style=style or "grey62"))
            if rights_w:
                rights = f"{entry.mode:<9} {entry.owner}".strip() if entry.mode else ""
                cells.append(Text(rights, style=style or "grey50", no_wrap=True, overflow="ellipsis"))
            table.add_row(*cells)
        if wanted is not None:
            for index, entry in enumerate(rows):
                if entry.name == wanted:
                    table.move_cursor(row=index, animate=False)
                    break
        self.render_head()
        self.render_status()

    def render_head(self) -> None:
        if not self.is_mounted:
            return
        path = self.path if self.path is not None else "…"
        if self.path == "":
            path = _("Drives")
        width = max(self.size.width - 4 - (len(self.user) + 4 if self.user else 0), 10)
        if len(path) > width:
            path = "…" + path[-(width - 1):]
        self.query_one(".fp-path", Static).update(Text(path, no_wrap=True))
        user = self.query_one(".fp-user", Static)
        if isinstance(self.fs, ShellFS) and self.user:
            root = self.user == "root"
            user.update(Text.assemble(
                (f" {self.user}@{self.fs.title} ", "bold white on red" if root else
                 ("bold black on yellow" if self.fs.run_as else "bold"))))
        else:
            user.update(Text(self.fs.title, style="grey50"))

    def render_status(self) -> None:
        if not self.is_mounted:
            return
        status = self.query_one(".fp-status", Static)
        if self.loading:
            status.update(Text(_("Loading…"), style="bold"))
            return
        if self.error and self.path is None:
            status.update(Text(self.error + " · " + _("Ctrl+R: try again"), style="red", no_wrap=True,
                               overflow="ellipsis"))
            return
        if self.query_text:
            status.update(Text.assemble((_("Search: "), "bold"), (self.query_text, "bold reverse"),
                                        ("  " + _("Esc: clear"), "grey50")))
            return
        if self.marked:
            size = sum(e.size for e in self.entries if e.name in self.marked and not e.is_dir)
            text = _n("{n} marked", "{n} marked", len(self.marked)) + (f" · {human_bytes(size)}" if size else "")
            status.update(Text(text, style="bold yellow"))
            return
        entry = self.cursor_entry()
        folders = sum(1 for e in self.entries if e.is_dir)
        files = len(self.entries) - folders
        summary = _n("{n} folder", "{n} folders", folders) + ", " + _n("{n} file", "{n} files", files)
        if entry is None or entry.name == UP:
            status.update(Text(summary, no_wrap=True))
            return
        detail = entry.name
        if not entry.is_dir:
            detail += f" · {human_bytes(entry.size)}"
        status.update(Text.assemble((detail, "bold"), ("  " + summary, "grey50"), no_wrap=True,
                                    overflow="ellipsis"))

    def on_resize(self, _event: events.Resize) -> None:
        if self.path is not None or self.error:
            self.render_rows()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self.render_status()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        entry = self.cursor_entry()
        if entry is not None:
            self.open_entry(entry)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        event.stop()
        key = event.column_key.value
        if key not in SORTS:
            return
        if self.sort == key:
            self.reverse = not self.reverse
        else:
            self.sort, self.reverse = key, key != "name"  # biggest / newest first
        self.render_rows()

    # -- cursor, marks, search -------------------------------------------------------

    def cursor_entry(self) -> Entry | None:
        if not self.is_mounted:
            return None
        row = self.table.cursor_row
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def selected_names(self) -> list[str]:
        """Marked names, or the one under the cursor."""
        if self.marked:
            return [e.name for e in self._sorted() if e.name in self.marked]
        entry = self.cursor_entry()
        return [] if entry is None or entry.name == UP or self.path == "" else [entry.name]

    def toggle_mark(self) -> None:
        entry = self.cursor_entry()
        if entry is not None and entry.name != UP and self.path != "":
            self.marked ^= {entry.name}
            row = self.table.cursor_row
            self.render_rows(entry.name)
            self.table.move_cursor(row=min(row + 1, len(self.rows) - 1), animate=False)

    def mark_mask(self, mask: str, select: bool) -> int:
        masks = [m.strip() for m in mask.split(",") if m.strip()] or ["*"]
        names = {e.name for e in self.entries if not e.is_dir
                 and any(fnmatch.fnmatch(e.name.lower(), m.lower()) for m in masks)}
        before = len(self.marked)
        self.marked = self.marked | names if select else self.marked - names
        self.render_rows()
        return abs(len(self.marked) - before)

    def invert(self) -> None:
        files = {e.name for e in self.entries if not e.is_dir}
        self.marked = (files - self.marked) | {n for n in self.marked if n not in files}
        self.render_rows()

    def search(self, char: str) -> None:
        now = time.monotonic()
        if now - self._typed_at > SEARCH_RESET:
            self.query_text = ""
        self._typed_at = now
        self.query_text += char
        if not self._jump():
            self.query_text = self.query_text[:-1] or ""
            self.app.bell()
        self.render_status()

    def search_back(self) -> None:
        self.query_text = self.query_text[:-1]
        self._typed_at = time.monotonic()
        self._jump()
        self.render_status()

    def clear_search(self, render: bool = True) -> None:
        self.query_text = ""
        if render:
            self.render_status()

    def _jump(self) -> bool:
        needle = self.query_text.lower()
        if not needle:
            return True
        for test in (lambda name: name.startswith(needle), lambda name: needle in name):
            for index, entry in enumerate(self.rows):
                if entry.name != UP and test(entry.name.lower()):
                    self.table.move_cursor(row=index, animate=False)
                    return True
        return False


class FilesSection(Vertical):
    DEFAULT_CSS = """
    FilesSection { width: 1fr; height: 1fr; }
    FilesSection #fm-panels { width: 1fr; height: 1fr; }
    FilesSection #fm-bar { height: 1; width: 1fr; }
    FilesSection #fm-bar Button {
        width: auto; min-width: 0; height: 1; margin: 0 1 0 0; padding: 0 1 0 0;
        border: none; background: $panel;
    }
    FilesSection #fm-bar Button:hover { background: $accent 40%; }
    """

    def __init__(self, local: FileSystem, remote: FileSystem | None, remote_error: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.local_fs = local
        self.remote_fs = remote
        self.remote_error = remote_error
        self.active_side = "left"

    def compose(self) -> ComposeResult:
        with Horizontal(id="fm-panels"):
            yield FilePanel(self.local_fs, id="fm-left")
            if self.remote_fs is not None:
                yield FilePanel(self.remote_fs, id="fm-right")
            else:
                yield Static(Text(self.remote_error, style="red"), id="fm-right-error")
        with Horizontal(id="fm-bar"):
            for key, label, action in KEY_BAR:
                button = Button(Text.assemble((f" {key} ", "bold reverse"), " ", _(label)), id=f"fm-{action}",
                                compact=True)
                button.can_focus = False
                yield button

    async def on_mount(self) -> None:
        loads = [self.panel("left").load(None)]
        if self.remote_fs is not None:
            loads.append(self.panel("right").load(None))
        await asyncio.gather(*loads)

    # -- panels ----------------------------------------------------------------

    def panel(self, side: str) -> FilePanel:
        return self.query_one(f"#fm-{side}", FilePanel)

    @property
    def active(self) -> FilePanel:
        if self.remote_fs is None:
            return self.panel("left")
        return self.panel(self.active_side)

    @property
    def passive(self) -> FilePanel | None:
        if self.remote_fs is None:
            return None
        return self.panel("right" if self.active_side == "left" else "left")

    def focus_active(self) -> None:
        self.active.focus_table()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        for panel in self.query(FilePanel):
            if event.widget in panel.query(FileTable):
                self.active_side = "left" if panel.id == "fm-left" else "right"

    @property
    def vault(self) -> Vault:
        vault = getattr(self.app, "vault", None)
        if vault is None:
            vault = self.app.vault = Vault()
        vault.ensure_loaded()
        return vault

    # -- commands ----------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("fm-"):
            event.stop()
            self.run_operation(self.active, button_id[3:])

    def on_file_panel_operation(self, event: FilePanel.Operation) -> None:
        event.stop()
        self.run_operation(event.panel, event.name)

    def run_operation(self, panel: FilePanel, name: str) -> None:
        if name == "other":
            other = self.passive if panel is self.active else self.active
            if other is not None:
                other.focus_table()
            return
        if name == "run_as":
            self.run_as()
            return
        self.operation(panel, name)

    @work(exclusive=True, group="files-op")
    async def operation(self, panel: FilePanel, name: str) -> None:
        try:
            if name == "view":
                await self._view(panel, edit=False)
            elif name == "edit":
                await self._view(panel, edit=True)
            elif name in ("copy", "move"):
                await self._transfer(panel, move=name == "move")
            elif name == "rename":
                await self._rename(panel)
            elif name == "mkdir":
                await self._mkdir(panel)
            elif name == "delete":
                await self._delete(panel)
            elif name in ("select", "unselect"):
                await self._mask(panel, name == "select")
            elif name == "goto":
                await self._goto(panel)
        finally:
            try:
                if panel.is_mounted and self.app.screen is self.screen:
                    panel.focus_table()
            except Exception:  # noqa: BLE001 - the app is closing (no screen any more)
                pass

    def _fail(self, error: object, title: str | None = None) -> None:
        self.app.notify(str(error), title=title or _("Files"), severity="error", timeout=8)

    async def _view(self, panel: FilePanel, edit: bool) -> None:
        entry = panel.cursor_entry()
        if entry is None or panel.path is None:
            return
        if entry.name == UP or entry.is_dir:
            panel.open_entry(entry)
            return
        if entry.kind == "other":
            self._fail(_("“{name}” is not a regular file", name=entry.name))
            return
        fs, path = panel.fs, panel.fs.join(panel.path, entry.name)
        limit = EDIT_LIMIT + 1 if edit else VIEW_LIMIT
        if edit and entry.size > EDIT_LIMIT:
            self._fail(_("The file is too big to edit here ({size})", size=human_bytes(entry.size)))
            return
        try:
            data = await asyncio.to_thread(fs.read_head, path, limit)
        except FsError as exc:
            self._fail(exc, _("Cannot read the file"))
            return
        binary = b"\x00" in data[:8192]
        title = f"{fs.title}: {path}" if fs.remote else path
        if edit:
            if binary:
                self._fail(_("This is a binary file: it cannot be edited as text"))
                return
            text = data.decode("utf-8", errors="surrogateescape")

            async def save(new_text: str) -> str | None:
                try:
                    await asyncio.to_thread(fs.write_file, path, new_text.encode("utf-8", errors="surrogateescape"))
                except FsError as exc:
                    return str(exc)
                panel.reload(select=entry.name)
                return None

            await self.app.push_screen_wait(ViewerScreen(title, text, _("Editing"), save=save))
            return
        if binary:
            text, note = hex_dump(data), _("binary file, hex view")
        else:
            text, note = data.decode("utf-8", errors="replace"), ""
        if entry.size > len(data):
            note = " · ".join(filter(None, [note, _("first {size} shown", size=human_bytes(len(data)))]))
        await self.app.push_screen_wait(ViewerScreen(title, text, note))

    async def _transfer(self, panel: FilePanel, move: bool) -> None:
        source = panel
        target = self.passive if panel is self.active else self.active
        if target is None or target is source or source.path is None or target.path is None:
            self._fail(_("The other panel is not ready"))
            return
        names = source.selected_names()
        if not names:
            self.app.bell()
            return
        what = f"“{names[0]}”" if len(names) == 1 else _n("{n} item", "{n} items", len(names))
        verb = _("Move") if move else _("Copy")
        prompt = (_("Move {what} to:", what=what) if move else _("Copy {what} to:", what=what))
        prompt += "  " + (f"({target.fs.title})" if target.fs.remote else f"({_('this computer')})")
        destination = await self.app.push_screen_wait(AskText(verb, prompt, target.path, ok=verb))
        if destination is None:
            return
        destination = destination.strip() or target.path
        same_fs = source.fs is target.fs
        if same_fs and destination.rstrip("/\\") == source.path.rstrip("/\\"):
            self._fail(_("Cannot copy a file onto itself"))
            return
        if same_fs and any(is_inside(source.fs, source.path, name, destination) for name in names):
            self._fail(_("Cannot copy a folder into itself"))
            return

        cancel = threading.Event()
        progress = ProgressScreen(f"{verb}: {what} → {destination}", cancel)
        await self.app.push_screen(progress)
        cancelled = False
        errors: list[str] = []
        skip: set[str] = set()
        copied = 0
        try:
            if move and same_fs:
                progress.phase(_("Moving…"))
                for name in names:
                    try:
                        await asyncio.to_thread(source.fs.rename, source.fs.join(source.path, name),
                                                source.fs.join(destination, name))
                        copied += 1
                    except FsError as exc:
                        errors.append(f"{name}: {exc}")
            else:
                try:
                    items, conflicts, problems, blocked = await asyncio.to_thread(
                        plan_copy, source.fs, source.path, names, target.fs, destination)
                except FsError as exc:
                    self._fail(exc, _("Cannot prepare the copy"))
                    return
                errors += problems
                skip |= blocked
                decision = ""
                for number, conflict in enumerate(conflicts, 1):
                    rel = conflict.source.rel
                    if decision == "all":
                        continue
                    if decision == "none" or (decision == "newer" and conflict.source.mtime <= conflict.target.mtime):
                        skip.add(rel)
                        continue
                    if decision == "newer":
                        continue
                    answer = await self.app.push_screen_wait(
                        OverwriteScreen(conflict, number, len(conflicts), f"→ {destination}"))
                    if answer == "cancel":
                        cancelled = True
                        return
                    if answer == "no":
                        skip.add(rel)
                    elif answer in ("all", "none", "newer"):
                        decision = answer
                        if answer == "none" or (answer == "newer" and conflict.source.mtime <= conflict.target.mtime):
                            skip.add(rel)
                chosen = [item for item in items if item.rel not in skip]
                files = [item for item in chosen if item.kind != "dir"]
                if not chosen:
                    self.app.notify(_("Nothing to copy: every file was skipped"), timeout=4)
                    return
                total = sum(item.size for item in files)
                progress.start(total, len(files))
                state = {"done": 0, "shown": 0.0}

                def tick(count: int, name: str) -> None:
                    state["done"] += count
                    now = time.monotonic()
                    if now - state["shown"] >= 0.1:
                        state["shown"] = now
                        self.app.call_from_thread(progress.advance, state["done"], name)

                try:
                    errors += await asyncio.to_thread(
                        copy_items, source.fs, source.path, names, items, target.fs, destination, skip, tick, cancel)
                except Cancelled:
                    cancelled = True
                except FsError as exc:
                    if cancel.is_set():
                        cancelled = True
                    else:
                        errors.append(str(exc))
                if not cancelled and not cancel.is_set():
                    progress.advance(total, "")
                    copied = len(files)
                    if move and not errors:
                        progress.phase(_("Deleting the originals…"))
                        try:
                            await asyncio.to_thread(source.fs.remove, source.path, removable(names, skip))
                        except FsError as exc:
                            errors.append(str(exc))
                elif cancel.is_set():
                    cancelled = True
        finally:
            if progress.is_attached:
                progress.dismiss()
            if not cancelled and not errors:
                source.marked.clear()
            source.reload()
            if target is not source:
                target.reload(select=names[0] if destination == target.path else None)
        if cancelled:
            self.app.notify(_("Stopped. Files copied so far stay where they are."), severity="warning", timeout=6)
        elif errors:
            more = len(errors) - 6
            text = "\n".join(errors[:6]) + ("\n" + _n("…and {n} more", "…and {n} more", more) if more > 0 else "")
            self.app.notify(text, title=_("Done with problems"), severity="warning", timeout=12)
        else:
            done = _("Moved") if move else _("Copied")
            skipped = len(skip)
            message = f"{done}: " + _n("{n} file", "{n} files", copied)
            if skipped:
                message += " · " + _n("{n} skipped", "{n} skipped", skipped)
            self.app.notify(message, timeout=4)

    async def _rename(self, panel: FilePanel) -> None:
        entry = panel.cursor_entry()
        if entry is None or entry.name == UP or panel.path is None or panel.path == "":
            return
        stem = entry.name.rfind(".")
        select = (0, stem) if stem > 0 and not entry.is_dir else (0, len(entry.name))
        name = await self.app.push_screen_wait(
            AskText(_("Rename"), _("New name for “{name}”:", name=entry.name), entry.name, ok=_("Rename"),
                    select=select))
        if name is None or name.strip() == entry.name:
            return
        try:
            new = check_name(name)
            await asyncio.to_thread(panel.fs.rename, panel.fs.join(panel.path, entry.name),
                                    panel.fs.join(panel.path, new))
        except FsError as exc:
            self._fail(exc, _("Cannot rename"))
            return
        panel.marked.discard(entry.name)
        await panel.load(panel.path, select=new, keep_marks=True)

    async def _mkdir(self, panel: FilePanel) -> None:
        if panel.path is None or panel.path == "":
            return
        name = await self.app.push_screen_wait(
            AskText(_("New folder"), _("Folder name (a/b/c creates nested folders):"), ok=_("Create")))
        if not name or not name.strip():
            return
        name = name.strip().strip("/")
        try:
            parts = [check_name(part) for part in name.split("/")]
            await asyncio.to_thread(panel.fs.mkdir, panel.fs.join(panel.path, "/".join(parts)))
        except FsError as exc:
            self._fail(exc, _("Cannot create the folder"))
            return
        await panel.load(panel.path, select=parts[0], keep_marks=True)

    async def _delete(self, panel: FilePanel) -> None:
        names = panel.selected_names()
        if not names or panel.path is None:
            self.app.bell()
            return
        entries = {e.name: e for e in panel.entries}
        folders = sum(1 for n in names if n in entries and entries[n].is_dir)
        body = Text()
        listed = names[:8]
        for name in listed:
            body.append(("/" if name in entries and entries[name].is_dir else " ") + name + "\n",
                        "bold" if name in entries and entries[name].is_dir else "")
        if len(names) > len(listed):
            body.append(_n("…and {n} more", "…and {n} more", len(names) - len(listed)) + "\n", "grey50")
        if folders:
            body.append("\n" + _("Folders are deleted with everything inside."), "bold red")
        where = f"{panel.fs.title}: {panel.path}" if panel.fs.remote else panel.path
        body.append("\n" + where, "grey50")
        if panel.fs.run_as:
            body.append("\n" + _("As user {user}", user=panel.fs.run_as), "bold red")
        title = _n("Delete {n} item?", "Delete {n} items?", len(names))
        if not await self.app.push_screen_wait(Confirm(title, body, _("Delete"))):
            return
        panel.loading = True
        panel.render_status()
        try:
            await asyncio.to_thread(panel.fs.remove, panel.path, names)
        except FsError as exc:
            self._fail(exc, _("Cannot delete"))
        panel.marked -= set(names)
        cursor = panel.cursor_entry()
        rows = [e.name for e in panel.rows]
        follow = None
        if cursor is not None and cursor.name in names:
            index = rows.index(cursor.name)
            rest = [n for n in rows[index:] + rows[:index][::-1] if n not in names]
            follow = rest[0] if rest else None
        await panel.load(panel.path, select=follow, keep_marks=True)
        self.app.notify(_n("Deleted {n} item", "Deleted {n} items", len(names)), timeout=3)

    async def _mask(self, panel: FilePanel, select: bool) -> None:
        mask = await self.app.push_screen_wait(AskText(
            _("Select files") if select else _("Unselect files"),
            _("Mask, e.g. *.log or *.jpg,*.png:"), "*", ok=_("OK")))
        if mask is not None:
            panel.mark_mask(mask, select)

    async def _goto(self, panel: FilePanel) -> None:
        path = await self.app.push_screen_wait(
            AskText(_("Go to folder"), _("Path:"), panel.path or "", ok=_("Open")))
        if path and path.strip():
            await panel.load(path.strip())

    # -- run as another user -------------------------------------------------------------

    @work(exclusive=True, group="files-op")
    async def run_as(self) -> None:
        if self.remote_fs is None or not isinstance(self.remote_fs, ShellFS):
            self._fail(_("Run as another user works for servers connected over SSH"))
            return
        fs = self.remote_fs
        panel = self.panel("right")
        login = await asyncio.to_thread(fs.login)
        choice = await self.app.push_screen_wait(RunAsScreen(self.vault, login, fs.run_as))
        if choice == MANAGE:
            self.app.open_credentials()
            return
        if choice is None:
            panel.focus_table()
            return
        previous = fs.su
        if choice == SELF:
            fs.switch_user(None)
        elif isinstance(choice, Credential):
            user = choice.login.strip() or "root"
            fs.switch_user(user, choice.password)
            panel.loading = True
            panel.render_status()
            self.app.notify(_("Switching to {user}…", user=user), timeout=2)
        path = panel.path or ""
        try:
            try:
                await asyncio.to_thread(fs.list, path)
            except FsError:
                if not path:
                    raise
                path = ""  # that user cannot open this folder: its home instead
                await asyncio.to_thread(fs.list, path)
        except FsError as exc:
            fs.su = previous
            panel.loading = False
            panel.render_status()
            self._fail(exc, _("Cannot open the files as another user"))
            panel.focus_table()
            return
        await panel.load(path)
        panel.focus_table()
