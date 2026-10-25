"""Host configuration (TOML, no extra dependencies)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .i18n import _, normalize
from .paths import config_dir


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class HostSpec:
    name: str
    target: str
    port: int | None = None
    identity: str | None = None
    tags: tuple[str, ...] = ()
    local: bool = False
    strict_host_key: str = "accept-new"
    interval: float | None = None
    web_logs: tuple[str, ...] = ()  # extra access-log paths/globs besides the usual places
    ssh_options: tuple[str, ...] = ()  # extra `ssh -o` options, e.g. "ProxyJump=bastion"
    enabled: bool = True  # false: listed, but never polled
    title: str | None = None  # human name shown instead of `name`, e.g. "Shop (prod)"


@dataclass(frozen=True)
class Settings:
    interval: float = 2.0
    timeout: float = 8.0
    connect_timeout: int = 5
    hidden_interval_factor: float = 3.0
    max_parallel: int = 8
    language: str | None = None  # "uk" | "en"; None = prefs.json or Ukrainian


DEFAULT_CONFIG = config_dir() / "hosts.toml"

SEARCH_PATHS = (Path("zir.toml"), Path("hosts.toml"))


def find_config(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        if not explicit.expanduser().is_file():
            raise ConfigError(_("config not found: {path}", path=explicit))
        return explicit.expanduser()

    for path in (DEFAULT_CONFIG, *SEARCH_PATHS):
        if path.expanduser().is_file():
            return path.expanduser()
    return None


def _as_str_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ConfigError(_("tags must be a string or a list of strings"))


def load_config(path: Path | None) -> tuple[list[HostSpec], Settings]:
    if path is None:
        return [], Settings()

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(_("invalid TOML in {path}: {error}", path=path, error=exc)) from exc

    raw_settings = data.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise ConfigError(_("[settings] must be a table"))

    settings = Settings(
        interval=float(raw_settings.get("interval", 2.0)),
        timeout=float(raw_settings.get("timeout", 8.0)),
        connect_timeout=int(raw_settings.get("connect_timeout", 5)),
        hidden_interval_factor=float(raw_settings.get("hidden_interval_factor", 3.0)),
        max_parallel=int(raw_settings.get("max_parallel", 8)),
        language=normalize(raw_settings.get("language")),
    )
    if raw_settings.get("language") is not None and settings.language is None:
        raise ConfigError(_("settings.language must be \"uk\" or \"en\""))
    if settings.interval < 0.5:
        raise ConfigError(_("settings.interval must be >= 0.5 seconds"))
    if settings.timeout < 1:
        raise ConfigError(_("settings.timeout must be >= 1 second"))
    if settings.hidden_interval_factor < 1:
        raise ConfigError(_("settings.hidden_interval_factor must be >= 1"))
    if settings.max_parallel < 1:
        raise ConfigError(_("settings.max_parallel must be >= 1"))

    raw_hosts = data.get("hosts", {})
    if not isinstance(raw_hosts, dict):
        raise ConfigError(_("[hosts] must be a table"))

    hosts: list[HostSpec] = []
    for name, value in raw_hosts.items():
        if isinstance(value, str):
            hosts.append(HostSpec(name=name, target=value))
            continue
        if not isinstance(value, dict):
            raise ConfigError(_("[hosts.{name}] must be a string or a table", name=name))

        local = bool(value.get("local", False))
        target = str(value.get("target") or value.get("host") or name)
        port = value.get("port")
        interval = value.get("interval")
        if interval is not None and float(interval) < 0.5:
            raise ConfigError(_("hosts.{name}.interval must be >= 0.5 seconds", name=name))
        hosts.append(
            HostSpec(
                name=name,
                target=target,
                port=int(port) if port is not None else None,
                identity=str(value["identity"]) if value.get("identity") else None,
                tags=_as_str_list(value.get("tags")),
                local=local,
                strict_host_key=str(value.get("strict_host_key", "accept-new")),
                interval=float(interval) if interval is not None else None,
                web_logs=_as_str_list(value.get("web_logs")),
                ssh_options=_as_str_list(value.get("ssh_options")),
                enabled=bool(value.get("enabled", True)),
                title=str(value["title"]).strip() or None if value.get("title") else None,
            )
        )

    return hosts, settings


def hosts_from_args(names: list[str]) -> list[HostSpec]:
    specs: list[HostSpec] = []
    seen: dict[str, int] = {}
    for raw in names:
        raw = raw.strip()
        if not raw:
            continue

        target = raw
        port: int | None = None
        if ":" in raw and raw.count(":") == 1:
            maybe_host, maybe_port = raw.rsplit(":", 1)
            if maybe_port.isdigit():
                target, port = maybe_host, int(maybe_port)

        name = target.split("@")[-1]
        # Two ad-hoc destinations with the same host name must not collide.
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}-{seen[name]}"

        specs.append(HostSpec(name=name, target=target, port=port))
    return specs
