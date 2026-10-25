"""Web server, PHP-FPM, MySQL/MariaDB and DDoS signals.

Read-only like everything else: access logs are read incrementally (only the
bytes written since the previous poll, capped per poll so a flood cannot make
the probe itself heavy), MySQL is asked SHOW STATUS / PROCESSLIST through the
local socket of the SSH user. Nothing is configured on the server.
"""

from __future__ import annotations

import ipaddress
import re
from collections import deque
from dataclasses import dataclass, field

from .i18n import _

# Bytes of new access-log data read per poll (all files together).
LOG_READ_CAP = 6_000_000
TOP = 10

# --------------------------------------------------------------------------
# Remote script (appended to PROBE_SCRIPT; ZIR_LOGS holds previous offsets)
# --------------------------------------------------------------------------

STACK_SCRIPT = r"""
sec stack_procs
# $PS columns: pid user pcpu pmem rss etime stat comm args (set by PROBE_SCRIPT).
FPM_EXES=""
for pid in $(printf '%s\n' "$PS" | awk '($8 ~ /^php-fpm/ || $8 ~ /^php[0-9.]*-fpm/) && / master process/ { print $1 }'); do
    FPM_EXES="$FPM_EXES $pid=$(readlink "/proc/$pid/exe" 2>/dev/null)"
done
printf '%s\n' "$PS" | awk -v hz="$(getconf CLK_TCK 2>/dev/null || echo 100)" -v exes="$FPM_EXES" '
BEGIN {
    ne = split(exes, e, " ")
    for (i = 1; i <= ne; i++) { p = index(e[i], "="); exe[substr(e[i], 1, p - 1)] = substr(e[i], p + 1) }
}
NF >= 8 {
    pid = $1; comm = $8; rss = $5; st = $7; args = ""
    for (i = 9; i <= NF; i++) args = args " " $i
    kind = ""; group = comm
    if (comm ~ /^nginx/) kind = "nginx"
    else if (comm == "apache2" || comm == "httpd") kind = "apache"
    else if (comm ~ /^(lshttpd|litespeed|openlitespeed|lsphp)/) kind = "litespeed"
    else if (comm == "caddy") kind = "caddy"
    else if (comm ~ /^php-fpm/ || comm ~ /^php[0-9.]*-fpm/) {
        kind = "php-fpm"
        if (args ~ /master process/) group = "master"
        else if (match(args, /pool [^ ]+/)) group = substr(args, RSTART + 5, RLENGTH - 5)
    }
    else if (comm == "mysqld" || comm == "mariadbd") kind = "db"
    if (kind == "") next
    # utime + stime in clock ticks: finer than ps "times" (whole seconds).
    ticks = 0; line = ""
    if ((getline line < ("/proc/" pid "/stat")) > 0) {
        sub(/^.*\) /, "", line); split(line, f, " "); ticks = f[12] + f[13]
    }
    close("/proc/" pid "/stat")
    key = kind "|" group
    n[key]++; t[key] += ticks; r[key] += rss
    if (st ~ /^[RD]/) run[key]++
    if (kind == "php-fpm" && group == "master") {
        conf = args; sub(/^.*\(/, "", conf); sub(/\).*$/, "", conf)
        masters[pid] = exe[pid] "|" conf
    }
}
END {
    for (k in n) printf "%s|%d|%.2f|%d|%d\n", k, n[k], t[k] / hz, r[k] * 1024, run[k] + 0
    for (p in masters) printf "M|%s\n", masters[p]
}'

if [ "$ZIR_QUICK" != 1 ]; then
sec php_pools
fpm_files=""
for c in $(printf '%s\n' "$PS" | sed -n 's/^.* php-fpm: master process (\(.*\))$/\1/p' | sort -u); do
    [ -r "$c" ] || continue
    fpm_files="$fpm_files $c"
    for inc in $(sed -n 's/^[[:space:]]*include[[:space:]]*=[[:space:]]*//p' "$c" | tr -d '"' ); do
        case "$inc" in /*) ;; *) inc="$(dirname "$c")/$inc" ;; esac
        fpm_files="$fpm_files $inc"
    done
done
for f in $fpm_files /etc/php/*/fpm/pool.d/*.conf /etc/php5/fpm/pool.d/*.conf /etc/php-fpm.d/*.conf \
         /etc/opt/remi/php*/php-fpm.d/*.conf /opt/remi/php*/root/etc/php-fpm.d/*.conf \
         /usr/local/etc/php-fpm.d/*.conf /opt/alt/php*/etc/php-fpm.d/*.conf /etc/php*/php-fpm.d/*.conf \
         /opt/php*/etc/php-fpm.d/*.conf /usr/local/php*/etc/php-fpm.d/*.conf \
         /opt/cpanel/ea-php*/root/etc/php-fpm.d/*.conf /opt/plesk/php/*/etc/php-fpm.d/*.conf; do
    [ -r "$f" ] || continue
    awk '/^[ \t]*\[[^]]+\]/ { pool = $0; gsub(/[][ \t]/, "", pool) }
         /^[ \t]*pm\.max_children[ \t]*=/ { v = $0; sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t;].*$/, "", v); max[pool] = v }
         /^[ \t]*pm[ \t]*=/ { v = $0; sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t;].*$/, "", v); mode[pool] = v }
         END { for (p in max) if (p != "global") print p "|" max[p] "|" mode[p]
               for (p in mode) if (!(p in max) && p != "global") print p "||" mode[p] }' "$f" 2>/dev/null
done | sort -u | head -n 60

sec php_warnings
for f in /var/log/php*-fpm.log /var/log/php-fpm/*.log /var/log/php*/fpm*.log /opt/remi/php*/root/var/log/php-fpm/*.log; do
    [ -r "$f" ] && tail -n 400 "$f" 2>/dev/null
done | grep -i "max_children" | tail -n 3
fi

sec php_versions
# `php-fpm -v` only for binaries whose version is not known yet (ZIR_PHP_KNOWN).
for exe in $(printf '%s\n' $FPM_EXES | sed 's/^[0-9]*=//' | grep . | sort -u); do
    case " $ZIR_PHP_KNOWN " in *" $exe "*) continue ;; esac
    printf 'V|%s|%s\n' "$exe" "$("$exe" -v 2>/dev/null | head -n 1)"
done

sec weblogs
budget=${ZIR_CAP:-6000000}
idx=0; denied=0; seen=""; plan=""
NL='
'
LOGS="$NL$ZIR_LOGS$NL"
# One candidate log file; returns 1 once 40 files are taken.
log_file() {
    f=$1
    if [ ! -e "$f" ]; then
        # Not visible: keep only an exact path (from the config) whose folder is closed to this user.
        case "$f" in *'*'*|*'?'*|*'['*) return 0 ;; esac
        [ -d "${f%/*}" ] && [ ! -x "${f%/*}" ] || return 0
    elif [ ! -f "$f" ]; then return 0; fi
    case "$f" in *error*|*.gz|*.[0-9]|*.bytes) return 0 ;; esac
    case "$seen" in *"<$f>"*) return 0 ;; esac
    seen="$seen<$f>"
    if [ ! -r "$f" ]; then                                              # exists, but not for this user
        denied=$((denied + 1)); [ "$denied" -le 10 ] && printf 'D|%s\n' "$f"
        return 0
    fi
    idx=$((idx + 1)); [ "$idx" -gt 40 ] && return 1
    set -- $(stat -L -c '%i %s' "$f" 2>/dev/null); ino=${1:-0}; size=${2:-0}
    prev=""
    case "$LOGS" in *"$NL$f|"*) prev=${LOGS#*"$NL$f|"}; prev=${prev%%"$NL"*}; prev="${prev%%|*} ${prev#*|}" ;; esac
    pino=${prev%% *}; poff=${prev##* }
    if [ -z "$prev" ]; then off=$size                                   # first look: start at the end
    elif [ "$pino" != "$ino" ] || [ "$size" -lt "$poff" ]; then off=0   # rotated or truncated
    else off=$poff; fi
    delta=$((size - off)); want=$delta
    [ "$want" -gt "$budget" ] && want=$budget
    budget=$((budget - want))
    printf 'F|%s|%s|%s|%s|%s|%s\n' "$idx" "$ino" "$size" "$delta" "$want" "$f"
    [ "$want" -gt 0 ] && plan="$plan$idx $size $want $f
"
    return 0
}
if [ "$ZIR_QUICK" = 1 ]; then
    # Between full polls: only the logs found last time (their offsets are in ZIR_LOGS).
    while IFS='|' read -r f _rest; do
        [ -n "$f" ] || continue
        log_file "$f" || break
    done <<EOF
$ZIR_LOGS
EOF
else
    # Paths named in the web server config (any file names); variables like $host become a glob.
    # -R: sites-enabled/* are symlinks, plain -r would skip them.
    cfg=$(grep -RhoE '^[[:space:]]*(access_log|CustomLog|TransferLog)[[:space:]]+[^[:space:];]+' \
              ${ZIR_WEBCONF:-/etc/nginx /etc/apache2 /etc/httpd} 2>/dev/null \
          | awk '{print $2}' | tr -d "\"'" | sed 's|^\${APACHE_LOG_DIR}|/var/log/apache2|' | grep '^/' \
          | sed 's/\$[{]\{0,1\}[A-Za-z_][A-Za-z0-9_]*[}]\{0,1\}/*/g' | sort -u | head -n 40)
    for f in /var/log/nginx/*access*.log /var/log/nginx/*/*access*.log /var/log/nginx/domains/*.log \
             /var/log/nginx/*.log /var/log/nginx/*/*.log \
             /var/log/apache2/*access*.log /var/log/apache2/domains/*.log /var/log/httpd/*access_log \
             /var/log/httpd/*access*.log /var/log/httpd/domains/*.log /var/www/httpd-logs/*access.log \
             /home/*/logs/*access*.log /home/domains/*/logs/*access*.log \
             /usr/local/lsws/logs/access.log /var/log/caddy/*.log $cfg $ZIR_EXTRA_LOGS; do
        log_file "$f" || break
    done
fi
sec webtraffic
printf '%s' "$plan" | while read -r idx size want f; do
    [ -n "$f" ] || continue
    # The newest `want` bytes of what was appended since the previous poll, after a marker
    # line with the file number (on its own line even if the chunk ends mid-line).
    printf '\n__zir_file__ %s\n' "$idx"
    tail -c "+$((size - want + 1))" "$f" 2>/dev/null | head -c "$want"
done | awk '
$1 == "__zir_file__" { idx = $2; next }
$0 == "" { next }
{
    line = $0
    total++; per[idx]++
    split(line, w, " "); ip = w[1]
    if (ip !~ /^[0-9a-fA-F:.]+$/ && w[2] ~ /^[0-9a-fA-F:.]+$/) ip = w[2]
    q = split(line, part, "\""); st = ""; path = ""
    if (q >= 3) {
        # The status follows the request; custom formats may put e.g. -$request_time- before it.
        n = split(part[3], s, " "); st = s[1]
        for (j = 1; j <= n && j <= 3; j++) if (s[j] ~ /^[1-5][0-9][0-9]$/) { st = s[j]; break }
        split(part[2], r, " "); path = r[2]; sub(/\?.*/, "", path)
    }
    if (st ~ /^[1-5][0-9][0-9]$/) cls[substr(st, 1, 1)]++; else cls["x"]++
    ips[ip]++
    if (path != "") { paths[path]++; if (path ~ /(wp-login\.php|xmlrpc\.php)$/) brute++ }
}
END {
    printf "T|%d|%d\n", total, brute + 0
    for (k in per) printf "P|%d|%s\n", per[k], k
    for (k in cls) printf "S|%d|%s\n", cls[k], k
    for (k in ips) printf "I|%d|%s\n", ips[k], k
    for (k in paths) printf "U|%d|%s\n", paths[k], k
}' | sort -t'|' -k1,1 -k2,2nr | awk -F'|' '{ c[$1]++ } $1 == "T" || $1 == "S" || $1 == "P" || c[$1] <= 10'

sec tcp
SS=$(ss -Htan 2>/dev/null)
printf '%s\n' "$SS" | awk 'NF { s[$1]++ } END { for (k in s) print k "|" s[k] }'
sec tcp_peers
printf '%s\n' "$SS" | awk '
$1 == "ESTAB" {
    local = $4; peer = $5
    lp = local; sub(/^.*:/, "", lp)
    if (lp == "80" || lp == "443" || lp == "8080" || lp == "8443") web++
    sub(/:[0-9]+$/, "", peer); gsub(/^\[|\]$/, "", peer); sub(/^::ffff:/, "", peer)
    if (peer ~ /^127\./ || peer == "::1") next
    c[peer]++
}
END { printf "W|%d\n", web + 0; for (k in c) printf "C|%d|%s\n", c[k], k }' | sort -t'|' -k1,1r -k2,2nr | head -n 11
sec conntrack
cat /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null
if [ "$ZIR_QUICK" != 1 ]; then
sec synflood
dmesg 2>/dev/null | tail -n 300 | grep -c "SYN flooding"
fi

sec mysql
if printf '%s\n' "$PS" | awk '$8 == "mysqld" || $8 == "mariadbd" { found = 1; exit } END { exit !found }'; then
    M=$(command -v mariadb || command -v mysql)
    T=""; command -v timeout >/dev/null 2>&1 && T="timeout 5"
    if [ -z "$M" ]; then echo "__noclient"
    else
        # Credentials that already exist on the server. Passwords never leave it:
        # they go from the file to the local mysql client only.
        db_try() { $T "$M" "$@" --connect-timeout=3 -N -B -e "SELECT 1" >/dev/null 2>&1; }
        hestia_auth() {
            u=$(sed -n "s/.*USER='\([^']*\)'.*/\1/p" "$1" | head -n 1)
            pw=$(sed -n "s/.*PASSWORD='\([^']*\)'.*/\1/p" "$1" | head -n 1)
            [ -n "$u" ] && export MYSQL_PWD="$pw" && auth="-u$u" && src="$(basename "$(dirname "$(dirname "$1")")")"
        }
        # The source that worked last time (ZIR_DB_AUTH), used without a test login.
        db_use() {
            auth=""; src="$1"
            case "$1" in
                default) ;;
                debian.cnf) [ -r /etc/mysql/debian.cnf ] && auth="--defaults-file=/etc/mysql/debian.cnf" ;;
                DirectAdmin) [ -r /usr/local/directadmin/conf/my.cnf ] && auth="--defaults-extra-file=/usr/local/directadmin/conf/my.cnf" ;;
                Plesk) [ -r /etc/psa/.psa.shadow ] && export MYSQL_PWD="$(cat /etc/psa/.psa.shadow)" && auth="-uadmin" ;;
                hestia|vesta) [ -r "/usr/local/$1/conf/mysql.conf" ] && hestia_auth "/usr/local/$1/conf/mysql.conf" ;;
                *) return 1 ;;
            esac
            [ "$1" = default ] || [ -n "$auth" ]
        }
        # Every known source, tried in order.
        db_find() {
            auth=""; src="default"; unset MYSQL_PWD
            if db_try; then :
            elif [ -r /etc/mysql/debian.cnf ] && db_try --defaults-file=/etc/mysql/debian.cnf; then
                auth="--defaults-file=/etc/mysql/debian.cnf"; src="debian.cnf"
            elif [ -r /usr/local/directadmin/conf/my.cnf ] && db_try --defaults-extra-file=/usr/local/directadmin/conf/my.cnf; then
                auth="--defaults-extra-file=/usr/local/directadmin/conf/my.cnf"; src="DirectAdmin"
            elif [ -r /etc/psa/.psa.shadow ] && MYSQL_PWD="$(cat /etc/psa/.psa.shadow)" db_try -uadmin; then
                export MYSQL_PWD="$(cat /etc/psa/.psa.shadow)"; auth="-uadmin"; src="Plesk"
            else
                for f in /usr/local/hestia/conf/mysql.conf /usr/local/vesta/conf/mysql.conf; do
                    [ -r "$f" ] || continue
                    u=$(sed -n "s/.*USER='\([^']*\)'.*/\1/p" "$f" | head -n 1)
                    pw=$(sed -n "s/.*PASSWORD='\([^']*\)'.*/\1/p" "$f" | head -n 1)
                    if [ -n "$u" ] && MYSQL_PWD="$pw" db_try -u"$u"; then
                        export MYSQL_PWD="$pw"; auth="-u$u"; src="$(basename "$(dirname "$(dirname "$f")")")"; break
                    fi
                done
            fi
        }
        # Status, variables and running queries in one client call; the marker row starts the next section.
        db_query() {
            $T "$M" $auth --connect-timeout=3 -N -B -e "SHOW GLOBAL STATUS WHERE Variable_name IN ('Questions','Com_select','Com_insert','Com_update','Com_delete','Threads_connected','Threads_running','Max_used_connections','Slow_queries','Aborted_connects','Connections','Uptime','Innodb_buffer_pool_read_requests','Innodb_buffer_pool_reads','Innodb_row_lock_waits','Created_tmp_disk_tables'); SHOW GLOBAL VARIABLES WHERE Variable_name IN ('max_connections','version','version_comment'); SELECT '__SEC__mysql_queries'; SELECT TIME, USER, IFNULL(DB,''), IFNULL(STATE,''), LEFT(REPLACE(REPLACE(IFNULL(INFO,''), CHAR(10), ' '), CHAR(9), ' '), 160) FROM information_schema.PROCESSLIST WHERE COMMAND NOT IN ('Sleep','Daemon','Binlog Dump') AND ID <> CONNECTION_ID() ORDER BY TIME DESC LIMIT 10" 2>&1 | head -n 60
        }
        if [ -n "$ZIR_DB_AUTH" ] && db_use "$ZIR_DB_AUTH"; then
            out=$(db_query)
            case "$out" in ERROR*) db_find; out=$(db_query) ;; esac
        else
            db_find; out=$(db_query)
        fi
        echo "__auth=$src"
        printf '%s\n' "$out"
    fi
else
    echo "__absent"
fi
sec key_services
if [ "$INIT" != "systemd" ]; then
    # No systemd (containers, some VPS): fall back to the process list.
    echo "procs=$(printf '%s\n' "$PS" | awk 'NF >= 8 { print $8 }' | sort -u | tr '\n' ' ')"
fi
units=$(printf '%s\n' "$SERVICES" | awk '{print $1}' \
    | grep -E "^($ZIR_UNITS)\.service$" | head -n 60)
if [ -n "$units" ]; then
    props=Id,ActiveState,SubState,UnitFileState,Type,ActiveEnterTimestampMonotonic,MemoryCurrent
    # Old systemd (CentOS 7, v219) has no NRestarts and stops after the first unit.
    shown=$(systemctl show $units -p "$props,NRestarts" 2>/dev/null) || shown=$(systemctl show $units -p "$props" 2>/dev/null)
    printf '%s\n' "$shown"
fi
echo "now=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"

# Each probe reports its own problems in its section; a failing last command
# (e.g. no database access) must not make the whole poll look like an SSH error.
exit 0
"""


