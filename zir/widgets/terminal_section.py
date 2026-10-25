"""The Terminal section: several SSH sessions to one host, each in its own tab."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.content import Content
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static, TabbedContent, TabPane

from ..credentials import Vault
from ..i18n import _
from .credentials import MANAGE, CredentialPicker
from .terminal import SUPPORTED, TerminalView


# Most useful first: what does not fit the bar goes to the "ⓘ more" tooltip.
HINTS = (
    ("Alt+T", "new terminal"),
    ("Alt+W", "close it"),
    ("F6", "passwords"),
    ("F3/F4", "sections"),
    ("drag", "copy"),
    ("Shift+drag", "copy in mc/htop/vim"),
    ("double click", "word"),
    ("right click", "paste"),
    ("wheel", "history"),
)


class TerminalHint(Static):
    """Key hints that fit the width, right aligned; the full list in a tooltip."""

    def on_mount(self) -> None:
        column = max(len(_(key)) for key, _what in HINTS) + 2
        full = Text()
        for index, (key, what) in enumerate(HINTS):
            if index:
                full.append("\n")
            full.append(_(key).ljust(column), style="bold")
            full.append(_(what))
        self.tooltip = full

    def on_resize(self) -> None:
        width = self.size.width
        more = Content.assemble(" · ", ("ⓘ " + _("more"), "underline $accent"))
        line = Content()
        for index, (key, what) in enumerate(HINTS):
            part = Content.assemble((" · " if index else ""), (_(key), "bold $text"), " ", _(what))
            rest = 0 if index == len(HINTS) - 1 else more.cell_length
            if line.cell_length + part.cell_length + rest > width:
                line += more
                break
            line += part
        self.update(line)


class TerminalSection(Vertical):
    DEFAULT_CSS = """
    TerminalSection { width: 1fr; height: 1fr; }
    TerminalSection #term-bar { height: 1; }
    TerminalSection #term-bar Button {
        height: 1; min-width: 0; margin-right: 1; padding: 0 1;
        border: none; border-top: none; border-bottom: none;
    }
    TerminalSection #term-hint { width: 1fr; height: 1; color: $text-muted; content-align: right middle; }
    TerminalSection #term-tabs { width: 1fr; height: 1fr; }
    TerminalSection #term-tabs TabPane { padding: 0; }
    TerminalSection #term-empty { padding: 1 2; color: $text-muted; }
    """

    def __init__(self, command: list[str], cwd: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.command = command
        self.cwd = cwd
        self._counter = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="term-bar"):
            yield Button(_("+ New terminal"), id="term-new", variant="primary")
            yield Button(_("✕ Close"), id="term-close")
            yield Button(_("🔑 Passwords"), id="term-passwords")
            yield TerminalHint(id="term-hint")
        if not SUPPORTED:
            yield Static(
                _("The built-in terminal works on macOS and Linux for now. On Windows use: {cmd}",
                  cmd=" ".join(self.command)),
                id="term-empty",
            )
        yield TabbedContent(id="term-tabs")

    # -- sessions ------------------------------------------------------------

    @property
    def tabs(self) -> TabbedContent:
        return self.query_one("#term-tabs", TabbedContent)

    def active_terminal(self) -> TerminalView | None:
        pane = self.tabs.active_pane
        return pane.query_one(TerminalView) if pane is not None else None

    def sessions(self) -> list[TerminalView]:
        return list(self.query(TerminalView))

    async def new_session(self) -> TerminalView | None:
        if not SUPPORTED:
            return None
        self._counter += 1
        view = TerminalView(self.command, cwd=self.cwd)
        pane = TabPane(_("Terminal {n}", n=self._counter), view, id=f"term-{self._counter}")
        await self.tabs.add_pane(pane)
        self.tabs.active = pane.id
        # Start once laid out, so the remote shell gets the real window size.
        self.call_after_refresh(self._start, view)
        return view

    def _start(self, view: TerminalView) -> None:
        view.start()
        view.focus()

    async def ensure_session(self) -> TerminalView | None:
        view = self.active_terminal()
        if view is None:
            view = await self.new_session()
        elif not view.running and view.exit_code is None:
            self._start(view)
        else:
            view.focus()
        return view

    async def close_session(self) -> None:
        pane = self.tabs.active_pane
        if pane is None:
            return
        pane.query_one(TerminalView).stop()
        await self.tabs.remove_pane(pane.id)
        view = self.active_terminal()
        if view is not None:
            view.focus()

    async def paste(self, text: str) -> None:
        view = await self.ensure_session()
        if view is not None:
            view.paste(text)

    def stop_all(self) -> None:
        for view in self.sessions():
            view.stop()

    # -- identity cards ------------------------------------------------------

    @property
    def vault(self) -> Vault:
        vault = getattr(self.app, "vault", None)
        if vault is None:
            vault = self.app.vault = Vault()
        vault.ensure_loaded()
        return vault

    def pick_credential(self, view: TerminalView) -> None:
        def done(choice) -> None:
            if choice == MANAGE:
                self.app.action_credentials()
            elif choice is not None and view.running:
                view.submit_secret(choice.password)
            else:
                view.focus()

        self.app.push_screen(CredentialPicker(self.vault), done)

    # -- events --------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "term-new":
            event.stop()
            await self.new_session()
        elif event.button.id == "term-close":
            event.stop()
            await self.close_session()
        elif event.button.id == "term-passwords":
            event.stop()
            view = await self.ensure_session()
            if view is not None:
                self.pick_credential(view)

    def on_terminal_view_password_prompt(self, event: TerminalView.PasswordPrompt) -> None:
        event.stop()
        # Only for the session the user is looking at, with no other window open.
        if event.terminal is not self.active_terminal() or self.app.screen is not self.screen:
            return
        if not self.vault.cards or not self.is_visible_on_screen():
            return
        self.pick_credential(event.terminal)

    def is_visible_on_screen(self) -> bool:
        return self.display and self.region.area > 0

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id == "term-tabs":
            event.stop()
            view = self.active_terminal()
            if view is not None:
                view.focus()

    def _tab_of(self, view: TerminalView):
        for pane in self.tabs.query(TabPane):
            if view in pane.query(TerminalView):
                return self.tabs.get_tab(pane.id)
        return None

    def on_terminal_view_started(self, event: TerminalView.Started) -> None:
        event.stop()
        tab = self._tab_of(event.terminal)
        if tab is not None:
            tab.label = tab.label.plain.split(" · ")[0]

    def on_terminal_view_exited(self, event: TerminalView.Exited) -> None:
        event.stop()
        tab = self._tab_of(event.terminal)
        if tab is not None:
            tab.label = tab.label.plain.split(" · ")[0] + " · " + _("ended")
        event.terminal.note(_("[session ended — press Enter to reconnect]"))
