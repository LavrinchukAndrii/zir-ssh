"""F2: settings: interface language and human names of the servers; saved to prefs.json."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from ..config import DEFAULT_CONFIG, Settings
from ..i18n import LANGUAGES, _, get_language, localized
from ..prefs import load_prefs, prefs_path, save_prefs

RESTART = "restart"  # language changed: the UI is rebuilt
TITLES = "titles"    # only server names changed: applied in place


@localized
class SettingsScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    SettingsScreen > Vertical {
        width: 84;
        max-width: 95%;
        height: auto;
        max-height: 95%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    SettingsScreen .title { text-style: bold; margin-bottom: 1; }
    SettingsScreen .label { color: $text-muted; margin-top: 1; }
    SettingsScreen RadioSet { width: 1fr; }
    SettingsScreen #settings-info { height: auto; margin-top: 1; color: $text-muted; }
    SettingsScreen #titles { height: auto; max-height: 12; }
    SettingsScreen .title-row { height: 1; margin-bottom: 1; }
    SettingsScreen .title-host { width: 34; height: 1; }
    SettingsScreen .title-row Input {
        width: 1fr; height: 1; border: none; padding: 0 1; background: $boost;
    }
    SettingsScreen .title-row Input:focus { background: $accent 30%; }
    SettingsScreen #settings-buttons { height: auto; margin-top: 1; }
    SettingsScreen #settings-buttons Button { margin-right: 2; }
    """

    def __init__(
        self,
        settings: Settings | None = None,
        config_path: Path | None = None,
        hosts: list[tuple[str, str, str | None]] | None = None,  # (name, target, title from hosts.toml)
    ) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.config_path = config_path or DEFAULT_CONFIG
        self.hosts = hosts or []
        saved = load_prefs().get("titles", {})
        self.saved_titles: dict[str, str] = saved if isinstance(saved, dict) else {}

    def compose(self) -> ComposeResult:
        current = get_language()
        with Vertical():
            yield Static(_("Settings"), classes="title")
            yield Static(_("Interface language"), classes="label")
            with RadioSet(id="language"):
                for code, name in LANGUAGES.items():
                    yield RadioButton(name, value=code == current, id=f"lang-{code}")
            if self.hosts:
                yield Static(_("Server names (empty = technical name)"), classes="label")
                with VerticalScroll(id="titles"):
                    for index, (name, target, config_title) in enumerate(self.hosts):
                        with Horizontal(classes="title-row"):
                            host = Text(name, style="bold")
                            if target != name:
                                host.append(f"  {target}", style="grey50")
                            host.truncate(33, overflow="ellipsis")
                            yield Static(host, classes="title-host")
                            yield Input(
                                value=self.saved_titles.get(name) or config_title or "",
                                placeholder=_("e.g. Shop (production)"),
                                id=f"title-{index}",
                            )
            yield Static(
                Text.assemble(
                    _("Servers and polling: "), (str(self.config_path), "bold"), "\n",
                    _("interval {interval} s, timeout {timeout} s, up to {parallel} SSH sessions at once",
                      interval=f"{self.settings.interval:g}", timeout=f"{self.settings.timeout:g}",
                      parallel=self.settings.max_parallel),
                    "\n", _("This window saves to: "), str(prefs_path()),
                ),
                id="settings-info",
            )
            with Horizontal(id="settings-buttons"):
                yield Button(_("Save"), variant="primary", id="settings-save")
                yield Button(_("Cancel"), id="settings-cancel")

    def on_mount(self) -> None:
        self.query_one(RadioSet).focus()

    def _selected_language(self) -> str:
        pressed = self.query_one(RadioSet).pressed_button
        return pressed.id.removeprefix("lang-") if pressed and pressed.id else get_language()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # do not let the app behind the modal handle it too
        if event.button.id == "settings-save":
            self.action_save()
        else:
            self.action_cancel()

    def _titles(self) -> dict[str, str]:
        titles = {}
        for index, (name, _target, config_title) in enumerate(self.hosts):
            value = self.query_one(f"#title-{index}", Input).value.strip()
            if value and value != config_title:
                titles[name] = value
        return titles

    def action_save(self) -> None:
        language = self._selected_language()
        updates: dict = {"language": language}
        titles_changed = False
        if self.hosts:
            titles = self._titles()
            # Keep names of hosts that are not shown now (another config / CLI hosts).
            known = {name for name, _t, _c in self.hosts}
            merged = {k: v for k, v in self.saved_titles.items() if k not in known} | titles
            titles_changed = merged != self.saved_titles
            updates["titles"] = merged
        save_prefs(updates)
        if language != get_language():
            self.dismiss(RESTART)
        else:
            self.dismiss(TITLES if titles_changed else None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()

    def action_cancel(self) -> None:
        self.dismiss(None)
