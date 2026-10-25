"""PyInstaller entry point (needs a script, not a package module)."""

import os
import sys

# Only the newest Rich unicode table is bundled (see zir.spec).
os.environ.pop("UNICODE_VERSION", None)

from zir.keysetup import answer_askpass  # noqa: E402

if __name__ == "__main__":
    # ssh runs zir.exe as SSH_ASKPASS while adding a key (Windows).
    if answer_askpass():
        raise SystemExit(0)

    from zir.cli import main
    from zir.desktop import (
        close_terminal_window,
        open_own_window,
        opened_from_app_bundle,
        reopen_in_terminal,
        reopen_in_windows_terminal,
        resize_terminal,
        started_from_app_bundle,
        started_in_legacy_console,
    )

    if started_from_app_bundle() and len(sys.argv) == 1:
        code = open_own_window()
        raise SystemExit(reopen_in_terminal() if code is None else code)
    if started_in_legacy_console() and len(sys.argv) == 1 and reopen_in_windows_terminal():
        raise SystemExit(0)
    if opened_from_app_bundle() and len(sys.argv) == 1:
        resize_terminal()
        try:
            raise SystemExit(main())
        finally:
            close_terminal_window()  # no leftover "[Process completed]" window
    raise SystemExit(main())
