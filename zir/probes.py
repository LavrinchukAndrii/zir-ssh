"""Remote probes and parsers.

Everything here is deliberately read-only.  A single shell script is sent to a
host over SSH; the output is split into named sections and parsed locally.
"""

from __future__ import annotations

import re
import time

from dataclasses import dataclass, field

from .i18n import _
from .services import STACK_SCRIPT, Stack, StackMetrics, StackTracker, evaluate_stack, parse_stack, script_header

# --------------------------------------------------------------------------
# Remote script
# --------------------------------------------------------------------------

PROBE_SCRIPT = r"""
export LC_ALL=C LANG=C
sec() { printf '\n__SEC__%s\n' "$1"; }
# One process list and one unit list for every probe below.
PS=$(ps -e -o pid= -o user= -o pcpu= -o pmem= -o rss= -o etime= -o stat= -o comm= -o args= --sort=-pcpu 2>/dev/null)
INIT=$(printf '%s\n' "$PS" | awk '$1 == 1 { print $8; exit }')
SERVICES=$(systemctl list-units --type=service --all --no-legend --plain 2>/dev/null)

sec meta
printf 'uptime=%s\n' "$(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo 0)"
# ZIR_QUICK=1: things that rarely change are skipped and kept from the previous full poll.
if [ "$ZIR_QUICK" != 1 ]; then
printf 'hostname=%s\n' "$(hostname 2>/dev/null || echo unknown)"
if [ -r /etc/os-release ]; then
    . /etc/os-release 2>/dev/null
    printf 'os=%s\n' "${PRETTY_NAME:-Linux}"
else
    printf 'os=%s\n' "Linux"
fi
printf 'kernel=%s\n' "$(uname -sr 2>/dev/null || echo unknown)"
printf 'cpus=%s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 0)"
printf 'ip=%s\n' "$(hostname -I 2>/dev/null | awk '{print $1}')"
printf 'init=%s\n' "$INIT"
fi

sec load
cat /proc/loadavg 2>/dev/null

sec cpu
head -n 1 /proc/stat 2>/dev/null

sec mem
cat /proc/meminfo 2>/dev/null

sec disk
df -P -B1 -x tmpfs -x devtmpfs -x squashfs 2>/dev/null

sec inode
df -Pi -x tmpfs -x devtmpfs -x squashfs 2>/dev/null

sec net
cat /proc/net/dev 2>/dev/null

sec procs
printf '%s\n' "$PS" | head -n 30

sec procs_total
printf '%s\n' "$PS" | grep -c .

sec services_failed
systemctl --failed --no-legend --plain 2>/dev/null

sec services_running
printf '%s\n' "$SERVICES" | awk '$4 == "running"' | wc -l

if [ "$ZIR_QUICK" != 1 ]; then
sec logs
journalctl -p warning -b -n 40 --no-pager -o short-iso 2>/dev/null

sec docker
docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null

sec who
who 2>/dev/null

sec listen
ss -tulpn 2>/dev/null | head -n 30
fi
"""


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CpuSample:
    user: int = 0
    nice: int = 0
    system: int = 0
    idle: int = 0
    iowait: int = 0
    irq: int = 0
    softirq: int = 0
    steal: int = 0

    @property
    def total(self) -> int:
        return (
            self.user
            + self.nice
            + self.system
            + self.idle
            + self.iowait
            + self.irq
            + self.softirq
            + self.steal
        )

    @property
    def idle_total(self) -> int:
        return self.idle + self.iowait

    def percent_since(self, previous: "CpuSample") -> float | None:
        total = self.total - previous.total
        idle = self.idle_total - previous.idle_total
        if total <= 0 or idle < 0:
            return None
        return max(0.0, min(100.0, 100.0 * (total - idle) / total))


@dataclass(frozen=True)
class NetCounters:
    rx: int = 0
    tx: int = 0


@dataclass(frozen=True)
class DiskUsage:
    filesystem: str
    mount: str
    size: int
    used: int
    available: int
    percent: float
    inode_percent: float = 0.0


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    user: str
    cpu: float
    mem: float
    rss: int
    etime: str
    state: str
    comm: str
    args: str


@dataclass(frozen=True)
class ServiceFailure:
    unit: str
    state: str
    description: str


@dataclass(frozen=True)
class DockerContainer:
    name: str
    image: str
    status: str


@dataclass(frozen=True)
class ListenSocket:
    proto: str
    local: str
    process: str


