"""ZIR_SELF_TEST=1 zir: start the UI without a screen and exit.

The build runs this on every platform, so a module missing from the single-file
binary (PyInstaller leaves out what it was told to) fails the build instead of
the user's first start. `--report` alone never imports the UI.
"""

from __future__ import annotations

import io
import tarfile


def _tar_round_trip() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w|", format=tarfile.GNU_FORMAT) as tar:
        info = tarfile.TarInfo("файл.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))
    buffer.seek(0)
    with tarfile.open(fileobj=buffer, mode="r|") as tar:
        names = [member.name for member in tar]
    assert names == ["файл.txt"], names


def run() -> int:
    _tar_round_trip()

    from .app import ZirApp
    from .collector import build_collectors, demo_specs
    from .config import Settings
    from .store import Session
    from .widgets.file_dialogs import ViewerScreen
    from .widgets.files_section import FilesSection

    specs = demo_specs(2)
    app = ZirApp(specs, build_collectors(specs, 8.0, 5, demo=True), Settings(), Session(), None,
                 persist_session=False)
    problems: list[str] = []

    async def pilot(pilot) -> None:
        try:
            await pilot.pause(0.5)  # let the start-up settle (it picks the initial tab)
            app.servers.active = app.pane_ids[specs[0].name]  # the first server tab
            await pilot.pause(0.3)
            view = app._active_view()
            view.show_section("files")
            for _attempt in range(100):
                await pilot.pause(0.05)
                sections = view.query(FilesSection)
                if sections and sections.first().panel("right").path is not None:
                    break
            else:
                problems.append("the Files section did not load")
            await app.push_screen(ViewerScreen("self test", "text", save=None))
            await pilot.pause(0.2)
        except Exception as exc:  # noqa: BLE001 - reported below
            problems.append(f"{type(exc).__name__}: {exc}")
        finally:
            app.exit()

    app.run(headless=True, auto_pilot=pilot, size=(120, 36))
    if problems:
        print("self test failed: " + "; ".join(problems))
        return 1
    print("self test ok")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
