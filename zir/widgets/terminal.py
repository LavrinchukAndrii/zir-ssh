"""A real interactive SSH session inside a host tab.

pyte emulates the terminal (xterm, 256 colours), `ssh -t` runs on a
pseudo-terminal (zir.ptysession: ptyprocess on macOS / Linux, ConPTY on
Windows). Output is fed as it arrives, but the widget repaints at most 60 times
a second and only the lines that changed, so a busy `tail -f` does not slow
the rest of the UI down.

Lines that scroll off the top are kept as plain text plus colour runs (not
pyte's per-cell objects): 5000 lines of history cost a few MB, not ~90 MB.
"""

from __future__ import annotations

import functools
import importlib.util
import math
import os
import re
from collections import deque

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.geometry import Region
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from ..clipboard import copy_text, paste_text
from ..ptysession import BACKEND, PtySession

# pyte and the pty library are imported on the first session (most runs never
# open a terminal).
SUPPORTED = BACKEND is not None and importlib.util.find_spec("pyte") is not None
HISTORY = 5000  # lines of scrollback per session
DECCKM = 1 << 5  # pyte stores private mode 1 (application cursor keys) shifted by 5
# Mouse tracking modes a remote program can switch on (also stored shifted by 5).
MOUSE_CLICKS, MOUSE_DRAG, MOUSE_ANY, MOUSE_SGR = (m << 5 for m in (1000, 1002, 1003, 1006))
FRAME = 1 / 60
# The text left of the cursor when a program waits for a password:
# "Password:", "Пароль:", "[sudo] password for lvl:", "Enter passphrase for key '…':".
PASSWORD_PROMPT = re.compile(r"(password|passphrase|пароль|парольна фраза)[^:\n]{0,60}:\s*$", re.IGNORECASE)

KEYS = {
    "enter": "\r", "tab": "\t", "shift+tab": "\x1b[Z", "backspace": "\x7f", "escape": "\x1b",
    "space": " ", "ctrl+space": "\x00", "ctrl+@": "\x00",
    "ctrl+backslash": "\x1c", "ctrl+circumflex_accent": "\x1e", "ctrl+underscore": "\x1f",
    "ctrl+tab": "\t", "ctrl+shift+tab": "\x1b[Z", "alt+enter": "\x1b\r", "alt+backspace": "\x1b\x7f",
}
# Keys with xterm modifier variants: "\x1b[1;<mod>X" or "\x1b[<n>;<mod>~".
CURSOR_KEYS = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
TILDE_KEYS = {"insert": 2, "delete": 3, "pageup": 5, "pagedown": 6, "f5": 15, "f6": 17, "f7": 18,
              "f8": 19, "f9": 20, "f10": 21, "f11": 23, "f12": 24}
SS3_KEYS = {"f1": "P", "f2": "Q", "f3": "R", "f4": "S"}
MODIFIERS = {"shift": 1, "alt": 2, "ctrl": 4}
ARROWS = {"up": "A", "down": "B", "right": "C", "left": "D"}
# Ctrl+] opens zir's menu from the terminal; every other key goes to the shell / mc.
MENU_KEY = "ctrl+right_square_bracket"
# Classic xterm palette as explicit RGB: named ANSI colours would be remapped by
# Textual's own ANSI theme (Monokai turns blue into violet, e.g. mc panels).
COLORS = {
    "default": None,
    "black": "#000000", "red": "#cd0000", "green": "#00cd00", "brown": "#cdcd00",
    "blue": "#0000ee", "magenta": "#cd00cd", "cyan": "#00cdcd", "white": "#e5e5e5",
    "brightblack": "#7f7f7f", "brightred": "#ff0000", "brightgreen": "#00ff00",
    "brightbrown": "#ffff00", "brightblue": "#5c5cff", "brightmagenta": "#ff00ff",
    "brightcyan": "#00ffff", "brightwhite": "#ffffff",
}


def _color(name: str) -> str | None:
    if name in COLORS:
        return COLORS[name]
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return "#" + name
    return name


_STYLES: dict[tuple, Style] = {}


