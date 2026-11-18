"""File systems for the Files section: this computer and a server over SSH.

The server side needs nothing but a POSIX shell, `stat`, `find` and `tar`
(GNU, BSD and BusyBox all work). Files travel as a tar stream through the
user's own `ssh`, which shares the polling connection (ControlMaster).

"Run as" another user goes through `su - <user>`: su reads its password from
a terminal, so those commands run under `ssh -tt` and only exchange text.
File contents then go through a private temporary file that the SSH user
writes or reads, while the other user extracts or packs it.

Every method here blocks: the UI calls them in a thread.
"""

from __future__ import annotations

import base64
import os
import queue
import re
import shlex
import shutil
import stat as statmod
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .i18n import _

CHUNK = 256 * 1024
VIEW_LIMIT = 512 * 1024  # F3 shows at most this much of a file
EDIT_LIMIT = 1024 * 1024  # F4 refuses bigger files
TIMEOUT = 60.0  # one remote command (listing, mkdir, ...)
LONG_TIMEOUT = 6 * 3600.0  # packing / unpacking under su

Progress = Callable[[int, str], None]  # (bytes done since the last call, current file)


class FsError(Exception):
    """A file operation failed; the message is shown to the user."""


class Cancelled(Exception):
    pass


@dataclass
class Entry:
    """One row of a panel."""

    name: str
    kind: str  # dir, file, link, other
    size: int = 0
    mtime: float = 0.0
    mode: str = ""  # rwxr-xr-x ("" when unknown, e.g. on Windows)
    owner: str = ""
    link_dir: bool = False  # a symlink to a folder: opens like one

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir" or self.link_dir

    @property
    def executable(self) -> bool:
        return self.kind == "file" and "x" in self.mode


@dataclass
class Item:
    """A file or folder inside a copy, relative to the folder it is copied from."""

    rel: str  # always with "/"
    kind: str
    size: int = 0
    mtime: float = 0.0


def _kind_from_mode(mode: str) -> str:
    return {"d": "dir", "l": "link", "-": "file"}.get(mode[:1], "other")


def _parse_stat_line(line: str) -> tuple[int, float, str, str, str] | None:
    """`size/mtime/mode/owner/path` (the path may contain "/")."""
    parts = line.split("/", 4)
    if len(parts) < 5:
        return None
    size, mtime, mode, owner, path = parts
    try:
        return int(size), float(mtime), mode, owner, path
    except ValueError:
        return None


def check_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise FsError(_("“{name}” is not a valid name", name=name))
    return name


class FileSystem:
    remote = False
    run_as: str | None = None

    @property
    def title(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def home(self) -> str:
        raise NotImplementedError

    def list(self, path: str) -> tuple[str, list[Entry]]:
        """The folder's real path and its entries ("" is home, or the drives on Windows)."""
        raise NotImplementedError

    def join(self, folder: str, name: str) -> str:
        raise NotImplementedError

    def parent(self, path: str) -> str | None:
        raise NotImplementedError

    def basename(self, path: str) -> str:
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def remove(self, folder: str, names: list[str]) -> None:
        raise NotImplementedError

    def rename(self, source: str, target: str) -> None:
        raise NotImplementedError

    def inventory(self, folder: str, names: Iterable[str]) -> list[Item]:
        """`names` in `folder` and everything inside them (missing names are left out)."""
        raise NotImplementedError

    def read_head(self, path: str, limit: int) -> bytes:
        raise NotImplementedError

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# This computer
# ---------------------------------------------------------------------------


def _owner(uid: int, cache: dict[int, str] = {}) -> str:  # noqa: B006 - deliberate cache
    if uid not in cache:
        try:
            import pwd

            cache[uid] = pwd.getpwuid(uid).pw_name
        except (ImportError, KeyError):
            cache[uid] = str(uid)
    return cache[uid]


def _windows_drives() -> list[str]:
    import string

    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]


