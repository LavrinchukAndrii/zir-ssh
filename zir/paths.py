"""Per-user directories for config, SSH control sockets and UI state.

Linux/macOS keep the XDG layout (~/.config, ~/.cache, ~/.local/state);
Windows uses the native %APPDATA% / %LOCALAPPDATA% locations via platformdirs.
Folders left by the old name (serverdeck) are moved over on start.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP = "zir"
LEGACY_APP = "serverdeck"


def _xdg(variable: str, fallback: str, app: str = APP) -> Path:
    return Path(os.environ.get(variable) or Path.home() / fallback) / app


def config_dir(app: str = APP) -> Path:
    if sys.platform == "win32":
        from platformdirs import user_config_dir

        return Path(user_config_dir(app, appauthor=False, roaming=True))
    return _xdg("XDG_CONFIG_HOME", ".config", app)


def cache_dir(app: str = APP) -> Path:
    if sys.platform == "win32":
        from platformdirs import user_cache_dir

        return Path(user_cache_dir(app, appauthor=False))
    return _xdg("XDG_CACHE_HOME", ".cache", app)


def state_dir(app: str = APP) -> Path:
    if sys.platform == "win32":
        from platformdirs import user_state_dir

        return Path(user_state_dir(app, appauthor=False))
    return _xdg("XDG_STATE_HOME", ".local/state", app)


def migrate_legacy() -> None:
    """Move ~/.config/serverdeck (hosts, credentials, prefs), the cache and the
    state folders to the new name, once. Never stops the app from starting."""
    for folder in (config_dir, cache_dir, state_dir):
        old, new = folder(LEGACY_APP), folder()
        if new.exists() or not old.is_dir():
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
        except OSError:
            try:
                shutil.copytree(old, new)
            except OSError:
                pass
