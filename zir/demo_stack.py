"""Fake web / PHP / database data for --demo.

web-* run nginx + PHP-FPM (web-1 also a local MariaDB), db-* run MariaDB,
worker-* have a saturated PHP pool, and web-2 is hit by a short "attack"
every 90 seconds so the Attacks tab has something to show.
"""

from __future__ import annotations

import random
import time
from collections import deque

from .services import (
    LogFile, Mysql, MysqlQuery, PhpPool, ProcGroup, ServiceStatus, Stack, StackMetrics, WebTraffic, median,
)

ATTACK_EVERY = 90
ATTACK_FOR = 30
ATTACKER = "45.141.84.66"  # must be a public address: private/test ranges are ignored


class DemoStack:
    def __init__(self, name: str, rng: random.Random) -> None:
        self.name = name
        self.rng = rng
        family = name.rsplit("-", 1)[0]
        self.web = family in ("web", "edge")
        self.php = family in ("web", "worker")
        self.db = family == "db" or name == "web-1"
        self.base_rps = rng.uniform(40, 160) if self.web else 0.0
        self.questions = rng.randint(10**6, 10**7)
        self.cpu_seconds: dict[str, float] = {}
        self.history: deque[float] = deque(maxlen=150)
        self.booted = time.monotonic() - 86400 * rng.uniform(3, 30)

    def attacking(self) -> bool:
        return self.name == "web-2" and time.time() % ATTACK_EVERY < ATTACK_FOR

    def build(self, cpus: int) -> tuple[Stack, StackMetrics]:
        rng, stack, rates = self.rng, Stack(), StackMetrics()
        attack = self.attacking()

        def group(kind: str, name: str, count: int, cpu_pct: float, rss_mb: float, running: int) -> None:
            key = f"{kind}|{name}"
            self.cpu_seconds[key] = self.cpu_seconds.get(key, 0.0) + cpu_pct / 100 * 2
            stack.groups.append(ProcGroup(kind, name, count, self.cpu_seconds[key], int(rss_mb * 2**20), running))
            rates.group_cpu[key] = cpu_pct

        if self.web:
            rps = self.base_rps * rng.uniform(0.8, 1.25) * (14 if attack else 1)
            group("nginx", "nginx", cpus + 1, min(95, rps / 20), 60 + cpus * 12, rng.randint(0, 2))
            stack.web = WebTraffic(files=[LogFile(1, f"/var/log/nginx/{self.name}.example.com.access.log", "1", 10**8, 1, 1)])
            rates.rps = rps
            self.history.append(min(rps, self.base_rps * 1.3))
            rates.rps_baseline = median(self.history) if len(self.history) >= 3 else None
            errors = 0.35 if attack else rng.uniform(0.002, 0.02)
            rates.status_rps = {"2": rps * (0.9 - errors), "3": rps * 0.06, "4": rps * 0.04, "5": rps * errors}
            ips = [(rps * share, f"198.51.100.{i * 7 + 3}") for i, share in enumerate((0.07, 0.05, 0.04, 0.03, 0.02))]
            if attack:
                ips.insert(0, (rps * 0.62, ATTACKER))
            rates.ip_rps = ips
            paths = ["/", "/catalog", "/api/cart", "/static/app.js", "/search"]
            rates.path_rps = [(rps * share, path) for path, share in zip(paths, (0.3, 0.2, 0.15, 0.1, 0.05))]
            if attack:
                rates.path_rps.insert(0, (rps * 0.55, "/wp-login.php"))
                rates.bruteforce_rps = rps * 0.55
            rates.file_rps = {f"/var/log/nginx/{self.name}.example.com.access.log": rps * 0.7,
                              "/var/log/nginx/shop.example.com.access.log": rps * 0.3}
            stack.web_connections = int(rps * 1.5)
            stack.tcp_states = {"ESTAB": int(rps * 1.8), "TIME-WAIT": int(rps * 3), "SYN-RECV": 420 if attack else rng.randint(0, 4)}
            stack.peers = [(int(rps * 0.4), ATTACKER)] if attack else []
            stack.peers += [(rng.randint(2, 30), f"198.51.100.{i * 7 + 3}") for i in range(5)]
            stack.peers.sort(reverse=True)
            stack.conntrack = (int(rps * (40 if attack else 4)), 262144)

        if self.php:
            limit = 8 if self.name.startswith("worker") else 24
            busy = limit if self.name.startswith("worker") else rng.randint(1, 6) + (12 if attack else 0)
            group("php-fpm", "master", 1, 0.1, 20, 0)
            group("php-fpm", "www", min(limit, busy + 2), busy * 6.0, 40 * (busy + 2), min(busy, limit))
            stack.php_pools = [PhpPool("www", limit, "dynamic")]
            if self.name.startswith("worker"):
                stack.php_warnings = ["[23-Oct-2026 09:12:44] WARNING: [pool www] server reached pm.max_children setting (8), consider raising it"]

        if self.db:
            qps = rng.uniform(150, 900) * (3 if attack else 1)
            self.questions += int(qps * 2)
            connected = rng.randint(8, 40) + (90 if attack else 0)
            group("db", "mariadbd", 1, min(90, qps / 15), 2048, rng.randint(0, 3))
            stack.mysql = Mysql(
                "ok",
                status={"Questions": self.questions, "Threads_connected": connected, "Threads_running": rng.randint(1, 5),
                        "Max_used_connections": 141, "Innodb_buffer_pool_read_requests": 10**9,
                        "Innodb_buffer_pool_reads": 10**9 // rng.randint(300, 3000)},
                variables={"version": "10.11.6-MariaDB", "version_comment": "Debian 12", "max_connections": "151"},
                queries=[
                    MysqlQuery(rng.randint(0, 4), "shop", "shop", "Sending data",
                               "SELECT p.* FROM products p JOIN stock s ON s.product_id = p.id WHERE s.qty > 0 ORDER BY p.rank LIMIT 50"),
                    MysqlQuery(rng.randint(0, 1), "shop", "shop", "updating", "UPDATE carts SET updated_at = NOW() WHERE id = 81723"),
                ],
            )
            rates.qps = qps
            rates.db_rates = {"Com_select": qps * 0.8, "Com_insert": qps * 0.1, "Com_update": qps * 0.08, "Com_delete": qps * 0.02}
            rates.slow_per_min = rng.choice((0, 0, 0, 1, 2))
        stack.services = self.services()
        return stack, rates

    def services(self) -> list[ServiceStatus]:
        up = time.monotonic() - self.booted

        def svc(unit: str, name: str, group: str, state: str = "running", enabled: str = "enabled") -> ServiceStatus:
            active = {"running": "active", "stopped": "inactive", "failed": "failed"}[state]
            sub = {"running": "running", "stopped": "dead", "failed": "failed"}[state]
            memory = (sum(map(ord, unit)) % 40 + 2) * 12 * 2**20 if state == "running" else None
            return ServiceStatus(unit, name, group, active, sub, enabled, "notify",
                                 up if state == "running" else None, memory, 0)

        items = [svc("ssh", "SSH", "system"), svc("cron", "cron", "system")]
        if self.web:
            items += [svc("nginx", "nginx", "web"), svc("fail2ban", "fail2ban", "system"),
                      svc("apache2", "Apache", "web", "stopped", "disabled")]
        if self.php:
            items.append(svc("php8.3-fpm", "PHP-FPM", "php"))
        if self.db:
            items.append(svc("mariadb", "MariaDB", "db"))
        if self.name == "web-1":
            items += [svc("pure-ftpd", "Pure-FTPd", "ftp"), svc("exim4", "Exim", "mail"), svc("redis-server", "Redis", "db")]
        if self.name.startswith("worker"):
            items += [svc("supervisor", "Supervisor", "system"), svc("redis-server", "Redis", "db")]
        if self.name.startswith("cache"):
            items += [svc("memcached", "Memcached", "db"), svc("pure-ftpd", "Pure-FTPd", "ftp", "stopped")]
        rank = {"crit": 0, "warn": 1, "ok": 2, "off": 3}
        return sorted(items, key=lambda s: (rank[s.state], s.group, s.unit))
