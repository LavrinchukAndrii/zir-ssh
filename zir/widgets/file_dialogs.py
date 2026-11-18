"""Dialogs of the Files section: names, confirmations, overwrite questions, progress, viewer."""

from __future__ import annotations

import threading
import time
from typing import Awaitable, Callable

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, ProgressBar, Static, TextArea
from textual.widgets.option_list import Option

from ..credentials import Credential, Vault
from ..filesystems import Conflict
from ..formatting import human_bytes
from ..i18n import _, _n, localized
from .credentials import MANAGE

SELF = "self"  # RunAsScreen result: back to the SSH user

DIALOG_CSS = """
{name} {{ align: center middle; background: $background 50%; }}
{name} > Vertical {{
    width: {width};
    max-width: 95%;
    height: auto;
    max-height: 90%;
    border: round {border};
    background: $surface;
    padding: 1 2;
}}
{name} .dlg-title {{ text-style: bold; height: auto; margin-bottom: 1; }}
{name} .dlg-text {{ height: auto; }}
{name} .dlg-error {{ height: auto; color: $error; }}
{name} .dlg-buttons {{ height: auto; margin-top: 1; }}
{name} .dlg-buttons Button {{ margin-right: 1; }}
"""


def _css(name: str, width: int | str = 70, border: str = "$accent") -> str:
    return DIALOG_CSS.format(name=name, width=width, border=border)


