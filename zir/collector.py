"""SSH transport, collectors and the demo data source."""

from __future__ import annotations

import asyncio
import functools
import os
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import HostSpec
from .i18n import _
from .demo_stack import DemoStack
from .services import StackTracker
from .paths import cache_dir
from .probes import (
    CpuSample,
    DiskUsage,
    DockerContainer,
    ListenSocket,
    Metrics,
    NetCounters,
    ProcessInfo,
    build_probe,
    carry_slow,
    ServiceFailure,
    Snapshot,
    compute_metrics,
    parse_snapshot,
)

# SSH connections stay open this long after the last poll (shared with the terminal).
CONTROL_PERSIST = 300
# Rarely changing data (OS, PHP-FPM config, log discovery, ports, containers) is
# probed at most this often; failures and load are probed on every poll.
SLOW_PROBE_SECONDS = 10.0


class ProbeError(Exception):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@functools.cache
def _cache_dir() -> Path:
    path = cache_dir() / "ssh"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _friendly_error(stderr: str) -> tuple[str, str]:
    text = " ".join(line.strip() for line in stderr.strip().splitlines() if line.strip())
    lowered = text.lower()

    if "permission denied" in lowered:
        return (
            text,
            _("The server does not accept your SSH key. Press a to enter the password once and add the key."),
        )
    if "host key verification failed" in lowered:
        return (
            text,
            _("The server key has changed or is unknown. Connect once with plain ssh, or set strict_host_key = \"accept-new\"."),
        )
    if "could not resolve hostname" in lowered:
        return (text, _("Check the SSH alias in ~/.ssh/config or use user@ip."))
    if "connection refused" in lowered:
        return (text, _("Check the port and that sshd is running."))
    if "connection timed out" in lowered or "operation timed out" in lowered:
        return (text, _("Host is unreachable (VPN/firewall?), or increase the timeout."))
    return (text or _("ssh failed"), "")


def local_shell_command() -> list[str]:
    """The user's own login shell on this computer."""
    return [os.environ.get("SHELL") or "/bin/bash", "-l"]


def interactive_command(spec: HostSpec, connect_timeout: int = 8) -> list[str]:
    """`ssh -t` for the built-in terminal. Shares the polling connection
    (ControlMaster), so a new terminal opens without a second login."""
    if spec.local:
        return local_shell_command()
    command = [
        shutil.which("ssh") or "ssh",
        "-t",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=15",
        "-o", f"StrictHostKeyChecking={spec.strict_host_key}",
    ]
    if sys.platform != "win32":
        command += ["-o", "ControlMaster=auto", "-o", f"ControlPath={_cache_dir()}/%C",
                    "-o", f"ControlPersist={CONTROL_PERSIST}"]
    for option in spec.ssh_options:
        command += ["-o", option]
    if spec.port:
        command += ["-p", str(spec.port)]
    if spec.identity:
        command += ["-i", os.path.expanduser(spec.identity)]
    return command + [spec.target]


@dataclass
class HostReading:
    spec: HostSpec
    snapshot: Snapshot | None = None
    metrics: Metrics = field(default_factory=Metrics)
    error: str | None = None
    hint: str = ""
    latency: float = 0.0
    at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.error is None and self.snapshot is not None


