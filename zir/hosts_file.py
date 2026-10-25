"""Add / remove hosts in hosts.toml from the UI without disturbing the rest of it.

Hosts are appended as their own `[hosts."name"]` table; removal drops only that
table (or the `name = "target"` line of the short form). Every change is parsed
back with tomllib before it is written, so a mistake can never break the file.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from .config import ConfigError, HostSpec
from .i18n import _

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def host_block(spec: HostSpec) -> str:
    lines = [f"[hosts.{_toml_string(spec.name)}]"]
    if spec.title:
        lines.append(f"title = {_toml_string(spec.title)}")
    lines.append(f"target = {_toml_string(spec.target)}")
    if spec.port:
        lines.append(f"port = {spec.port}")
    if spec.identity:
        lines.append(f"identity = {_toml_string(spec.identity)}")
    if spec.tags:
        lines.append("tags = [" + ", ".join(_toml_string(t) for t in spec.tags) + "]")
    return "\n".join(lines) + "\n"


def _write_checked(path: Path, text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - guarded by construction
        raise ConfigError(_("invalid TOML in {path}: {error}", path=path, error=exc)) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def add_host(path: Path, spec: HostSpec) -> None:
    text = path.read_text() if path.exists() else (
        "# Zir SSH servers. See hosts.example.toml for all options.\n\n[settings]\ninterval = 2.0\n"
    )
    hosts = tomllib.loads(text).get("hosts", {}) if text.strip() else {}
    if spec.name in hosts:
        raise ConfigError(_("a server named {name} already exists", name=spec.name))
    if text and not text.endswith("\n"):
        text += "\n"
    _write_checked(path, text + "\n" + host_block(spec))


def remove_host(path: Path, name: str) -> bool:
    """True if the host was found in the file and removed."""
    if not path.exists():
        return False
    lines = path.read_text().splitlines(keepends=True)
    header = re.compile(r"^\s*\[\s*hosts\s*\.\s*(\"(?:[^\"\\\\]|\\\\.)*\"|'[^']*'|[A-Za-z0-9_-]+)\s*\]\s*(#.*)?$")
    out: list[str] = []
    skipping = False
    removed = False
    in_hosts_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            match = header.match(stripped)
            key = None
            if match:
                key = match.group(1)
                key = tomllib.loads(f"k = {key}")["k"] if key[0] in "\"'" else key
            skipping = key == name
            removed = removed or skipping
            in_hosts_table = stripped.split("#")[0].strip() == "[hosts]"
            if skipping:
                continue
        elif skipping:
            continue
        elif in_hosts_table and re.match(rf"^\s*(\"{re.escape(name)}\"|{re.escape(name)})\s*=", line):
            removed = True  # short form: name = "user@host"
            continue
        out.append(line)
    if removed:
        text = re.sub(r"\n{3,}", "\n\n", "".join(out))
        _write_checked(path, text)
    return removed


def parse_address(address: str, port_text: str = "") -> tuple[str, int | None]:
    """'root@1.2.3.4:2345' / '1.2.3.4' + '2345' -> (target, port)."""
    address = address.strip()
    port = None
    if address.count(":") == 1 and address.rsplit(":", 1)[1].isdigit():
        address, port_part = address.rsplit(":", 1)
        port = int(port_part)
    if port_text.strip():
        if not port_text.strip().isdigit() or not 0 < int(port_text) < 65536:
            raise ConfigError(_("port must be a number from 1 to 65535"))
        port = int(port_text)
    if not address or any(ch.isspace() for ch in address):
        raise ConfigError(_("enter an address: user@host, an IP or an alias from ~/.ssh/config"))
    return address, (None if port == 22 else port)


def suggest_name(target: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "-", target.split("@")[-1]).strip("-.") or "server"
    name, index = base, 2
    while name in taken:
        name, index = f"{base}-{index}", index + 1
    return name