def when(mtime: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""


@localized
class AskText(ModalScreen[str | None]):
    """One line of text: a name, a path or a mask."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _css("AskText")

    def __init__(self, title: str, prompt: str, value: str = "", ok: str | None = None,
                 select: tuple[int, int] | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.prompt = prompt
        self.value = value
        self.ok = ok or _("OK")
        self.select = select

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="dlg-title")
            yield Static(self.prompt, classes="dlg-text")
            yield Input(self.value, id="ask-input")
            with Horizontal(classes="dlg-buttons"):
                yield Button(self.ok, variant="primary", id="ask-ok")
                yield Button(_("Cancel"), id="ask-cancel")

    def on_mount(self) -> None:
        field = self.query_one("#ask-input", Input)
        field.focus()
        if self.select is not None:  # e.g. the name without its extension
            field.selection = self.select  # type: ignore[assignment]

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(self.query_one("#ask-input", Input).value if event.button.id == "ask-ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


@localized
class Confirm(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "Yes", show=False),
        Binding("n,escape", "answer(False)", "No"),
    ]
    DEFAULT_CSS = _css("Confirm", border="$error")

    def __init__(self, title: str, body: Text | str, yes: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.yes = yes

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="dlg-title")
            yield Static(self.body, classes="dlg-text")
            with Horizontal(classes="dlg-buttons"):
                yield Button(self.yes, variant="error", id="confirm-yes")
                yield Button(_("Cancel"), id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()  # Enter does not delete by accident

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


OVERWRITE_ANSWERS = (
    # id, label, key
    ("yes", "Overwrite", "y"),
    ("no", "Skip", "n"),
    ("all", "Overwrite all", "a"),
    ("newer", "All older ones", "o"),
    ("none", "Skip all", "s"),
)


@localized
class OverwriteScreen(ModalScreen[str]):
    """A file exists at the target: overwrite, skip, or decide for all of them at once."""

    BINDINGS = [
        *(Binding(key, f"answer('{answer}')", show=False) for answer, _label, key in OVERWRITE_ANSWERS),
        Binding("escape", "answer('cancel')", "Cancel"),
    ]
    DEFAULT_CSS = _css("OverwriteScreen", width=84, border="$warning") + """
    OverwriteScreen .dlg-buttons { height: auto; }
    OverwriteScreen #ow-hint { color: $text-muted; height: auto; margin-top: 1; }
    """

    def __init__(self, conflict: Conflict, number: int, total: int, target_title: str) -> None:
        super().__init__()
        self.conflict = conflict
        self.number = number
        self.total = total
        self.target_title = target_title

    def compose(self) -> ComposeResult:
        source, target = self.conflict.source, self.conflict.target
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(style="bold")
        table.add_column(justify="right")
        table.add_column()
        newer = source.mtime > target.mtime
        table.add_row(_("New:"), human_bytes(source.size), Text(when(source.mtime), "green" if newer else ""))
        table.add_row(_("Existing:"), human_bytes(target.size), Text(when(target.mtime), "" if newer else "green"))
        heading = Text.assemble(
            (_("The file already exists"), "bold"),
            ("  " + _("{number} of {total}", number=self.number, total=self.total), "grey50")
            if self.total > 1 else "",
            "\n", (self.conflict.source.rel, "bold cyan"), ("  " + self.target_title, "grey50"),
        )
        with Vertical():
            yield Static(heading, classes="dlg-title")
            yield Static(table, classes="dlg-text")
            with Horizontal(classes="dlg-buttons"):
                for answer, label, _key in OVERWRITE_ANSWERS:
                    yield Button(_(label), id=f"ow-{answer}", variant="warning" if answer == "yes" else "default")
                yield Button(_("Cancel"), id="ow-cancel", variant="error")
            yield Static(_("y overwrite · n skip · a overwrite all · o overwrite all older · s skip all · Esc cancel"),
                         id="ow-hint")

    def on_mount(self) -> None:
        self.query_one("#ow-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss((event.button.id or "ow-cancel").removeprefix("ow-"))

    def action_answer(self, answer: str) -> None:
        self.dismiss(answer)


@localized
class ProgressScreen(ModalScreen[None]):
    """Copying in progress; Cancel stops it after the current block."""

    BINDINGS = [Binding("escape", "stop", "Cancel")]
    DEFAULT_CSS = _css("ProgressScreen", width=80) + """
    ProgressScreen ProgressBar { margin: 1 0 0 0; }
    ProgressScreen #pg-file { height: 1; color: $text-muted; }
    ProgressScreen #pg-stats { height: 1; margin-top: 1; }
    """

    def __init__(self, title: str, cancel: threading.Event) -> None:
        super().__init__()
        self.title_text = title
        self.cancel = cancel
        self.total = 0
        self.done = 0
        self.started = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="dlg-title", id="pg-title")
            yield Static(_("Preparing…"), id="pg-file")
            yield ProgressBar(total=None, show_eta=True, id="pg-bar")
            yield Static("", id="pg-stats")
            with Horizontal(classes="dlg-buttons"):
                yield Button(_("Cancel"), variant="error", id="pg-cancel")

    def phase(self, text: str) -> None:
        self.query_one("#pg-file", Static).update(text)

    def start(self, total: int, files: int) -> None:
        self.total = total
        self.done = 0
        self.started = time.monotonic()
        self.query_one("#pg-bar", ProgressBar).update(total=max(total, 1), progress=0)
        self.query_one("#pg-stats", Static).update(
            _n("{n} file", "{n} files", files) + " · " + human_bytes(total))

    def advance(self, done: int, name: str) -> None:
        self.done = done
        self.query_one("#pg-file", Static).update(Text(name, overflow="ellipsis", no_wrap=True))
        self.query_one("#pg-bar", ProgressBar).update(progress=done)
        seconds = max(time.monotonic() - self.started, 0.001)
        self.query_one("#pg-stats", Static).update(
            f"{human_bytes(done)} / {human_bytes(self.total)} · {human_bytes(done / seconds)}/s")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_stop()

    def action_stop(self) -> None:
        self.cancel.set()
        self.query_one("#pg-file", Static).update(Text(_("Stopping…"), style="bold yellow"))
        self.query_one("#pg-cancel", Button).disabled = True


def hex_dump(data: bytes, limit: int = 64 * 1024) -> str:
    lines = []
    for offset in range(0, min(len(data), limit), 16):
        chunk = data[offset:offset + 16]
        hexes = " ".join(f"{byte:02x}" for byte in chunk)
        text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset:08x}  {hexes:<47}  {text}")
    return "\n".join(lines)


@localized
class ViewerScreen(ModalScreen[None]):
    """F3 (read only) and F4 (edit, Ctrl+S saves)."""

    BINDINGS = [
        # Priority: in "indent" mode the TextArea itself uses Escape to release focus.
        Binding("escape", "close", "Close", priority=True),
        Binding("f3,f10", "close", "Close", show=False, priority=True),
        Binding("ctrl+s,f2", "save", "Save", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    ViewerScreen { align: center middle; background: $background 50%; }
    ViewerScreen > Vertical {
        width: 96%;
        height: 94%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    ViewerScreen #vw-title { height: 1; text-style: bold; }
    ViewerScreen TextArea { height: 1fr; border: none; padding: 0; }
    ViewerScreen #vw-status { height: 1; color: $text-muted; }
    """

    def __init__(self, title: str, text: str, note: str = "",
                 save: Callable[[str], Awaitable[str | None]] | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.text = text
        self.note = note
        self.saver = save
        self.saved_text = text
        self._discard = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.title_text, overflow="ellipsis", no_wrap=True), id="vw-title")
            yield TextArea(self.text, read_only=self.saver is None, show_line_numbers=True, soft_wrap=False,
                           tab_behavior="indent", id="vw-text")
            yield Static(self._hint(), id="vw-status")

    def _hint(self) -> str:
        if self.saver is None:
            return " · ".join(filter(None, [self.note, _("Esc / F3: close")]))
        return " · ".join(filter(None, [self.note, _("Ctrl+S: save · Esc: close")]))

    def on_mount(self) -> None:
        area = self.query_one(TextArea)
        area.focus()
        area.move_cursor((0, 0))

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._discard = False

    async def action_save(self) -> None:
        if self.saver is None:
            return
        text = self.query_one(TextArea).text
        status = self.query_one("#vw-status", Static)
        status.update(Text(_("Saving…"), style="bold"))
        error = await self.saver(text)
        if error:
            status.update(Text(_("Not saved: {error}", error=error), style="bold red"))
            return
        self.saved_text = text
        status.update(Text(_("Saved") + " · " + self._hint(), style="green"))

    def action_close(self) -> None:
        if self.saver is not None and self.query_one(TextArea).text != self.saved_text and not self._discard:
            self._discard = True
            self.query_one("#vw-status", Static).update(
                Text(_("There are unsaved changes: Ctrl+S saves, Esc again closes without saving"), style="bold yellow"))
            return
        self.dismiss(None)