def _style(attrs: tuple, flip: bool) -> Style:
    """Rich style of a cell from pyte's attributes (fg, bg, bold, italics, underscore,
    strikethrough, reverse, …); `flip` inverts it (cursor, selection). Cached: a
    screen has only a handful of different styles."""
    key = (attrs, flip)
    style = _STYLES.get(key)
    if style is None:
        fg, bg, bold, italics, underscore, strikethrough, reverse = attrs[:7]
        style = Style(color=_color(fg), bgcolor=_color(bg), bold=bold, italic=italics,
                      underline=underscore, strike=strikethrough, reverse=reverse != flip)
        if len(_STYLES) > 4096:
            _STYLES.clear()
        _STYLES[key] = style
    return style


def freeze(line) -> tuple:
    """A screen line as (cells, runs): the text of each cell (a str when every cell
    holds one character, else a tuple) and (length, attributes) runs."""
    if not line:
        return ("", ())
    width = max(line) + 1
    chars = [line[x] for x in range(width)]
    datas = [char.data for char in chars]
    cells = "".join(datas) if min(map(len, datas)) == max(map(len, datas)) == 1 else tuple(datas)
    runs = []
    start, attrs = 0, chars[0][1:]
    for x in range(1, width):
        current = chars[x][1:]
        if current != attrs:
            runs.append((x - start, attrs))
            start, attrs = x, current
    runs.append((width - start, attrs))
    return (cells, tuple(runs))


@functools.cache
def _pty():
    import pyte
    from pyte.screens import Margins

    class ScrollbackScreen(pyte.Screen):
        """pyte screen that keeps the lines scrolled off the top (compactly)."""

        def __init__(self, columns: int, lines: int, history: int) -> None:
            self.scrollback: deque[tuple] = deque(maxlen=history)
            super().__init__(columns, lines)

        def index(self) -> None:
            top, bottom = self.margins or Margins(0, self.lines - 1)
            if self.cursor.y == bottom:
                self.scrollback.append(freeze(self.buffer[top]))
            super().index()

        def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
            super().erase_in_display(how, *args, **kwargs)
            if how == 3:  # `clear` also drops the scrollback
                self.scrollback.clear()

        def reset(self) -> None:
            super().reset()
            self.scrollback.clear()

    return pyte, ScrollbackScreen


def encode_mouse(button: int, x: int, y: int, press: bool, sgr: bool) -> str:
    """xterm mouse report; x/y are 0-based cells."""
    if sgr:
        return f"\x1b[<{button};{x + 1};{y + 1}{'M' if press else 'm'}"
    code = button if press else 3 | (button & ~3 & ~64)  # X10 has no per-button release
    return "\x1b[M" + chr(32 + code) + chr(32 + min(x + 1, 223)) + chr(32 + min(y + 1, 223))


def _special_key(key: str, app_cursor: bool) -> str | None:
    """Cursor, editing and function keys, with xterm modifier codes (mc, vim, htop)."""
    *mods, base = key.split("+")
    if base.startswith("f") and base[1:].isdigit() and 13 <= int(base[1:]) <= 24:
        mods, base = [*mods, "shift"], f"f{int(base[1:]) - 12}"  # Textual names Shift+F1..F12 F13..F24
    if any(m not in MODIFIERS for m in mods):
        return None
    code = 1 + sum(MODIFIERS[m] for m in set(mods))
    if base in CURSOR_KEYS:
        if code > 1:
            return f"\x1b[1;{code}{CURSOR_KEYS[base]}"
        if base in ARROWS:
            return ("\x1bO" if app_cursor else "\x1b[") + CURSOR_KEYS[base]
        return "\x1b[" + CURSOR_KEYS[base]
    if base in SS3_KEYS:
        return f"\x1b[1;{code}{SS3_KEYS[base]}" if code > 1 else "\x1bO" + SS3_KEYS[base]
    if base in TILDE_KEYS:
        return f"\x1b[{TILDE_KEYS[base]};{code}~" if code > 1 else f"\x1b[{TILDE_KEYS[base]}~"
    return None


