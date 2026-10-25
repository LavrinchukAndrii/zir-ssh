"""Install the user's SSH public key on a host, like ssh-copy-id.

The only thing zir ever writes to a server, and only after the user typed the
password in the UI. The password goes to ssh through SSH_ASKPASS: a 0600 temp
file read by a tiny helper, deleted as soon as ssh exits. It is never stored.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .config import HostSpec
from .i18n import _
from .paths import cache_dir

ASKPASS_ENV = "ZIR_ASKPASS_FILE"
DEFAULT_KEYS = ("id_ed25519", "id_ecdsa", "id_rsa")

# POSIX sh, so it also works where bash is missing. The key arrives on stdin.
REMOTE_INSTALL = (
    "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
    "IFS= read -r key && "
    '{ grep -qxF "$key" ~/.ssh/authorized_keys || printf "%s\\n" "$key" >> ~/.ssh/authorized_keys; } && '
    "{ command -v restorecon >/dev/null 2>&1 && restorecon -R ~/.ssh || true; }"
)


class KeySetupError(Exception):
    pass


def ssh_dir() -> Path:
    return Path.home() / ".ssh"


def public_key_for(spec: HostSpec) -> Path | None:
    """The key ssh will offer for this host: its `identity`, else the first default one."""
    if spec.identity:
        candidate = Path(os.path.expanduser(spec.identity) + ".pub")
        return candidate if candidate.is_file() else None
    for name in DEFAULT_KEYS:
        candidate = ssh_dir() / f"{name}.pub"
        if candidate.is_file():
            return candidate
    return None


async def ensure_public_key(spec: HostSpec) -> tuple[Path, bool]:
    """Return (public key, created). Creates ~/.ssh/id_ed25519 without passphrase if none exists."""
    existing = public_key_for(spec)
    if existing is not None:
        return existing, False
    if spec.identity:
        raise KeySetupError(_("{key}.pub not found (set in identity for this host)", key=spec.identity))
    return await create_default_key(), True


async def create_default_key() -> Path:
    """Create ~/.ssh/id_ed25519 without passphrase; return the public key."""
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        raise KeySetupError(_("ssh-keygen not found; install the OpenSSH client"))
    ssh_dir().mkdir(mode=0o700, exist_ok=True)
    private = ssh_dir() / "id_ed25519"
    process = await asyncio.create_subprocess_exec(
        keygen, "-q", "-t", "ed25519", "-N", "", "-C", "zir", "-f", str(private),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise KeySetupError(_("ssh-keygen failed: {error}", error=stderr.decode(errors="replace").strip()))
    return private.with_suffix(".pub")


def _askpass_program(workdir: Path) -> str:
    """A program ssh runs to get the password (it prints the temp file)."""
    if sys.platform == "win32":
        if getattr(sys, "frozen", False):
            return sys.executable  # zir.exe answers itself, see answer_askpass()
        script = workdir / "askpass.cmd"
        script.write_text(f'@type "%{ASKPASS_ENV}%"\r\n')
        return str(script)
    script = workdir / "askpass.sh"
    script.write_text(f'#!/bin/sh\ncat "${ASKPASS_ENV}"\n')
    script.chmod(0o700)
    return str(script)


def answer_askpass() -> bool:
    """Called at startup: if ssh launched us as SSH_ASKPASS, print the password."""
    path = os.environ.get(ASKPASS_ENV)
    if not path or len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        return False
    try:
        sys.stdout.write(Path(path).read_text())
        sys.stdout.flush()
    except OSError:
        pass
    return True


def _ssh_command(spec: HostSpec, connect_timeout: int) -> list[str]:
    ssh = shutil.which("ssh")
    if ssh is None:
        raise KeySetupError(_("ssh client not found in PATH"))
    command = [
        ssh,
        "-T",
        "-o", "BatchMode=no",
        "-o", "PubkeyAuthentication=no",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", f"StrictHostKeyChecking={spec.strict_host_key}",
        "-o", "LogLevel=ERROR",
    ]
    if sys.platform != "win32":
        command += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    for option in spec.ssh_options:
        command += ["-o", option]
    if spec.port:
        command += ["-p", str(spec.port)]
    command += [spec.target, REMOTE_INSTALL]
    return command


async def install_key(
    spec: HostSpec, password: str, public_key: Path, timeout: float = 30.0, connect_timeout: int = 8
) -> None:
    key_line = public_key.read_text().strip()
    if not key_line:
        raise KeySetupError(_("{key} is empty", key=public_key))

    base = cache_dir()
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base, prefix="askpass-") as tmp:
        workdir = Path(tmp)
        os.chmod(workdir, 0o700)
        secret = workdir / "password"
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(password)

        env = {
            **os.environ,
            "SSH_ASKPASS": _askpass_program(workdir),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),  # older ssh only uses askpass with DISPLAY
            ASKPASS_ENV: str(secret),
        }
        process = await asyncio.create_subprocess_exec(
            *_ssh_command(spec, connect_timeout),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=sys.platform != "win32",  # no controlling tty: ssh must use askpass
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate((key_line + "\n").encode()), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise KeySetupError(_("no answer from {target} within {sec}s", target=spec.target, sec=f"{timeout:.0f}")) from None

    if process.returncode != 0:
        message = " ".join(stderr.decode(errors="replace").split())
        if "permission denied" in message.lower():
            raise KeySetupError(_("wrong password (or password login is disabled on the server)"))
        raise KeySetupError(message or _("ssh exited with code {code}", code=process.returncode))