@localized
class RunAsScreen(ModalScreen[Credential | str | None]):
    """F9: open the server panel as another user (su - <login of the card>)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _css("RunAsScreen", width=64) + """
    RunAsScreen OptionList { height: auto; max-height: 16; border: none; padding: 0; }
    RunAsScreen #ra-hint { height: auto; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, vault: Vault, login: str, current: str | None) -> None:
        super().__init__()
        self.vault = vault
        self.login = login
        self.current = current

    def compose(self) -> ComposeResult:
        options = []
        if self.current:
            options.append(Option(Text.assemble(
                ("← ", "bold"), _("Back to the SSH user"), (f"  {self.login}" if self.login else "", "grey50")),
                id=SELF))
        for card in self.vault.cards:
            user = card.login.strip() or "root"
            label = Text(card.name or "—", style="bold")
            label.append(f"  su - {user}", style="red" if user == "root" else "cyan")
            options.append(Option(label, id=card.id))
        options.append(Option(Text(_("Manage identity cards… (F6)"), style="italic"), id=MANAGE))
        with Vertical():
            yield Static(_("Open the server files as another user"), classes="dlg-title")
            if self.vault.error:
                yield Static(self.vault.error, classes="dlg-error")
            yield OptionList(*options)
            yield Static(_("The card's login is the user (root if it is empty), its password is given to su. "
                           "Changes are then made with that user's rights."), id="ra-hint")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id in (SELF, MANAGE):
            self.dismiss(event.option.id)
            return
        self.dismiss(next((c for c in self.vault.cards if c.id == event.option.id), None))

    def action_cancel(self) -> None:
        self.dismiss(None)