# Services shown on the dashboard: (systemd unit regex, display name, group).
KEY_SERVICES = (
    (r"nginx", "nginx", "web"),
    (r"apache2|httpd", "Apache", "web"),
    (r"lsws|lshttpd|openlitespeed", "LiteSpeed", "web"),
    (r"caddy", "Caddy", "web"),
    (r"haproxy", "HAProxy", "web"),
    (r"varnish", "Varnish", "web"),
    (r"php[0-9.]*-fpm|php-fpm|php[0-9]+-php-fpm", "PHP-FPM", "php"),
    (r"mariadb", "MariaDB", "db"),
    (r"mysql|mysqld", "MySQL", "db"),
    (r"postgresql[0-9@.-]*", "PostgreSQL", "db"),
    (r"redis(-server)?", "Redis", "db"),
    (r"memcached", "Memcached", "db"),
    (r"mongod", "MongoDB", "db"),
    (r"elasticsearch", "Elasticsearch", "db"),
    (r"clickhouse-server", "ClickHouse", "db"),
    (r"pure-ftpd[a-z-]*", "Pure-FTPd", "ftp"),
    (r"proftpd", "ProFTPD", "ftp"),
    (r"vsftpd", "vsftpd", "ftp"),
    (r"exim4?", "Exim", "mail"),
    (r"postfix", "Postfix", "mail"),
    (r"dovecot", "Dovecot", "mail"),
    (r"opendkim", "OpenDKIM", "mail"),
    (r"spamassassin|spamd", "SpamAssassin", "mail"),
    (r"rspamd", "Rspamd", "mail"),
    (r"named|bind9", "BIND", "dns"),
    (r"pdns", "PowerDNS", "dns"),
    (r"unbound", "Unbound", "dns"),
    (r"hestia", "Hestia", "panel"),
    (r"vesta", "Vesta", "panel"),
    (r"ihttpd|ispmanager", "ISPmanager", "panel"),
    (r"directadmin", "DirectAdmin", "panel"),
    (r"webmin", "Webmin", "panel"),
    (r"sw-cp-server|psa", "Plesk", "panel"),
    (r"ssh|sshd", "SSH", "system"),
    (r"cron|crond", "cron", "system"),
    (r"fail2ban", "fail2ban", "system"),
    (r"ufw", "UFW", "system"),
    (r"firewalld", "firewalld", "system"),
    (r"docker", "Docker", "system"),
    (r"containerd", "containerd", "system"),
    (r"supervisor|supervisord", "Supervisor", "system"),
    (r"rsyslog", "rsyslog", "system"),
    (r"chrony|chronyd|ntp|ntpd|systemd-timesyncd", "time sync", "system"),
    (r"pm2-[a-z0-9_-]+", "PM2", "system"),
)
UNITS_REGEX = "|".join(pattern for pattern, _name, _group in KEY_SERVICES)
# Without systemd: process name -> (display name, group).
PROCESS_SERVICES = {
    "nginx": ("nginx", "web"), "apache2": ("Apache", "web"), "httpd": ("Apache", "web"),
    "litespeed": ("LiteSpeed", "web"), "lshttpd": ("LiteSpeed", "web"), "caddy": ("Caddy", "web"),
    "haproxy": ("HAProxy", "web"), "varnishd": ("Varnish", "web"),
    "mariadbd": ("MariaDB", "db"), "mysqld": ("MySQL", "db"), "postgres": ("PostgreSQL", "db"),
    "redis-server": ("Redis", "db"), "memcached": ("Memcached", "db"), "mongod": ("MongoDB", "db"),
    "pure-ftpd": ("Pure-FTPd", "ftp"), "proftpd": ("ProFTPD", "ftp"), "vsftpd": ("vsftpd", "ftp"),
    "exim4": ("Exim", "mail"), "exim": ("Exim", "mail"), "master": ("Postfix", "mail"),
    "dovecot": ("Dovecot", "mail"), "opendkim": ("OpenDKIM", "mail"), "named": ("BIND", "dns"),
    "pdns_server": ("PowerDNS", "dns"), "unbound": ("Unbound", "dns"),
    "sshd": ("SSH", "system"), "cron": ("cron", "system"), "crond": ("cron", "system"),
    "fail2ban-server": ("fail2ban", "system"), "dockerd": ("Docker", "system"),
    "supervisord": ("Supervisor", "system"), "rsyslogd": ("rsyslog", "system"),
}
GROUP_ORDER = ("web", "php", "db", "ftp", "mail", "dns", "panel", "system")