def encode_key(event: events.Key, app_cursor: bool) -> str | None:
    key = event.key
    if key == MENU_KEY:
        return None  # zir's own menu, the only key the terminal does not get
    if key in KEYS:
        return KEYS[key]
    special = _special_key(key, app_cursor)
    if special is not None:
        return special
    if key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
        return chr(ord(key[5]) - 96)
    if key.startswith("alt+"):  # Esc + the key (mc: Alt+T listing mode, Alt+O, …; bash: Alt+B / Alt+F)
        rest = key[4:]
        char = event.character[-1] if event.character else (rest if len(rest) == 1 else None)
        if char is None and rest.startswith("shift+") and len(rest) == 7:
            char = rest[6].upper()
        return "\x1b" + char if char else None
    if event.character and event.is_printable:
        return event.character
    return None


def _start_error(command: list[str], exc: Exception) -> str:
    return f"[{command[0] if command else '?'}: {exc}]"


class TerminalView(Widget, can_focus=True):
    ALLOW_SELECT = False  # own selection below, Textual's would fight it
    DEFAULT_CSS = """
    TerminalView {
        width: 1fr;
        height: 1fr;
        background: #0c0f14;
        color: #d6dae3;
    }
    TerminalView:focus { background: #0c0f14; }
    """

    class Started(Message):
        def __init__(self, terminal: "TerminalView") -> None:
            super().__init__()
            self.terminal = terminal

    class Exited(Message):
        def __init__(self, terminal: "TerminalView", code: int | None) -> None:
            super().__init__()
            self.terminal = terminal
            self.code = code

    class PasswordPrompt(Message):
        """The program in the terminal asks for a password (once per prompt)."""

        def __init__(self, terminal: "TerminalView") -> None:
            super().__init__()
            self.terminal = terminal

    def __init__(self, command: list[str], cwd: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.command = command
        self.cwd = cwd
        self._prompt_seen: tuple[int, str] | None = None
        self.process = None
        self.screen_buf = None
        self.stream = None
        self.exit_code: int | None = None
        self._strips: dict[int, Strip] = {}
        self._offset = 0  # lines scrolled back into the history; 0 = the live screen
        self._cursor_at: tuple[int, int, bool] | None = None  # (y, x, hidden) last painted
        self._repaint_pending = False
        self._got_output = False
        self._pending_paste: list[str] = []
        self._select_from: tuple[int, int] | None = None   # (y, x) where the drag started
        self._selection: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._mouse_button: int | None = None
        self._local_select = False  # Shift+drag: select even if the program took the mouse

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.isalive()

    def start(self) -> None:
        if not SUPPORTED or self.running:
            return
        pyte, ScrollbackScreen = _pty()
        cols, rows = max(self.size.width, 20), max(self.size.height, 5)
        self.screen_buf = ScrollbackScreen(cols, rows, HISTORY)
        self.stream = pyte.ByteStream(self.screen_buf)
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        try:
            self.process = PtySession(self.command, rows, cols, env, self.cwd, self._output, self._ended)
        except Exception as exc:  # noqa: BLE001 - e.g. ssh.exe missing: shown in the terminal
            self.process = None
            self.exit_code = None
            self.note(_start_error(self.command, exc))
            self.post_message(self.Exited(self, None))
            return
        self.exit_code = None
        self._got_output = False
        self._offset = 0
        self._cursor_at = None
        self._strips.clear()
        self.post_message(self.Started(self))
        self.refresh()

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            process.stop()

    def on_unmount(self) -> None:
        self.stop()

    def _ended(self, code: int | None) -> None:
        self.process = None
        self.exit_code = code
        self.post_message(self.Exited(self, code))

    def _output(self, data: bytes) -> None:
        self._feed(data)
        if not self._got_output:
            self._got_output = True
            for text in self._pending_paste:
                self.write(text)
            self._pending_paste.clear()

    def _feed(self, data: bytes) -> None:
        self.stream.feed(data)
        if self._offset:  # new output brings the view back to the live screen
            self._offset = 0
            self._strips.clear()
            self.refresh()
        self._schedule_repaint()

    def _schedule_repaint(self) -> None:
        if not self._repaint_pending:
            self._repaint_pending = True
            self.set_timer(FRAME, self._repaint)

    def _repaint(self) -> None:
        self._repaint_pending = False
        screen = self.screen_buf
        if screen is None:
            self.refresh()
            return
        dirty = set(screen.dirty)
        screen.dirty.clear()
        cursor = (screen.cursor.y, screen.cursor.x, screen.cursor.hidden)
        if cursor != self._cursor_at:
            dirty.add(cursor[0])
            if self._cursor_at is not None:
                dirty.add(self._cursor_at[0])
            self._cursor_at = cursor
        for y in dirty:
            self._strips.pop(y + self._offset, None)
        self._check_password_prompt()
        if len(dirty) >= screen.lines or self._offset:
            self.refresh()
        elif dirty:
            # Only the changed lines: `tail -f` repaints one line, not the whole terminal.
            width = self.size.width
            self.refresh(*(Region(0, y, width, 1) for y in dirty))

    # -- scrollback ------------------------------------------------------

    def _scroll(self, lines: int) -> None:
        """Move the view `lines` back into the history (negative: towards the live screen)."""
        screen = self.screen_buf
        offset = max(0, min(self._offset + lines, len(screen.scrollback)))
        if offset != self._offset:
            self._offset = offset
            self._strips.clear()
            self.refresh()

    def _page(self, up: bool) -> None:
        """Half a screen, as Shift+PgUp / PgDn and the wheel always moved."""
        step = int(math.ceil(self.screen_buf.lines * 0.5))
        self._scroll(step if up else -step)

    def _row(self, y: int):
        """Line `y` of the view: a frozen history line, or the live pyte line."""
        screen = self.screen_buf
        if self._offset:
            index = len(screen.scrollback) - self._offset + y
            if index < len(screen.scrollback):
                return screen.scrollback[index]
            y = index - len(screen.scrollback)
        return screen.buffer[y]

    def _cells(self, y: int) -> list[str]:
        """The text of every cell of view line `y`, as wide as the screen."""
        row, columns = self._row(y), self.screen_buf.columns
        if isinstance(row, tuple):
            cells = list(row[0][:columns])
            return cells + [" "] * (columns - len(cells))
        return [row[x].data for x in range(columns)]

    def text_before_cursor(self) -> str:
        screen = self.screen_buf
        if screen is None:
            return ""
        row = screen.buffer[screen.cursor.y]
        return "".join(row[x].data for x in range(min(screen.cursor.x, screen.columns)))

    def _check_password_prompt(self) -> None:
        text = self.text_before_cursor()
        if not self.running or not PASSWORD_PROMPT.search(text):
            self._prompt_seen = None
            return
        seen = (self.screen_buf.cursor.y, text)
        if seen != self._prompt_seen:
            self._prompt_seen = seen
            self.post_message(self.PasswordPrompt(self))

    def submit_secret(self, text: str) -> None:
        """Answer a password prompt: type the text and press Enter."""
        self._to_bottom()
        self.write(text + "\r")
        self.focus()

    # -- input -----------------------------------------------------------

    def write(self, text: str) -> None:
        if self.running:
            self.process.write(text)

    def note(self, text: str) -> None:
        """Print a dim line into the terminal (not sent to the server)."""
        if self.stream is not None:
            self._feed(("\r\n\x1b[2m" + text + "\x1b[0m\r\n").encode())

    def paste(self, text: str) -> None:
        """Type text at the prompt without pressing Enter (snippets)."""
        if not self.running:
            self.start()
        if self._got_output:
            self.write(text)
        else:
            self._pending_paste.append(text)  # ssh is still connecting
        self.focus()

    def _to_bottom(self) -> None:
        if self.screen_buf is not None and self._offset:
            self._offset = 0
            self._strips.clear()
            self.refresh()

    def on_key(self, event: events.Key) -> None:
        if self.screen_buf is None:
            return
        if event.key in ("shift+pageup", "shift+pagedown"):
            event.stop()
            event.prevent_default()
            self._page(up=event.key == "shift+pageup")
            return
        if not self.running:
            if event.key == "enter":
                event.stop()
                self.start()
            return
        data = encode_key(event, DECCKM in self.screen_buf.mode)
        if data is None:
            return
        event.stop()
        event.prevent_default()
        self._set_selection(None)
        self._to_bottom()
        self.write(data)

    def on_paste(self, event: events.Paste) -> None:
        event.stop()
        self.write(event.text)

    # -- mouse -------------------------------------------------------------
    # When the remote program asked for the mouse (htop, mc, vim, tmux), events
    # are reported to it, unless Shift is held. Otherwise dragging selects text
    # (copied on release), double / triple click copies a word / line, right
    # click pastes, and the wheel scrolls the history.

    @property
    def program_mouse(self) -> bool:
        """The remote program asked for mouse events."""
        mode = self.screen_buf.mode if self.screen_buf is not None else ()
        return self.running and any(m in mode for m in (MOUSE_CLICKS, MOUSE_DRAG, MOUSE_ANY))

    @property
    def mouse_reporting(self) -> bool:
        return self.program_mouse and not self._local_select

    def _report(self, button: int, event: events.MouseEvent, press: bool = True) -> None:
        if event.shift:
            button |= 4
        if event.meta:
            button |= 8
        if event.ctrl:
            button |= 16
        self.write(encode_mouse(button, max(event.x, 0), max(event.y, 0), press, MOUSE_SGR in self.screen_buf.mode))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.focus()
        if self.screen_buf is None:
            return
        event.stop()
        button = {1: 0, 2: 1, 3: 2}.get(event.button, 0)
        self._local_select = event.shift and event.button == 1 and self.program_mouse
        if self.mouse_reporting:
            self._mouse_button = button
            self.capture_mouse()
            self._report(button, event)
            return
        if event.button == 3:  # right click: paste
            text = paste_text(self.app)
            if text:
                self._to_bottom()
                self.write(text)
            return
        if event.button == 1:
            self._select_from = (event.y, event.x)
            self._set_selection(None)
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.screen_buf is None:
            return
        if self.mouse_reporting:
            mode = self.screen_buf.mode
            held = self._mouse_button is not None
            if MOUSE_ANY in mode or (MOUSE_DRAG in mode and held):
                event.stop()
                self._report((self._mouse_button if held else 3) | 32, event)
            return
        if self._select_from is not None:
            event.stop()
            self._set_selection((self._select_from, (event.y, event.x)))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.screen_buf is None:
            return
        self.release_mouse()
        if self.mouse_reporting:
            event.stop()
            self._report(self._mouse_button if self._mouse_button is not None else 0, event, press=False)
            self._mouse_button = None
            return
        if self._select_from is not None:
            event.stop()
            self._select_from = None
            self._local_select = False
            text = self.selected_text()
            if text:
                copy_text(self.app, text)
            else:
                self._set_selection(None)

    def on_click(self, event: events.Click) -> None:
        if self.screen_buf is None or event.button != 1 or event.chain < 2:
            return
        if self.program_mouse and not event.shift:
            return
        event.stop()
        y, x = event.y, event.x
        if not 0 <= y < self.screen_buf.lines:
            return
        if event.chain == 2:
            span = self._word_at(y, x)
            if span is None:
                return
            self._set_selection(((y, span[0]), (y, span[1])))
        else:
            self._set_selection(((y, 0), (y, self.screen_buf.columns - 1)))
        text = self.selected_text()
        if text:
            copy_text(self.app, text)

    def _word_at(self, y: int, x: int) -> tuple[int, int] | None:
        cells = self._cells(y)
        last = self.screen_buf.columns - 1
        blank = lambda i: not cells[i].strip()  # noqa: E731
        if not 0 <= x <= last or blank(x):
            return None
        start, end = x, x
        while start > 0 and not blank(start - 1):
            start -= 1
        while end < last and not blank(end + 1):
            end += 1
        if start == end:  # a one-character selection reads as none, see _selected_cells
            return None
        return start, end

    def _wheel(self, event: events.MouseEvent, up: bool) -> None:
        if self.screen_buf is None:
            return
        event.stop()
        if self.mouse_reporting:
            self._report(64 if up else 65, event)
            return
        self._page(up)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, up=False)

    # -- selection -------------------------------------------------------------

    def _ordered(self):
        if self._selection is None:
            return None
        start, end = sorted(self._selection)
        return start, end

    def _set_selection(self, selection) -> None:
        if selection == self._selection:
            return
        # Only the lines inside the old or the new selection change.
        rows = set()
        for chosen in (self._selection, selection):
            if chosen is not None:
                (y0, _x0), (y1, _x1) = sorted(chosen)
                rows.update(range(y0, y1 + 1))
        self._selection = selection
        for y in rows:
            self._strips.pop(y, None)
        width = self.size.width
        self.refresh(*(Region(0, y, width, 1) for y in rows))

    def _selected_cells(self, y: int) -> tuple[int, int] | None:
        ordered = self._ordered()
        if ordered is None:
            return None
        (y0, x0), (y1, x1) = ordered
        if not y0 <= y <= y1 or (y0, x0) == (y1, x1):
            return None
        start = x0 if y == y0 else 0
        end = x1 if y == y1 else self.screen_buf.columns - 1
        return start, end

    def selected_text(self) -> str:
        ordered = self._ordered()
        if ordered is None or self.screen_buf is None:
            return ""
        (y0, _x0), (y1, _x1) = ordered
        lines = []
        for y in range(y0, min(y1, self.screen_buf.lines - 1) + 1):
            cells = self._selected_cells(y)
            if cells is None:
                continue
            text = self._cells(y)
            lines.append("".join(text[x] for x in range(cells[0], cells[1] + 1)).rstrip())
        return "\n".join(lines).strip("\n")

    def on_resize(self, event: events.Resize) -> None:
        if self.screen_buf is None:
            return
        cols, rows = max(event.size.width, 20), max(event.size.height, 5)
        self.screen_buf.resize(rows, cols)
        if self.running:
            self.process.resize(rows, cols)
        self._offset = min(self._offset, len(self.screen_buf.scrollback))
        self._strips.clear()

    def on_focus(self) -> None:
        self._refresh_cursor_line()

    def on_blur(self) -> None:
        self._refresh_cursor_line()

    def _refresh_cursor_line(self) -> None:
        screen = self.screen_buf
        if screen is None:
            self.refresh()
            return
        y = screen.cursor.y + self._offset
        self._strips.pop(y, None)
        self.refresh(Region(0, y, self.size.width, 1))

    # -- rendering ---------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        screen = self.screen_buf
        width = self.size.width
        if screen is None or y >= screen.lines:
            return Strip.blank(width)
        cursor_x = None
        if self.has_focus and not screen.cursor.hidden and screen.cursor.y + self._offset == y:
            cursor_x = screen.cursor.x
        if cursor_x is None and y in self._strips:
            return self._strips[y]

        row = self._row(y)
        cells, runs = row if isinstance(row, tuple) else freeze(row)
        selected = self._selected_cells(y)
        segments = _segments(cells, runs, min(screen.columns, width), screen.default_char[1:],
                             selected, cursor_x)
        strip = Strip(segments).adjust_cell_length(width)
        if cursor_x is None:
            self._strips[y] = strip
        return strip


