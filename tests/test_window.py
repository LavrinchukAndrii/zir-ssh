import sys
import threading

import pytest

from zir import desktop, window

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty")


def run_bridge(command, cols=80, rows=24, send=None, timeout=10):
    output = []
    done = threading.Event()
    codes = []

    def on_exit(code):
        codes.append(code)
        done.set()

    bridge = window.PtyBridge(command, output.append, on_exit)
    bridge.start(cols, rows)
    if send is not None:
        send(bridge)
    assert done.wait(timeout), "child did not exit"
    return "".join(output), codes[0]


def test_bridge_passes_utf8_output_and_exit_code():
    text, code = run_bridge(["sh", "-c", 'printf "привіт ═══"; exit 3'])
    assert "привіт ═══" in text
    assert code == 3


def test_bridge_resize_and_input():
    def send(bridge):
        bridge.resize(120, 40)
        bridge.write("hello\r")

    text, code = run_bridge(["sh", "-c", "read x; stty size; echo got:$x"], send=send)
    assert "40 120" in text
    assert "got:hello" in text
    assert code == 0


def test_child_env(monkeypatch):
    monkeypatch.setattr(window, "_login_path", lambda shell: "/opt/homebrew/bin:/usr/bin:/bin")
    env = window.child_env({"PATH": "/usr/bin:/bin", "SHELL": "/bin/zsh"})
    assert env["ZIR_IN_WINDOW"] == "1"
    assert env["TERM"] == "xterm-256color"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"


def test_page_html_inlines_xterm():
    html = window.page_html()
    assert "XTERM_SCRIPTS" not in html and "XTERM_CSS" not in html
    assert "FitAddon" in html and "WebglAddon" in html and "Unicode11Addon" in html
    assert ".xterm" in html
    assert "zirWrite" in html


def test_child_inside_window_is_not_the_terminal_app(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/Applications/zir.app/Contents/MacOS/zir")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("ZIR_IN_WINDOW", raising=False)
    assert desktop.opened_from_app_bundle()
    monkeypatch.setenv("ZIR_IN_WINDOW", "1")
    assert not desktop.opened_from_app_bundle()


def test_open_own_window_falls_back_to_terminal_app(monkeypatch):
    monkeypatch.setenv("ZIR_TERMINAL", "iTerm")
    assert desktop.open_own_window() is None


def test_open_own_window_runs_window(monkeypatch):
    pytest.importorskip("webview")
    monkeypatch.delenv("ZIR_TERMINAL", raising=False)
    monkeypatch.setattr(window, "run_window", lambda: 7)
    assert desktop.open_own_window() == 7
