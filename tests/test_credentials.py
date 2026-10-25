"""Identity cards: encrypted with this computer's SSH key."""

import stat
import sys

import pytest

from zir import credentials
from zir.credentials import Credential, Vault, VaultError, load, save
from zir.widgets.terminal import PASSWORD_PROMPT


@pytest.fixture
def ssh(tmp_path, monkeypatch):
    folder = tmp_path / "ssh"
    folder.mkdir()
    (folder / "id_ed25519").write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nfake-key-one\n")
    monkeypatch.setattr(credentials, "ssh_dir", lambda: folder)
    return folder


def test_round_trip_and_nothing_in_plain_text(ssh, tmp_path):
    path = tmp_path / "credentials.enc"
    save([Credential("root on debian", "root", "s3cret-пароль")], path)
    blob = path.read_bytes()
    assert b"s3cret" not in blob and b"root" not in blob
    if sys.platform != "win32":  # Windows has no POSIX modes; the file lives in the user's own profile
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    [card] = load(path)
    assert (card.name, card.login, card.password) == ("root on debian", "root", "s3cret-пароль")


def test_another_key_cannot_decrypt(ssh, tmp_path):
    path = tmp_path / "credentials.enc"
    save([Credential("a", "b", "c")], path)
    (ssh / "id_ed25519").write_bytes(b"another computer's key")
    with pytest.raises(VaultError, match="another SSH key"):
        load(path)


def test_missing_key_and_damaged_file(ssh, tmp_path):
    path = tmp_path / "credentials.enc"
    assert load(path) == []  # nothing saved yet
    save([Credential("a", "b", "c")], path)
    path.write_bytes(path.read_bytes()[:-3] + b"xyz")
    with pytest.raises(VaultError, match="damaged"):
        load(path)
    (ssh / "id_ed25519").unlink()
    with pytest.raises(VaultError, match="No private SSH key"):
        load(path)


def test_vault_never_overwrites_cards_it_could_not_read(ssh, tmp_path):
    path = tmp_path / "credentials.enc"
    save([Credential("keep me", "", "x")], path)
    (ssh / "id_ed25519").write_bytes(b"another key")
    vault = Vault(path)
    vault.ensure_loaded()
    assert vault.error and not vault.loaded
    vault.cards.append(Credential("new", "", "y"))
    with pytest.raises(VaultError):
        vault.save()


@pytest.mark.parametrize("text", [
    "Пароль: ", "Password:", "[sudo] password for lvl: ", "Enter passphrase for key '/x/id_rsa': ",
    "root@10.0.0.1's password: ",
])
def test_password_prompts_are_recognised(text):
    assert PASSWORD_PROMPT.search(text)


@pytest.mark.parametrize("text", [
    "user@debian:~$ ", "passwd: password updated successfully", "cat /etc/passwd", "Password changed: yes ok",
])
def test_other_output_is_not_a_prompt(text):
    assert not PASSWORD_PROMPT.search(text)


def test_manager_creates_edits_and_deletes(ssh, tmp_path):
    import asyncio

    from textual.widgets import Input

    from zir.widgets.credentials import CredentialManager
    from test_app import make_app

    path = tmp_path / "credentials.enc"

    async def scenario():
        app = make_app()
        app.vault = Vault(path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f6")
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, CredentialManager)
            screen.query_one("#cred-name", Input).value = "root on debian"
            screen.query_one("#cred-login", Input).value = "root"
            screen.query_one("#cred-password", Input).value = "pw-1"
            await pilot.click("#cred-save")
            await pilot.pause(0.2)
            assert [c.password for c in load(path)] == ["pw-1"]

            password = screen.query_one("#cred-password", Input)
            assert password.password
            await pilot.click("#cred-reveal")
            assert not password.password

            password.value = "pw-2"
            await pilot.click("#cred-save")
            await pilot.pause(0.2)
            assert [c.password for c in load(path)] == ["pw-2"]

            await pilot.click("#cred-delete")
            assert str(screen.query_one("#cred-delete").label) == "Really delete?"
            await pilot.pause(0.6)  # not a double click
            await pilot.click("#cred-delete")  # confirm
            await pilot.pause(0.2)
            assert load(path) == []

    asyncio.run(scenario())
