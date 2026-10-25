"""The Snippets section: "Check server" fills it with commands for what is installed."""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Static

from .. import snippets
from ..i18n import _


class SnippetsSection(Vertical):
    DEFAULT_CSS = """
    SnippetsSection { width: 1fr; height: 1fr; }
    SnippetsSection #snip-bar { height: 3; }
    SnippetsSection #snip-check { margin-right: 1; }
    SnippetsSection #snip-filter { width: 32; margin-right: 1; }
    SnippetsSection #snip-status { width: 1fr; height: 3; content-align: left middle; color: $text-muted; }
    SnippetsSection #snip-list { width: 1fr; height: 1fr; }
    SnippetsSection .snip-group { height: auto; margin-top: 1; padding: 0 1; text-style: bold; color: $accent; }
    SnippetsSection .snip-row { height: auto; padding: 0 1; }
    SnippetsSection .snip-row:hover { background: $boost; }
    SnippetsSection .snip-text { width: 1fr; height: auto; }
    #snip-list .snip-row Button {
        min-width: 12; height: 1; margin: 0 0 0 1;
        border: none; border-top: none; border-bottom: none;
    }
    SnippetsSection #snip-empty, SnippetsSection .snip-none { padding: 2 2; color: $text-muted; }
    """

    class Insert(Message):
        """Type this command into the host's terminal (without Enter)."""

        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    def __init__(self, host: str, runner: Callable[[], Awaitable[str]] | None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.host = host
        self.runner = runner
        self.data = snippets.load(host)
        self.commands: list[str] = []
        self._rows: list[tuple[Horizontal, Static, str]] = []  # row, its group title, text to filter on
        self._nothing: Static | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="snip-bar"):
            yield Button(_("Check server"), id="snip-check", variant="primary")
            yield Input(placeholder=_("filter…"), id="snip-filter")
            yield Static(id="snip-status")
        yield VerticalScroll(id="snip-list")

    def on_mount(self) -> None:
        self._show()

    # -- rendering -------------------------------------------------------------

    def _status(self) -> Text:
        if not self.data:
            return Text(_("Not checked yet. Press “Check server”: Zir SSH looks at what is installed (read-only) "
                          "and prepares commands for it."))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.data.get("checked_at", 0)))
        found = snippets.found_summary(self.data["facts"]) or "—"
        return Text.assemble(_("Checked {when}", when=when), " · ", _("found: "), (found, "bold"))

    def _show(self) -> None:
        """Rebuild the list (after loading or a check); the filter only hides rows."""
        self.query_one("#snip-status", Static).update(self._status())
        box = self.query_one("#snip-list", VerticalScroll)
        box.remove_children()
        self.commands = []
        self._rows = []
        self._nothing = None
        if not self.data:
            box.mount(Static(_("Commands appear here after the check. Insert types a command into the "
                               "Terminal tab without running it, so you can review it first."), id="snip-empty"))
            return
        widgets = []
        for group in snippets.build(self.data["facts"]):
            header = Static(group.title, classes="snip-group")
            widgets.append(header)
            for snippet in group.snippets:
                index = len(self.commands)
                self.commands.append(snippet.command)
                text = Text.assemble((snippet.title, "bold"), "\n", ("$ " + snippet.command, "cyan"))
                row = Horizontal(
                    Static(text, classes="snip-text"),
                    Button(_("Insert"), id=f"snip-{index}", variant="success"),
                    classes="snip-row",
                )
                self._rows.append((row, header, f"{snippet.title}\n{snippet.command}\n{group.title}".lower()))
                widgets.append(row)
        self._nothing = Static(_("Nothing matches the filter."), classes="snip-none")
        widgets.append(self._nothing)
        box.mount_all(widgets)
        self._filter()

    def _filter(self) -> None:
        if self._nothing is None:  # not checked yet: no list to filter
            return
        needle = self.query_one("#snip-filter", Input).value.strip().lower()
        shown: set[Static] = set()
        headers: set[Static] = set()
        for row, header, haystack in self._rows:
            row.display = not needle or needle in haystack
            headers.add(header)
            if row.display:
                shown.add(header)
        for header in headers:
            header.display = header in shown
        self._nothing.display = not shown

    # -- events -----------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "snip-filter":
            event.stop()
            self._filter()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == "snip-check":
            self.check()
        elif button_id.startswith("snip-") and button_id[5:].isdigit():
            index = int(button_id[5:])
            if index < len(self.commands):
                self.post_message(self.Insert(self.commands[index]))

    @work(exclusive=True, group="snippets-check")
    async def check(self) -> None:
        if self.runner is None:
            return
        if not self.is_mounted:
            await self.app.wait_for_refresh()
        button = self.query_one("#snip-check", Button)
        status = self.query_one("#snip-status", Static)
        button.disabled = True
        status.update(Text(_("Checking installed services…"), style="bold"))
        try:
            raw = await self.runner()
        except Exception as exc:  # noqa: BLE001 - shown to the user, the app keeps running
            status.update(Text(_("Check failed: {error}", error=exc), style="red"))
            return
        finally:
            button.disabled = False
        self.data = snippets.save(self.host, snippets.parse_facts(raw))
        self._show()