def script_header(
    files: list["LogFile"],
    extra_logs: tuple[str, ...] = (),
    php_known: tuple[str, ...] = (),
    quick: bool = False,
    db_auth: str = "",
) -> str:
    """Shell variables placed before the probe: where each log was read up to, whether
    the slow probes are skipped this time, which database login worked last time."""
    state = "\n".join(f"{f.path}|{f.inode}|{f.size}" for f in files)
    extra = " ".join(_shell_quote(path) for path in extra_logs)
    return (
        f"ZIR_LOGS={_shell_quote(state)}\nZIR_CAP={LOG_READ_CAP}\n"
        f"ZIR_EXTRA_LOGS={_shell_quote(extra)}\nZIR_UNITS={_shell_quote(UNITS_REGEX)}\n"
        f"ZIR_PHP_KNOWN={_shell_quote(' '.join(php_known))}\n"
        f"ZIR_QUICK={1 if quick else 0}\nZIR_DB_AUTH={_shell_quote(db_auth)}\n"
    )


def median(values) -> float:
    """statistics.median without the statistics module (and fractions / decimal it pulls in)."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcGroup:
    kind: str        # nginx | apache | litespeed | caddy | php-fpm | db
    group: str       # process name, or the PHP-FPM pool
    count: int
    cpu_seconds: float  # cumulative CPU time of the live processes
    rss: int
    running: int     # in state R/D right now (busy)


@dataclass(frozen=True)
class LogFile:
    index: int
    path: str
    inode: str
    size: int
    delta: int   # new bytes since the previous poll
    read: int    # bytes actually read (<= delta, capped)


@dataclass
class WebTraffic:
    files: list[LogFile] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)   # access logs this SSH user may not read
    lines: int = 0
    bruteforce: int = 0
    status: dict[str, int] = field(default_factory=dict)   # "2" -> count, "x" -> unparsed
    per_file: dict[int, int] = field(default_factory=dict)
    top_ips: list[tuple[int, str]] = field(default_factory=list)
    top_paths: list[tuple[int, str]] = field(default_factory=list)

    @property
    def scale(self) -> float:
        """Reading was capped: extrapolate counts to all new bytes."""
        read = sum(f.read for f in self.files)
        delta = sum(f.delta for f in self.files)
        return delta / read if read and delta > read else 1.0


@dataclass(frozen=True)
class PhpPool:
    name: str
    max_children: int
    pm: str


@dataclass(frozen=True)
class MysqlQuery:
    seconds: int
    user: str
    db: str
    state: str
    sql: str


@dataclass
class Mysql:
    state: str = "absent"   # absent | no_client | no_access | error | ok
    message: str = ""
    status: dict[str, int] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)
    queries: list[MysqlQuery] = field(default_factory=list)
    auth: str = ""  # where the credentials came from: default (socket / ~/.my.cnf), debian.cnf, Plesk …

    @property
    def product(self) -> str:
        comment = self.variables.get("version_comment", "")
        version = self.variables.get("version", "").split("-")[0]
        name = "MariaDB" if "mariadb" in (comment + self.variables.get("version", "")).lower() else "MySQL"
        return f"{name} {version}".strip()

    @property
    def max_connections(self) -> int:
        try:
            return int(self.variables.get("max_connections", "0"))
        except ValueError:
            return 0


@dataclass(frozen=True)
class ServiceStatus:
    unit: str          # nginx, php8.2-fpm …
    name: str          # display name: nginx, PHP-FPM …
    group: str         # web | php | db | ftp | mail | dns | panel | system
    active: str        # active | inactive | failed | activating | reloading | deactivating
    sub: str           # running | exited | dead | failed …
    enabled: str       # enabled | disabled | static | masked …
    kind: str          # simple | forking | notify | oneshot …
    since: float | None  # seconds the unit has been active
    memory: int | None
    restarts: int

    @property
    def state(self) -> str:
        """ok | warn | crit | off (installed but not in use)."""
        if self.active == "failed" or self.sub == "failed":
            return "crit"
        if self.active == "active":
            return "ok"
        if self.active in ("activating", "reloading", "deactivating"):
            return "warn"
        if self.kind == "oneshot":
            return "ok" if self.sub in ("exited", "dead") else "warn"
        # Stopped: a problem only if it is supposed to run (enabled at boot).
        return "crit" if self.enabled == "enabled" else "off"


@dataclass(frozen=True)
class PhpMaster:
    exe: str      # /usr/sbin/php-fpm5.6, /opt/remi/php56/root/usr/sbin/php-fpm …
    config: str   # /etc/php-fpm.conf

    @property
    def version(self) -> str:
        """5.6 from php-fpm5.6, php56, php5.6, ea-php56 …; "" when the path does not tell."""
        match = re.search(r"php-?fpm(\d)\.?(\d+)|php(\d)\.?(\d)(?=[/-]|$)", self.exe + "/")
        if not match:
            match = re.search(r"php(\d)\.?(\d)", self.config)
        if not match:
            return ""
        digits = [g for g in match.groups() if g]
        return f"{digits[0]}.{digits[1]}"


WEB_NAMES = {"nginx": "nginx", "apache": "Apache", "litespeed": "LiteSpeed", "caddy": "Caddy"}


@dataclass
class Stack:
    groups: list[ProcGroup] = field(default_factory=list)
    php_masters: list[PhpMaster] = field(default_factory=list)
    php_versions: dict[str, str] = field(default_factory=dict)  # exe -> "5.6.40"
    php_pools: list[PhpPool] = field(default_factory=list)
    php_warnings: list[str] = field(default_factory=list)
    web: WebTraffic = field(default_factory=WebTraffic)
    tcp_states: dict[str, int] = field(default_factory=dict)
    web_connections: int = 0
    peers: list[tuple[int, str]] = field(default_factory=list)  # established connections per IP
    conntrack: tuple[int, int] = (0, 0)
    synflood_messages: int = 0
    mysql: Mysql = field(default_factory=Mysql)
    services: list[ServiceStatus] = field(default_factory=list)

    def php_version_label(self) -> str:
        """'5.6.40', '8.2', '7.4, 8.2' … for all running PHP-FPM masters."""
        versions = []
        for master in self.php_masters:
            version = self.php_versions.get(master.exe) or master.version
            if version and version not in versions:
                versions.append(version)
        return ", ".join(versions)

    def kinds(self) -> set[str]:
        return {group.kind for group in self.groups}

    def of(self, kind: str) -> list[ProcGroup]:
        return [group for group in self.groups if group.kind == kind]

    def web_kinds(self) -> list[str]:
        """Running web servers: from the process list, else from systemd (when `ps` cannot
        see other users' processes, e.g. /proc mounted with hidepid)."""
        kinds = self.kinds()
        found = [kind for kind in WEB_NAMES if kind in kinds]
        if not found:
            active = {s.name for s in self.services if s.group == "web" and s.active == "active"}
            found = [kind for kind, name in WEB_NAMES.items() if name in active]
        return found

    def web_service(self, kind: str) -> ServiceStatus | None:
        return next((s for s in self.services if s.name == WEB_NAMES[kind] and s.active == "active"), None)

    @property
    def web_server(self) -> str:
        return " + ".join(WEB_NAMES[kind] for kind in self.web_kinds())

    @property
    def syn_recv(self) -> int:
        return self.tcp_states.get("SYN-RECV", 0)

    @property
    def established(self) -> int:
        return self.tcp_states.get("ESTAB", 0)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return default


def parse_stack(sections: dict[str, list[str]]) -> Stack:
    stack = Stack()

    for line in sections.get("stack_procs", []):
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] == "M":
            stack.php_masters.append(PhpMaster(parts[1], parts[2]))
        elif len(parts) == 6:
            stack.groups.append(
                ProcGroup(parts[0], parts[1], _int(parts[2]), _float(parts[3]), _int(parts[4]), _int(parts[5]))
            )
    stack.groups.sort(key=lambda g: (g.kind, g.group))

    for line in sections.get("php_pools", []):
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] and parts[0] not in {p.name for p in stack.php_pools}:
            stack.php_pools.append(PhpPool(parts[0], _int(parts[1]), parts[2]))
    stack.php_warnings = [line.strip() for line in sections.get("php_warnings", []) if line.strip()]
    for line in sections.get("php_versions", []):
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and parts[0] == "V":
            match = re.search(r"PHP (\d+\.\d+(?:\.\d+)?)", parts[2])
            stack.php_versions[parts[1]] = match.group(1) if match else ""

    for line in sections.get("weblogs", []):
        parts = line.rstrip("\n").split("|", 6)  # the path is last and may contain "|"
        if len(parts) == 7 and parts[0] == "F":
            stack.web.files.append(
                LogFile(_int(parts[1]), parts[6], parts[2], _int(parts[3]), _int(parts[4]), _int(parts[5]))
            )
        elif line.startswith("D|"):
            stack.web.unreadable.append(line.rstrip("\n")[2:])
    for line in sections.get("webtraffic", []):
        parts = line.rstrip("\n").split("|", 2)
        if parts[0] == "T" and len(parts) == 3:
            stack.web.lines, stack.web.bruteforce = _int(parts[1]), _int(parts[2])
        elif len(parts) == 3:
            count, key = _int(parts[1]), parts[2]
            if parts[0] == "S":
                stack.web.status[key] = count
            elif parts[0] == "P":
                stack.web.per_file[_int(key)] = count
            elif parts[0] == "I":
                stack.web.top_ips.append((count, key))
            elif parts[0] == "U":
                stack.web.top_paths.append((count, key))

    for line in sections.get("tcp", []):
        state, _sep, count = line.strip().partition("|")
        if state:
            stack.tcp_states[state] = _int(count)
    for line in sections.get("tcp_peers", []):
        parts = line.strip().split("|", 2)
        if parts[0] == "W" and len(parts) == 2:
            stack.web_connections = _int(parts[1])
        elif parts[0] == "C" and len(parts) == 3:
            stack.peers.append((_int(parts[1]), parts[2]))

    conntrack = [_int(line) for line in sections.get("conntrack", []) if line.strip()]
    if len(conntrack) == 2:
        stack.conntrack = (conntrack[0], conntrack[1])
    synflood = [line for line in sections.get("synflood", []) if line.strip()]
    stack.synflood_messages = _int(synflood[0]) if synflood else 0

    stack.mysql = parse_mysql(sections.get("mysql", []), sections.get("mysql_queries", []))
    stack.services = parse_key_services(sections.get("key_services", []))
    return stack


