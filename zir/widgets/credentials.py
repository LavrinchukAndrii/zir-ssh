"""Identity cards: the picker shown at a password prompt and the F6 manager."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..credentials import Credential, Vault, VaultError, private_keys
from ..i18n import _, localized
from ..keysetup import KeySetupError, create_default_key

MANAGE = "manage"  # picker result: open the manager instead


def _label(card: Credential) -> Text:
    text = Text(card.name or "—", style="bold")
    if card.login:
        text.append(f"  {card.login}", style="grey50")
    return text


@localized
class CredentialPicker(ModalScreen[Credential | str | None]):
    """Pick a card: its password is typed into the terminal and Enter pressed."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    CredentialPicker { align: center middle; background: $background 40%; }
    CredentialPicker > Vertical {
        width: 56;
        max-width: 95%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    CredentialPicker .title { text-style: bold; height: 1; margin-bottom: 1; }
    CredentialPicker OptionList { height: auto; max-height: 16; border: none; padding: 0; }
    CredentialPicker #cred-error { height: auto; color: $error; }
    CredentialPicker #cred-hint { height: auto; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, vault: Vault) -> None:
        super().__init__()
        self.vault = vault

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(_("🔑 Insert a password"), classes="title")
            if self.vault.error:
                yield Static(self.vault.error, id="cred-error")
            yield OptionList(
                *[Option(_label(card), id=card.id) for card in self.vault.cards],
                Option(Text(_("Manage identity cards… (F6)"), style="italic"), id=MANAGE),
            )
            yield Static(_("Enter: type the password and press Enter · Esc: close"), id="cred-hint")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id == MANAGE:
            self.dismiss(MANAGE)
            return
        self.dismiss(next((c for c in self.vault.cards if c.id == event.option.id), None))

    def action_cancel(self) -> None:
        self.dismiss(None)


@localized
class CredentialManager(ModalScreen[None]):
    """Create, edit and delete identity cards."""

    BINDINGS = [Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    CredentialManager { align: center middle; }
    CredentialManager > Vertical {
        width: 96;
        max-width: 95%;
        height: auto;
        max-height: 95%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    CredentialManager .title { text-style: bold; }
    CredentialManager #cred-info { height: auto; color: $text-muted; margin-bottom: 1; }
    CredentialManager #cred-body { height: 16; }
    CredentialManager #cred-list { width: 34; height: 1fr; margin-right: 2; }
    CredentialManager #cred-form { width: 1fr; height: auto; }
    CredentialManager .label { color: $text-muted; height: 1; }
    CredentialManager .pw-row { height: auto; }
    CredentialManager .pw-row Input { width: 1fr; }
    CredentialManager #cred-reveal { min-width: 6; width: 6; margin-left: 1; }
    CredentialManager #cred-status { height: auto; margin-top: 1; }
    CredentialManager #cred-buttons { height: auto; margin-top: 1; }
    CredentialManager #cred-buttons Button { margin-right: 2; }
    """

    def __init__(self, vault: Vault) -> None:
        super().__init__()
        self.vault = vault
        self.current: Credential | None = None  # None: a new card
        self._confirm_delete = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(_("Identity cards"), classes="title")
            yield Static(
                _("Encrypted with your SSH key, cannot be read on another computer. "
                  "When the terminal asks for a password, pick a card: its password is typed and Enter pressed."),
                id="cred-info",
            )
            with Horizontal(id="cred-body"):
                yield OptionList(id="cred-list")
                with Vertical(id="cred-form"):
                    yield Static(_("Name"), classes="label")
                    yield Input(placeholder=_("e.g. root on debian"), id="cred-name")
                    yield Static(_("Login"), classes="label")
                    yield Input(id="cred-login")
                    yield Static(_("Password"), classes="label")
                    with Horizontal(classes="pw-row"):
                        yield Input(password=True, id="cred-password")
                        yield Button("👁", id="cred-reveal")
            yield Static(id="cred-status")
            with Horizontal(id="cred-buttons"):
                yield Button(_("New"), id="cred-new")
                yield Button(_("Save"), variant="primary", id="cred-save")
                yield Button(_("Delete"), variant="error", id="cred-delete")
                yield Button(_("Close"), id="cred-close")

    def on_mount(self) -> None:
        self.vault.ensure_loaded()
        self._edit(self.vault.cards[0] if self.vault.cards else None)
        self._fill_list()
        if self.vault.error:
            self._status(self.vault.error, error=True)

    # -- helpers ---------------------------------------------------------------

    def _status(self, message: str, error: bool = False) -> None:
        self.query_one("#cred-status", Static).update(Text(message, style="red" if error else "green"))

    def _fill_list(self) -> None:
        options = self.query_one("#cred-list", OptionList)
        options.clear_options()
        options.add_options([Option(_label(card), id=card.id) for card in self.vault.cards])
        options.add_option(Option(Text(_("+ New card"), style="italic"), id="new"))
        ids = [card.id for card in self.vault.cards]
        options.highlighted = ids.index(self.current.id) if self.current and self.current.id in ids else len(ids)

    def _edit(self, card: Credential | None) -> None:
        self.current = card
        self._confirm_delete = False
        self.query_one("#cred-delete", Button).label = _("Delete")
        self.query_one("#cred-delete", Button).disabled = card is None
        self.query_one("#cred-name", Input).value = card.name if card else ""
        self.query_one("#cred-login", Input).value = card.login if card else ""
        password = self.query_one("#cred-password", Input)
        password.value = card.password if card else ""
        password.password = True
        self.query_one("#cred-name", Input).focus()

    async def _save(self) -> None:
        name = self.query_one("#cred-name", Input).value.strip()
        login = self.query_one("#cred-login", Input).value.strip()
        password = self.query_one("#cred-password", Input).value
        if not name:
            self._status(_("Type a name for the card."), error=True)
            return
        if not password:
            self._status(_("Type the password."), error=True)
            return
        if not private_keys():
            try:
                await create_default_key()
            except KeySetupError as exc:
                self._status(str(exc), error=True)
                return
        before = list(self.vault.cards)
        if self.current is None:
            self.current = Credential(name, login, password)
            self.vault.cards.append(self.current)
        else:
            self.current.name, self.current.login, self.current.password = name, login, password
        try:
            self.vault.save()
        except VaultError as exc:
            self.vault.cards = before
            self._status(str(exc), error=True)
            return
        self.query_one("#cred-delete", Button).disabled = False
        self._fill_list()
        self._status(_("Saved “{name}”.", name=name))

    def _delete(self) -> None:
        if self.current is None:
            return
        delete = self.query_one("#cred-delete", Button)
        if not self._confirm_delete:
            self._confirm_delete = True
            delete.label = _("Really delete?")
            return
        name = self.current.name
        before = list(self.vault.cards)
        self.vault.cards = [c for c in self.vault.cards if c.id != self.current.id]
        try:
            self.vault.save()
        except VaultError as exc:
            self.vault.cards = before
            self._status(str(exc), error=True)
            return
        self._edit(self.vault.cards[0] if self.vault.cards else None)
        self._fill_list()
        self._status(_("Deleted “{name}”.", name=name))

    # -- events ------------------------------------------------------------------

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        card = next((c for c in self.vault.cards if c.id == event.option.id), None)
        if card is not self.current:
            self._edit(card)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.query_one("#cred-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.run_worker(self._save(), exclusive=True, group="cred-save")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # do not let the app behind the modal handle it too
        button = event.button.id
        if button == "cred-reveal":
            password = self.query_one("#cred-password", Input)
            password.password = not password.password
        elif button == "cred-new":
            self._edit(None)
            self._fill_list()
            self._status("")
        elif button == "cred-save":
            await self._save()
        elif button == "cred-delete":
            self._delete()
        elif button == "cred-close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
