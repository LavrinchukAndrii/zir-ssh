"""The Files section's file systems: this computer and a server (here: `sh` on this computer)."""

import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from zir.filesystems import (
    FsError,
    LocalFS,
    Runner,
    ShellFS,
    SuRunner,
    copy_items,
    is_inside,
    plan_copy,
    removable,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the server side needs a POSIX shell")


def _tree(root: Path) -> None:
    (root / "site" / "css").mkdir(parents=True)
    (root / "site" / "index.html").write_text("<h1>hi</h1>")
    (root / "site" / "css" / "main.css").write_text("body{}")
    (root / "site" / "empty").mkdir()
    (root / "notes.txt").write_text("hello\n")
    (root / "Файл з пробілом.txt").write_text("юнікод", encoding="utf-8")


def _progress_counter():
    seen = []
    return seen, lambda n, name: seen.append((n, name))


def test_local_listing_and_operations(tmp_path):
    _tree(tmp_path)
    fs = LocalFS()
    real, entries = fs.list(str(tmp_path))
    assert real == str(tmp_path)
    by_name = {e.name: e for e in entries}
    assert by_name["site"].is_dir and by_name["notes.txt"].size == 6
    assert "Файл з пробілом.txt" in by_name

    fs.mkdir(fs.join(real, "new"))
    with pytest.raises(FsError):
        fs.mkdir(fs.join(real, "new"))
    fs.rename(fs.join(real, "new"), fs.join(real, "renamed"))
    with pytest.raises(FsError):
        fs.rename(fs.join(real, "notes.txt"), fs.join(real, "renamed"))
    fs.remove(real, ["renamed", "notes.txt"])
    assert not (tmp_path / "renamed").exists() and not (tmp_path / "notes.txt").exists()
    assert fs.parent(str(tmp_path / "site")) == str(tmp_path)
    rels = {item.rel for item in fs.inventory(real, ["site", "missing"])}
    assert rels == {"site", "site/css", "site/css/main.css", "site/index.html", "site/empty"}


def test_local_to_local_copy_with_conflicts(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.mkdir()
    dst.mkdir()
    _tree(src)
    (dst / "site").mkdir()
    (dst / "site" / "index.html").write_text("old")
    (dst / "notes.txt").write_text("old")
    fs = LocalFS()
    items, conflicts, problems, _blocked = plan_copy(fs, str(src), ["site", "notes.txt"], fs, str(dst))
    assert {c.source.rel for c in conflicts} == {"site/index.html", "notes.txt"}
    assert not problems
    seen, progress = _progress_counter()
    errors = copy_items(fs, str(src), ["site", "notes.txt"], items, fs, str(dst), {"notes.txt"},
                        progress, threading.Event())
    assert not errors
    assert (dst / "site" / "index.html").read_text() == "<h1>hi</h1>"  # overwritten
    assert (dst / "notes.txt").read_text() == "old"  # skipped
    assert (dst / "site" / "empty").is_dir()
    assert sum(n for n, _name in seen) == len("<h1>hi</h1>") + len("body{}")
    assert removable(["site", "notes.txt"], {"notes.txt"}) == ["site"]


def test_copying_a_folder_into_itself_is_detected(tmp_path):
    fs = LocalFS()
    assert is_inside(fs, str(tmp_path), "site", str(tmp_path / "site" / "css"))
    assert not is_inside(fs, str(tmp_path), "site", str(tmp_path / "site2"))


@posix_only
def test_shell_listing_handles_hidden_links_and_errors(tmp_path):
    _tree(tmp_path)
    (tmp_path / ".hidden").write_text("x")
    os.symlink(tmp_path / "site", tmp_path / "link-to-site")
    os.symlink(tmp_path / "notes.txt", tmp_path / "link-to-file")
    (tmp_path / "run.sh").write_text("#!/bin/sh\n")
    (tmp_path / "run.sh").chmod(0o755)
    fs = ShellFS(Runner(), "test")
    real, entries = fs.list(str(tmp_path))
    by_name = {e.name: e for e in entries}
    assert os.path.realpath(real) == os.path.realpath(tmp_path)
    assert set(by_name) == {"site", ".hidden", "link-to-site", "link-to-file", "notes.txt", "run.sh",
                            "Файл з пробілом.txt"}
    assert by_name["site"].kind == "dir"
    assert by_name["link-to-site"].kind == "link" and by_name["link-to-site"].is_dir
    assert by_name["link-to-file"].kind == "link" and not by_name["link-to-file"].is_dir
    assert by_name["notes.txt"].size == 6 and by_name["notes.txt"].mode.startswith("rw")
    assert by_name["run.sh"].executable
    assert fs.list("")[0] == os.path.expanduser("~") or fs.list("")[0]  # home
    with pytest.raises(FsError):
        fs.list(str(tmp_path / "missing"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert fs.list(str(empty))[1] == []


@posix_only
def test_shell_operations(tmp_path):
    _tree(tmp_path)
    fs = ShellFS(Runner(), "test")
    folder = str(tmp_path)
    fs.mkdir(fs.join(folder, "new dir"))
    assert (tmp_path / "new dir").is_dir()
    with pytest.raises(FsError, match="already exists|вже існує"):
        fs.mkdir(fs.join(folder, "new dir"))
    fs.rename(fs.join(folder, "new dir"), fs.join(folder, "-dash"))
    assert (tmp_path / "-dash").is_dir()
    fs.remove(folder, ["-dash", "site"])
    assert not (tmp_path / "-dash").exists() and not (tmp_path / "site").exists()
    assert fs.read_head(fs.join(folder, "notes.txt"), 3) == b"hel"
    fs.write_file(fs.join(folder, "notes.txt"), b"new text")
    assert (tmp_path / "notes.txt").read_text() == "new text"
    assert fs.parent("/var/www") == "/var" and fs.parent("/var") == "/" and fs.parent("/") is None


@posix_only
def test_upload_and_download_round_trip(tmp_path):
    local_dir, server_dir, back_dir = tmp_path / "local", tmp_path / "server", tmp_path / "back"
    local_dir.mkdir()
    _tree(local_dir)
    big = local_dir / "site" / "big.bin"
    big.write_bytes(os.urandom(700_000))
    (local_dir / "site" / "run.sh").write_text("#!/bin/sh\n")
    (local_dir / "site" / "run.sh").chmod(0o750)
    os.symlink("index.html", local_dir / "site" / "home.html")
    local, server = LocalFS(), ShellFS(Runner(), "test")
    names = ["site", "Файл з пробілом.txt"]

    # Upload into a folder that does not exist yet.
    items, conflicts, _problems, _blocked = plan_copy(local, str(local_dir), names, server, str(server_dir))
    assert not conflicts
    seen, progress = _progress_counter()
    errors = copy_items(local, str(local_dir), names, items, server, str(server_dir), set(), progress,
                        threading.Event())
    assert not errors
    assert (server_dir / "site" / "big.bin").read_bytes() == big.read_bytes()
    assert (server_dir / "Файл з пробілом.txt").read_text(encoding="utf-8") == "юнікод"
    assert os.readlink(server_dir / "site" / "home.html") == "index.html"
    assert stat.S_IMODE((server_dir / "site" / "run.sh").stat().st_mode) & 0o100
    assert sum(n for n, _name in seen) >= 700_000

    # Uploading again: every file is a conflict; skip one.
    (server_dir / "site" / "index.html").write_text("changed on the server")
    items, conflicts, _problems, _blocked = plan_copy(local, str(local_dir), names, server, str(server_dir))
    assert "site/index.html" in {c.source.rel for c in conflicts}
    copy_items(local, str(local_dir), names, items, server, str(server_dir), {"site/index.html"},
               lambda *a: None, threading.Event())
    assert (server_dir / "site" / "index.html").read_text() == "changed on the server"

    # Download with one file kept.
    back_dir.mkdir()
    (back_dir / "site").mkdir()
    (back_dir / "site" / "index.html").write_text("mine")
    items, conflicts, _problems, _blocked = plan_copy(server, str(server_dir), ["site"], local, str(back_dir))
    assert [c.source.rel for c in conflicts] == ["site/index.html"]
    seen, progress = _progress_counter()
    errors = copy_items(server, str(server_dir), ["site"], items, local, str(back_dir), {"site/index.html"},
                        progress, threading.Event())
    assert not errors
    assert (back_dir / "site" / "index.html").read_text() == "mine"
    assert (back_dir / "site" / "big.bin").read_bytes() == big.read_bytes()
    assert (back_dir / "site" / "css" / "main.css").read_text() == "body{}"
    assert (back_dir / "site" / "empty").is_dir()
    assert os.readlink(back_dir / "site" / "home.html") == "index.html"


@posix_only
def test_folder_over_file_is_a_problem_not_a_conflict(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "data").mkdir()
    (src / "data" / "a.txt").write_text("a")
    (dst / "data").write_text("a file")
    items, conflicts, problems, _blocked = plan_copy(LocalFS(), str(src), ["data"], ShellFS(Runner(), "t"), str(dst))
    assert not conflicts and len(problems) == 1 and _blocked == {"data", "data/a.txt"}


@posix_only
def test_su_runner_answers_the_password_prompt(tmp_path, monkeypatch):
    """A stand-in `su` that asks for a password like the real one."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "su").write_text(
        "#!/bin/sh\n"
        "printf 'Password: '\n"
        "read -r pw\n"
        "if [ \"$pw\" != secret ]; then echo; echo 'su: Authentication failure'; exit 1; fi\n"
        "echo\n"
        "shift 4\n"  # - user -s /bin/sh
        "shift\n"  # -c
        "exec /bin/sh -c \"$1\"\n"
    )
    (fake / "su").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    _tree(tmp_path)
    fs = ShellFS(Runner(), "test")
    fs.switch_user("root", "secret")
    real, entries = fs.list(str(tmp_path))
    assert "notes.txt" in {e.name for e in entries}
    with pytest.raises(FsError):
        fs.list(str(tmp_path / "missing"))
    assert fs.read_head(str(tmp_path / "notes.txt"), 100) == b"hello\n"

    # Files go through a private temporary file the other user unpacks.
    target = tmp_path / "uploaded"
    local_dir = tmp_path / "site"
    items, _c, _p, _b = plan_copy(LocalFS(), str(tmp_path), ["site"], fs, str(target))
    assert not copy_items(LocalFS(), str(tmp_path), ["site"], items, fs, str(target), set(),
                          lambda *a: None, threading.Event())
    assert (target / "site" / "css" / "main.css").read_text() == "body{}"
    back = tmp_path / "back"
    items, _c, _p, _b = plan_copy(fs, str(target), ["site"], LocalFS(), str(back))
    assert not copy_items(fs, str(target), ["site"], items, LocalFS(), str(back), set(),
                          lambda *a: None, threading.Event())
    assert (back / "site" / "index.html").read_text() == (local_dir / "index.html").read_text()
    fs.write_file(str(tmp_path / "notes.txt"), b"as root")
    assert (tmp_path / "notes.txt").read_text() == "as root"

    fs.switch_user("root", "wrong")
    with pytest.raises(FsError, match="root"):
        fs.list(str(tmp_path))
    assert isinstance(fs.su, SuRunner)
    fs.switch_user(None)
    assert fs.su is None
