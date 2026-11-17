"""Ctrl+] in the terminal: zir's own commands, so every other key can go to the shell / mc."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..i18n import _, localized

# (result, key shown and pressed in the menu, Textual key names, label)
ITEMS = (
    ("snippets", "s", ("s",), "Insert a snippet…"),
    ("passwords", "p", ("p",), "Insert a password…"),
    ("new", "n", ("n",), "New terminal session"),
    ("close", "w", ("w",), "Close this terminal session"),
    ("section-1", "←", ("left",), "Previous section of the host"),
    ("section+1", "→", ("right",), "Next section of the host"),
    ("tab-1", "[", ("left_square_bracket",), "Previous server tab"),
    ("tab+1", "]", ("right_square_bracket",), "Next server tab"),
    ("switch", "k", ("k",), "Find a server…"),
    ("send", "Ctrl+]", ("ctrl+right_square_bracket",), "Send Ctrl+] to the terminal"),
)


@localized
class TerminalMenu(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    TerminalMenu { align: center middle; background: $background 40%; }
    TerminalMenu > Vertical {
        width: 52;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    TerminalMenu .title { text-style: bold; height: 1; margin-bottom: 1; }
    TerminalMenu OptionList { height: auto; border: none; padding: 0; }
    TerminalMenu #tmenu-hint { height: auto; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, local: bool = False) -> None:
        super().__init__()
        self.local = local  # the Local tab has no host sections

    def _items(self):
        skip = {"section-1", "section+1"} if self.local else set()
        return [item for item in ITEMS if item[0] not in skip]

    def compose(self) -> ComposeResult:
        column = max(len(key) for _result, key, _keys, _label in ITEMS) + 2
        with Vertical():
            yield Static(_("Zir SSH menu"), classes="title")
            yield OptionList(*[
                Option(Text.assemble((key.ljust(column), "bold cyan"), _(label)), id=result)
                for result, key, _keys, label in self._items()
            ])
            yield Static(_("Press the key or choose with Enter · Esc: back to the terminal"), id="tmenu-hint")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_key(self, event: events.Key) -> None:
        for result, _key, keys, _label in self._items():
            if event.key in keys:
                event.stop()
                event.prevent_default()
                self.dismiss(result)
                return

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