@dataclass
class Snapshot:
    taken_at: float = field(default_factory=time.time)
    hostname: str = "unknown"
    os: str = "Linux"
    kernel: str = "unknown"
    uptime: float = 0.0
    cpus: int = 0
    ip: str = ""
    init: str = ""
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cpu: CpuSample = field(default_factory=CpuSample)
    mem_total: int = 0
    mem_available: int = 0
    mem_free: int = 0
    cached: int = 0
    buffers: int = 0
    dirty: int = 0
    swap_total: int = 0
    swap_free: int = 0
    disks: list[DiskUsage] = field(default_factory=list)
    net: dict[str, NetCounters] = field(default_factory=dict)
    procs: list[ProcessInfo] = field(default_factory=list)
    procs_total: int = 0
    failed_services: list[ServiceFailure] = field(default_factory=list)
    running_services: int = 0
    logs: list[str] = field(default_factory=list)
    docker: list[DockerContainer] = field(default_factory=list)
    who: list[str] = field(default_factory=list)
    listening: list[ListenSocket] = field(default_factory=list)
    stack: Stack = field(default_factory=Stack)

    # -- derived values ----------------------------------------------------

    @property
    def mem_used(self) -> int:
        return max(self.mem_total - self.mem_available, 0)

    @property
    def mem_percent(self) -> float:
        if self.mem_total <= 0:
            return 0.0
        return 100.0 * self.mem_used / self.mem_total

    @property
    def swap_used(self) -> int:
        return max(self.swap_total - self.swap_free, 0)

    @property
    def swap_percent(self) -> float:
        if self.swap_total <= 0:
            return 0.0
        return 100.0 * self.swap_used / self.swap_total

    @property
    def boot_time(self) -> float:
        return self.taken_at - self.uptime

    @property
    def worst_disk(self) -> DiskUsage | None:
        return max(self.disks, key=lambda d: d.percent, default=None)

    @property
    def load_per_cpu(self) -> float:
        if self.cpus <= 0:
            return 0.0
        return self.load[0] / self.cpus


@dataclass
class Metrics:
    cpu_pct: float | None = None
    net_rates: dict[str, tuple[float, float]] = field(default_factory=dict)
    stack: StackMetrics = field(default_factory=StackMetrics)


