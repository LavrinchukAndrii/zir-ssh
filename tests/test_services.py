"""Web / PHP / database / attack probes, parsed from real Debian 12 output (tests/fixtures)."""

from pathlib import Path

import os
import shlex
import subprocess
import sys

import pytest

from zir.probes import compute_metrics, evaluate, parse_snapshot
from zir.services import (
    STACK_SCRIPT, LogFile, StackTracker, WebTraffic, evaluate_stack, parse_mysql, parse_stack, Stack, StackMetrics,
    script_header,
)
from zir.widgets.stack_sections import _site

FIXTURES = Path(__file__).parent / "fixtures"


def _snapshots():
    first = parse_snapshot((FIXTURES / "probe1_debian12.txt").read_text())
    second = parse_snapshot((FIXTURES / "probe2_debian12.txt").read_text())
    second.taken_at = first.taken_at + 2.0
    return first, second


def test_parses_services_from_real_server():
    _first, snap = _snapshots()
    stack = snap.stack
    assert {"nginx", "php-fpm", "db"} <= stack.kinds()
    assert stack.web_server == "nginx"
    pools = {p.name: p for p in stack.php_pools}
    assert pools["www"].max_children == 4 and pools["www"].pm == "dynamic"
    assert stack.mysql.state == "ok"
    assert stack.mysql.product.startswith("MariaDB 10.11")
    assert stack.mysql.max_connections == 151
    assert stack.mysql.status["Questions"] > 0
    assert stack.conntrack[1] > 0


def test_access_log_is_read_incrementally():
    first, second = _snapshots()
    assert first.stack.web.files[0].delta == 0  # first look starts at the end of the log
    log = second.stack.web.files[0]
    assert log.path == "/var/log/nginx/access.log" and log.delta > 0
    assert second.stack.web.lines == 304
    assert second.stack.web.status == {"2": 152, "4": 152}
    assert second.stack.web.bruteforce == 152  # POST /xmlrpc.php


def test_rates_and_findings():
    first, second = _snapshots()
    metrics = compute_metrics(second, first, StackTracker())
    rates = metrics.stack
    assert round(rates.rps) == 152  # 304 lines in 2 s
    assert round(rates.bruteforce_rps) == 76
    assert rates.qps is not None and rates.qps > 0
    messages = [f for f in evaluate(second, metrics) if f.topic == "ddos"]
    assert any("xmlrpc" in f.message for f in messages)
    assert all(f.level == "crit" for f in messages if "xmlrpc" in f.message)


def test_next_probe_continues_from_saved_offsets():
    _first, second = _snapshots()
    header = script_header(second.stack.web.files)
    log = second.stack.web.files[0]
    assert f"/var/log/nginx/access.log|{log.inode}|{log.size}" in header


def _find_logs(conf: Path, extra: str = "") -> WebTraffic:
    """Run only the `sec weblogs` part of the probe against a fake web server config.

    The probe also looks in the usual places (/var/log/nginx, /var/log/apache2…), which exist on
    machines with a real web server (GitHub runners have both), so only the test's own files count.
    """
    part = STACK_SCRIPT[STACK_SCRIPT.index("sec weblogs"):STACK_SCRIPT.index("sec webtraffic")]
    header = f"sec() {{ :; }}\nZIR_WEBCONF={shlex.quote(str(conf))}\nZIR_EXTRA_LOGS={shlex.quote(extra)}\n"
    out = subprocess.run(["sh", "-c", header + part], capture_output=True, text=True).stdout
    web = parse_stack({"weblogs": out.splitlines()}).web
    root = f"{conf.parent}/"
    web.files = [f for f in web.files if f.path.startswith(root)]
    web.unreadable = [path for path in web.unreadable if path.startswith(root)]
    return web


