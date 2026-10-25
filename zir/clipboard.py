"""System clipboard for the whole app.

Textual copies with OSC 52 only, which macOS Terminal (where zir.app opens)
ignores. Here the system tool (pbcopy, wl-copy, xclip, clip.exe) is run as
well, on Windows the clipboard API is called directly; OSC 52 stays for zir
started over SSH on another machine.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from textual import events
from textual.actions import SkipAction
from textual.binding import Binding


@functools.cache
def _clipboard_tools() -> tuple[list[str] | None, list[str] | None]:
    if sys.platform == "darwin":
        return ["pbcopy"], ["pbpaste"]
    if shutil.which("wl-copy"):
        return ["wl-copy"], ["wl-paste", "--no-newline"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("clip.exe"):  # WSL
        return ["clip.exe"], None
    return None, None


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _win32_api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    return ctypes, user32, kernel32


def _win32_open(user32) -> bool:
    import time

    for _attempt in range(10):  # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _win32_copy(text: str) -> None:
    """Windows: put Unicode text on the clipboard (clip.exe garbles Cyrillic)."""
    ctypes, user32, kernel32 = _win32_api()
    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not _win32_open(user32):
        kernel32.GlobalFree(handle)
        return
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)  # the clipboard owns it only on success
    finally:
        user32.CloseClipboard()


def _win32_paste() -> str:
    ctypes, user32, kernel32 = _win32_api()
    if not _win32_open(user32):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def system_copy(text: str) -> None:
    if sys.platform == "win32":
        try:
            _win32_copy(text)
        except OSError:
            pass
        return
    copy, _paste = _clipboard_tools()
    if copy:
        # pbcopy / xclip take a few ms: not on the UI thread. One worker keeps the order.
        _copier().submit(_run_copy, copy, text)


@functools.cache
def _copier() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="zir-clipboard")


def _run_copy(command: list[str], text: str) -> None:
    try:
        subprocess.run(command, input=text.encode(), timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def copy_text(app, text: str, notify: bool = True) -> None:
    """Copy to the clipboard and say how much was copied."""
    app.copy_to_clipboard(text)
    if notify and text:
        app.notify(copied_message(len(text)), timeout=1.5)


def paste_text(app) -> str:
    if sys.platform == "win32":
        try:
            return _win32_paste().replace("\r\n", "\n") or getattr(app, "_clipboard", "") or ""
        except OSError:
            return getattr(app, "_clipboard", "") or ""
    _copy, paste = _clipboard_tools()
    if paste:
        try:
            return subprocess.run(paste, capture_output=True, timeout=2, check=False).stdout.decode(errors="replace")
        except (OSError, subprocess.SubprocessError):
            pass
    return getattr(app, "_clipboard", "") or ""


def copied_message(count: int) -> str:
    from .i18n import _n

    return _n("Copied {n} character", "Copied {n} characters", count)


def _plain(cell) -> str:
    return cell.plain if hasattr(cell, "plain") else str(cell)


class ClipboardMixin:
    """Mouse selection is copied on release; Ctrl+C / y copies the table row."""

    CLIPBOARD_BINDINGS = [Binding("ctrl+c,y", "copy_row", "Copy", show=False)]

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        system_copy(text)

    @property
    def clipboard(self) -> str:
        """Ctrl+V in Input / TextArea reads this: the system clipboard, not only
        what was copied inside zir (the Windows console sends no paste event)."""
        return paste_text(self)

    def on_text_selected(self, event: events.TextSelected) -> None:
        text = self.screen.get_selected_text()
        if text:
            copy_text(self, text)

    def action_copy_row(self) -> None:
        from textual.widgets import DataTable

        table = self.focused
        if not isinstance(table, DataTable) or table.row_count == 0:
            raise SkipAction()
        if self.screen.get_selected_text():
            raise SkipAction()  # Ctrl+C copies the mouse selection first
        row = table.get_row_at(table.cursor_row)
        copy_text(self, "\t".join(_plain(cell).strip() for cell in row))