@dataclass(frozen=True)
class Finding:
    level: str  # "warn" | "crit"
    message: str
    topic: str = ""  # "" (system) | ddos | web | php | db


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def _split_sections(raw: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.replace("\r\n", "\n").splitlines():
        if line.startswith("__SEC__"):
            current = line[len("__SEC__") :].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _kv(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_cpu(line: str) -> CpuSample:
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return CpuSample()
    values = [_to_int(part) for part in parts[1:11]]
    values += [0] * (10 - len(values))
    return CpuSample(*values[:8])


def parse_meminfo(lines: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        number = value.strip().split()[0] if value.strip() else "0"
        result[key] = _to_int(number) * 1024
    return result


def parse_df(lines: list[str]) -> list[DiskUsage]:
    disks: list[DiskUsage] = []
    for line in lines:
        if not line.strip() or line.lower().startswith("filesystem"):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        filesystem, size, used, available, percent, mount = parts
        size_bytes = _to_int(size)
        if size_bytes <= 0:
            continue
        disks.append(
            DiskUsage(
                filesystem=filesystem,
                mount=mount,
                size=size_bytes,
                used=_to_int(used),
                available=_to_int(available),
                percent=_to_float(percent.rstrip("%")),
            )
        )
    return disks


def parse_inodes(lines: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for line in lines:
        if not line.strip() or line.lower().startswith("filesystem"):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        result[parts[5]] = _to_float(parts[4].rstrip("%"))
    return result


def parse_net(lines: list[str]) -> dict[str, NetCounters]:
    result: dict[str, NetCounters] = {}
    for line in lines:
        if ":" not in line or "|" in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        values = rest.split()
        if not name or len(values) < 9:
            continue
        result[name] = NetCounters(rx=_to_int(values[0]), tx=_to_int(values[8]))
    return result


def parse_procs(lines: list[str]) -> list[ProcessInfo]:
    procs: list[ProcessInfo] = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        parts += [""] * (9 - len(parts))
        pid = _to_int(parts[0], -1)
        if pid < 0:
            continue
        comm = parts[7].strip()
        args = parts[8].strip() or comm
        procs.append(
            ProcessInfo(
                pid=pid,
                user=parts[1],
                cpu=_to_float(parts[2]),
                mem=_to_float(parts[3]),
                rss=_to_int(parts[4]) * 1024,
                etime=parts[5],
                state=parts[6],
                comm=comm,
                args=args,
            )
        )
    return procs


def parse_services(lines: list[str]) -> list[ServiceFailure]:
    services: list[ServiceFailure] = []
    for line in lines:
        stripped = line.strip().lstrip("*\u25cf ").strip()
        if not stripped or "loaded units listed" in stripped.lower():
            continue
        parts = stripped.split(None, 4)
        if len(parts) < 4 or "." not in parts[0]:
            continue
        services.append(
            ServiceFailure(
                unit=parts[0],
                state=parts[3],
                description=parts[4] if len(parts) > 4 else "",
            )
        )
    return services


def parse_docker(lines: list[str]) -> list[DockerContainer]:
    containers: list[DockerContainer] = []
    for line in lines:
        if not line.strip() or "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        containers.append(
            DockerContainer(name=parts[0], image=parts[1], status=parts[2])
        )
    return containers


_PROCESS_RE = re.compile(r'users:\(\("([^"]+)"')


def parse_listen(lines: list[str]) -> list[ListenSocket]:
    sockets: list[ListenSocket] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("Netid", "State")):
            continue
        parts = stripped.split(None, 5)
        if len(parts) < 5 or parts[0] not in {"tcp", "udp", "tcp6", "udp6"}:
            continue
        match = _PROCESS_RE.search(stripped)
        sockets.append(
            ListenSocket(
                proto=parts[0],
                local=parts[4],
                process=match.group(1) if match else "",
            )
        )
    return sockets


def parse_snapshot(raw: str) -> Snapshot:
    sections = _split_sections(raw)
    meta = _kv(sections.get("meta", []))
    mem = parse_meminfo(sections.get("mem", []))
    inodes = parse_inodes(sections.get("inode", []))

    disks = parse_df(sections.get("disk", []))
    if inodes:
        disks = [
            DiskUsage(
                filesystem=disk.filesystem,
                mount=disk.mount,
                size=disk.size,
                used=disk.used,
                available=disk.available,
                percent=disk.percent,
                inode_percent=inodes.get(disk.mount, 0.0),
            )
            for disk in disks
        ]

    load_parts = (sections.get("load", [""])[0] if sections.get("load") else "").split()
    load = tuple(_to_float(part) for part in load_parts[:3])
    load = tuple(list(load) + [0.0] * (3 - len(load)))  # type: ignore[assignment]

    return Snapshot(
        taken_at=time.time(),
        hostname=meta.get("hostname", "unknown"),
        os=meta.get("os", "Linux"),
        kernel=meta.get("kernel", "unknown"),
        uptime=_to_float(meta.get("uptime", "0")),
        cpus=_to_int(meta.get("cpus", "0")),
        ip=meta.get("ip", ""),
        init=meta.get("init", ""),
        load=load,  # type: ignore[arg-type]
        cpu=parse_cpu(sections.get("cpu", [""])[0] if sections.get("cpu") else ""),
        mem_total=mem.get("MemTotal", 0),
        mem_available=mem.get("MemAvailable", mem.get("MemFree", 0)),
        mem_free=mem.get("MemFree", 0),
        cached=mem.get("Cached", 0),
        buffers=mem.get("Buffers", 0),
        dirty=mem.get("Dirty", 0),
        swap_total=mem.get("SwapTotal", 0),
        swap_free=mem.get("SwapFree", 0),
        disks=disks,
        net=parse_net(sections.get("net", [])),
        procs=parse_procs(sections.get("procs", [])),
        procs_total=_to_int(
            (sections.get("procs_total", ["0"])[0] if sections.get("procs_total") else "0")
        ),
        failed_services=parse_services(sections.get("services_failed", [])),
        running_services=_to_int(
            (
                sections.get("services_running", ["0"])[0]
                if sections.get("services_running")
                else "0"
            )
        ),
        logs=[line for line in sections.get("logs", []) if line.strip()],
        docker=parse_docker(sections.get("docker", [])),
        who=[line for line in sections.get("who", []) if line.strip()],
        listening=parse_listen(sections.get("listen", [])),
        stack=parse_stack(sections),
    )


# --------------------------------------------------------------------------
# Derived metrics and health rules
# --------------------------------------------------------------------------


def build_probe(previous: Snapshot | None, extra_logs: tuple[str, ...] = (), quick: bool = False) -> str:
    """The script for one poll: where the access logs were read up to, then the probes.
    `quick` skips the slow probes (see `carry_slow`); it needs a previous snapshot."""
    if previous is None:
        return script_header([], extra_logs) + PROBE_SCRIPT + STACK_SCRIPT
    mysql = previous.stack.mysql
    return (
        script_header(
            previous.stack.web.files,
            extra_logs,
            tuple(previous.stack.php_versions),
            quick=quick,
            # Only a login that worked: a failing one would be retried before the search.
            db_auth=mysql.auth if mysql.state == "ok" else "",
        )
        + PROBE_SCRIPT
        + STACK_SCRIPT
    )


# Filled only by a full poll; a quick one keeps them from the previous snapshot.
SLOW_FIELDS = ("hostname", "os", "kernel", "cpus", "ip", "init", "logs", "docker", "who", "listening")
SLOW_STACK_FIELDS = ("php_pools", "php_warnings", "synflood_messages")


def carry_slow(snapshot: Snapshot, previous: Snapshot) -> None:
    """Complete a quick poll with what the last full poll found."""
    for name in SLOW_FIELDS:
        setattr(snapshot, name, getattr(previous, name))
    for name in SLOW_STACK_FIELDS:
        setattr(snapshot.stack, name, getattr(previous.stack, name))
    web = snapshot.stack.web
    web.unreadable = list(dict.fromkeys(previous.stack.web.unreadable + web.unreadable))


def compute_metrics(
    snapshot: Snapshot, previous: Snapshot | None, tracker: StackTracker | None = None
) -> Metrics:
    if previous is None:
        return Metrics()

    dt = max(snapshot.taken_at - previous.taken_at, 0.001)
    rates: dict[str, tuple[float, float]] = {}
    for name, current in snapshot.net.items():
        old = previous.net.get(name)
        if old is None:
            continue
        rx = max(current.rx - old.rx, 0) / dt
        tx = max(current.tx - old.tx, 0) / dt
        rates[name] = (rx, tx)

    return Metrics(
        cpu_pct=snapshot.cpu.percent_since(previous.cpu),
        net_rates=rates,
        stack=(tracker or StackTracker()).compute(snapshot.stack, previous.stack, dt),
    )


def evaluate(snapshot: Snapshot, metrics: Metrics) -> list[Finding]:
    findings: list[Finding] = []

    if metrics.cpu_pct is not None:
        if metrics.cpu_pct >= 95:
            findings.append(Finding("crit", _("CPU at {pct}%", pct=f"{metrics.cpu_pct:.0f}")))
        elif metrics.cpu_pct >= 85:
            findings.append(Finding("warn", _("CPU at {pct}%", pct=f"{metrics.cpu_pct:.0f}")))

    if snapshot.mem_total:
        if snapshot.mem_percent >= 95:
            findings.append(Finding("crit", _("memory at {pct}%", pct=f"{snapshot.mem_percent:.0f}")))
        elif snapshot.mem_percent >= 85:
            findings.append(Finding("warn", _("memory at {pct}%", pct=f"{snapshot.mem_percent:.0f}")))

    if snapshot.swap_total and snapshot.swap_percent >= 80:
        level = "crit" if snapshot.swap_percent >= 95 else "warn"
        findings.append(Finding(level, _("swap at {pct}%", pct=f"{snapshot.swap_percent:.0f}")))

    if snapshot.cpus and snapshot.load_per_cpu >= 2:
        level = "crit" if snapshot.load_per_cpu >= 4 else "warn"
        findings.append(
            Finding(
                level,
                _("load {load} on {cpus} vCPU", load=f"{snapshot.load[0]:.1f}", cpus=snapshot.cpus),
            )
        )

    for disk in snapshot.disks:
        if disk.percent >= 95:
            findings.append(Finding("crit", _("disk {mount} at {pct}%", mount=disk.mount, pct=f"{disk.percent:.0f}")))
        elif disk.percent >= 85:
            findings.append(Finding("warn", _("disk {mount} at {pct}%", mount=disk.mount, pct=f"{disk.percent:.0f}")))
        if disk.inode_percent >= 95:
            findings.append(Finding("crit", _("inodes {mount} at {pct}%", mount=disk.mount, pct=f"{disk.inode_percent:.0f}")))
        elif disk.inode_percent >= 90:
            findings.append(Finding("warn", _("inodes {mount} at {pct}%", mount=disk.mount, pct=f"{disk.inode_percent:.0f}")))

    for service in snapshot.failed_services:
        findings.append(Finding("crit", _("failed unit: {unit}", unit=service.unit)))

    for level, topic, message in evaluate_stack(snapshot.stack, metrics.stack, snapshot.cpus):
        findings.append(Finding(level, message, topic))

    findings.sort(key=lambda finding: 0 if finding.level == "crit" else 1)
    return findings


def severity(snapshot: Snapshot | None, metrics: Metrics, error: str | None) -> str:
    if error:
        return "error"
    if snapshot is None:
        return "error"
    return level_of(evaluate(snapshot, metrics))


def level_of(findings: list[Finding]) -> str:
    if any(finding.level == "crit" for finding in findings):
        return "crit"
    if findings:
        return "warn"
    return "ok"