_COMPILED = [(re.compile(rf"^({pattern})$"), name, group) for pattern, name, group in KEY_SERVICES]


def parse_key_services(lines: list[str]) -> list[ServiceStatus]:
    """`systemctl show` blocks (blank-line separated) + `now=<uptime>`;
    `procs=` lists process names when there is no systemd."""
    now = None
    processes: set[str] = set()
    blocks: list[dict[str, str]] = [{}]
    for line in lines:
        line = line.strip()
        if not line:
            if blocks[-1]:
                blocks.append({})
            continue
        key, sep, value = line.partition("=")
        if key == "now":
            now = _float(value, None)
        elif key == "procs":
            processes = set(value.split())
        elif sep:
            blocks[-1][key] = value

    services = []
    for block in blocks:
        unit = block.get("Id", "").removesuffix(".service")
        match = next(((name, group) for regex, name, group in _COMPILED if regex.match(unit)), None)
        if not unit or match is None:
            continue
        since = None
        entered = _int(block.get("ActiveEnterTimestampMonotonic", "0"))
        if now is not None and entered > 0 and block.get("ActiveState") == "active":
            since = max(now - entered / 1e6, 0.0)
        memory = block.get("MemoryCurrent", "")
        services.append(ServiceStatus(
            unit=unit, name=match[0], group=match[1],
            active=block.get("ActiveState", ""), sub=block.get("SubState", ""),
            enabled=block.get("UnitFileState", ""), kind=block.get("Type", ""),
            since=since,
            memory=int(memory) if memory.isdigit() and int(memory) < 2**63 else None,
            restarts=_int(block.get("NRestarts", "0")),
        ))
    if not services and processes:
        php = sorted(p for p in processes if re.match(r"^php-fpm|^php[0-9.]*-fpm", p))
        found = {comm: PROCESS_SERVICES[comm] for comm in processes if comm in PROCESS_SERVICES}
        if php:
            found[php[0]] = ("PHP-FPM", "php")
        for comm, (name, group) in found.items():
            services.append(ServiceStatus(comm, name, group, "active", "running", "", "process", None, None, 0))
    rank = {"crit": 0, "warn": 1, "ok": 2, "off": 3}
    services.sort(key=lambda s: (rank[s.state], GROUP_ORDER.index(s.group), s.unit))
    return services


