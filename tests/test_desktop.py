import sys

from zir import desktop


def test_install_desktop_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    entry, icon = desktop.install_desktop_entry()
    assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    text = entry.read_text()
    assert "Terminal=true" in text
    assert f"Icon={icon}" in text
    assert "Exec=" in text


def test_not_from_app_bundle_in_tests(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/zir")
    assert not desktop.started_from_app_bundle()
    assert not desktop.opened_from_app_bundle()


def test_resize_terminal(capsys):
    desktop.resize_terminal()
    assert capsys.readouterr().out == "\x1b[8;51;197t"


def test_close_terminal_window(monkeypatch):
    calls = []
    monkeypatch.delenv("ZIR_TERMINAL", raising=False)
    monkeypatch.setattr(desktop.os, "ttyname", lambda fd: "/dev/ttys004")
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    desktop.close_terminal_window()
    (args, kw), = calls
    assert args[-1] == 'tell application "Terminal" to close (every window whose tty of selected tab is "/dev/ttys004")'
    assert "osascript" in args[2]
    assert kw["start_new_session"]


def test_close_terminal_window_other_terminal(monkeypatch):
    calls = []
    monkeypatch.setenv("ZIR_TERMINAL", "iTerm")
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **kw: calls.append(args))
    desktop.close_terminal_window()
    assert calls == []


def test_legacy_console_only_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not desktop.started_in_legacy_console()
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop.started_in_legacy_console()
    monkeypatch.setenv("WT_SESSION", "1")
    assert not desktop.started_in_legacy_console()