class LocalFS(FileSystem):
    """Files on this computer. On Windows "" lists the drives."""

    def __init__(self, label: str | None = None) -> None:
        self.label = label

    @property
    def title(self) -> str:
        return self.label or _("This computer")

    def home(self) -> str:
        return os.path.expanduser("~")

    def list(self, path: str) -> tuple[str, list[Entry]]:
        if not path and sys.platform == "win32":
            return "", [Entry(drive, "dir") for drive in _windows_drives()]
        path = os.path.abspath(path or self.home())
        entries = []
        try:
            with os.scandir(path) as listing:
                for item in listing:
                    entries.append(self._entry(item))
        except OSError as exc:
            raise FsError(exc.strerror or str(exc)) from None
        return path, entries

    @staticmethod
    def _entry(item: os.DirEntry) -> Entry:
        try:
            info = item.stat(follow_symlinks=False)
        except OSError:
            return Entry(item.name, "other")
        if statmod.S_ISLNK(info.st_mode):
            kind = "link"
        elif statmod.S_ISDIR(info.st_mode):
            kind = "dir"
        elif statmod.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        link_dir = False
        if kind == "link":
            try:
                link_dir = item.is_dir(follow_symlinks=True)
            except OSError:
                pass
        posix = sys.platform != "win32"
        return Entry(
            item.name, kind, info.st_size if kind == "file" else 0, info.st_mtime,
            statmod.filemode(info.st_mode)[1:] if posix else "",
            _owner(info.st_uid) if posix else "",
            link_dir,
        )

    def join(self, folder: str, name: str) -> str:
        return os.path.join(folder, name) if folder else name

    def parent(self, path: str) -> str | None:
        if not path:
            return None
        parent = os.path.dirname(path.rstrip("\\/") or path)
        if parent == path or not parent:
            return "" if sys.platform == "win32" else None
        if sys.platform == "win32" and os.path.dirname(path) == path:
            return ""  # C:\ -> the drives
        return parent

    def basename(self, path: str) -> str:
        return os.path.basename(path.rstrip("\\/")) or path

    def mkdir(self, path: str) -> None:
        try:
            os.makedirs(path)
        except FileExistsError:
            raise FsError(_("“{name}” already exists", name=self.basename(path))) from None
        except OSError as exc:
            raise FsError(exc.strerror or str(exc)) from None

    def ensure_dir(self, path: str) -> None:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise FsError(exc.strerror or str(exc)) from None

    def remove(self, folder: str, names: list[str]) -> None:
        def unlock(function, target, _info) -> None:  # read-only files on Windows
            os.chmod(target, statmod.S_IWRITE)
            function(target)

        for name in names:
            path = self.join(folder, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    if sys.version_info >= (3, 12):
                        shutil.rmtree(path, onexc=unlock)
                    else:  # pragma: no cover - Python 3.11
                        shutil.rmtree(path, onerror=unlock)
                else:
                    try:
                        os.remove(path)
                    except PermissionError:
                        if sys.platform != "win32":
                            raise
                        os.chmod(path, statmod.S_IWRITE)
                        os.remove(path)
            except OSError as exc:
                raise FsError(f"{name}: {exc.strerror or exc}") from None

    def rename(self, source: str, target: str) -> None:
        if os.path.lexists(target):
            raise FsError(_("“{name}” already exists", name=self.basename(target)))
        try:
            os.rename(source, target)
        except OSError as exc:
            if getattr(exc, "errno", None) == 18:  # another disk: copy and delete
                try:
                    shutil.move(source, target)
                    return
                except OSError as again:
                    raise FsError(again.strerror or str(again)) from None
            raise FsError(exc.strerror or str(exc)) from None

    def inventory(self, folder: str, names: Iterable[str]) -> list[Item]:
        items = []
        for name in names:
            top = self.join(folder, name)
            if not os.path.lexists(top):
                continue
            items.append(self._item(top, name))
            if os.path.isdir(top) and not os.path.islink(top):
                for root, dirs, files in os.walk(top):
                    dirs.sort()
                    rel_root = os.path.relpath(root, folder).replace(os.sep, "/")
                    for child in sorted(dirs) + sorted(files):
                        path = os.path.join(root, child)
                        items.append(self._item(path, f"{rel_root}/{child}"))
                    # os.walk does not descend into symlinked folders (followlinks=False).
        return items

    @staticmethod
    def _item(path: str, rel: str) -> Item:
        try:
            info = os.lstat(path)
        except OSError:
            return Item(rel, "other")
        if statmod.S_ISLNK(info.st_mode):
            return Item(rel, "link", 0, info.st_mtime)
        if statmod.S_ISDIR(info.st_mode):
            return Item(rel, "dir", 0, info.st_mtime)
        if statmod.S_ISREG(info.st_mode):
            return Item(rel, "file", info.st_size, info.st_mtime)
        return Item(rel, "other", 0, info.st_mtime)

    def read_head(self, path: str, limit: int) -> bytes:
        try:
            with open(path, "rb") as handle:
                return handle.read(limit)
        except OSError as exc:
            raise FsError(exc.strerror or str(exc)) from None

    def write_file(self, path: str, data: bytes) -> None:
        try:
            with open(path, "r+b" if os.path.exists(path) else "wb") as handle:  # keeps owner and mode
                handle.write(data)
                handle.truncate()
        except OSError as exc:
            raise FsError(exc.strerror or str(exc)) from None

    # -- copying -----------------------------------------------------------

    def export_tar(self, folder: str, items: list[Item], out, progress: Progress, cancel: threading.Event) -> list[str]:
        """Write `items` as a tar stream to `out`. Returns the files that could not be read."""
        errors = []
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.GNU_FORMAT, encoding="utf-8") as tar:
            for item in items:
                if cancel.is_set():
                    raise Cancelled()
                path = os.path.join(folder, *item.rel.split("/"))
                try:
                    info = tar.gettarinfo(path, arcname=item.rel)
                except OSError as exc:
                    errors.append(f"{item.rel}: {exc.strerror or exc}")
                    continue
                info.uid = info.gid = 0
                info.uname = info.gname = ""  # the receiving user owns the copy
                if sys.platform == "win32":  # Windows has no Unix rights: usual defaults
                    info.mode = 0o755 if info.isdir() else 0o644
                if info.isreg():
                    try:
                        with open(path, "rb") as handle:
                            tar.addfile(info, _CountingReader(handle, progress, item.rel, cancel))
                    except OSError as exc:
                        errors.append(f"{item.rel}: {exc.strerror or exc}")
                elif info.isdir() or info.issym():
                    tar.addfile(info)
        return errors

    def import_tar(self, folder: str, stream, skip: set[str], progress: Progress, cancel: threading.Event) -> list[str]:
        """Unpack a tar stream into `folder`, leaving out `skip`. Returns the problems."""
        errors: list[str] = []
        folders: list[tuple[str, float, int]] = []
        root = os.path.abspath(folder)
        with tarfile.open(fileobj=stream, mode="r|*", encoding="utf-8", errors="surrogateescape") as tar:
            for member in tar:
                if cancel.is_set():
                    raise Cancelled()
                rel = member.name
                while rel.startswith("./"):
                    rel = rel[2:]
                rel = rel.rstrip("/")
                if not rel or rel == ".":
                    continue
                parts = rel.split("/")
                if rel.startswith("/") or ".." in parts or (sys.platform == "win32" and ":" in parts[0]):
                    errors.append(_("{name}: unsafe path, skipped", name=rel))
                    continue
                if rel in skip:
                    continue
                target = os.path.join(root, *parts)
                try:
                    if member.isdir():
                        if os.path.lexists(target) and not os.path.isdir(target):
                            errors.append(_("{name}: a file with this name already exists", name=rel))
                            continue
                        os.makedirs(target, exist_ok=True)
                        folders.append((target, member.mtime, member.mode))
                    elif member.isreg():
                        if os.path.isdir(target) and not os.path.islink(target):
                            errors.append(_("{name}: a folder with this name already exists", name=rel))
                            continue
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        if os.path.islink(target):
                            os.remove(target)
                        source = tar.extractfile(member)
                        with open(target, "wb") as handle:
                            while True:
                                if cancel.is_set():
                                    raise Cancelled()
                                block = source.read(CHUNK)
                                if not block:
                                    break
                                handle.write(block)
                                progress(len(block), rel)
                        self._finish(target, member)
                    elif member.issym():
                        if os.path.lexists(target):
                            if os.path.isdir(target) and not os.path.islink(target):
                                errors.append(_("{name}: a folder with this name already exists", name=rel))
                                continue
                            os.remove(target)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        os.symlink(member.linkname, target)
                    elif member.islnk():
                        linked = os.path.join(root, *member.linkname.lstrip("./").split("/"))
                        if os.path.lexists(target):
                            os.remove(target)
                        shutil.copy2(linked, target)
                    else:
                        errors.append(_("{name}: special file, skipped", name=rel))
                except Cancelled:
                    raise
                except OSError as exc:
                    errors.append(f"{rel}: {exc.strerror or exc}")
        for path, mtime, mode in reversed(folders):  # after their content changed them
            self._finish_dir(path, mtime, mode)
        return errors

    @staticmethod
    def _finish(path: str, member: tarfile.TarInfo) -> None:
        try:
            if sys.platform != "win32":
                os.chmod(path, member.mode & 0o777)
            os.utime(path, (member.mtime, member.mtime))
        except OSError:
            pass

    @staticmethod
    def _finish_dir(path: str, mtime: float, mode: int) -> None:
        try:
            if sys.platform != "win32" and mode:
                os.chmod(path, (mode & 0o777) | 0o700)
            os.utime(path, (mtime, mtime))
        except OSError:
            pass

    def copy_local(self, folder: str, items: list[Item], target_folder: str, skip: set[str],
                   progress: Progress, cancel: threading.Event) -> list[str]:
        """Copy between two folders of this computer."""
        errors = []
        for item in items:
            if cancel.is_set():
                raise Cancelled()
            if item.rel in skip:
                continue
            source = os.path.join(folder, *item.rel.split("/"))
            target = os.path.join(target_folder, *item.rel.split("/"))
            try:
                if item.kind == "dir":
                    if os.path.lexists(target) and not os.path.isdir(target):
                        errors.append(_("{name}: a file with this name already exists", name=item.rel))
                        continue
                    os.makedirs(target, exist_ok=True)
                elif item.kind == "link":
                    if os.path.lexists(target):
                        os.remove(target)
                    os.symlink(os.readlink(source), target)
                elif item.kind == "file":
                    if os.path.isdir(target) and not os.path.islink(target):
                        errors.append(_("{name}: a folder with this name already exists", name=item.rel))
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(source, "rb") as src, open(target, "wb") as dst:
                        while True:
                            if cancel.is_set():
                                raise Cancelled()
                            block = src.read(CHUNK)
                            if not block:
                                break
                            dst.write(block)
                            progress(len(block), item.rel)
                    shutil.copystat(source, target)
                else:
                    errors.append(_("{name}: special file, skipped", name=item.rel))
            except Cancelled:
                raise
            except OSError as exc:
                errors.append(f"{item.rel}: {exc.strerror or exc}")
        return errors


