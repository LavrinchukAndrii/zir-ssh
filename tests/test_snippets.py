from pathlib import Path

from zir import snippets

FIXTURES = Path(__file__).parent / "fixtures"


def _facts():
    return snippets.parse_facts((FIXTURES / "detect_debian12.txt").read_text())


def test_detection_of_real_server():
    facts = _facts()
    assert facts["pkg"] == "apt" and facts["root"] == "1"
    assert {"nginx", "php8.2-fpm", "mariadb"} <= set(facts["unit"])
    assert snippets.found_summary(facts) == "nginx, PHP 8.2, MariaDB"


def test_snippets_match_what_is_installed():
    groups = {g.title: [s.command for s in g.snippets] for g in snippets.build(_facts())}
    assert "nginx" in groups and "PHP" in groups and "MySQL / MariaDB" in groups
    assert "Docker" not in groups and "Apache" not in groups  # not installed there
    assert "nginx -t" in groups["nginx"]
    assert "php-fpm8.2 -t" in groups["PHP"]
    assert "systemctl restart mariadb" in groups["MySQL / MariaDB"]
    # root: no sudo prefix
    assert not any(c.startswith("sudo ") for cs in groups.values() for c in cs)


def test_sudo_for_regular_user_and_firewall_variants():
    facts = {**_facts(), "root": "0", "cmd": _facts()["cmd"] + ["ufw", "fail2ban-client"]}
    commands = [s.command for g in snippets.build(facts) for s in g.snippets]
    assert "sudo nginx -t" in commands
    assert "sudo ufw insert 1 deny from <IP>" in commands
    assert "sudo fail2ban-client status" in commands


def test_storage_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert snippets.load("root@vps:2345") is None
    saved = snippets.save("root@vps:2345", _facts())
    assert snippets.load("root@vps:2345")["facts"] == saved["facts"]
