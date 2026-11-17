from pathlib import Path

from zir import snippets

FIXTURES = Path(__file__).parent / "fixtures"


def _facts():
    return snippets.parse_facts((FIXTURES / "detect_debian12.txt").read_text(encoding="utf-8"))


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


# -- user snippets ---------------------------------------------------------------


def test_user_snippets_round_trip_in_utf8():
    from zir.snippets import GLOBAL, UserSnippets

    store = UserSnippets()
    first = store.add(GLOBAL, "Черга", "php artisan queue:restart")
    store.add("web-1", "Лог", "tail -f /var/log/nginx/error.log")
    store.save()
    text = store.path.read_text(encoding="utf-8")
    assert "Черга" in text  # stored as text, not \u escapes

    again = UserSnippets()
    own, host = again.lists("web-1")
    assert [(s.id, s.title) for s in own] == [(first.id, "Черга")]
    assert [s.command for s in host] == ["tail -f /var/log/nginx/error.log"]
    assert again.lists("other")[1] == [] and again.lists(None)[1] == []


def test_user_snippets_edit_move_delete():
    from zir.snippets import GLOBAL, UserSnippets

    store = UserSnippets()
    a, b, c = (store.add(GLOBAL, name, f"echo {name}") for name in "abc")
    assert store.move(c.id, -1) and [s.title for s in store.of(GLOBAL)] == ["a", "c", "b"]
    assert not store.move(a.id, -1)  # already first
    assert not store.move(b.id, 1)  # already last
    store.update(a.id, "A", "echo A", "web-1")  # other scope: moved to that list
    assert [s.title for s in store.of(GLOBAL)] == ["c", "b"]
    assert [(s.title, s.command) for s in store.of("web-1")] == [("A", "echo A")]
    assert store.delete(b.id) and not store.delete(b.id)
    store.save()
    assert [s.title for s in UserSnippets().of(GLOBAL)] == ["c"]


def test_broken_user_snippets_file_gives_empty_lists():
    from zir.snippets import GLOBAL, UserSnippets

    store = UserSnippets()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    for broken in ("{not json", "[]", '{"global": [{"title": 1}, "x", {"title": "t", "command": ""}]}'):
        store.path.write_text(broken, encoding="utf-8")
        assert UserSnippets().of(GLOBAL) == []


def test_terminal_key_encoding_for_mc():
    from textual import events

    from zir.widgets.terminal import encode_key

    cases = {
        "f3": "\x1bOR", "f4": "\x1bOS", "f6": "\x1b[17~", "f15": "\x1b[1;2R", "f17": "\x1b[15;2~",
        "ctrl+pageup": "\x1b[5;5~", "ctrl+backslash": "\x1c", "shift+up": "\x1b[1;2A",
        "ctrl+home": "\x1b[1;5H", "ctrl+left": "\x1b[1;5D", "delete": "\x1b[3~", "ctrl+a": "\x01",
    }
    for key, expected in cases.items():
        assert encode_key(events.Key(key, None), False) == expected, key
    assert encode_key(events.Key("up", None), True) == "\x1bOA"
    assert encode_key(events.Key("alt+t", "t"), False) == "\x1bt"
    assert encode_key(events.Key("ctrl+right_square_bracket", None), False) is None  # zir's menu


def test_snippet_manager_create_edit_reorder_delete():
    import asyncio

    from textual.app import App
    from textual.widgets import Input

    from zir.snippets import GLOBAL, UserSnippets
    from zir.widgets.snippet_manager import SnippetManager

    async def scenario():
        app = App()
        async with app.run_test(size=(140, 40)) as pilot:
            manager = SnippetManager("web-1")
            await app.push_screen(manager)
            await pilot.pause()

            async def create(title, command, only_here=False):
                manager.query_one("#snipm-new").press()
                await pilot.pause()
                manager.query_one("#snipm-title", Input).value = title
                manager.query_one("#snipm-command", Input).value = command
                if only_here:
                    manager.query_one("#snipm-host").value = True
                manager.query_one("#snipm-save").press()
                await pilot.pause()

            await create("Один", "echo 1")
            await create("Два", "echo 2")
            await create("Тут", "echo here", only_here=True)
            saved = UserSnippets()
            assert [s.title for s in saved.of(GLOBAL)] == ["Один", "Два"]
            assert [s.title for s in saved.of("web-1")] == ["Тут"]

            two = saved.of(GLOBAL)[1]
            manager._edit(manager.store.find(two.id)[1])
            manager.query_one("#snipm-up").press()  # reorder
            await pilot.pause()
            manager.query_one("#snipm-command", Input).value = "echo two"
            manager.query_one("#snipm-save").press()  # edit
            await pilot.pause()
            assert [(s.title, s.command) for s in UserSnippets().of(GLOBAL)] == [("Два", "echo two"), ("Один", "echo 1")]

            manager.query_one("#snipm-delete").press()  # asks first
            await pilot.pause()
            assert len(UserSnippets().of(GLOBAL)) == 2
            manager.query_one("#snipm-delete").press()
            await pilot.pause()
            assert [s.title for s in UserSnippets().of(GLOBAL)] == ["Один"]

    asyncio.run(scenario())
