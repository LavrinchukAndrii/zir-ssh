"""Desktop integration: open a terminal when started from Finder, Linux menu entry."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

# Window size the terminal opened by zir.app gets (almost the whole screen).
WINDOW_COLUMNS = 197
WINDOW_ROWS = 51


def _bundle_executable() -> bool:
    return (
        sys.platform == "darwin"
        and getattr(sys, "frozen", False)
        and ".app/Contents/MacOS/" in sys.executable
    )


def started_from_app_bundle() -> bool:
    """macOS: double-clicked zir.app, so there is no terminal to draw in."""
    return _bundle_executable() and not sys.stdin.isatty()


def opened_from_app_bundle() -> bool:
    """macOS: the terminal window that reopen_in_terminal() opened for zir.app."""
    return _bundle_executable() and sys.stdin.isatty() and not os.environ.get("ZIR_IN_WINDOW")


def _terminal_env() -> str | None:
    """ZIR_TERMINAL, or SDECK_TERMINAL from before the rename."""
    return os.environ.get("ZIR_TERMINAL") or os.environ.get("SDECK_TERMINAL")


def open_own_window() -> int | None:
    """macOS: run zir in zir.app's own window, so the Dock shows zir, not Terminal.

    None means "use a terminal app instead": ZIR_TERMINAL asks for one, or
    the window parts (pywebview) are missing.
    """
    if _terminal_env():
        return None
    try:
        import webview  # noqa: F401

        from .window import run_window
    except ImportError:
        return None
    return run_window()


def resize_terminal(columns: int = WINDOW_COLUMNS, rows: int = WINDOW_ROWS) -> None:
    """Ask the terminal (Terminal.app, iTerm2, xterm) to resize its window; it clamps to the screen."""
    try:
        sys.stdout.write(f"\x1b[8;{rows};{columns}t")
        sys.stdout.flush()
    except OSError:
        pass


def reopen_in_terminal() -> int:
    """Run this same executable in a new terminal window (ZIR_TERMINAL=iTerm to change)."""
    terminal = (_terminal_env() or "Terminal")
    return subprocess.run(["open", "-a", terminal, sys.executable]).returncode


def started_in_legacy_console() -> bool:
    """Windows: the classic console (conhost), which redraws a full-colour UI
    slowly, lacks block glyphs and gives the app no paste events."""
    return (
        sys.platform == "win32"
        and getattr(sys, "frozen", False)
        and "WT_SESSION" not in os.environ
        and not os.environ.get("ZIR_NO_WT")
    )


def reopen_in_windows_terminal() -> bool:
    """Run this same executable in Windows Terminal, if it is installed."""
    wt = shutil.which("wt")
    if wt is None:
        return False
    try:
        subprocess.Popen([wt, "new-tab", "--title", "Zir SSH", sys.executable])
    except OSError:
        return False
    return True


def close_terminal_window() -> None:
    """macOS: close the Terminal window zir.app opened, once zir has exited."""
    if (_terminal_env() or "Terminal") != "Terminal":
        return
    try:
        tty = os.ttyname(0)
    except OSError:
        return
    script = f'tell application "Terminal" to close (every window whose tty of selected tab is "{tty}")'
    try:
        # Detached: it outlives zir and closes the window after the shell has finished.
        subprocess.Popen(
            ["sh", "-c", 'sleep 0.5; osascript -e "$0"', script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _executable() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("zir") or f"{sys.executable} -m zir.cli"


def install_desktop_entry() -> list[Path]:
    """Linux: add zir with its icon to the applications menu (per user, no sudo)."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    icon = data / "icons" / "hicolor" / "512x512" / "apps" / "zir.png"
    entry = data / "applications" / "zir.desktop"

    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(resources.files("zir").joinpath("assets/zir.png").read_bytes())

    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Zir SSH\n"
        "GenericName=Server monitor\n"
        "GenericName[uk]=Моніторинг серверів\n"
        "Comment=Read-only overview of your servers over SSH\n"
        "Comment[uk]=Огляд ваших серверів через SSH, лише для читання\n"
        f"Exec={_executable()}\n"
        f"Icon={icon}\n"
        "Terminal=true\n"
        "Categories=System;Monitor;Network;\n"
        "Keywords=ssh;server;monitoring;\n",
        encoding="utf-8",
    )
    entry.chmod(0o755)
    for tool in (["update-desktop-database", str(entry.parent)], ["gtk-update-icon-cache", "-q", str(data / "icons" / "hicolor")]):
        if shutil.which(tool[0]):
            subprocess.run(tool, capture_output=True)
    return [entry, icon]