class _CountingReader:
    """A file wrapper that reports how much tarfile has read from it."""

    def __init__(self, handle, progress: Progress, name: str, cancel: threading.Event) -> None:
        self.handle = handle
        self.progress = progress
        self.name = name
        self.cancel = cancel

    def read(self, size: int = -1) -> bytes:
        if self.cancel.is_set():
            raise Cancelled()
        block = self.handle.read(size)
        if block:
            self.progress(len(block), self.name)
        return block


# ---------------------------------------------------------------------------
# A server: POSIX shell commands
# ---------------------------------------------------------------------------

# GNU / BusyBox stat and BSD stat: size/mtime/rights/owner/name.
STAT_SETUP = (
    "if stat -c %s / >/dev/null 2>&1; then F=-c; P='%s/%Y/%A/%U/%n'; "
    "else F=-f; P='%z/%m/%Sp/%Su/%N'; fi\n"
)
LIST_SCRIPT = (
    'if [ -n "$1" ]; then cd -- "$1" || exit 3; else cd || exit 3; fi\n'
    '[ -r . ] || { echo "Permission denied" >&2; exit 4; }\n'
    "pwd\n" + STAT_SETUP +
    "set -- .[!.]* ..?* *\n"
    'stat $F "$P" -- "$@" 2>/dev/null\n'
    "echo //\n"
    'for f; do if [ -L "$f" ] && [ -d "$f" ]; then printf "%s\\n" "$f"; fi; done\n'
    "exit 0\n"
)
INVENTORY_SCRIPT = (
    '[ -d "$1" ] || exit 0\ncd -- "$1" || exit 3\nshift\n' + STAT_SETUP +
    'for n; do if [ -e "$n" ] || [ -L "$n" ]; then find "./$n" -exec stat $F "$P" -- {} +; fi; done\n'
    "exit 0\n"
)
MKDIR_SCRIPT = (
    'if [ -e "$1" ] || [ -L "$1" ]; then echo "exists" >&2; exit 17; fi\n'
    'mkdir -p -- "$1"\n'
)
ENSURE_DIR_SCRIPT = 'mkdir -p -- "$1"\n'
REMOVE_SCRIPT = 'cd -- "$1" || exit 3\nshift\nrm -rf -- "$@"\n'
RENAME_SCRIPT = (
    'if [ -e "$2" ] || [ -L "$2" ]; then echo "exists" >&2; exit 17; fi\n'
    'mv -- "$1" "$2"\n'
)
HEAD_SCRIPT = 'head -c "$2" -- "$1"\n'
HEAD64_SCRIPT = 'head -c "$2" -- "$1" | base64\n'
WRITE_SCRIPT = 'cat > "$1"\n'  # keeps the file's owner and rights
COPY_INTO_SCRIPT = 'cat -- "$2" > "$1"\n'
PACK_SCRIPT = 'cd -- "$1" || exit 3\nshift\nexec tar -cf - "$@"\n'
UNPACK_SCRIPT = 'cd -- "$1" || exit 3\nexec tar -xf -\n'
UNPACK_FILE_SCRIPT = 'cd -- "$1" || exit 3\ntar -xf "$2"\n'
# A private temporary file: the folder can be passed through, not listed, and
# the file has an unguessable name, so other users of the server cannot read it.
STAGE_IN_SCRIPT = (
    "umask 077\n"
    "d=$(mktemp -d \"${TMPDIR:-/tmp}/zir.XXXXXXXX\") || exit 1\n"
    'f=$(mktemp "$d/XXXXXXXXXXXX") || exit 1\n'
    'cat > "$f" || exit 1\n'
    'chmod 711 "$d"; chmod 644 "$f"\n'
    'printf "%s\\n" "$f"\n'
)
STAGE_PACK_SCRIPT = (
    "umask 077\n"
    "d=$(mktemp -d \"${TMPDIR:-/tmp}/zir.XXXXXXXX\") || exit 1\n"
    'f=$(mktemp "$d/XXXXXXXXXXXX") || exit 1\n'
    'cd -- "$1" || exit 3\nshift\n'
    'tar -cf "$f" "$@"; c=$?\n'
    'chmod 711 "$d"; chmod 644 "$f"\n'
    'printf "%s\\n" "$f"\n'
    'exit $c\n'
)
STAGE_PATH_SCRIPT = 'f=$(mktemp "${TMPDIR:-/tmp}/zir.XXXXXXXX") || exit 1; printf "%s\\n" "$f"\n'
CAT_SCRIPT = 'exec cat -- "$1"\n'
CLEAN_SCRIPT = 'case "$1" in */zir.*) rm -rf -- "${1%/*}" ;; esac\n'
CLEAN_FILE_SCRIPT = 'case "$1" in */zir.*) rm -f -- "$1" ;; esac\n'
WHOAMI_SCRIPT = "id -un 2>/dev/null || whoami\n"