def parse_mysql(lines: list[str], query_lines: list[str]) -> Mysql:
    text = [line for line in lines if line.strip()]
    auth = ""
    if text and text[0].startswith("__auth="):
        auth = text.pop(0).strip().partition("=")[2]
    if not text or text[0].strip() == "__absent":
        return Mysql("absent")
    if text[0].strip() == "__noclient":
        return Mysql("no_client")
    joined = " ".join(text)
    if "ERROR" in joined and ("Access denied" in joined or "1045" in joined or "1698" in joined):
        return Mysql("no_access", joined.strip(), auth=auth)
    if text[0].startswith("ERROR"):
        return Mysql("error", joined.strip(), auth=auth)

    mysql = Mysql("ok", auth=auth)
    for line in text:
        name, _sep, value = line.partition("\t")
        if not _sep:
            continue
        if name in ("version", "version_comment", "max_connections"):
            mysql.variables[name] = value.strip()
        else:
            mysql.status[name] = _int(value)
    for line in query_lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 5:
            mysql.queries.append(MysqlQuery(_int(parts[0]), parts[1], parts[2], parts[3], parts[4]))
    return mysql


# --------------------------------------------------------------------------
# Rates between two polls
# --------------------------------------------------------------------------


@dataclass
class StackMetrics:
    rps: float | None = None
    rps_baseline: float | None = None     # median of recent polls
    status_rps: dict[str, float] = field(default_factory=dict)
    file_rps: dict[str, float] = field(default_factory=dict)   # log path -> req/s
    ip_rps: list[tuple[float, str]] = field(default_factory=list)
    path_rps: list[tuple[float, str]] = field(default_factory=list)
    bruteforce_rps: float = 0.0
    qps: float | None = None
    db_rates: dict[str, float] = field(default_factory=dict)  # Com_select ... per second
    slow_per_min: float = 0.0
    aborted_per_min: float = 0.0
    group_cpu: dict[str, float] = field(default_factory=dict)  # "kind|group" -> CPU %


