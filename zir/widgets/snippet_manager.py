"""The user's own snippets: create, edit, delete and reorder them."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, RadioButton, RadioSet, Static
from textual.widgets.option_list import Option

from ..i18n import _, localized
from ..snippets import GLOBAL, UserSnippet, UserSnippets

NEW = "new"  # the "+ New snippet" option


def snippet_label(item: UserSnippet) -> Text:
    return Text.assemble((item.title or "—", "bold"), "\n", ("$ " + item.command, "cyan"))


@localized
class SnippetManager(ModalScreen[None]):
    """Two lists: snippets for every server and snippets only for `host` (None: the Local tab)."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+up", "move(-1)", "Move up", show=False),
        Binding("ctrl+down", "move(1)", "Move down", show=False),
    ]
    DEFAULT_CSS = """
    SnippetManager { align: center middle; }
    SnippetManager > Vertical {
        width: 110;
        max-width: 95%;
        height: auto;
        max-height: 95%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    SnippetManager .title { text-style: bold; }
    SnippetManager #snipm-info { height: auto; color: $text-muted; margin-bottom: 1; }
    SnippetManager #snipm-body { height: 20; }
    SnippetManager #snipm-list { width: 46; height: 1fr; margin-right: 2; }
    SnippetManager #snipm-form { width: 1fr; height: auto; }
    SnippetManager .label { color: $text-muted; height: 1; }
    SnippetManager RadioSet { width: 1fr; height: auto; }
    SnippetManager #snipm-status { height: auto; margin-top: 1; }
    SnippetManager #snipm-buttons { height: auto; margin-top: 1; }
    SnippetManager #snipm-buttons Button { margin-right: 1; min-width: 10; }
    """

    def __init__(self, host: str | None, store: UserSnippets | None = None) -> None:
        super().__init__()
        self.host = host
        self.store = store or UserSnippets()
        self.current: UserSnippet | None = None  # None: a new snippet
        self._confirm_delete = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(_("My snippets"), classes="title")
            yield Static(
                _("Your own commands. Insert types them into the terminal without pressing Enter. "
                  "Ctrl+↑ / Ctrl+↓ change the order."),
                id="snipm-info",
            )
            with Horizontal(id="snipm-body"):
                yield OptionList(id="snipm-list")
                with Vertical(id="snipm-form"):
                    yield Static(_("Name"), classes="label")
                    yield Input(placeholder=_("e.g. Restart the queue"), id="snipm-title")
                    yield Static(_("Command"), classes="label")
                    yield Input(placeholder="sudo systemctl restart …", id="snipm-command")
                    yield Static(_("Show for"), classes="label")
                    with RadioSet(id="snipm-scope"):
                        yield RadioButton(_("all servers"), id="snipm-global", value=True)
                        if self.host:
                            yield RadioButton(_("only {host}", host=self.host), id="snipm-host")
            yield Static(id="snipm-status")
            with Horizontal(id="snipm-buttons"):
                yield Button(_("New"), id="snipm-new")
                yield Button(_("Save"), variant="primary", id="snipm-save")
                yield Button(_("Delete"), variant="error", id="snipm-delete")
                yield Button("↑ " + _("Up"), id="snipm-up")
                yield Button("↓ " + _("Down"), id="snipm-down")
                yield Button(_("Close"), id="snipm-close")

    def on_mount(self) -> None:
        own, host = self.store.lists(self.host)
        first = (own + host)[0] if own + host else None
        self._edit(first)
        self._fill_list()

    # -- helpers -------------------------------------------------------------------

    def _status(self, message: str, error: bool = False) -> None:
        self.query_one("#snipm-status", Static).update(Text(message, style="red" if error else "green"))

    def _scope(self) -> str:
        host_button = self.query("#snipm-host")
        if self.host and host_button and host_button.first(RadioButton).value:
            return self.host
        return GLOBAL

    def _fill_list(self) -> None:
        options = self.query_one("#snipm-list", OptionList)
        options.clear_options()
        own, host = self.store.lists(self.host)
        groups = [(_("For all servers"), own)]
        if self.host:
            groups.append((_("Only for {host}", host=self.host), host))
        for index, (title, items) in enumerate(groups):
            options.add_option(Option(Text(title, style="bold"), id=f"head-{index}", disabled=True))
            if not items:
                options.add_option(Option(Text("  " + _("(empty)"), style="italic grey50"), id=f"empty-{index}",
                                          disabled=True))
            options.add_options([Option(snippet_label(item), id=item.id) for item in items])
        options.add_option(Option(Text(_("+ New snippet"), style="italic"), id=NEW))
        target = self.current.id if self.current else NEW
        try:
            options.highlighted = options.get_option_index(target)
        except LookupError:
            options.highlighted = options.get_option_index(NEW)

    def _edit(self, item: UserSnippet | None) -> None:
        self.current = item
        self._confirm_delete = False
        delete = self.query_one("#snipm-delete", Button)
        delete.label = _("Delete")
        for selector in ("#snipm-delete", "#snipm-up", "#snipm-down"):
            self.query_one(selector, Button).disabled = item is None
        self.query_one("#snipm-title", Input).value = item.title if item else ""
        self.query_one("#snipm-command", Input).value = item.command if item else ""
        found = self.store.find(item.id) if item else None
        scope = found[0] if found else GLOBAL
        button = "#snipm-host" if scope != GLOBAL and self.host else "#snipm-global"
        self.query_one(button, RadioButton).value = True

    def _write(self) -> bool:
        try:
            self.store.save()
        except OSError as exc:
            self.store.load()  # back to what is on disk
            self._status(_("Could not save: {error}", error=exc), error=True)
            return False
        return True

    def _save(self) -> None:
        title = self.query_one("#snipm-title", Input).value.strip()
        command = self.query_one("#snipm-command", Input).value.strip()
        if not command:
            self._status(_("Type the command."), error=True)
            self.query_one("#snipm-command", Input).focus()
            return
        title = title or command
        scope = self._scope()
        if self.current is None:
            item = self.store.add(scope, title, command)
        else:
            item = self.store.update(self.current.id, title, command, scope)
        if not self._write():
            return
        self._edit(item)
        self._fill_list()
        self._status(_("Saved “{name}”.", name=title))

    def _delete(self) -> None:
        if self.current is None:
            return
        delete = self.query_one("#snipm-delete", Button)
        if not self._confirm_delete:
            self._confirm_delete = True
            delete.label = _("Really delete?")
            return
        name = self.current.title
        self.store.delete(self.current.id)
        if not self._write():
            return
        self._edit(None)
        self._fill_list()
        self._status(_("Deleted “{name}”.", name=name))

    def action_move(self, delta: int) -> None:
        if self.current is None:
            return
        if self.store.move(self.current.id, delta) and self._write():
            self._fill_list()
            self._status("")

    # -- events ----------------------------------------------------------------------

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        found = self.store.find(event.option.id or "")
        item = found[1] if found else None
        if item is not self.current:
            self._edit(item)
            self._status("")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.query_one("#snipm-title", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._save()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # do not let the app behind the modal handle it too
        button = event.button.id
        if button == "snipm-new":
            self._edit(None)
            self._fill_list()
            self._status("")
            self.query_one("#snipm-title", Input).focus()
        elif button == "snipm-save":
            self._save()
        elif button == "snipm-delete":
            self._delete()
        elif button == "snipm-up":
            self.action_move(-1)
        elif button == "snipm-down":
            self.action_move(1)
        elif button == "snipm-close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
