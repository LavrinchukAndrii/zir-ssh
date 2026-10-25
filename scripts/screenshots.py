"""Regenerate the README screenshots from the demo fleet (no SSH, no network).

    python scripts/screenshots.py            # -> docs/screenshots/*.svg (English and Ukrainian)

Takes a few minutes: the charts need some history and the Attacks shot waits for the demo attack.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
SIZE = (160, 45)
WARMUP = 60  # seconds of demo polling, so the charts have some history

# file name -> (Alt+N tab: 2 = Fleet, 3 = web-1, 4 = web-2; steps of "]" from Overview)
SHOTS = {
    "fleet": (2, 0),
    "overview": (3, 0),
    "web": (3, 1),
    "database": (3, 2),
    "processes": (3, 5),
    "attacks": (4, 4),  # web-2 is the demo attack target
}


async def wait_for_attack() -> None:
    from zir.demo_stack import ATTACK_EVERY, ATTACK_FOR

    while not 8 <= time.time() % ATTACK_EVERY < ATTACK_FOR - 4:
        await asyncio.sleep(0.5)


async def shoot(language: str) -> None:
    from zir.app import ZirApp
    from zir.collector import build_collectors, demo_specs
    from zir.config import Settings
    from zir.i18n import set_language
    from zir.store import Session

    set_language(language)
    specs = demo_specs(6)
    app = ZirApp(
        specs,
        build_collectors(specs, 8.0, 5, demo=True),
        Settings(interval=0.5),
        Session(),
        None,
        persist_session=False,
    )
    suffix = "" if language == "en" else f"-{language}"
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("alt+3")
        await pilot.pause(WARMUP)
        for name, (tab, steps) in SHOTS.items():
            if name == "attacks":
                await wait_for_attack()
            await pilot.press(f"alt+{tab}")
            await pilot.pause(0.5)
            for _ in range(steps):
                await pilot.press("right_square_bracket")
            await pilot.pause(2.5)
            print(app.save_screenshot(f"{name}{suffix}.svg", str(OUT)))
            for _ in range(steps):  # back to Overview for the next shot
                await pilot.press("left_square_bracket")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    # A throwaway home: no real prefs, session, SSH keys or shell prompt end up in the images.
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        os.environ.pop("ZIR_LANG", None)
        sys.path.insert(0, str(ROOT))
        for language in ("en", "uk"):
            asyncio.run(shoot(language))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