class StackTracker:
    """Per-host memory needed for rates and the traffic baseline."""

    def __init__(self) -> None:
        self.rps_history: deque[float] = deque(maxlen=150)

    def compute(self, current: Stack, previous: Stack | None, dt: float) -> StackMetrics:
        metrics = StackMetrics()
        if previous is None or dt <= 0:
            return metrics

        # Web requests: only meaningful when we had offsets from the previous poll.
        known = {f.path for f in previous.web.files}
        if current.web.files and any(f.path in known for f in current.web.files):
            scale = current.web.scale
            metrics.rps = current.web.lines * scale / dt
            metrics.status_rps = {k: v * scale / dt for k, v in current.web.status.items()}
            paths = {f.index: f.path for f in current.web.files}
            metrics.file_rps = {paths.get(i, str(i)): n * scale / dt for i, n in current.web.per_file.items()}
            metrics.ip_rps = [(n * scale / dt, ip) for n, ip in current.web.top_ips]
            metrics.path_rps = [(n * scale / dt, p) for n, p in current.web.top_paths]
            metrics.bruteforce_rps = current.web.bruteforce * scale / dt
            if len(self.rps_history) >= 10:
                metrics.rps_baseline = median(self.rps_history)
            self.rps_history.append(metrics.rps)

        mysql, old = current.mysql, previous.mysql
        if mysql.state == "ok" and old.state == "ok":
            def rate(name: str) -> float:
                return max(mysql.status.get(name, 0) - old.status.get(name, 0), 0) / dt

            metrics.qps = rate("Questions")
            metrics.db_rates = {
                name: rate(name) for name in ("Com_select", "Com_insert", "Com_update", "Com_delete")
            }
            metrics.slow_per_min = rate("Slow_queries") * 60
            metrics.aborted_per_min = rate("Aborted_connects") * 60

        before = {f"{g.kind}|{g.group}": g.cpu_seconds for g in previous.groups}
        for group in current.groups:
            key = f"{group.kind}|{group.group}"
            if key in before:
                # Workers come and go, so the sum can shrink; never report negative CPU.
                metrics.group_cpu[key] = max(group.cpu_seconds - before[key], 0) / dt * 100
        return metrics


