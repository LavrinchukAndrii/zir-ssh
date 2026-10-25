"""Modal asking for a server password once, to install the SSH key."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..config import HostSpec
from ..i18n import _, localized


@localized
class PasswordScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "skip", "Skip")]
    DEFAULT_CSS = """
    PasswordScreen { align: center middle; }
    PasswordScreen > Vertical {
        width: 76;
        max-width: 95%;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    PasswordScreen #pw-text { height: auto; margin-bottom: 1; }
    PasswordScreen #pw-error { height: auto; color: $error; }
    PasswordScreen #pw-buttons { height: auto; margin-top: 1; }
    PasswordScreen #pw-buttons Button { margin-right: 2; }
    """

    def __init__(self, spec: HostSpec, key_hint: str, error: str = "") -> None:
        super().__init__()
        self.spec = spec
        self.key_hint = key_hint
        self.error = error

    def compose(self) -> ComposeResult:
        address = self.spec.target + (f":{self.spec.port}" if self.spec.port else "")
        with Vertical():
            yield Static(
                Text.assemble(
                    (_("Connect "), "bold"), (address, "bold cyan"), "\n\n",
                    _("The server does not accept your SSH key yet. Enter its password once: Zir SSH will add "),
                    (self.key_hint, "bold"),
                    _(" to ~/.ssh/authorized_keys there, and from then on log in without a password."), "\n",
                    (_("The password is used for this one login and is not saved."), "grey50"),
                ),
                id="pw-text",
            )
            yield Input(password=True, placeholder=_("password for {target}", target=self.spec.target), id="pw-input")
            yield Static(self.error, id="pw-error")
            with Horizontal(id="pw-buttons"):
                yield Button(_("Add key"), variant="primary", id="pw-ok")
                yield Button(_("Skip"), id="pw-skip")

    def on_mount(self) -> None:
        self.query_one("#pw-input", Input).focus()

    def _submit(self) -> None:
        password = self.query_one("#pw-input", Input).value
        if not password:
            self.query_one("#pw-error", Static).update(_("Type the password, or press Esc to skip."))
            return
        self.dismiss(password)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # do not let the app behind the modal handle it too
        if event.button.id == "pw-ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_skip(self) -> None:
        self.dismiss(None)