def test_access_logs_with_custom_names_are_found_through_the_config(tmp_path):
    logs = tmp_path / "logs"
    (logs / "wms01").mkdir(parents=True)
    for name in ("access-wms01.log", "access-wms-dev.log", "error-wms01.log"):
        (logs / name).write_text("x\n")
    (logs / "wms01" / "requests.log").write_text("x\n")
    conf = tmp_path / "nginx"
    (conf / "sites-enabled").mkdir(parents=True)
    (conf / "sites-enabled" / "wms").write_text(
        f"server {{\n    access_log {logs}/access-wms01.log combined;\n    access_log  \"{logs}/access-wms-dev.log\";\n"
        f"    # access_log {logs}/commented.log;\n    access_log {logs}/$host/requests.log;\n"
        f"    error_log {logs}/error-wms01.log;\n    access_log off;\n}}\n"
    )
    paths = sorted(f.path for f in _find_logs(conf).files)
    assert paths == sorted([f"{logs}/access-wms01.log", f"{logs}/access-wms-dev.log", f"{logs}/wms01/requests.log"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_unreadable_access_logs_are_reported(tmp_path):
    log = tmp_path / "access-wms01.log"
    log.write_text("x\n")
    log.chmod(0)
    web = _find_logs(tmp_path / "none", extra=str(log))
    assert web.files == [] and web.unreadable == [str(log)]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_logs_in_a_closed_folder_are_reported_from_the_config(tmp_path):
    closed = tmp_path / "nginx-logs"
    closed.mkdir()
    (closed / "access-parser.log").write_text("x\n")
    conf = tmp_path / "nginx"
    conf.mkdir()
    (conf / "nginx.conf").write_text(f"access_log {closed}/access-parser.log;\naccess_log {closed}/gone/x.log;\n")
    closed.chmod(0)
    try:
        web = _find_logs(conf, extra=f"{closed}/*access*.log")
    finally:
        closed.chmod(0o755)
    assert web.files == [] and web.unreadable == [f"{closed}/access-parser.log"]


def test_status_is_found_in_a_custom_log_format(tmp_path):
    """log_format main '... "$request" -$request_time- $status $body_bytes_sent ...'"""
    log = tmp_path / "example.com.access.log"
    log.write_text(
        '200.180.121.140 - - [24/Oct/2026:23:20:24 +0300] "GET /a HTTP/1.1" -0.116- 404 39865 "-" "Mozilla/5.0" "-"\n'
        '144.76.68.124 - - [24/Oct/2026:23:20:26 +0300] "GET /b?x=1 HTTP/2.0" -0.171- 200 42696 "-" "bot" "-"\n'
        '1.2.3.4 - - [24/Oct/2026:23:20:27 +0300] "GET /c HTTP/1.1" 301 0 "-" "curl"\n'
    )
    part = STACK_SCRIPT[STACK_SCRIPT.index("sec weblogs"):STACK_SCRIPT.index("sec tcp")]
    header = (f"sec() {{ printf '\\n__SEC__%s\\n' \"$1\"; }}\n"
              "stat -c %s / >/dev/null 2>&1 || stat() { command stat -L -f '%i %z' \"$4\"; }  # BSD stat (macOS)\n"
              f"ZIR_WEBCONF={shlex.quote(str(tmp_path / 'none'))}\n"
              f"ZIR_EXTRA_LOGS={shlex.quote(str(log))}\nZIR_LOGS={shlex.quote(f'{log}|0|0')}\n")
    out = subprocess.run(["sh", "-c", header + part], capture_output=True, text=True).stdout
    sections: dict[str, list[str]] = {}
    for line in out.splitlines():
        if line.startswith("__SEC__"):
            sections[line[7:]] = []
        elif sections:
            sections[list(sections)[-1]].append(line)
    web = parse_stack(sections).web
    assert web.lines == 3 and web.status == {"2": 1, "3": 1, "4": 1}
    assert sorted(path for _n, path in web.top_paths) == ["/a", "/b", "/c"]


def test_site_names_from_log_paths():
    assert _site("/var/log/nginx/example.com.access.log") == "example.com"
    assert _site("/var/log/nginx/access-wms01.log") == "wms01"
    assert _site("/var/log/nginx/access-wms-dev.log") == "wms-dev"
    assert _site("/home/domains/shop.ua/logs/access.log") == "shop.ua"
    assert _site("/var/log/nginx/access.log") == "access"


def test_capped_reading_is_extrapolated():
    web = WebTraffic(files=[LogFile(1, "/a.log", "1", 10, delta=6_000_000 * 4, read=6_000_000)], lines=1000)
    assert web.scale == 4


def test_ddos_rules():
    stack = Stack()
    stack.tcp_states = {"SYN-RECV": 600, "ESTAB": 900}
    stack.peers = [(350, "45.141.84.66"), (500, "10.0.0.5")]  # private addresses are never blamed
    stack.conntrack = (96_000, 100_000)
    rates = StackMetrics(rps=2000, rps_baseline=100, ip_rps=[(1200, "45.141.84.66")], status_rps={"5": 500})
    found = evaluate_stack(stack, rates, cpus=4)
    text = " | ".join(message for _level, _topic, message in found)
    for expected in ("traffic spike", "one IP sends 60%", "350 connections from 45.141.84.66",
                     "SYN flood", "conntrack table 96%", "25% of responses are server errors"):
        assert expected in text, expected
    assert "10.0.0.5" not in text
    assert all(level == "crit" for level, topic, _m in found if topic == "ddos")


def test_mysql_access_denied_is_not_an_error():
    mysql = parse_mysql(["ERROR 1698 (28000): Access denied for user 'deploy'@'localhost'"], [])
    assert mysql.state == "no_access"
    assert parse_mysql(["__absent"], []).state == "absent"


def test_failing_last_probe_does_not_break_the_poll(monkeypatch):
    """Regression: a non-root user without DB access made the script exit 1 and the whole poll fail."""
    import asyncio

    import pytest

    from zir.collector import Collector, ProbeError
    from zir.config import HostSpec

    collector = Collector(HostSpec(name="h", target="h"))
    monkeypatch.setattr(collector, "_command", lambda: ["sh", "-c", "printf '__SEC__meta\\nhostname=h\\n'; exit 1"])
    assert "hostname=h" in asyncio.run(collector._run(""))

    monkeypatch.setattr(collector, "_command", lambda: ["sh", "-c", "echo 'Permission denied (publickey)' >&2; exit 255"])
    with pytest.raises(ProbeError, match="Permission denied"):
        asyncio.run(collector._run(""))


def test_probe_script_always_exits_zero():
    from zir.services import STACK_SCRIPT

    assert STACK_SCRIPT.rstrip().endswith("exit 0")


SYSTEMCTL_SHOW = """Id=nginx.service
Type=forking
ActiveState=active
SubState=running
UnitFileState=enabled
ActiveEnterTimestampMonotonic=1000000000
MemoryCurrent=52428800
NRestarts=0

Id=php8.2-fpm.service
Type=notify
ActiveState=failed
SubState=failed
UnitFileState=enabled
ActiveEnterTimestampMonotonic=0
MemoryCurrent=[not set]
NRestarts=3

Id=pure-ftpd.service
Type=forking
ActiveState=inactive
SubState=dead
UnitFileState=enabled
ActiveEnterTimestampMonotonic=0
MemoryCurrent=[not set]
NRestarts=0

Id=apache2.service
Type=forking
ActiveState=inactive
SubState=dead
UnitFileState=disabled
ActiveEnterTimestampMonotonic=0
MemoryCurrent=[not set]
NRestarts=0

Id=certbot-renew-or-not.service
Type=oneshot
ActiveState=inactive
SubState=dead
UnitFileState=enabled

Id=mariadb.service
Type=notify
ActiveState=active
SubState=running
UnitFileState=enabled
ActiveEnterTimestampMonotonic=5000000000
MemoryCurrent=209715200
NRestarts=1
now=8600.5"""


def test_key_services_from_systemctl_show():
    from zir.services import parse_key_services

    services = {s.unit: s for s in parse_key_services(SYSTEMCTL_SHOW.splitlines())}
    assert "certbot-renew-or-not" not in services  # not a key service
    assert services["php8.2-fpm"].state == "crit" and services["php8.2-fpm"].name == "PHP-FPM"
    assert services["pure-ftpd"].state == "crit" and services["pure-ftpd"].group == "ftp"  # enabled but stopped
    assert services["apache2"].state == "off"  # installed, not used
    assert services["nginx"].state == "ok" and round(services["nginx"].since) == 7600
    assert services["nginx"].memory == 50 * 2**20 and services["php8.2-fpm"].memory is None
    assert services["mariadb"].restarts == 1
    order = [s.unit for s in parse_key_services(SYSTEMCTL_SHOW.splitlines())]
    assert order[:2] == ["php8.2-fpm", "pure-ftpd"]  # problems first


def test_stopped_service_is_a_finding():
    from zir.services import parse_key_services

    stack = Stack(services=parse_key_services(SYSTEMCTL_SHOW.splitlines()))
    found = [(level, message) for level, topic, message in evaluate_stack(stack, StackMetrics(), 2) if topic == "services"]
    assert ("crit", "Pure-FTPd (pure-ftpd) is stopped") in found
    assert not any("apache2" in message for _level, message in found)  # disabled on purpose


def test_services_without_systemd_from_processes():
    from zir.services import parse_key_services

    services = parse_key_services(["procs=bash mariadbd nginx php-fpm8.2 pure-ftpd sshd tr", "now=10"])
    assert {s.name for s in services} == {"MariaDB", "nginx", "PHP-FPM", "Pure-FTPd", "SSH"}
    assert all(s.state == "ok" for s in services)


def test_php56_centos_style_with_idle_ondemand_pool():
    """Regression: PHP-FPM 5.6 with pm = ondemand and no traffic was shown as "not running"."""
    from zir.services import parse_stack

    sections = {
        "stack_procs": ["php-fpm|master|1|0.31|9437184|0", "M|/usr/sbin/php-fpm|/etc/php-fpm.conf"],
        "php_pools": ["www|50|ondemand", "site2||ondemand"],
        "php_versions": ["V|/usr/sbin/php-fpm|PHP 5.6.40 (fpm-fcgi) (built: Feb 15 2023 11:31:20)"],
    }
    stack = parse_stack(sections)
    assert stack.of("php-fpm") and stack.php_version_label() == "5.6.40"
    assert {p.name: (p.max_children, p.pm) for p in stack.php_pools} == {"www": (50, "ondemand"), "site2": (0, "ondemand")}


def test_php_section_shows_idle_pools(monkeypatch):
    import asyncio

    from zir.services import PhpMaster, PhpPool, ProcGroup
    from test_app import make_app

    async def scenario():
        app = make_app(2)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("alt+3")
            await pilot.pause(1.2)
            view = app._active_view()
            reading = app.store["web-1"].reading
            stack = reading.snapshot.stack
            stack.groups = [ProcGroup("php-fpm", "master", 1, 0.3, 9 * 2**20, 0)]
            stack.php_masters = [PhpMaster("/usr/sbin/php-fpm", "/etc/php-fpm.conf")]
            stack.php_versions = {"/usr/sbin/php-fpm": "5.6.40"}
            stack.php_pools = [PhpPool("www", 50, "ondemand")]
            app.store["web-1"].paused = True  # keep this reading on screen
            view.show_section("php")
            await pilot.pause(0.3)
            view.render_php(reading)
            await pilot.pause(0.2)
            head = str(view.query_one("#php-head").render())
            assert "PHP-FPM 5.6.40" in head and "not running" not in head
            table = view.query_one("#php-pools")
            assert table.row_count == 1 and str(table.get_row_at(0)[1]) == "ondemand"
            assert "ondemand" in str(view.query_one("#php-note").render())

    asyncio.run(scenario())


def test_database_access_setup_answers():
    import asyncio

    import pytest

    from zir.dbaccess import DbAccessError, setup_access, setup_script

    class Fake:
        def __init__(self, answer):
            self.answer, self.script = answer, ""

        async def run_script(self, script):
            self.script = script
            return self.answer

    with pytest.raises(DbAccessError, match="Wrong user or password"):
        asyncio.run(setup_access(Fake("__bad\n"), "root", "x"))
    fake = Fake("__ok /root/.my.cnf.zir-backup-1\n")
    message = asyncio.run(setup_access(fake, "root", "it's \"tricky\""))
    assert "saved" in message and "zir-backup-1" in message
    # The password is shell-quoted inside the script, never passed on a command line.
    assert "DB_PASS='it'\\''s \"tricky\"'" in fake.script
    assert "chmod 600" in setup_script("u", "p")


def test_mysql_auth_source_is_reported():
    from zir.services import parse_mysql

    mysql = parse_mysql(["__auth=debian.cnf", "Questions\t10", "max_connections\t151"], [])
    assert mysql.state == "ok" and mysql.auth == "debian.cnf" and mysql.max_connections == 151
    denied = parse_mysql(["__auth=default", "ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: NO)"], [])
    assert denied.state == "no_access"


def test_web_server_from_systemd_when_processes_are_hidden():
    from zir.services import parse_stack

    # ps shows nothing (e.g. /proc with hidepid for the SSH user), systemd knows nginx runs.
    stack = parse_stack({"stack_procs": [], "key_services": [
        "Id=nginx.service", "ActiveState=active", "SubState=running", "UnitFileState=enabled",
        "Type=forking", "MemoryCurrent=12345678", "", "now=100"]})
    assert stack.web_kinds() == ["nginx"] and stack.web_server == "nginx"
    assert stack.web_service("nginx").memory == 12345678
    stopped = parse_stack({"key_services": ["Id=nginx.service", "ActiveState=inactive", "now=1"]})
    assert stopped.web_server == ""


def test_quick_poll_keeps_what_the_full_poll_found():
    from zir.probes import Snapshot, carry_slow

    first, _second = _snapshots()
    first.stack.web.unreadable = ["/var/log/nginx/closed.log"]
    quick = Snapshot()
    carry_slow(quick, first)
    assert (quick.hostname, quick.os, quick.cpus, quick.init) == (first.hostname, first.os, first.cpus, first.init)
    assert quick.listening == first.listening and quick.logs == first.logs
    assert quick.stack.php_pools == first.stack.php_pools
    assert quick.stack.web.unreadable == ["/var/log/nginx/closed.log"]


def test_probe_header_skips_slow_probes_and_reuses_the_database_login():
    from zir.probes import build_probe
    from zir.services import Mysql

    _first, second = _snapshots()
    second.stack.mysql = Mysql("ok", auth="debian.cnf")
    assert "ZIR_QUICK=1\nZIR_DB_AUTH='debian.cnf'" in build_probe(second, quick=True)
    assert "ZIR_QUICK=0" in build_probe(second)
    second.stack.mysql = Mysql("no_access", auth="default")  # a failing login is not repeated first
    assert "ZIR_DB_AUTH=''" in build_probe(second)
    assert "ZIR_QUICK=0" in build_probe(None)


def test_quick_poll_reads_only_the_known_logs(tmp_path):
    known = tmp_path / "access.log"
    known.write_text("1.2.3.4 - - [x] \"GET / HTTP/1.1\" 200 1\n")
    (tmp_path / "other-access.log").write_text("x\n")
    part = STACK_SCRIPT[STACK_SCRIPT.index("sec weblogs"):STACK_SCRIPT.index("sec webtraffic")]
    header = (f"sec() {{ :; }}\n"
              "stat -c %s / >/dev/null 2>&1 || stat() { command stat -L -f '%i %z' \"$4\"; }  # BSD stat (macOS)\n"
              f"ZIR_QUICK=1\nZIR_LOGS={shlex.quote(f'{known}|0|0')}\n"
              f"ZIR_EXTRA_LOGS={shlex.quote(str(tmp_path / 'other-access.log'))}\n")
    out = subprocess.run(["sh", "-c", header + part], capture_output=True, text=True).stdout
    files = parse_stack({"weblogs": out.splitlines()}).web.files
    assert [f.path for f in files] == [str(known)]
    assert files[0].delta == known.stat().st_size  # continues from the saved offset (0)
