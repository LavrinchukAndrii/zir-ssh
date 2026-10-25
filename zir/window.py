"""macOS: zir.app's own window, a terminal (xterm.js in a WebView) that runs zir.

Without it zir.app had to open Terminal, so the Dock showed Terminal instead
of zir. The window runs this same executable in a pty and passes bytes both
ways; the TUI itself does not know the difference.
"""

from __future__ import annotations

import codecs
import json
import os
import select
import subprocess
import sys
import threading
import time
from importlib import resources
from typing import Callable

from .desktop import WINDOW_COLUMNS, WINDOW_ROWS

CELL_WIDTH = 7.8  # Menlo 13px
CELL_HEIGHT = 17
FLUSH_INTERVAL = 0.016
FLUSH_BYTES = 64 * 1024

_SCRIPTS = ("xterm.js", "addon-fit.js", "addon-unicode11.js", "addon-webgl.js")


def child_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-c", "from zir.cli import main; raise SystemExit(main())"]


def _login_path(shell: str) -> str | None:
    """PATH of the user's login shell: apps started from the Dock get only /usr/bin:/bin."""
    try:
        result = subprocess.run(
            [shell, "-l", "-c", 'printf %s "$PATH"'],
            capture_output=True,
            text=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["ZIR_IN_WINDOW"] = "1"
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["TERM_PROGRAM"] = "zir"
    if not env.get("LANG"):
        env["LANG"] = "en_US.UTF-8"
    shell = env.get("SHELL") or "/bin/zsh"
    env["SHELL"] = shell
    path = _login_path(shell)
    if path:
        env["PATH"] = path
    return env


class PtyBridge:
    """Runs a command in a pty; its output goes to `on_output` in small batches."""

    def __init__(
        self,
        command: list[str],
        on_output: Callable[[str], None],
        on_exit: Callable[[int], None],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = command
        self.on_output = on_output
        self.on_exit = on_exit
        self.env = env
        self.cwd = cwd
        self.process = None
        self._lock = threading.Lock()

    def start(self, cols: int, rows: int) -> None:
        from ptyprocess import PtyProcess

        self.process = PtyProcess.spawn(
            self.command, dimensions=(rows, cols), env=self.env, cwd=self.cwd
        )
        threading.Thread(target=self._read_loop, name="zir-pty", daemon=True).start()

    def _read_loop(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer: list[bytes] = []
        size = 0
        last_flush = time.monotonic()
        fd = self.process.fd

        while True:
            timeout = FLUSH_INTERVAL if buffer else None
            try:
                readable = select.select([fd], [], [], timeout)[0]
            except (OSError, ValueError):
                break
            if readable:
                try:
                    data = os.read(fd, 65536)
                except OSError:  # EIO: the child closed the pty
                    data = b""
                if not data:
                    break
                buffer.append(data)
                size += len(data)
            now = time.monotonic()
            if buffer and (size >= FLUSH_BYTES or now - last_flush >= FLUSH_INTERVAL or not readable):
                self._emit(decoder.decode(b"".join(buffer)))
                buffer.clear()
                size = 0
                last_flush = now
        self._emit(decoder.decode(b"".join(buffer), final=True))
        self.on_exit(self._exit_status())

    def _emit(self, text: str) -> None:
        if text:
            self.on_output(text)

    def _exit_status(self) -> int:
        try:
            self.process.wait()
        except Exception:
            pass
        status = self.process.exitstatus
        return status if status is not None else 1

    def write(self, data: str) -> None:
        with self._lock:
            if self.process is not None and self.process.isalive():
                self.process.write(data.encode("utf-8"))

    def resize(self, cols: int, rows: int) -> None:
        if self.process is not None and cols > 0 and rows > 0:
            try:
                self.process.setwinsize(rows, cols)
            except OSError:
                pass

    def close(self) -> None:
        if self.process is not None and self.process.isalive():
            try:
                self.process.terminate(force=True)
            except Exception:
                pass


def page_html() -> str:
    """index.html with xterm.js inlined: no local web server needed."""
    folder = resources.files("zir").joinpath("assets/term")
    css = folder.joinpath("xterm.css").read_text()
    scripts = "\n".join(
        f"<script>\n{folder.joinpath(name).read_text()}\n</script>" for name in _SCRIPTS
    )
    html = folder.joinpath("index.html").read_text()
    return html.replace("/*XTERM_CSS*/", css).replace("<!--XTERM_SCRIPTS-->", scripts)


class Api:
    """Called from the page's JavaScript (window.pywebview.api)."""

    def __init__(self, bridge: PtyBridge) -> None:
        self._bridge = bridge
        self._started = False

    def ready(self, cols: int, rows: int) -> None:
        if not self._started:
            self._started = True
            self._bridge.start(int(cols), int(rows))

    def input(self, data: str) -> None:
        self._bridge.write(data)

    def resize(self, cols: int, rows: int) -> None:
        self._bridge.resize(int(cols), int(rows))


def _window_size(webview) -> tuple[int, int]:
    width = int(WINDOW_COLUMNS * CELL_WIDTH) + 8
    height = int(WINDOW_ROWS * CELL_HEIGHT) + 8 + 28  # + title bar
    try:
        screen = webview.screens[0]
        width = min(width, screen.width - 40)
        height = min(height, screen.height - 80)
    except Exception:
        pass
    return width, height


def run_window(command: list[str] | None = None) -> int:
    import webview

    status = {"code": 0}
    holder: dict = {}

    def on_output(text: str) -> None:
        window = holder.get("window")
        if window is not None:
            try:
                window.run_js(f"zirWrite({json.dumps(text)})")
            except Exception:
                pass

    def on_exit(code: int) -> None:
        status["code"] = code
        window = holder.get("window")
        if window is not None:
            window.destroy()

    bridge = PtyBridge(
        command or child_command(),
        on_output,
        on_exit,
        env=child_env(),
        cwd=os.path.expanduser("~"),
    )
    width, height = _window_size(webview)
    window = webview.create_window(
        "Zir SSH",
        html=page_html(),
        js_api=Api(bridge),
        width=width,
        height=height,
        background_color="#000000",
    )
    holder["window"] = window
    window.events.closed += bridge.close  # red button or Cmd+Q: don't leave zir running
    webview.start()
    bridge.close()
    return status["code"]
