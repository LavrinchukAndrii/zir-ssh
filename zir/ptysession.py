"""A program on a pseudo-terminal, for the built-in terminal widget.

macOS / Linux: ptyprocess; the pty's file descriptor is watched by the event loop.
Windows: ConPTY through pywinpty; a thread reads (the Windows event loop cannot
watch pipes) and hands the output over to the event loop.

Output reaches `on_data(bytes)` and the end `on_exit(exit code)`, both on the
event loop's thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import os
import sys
import threading
from typing import Callable

READ_SIZE = 16384  # bytes per read: keeps each UI pause short
EXIT_POLL = 0.5  # Windows: seconds between checks whether the program has ended
WINPTY_FILLER = "0011Ignore"

if sys.platform == "win32":
    BACKEND = "winpty" if importlib.util.find_spec("winpty") is not None else None
else:
    BACKEND = "ptyprocess" if importlib.util.find_spec("ptyprocess") is not None else None


class PtySession:
    def __init__(
        self,
        command: list[str],
        rows: int,
        cols: int,
        env: dict[str, str],
        cwd: str | None,
        on_data: Callable[[bytes], None],
        on_exit: Callable[[int | None], None],
    ) -> None:
        self.on_data = on_data
        self.on_exit = on_exit
        self.loop = asyncio.get_running_loop()
        # Callbacks from the reader thread run in the starter's context (Textual's
        # active app lives in a ContextVar; a bare thread does not have it).
        self.context = contextvars.copy_context()
        self.exit_code: int | None = None
        self._closed = False
        self._spawn(command, rows, cols, env, cwd)

    # -- the same for both ----------------------------------------------------

    def _finish(self) -> None:
        """The program ended (output closed): collect the exit code, once."""
        if self._closed:
            return
        self._closed = True
        try:
            self.process.wait()
            self.exit_code = self.process.exitstatus
        except Exception:  # noqa: BLE001 - the code is only shown, never needed
            self.exit_code = None
        self._close()
        self.on_exit(self.exit_code)

    def isalive(self) -> bool:
        if self._closed:
            return False
        try:
            return self.process.isalive()
        except Exception:  # noqa: BLE001
            return False

    def resize(self, rows: int, cols: int) -> None:
        if self.isalive():
            try:
                self.process.setwinsize(rows, cols)
            except Exception:  # noqa: BLE001 - resizing a closing session
                pass

    def stop(self) -> None:
        """Kill the program; on_exit is not called."""
        if self._closed:
            return
        self._closed = True
        self._unwatch()
        try:
            if self.process.isalive():
                self.process.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
        self._close()

    def _close(self) -> None:
        try:
            self.process.close(force=True)
        except Exception:  # noqa: BLE001
            pass

    # -- per platform ------------------------------------------------------------

    if BACKEND == "winpty":

        def _spawn(self, command, rows, cols, env, cwd) -> None:
            from winpty import PtyProcess

            self.process = PtyProcess.spawn(command, dimensions=(rows, cols), env=env, cwd=cwd)
            # pywinpty sleeps 0.1 s after each signal and on close, on the UI thread.
            self.process.delayafterterminate = self.process.delayafterclose = 0.01
            self._thread = threading.Thread(target=self._read_loop, name="zir-pty", daemon=True)
            self._thread.start()
            self.loop.call_later(EXIT_POLL, self._watch_exit)

        def _watch_exit(self) -> None:
            """ConPTY may keep the output open after the program ended: closing
            it wakes the reader thread up, which then reports the end."""
            if self._closed:
                return
            try:
                alive = self.process.isalive()
            except Exception:  # noqa: BLE001
                alive = False
            if alive:
                self.loop.call_later(EXIT_POLL, self._watch_exit)
                return
            self.loop.call_later(EXIT_POLL, self._unblock_reader)  # let the last output arrive

        def _unblock_reader(self) -> None:
            if not self._closed:
                try:
                    self.process.fileobj.close()
                except Exception:  # noqa: BLE001
                    pass

        def _read_loop(self) -> None:
            while not self._closed:
                try:
                    text = self.process.read(READ_SIZE)
                except Exception:  # noqa: BLE001 - EOFError when the program ends, OSError when stopped
                    break
                text = text.replace(WINPTY_FILLER, "")  # pywinpty's "nothing read" marker
                if text:
                    self._post(self._deliver, text.encode("utf-8", "replace"))
            self._post(self._finish)

        def _post(self, callback, *args) -> None:
            try:
                self.loop.call_soon_threadsafe(callback, *args, context=self.context)
            except RuntimeError:  # the app has closed its loop
                pass

        def _deliver(self, data: bytes) -> None:
            if not self._closed:
                self.on_data(data)

        def write(self, text: str) -> None:
            if self.isalive():
                self.process.write(text)

        def _unwatch(self) -> None:
            pass  # the thread ends when the process is closed

    else:

        def _spawn(self, command, rows, cols, env, cwd) -> None:
            from ptyprocess import PtyProcess

            self.process = PtyProcess.spawn(command, dimensions=(rows, cols), env=env, cwd=cwd)
            # ptyprocess sleeps 0.1 s after each signal and on close, on the UI thread.
            self.process.delayafterterminate = self.process.delayafterclose = 0.01
            self.loop.add_reader(self.process.fd, self._read)

        def _read(self) -> None:
            try:
                data = os.read(self.process.fd, READ_SIZE)
            except OSError:
                data = b""
            if data:
                self.on_data(data)
                return
            self._unwatch()
            self._finish()

        def write(self, text: str) -> None:
            if self.isalive():
                os.write(self.process.fd, text.encode())

        def _unwatch(self) -> None:
            try:
                self.loop.remove_reader(self.process.fd)
            except (RuntimeError, ValueError, OSError):
                pass
