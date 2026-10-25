"""Key installation with a fake `ssh` that behaves like a password-only server."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from zir import keysetup
from zir.config import HostSpec

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake ssh is a POSIX script")

FAKE_SSH = """#!/bin/sh
# Accepts only password "s3cret" (asked via $SSH_ASKPASS), appends stdin to $FAKE_AUTH_KEYS.
[ "$SSH_ASKPASS_REQUIRE" = force ] || { echo "askpass not forced" >&2; exit 2; }
answer=$("$SSH_ASKPASS" "password:")
if [ "$answer" != "s3cret" ]; then
  echo "root@host: Permission denied (publickey,password)." >&2
  exit 255
fi
echo "$@" > "$FAKE_ARGS"
cat >> "$FAKE_AUTH_KEYS"
"""


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AUTH_KEYS", str(tmp_path / "authorized_keys"))
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAATEST zir\n")
    return tmp_path, key


def test_install_key_with_password(fake_env):
    tmp_path, key = fake_env
    spec = HostSpec(name="h", target="root@203.0.113.10", port=2345)
    asyncio.run(keysetup.install_key(spec, "s3cret", key))

    assert (tmp_path / "authorized_keys").read_text() == "ssh-ed25519 AAAATEST zir\n"
    args = (tmp_path / "args").read_text()
    assert "-p 2345" in args and "PubkeyAuthentication=no" in args
    assert "root@203.0.113.10" in args
    # The temporary password file and askpass helper are gone.
    assert not any((tmp_path / "cache" / "zir").glob("askpass-*"))


def test_wrong_password(fake_env):
    _, key = fake_env
    spec = HostSpec(name="h", target="root@host")
    with pytest.raises(keysetup.KeySetupError, match="wrong password"):
        asyncio.run(keysetup.install_key(spec, "nope", key))


def test_public_key_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(keysetup, "ssh_dir", lambda: tmp_path)
    assert keysetup.public_key_for(HostSpec(name="h", target="h")) is None
    (tmp_path / "id_rsa.pub").write_text("ssh-rsa X\n")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 Y\n")
    assert keysetup.public_key_for(HostSpec(name="h", target="h")).name == "id_ed25519.pub"
    custom = tmp_path / "edge"
    (tmp_path / "edge.pub").write_text("ssh-ed25519 Z\n")
    assert keysetup.public_key_for(HostSpec(name="h", target="h", identity=str(custom))) == Path(
        str(custom) + ".pub"
    )


def test_generates_key_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(keysetup, "ssh_dir", lambda: tmp_path / ".ssh")
    public, created = asyncio.run(keysetup.ensure_public_key(HostSpec(name="h", target="h")))
    assert created and public.read_text().startswith("ssh-ed25519 ")
    assert oct((tmp_path / ".ssh" / "id_ed25519").stat().st_mode & 0o777) == "0o600"