# --------------------------------------------------------------------------
# Health rules
# --------------------------------------------------------------------------


def _public(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_global


def evaluate_stack(stack: Stack, metrics: StackMetrics, cpus: int) -> list[tuple[str, str, str]]:
    """(level, topic, message); topic is ddos | web | php | db."""
    out: list[tuple[str, str, str]] = []

    # --- DDoS / abuse ---------------------------------------------------------
    rps, base = metrics.rps, metrics.rps_baseline
    if rps is not None and base is not None and base >= 1:
        if rps >= max(base * 10, base + 200):
            out.append(("crit", "ddos", _("traffic spike: {rps} req/s, {x}× above normal", rps=f"{rps:.0f}", x=f"{rps / base:.0f}")))
        elif rps >= max(base * 5, base + 50):
            out.append(("warn", "ddos", _("traffic spike: {rps} req/s, {x}× above normal", rps=f"{rps:.0f}", x=f"{rps / base:.0f}")))

    if rps and metrics.ip_rps:
        ip_rate, ip = metrics.ip_rps[0]
        share = ip_rate / rps * 100
        if _public(ip):
            if share >= 50 and ip_rate >= 30:
                out.append(("crit", "ddos", _("one IP sends {share}% of requests: {ip} ({rate} req/s)", share=f"{share:.0f}", ip=ip, rate=f"{ip_rate:.0f}")))
            elif share >= 30 and ip_rate >= 10:
                out.append(("warn", "ddos", _("one IP sends {share}% of requests: {ip} ({rate} req/s)", share=f"{share:.0f}", ip=ip, rate=f"{ip_rate:.0f}")))

    for count, ip in stack.peers[:3]:
        if not _public(ip):
            continue
        if count >= 300:
            out.append(("crit", "ddos", _("{n} connections from {ip}", n=count, ip=ip)))
        elif count >= 100:
            out.append(("warn", "ddos", _("{n} connections from {ip}", n=count, ip=ip)))

    if stack.syn_recv >= 500:
        out.append(("crit", "ddos", _("possible SYN flood: {n} half-open connections", n=stack.syn_recv)))
    elif stack.syn_recv >= 100:
        out.append(("warn", "ddos", _("possible SYN flood: {n} half-open connections", n=stack.syn_recv)))
    if stack.synflood_messages:
        out.append(("warn", "ddos", _("kernel reported SYN flooding ({n} times recently)", n=stack.synflood_messages)))

    used, limit = stack.conntrack
    if limit:
        pct = used / limit * 100
        if pct >= 95:
            out.append(("crit", "ddos", _("conntrack table {pct}% full", pct=f"{pct:.0f}")))
        elif pct >= 80:
            out.append(("warn", "ddos", _("conntrack table {pct}% full", pct=f"{pct:.0f}")))

    if metrics.bruteforce_rps >= 10:
        out.append(("crit", "ddos", _("password brute force: {rate} req/s to wp-login.php / xmlrpc.php", rate=f"{metrics.bruteforce_rps:.1f}")))
    elif metrics.bruteforce_rps >= 2:
        out.append(("warn", "ddos", _("password brute force: {rate} req/s to wp-login.php / xmlrpc.php", rate=f"{metrics.bruteforce_rps:.1f}")))

    # --- key services: enabled but not running ----------------------------------
    for service in stack.services:
        if service.state == "crit" and service.active != "failed":  # failed ones come from systemctl --failed
            out.append(("crit", "services", _("{name} ({unit}) is stopped", name=service.name, unit=service.unit)))
        elif service.state == "warn":
            out.append(("warn", "services", _("{name} ({unit}) is {state}", name=service.name, unit=service.unit, state=service.active)))

    # --- web errors -------------------------------------------------------------
    if rps and rps >= 1:
        errors = metrics.status_rps.get("5", 0.0) / rps * 100
        if errors >= 20:
            out.append(("crit", "web", _("{pct}% of responses are server errors (5xx)", pct=f"{errors:.0f}")))
        elif errors >= 5:
            out.append(("warn", "web", _("{pct}% of responses are server errors (5xx)", pct=f"{errors:.0f}")))

    # --- PHP-FPM ------------------------------------------------------------------
    limits = {pool.name: pool.max_children for pool in stack.php_pools if pool.max_children}
    for group in stack.of("php-fpm"):
        limit = limits.get(group.group)
        if not limit or group.group == "master":
            continue
        if group.running >= limit:
            out.append(("crit", "php", _("PHP pool {pool}: all {max} workers busy", pool=group.group, max=limit)))
        elif group.count >= limit:
            out.append(("warn", "php", _("PHP pool {pool} reached pm.max_children = {max}", pool=group.group, max=limit)))
    if stack.php_warnings:
        out.append(("warn", "php", _("PHP-FPM log: max_children reached recently")))

    # --- MySQL / MariaDB ----------------------------------------------------------
    mysql = stack.mysql
    if mysql.state == "ok":
        connected = mysql.status.get("Threads_connected", 0)
        limit = mysql.max_connections
        if limit:
            pct = connected / limit * 100
            if pct >= 95:
                out.append(("crit", "db", _("database connections {n} of {max}", n=connected, max=limit)))
            elif pct >= 80:
                out.append(("warn", "db", _("database connections {n} of {max}", n=connected, max=limit)))
        running = mysql.status.get("Threads_running", 0)
        if cpus and running >= 4 * cpus:
            out.append(("crit", "db", _("{n} queries running at once", n=running)))
        elif cpus and running >= 2 * cpus:
            out.append(("warn", "db", _("{n} queries running at once", n=running)))
        if metrics.slow_per_min >= 5:
            out.append(("warn", "db", _("{n} slow queries per minute", n=f"{metrics.slow_per_min:.0f}")))
        if metrics.aborted_per_min >= 10:
            out.append(("warn", "ddos", _("{n} failed database logins per minute", n=f"{metrics.aborted_per_min:.0f}")))
    return out
