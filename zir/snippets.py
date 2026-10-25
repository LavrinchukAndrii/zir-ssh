"""Server-specific command snippets.

"Check" runs DETECT_SCRIPT (read-only) on the host; the facts it finds are
saved per host, and the snippet list is built from them every time it is shown
(so labels follow the interface language). Snippets are only typed into the
terminal, never executed by zir: the user reviews and presses Enter.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .i18n import _
from .paths import state_dir

DETECT_SCRIPT = r"""
export LC_ALL=C
if [ -r /etc/os-release ]; then . /etc/os-release; fi
echo "os_id=${ID:-linux}"
echo "os_like=${ID_LIKE:-}"
echo "os_name=${PRETTY_NAME:-Linux}"
for p in apt dnf yum apk zypper pacman; do command -v $p >/dev/null 2>&1 && { echo "pkg=$p"; break; }; done
[ "$(id -u)" = 0 ] && echo "root=1" || echo "root=0"
for c in nginx apache2ctl apachectl httpd php mysql mariadb mysqladmin psql redis-cli memcached docker \
         docker-compose ufw firewall-cmd nft iptables fail2ban-client certbot supervisorctl pm2 composer \
         wp git node npm crontab journalctl systemctl htop iotop ncdu lsof ss netstat; do
    command -v "$c" >/dev/null 2>&1 && echo "cmd=$c"
done
systemctl list-unit-files --type=service --no-legend --plain 2>/dev/null | awk '{print $1}' \
    | grep -E '^(nginx|apache2|httpd|php[0-9.]*-fpm|php-fpm|mysql|mysqld|mariadb|postgresql[0-9@.-]*|redis(-server)?|memcached|docker|fail2ban|ufw|firewalld|supervisor(d)?|cron|crond|lsws|lshttpd|caddy|haproxy|varnish|elasticsearch|rabbitmq-server|mongod|pm2-[a-z]+|sshd?)\.service$' \
    | sed 's/\.service$//' | sed 's/^/unit=/'
