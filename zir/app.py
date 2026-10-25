"""Textual UI for zir: one top-level tab per server, plus the Fleet tab."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

from .clipboard import ClipboardMixin
from .collector import Collector, ProbeError, build_collectors, interactive_command, local_shell_command
from .config import DEFAULT_CONFIG, ConfigError, HostSpec, Settings
from .credentials import Vault
from .hosts_file import add_host, remove_host
from .i18n import _, localized
from .keysetup import KeySetupError, ensure_public_key, install_key, public_key_for
from .poller import HostWorker
from .prefs import load_prefs, save_prefs
from .store import BAD_LEVELS, HostStore, Session, set_disabled
from .widgets.badges import tab_label
from .widgets.credentials import CredentialManager
from .widgets.fleet_view import FleetView
from .dbaccess import DbAccessError, setup_access
from .widgets.db_access import DbAccessScreen
from .widgets.host_editor import AddHostScreen, ConfirmRemove
from .widgets.stack_sections import DbAccessRequested
from .widgets.host_view import HostView
from .widgets.password import PasswordScreen
from .widgets.settings import RESTART, TITLES, SettingsScreen
from .snippets import DETECT_SCRIPT
from .widgets.switcher import hosts_provider
from .widgets.terminal import TerminalView
from .widgets.terminal_section import TerminalSection

LOCAL_PANE = "local-pane"  # a shell on this computer, before Fleet
FLEET_PANE = "fleet-pane"
# What macOS terminals send for Option+1..9 (Textual reports these characters,
# also for ESC+digit), so Alt+digit works without "Use Option as Meta key".
OPTION_DIGITS = ("inverted_exclamation_mark", "trade_mark_sign", "pound_sign", "cent_sign",
                 "infinity", "section_sign", "pilcrow_sign", "bullet", "ª")
FRAME_SECONDS = 0.2  # render from the store at 5 fps, not on every reading

KEYS = (
    ("Ctrl+PgDn / Ctrl+Tab", "next server tab"),
    ("Ctrl+PgUp / Ctrl+Shift+Tab", "previous server tab"),
    ("Alt+1..9", "jump to tab by number (1 = Local, 2 = Fleet)"),
    ("Ctrl+K", "find a host by name, tag or address"),
    ("Ctrl+N / Delete", "add a new server / remove the current one"),
    ("Ctrl+T / Ctrl+W", "open a closed host / close the current tab"),
    ("Enter (Fleet)", "open the selected host"),
    ("[ / ]  F3 / F4", "previous / next section of the host (F3/F4 also from the terminal)"),
    ("Alt+T / Alt+W", "new / close terminal session (Terminal section)"),
    ("Mouse drag", "copy the selected text (Shift+drag inside mc, htop, vim)"),
    ("Double / triple click", "copy a word / line in the terminal"),
    ("Ctrl+C / y", "copy the selected table row"),
    ("Ctrl+R", "poll every host now"),
    ("p / P", "pause this host / pause all"),
    ("d", "disable / enable this host (no SSH at all while disabled)"),
    ("a", "add your SSH key to the host (asks for its password once)"),
    ("F2", "settings (language)"),
    ("F6", "identity cards: logins and passwords for the terminal"),
    ("?", "this help"),
    ("q", "quit"),
)


@localized
class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical {
        width: 96;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        for key, what in KEYS:
            table.add_row(_(key), _(what))
        legend = Text.assemble(
            "\n", _("Badges:"), " ", ("●", "green"), " ", _("ok"), "  ", ("▲", "yellow"), " ", _("warn"), "  ",
            ("■", "red"), " ", _("crit"), "  ", ("×", "red"), " ", _("unreachable"), "  ",
            ("·", "grey50"), " ", _("pending"), "  ", ("~", "yellow"), " ", _("stale"), "  ‖ ", _("paused"),
            ("\n\n" + _("Alt is the Option key on a Mac. Ctrl+PgUp/PgDn work everywhere. Esc closes this window."), "grey50"),
        )
        with Vertical():
            yield Static(table)
            yield Static(legend)


@localized
class ConfirmQuit(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "Quit"),
        Binding("n,escape", "answer(False)", "Stay"),
    ]
    DEFAULT_CSS = """
    ConfirmQuit { align: center middle; }
    ConfirmQuit > Vertical {
        width: auto;
        height: auto;
        max-width: 80%;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }
    ConfirmQuit Horizontal { width: auto; height: auto; margin-top: 1; }
    ConfirmQuit Button { margin-right: 2; }
    """

    def __init__(self, bad: list[str]) -> None:
        super().__init__()
        self.bad = bad

    def compose(self) -> ComposeResult:
        text = Text()
        if self.bad:
            text.append(_("Hosts need attention: "), "bold red")
            text.append(", ".join(self.bad) + "\n\n")
        text.append(_("Quit Zir SSH?"), "bold")
        text.append("  y / n", "grey50")
        with Vertical():
            yield Static(text)
            with Horizontal():
                yield Button(_("Quit"), variant="error", id="quit-yes")
                yield Button(_("Stay"), id="quit-no")

    def on_mount(self) -> None:
        # Enter on the default button keeps zir running: no accidental quit.
        self.query_one("#quit-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "quit-yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


@localized
class ZirApp(ClipboardMixin, App[str | None]):
    TITLE = "Zir SSH"
    # Textual's own Ctrl+P palette is English-only and duplicates Ctrl+K.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
    }
    #servers {
        width: 1fr;
        height: 1fr;
    }
    #servers > ContentSwitcher {
        width: 1fr;
        height: 1fr;
    }
    #servers > ContentSwitcher > TabPane {
        width: 1fr;
        height: 1fr;
        padding: 0;
    }
    Tab.flash {
        background: $error;
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("ctrl+pagedown,ctrl+tab", "next_tab", "Next", show=False, priority=True),
        Binding("ctrl+pageup,ctrl+shift+tab", "prev_tab", "Prev", show=False, priority=True),
        *(
            Binding(f"alt+{number},{OPTION_DIGITS[number - 1]}", f"jump({number})", show=False, priority=True)
            for number in range(1, 10)
        ),
        Binding("ctrl+n", "add_host", "Add server", priority=True),
        Binding("delete", "remove_host", "Remove server", show=False),
        Binding("ctrl+k", "switch_host", "Switch", priority=True),
        Binding("ctrl+t", "open_host", "Open", priority=True),
        Binding("ctrl+w", "close_tab", "Close", priority=True),
        Binding("ctrl+r", "refresh_now", "Refresh", priority=True),
        Binding("left_square_bracket", "section(-1)", "Section", show=False),
        Binding("right_square_bracket", "section(1)", "Section", show=False),
        Binding("f3", "section(-1)", "Section", show=False, priority=True),
        Binding("f4", "section(1)", "Section", show=False, priority=True),
        Binding("alt+t,dagger", "new_terminal", "Terminal", show=False, priority=True),
        Binding("alt+w,n_ary_summation", "close_terminal", "Terminal", show=False, priority=True),
        Binding("p", "pause_host", "Pause"),
        Binding("P", "pause_all", "Pause all", show=False),
        Binding("a", "add_key", "Add key", show=False),
        Binding("d", "toggle_host", "Disable", show=False),
        Binding("f2", "settings", "Settings"),
        Binding("f6", "credentials", "Credentials", priority=True),
        Binding("question_mark", "help", "Help"),
        Binding("q", "request_quit", "Quit"),
        Binding("ctrl+q", "request_quit", "Quit", show=False, priority=True),
        *ClipboardMixin.CLIPBOARD_BINDINGS,
    ]

    def __init__(
        self,
        specs: list[HostSpec],
        collectors,
        settings: Settings | None = None,
        session: Session | None = None,
        open_hosts: list[str] | None = None,
        persist_session: bool = True,
        store: HostStore | None = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        # Instant tab switches: no sliding underline or fade animations.
        self.animation_level = "none"
        self.sub_title = _("read-only server overview")
        self.specs = list(specs)
        self.collectors = collectors
        self.settings = settings or Settings()
        self.vault = Vault()  # identity cards, decrypted on first use
        self.session = session or Session()
        self.persist_session = persist_session
        self.config_path = config_path
        # A store from a previous run (language switch) keeps data and history.
        self.store = store or HostStore(self.specs)
        self.workers_by_host: dict[str, HostWorker] = {}
        self._poll_jobs: dict = {}
        self.apply_titles()
        disabled = set(load_prefs().get("disabled_hosts", []))
        for spec in self.specs:
            off = not spec.enabled or spec.name in disabled
            if off != self.store[spec.name].disabled:
                set_disabled(self.store[spec.name], off)

        names = [spec.name for spec in self.specs]
        self.pane_ids = {name: f"host-{index}" for index, name in enumerate(names)}
        self._pane_counter = len(names)
        self.host_by_pane = {pane: name for name, pane in self.pane_ids.items()}
        wanted = open_hosts if open_hosts is not None else names
        self.open_hosts = [name for name in wanted if name in self.pane_ids]
        self._labels: dict[str, tuple] = {}  # per open host: what its tab label was built from
        self._views: dict[str, HostView] = {}
        self._session_timer = None
        # Hosts that rejected our key: ask for the password once per run.
        self._asked: set[str] = set()
        self._ask_queue: list[str] = []

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def apply_titles(self) -> None:
        """Human names: F2 settings (prefs.json) win over `title` in hosts.toml."""
        titles = load_prefs().get("titles", {})
        titles = titles if isinstance(titles, dict) else {}
        for spec in self.specs:
            state = self.store[spec.name]
            title = titles.get(spec.name) or spec.title
            if title != state.title:
                state.title = title
                state.version += 1

    def interval_of(self, name: str) -> float:
        worker = self.workers_by_host.get(name)
        base = self.store[name].spec.interval or self.settings.interval
        if worker is not None and not worker.visible:
            return base * self.settings.hidden_interval_factor
        return base

    def _host_pane(self, name: str) -> TabPane:
        state = self.store[name]
        collector = self.collectors[name]

        async def check() -> str:
            return await collector.run_script(DETECT_SCRIPT)

        view = HostView(
            state,
            self.session.sections.get(name),
            terminal_command=(
                collector.terminal_command() if hasattr(collector, "terminal_command")
                else interactive_command(state.spec, self.settings.connect_timeout)
            ),
            check_runner=check,
        )
        return TabPane(tab_label(state, time.time(), self.interval_of(name)), view, id=self.pane_ids[name])

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        active = self.session.active
        initial = self.pane_ids[active] if active in self.open_hosts else FLEET_PANE
        with TabbedContent(id="servers", initial=initial):
            with TabPane(_("Local"), id=LOCAL_PANE):
                yield TerminalSection(local_shell_command(), cwd=str(Path.home()), id="local-terminal")
            with TabPane(_("Fleet({n})", n=len(self.specs)), id=FLEET_PANE):
                yield FleetView(self.store, self.interval_of)
            for name in self.open_hosts:
                yield self._host_pane(name)
        yield Footer()

    def on_mount(self) -> None:
        # Keep references: while a modal (help, palette) is open, self.query_one
        # searches that modal screen instead of the main one.
        self.servers = self.query_one("#servers", TabbedContent)
        self.fleet = self.query_one(FleetView)
        self.local = self.query_one("#local-terminal", TerminalSection)
        self._semaphore = asyncio.Semaphore(self.settings.max_parallel)
        for spec in self.specs:
            self._start_polling(spec)

        self._sync_visibility()
        self.set_interval(FRAME_SECONDS, self._frame)
        # Focusing a widget inside a pane activates that pane, so only focus Fleet
        # when it is the restored tab.
        if self.active_host is None:
            self.fleet.focus_table()

    def _start_polling(self, spec: HostSpec) -> None:
        worker = HostWorker(
            spec.name,
            self.collectors[spec.name],
            self.store,
            self._semaphore,
            self.settings,
            on_bad=self._host_went_bad,
        )
        self.workers_by_host[spec.name] = worker
        self._poll_jobs[spec.name] = self.run_worker(
            worker.run(), group="poll", name=f"poll:{spec.name}", exclusive=False
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @property
    def active_host(self) -> str | None:
        pane = self.servers.active
        return self.host_by_pane.get(pane)

    @property
    def on_local(self) -> bool:
        return self.servers.active == LOCAL_PANE

    def _active_terminal_section(self) -> TerminalSection | None:
        """The local terminal, or the Terminal section of the open host."""
        if self.on_local:
            return self.local
        view = self._active_view()
        if view is not None and view.section == "terminal":
            return view.terminal()
        return None

    def _active_view(self) -> HostView | None:
        name = self.active_host
        if name is None:
            return None
        view = self._views.get(name)
        if view is None or not view.is_attached:  # first look, or the tab was closed and reopened
            try:
                view = self.servers.query_one(f"#{self.pane_ids[name]} HostView", HostView)
            except Exception:  # pane is being removed
                return None
            self._views[name] = view
        return view

    def _frame(self) -> None:
        if self._ask_queue and len(self.screen_stack) == 1:
            self._prompt_password(self._ask_queue.pop(0))
        now = time.time()
        servers = self.servers
        for name in self.open_hosts:
            state = self.store[name]
            interval = self.interval_of(name)
            # The label only changes with new data, a pause, a rename or going stale.
            key = (state.version, state.paused, state.disabled, state.title, state.is_stale(now, interval))
            if self._labels.get(name) != key:
                self._labels[name] = key
                try:
                    servers.get_tab(self.pane_ids[name]).label = tab_label(state, now, interval)
                except Exception:  # tab is mounting / unmounting
                    self._labels.pop(name, None)

        view = self._active_view()
        if view is not None:
            view.refresh_data()
        elif self.servers.active == FLEET_PANE:
            self.fleet.refresh_data()

    def _sync_visibility(self) -> None:
        active = self.active_host
        for name, worker in self.workers_by_host.items():
            was_visible = worker.visible
            worker.visible = name == active
            state = self.store[name]
            if worker.visible and not was_visible and not state.paused:
                last = state.reading.at if state.reading else 0
                if time.time() - last > worker.interval:
                    worker.poke()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id != "servers":
            return
        self._sync_visibility()
        view = self._active_view()
        if view is not None:
            view.refresh_data(force=True)
        elif self.on_local:
            self.call_later(self.local.ensure_session)  # the shell starts on the first visit
        else:
            self.fleet.refresh_data(force=True)
        self._save_session()

    def on_host_view_section_changed(self, event: HostView.SectionChanged) -> None:
        self.session.sections[event.host] = event.section
        self._save_session()

    def on_fleet_view_open_host(self, event: FleetView.OpenHost) -> None:
        self.call_later(self.open_host, event.name)

    def _host_went_bad(self, name: str) -> None:
        reading = self.store[name].reading
        if reading and "permission denied" in (reading.error or "").lower():
            if name not in self._asked:
                self._asked.add(name)
                self._ask_queue.append(name)
            return
        if name == self.active_host:
            return
        detail = reading.error if reading and reading.error else _("critical findings")
        self.notify(f"{self.store[name].label}: {detail}", title=_("Host needs attention"), severity="error", timeout=6)
        if name in self.open_hosts:
            try:
                tab = self.servers.get_tab(self.pane_ids[name])
            except Exception:
                return
            tab.add_class("flash")
            self.set_timer(1.5, lambda: tab.remove_class("flash"))

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _save_session(self, now: bool = False) -> None:
        self.session.open = list(self.open_hosts)
        try:
            self.session.active = self.active_host
        except Exception:  # widgets already gone during shutdown
            pass
        if not self.persist_session:
            return
        if now:
            if self._session_timer is not None:
                self._session_timer.stop()
                self._session_timer = None
            self.session.save()
        elif self._session_timer is None:
            # Flipping through tabs writes the file once, not on every step.
            self._session_timer = self.set_timer(1.0, self._write_session)

    def _write_session(self) -> None:
        self._session_timer = None
        self.session.save()

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------

    def _pane_order(self) -> list[str]:
        return [LOCAL_PANE, FLEET_PANE, *(self.pane_ids[name] for name in self.open_hosts)]

    def _activate_index(self, index: int) -> None:
        order = self._pane_order()
        self.servers.active = order[index % len(order)]

    def _current_index(self) -> int:
        order = self._pane_order()
        active = self.servers.active
        return order.index(active) if active in order else 0

    def action_next_tab(self) -> None:
        self._activate_index(self._current_index() + 1)

    def action_prev_tab(self) -> None:
        self._activate_index(self._current_index() - 1)

    def action_jump(self, number: int) -> None:
        if number - 1 < len(self._pane_order()):
            self._activate_index(number - 1)

    async def open_host(self, name: str) -> None:
        servers = self.servers
        if name not in self.open_hosts:
            self.open_hosts.append(name)
            await servers.add_pane(self._host_pane(name))
        servers.active = self.pane_ids[name]

    async def action_close_tab(self) -> None:
        if self._terminal_focused():
            raise SkipAction()
        name = self.active_host
        if name is None:
            self.notify(_("This tab cannot be closed") if self.on_local else _("Fleet tab cannot be closed"), timeout=2)
            return
        index = self._current_index()
        self.open_hosts.remove(name)
        self._labels.pop(name, None)
        self._views.pop(name, None)
        await self.servers.remove_pane(self.pane_ids[name])
        self._activate_index(min(index, len(self._pane_order()) - 1))
        self._sync_visibility()
        self._save_session()

    def _palette(self, names: list[str], placeholder: str) -> None:
        specs = [spec for spec in self.specs if spec.name in names]
        hint = lambda name: (
            f"{_(self.store[name].level)} · {self.store[name].spec.target}"
            + ("" if name in self.open_hosts else " · " + _("closed"))
        )
        pick = lambda name: self.call_later(self.open_host, name)
        titles = {s.spec.name: s.label for s in self.store}
        self.push_screen(
            CommandPalette(providers=[hosts_provider(specs, pick, hint, titles)], placeholder=placeholder)
        )

    def _terminal_focused(self) -> bool:
        return isinstance(self.focused, TerminalView)

    def action_switch_host(self) -> None:
        if self._terminal_focused():
            raise SkipAction()  # Ctrl+K belongs to the shell there
        self._palette([spec.name for spec in self.specs], _("Host name, tag or address…"))

    def action_open_host(self) -> None:
        if self._terminal_focused():
            raise SkipAction()
        closed = [spec.name for spec in self.specs if spec.name not in self.open_hosts]
        if not closed:
            self.notify(_("All hosts are already open"), timeout=2)
            return
        self._palette(closed, _("Open a host tab…"))

    def action_section(self, delta: int) -> None:
        view = self._active_view()
        if view is not None:
            view.cycle_section(delta)

    async def action_new_terminal(self) -> None:
        section = self._active_terminal_section()
        if section is None:
            view = self._active_view()
            if view is None:
                raise SkipAction()
            view.show_section("terminal")
            return  # switching to the section already opens a session if there is none
        await section.new_session()

    async def action_close_terminal(self) -> None:
        section = self._active_terminal_section()
        if section is None:
            raise SkipAction()
        await section.close_session()

    # ------------------------------------------------------------------
    # Polling control
    # ------------------------------------------------------------------

    def action_refresh_now(self) -> None:
        if self._terminal_focused():
            raise SkipAction()
        for name, worker in self.workers_by_host.items():
            if not self.store[name].paused:
                worker.poke()
        self.notify(_("Polling all hosts…"), timeout=1)

    def _set_paused(self, name: str, paused: bool) -> None:
        self.store[name].paused = paused
        self.store[name].version += 1  # re-render the paused marker
        if not paused:
            self.workers_by_host[name].poke()

    def action_pause_host(self) -> None:
        if self.on_local:
            return
        name = self.active_host
        if name is None:
            self.action_pause_all()
            return
        paused = not self.store[name].paused
        self._set_paused(name, paused)
        self.notify(f"{name}: {_('paused') if paused else _('resumed')}", timeout=1)

    def _current_or_selected_host(self) -> str | None:
        """The open host, or the Fleet row under the cursor (nothing on the Local tab)."""
        if self.on_local:
            return None
        return self.active_host or self.fleet.selected_host()

    def action_toggle_host(self) -> None:
        name = self._current_or_selected_host()
        if name is None:
            return
        state = self.store[name]
        if not state.spec.enabled:
            self.notify(_("{name} is disabled in hosts.toml (enabled = false)", name=name), timeout=4)
            return
        set_disabled(state, not state.disabled)
        disabled = {s.spec.name for s in self.store if s.disabled}
        save_prefs({"disabled_hosts": sorted(disabled)})
        if not state.disabled:
            self.workers_by_host[name].poke()
        message = _("{name}: disabled, not polled", name=name) if state.disabled else _("{name}: enabled", name=name)
        self.notify(message, timeout=2)

    def action_pause_all(self) -> None:
        paused = not all(state.paused for state in self.store)
        for state in self.store:
            self._set_paused(state.spec.name, paused)
        self.notify(_("All hosts paused") if paused else _("All hosts resumed"), timeout=1)

    # ------------------------------------------------------------------
    # SSH key setup (the only write zir does, and only after a password)
    # ------------------------------------------------------------------

    def _prompt_password(self, name: str, error: str = "") -> None:
        spec = self.store[name].spec
        key = public_key_for(spec)
        if key is None:
            hint = _("a new key ~/.ssh/id_ed25519.pub")
        elif key.is_relative_to(Path.home()):
            hint = f"~/{key.relative_to(Path.home())}"
        else:
            hint = str(key)

        def done(password: str | None) -> None:
            if password:
                self.run_worker(self._install_key(name, password), group="keys", exclusive=False)

        self.push_screen(PasswordScreen(spec, hint, error), done)

    async def _install_key(self, name: str, password: str) -> None:
        spec = self.store[name].spec
        self.notify(_("{name}: adding SSH key…", name=name), timeout=3)
        try:
            public_key, created = await ensure_public_key(spec)
            await install_key(
                spec, password, public_key, connect_timeout=self.settings.connect_timeout
            )
        except KeySetupError as exc:
            self._prompt_password(name, str(exc))
            return
        if created:
            message = _("{name}: key added (created {key}), connecting…", name=name, key=public_key.name)
        else:
            message = _("{name}: key added, connecting…", name=name)
        self.notify(message, title=_("SSH key"), timeout=5)
        self.workers_by_host[name].poke()

    def on_db_access_requested(self, event: DbAccessRequested) -> None:
        event.stop()
        if self.active_host is not None:
            self._prompt_db_access(self.active_host)

    def _prompt_db_access(self, name: str, error: str = "") -> None:
        def done(result: tuple[str, str] | None) -> None:
            if result:
                self.run_worker(self._setup_db_access(name, *result), group="dbaccess", exclusive=False)

        self.push_screen(DbAccessScreen(self.store[name].label, error), done)

    async def _setup_db_access(self, name: str, user: str, password: str) -> None:
        self.notify(_("{name}: checking database access…", name=self.store[name].label), timeout=3)
        try:
            message = await setup_access(self.collectors[name], user, password)
        except (DbAccessError, ProbeError) as exc:
            self._prompt_db_access(name, str(exc))
            return
        self.notify(message, title=self.store[name].label, timeout=8)
        self.workers_by_host[name].poke()

    def action_add_key(self) -> None:
        name = self._current_or_selected_host()
        if name is None:
            self.notify(_("Open a host tab first"), timeout=2)
            return
        self._asked.add(name)
        self._prompt_password(name)

    # ------------------------------------------------------------------
    # Adding / removing servers
    # ------------------------------------------------------------------

    @property
    def hosts_path(self) -> Path:
        return self.config_path or DEFAULT_CONFIG

    def _refresh_fleet_label(self) -> None:
        self.servers.get_tab(FLEET_PANE).label = _("Fleet({n})", n=len(self.specs))

    async def _check_connection(self, spec: HostSpec) -> tuple[bool, str]:
        collector = Collector(spec, timeout=self.settings.timeout, connect_timeout=self.settings.connect_timeout)
        try:
            out = await collector.run_script("hostname; uname -sr")
        except ProbeError as exc:
            return False, str(exc)
        return True, " · ".join(line.strip() for line in out.splitlines() if line.strip())

    def action_add_host(self) -> None:
        if self._terminal_focused():
            raise SkipAction()  # Ctrl+N is "next history line" in the shell
        from .sshconfig import ssh_aliases

        def done(spec: HostSpec | None) -> None:
            if spec is not None:
                self.call_later(self.add_server, spec)

        self.push_screen(AddHostScreen({s.name for s in self.specs}, ssh_aliases(), self._check_connection), done)

    async def add_server(self, spec: HostSpec, save: bool = True) -> None:
        if save:
            try:
                add_host(self.hosts_path, spec)
            except (ConfigError, OSError) as exc:
                self.notify(str(exc), severity="error", timeout=6)
                return
        self.specs.append(spec)
        state = self.store.add(spec)
        state.title = spec.title
        self.pane_ids[spec.name] = pane = f"host-{self._pane_counter}"
        self._pane_counter += 1
        self.host_by_pane[pane] = spec.name
        self.collectors[spec.name] = build_collectors([spec], self.settings.timeout, self.settings.connect_timeout, False)[spec.name]
        self._start_polling(spec)
        self.fleet.add_host(state)
        self._refresh_fleet_label()
        await self.open_host(spec.name)
        self.notify(_("{name} added to {path}", name=state.label, path=self.hosts_path) if save
                    else _("{name} added", name=state.label), timeout=4)

    def action_remove_host(self) -> None:
        if self._terminal_focused():
            raise SkipAction()
        name = self._current_or_selected_host()
        if name is None:
            return
        state = self.store[name]
        in_config = self.config_path is not None or DEFAULT_CONFIG.exists()

        def done(confirmed: bool | None) -> None:
            if confirmed:
                self.call_later(self.remove_server, name)

        self.push_screen(ConfirmRemove(state.label, state.spec.target, in_config), done)

    async def remove_server(self, name: str) -> None:
        label = self.store[name].label
        try:
            removed = remove_host(self.hosts_path, name)
        except (ConfigError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=6)
            return
        job = self._poll_jobs.pop(name, None)
        if job is not None:
            job.cancel()
        self.workers_by_host.pop(name, None)
        if name in self.open_hosts:
            view = self.servers.query_one(f"#{self.pane_ids[name]} HostView", HostView)
            terminal = view.terminal()
            if terminal is not None:
                terminal.stop_all()
            index = self._current_index()
            self.open_hosts.remove(name)
            self._labels.pop(name, None)
            self._views.pop(name, None)
            await self.servers.remove_pane(self.pane_ids[name])
            self._activate_index(min(index, len(self._pane_order()) - 1))
        self.fleet.remove_host(name)
        self.specs = [spec for spec in self.specs if spec.name != name]
        self.store.remove(name)
        self.collectors.pop(name, None)
        self.host_by_pane.pop(self.pane_ids.pop(name), None)
        self._refresh_fleet_label()
        self._sync_visibility()
        self._save_session()
        self.notify(_("{name} removed", name=label) + ("" if removed else " · " + _("it was not in hosts.toml")), timeout=4)

    # ------------------------------------------------------------------
    # Settings / help / quit
    # ------------------------------------------------------------------

    def action_settings(self) -> None:
        def done(result: str | None) -> None:
            if result == RESTART:
                self.exit(RESTART)  # cli.py starts the UI again in the new language
            elif result == TITLES:
                self.apply_titles()  # tab labels and Fleet pick the new names up on the next frame

        hosts = [(spec.name, spec.target, spec.title) for spec in self.specs]
        self.push_screen(SettingsScreen(self.settings, self.config_path, hosts), done)

    def action_credentials(self) -> None:
        if isinstance(self.screen, CredentialManager):
            return
        self.push_screen(CredentialManager(self.vault), lambda _result: self._refocus_terminal())

    def _refocus_terminal(self) -> None:
        section = self._active_terminal_section()
        if section is not None:
            terminal = section.active_terminal()
            if terminal is not None:
                terminal.focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_request_quit(self) -> None:
        bad = [state.label for state in self.store if state.level in BAD_LEVELS]
        self.push_screen(ConfirmQuit(bad), lambda quit_: self.exit() if quit_ else None)

    def on_unmount(self) -> None:
        self._save_session(now=True)