class Collector:
    """Runs the probe script on one host over SSH and keeps the previous sample."""

    def __init__(self, spec: HostSpec, timeout: float = 8.0, connect_timeout: int = 5) -> None:
        self.spec = spec
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._previous: Snapshot | None = None
        self._last_metrics = Metrics()
        self._tracker = StackTracker()
        self._full_at: float | None = None  # monotonic time of the last full (slow) poll
        self._argv: list[str] | None = None

    # -- transport ---------------------------------------------------------

    def _command(self) -> list[str]:
        if self._argv is None:
            self._argv = self._build_command()
        return self._argv

    def _build_command(self) -> list[str]:
        if self.spec.local:
            return ["bash", "-s"]

        ssh = shutil.which("ssh")
        if ssh is None:
            raise ProbeError(
                _("ssh client not found in PATH"),
                _("Install OpenSSH Client (Windows: Settings → Optional features)."),
            )

        command = [
            ssh,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=2",
        ]
        # Windows OpenSSH has no connection multiplexing (ControlMaster).
        if sys.platform != "win32":
            command += [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={_cache_dir()}/%C",
                "-o",
                f"ControlPersist={CONTROL_PERSIST}",
            ]
        command += [
            "-o",
            "LogLevel=ERROR",
            "-o",
            f"StrictHostKeyChecking={self.spec.strict_host_key}",
        ]
        for option in self.spec.ssh_options:
            command += ["-o", option]
        if self.spec.port:
            command += ["-p", str(self.spec.port)]
        if self.spec.identity:
            command += ["-i", os.path.expanduser(self.spec.identity)]
        command += [self.spec.target, "bash -s"]
        return command

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()
            try:
                await process.wait()
            except ProcessLookupError:  # pragma: no cover - race
                pass

    async def _run(self, script: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *self._command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(script.encode()), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            await self._stop(process)
            raise ProbeError(
                _("timeout after {sec}s", sec=f"{self.timeout:.0f}"),
                _("Host is slow or unreachable; increase timeout or check the network."),
            ) from None
        except asyncio.CancelledError:
            await self._stop(process)
            raise

        output = stdout.decode(errors="replace")
        # 255 is ssh itself failing (auth, network). Other codes come from the
        # remote script: keep its output if it produced any sections.
        if process.returncode == 255 or (process.returncode != 0 and "__SEC__" not in output):
            message, hint = _friendly_error(stderr.decode(errors="replace"))
            raise ProbeError(message, hint)

        return output

    async def run_script(self, script: str) -> str:
        """Run any read-only script on the host (used by the snippets check)."""
        return await self._run(script)

    # -- collection --------------------------------------------------------

    async def collect(self) -> HostReading:
        started = time.monotonic()
        previous = self._previous
        quick = (
            previous is not None
            and self._full_at is not None
            and started - self._full_at < SLOW_PROBE_SECONDS
        )
        try:
            raw = await self._run(build_probe(previous, self.spec.web_logs, quick=quick))
            # Parsing a big poll takes a few ms: off the event loop, so the UI stays smooth.
            snapshot, metrics = await asyncio.to_thread(self._digest, raw, previous, quick)
            if not quick:
                self._full_at = started
            self._previous = snapshot
            self._last_metrics = metrics
            return HostReading(
                spec=self.spec,
                snapshot=snapshot,
                metrics=metrics,
                latency=time.monotonic() - started,
            )
        except ProbeError as exc:
            return HostReading(
                spec=self.spec,
                snapshot=self._previous,
                metrics=self._last_metrics,
                error=str(exc),
                hint=exc.hint,
                latency=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - a broken host must not stop the fleet
            return HostReading(
                spec=self.spec,
                snapshot=self._previous,
                metrics=self._last_metrics,
                error=f"{type(exc).__name__}: {exc}",
                latency=time.monotonic() - started,
            )

    def _digest(self, raw: str, previous: Snapshot | None, quick: bool) -> tuple[Snapshot, Metrics]:
        snapshot = parse_snapshot(raw)
        if previous is not None:
            if quick:
                carry_slow(snapshot, previous)
            # Versions are asked once per binary; keep the ones learned earlier.
            snapshot.stack.php_versions = {**previous.stack.php_versions, **snapshot.stack.php_versions}
        return snapshot, compute_metrics(snapshot, previous, self._tracker)


class LocalCollector(Collector):
    """Probe the machine the app is running on (Linux only, no SSH)."""

    def __init__(self, spec: HostSpec, timeout: float = 8.0) -> None:
        super().__init__(spec, timeout=timeout)


# --------------------------------------------------------------------------
# Demo data
# --------------------------------------------------------------------------

DEMO_HOSTS = [
    ("web-1", "Ubuntu 24.04.1 LTS", "6.8.0-45-generic", 4, 34, 61, 71, 0),
    ("web-2", "Ubuntu 24.04.1 LTS", "6.8.0-45-generic", 4, 17, 48, 44, 0),
    ("db-1", "Debian 12", "6.1.0-25-amd64", 8, 56, 78, 83, 0),
    ("cache-1", "Debian 12", "6.1.0-25-amd64", 2, 6, 22, 12, 0),
    ("worker-1", "Ubuntu 22.04.4 LTS", "5.15.0-119-generic", 8, 88, 71, 58, 1),
    ("backup-1", "Ubuntu 24.04.1 LTS", "6.8.0-45-generic", 2, 0, 0, 0, 0),
]


# Always slow to answer: shows that one sluggish host does not hold up the rest.
DEMO_SLOW_HOST = "edge-slow"
DEMO_SLOW_SECONDS = 4.0


def _demo_profile(name: str) -> tuple:
    for row in DEMO_HOSTS:
        if row[0] == name:
            return row
    # Generated hosts ("web-7") borrow the profile of their family ("web-1").
    family = name.rsplit("-", 1)[0]
    for row in DEMO_HOSTS:
        if row[0].rsplit("-", 1)[0] == family and row[0] != "backup-1":
            return (name, *row[1:])
    return (name, "Ubuntu 24.04.1 LTS", "6.8.0-45-generic", 4, 20, 40, 30, 0)


class DemoCollector:
    def __init__(self, spec: HostSpec) -> None:
        self.spec = spec
        self._rng = random.Random(spec.name)
        self._previous: Snapshot | None = None
        self._profile = _demo_profile(spec.name)
        self._cpu = float(self._profile[4])
        self._mem = float(self._profile[5])
        self._disk = float(self._profile[6])
        self._failed = int(self._profile[7])
        self._rx = self._rng.uniform(10e6, 900e6)
        self._tx = self._rng.uniform(5e6, 300e6)
        self._broken = spec.name == "backup-1"
        self._stack = DemoStack(spec.name, self._rng)
        self._booted = time.time() - self._rng.uniform(2 * 86400, 40 * 86400)
        self._ip = f"10.0.0.{self._rng.randint(10, 250)}"

    async def collect(self) -> HostReading:
        if self.spec.name == DEMO_SLOW_HOST:
            await asyncio.sleep(DEMO_SLOW_SECONDS)
        else:
            await asyncio.sleep(self._rng.uniform(0.03, 0.18))

        if self._broken:
            return HostReading(
                spec=self.spec,
                error=_("timeout after {sec}s", sec="8"),
                hint=_("Host is unreachable (demo). This is what a down host looks like."),
                latency=8.0,
            )

        self._cpu = min(99.0, max(1.0, self._cpu + self._rng.uniform(-6, 6)))
        self._mem = min(99.0, max(3.0, self._mem + self._rng.uniform(-1.5, 1.5)))
        self._disk = min(99.0, max(2.0, self._disk + self._rng.uniform(-0.2, 0.2)))
        self._rx += self._rng.uniform(0, 2.5e6)
        self._tx += self._rng.uniform(0, 1.1e6)

        name, os_name, kernel, cpus, *_rest = self._profile
        mem_total = 16 * 1024**3
        root_total = 40 * 1024**3
        var_total = 120 * 1024**3
        root_pct = min(self._disk, 96.0)
        var_pct = min(self._disk * 0.82, 92.0)
        snapshot = Snapshot(
            taken_at=time.time(),
            hostname=name,
            os=os_name,
            kernel=kernel,
            uptime=time.time() - self._booted,
            cpus=cpus,
            ip=self._ip,
            init="systemd",
            load=(
                self._cpu / 100 * cpus * 0.9,
                self._cpu / 100 * cpus * 0.8,
                self._cpu / 100 * cpus * 0.7,
            ),
            cpu=CpuSample(idle=int(self._cpu * 100), user=int((100 - self._cpu) * 100)),
            mem_total=mem_total,
            mem_available=int(mem_total * (1 - self._mem / 100)),
            mem_free=int(mem_total * (1 - self._mem / 100) * 0.3),
            cached=int(mem_total * 0.22),
            buffers=int(mem_total * 0.03),
            dirty=int(mem_total * 0.001),
            swap_total=2 * 1024**3,
            swap_free=int(2 * 1024**3 * (1 - min(self._mem / 100, 0.6))),
            disks=[
                DiskUsage(
                    "/dev/sda1",
                    "/",
                    root_total,
                    int(root_total * root_pct / 100),
                    int(root_total * (1 - root_pct / 100)),
                    root_pct,
                    root_pct * 0.3,
                ),
                DiskUsage(
                    "/dev/sdb1",
                    "/var",
                    var_total,
                    int(var_total * var_pct / 100),
                    int(var_total * (1 - var_pct / 100)),
                    var_pct,
                    var_pct * 0.25,
                ),
            ],
            net={"lo": NetCounters(self._rx, self._tx), "eth0": NetCounters(self._rx * 3, self._tx * 2)},
            procs=self._demo_procs(),
            procs_total=self._rng.randint(120, 260),
            failed_services=(
                [
                    ServiceFailure(
                        "laravel-worker.service",
                        "failed",
                        "Laravel queue worker",
                    )
                ]
                if self._failed
                else []
            ),
            running_services=self._rng.randint(18, 32),
            logs=self._demo_logs(),
            docker=[
                DockerContainer("app", "ghcr.io/acme/app:latest", "Up 3 days"),
                DockerContainer("redis", "redis:7-alpine", "Up 3 days"),
            ],
            who=[
                f"deploy pts/0        2026-10-23 09:12 (10.0.0.5)",
            ],
            listening=[
                ListenSocket("tcp", "0.0.0.0:22", "sshd"),
                ListenSocket("tcp", "0.0.0.0:80", "nginx"),
                ListenSocket("tcp", "0.0.0.0:443", "nginx"),
                ListenSocket("tcp", "127.0.0.1:3306", "mysqld"),
            ],
        )

        stack, stack_rates = self._stack.build(cpus)
        snapshot.stack = stack
        traffic = 12.0 if self._stack.attacking() else 1.0
        metrics = Metrics(
            cpu_pct=self._cpu,
            net_rates={
                "lo": (0.0, 0.0),
                "eth0": (self._rng.uniform(0.2e6, 4e6) * traffic, self._rng.uniform(0.1e6, 2e6) * traffic),
            },
            stack=stack_rates,
        )
        self._previous = snapshot
        return HostReading(
            spec=self.spec,
            snapshot=snapshot,
            metrics=metrics,
            latency=self._rng.uniform(0.05, 0.4),
        )

    def terminal_command(self) -> list[str]:
        """Demo hosts do not exist: the terminal opens a local shell instead."""
        banner = _("Demo: this is a local shell on your computer, not {host}.", host=self.spec.name)
        shell = os.environ.get("SHELL") or "/bin/sh"
        return ["/bin/sh", "-c", f'printf "\\033[2m%s\\033[0m\\n" "$1"; exec {shell}', "zir", banner]

    async def run_script(self, script: str) -> str:
        """Demo answer to the snippets check: a typical LEMP server."""
        await asyncio.sleep(0.4)
        lines = ["os_id=ubuntu", "os_like=debian", "os_name=Ubuntu 24.04.1 LTS", "pkg=apt", "root=1",
                 *(f"cmd={c}" for c in ("nginx", "php", "mysql", "mariadb", "mysqladmin", "redis-cli", "docker",
                                        "ufw", "fail2ban-client", "certbot", "journalctl", "systemctl", "htop", "ss")),
                 *(f"unit={u}" for u in ("nginx", "php8.3-fpm", "mariadb", "redis-server", "docker", "ufw", "fail2ban", "ssh")),
                 "php_version=8.3", "php_cli=8.3", "nginx_site=/etc/nginx/sites-enabled/shop.example.com",
                 "laravel=/var/www/shop", "compose=/srv/monitoring",
                 "access_log=/var/log/nginx/access.log", "error_log=/var/log/nginx/error.log"]
        return "\n".join(lines)

    def _demo_procs(self) -> list[ProcessInfo]:
        rows = [
            ("www-data", "php-fpm8.3", "php-fpm: pool www"),
            ("mysql", "mysqld", "/usr/sbin/mysqld"),
            ("root", "nginx", "nginx: worker process"),
            ("redis", "redis-server", "redis-server 127.0.0.1:6379"),
            ("deploy", "node", "node /srv/app/artisan horizon"),
            ("root", "systemd-journal", "/usr/lib/systemd/systemd-journald"),
            ("root", "sshd", "sshd: deploy@pts/0"),
            ("root", "containerd", "/usr/bin/containerd"),
            ("www-data", "php-fpm8.3", "php-fpm: pool www"),
            ("root", "cron", "/usr/sbin/cron -f"),
        ]
        procs: list[ProcessInfo] = []
        for index, (user, comm, args) in enumerate(rows):
            cpu = max(0.1, self._cpu / (index + 1.6) + self._rng.uniform(-1, 1))
            procs.append(
                ProcessInfo(
                    pid=1000 + index * 137,
                    user=user,
                    cpu=round(cpu, 1),
                    mem=round(max(0.1, self._rng.uniform(0.3, 8.0)), 1),
                    rss=int(self._rng.uniform(20, 900) * 1024**2),
                    etime=f"{self._rng.randint(1, 40)}d",
                    state="Ss" if index % 3 == 0 else "S",
                    comm=comm,
                    args=args,
                )
            )
        return procs

    def _demo_logs(self) -> list[str]:
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        rows = [
            f"{now} {self.spec.name} kernel: TCP: request_sock_TCP: Possible SYN flooding on port 443.",
            f"{now} {self.spec.name} nginx[812]: upstream response time 3.42s for /checkout",
            f"{now} {self.spec.name} systemd[1]: Started Session 42 of user deploy.",
            f"{now} {self.spec.name} cron[991]: pam_unix(cron:session): session opened for user root",
        ]
        if self._failed:
            rows.insert(
                0,
                f"{now} {self.spec.name} systemd[1]: laravel-worker.service: Main process exited, code=exited, status=1/FAILURE",
            )
        return rows


def build_collectors(
    specs: list[HostSpec], timeout: float, connect_timeout: int, demo: bool
) -> dict[str, Collector | DemoCollector]:
    collectors: dict[str, Collector | DemoCollector] = {}
    for spec in specs:
        if demo:
            collectors[spec.name] = DemoCollector(spec)
        elif spec.local:
            collectors[spec.name] = LocalCollector(spec, timeout=timeout)
        else:
            collectors[spec.name] = Collector(
                spec, timeout=timeout, connect_timeout=connect_timeout
            )
    return collectors


def demo_specs(count: int = len(DEMO_HOSTS)) -> list[HostSpec]:
    """Demo fleet of `count` hosts plus the always-slow one."""
    names = [row[0] for row in DEMO_HOSTS[:count]]
    families = [row[0].rsplit("-", 1)[0] for row in DEMO_HOSTS if row[0] != "backup-1"]
    serial: dict[str, int] = {}
    while len(names) < count:
        family = families[(len(names) - len(DEMO_HOSTS)) % len(families)]
        serial[family] = serial.get(family, 1) + 1
        name = f"{family}-{serial[family]}"
        if name not in names:
            names.append(name)
    names.append(DEMO_SLOW_HOST)
    return [HostSpec(name=name, target=name, tags=("demo",)) for name in names]
