"""First-run screen: shown when Zir SSH starts without any configured hosts.

Lets the user pick SSH aliases from ~/.ssh/config, type addresses, or start
the demo, instead of printing an error and exiting (which, when started by
double-click from Finder/Explorer, closes the window before it can be read).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Header, Input, SelectionList, Static

from .clipboard import ClipboardMixin
from .config import HostSpec, hosts_from_args
from .i18n import _, localized
from .sshconfig import SshAlias
from .widgets.settings import RESTART, SettingsScreen


@dataclass
class WelcomeResult:
    demo: bool = False
    specs: list[HostSpec] = field(default_factory=list)
    save: bool = False
    restart: bool = False  # language changed: show this screen again


def save_hosts(path: Path, specs: list[HostSpec]) -> None:
    """Write a new hosts.toml (never touches an existing file)."""
    lines = [
        "# Created by Zir SSH on first start. See hosts.example.toml for all options.",
        "",
        "[settings]",
        "interval = 2.0",
        "",
    ]
    for spec in specs:
        lines += ["", f'[hosts."{spec.name}"]', f'target = "{spec.target}"']
        if spec.port:
            lines.append(f"port = {spec.port}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@localized
class WelcomeApp(ClipboardMixin, App[WelcomeResult | None]):
    TITLE = "Zir SSH"
    # Textual's own Ctrl+P palette is English-only and duplicates Ctrl+K.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #body {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    #intro {
        height: auto;
        margin-bottom: 1;
    }
    #aliases {
        height: auto;
        max-height: 1fr;
        min-height: 3;
        border: round $panel;
    }
    #aliases-empty {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #manual {
        margin-top: 1;
    }
    #save {
        margin-top: 1;
    }
    #buttons {
        height: auto;
        margin-top: 1;
    }
    #buttons Button {
        margin-right: 2;
    }
    #error {
        height: auto;
        color: $error;
    }
    """

    BINDINGS = [
        Binding("ctrl+d", "demo", "Demo", priority=True),  # Input binds ctrl+d itself
        Binding("f2", "settings", "Language", priority=True),
        Binding("escape", "quit_welcome", "Quit"),
        Binding("ctrl+q", "quit_welcome", "Quit", show=False),
        *ClipboardMixin.CLIPBOARD_BINDINGS,
    ]

    def __init__(self, aliases: list[SshAlias], config_path: Path) -> None:
        super().__init__()
        self.sub_title = _("first start")
        self.aliases = aliases
        self.config_path = config_path

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="body"):
            yield Static(
                Text.assemble(
                    (_("No servers configured yet.") + "\n", "bold"),
                    _("Pick hosts from your SSH config, type addresses, or try the demo.") + "\n",
                    (_("Zir SSH only reads: no sudo, no changes on the servers. If a server asks for a password, "
                       "Zir SSH asks you once and adds your SSH key there (like ssh-copy-id)."), "grey50"),
                ),
                id="intro",
            )
            if self.aliases:
                yield SelectionList[str](
                    *((f"{a.name}  [dim]{a.detail}[/]", a.name) for a in self.aliases),
                    id="aliases",
                )
            else:
                yield Static(_("No Host entries found in ~/.ssh/config."), id="aliases-empty")
            yield Input(
                placeholder=_("other hosts: user@ip[:port] or ssh alias, separated by spaces or commas"),
                id="manual",
            )
            exists = self.config_path.exists()
            yield Checkbox(
                _("Save to {path}", path=self.config_path) if not exists
                else _("{path} exists, not changing it", path=self.config_path),
                value=not exists,
                disabled=exists,
                id="save",
            )
            with Horizontal(id="buttons"):
                yield Button(_("Connect"), variant="primary", id="connect")
                yield Button(_("Demo (no SSH)"), id="demo")
                yield Button(_("Language (F2)"), id="language")
                yield Button(_("Quit"), variant="error", id="quit")
            yield Static("", id="error")
        yield Footer()

    def on_mount(self) -> None:
        if self.aliases:
            self.query_one("#aliases", SelectionList).focus()
        else:
            self.query_one("#manual", Input).focus()

    def _chosen(self) -> list[HostSpec]:
        names: list[str] = []
        if self.aliases:
            names += list(self.query_one("#aliases", SelectionList).selected)
        manual = self.query_one("#manual", Input).value.replace(",", " ").split()
        return hosts_from_args(names + manual)

    def action_connect(self) -> None:
        specs = self._chosen()
        if not specs:
            self.query_one("#error", Static).update(
                _("Select at least one host (Space toggles) or type an address.")
            )
            return
        save = self.query_one("#save", Checkbox)
        self.exit(WelcomeResult(specs=specs, save=save.value and not save.disabled))

    def action_demo(self) -> None:
        self.exit(WelcomeResult(demo=True))

    def action_quit_welcome(self) -> None:
        self.exit(None)

    def action_settings(self) -> None:
        def done(result: str | None) -> None:
            if result == RESTART:
                self.exit(WelcomeResult(restart=True))

        self.push_screen(SettingsScreen(config_path=self.config_path), done)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_connect()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = {"connect": self.action_connect, "demo": self.action_demo,
                  "language": self.action_settings, "quit": self.action_quit_welcome}.get(event.button.id)
        if action is not None:
            action()