for d in /etc/php/*/fpm; do [ -d "$d" ] && echo "php_version=$(basename "$(dirname "$d")")"; done
command -v php >/dev/null 2>&1 && echo "php_cli=$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;' 2>/dev/null)"
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do [ -f "$f" ] && echo "nginx_site=$f"; done 2>/dev/null | head -n 30
for f in /etc/apache2/sites-enabled/*.conf /etc/httpd/conf.d/*.conf; do [ -f "$f" ] && echo "apache_site=$f"; done 2>/dev/null | head -n 30
find /var/www /srv /home /opt -maxdepth 4 -name artisan -type f 2>/dev/null | head -n 5 | sed 's|/artisan$||; s/^/laravel=/'
find /var/www /srv /home /opt -maxdepth 4 -name wp-config.php -type f 2>/dev/null | head -n 5 | sed 's|/wp-config.php$||; s/^/wordpress=/'
command -v docker >/dev/null 2>&1 && docker ps --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | sort -u | grep . | head -n 5 | sed 's/^/compose=/'
for f in /var/log/nginx/access.log /var/log/nginx/*access*.log /var/log/apache2/access.log /var/log/apache2/*access*.log /var/log/httpd/access_log; do [ -f "$f" ] && echo "access_log=$f"; done
for f in /var/log/nginx/error.log /var/log/nginx/*error*.log /var/log/apache2/error.log /var/log/apache2/*error*.log /var/log/httpd/error_log; do [ -f "$f" ] && echo "error_log=$f"; done
df -P / 2>/dev/null | awk 'NR==2 {print "root_use=" $5}'
"""

MULTI = {"cmd", "unit", "php_version", "nginx_site", "apache_site", "laravel", "wordpress", "compose", "access_log", "error_log"}


def parse_facts(raw: str) -> dict:
    facts: dict = {key: [] for key in MULTI}
    for line in raw.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or not value:
            continue
        if key in MULTI:
            if value not in facts[key]:
                facts[key].append(value)
        else:
            facts[key] = value
    return facts


# --------------------------------------------------------------------------
# Building the list
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Snippet:
    title: str
    command: str


@dataclass(frozen=True)
class Group:
    title: str
    snippets: tuple[Snippet, ...]


def build(facts: dict) -> list[Group]:
    has = set(facts.get("cmd", []))
    units = facts.get("unit", [])
    unit = lambda *names: next((u for u in units if u in names), None)  # noqa: E731
    sudo = "" if facts.get("root") == "1" else "sudo "
    pkg = facts.get("pkg", "")
    groups: list[Group] = []

    def add(title: str, items: list[tuple[str, str] | None]) -> None:
        clean = tuple(Snippet(t, c) for t, c in (i for i in items if i))
        if clean:
            groups.append(Group(title, clean))

    # System ------------------------------------------------------------------
    updates = {
        "apt": (f"{sudo}apt update && apt list --upgradable", f"{sudo}apt upgrade"),
        "dnf": ("dnf check-update", f"{sudo}dnf upgrade"),
        "yum": ("yum check-update", f"{sudo}yum update"),
        "apk": (f"{sudo}apk update && apk version -l '<'", f"{sudo}apk upgrade"),
    }.get(pkg)
    add(_("System"), [
        (_("Load, memory, uptime"), "uptime; free -h"),
        (_("Top processes by CPU"), "ps aux --sort=-%cpu | head -n 15"),
        (_("Top processes by memory"), "ps aux --sort=-%mem | head -n 15"),
        (_("Interactive process viewer"), "htop" if "htop" in has else "top"),
        (_("Disk space"), "df -h -x tmpfs -x devtmpfs"),
        (_("What takes the space on /"), f"{sudo}du -xh / --max-depth=2 2>/dev/null | sort -h | tail -n 20"),
        (_("Biggest files on /var"), f"{sudo}find /var -xdev -type f -size +100M -exec ls -lh {{}} + 2>/dev/null | sort -k5 -h | tail"),
        (_("Errors since boot"), "journalctl -p err -b --no-pager | tail -n 50") if "journalctl" in has else None,
        (_("Failed services"), "systemctl --failed") if "systemctl" in has else None,
        (_("Check for updates"), updates[0]) if updates else None,
        (_("Install updates"), updates[1]) if updates else None,
        (_("Is a reboot required?"), "[ -f /var/run/reboot-required ] && cat /var/run/reboot-required || echo no") if pkg == "apt" else None,
        (_("Listening ports"), f"{sudo}ss -tulpn"),
        (_("Who is logged in, last logins"), "w; last -n 10"),
    ])

    # Web server ----------------------------------------------------------------
    nginx = unit("nginx") or ("nginx" if "nginx" in has else None)
    if nginx:
        access = next((p for p in facts.get("access_log", []) if "nginx" in p), "/var/log/nginx/access.log")
        error = next((p for p in facts.get("error_log", []) if "nginx" in p), "/var/log/nginx/error.log")
        add("nginx", [
            (_("Test the configuration"), f"{sudo}nginx -t"),
            (_("Apply configuration (reload, no downtime)"), f"{sudo}nginx -t && {sudo}systemctl reload nginx"),
            (_("Restart"), f"{sudo}systemctl restart nginx"),
            (_("Status"), "systemctl status nginx --no-pager"),
            (_("Enabled sites"), "ls -l /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null"),
            (_("Live access log"), f"{sudo}tail -f {access}"),
            (_("Live error log"), f"{sudo}tail -f {error}"),
            (_("Top IPs in the last 10000 requests"), f"{sudo}tail -n 10000 {access} | awk '{{print $1}}' | sort | uniq -c | sort -rn | head -n 20"),
            (_("Top URLs in the last 10000 requests"), f"{sudo}tail -n 10000 {access} | awk '{{print $7}}' | sort | uniq -c | sort -rn | head -n 20"),
            (_("5xx errors in the last 10000 requests"), f"{sudo}tail -n 10000 {access} | awk '$9 ~ /^5/' | tail -n 30"),
        ])
    apache = unit("apache2", "httpd")
    if apache:
        ctl = "apache2ctl" if "apache2ctl" in has else "apachectl"
        add("Apache", [
            (_("Test the configuration"), f"{sudo}{ctl} configtest"),
            (_("Apply configuration (graceful reload)"), f"{sudo}{ctl} configtest && {sudo}systemctl reload {apache}"),
            (_("Restart"), f"{sudo}systemctl restart {apache}"),
            (_("Virtual hosts"), f"{sudo}{ctl} -S"),
            (_("Live error log"), f"{sudo}tail -f /var/log/{'apache2/error.log' if apache == 'apache2' else 'httpd/error_log'}"),
        ])

    # PHP ------------------------------------------------------------------------
    fpm_units = sorted(u for u in units if re.fullmatch(r"php[0-9.]*-fpm|php-fpm", u))
    if fpm_units or "php" in has:
        items: list[tuple[str, str] | None] = [(_("PHP version and modules"), "php -v && php -m") if "php" in has else None]
        for fpm in fpm_units:
            version = fpm[3:-4]  # php8.2-fpm -> 8.2 (Debian/Ubuntu); "" for php-fpm (RHEL)
            items += [
                (_("Test {unit} configuration", unit=fpm), f"{sudo}php-fpm{version} -t"),
                (_("Reload {unit} (no downtime)", unit=fpm), f"{sudo}systemctl reload {fpm}"),
                (_("Restart {unit}", unit=fpm), f"{sudo}systemctl restart {fpm}"),
            ]
            if version:
                items.append((_("Pool limits (pm.max_children …)"), f"grep -E '^pm' /etc/php/{version}/fpm/pool.d/*.conf"))
        items.append((_("PHP-FPM log: pool overload"), f"{sudo}grep -h max_children /var/log/php*-fpm.log 2>/dev/null | tail -n 20"))
        add("PHP", items)

    # Databases ------------------------------------------------------------------
    db_unit = unit("mariadb", "mysql", "mysqld")
    client = "mariadb" if "mariadb" in has else "mysql"
    if db_unit or "mysql" in has or "mariadb" in has:
        add("MySQL / MariaDB", [
            (_("Running queries"), f"{sudo}{client} -e 'SHOW FULL PROCESSLIST'"),
            (_("Stop a query (put its Id)"), f"{sudo}{client} -e 'KILL QUERY <Id>'"),
            (_("Server status"), f"{sudo}mysqladmin status" if "mysqladmin" in has else f"{sudo}{client} -e 'STATUS'"),
            (_("Database sizes"), f"{sudo}{client} -e \"SELECT table_schema AS db, ROUND(SUM(data_length+index_length)/1024/1024) AS mb FROM information_schema.tables GROUP BY 1 ORDER BY 2 DESC\""),
            (_("InnoDB status (locks, deadlocks)"), f"{sudo}{client} -e 'SHOW ENGINE INNODB STATUS\\G' | less"),
            (_("Back up one database"), f"{sudo}mysqldump --single-transaction <db> | gzip > ~/<db>-$(date +%F).sql.gz"),
            (_("Restart"), f"{sudo}systemctl restart {db_unit}") if db_unit else None,
        ])
    pg = next((u for u in units if u.startswith("postgresql")), None)
    if pg or "psql" in has:
        add("PostgreSQL", [
            (_("Running queries"), "sudo -u postgres psql -c \"SELECT pid, now()-query_start AS age, state, left(query, 80) FROM pg_stat_activity WHERE state <> 'idle' ORDER BY age DESC\""),
            (_("Database sizes"), "sudo -u postgres psql -c \"SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database ORDER BY pg_database_size(datname) DESC\""),
            (_("Restart"), f"{sudo}systemctl restart {pg}") if pg else None,
        ])
    if unit("redis-server", "redis") or "redis-cli" in has:
        add("Redis", [
            (_("Live statistics"), "redis-cli --stat"),
            (_("Memory and clients"), "redis-cli info memory | head -n 12; redis-cli info clients"),
            (_("Slow commands"), "redis-cli slowlog get 10"),
        ])

    # Containers -------------------------------------------------------------------
    if "docker" in has:
        items = [
            (_("Running containers"), "docker ps"),
            (_("Resource usage"), "docker stats --no-stream"),
            (_("Disk used by Docker"), "docker system df"),
            (_("Logs of a container"), "docker logs -f --tail 100 <container>"),
        ]
        for path in facts.get("compose", []):
            items += [(_("Compose in {path}: status", path=path), f"cd {path} && docker compose ps"),
                      (_("Compose in {path}: logs", path=path), f"cd {path} && docker compose logs -f --tail 100")]
        add("Docker", items)

    # Firewall / attacks -------------------------------------------------------------
    firewall: list[tuple[str, str] | None] = [
        (_("Connections per IP (top 20)"), "ss -Htn state established | awk '{print $4}' | sed 's/:[0-9]*$//' | sort | uniq -c | sort -rn | head -n 20"),
        (_("Half-open connections (SYN flood)"), "ss -Htn state syn-recv | wc -l"),
        (_("Connection summary"), "ss -s"),
    ]
    if "ufw" in has or unit("ufw"):
        firewall += [(_("Firewall rules"), f"{sudo}ufw status numbered"),
                     (_("Block an IP"), f"{sudo}ufw insert 1 deny from <IP>"),
                     (_("Unblock: delete rule by number"), f"{sudo}ufw delete <N>")]
    elif "firewall-cmd" in has or unit("firewalld"):
        firewall += [(_("Firewall rules"), f"{sudo}firewall-cmd --list-all"),
                     (_("Block an IP"), f"{sudo}firewall-cmd --add-rich-rule=\"rule family='ipv4' source address='<IP>' reject\""),
                     (_("Unblock an IP"), f"{sudo}firewall-cmd --remove-rich-rule=\"rule family='ipv4' source address='<IP>' reject\"")]
    elif "nft" in has:
        firewall += [(_("Firewall rules"), f"{sudo}nft list ruleset")]
    elif "iptables" in has:
        firewall += [(_("Firewall rules"), f"{sudo}iptables -L -n --line-numbers"),
                     (_("Block an IP"), f"{sudo}iptables -I INPUT -s <IP> -j DROP")]
    if "fail2ban-client" in has:
        firewall += [(_("fail2ban: jails"), f"{sudo}fail2ban-client status"),
                     (_("fail2ban: banned in sshd"), f"{sudo}fail2ban-client status sshd"),
                     (_("fail2ban: unban an IP"), f"{sudo}fail2ban-client unban <IP>")]
    add(_("Firewall and attacks"), firewall)

    # SSL ------------------------------------------------------------------------------
    if "certbot" in has:
        add(_("SSL certificates"), [
            (_("Certificates and expiry dates"), f"{sudo}certbot certificates"),
            (_("Test renewal"), f"{sudo}certbot renew --dry-run"),
        ])

    # Apps -------------------------------------------------------------------------------
    for path in facts.get("laravel", []):
        add(f"Laravel · {path}", [
            (_("Application status"), f"cd {path} && php artisan about"),
            (_("Clear caches"), f"cd {path} && php artisan optimize:clear"),
            (_("Queue failures"), f"cd {path} && php artisan queue:failed"),
            (_("Live application log"), f"tail -f {path}/storage/logs/laravel*.log"),
        ])
    for path in facts.get("wordpress", []):
        add(f"WordPress · {path}", [
            (_("Plugin list and updates"), f"cd {path} && wp plugin list --allow-root") if "wp" in has else None,
            (_("Core version and updates"), f"cd {path} && wp core check-update --allow-root") if "wp" in has else None,
            (_("Recently changed PHP files (malware check)"), f"find {path} -name '*.php' -mtime -2 -ls | head -n 30"),
        ])
    if "supervisorctl" in has:
        add("Supervisor", [(_("Processes"), f"{sudo}supervisorctl status"), (_("Restart all"), f"{sudo}supervisorctl restart all")])
    if "pm2" in has:
        add("PM2", [(_("Processes"), "pm2 ls"), (_("Logs"), "pm2 logs --lines 100")])
    return groups


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def _path(host: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return state_dir() / "snippets" / f"{safe}.json"


def load(host: str) -> dict | None:
    try:
        data = json.loads(_path(host).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("facts"), dict) else None


def save(host: str, facts: dict) -> dict:
    data = {"checked_at": time.time(), "facts": facts}
    path = _path(host)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except OSError:
        pass
    return data


def found_summary(facts: dict) -> str:
    """Short 'nginx, PHP 8.2, MariaDB, Docker' line."""
    units = set(facts.get("unit", []))
    has = set(facts.get("cmd", []))
    parts = []
    for name, keys in (("nginx", {"nginx"}), ("Apache", {"apache2", "httpd"}), ("LiteSpeed", {"lsws", "lshttpd"})):
        if units & keys:
            parts.append(name)
    versions = facts.get("php_version") or ([facts["php_cli"]] if facts.get("php_cli") else [])
    if versions:
        parts.append("PHP " + ", ".join(versions))
    if units & {"mariadb"}:
        parts.append("MariaDB")
    elif units & {"mysql", "mysqld"}:
        parts.append("MySQL")
    if any(u.startswith("postgresql") for u in units):
        parts.append("PostgreSQL")
    for name, key in (("Redis", "redis-server"), ("Docker", "docker"), ("fail2ban", "fail2ban"), ("UFW", "ufw"), ("firewalld", "firewalld")):
        if key in units or key in has:
            parts.append(name)
    if facts.get("laravel"):
        parts.append("Laravel")
    if facts.get("wordpress"):
        parts.append("WordPress")
    return ", ".join(parts)