def _segments(cells, runs, width: int, blank: tuple, selected: tuple[int, int] | None,
              cursor_x: int | None) -> list[Segment]:
    """Segments of one line, `width` cells wide: each run of equal attributes is one
    segment, split where the selection or the cursor inverts the colours."""
    flips = set()
    if selected is not None:
        flips.update((selected[0], selected[1] + 1))
    if cursor_x is not None:
        flips.update((cursor_x, cursor_x + 1))

    def inverted(x: int) -> bool:
        inside = selected is not None and selected[0] <= x <= selected[1]
        return inside != (x == cursor_x)

    def text(start: int, end: int) -> str:
        if isinstance(cells, str):
            chunk = cells[start:end]
        else:
            chunk = "".join(data or " " for data in cells[start:end])
        return chunk + " " * (end - start - (min(end, len(cells)) - min(start, len(cells))))

    segments: list[Segment] = []
    x = 0
    # Past the end of the stored text the line is blank (default colours).
    for length, attrs in (*runs, (max(width - sum(n for n, _a in runs), 0), blank)):
        end = min(x + length, width)
        while x < end:
            cut = min((f for f in flips if x < f < end), default=end)
            style = _style(attrs, inverted(x))
            if segments and segments[-1].style is style:
                segments[-1] = Segment(segments[-1].text + text(x, cut), style)
            else:
                segments.append(Segment(text(x, cut), style))
            x = cut
        if x >= width:
            break
    return segments
