"""Modal: database user and password, used once to write ~/.my.cnf on the server."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..i18n import _, localized


@localized
class DbAccessScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    DbAccessScreen { align: center middle; }
    DbAccessScreen > Vertical {
        width: 80; max-width: 95%; height: auto; border: round $accent; background: $surface; padding: 1 2;
    }
    DbAccessScreen .row { height: 3; }
    DbAccessScreen .row Static { width: 18; height: 3; content-align: left middle; color: $text-muted; }
    DbAccessScreen .row Input { width: 1fr; }
    DbAccessScreen #db-error { height: auto; color: $error; }
    DbAccessScreen #db-buttons { height: auto; margin-top: 1; }
    DbAccessScreen #db-buttons Button { margin-right: 2; }
    """

    def __init__(self, host_label: str, error: str = "") -> None:
        super().__init__()
        self.host_label = host_label
        self.error = error

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text.assemble(
                (_("Database access on {host}", host=self.host_label), "bold"), "\n\n",
                _("MySQL / MariaDB asks for a password. Enter it once: Zir SSH checks it and saves it to "
                  "~/.my.cnf on the server (only readable by that user), the standard place the mysql "
                  "client reads it from. Zir SSH itself does not store it."), "\n",
                (_("Zir SSH only reads statistics (SHOW STATUS, PROCESSLIST); a read-only user is enough."), "grey58"),
            ))
            with Horizontal(classes="row"):
                yield Static(_("DB user"))
                yield Input(value="root", id="db-user")
            with Horizontal(classes="row"):
                yield Static(_("Password"))
                yield Input(password=True, id="db-pass")
            yield Static(self.error, id="db-error")
            with Horizontal(id="db-buttons"):
                yield Button(_("Check and save"), variant="primary", id="db-ok")
                yield Button(_("Cancel"), id="db-cancel")

    def on_mount(self) -> None:
        self.query_one("#db-pass", Input).focus()

    def _submit(self) -> None:
        user = self.query_one("#db-user", Input).value.strip()
        password = self.query_one("#db-pass", Input).value
        if not user or not password:
            self.query_one("#db-error", Static).update(_("Enter the user and the password."))
            return
        self.dismiss((user, password))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "db-ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