BEGIN, ERRORS, END = "__ZIR_BEGIN__", "__ZIR_ERRORS__", "__ZIR_END__"
PROMPT = re.compile(r"[:：]\s*$")


class Runner:
    """Runs a POSIX shell script with arguments, locally (`sh`) or through `ssh`."""

    def __init__(self, ssh: list[str] | None = None, tty_ssh: list[str] | None = None) -> None:
        self.ssh = ssh  # ssh ... target (None: this computer, for tests and local hosts)
        self.tty_ssh = tty_ssh

    def argv(self, script: str, args: Iterable[str] = ()) -> list[str]:
        args = list(args)
        if self.ssh is None:
            return ["sh", "-c", script, "zir", *args]
        return [*self.ssh, " ".join(["sh", "-c", shlex.quote(script), "zir", *map(shlex.quote, args)])]

    def popen(self, script: str, args: Iterable[str] = (), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE):
        errors = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(self.argv(script, args), stdin=stdin, stdout=stdout, stderr=errors)
        except OSError as exc:
            errors.close()
            raise FsError(str(exc)) from None
        process.zir_errors = errors  # type: ignore[attr-defined]
        return process

    def run(self, script: str, args: Iterable[str] = (), data: bytes | None = None,
            timeout: float = TIMEOUT) -> tuple[int, bytes, str]:
        try:
            done = subprocess.run(
                self.argv(script, args), input=data, capture_output=True, timeout=timeout,
                stdin=None if data is not None else subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise FsError(_("timeout after {sec}s", sec=f"{timeout:.0f}")) from None
        except OSError as exc:
            raise FsError(str(exc)) from None
        if done.returncode == 255 and self.ssh is not None:
            raise FsError(_ssh_error(done.stderr.decode(errors="replace")))
        return done.returncode, done.stdout, done.stderr.decode(errors="replace").strip()


def _ssh_error(stderr: str) -> str:
    from .collector import _friendly_error

    message, hint = _friendly_error(stderr)
    return f"{message} {hint}".strip()


def finished(process: subprocess.Popen) -> str:
    """Wait for a streaming command; its error text if it failed."""
    code = process.wait()
    errors = process.zir_errors  # type: ignore[attr-defined]
    errors.seek(0)
    text = errors.read().decode(errors="replace").strip()
    errors.close()
    return "" if code == 0 else (text or _("exit code {code}", code=code))


class SuRunner:
    """Runs scripts as another user: `ssh -tt host su - <user> -s /bin/sh -c ...`.

    Only text goes through the terminal: the output is cut out between markers."""

    def __init__(self, runner: Runner, user: str, password: str) -> None:
        self.runner = runner
        self.user = user
        self.password = password

    def _command(self, script: str, args: Iterable[str]) -> list[str]:
        inner = " ".join(["sh", "-c", shlex.quote(script), "zir", *map(shlex.quote, args)])
        wrapper = (
            f"e=$(mktemp 2>/dev/null || echo /tmp/zir.err.$$); printf '\\n{BEGIN}\\n'; "
            f"{inner} 2>\"$e\" </dev/null; c=$?; printf '\\n{ERRORS}\\n'; cat \"$e\"; rm -f \"$e\"; "
            f"printf '\\n{END} %s\\n' \"$c\""
        )
        su = f"LC_ALL=C su - {shlex.quote(self.user)} -s /bin/sh -c {shlex.quote(wrapper)}"
        if self.runner.tty_ssh is None:  # this computer (tests): a terminal through `script`
            return ["sh", "-c", su]
        return [*self.runner.tty_ssh, su]

    def run(self, script: str, args: Iterable[str] = (), data: bytes | None = None,
            timeout: float = TIMEOUT) -> tuple[int, bytes, str]:
        if data is not None:  # pragma: no cover - callers stage data first
            raise FsError("SuRunner cannot pass data")
        try:
            process = subprocess.Popen(
                self._command(script, list(args)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise FsError(str(exc)) from None
        chunks: queue.Queue[bytes | None] = queue.Queue()

        def pump() -> None:
            try:
                while True:
                    block = process.stdout.read1(65536)  # type: ignore[union-attr]
                    if not block:
                        break
                    chunks.put(block)
            finally:
                chunks.put(None)

        threading.Thread(target=pump, daemon=True).start()
        output = b""
        answered = False
        deadline = time.monotonic() + timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise FsError(_("timeout after {sec}s", sec=f"{timeout:.0f}"))
                try:
                    block = chunks.get(timeout=min(left, 0.5))
                except queue.Empty:
                    continue
                if block is None:
                    break
                output += block
                if not answered and BEGIN.encode() not in output:
                    text = output.decode(errors="replace")
                    if PROMPT.search(text.rstrip("\r\n ") + " ") or PROMPT.search(text):
                        time.sleep(0.05)  # su switches echo off right after the prompt
                        try:
                            process.stdin.write(self.password.encode() + b"\n")  # type: ignore[union-attr]
                            process.stdin.flush()  # type: ignore[union-attr]
                        except OSError:
                            pass
                        answered = True
        finally:
            if process.poll() is None:
                try:
                    process.stdin.close()  # type: ignore[union-attr]
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        text = output.decode(errors="replace").replace("\r\n", "\n").replace("\r", "")
        if BEGIN not in text:
            if process.returncode == 255:
                raise FsError(_ssh_error(process.stderr.read().decode(errors="replace")))  # type: ignore[union-attr]
            lines = [line.strip() for line in text.splitlines() if line.strip() and not PROMPT.search(line)]
            detail = lines[-1] if lines else ""
            raise FsError(_("Cannot switch to {user}: wrong password or su is not allowed", user=self.user)
                          + (f" ({detail})" if detail else ""))
        body = text.split(BEGIN + "\n", 1)[1] if BEGIN + "\n" in text else text.split(BEGIN, 1)[1]
        stdout, _sep, rest = body.partition("\n" + ERRORS + "\n")
        errors, _sep, tail = rest.rpartition("\n" + END + " ")
        try:
            code = int(tail.split()[0])
        except (IndexError, ValueError):
            code = 1
        return code, stdout.encode(), errors.strip()


class ShellFS(FileSystem):
    """Files on a server (or, for tests, this computer through `sh`)."""

    remote = True

    def __init__(self, runner: Runner, label: str) -> None:
        self.runner = runner
        self.label = label
        self.su: SuRunner | None = None
        self._login: str | None = None

    # -- who ---------------------------------------------------------------

    @property
    def title(self) -> str:
        return self.label

    @property
    def run_as(self) -> str | None:  # type: ignore[override]
        return self.su.user if self.su else None

    def login(self) -> str:
        """The SSH user (whoami), asked once."""
        if self._login is None:
            code, out, _err = self.runner.run(WHOAMI_SCRIPT)
            self._login = out.decode(errors="replace").strip() if code == 0 else ""
        return self._login

    def switch_user(self, user: str | None, password: str = "") -> None:
        self.su = SuRunner(self.runner, user, password) if user else None

    def user(self) -> str:
        return self.su.user if self.su else self.login()

    def _exec(self, script: str, args: Iterable[str] = (), timeout: float = TIMEOUT) -> tuple[int, bytes, str]:
        return (self.su or self.runner).run(script, list(args), timeout=timeout)

    def _check(self, script: str, args: Iterable[str] = (), timeout: float = TIMEOUT) -> bytes:
        code, out, err = self._exec(script, args, timeout)
        if code == 17:
            raise FsError(_("“{name}” already exists", name=self.basename(list(args)[-1])))
        if code != 0:
            raise FsError(_clean_error(err) or _("exit code {code}", code=code))
        return out

    # -- paths -------------------------------------------------------------

    def home(self) -> str:
        return ""

    def join(self, folder: str, name: str) -> str:
        return folder.rstrip("/") + "/" + name if folder != "/" else "/" + name

    def parent(self, path: str) -> str | None:
        if not path or path == "/":
            return None
        return path.rstrip("/").rsplit("/", 1)[0] or "/"

    def basename(self, path: str) -> str:
        return path.rstrip("/").rsplit("/", 1)[-1] or "/"

    # -- reading -----------------------------------------------------------

    def list(self, path: str) -> tuple[str, list[Entry]]:
        code, out, err = self._exec(LIST_SCRIPT, [path])
        if code == 4:
            raise FsError(_("No permission to read this folder"))
        if code != 0:
            raise FsError(_clean_error(err) or _("Cannot open the folder"))
        text = out.decode("utf-8", errors="surrogateescape")
        head, _sep, links = text.partition("\n//\n")
        lines = head.split("\n")
        real = lines[0].strip() or path
        link_dirs = {line for line in links.split("\n") if line}
        entries = []
        for line in lines[1:]:
            parsed = _parse_stat_line(line)
            if parsed is None:
                continue
            size, mtime, mode, owner, name = parsed
            if name in (".", ".."):
                continue
            kind = _kind_from_mode(mode)
            entries.append(Entry(name, kind, size if kind == "file" else 0, mtime, mode[1:10], owner,
                                 name in link_dirs))
        return real, entries

    def inventory(self, folder: str, names: Iterable[str]) -> list[Item]:
        names = list(names)
        if not names:
            return []
        out = self._check(INVENTORY_SCRIPT, [folder, *names], timeout=LONG_TIMEOUT)
        items = []
        for line in out.decode("utf-8", errors="surrogateescape").split("\n"):
            parsed = _parse_stat_line(line)
            if parsed is None:
                continue
            size, mtime, mode, _owner_name, path = parsed
            rel = path[2:] if path.startswith("./") else path
            kind = _kind_from_mode(mode)
            items.append(Item(rel, kind, size if kind == "file" else 0, mtime))
        return items

    def read_head(self, path: str, limit: int) -> bytes:
        if self.su is None:
            code, out, err = self.runner.run(HEAD_SCRIPT, [path, str(limit)])
            if code != 0:
                raise FsError(_clean_error(err) or _("Cannot read the file"))
            return out
        out = self._check(HEAD64_SCRIPT, [path, str(limit)])
        try:
            return base64.b64decode(out)
        except ValueError:
            raise FsError(_("Cannot read the file")) from None

    # -- changing ----------------------------------------------------------

    def mkdir(self, path: str) -> None:
        self._check(MKDIR_SCRIPT, [path])

    def ensure_dir(self, path: str) -> None:
        self._check(ENSURE_DIR_SCRIPT, [path])

    def remove(self, folder: str, names: list[str]) -> None:
        self._check(REMOVE_SCRIPT, [folder, *names], timeout=LONG_TIMEOUT)

    def rename(self, source: str, target: str) -> None:
        self._check(RENAME_SCRIPT, [source, target], timeout=LONG_TIMEOUT)

    def write_file(self, path: str, data: bytes) -> None:
        if self.su is None:
            code, _out, err = self.runner.run(WRITE_SCRIPT, [path], data=data)
            if code != 0:
                raise FsError(_clean_error(err) or _("Cannot save the file"))
            return
        staged = self._stage_in(lambda stdin: stdin.write(data))
        try:
            self._check(COPY_INTO_SCRIPT, [path, staged])
        finally:
            self.runner.run(CLEAN_SCRIPT, [staged])

    # -- copying -----------------------------------------------------------

    def _stage_in(self, write: Callable) -> str:
        """Send data into a private temporary file as the SSH user; its path."""
        process = self.runner.popen(STAGE_IN_SCRIPT, stdin=subprocess.PIPE)
        failure = ""
        try:
            write(process.stdin)
        except (BrokenPipeError, OSError) as exc:
            failure = str(exc)
        except BaseException:
            process.kill()
            finished(process)
            raise
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
        path = process.stdout.read().decode(errors="replace").strip()
        error = finished(process)
        if error or failure or not path:
            raise FsError(_clean_error(error) or failure or _("Cannot create a temporary file on the server"))
        return path

    def upload(self, folder: str, write: Callable, cancel: threading.Event) -> None:
        """Unpack the tar stream that `write(stdin)` produces into `folder`."""
        if self.su is None:
            process = self.runner.popen(UNPACK_SCRIPT, [folder], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            try:
                write(process.stdin)
                process.stdin.close()
            except OSError:  # the server side stopped early: its error text says why
                pass
            except BaseException:
                process.kill()
                finished(process)
                raise
            error = finished(process)
            if error:
                raise FsError(_clean_error(error))
            return
        staged = self._stage_in(write)
        try:
            if cancel.is_set():
                raise Cancelled()
            self._check(UNPACK_FILE_SCRIPT, [folder, staged], timeout=LONG_TIMEOUT)
        finally:
            self.runner.run(CLEAN_SCRIPT, [staged])

    def download(self, folder: str, names: list[str], read: Callable) -> None:
        """Pack `names` and hand the tar stream to `read(stream)`."""
        paths = ["./" + name for name in names]
        staged = None
        if self.su is None:
            process = self.runner.popen(PACK_SCRIPT, [folder, *paths])
        else:
            code, out, err = self.su.run(STAGE_PACK_SCRIPT, [folder, *paths], timeout=LONG_TIMEOUT)
            lines = out.decode(errors="replace").strip().splitlines()
            staged = lines[-1].strip() if lines else ""
            if not staged.startswith("/") or code not in (0, 1):  # GNU tar: 1 = a file changed while reading
                if staged.startswith("/"):
                    self.su.run(CLEAN_SCRIPT, [staged])
                raise FsError(_clean_error(err) or _("exit code {code}", code=code))
            process = self.runner.popen(CAT_SCRIPT, [staged])
        try:
            try:
                read(process.stdout)
            except BaseException:
                process.kill()
                finished(process)
                raise
            error = finished(process)
            if error and process.returncode != 1:
                raise FsError(_clean_error(error))
        finally:
            if staged and self.su is not None:
                self.su.run(CLEAN_SCRIPT, [staged])


def _clean_error(text: str) -> str:
    """The last meaningful line of a shell error, without the `sh: 1:` / `zir:` noise."""
    lines = [line.strip() for line in text.replace("\r", "").splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    line = re.sub(r"^(zir|sh|bash|cd|tar|mkdir|mv|rm|find|cat|head):\s*(line \d+:\s*|\d+:\s*)?", "", line)
    return line


# ---------------------------------------------------------------------------
# Copying between the panels
# ---------------------------------------------------------------------------


@dataclass
class Conflict:
    source: Item
    target: Item


def plan_copy(source: FileSystem, folder: str, names: list[str], target: FileSystem,
              target_folder: str) -> tuple[list[Item], list[Conflict], list[str], set[str]]:
    """What will be copied, the files that already exist at the target, what cannot be
    copied (a folder over a file or the other way round) and the items left out for that."""
    items = source.inventory(folder, names)
    existing = {item.rel: item for item in target.inventory(target_folder, names)}
    conflicts: list[Conflict] = []
    problems: list[str] = []
    blocked: set[str] = set()
    blocked_dirs: list[str] = []
    for item in items:
        if any(item.rel.startswith(prefix + "/") for prefix in blocked_dirs):
            blocked.add(item.rel)
            continue
        there = existing.get(item.rel)
        if there is None:
            continue
        if item.kind == "dir":
            if there.kind != "dir":
                problems.append(_("{name}: a file with this name already exists", name=item.rel))
                blocked.add(item.rel)
                blocked_dirs.append(item.rel)
            continue  # folders merge
        if there.kind == "dir":
            problems.append(_("{name}: a folder with this name already exists", name=item.rel))
            blocked.add(item.rel)
            continue
        conflicts.append(Conflict(item, there))
    return items, conflicts, problems, blocked


def copy_items(source: FileSystem, folder: str, names: list[str], items: list[Item],
               target: FileSystem, target_folder: str, skip: set[str],
               progress: Progress, cancel: threading.Event) -> list[str]:
    """Copy (already planned) `items`; `skip` are files the user chose to keep. Returns problems."""
    errors: list[str] = []
    chosen = [item for item in items if item.rel not in skip]
    if isinstance(source, LocalFS) and isinstance(target, LocalFS):
        target.ensure_dir(target_folder)
        return source.copy_local(folder, items, target_folder, skip, progress, cancel)
    if isinstance(source, LocalFS) and isinstance(target, ShellFS):
        target.ensure_dir(target_folder)

        def write(stdin) -> None:
            errors.extend(source.export_tar(folder, chosen, stdin, progress, cancel))

        target.upload(target_folder, write, cancel)
        return errors
    if isinstance(source, ShellFS) and isinstance(target, LocalFS):
        target.ensure_dir(target_folder)

        def read(stream) -> None:
            errors.extend(target.import_tar(target_folder, stream, skip, progress, cancel))

        source.download(folder, names, read)
        return errors
    raise FsError(_("Copying between two servers is not supported"))


def removable(names: list[str], skip: set[str]) -> list[str]:
    """After a move: the names that were copied completely (nothing inside was skipped)."""
    return [name for name in names if not any(rel == name or rel.startswith(name + "/") for rel in skip)]


def is_inside(fs: FileSystem, folder: str, name: str, target_folder: str) -> bool:
    """Is `target_folder` the folder `name` itself or inside it (copying a folder into itself)?"""
    path = fs.join(folder, name)
    sep = "/" if isinstance(fs, ShellFS) else os.sep
    a = os.path.normcase(path.rstrip(sep)) if not isinstance(fs, ShellFS) else path.rstrip("/")
    b = os.path.normcase(target_folder.rstrip(sep)) if not isinstance(fs, ShellFS) else target_folder.rstrip("/")
    return b == a or b.startswith(a + sep)


def server_files(spec, collector, connect_timeout: int = 5) -> FileSystem:
    """The right panel's file system for a host: SSH, or this computer for local and demo hosts."""
    from .collector import Collector, ProbeError, ssh_command

    if spec.local or not isinstance(collector, Collector):
        return LocalFS(spec.name)
    try:
        plain = ssh_command(spec, connect_timeout) + [spec.target]
        tty = ssh_command(spec, connect_timeout, tty=True) + [spec.target]
    except ProbeError as exc:
        raise FsError(f"{exc} {exc.hint}".strip()) from None
    return ShellFS(Runner(plain, tty), spec.name)
