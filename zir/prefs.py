"""User preferences changed from the UI (separate from the hand-written hosts.toml)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import config_dir


def prefs_path() -> Path:
    return config_dir() / "prefs.json"


def load_prefs(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or prefs_path()).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_prefs(updates: dict, path: Path | None = None) -> None:
    path = path or prefs_path()
    data = {**load_prefs(path), **updates}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except OSError:
        pass
