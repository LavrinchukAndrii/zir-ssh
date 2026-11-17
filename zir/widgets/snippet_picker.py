"""Pick a snippet from the terminal: it is typed at the prompt, Enter is left to the user."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import snippets
from ..i18n import _, localized
from ..snippets import UserSnippets

MANAGE = "manage"  # picker result: open the snippet manager instead


def snippet_groups(host: str | None, data: dict | None = None,
                   store: UserSnippets | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(group title, [(title, command), ...]), ...]: own snippets first, then the checked ones.

    `data` is the saved check of the host (snippets.load), read from disk when not given.
    """
    store = store or UserSnippets()
    own, for_host = store.lists(host)
    groups = []
    if own:
        groups.append((_("My · all servers"), [(s.title, s.command) for s in own]))
    if for_host:
        groups.append((_("My · {host}", host=host), [(s.title, s.command) for s in for_host]))
    if data is None and host:
        data = snippets.load(host)
    if data:
        for group in snippets.build(data["facts"]):
            groups.append((group.title, [(s.title, s.command) for s in group.snippets]))
    return groups


@localized
class SnippetPicker(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    SnippetPicker { align: center middle; background: $background 40%; }
    SnippetPicker > Vertical {
        width: 100;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    SnippetPicker .title { text-style: bold; height: 1; margin-bottom: 1; }
    SnippetPicker Input { margin-bottom: 1; }
    SnippetPicker OptionList { height: auto; max-height: 24; border: none; padding: 0; }
    SnippetPicker #snipp-hint { height: auto; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, host: str | None, store: UserSnippets | None = None) -> None:
        super().__init__()
        self.host = host
        self.groups = snippet_groups(host, store=store)
        self.commands: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(_("📋 Insert a snippet"), classes="title")
            yield Input(placeholder=_("filter…"), id="snipp-filter")
            yield OptionList(id="snipp-list")
            yield Static(_("Enter: type the command (without running it) · ↑↓: choose · Esc: close"), id="snipp-hint")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one(Input).focus()

    def _fill(self, needle: str) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        self.commands = {}
        needle = needle.strip().lower()
        for index, (title, items) in enumerate(self.groups):
            shown = [(t, c) for t, c in items if not needle or needle in f"{t}\n{c}\n{title}".lower()]
            if not shown:
                continue
            options.add_option(Option(Text(title, style="bold"), id=f"head-{index}", disabled=True))
            for number, (snippet_title, command) in enumerate(shown):
                option_id = f"s-{index}-{number}"
                self.commands[option_id] = command
                options.add_option(Option(
                    Text.assemble("  ", (snippet_title, "bold"), "\n  ", ("$ " + command, "cyan")), id=option_id))
        if not self.commands:
            message = _("Nothing matches the filter.") if needle else _(
                "No snippets yet. Add your own below, or press “Check server” in the Snippets section.")
            options.add_option(Option(Text(message, style="italic grey50"), id="none", disabled=True))
        options.add_option(Option(Text(_("Manage my snippets…"), style="italic"), id=MANAGE))
        first = next(iter(self.commands), MANAGE)
        options.highlighted = options.get_option_index(first)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            self._choose(options.get_option_at_index(options.highlighted).id)

    def on_key(self, event: events.Key) -> None:
        """Arrows and pages move the list while the cursor stays in the filter."""
        actions = {"up": "cursor_up", "down": "cursor_down", "pageup": "page_up", "pagedown": "page_down"}
        if event.key in actions and isinstance(self.focused, Input):
            event.stop()
            event.prevent_default()
            getattr(self.query_one(OptionList), f"action_{actions[event.key]}")()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose(event.option.id)

    def _choose(self, option_id: str | None) -> None:
        if option_id == MANAGE:
            self.dismiss(MANAGE)
        elif option_id in self.commands:
            self.dismiss(self.commands[option_id])

    def action_cancel(self) -> None:
        self.dismiss(None)
