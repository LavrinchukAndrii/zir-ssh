import asyncio
import tomllib

from zir.config import load_config
from zir.sshconfig import SshAlias, ssh_aliases
from zir.welcome import WelcomeApp, save_hosts


def test_ssh_aliases_skip_wildcards_and_follow_include(tmp_path):
    extra = tmp_path / "extra.conf"
    extra.write_text("Host db-1\n  HostName 10.0.0.9\n")
    config = tmp_path / "config"
    config.write_text(
        f"Include {extra}\n"
        "Host *\n  ServerAliveInterval 30\n"
        "Host web-1 web-2\n  HostName=10.0.0.5\n  User deploy\n"
        "Host !bad ?x prod-*\n"
        "Match host foo\n  User nobody\n"
        "Host bitbucket.org\n  User git\n"
        "Host gh\n  HostName ssh.github.com\n"
    )
    aliases = ssh_aliases(config)
    assert [a.name for a in aliases] == ["db-1", "web-1", "web-2"]
    assert aliases[1].detail == "deploy@10.0.0.5"


def test_save_hosts_round_trip(tmp_path):
    from zir.config import hosts_from_args

    path = tmp_path / "zir" / "hosts.toml"
    save_hosts(path, hosts_from_args(["web-1", "root@10.0.0.7:2222"]))
    tomllib.loads(path.read_text())
    hosts, _ = load_config(path)
    assert [(h.name, h.target, h.port) for h in hosts] == [
        ("web-1", "web-1", None),
        ("10.0.0.7", "root@10.0.0.7", 2222),
    ]


def _run(app, keys, size=(100, 30)):
    async def scenario():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            for key in keys:
                await pilot.press(key)
            await pilot.pause()
    asyncio.run(scenario())
    return app.return_value


def test_pick_alias_and_type_address(tmp_path):
    aliases = [SshAlias("web-1", "10.0.0.5", "deploy"), SshAlias("db-1")]
    app = WelcomeApp(aliases, tmp_path / "hosts.toml")
    result = _run(app, ["space", "tab", *"root@10.0.0.7", "enter"])
    assert [s.name for s in result.specs] == ["web-1", "10.0.0.7"]
    assert result.save and not result.demo


def test_demo_and_quit(tmp_path):
    assert _run(WelcomeApp([], tmp_path / "hosts.toml"), ["ctrl+d"]).demo
    assert _run(WelcomeApp([], tmp_path / "hosts.toml"), ["escape"]) is None


def test_empty_connect_shows_error(tmp_path):
    app = WelcomeApp([], tmp_path / "hosts.toml")

    async def scenario():
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.press("enter")
            await pilot.pause()
            assert app.is_running
            assert "Select at least one host" in str(app.query_one("#error").render())
    asyncio.run(scenario())


def test_language_switch_from_welcome(tmp_path):
    from zir.prefs import load_prefs

    app = WelcomeApp([], tmp_path / "hosts.toml")

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f2")
            await pilot.pause(0.3)
            await pilot.click("#lang-uk")
            await pilot.click("#settings-save")
            await pilot.pause(0.3)

    asyncio.run(scenario())
    assert app.return_value.restart
    assert load_prefs()["language"] == "uk"  # tests start in English, so the other one is Ukrainian
