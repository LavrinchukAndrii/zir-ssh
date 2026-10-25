"""Ctrl+N: add a server from inside the app; Delete: remove one (with confirmation)."""

from __future__ import annotations

from typing import Awaitable, Callable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from ..config import ConfigError, HostSpec
from ..hosts_file import NAME_RE, parse_address, suggest_name
from ..i18n import _, localized
from ..sshconfig import SshAlias

CheckRunner = Callable[[HostSpec], Awaitable[tuple[bool, str]]]


@localized
class AddHostScreen(ModalScreen[HostSpec | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AddHostScreen { align: center middle; }
    AddHostScreen > Vertical {
        width: 84; max-width: 95%; height: auto; max-height: 95%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    AddHostScreen .title { text-style: bold; margin-bottom: 1; }
    AddHostScreen .row { height: 3; }
    AddHostScreen .row Static { width: 22; height: 3; content-align: left middle; color: $text-muted; }
    AddHostScreen .row Input, AddHostScreen .row Select { width: 1fr; }
    AddHostScreen #host-port { width: 12; }
    AddHostScreen #add-status { height: auto; min-height: 1; margin-top: 1; }
    AddHostScreen #add-buttons { height: auto; margin-top: 1; }
    AddHostScreen #add-buttons Button { margin-right: 2; }
    """

    def __init__(self, taken: set[str], aliases: list[SshAlias], check: CheckRunner | None = None) -> None:
        super().__init__()
        self.taken = taken
        self.aliases = [a for a in aliases if a.name not in taken]
        self.check_runner = check
        self._name_touched = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(_("New server"), classes="title")
            if self.aliases:
                with Horizontal(classes="row"):
                    yield Static(_("From ~/.ssh/config"))
                    yield Select([(f"{a.name}  ·  {a.detail}", a.name) for a in self.aliases],
                                 prompt=_("pick an alias (optional)"), id="host-alias")
            with Horizontal(classes="row"):
                yield Static(_("Address"))
                yield Input(placeholder="root@203.0.113.10", id="host-address")
                yield Input(placeholder="22", id="host-port", type="integer")
            with Horizontal(classes="row"):
                yield Static(_("Name (no spaces)"))
                yield Input(placeholder="shop-1", id="host-name")
            with Horizontal(classes="row"):
                yield Static(_("Human name"))
                yield Input(placeholder=_("e.g. Shop (production)"), id="host-title")
            with Horizontal(classes="row"):
                yield Static(_("SSH key (optional)"))
                yield Input(placeholder="~/.ssh/id_ed25519", id="host-identity")
            yield Static(
                Text(_("Password-only server? Add it anyway: Zir SSH asks for the password once and installs your key."),
                     style="grey58"),
                id="add-status",
            )
            with Horizontal(id="add-buttons"):
                yield Button(_("Add"), variant="primary", id="add-ok")
                yield Button(_("Check connection"), id="add-check")
                yield Button(_("Cancel"), id="add-cancel")

    def on_mount(self) -> None:
        self.query_one("#host-address", Input).focus()

    # -- form ------------------------------------------------------------------

    def _status(self, text: str, style: str = "") -> None:
        self.query_one("#add-status", Static).update(Text(text, style=style))

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if event.value is Select.BLANK:
            return
        alias = next((a for a in self.aliases if a.name == event.value), None)
        if alias is None:
            return
        self.query_one("#host-address", Input).value = alias.name  # ssh resolves user/host/port itself
        if not self._name_touched:
            self.query_one("#host-name", Input).value = suggest_name(alias.name, self.taken)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "host-name" and event.input.has_focus:
            self._name_touched = True
        elif event.input.id == "host-address" and not self._name_touched:
            target = event.value.strip().split(":")[0]
            self.query_one("#host-name", Input).value = suggest_name(target, self.taken) if target else ""

    def spec(self) -> HostSpec:
        value = lambda selector: self.query_one(selector, Input).value.strip()  # noqa: E731
        target, port = parse_address(value("#host-address"), value("#host-port"))
        name = value("#host-name") or suggest_name(target, self.taken)
        if not NAME_RE.match(name):
            raise ConfigError(_("name: latin letters, digits, dots, dashes; no spaces"))
        if name in self.taken:
            raise ConfigError(_("a server named {name} already exists", name=name))
        return HostSpec(name=name, target=target, port=port, title=value("#host-title") or None,
                        identity=value("#host-identity") or None)

    # -- actions -----------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_add()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        {"add-ok": self.action_add, "add-check": self.action_check, "add-cancel": self.action_cancel}.get(
            event.button.id, lambda: None
        )()

    def action_add(self) -> None:
        try:
            spec = self.spec()
        except ConfigError as exc:
            self._status(str(exc), "bold red")
            return
        self.dismiss(spec)

    @work(exclusive=True)
    async def action_check(self) -> None:
        try:
            spec = self.spec()
        except ConfigError as exc:
            self._status(str(exc), "bold red")
            return
        if self.check_runner is None:
            return
        self._status(_("Connecting to {target}…", target=spec.target), "bold")
        ok, message = await self.check_runner(spec)
        if ok:
            self._status("✓ " + _("Connected: {info}", info=message), "bold green")
        elif "permission denied" in message.lower():
            self._status(_("The server answers but does not accept your key yet. Add it: Zir SSH will ask for the "
                           "password once."), "bold yellow")
        else:
            self._status("✖ " + message, "bold red")

    def action_cancel(self) -> None:
        self.dismiss(None)


@localized
class ConfirmRemove(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Remove"), Binding("n,escape", "answer(False)", "Cancel")]
    DEFAULT_CSS = """
    ConfirmRemove { align: center middle; }
    ConfirmRemove > Vertical {
        width: 70; max-width: 95%; height: auto; border: round $error; background: $surface; padding: 1 2;
    }
    ConfirmRemove #rm-buttons { height: auto; margin-top: 1; }
    ConfirmRemove #rm-buttons Button { margin-right: 2; }
    """

    def __init__(self, label: str, target: str, in_config: bool) -> None:
        super().__init__()
        self.label, self.target, self.in_config = label, target, in_config

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text.assemble(
                (_("Remove server {name}?", name=self.label), "bold"), "\n", (self.target, "grey58"), "\n\n",
                _("It is removed from hosts.toml and its tab closes. Nothing changes on the server itself.")
                if self.in_config else
                _("This server is not in hosts.toml (opened from the command line): it is only closed now."),
            ))
            with Horizontal(id="rm-buttons"):
                yield Button(_("Remove"), variant="error", id="rm-yes")
                yield Button(_("Cancel"), id="rm-no")

    def on_mount(self) -> None:
        self.query_one("#rm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "rm-yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)
