"""Host aliases from ~/.ssh/config, offered on the first-run screen."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SshAlias:
    name: str
    hostname: str = ""
    user: str = ""

    @property
    def detail(self) -> str:
        host = self.hostname or self.name
        return f"{self.user}@{host}" if self.user else host


def _expand(pattern: str, base: Path) -> list[Path]:
    pattern = os.path.expanduser(pattern)
    if not os.path.isabs(pattern):
        pattern = str(base / pattern)
    return [Path(path) for path in sorted(glob.glob(pattern))]


def ssh_aliases(path: Path | None = None, _seen: set[Path] | None = None) -> list[SshAlias]:
    """Concrete `Host` names (no wildcards), following `Include`."""
    path = path or Path.home() / ".ssh" / "config"
    seen = _seen if _seen is not None else set()
    try:
        resolved = path.resolve()
        if resolved in seen:
            return []
        seen.add(resolved)
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []

    aliases: list[SshAlias] = []
    current: list[str] = []
    options: dict[str, str] = {}

    def flush() -> None:
        for name in current:
            aliases.append(SshAlias(name, options.get("hostname", ""), options.get("user", "")))

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _sep, value = line.replace("=", " ", 1).partition(" ")
        key, value = key.lower(), value.strip().strip('"')
        if key == "include":
            for pattern in value.split():
                for included in _expand(pattern, Path.home() / ".ssh"):
                    aliases.extend(ssh_aliases(included, seen))
        elif key in ("host", "match"):
            flush()
            current = [
                name for name in value.split()
                if key == "host" and not any(ch in name for ch in "*?!")
            ]
            options = {}
        elif current and key in ("hostname", "user"):
            options.setdefault(key, value)
    flush()

    unique: dict[str, SshAlias] = {}
    for alias in aliases:
        if not _is_git_host(alias):
            unique.setdefault(alias.name, alias)
    return list(unique.values())


# Git hosting entries are not servers you can monitor.
GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "dev.azure.com", "vs-ssh.visualstudio.com")


def _is_git_host(alias: SshAlias) -> bool:
    for host in (alias.name.lower(), alias.hostname.lower()):
        if any(host == git or host.endswith("." + git) for git in GIT_HOSTS):
            return True
    return False
