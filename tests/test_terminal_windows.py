"""The built-in terminal on Windows: ConPTY through pywinpty (runs on the Windows CI)."""

import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows ConPTY")


async def _wait_for(predicate, timeout=15.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


def test_conpty_session_runs_a_program_and_reports_its_end():
    from zir.ptysession import BACKEND, PtySession

    assert BACKEND == "winpty"

    async def scenario():
        output = bytearray()
        ended = []
        session = PtySession(["cmd.exe", "/c", "echo zir-%NUMBER%"], 24, 80, {**os.environ, "NUMBER": "42"},
                             None, output.extend, ended.append)
        assert await _wait_for(lambda: b"zir-42" in output)
        assert await _wait_for(lambda: bool(ended))
        assert not session.isalive()

    asyncio.run(scenario())


def test_conpty_session_takes_input_and_stops():
    from zir.ptysession import PtySession

    async def scenario():
        output = bytearray()
        ended = []
        session = PtySession(["cmd.exe"], 24, 80, dict(os.environ), None, output.extend, ended.append)
        await asyncio.sleep(0.5)
        session.write("echo typed-%COMPUTERNAME:~0,0%7\r")
        assert await _wait_for(lambda: b"typed-7" in output)
        session.resize(30, 100)
        session.stop()
        await asyncio.sleep(0.3)
        assert not session.isalive() and not ended  # stop() is not reported as an exit

    asyncio.run(scenario())


def test_terminal_tab_works_on_windows():
    from zir.widgets.terminal import SUPPORTED, TerminalView
    from test_app import make_app

    assert SUPPORTED

    async def scenario():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("alt+1")  # Local: PowerShell on this computer
            await pilot.pause(1.0)
            term = app.focused
            assert isinstance(term, TerminalView) and term.running
            term.write("echo ('win-' + (40+2))\r")
            lines = lambda: ["".join(c.data for c in term.screen_buf.buffer[y].values()) for y in range(term.screen_buf.lines)]  # noqa: E731
            assert await _wait_for(lambda: any("win-42" in line and "echo" not in line for line in lines()))

    asyncio.run(scenario())
