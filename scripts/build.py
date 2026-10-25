#!/usr/bin/env python3
"""Build single-file `zir` executables.

  python scripts/build.py          # executable for this OS/arch -> dist/
  python scripts/build.py --all    # macOS + Linux + Windows via GitHub Actions -> dist/
  python scripts/build.py --ci     # used by the workflow (current interpreter, no venv)

PyInstaller cannot cross-compile, so --all builds every target on its native
runner and downloads the results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
VENV = ROOT / ".venv-build"
SPEC = ROOT / "packaging" / "zir.spec"
WORKFLOW = "build.yml"


def target_name() -> str:
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"zir-{system}-{arch}"


def run(*command: str | Path, **kwargs) -> subprocess.CompletedProcess:
    printable = " ".join(str(part) for part in command)
    print(f"$ {printable}", flush=True)
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def ensure_venv() -> Path:
    python = venv_python()
    if not python.exists():
        run(sys.executable, "-m", "venv", VENV)
    run(python, "-m", "pip", "install", "-q", "--upgrade", "pip")
    run(python, "-m", "pip", "install", "-q", f"{ROOT}[build]")
    return python


def smoke_test(binary: Path) -> None:
    run(binary, "--version")
    # Exit code 1 means "some demo host is not ok", which is expected.
    result = subprocess.run(
        [str(binary), "--report", "--json", "--demo"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode not in (0, 1):
        sys.stderr.write(result.stderr)
        raise SystemExit(f"smoke test failed: exit code {result.returncode}")
    hosts = json.loads(result.stdout)
    if not hosts:
        raise SystemExit("smoke test failed: empty report")
    print(f"smoke test ok: {len(hosts)} demo host(s)")


def pyinstaller(python: Path, mode: str, distpath: Path, name: str) -> None:
    env = {**os.environ, "ZIR_BIN_NAME": name, "ZIR_MODE": mode}
    run(
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        distpath,
        "--workpath",
        BUILD / mode,
        SPEC,
        env=env,
    )


def build_macos_dmg(python: Path, name: str) -> Path:
    """zir.app packed into a .dmg with a drag-to-Applications window (packaging/dmg_settings.py)."""
    app_dist = BUILD / "app-dist"
    shutil.rmtree(app_dist, ignore_errors=True)
    pyinstaller(python, "app", app_dist, name)
    app = app_dist / "zir.app"
    # One consistent ad-hoc signature for the whole bundle (arm64 refuses unsigned code).
    run("codesign", "--force", "--deep", "--sign", "-", app)
    smoke_test(app / "Contents" / "MacOS" / "zir")

    dmg = DIST / f"{name}.dmg"
    dmg.unlink(missing_ok=True)
    packaging = ROOT / "packaging"
    run(python, "-m", "dmgbuild", "-s", packaging / "dmg_settings.py",
        "-D", f"app={app}", "-D", f"packaging={packaging}", "Zir SSH", dmg)
    return dmg


def build_local(python: Path) -> list[Path]:
    name = target_name()
    pyinstaller(python, "onefile", DIST, name)
    binary = DIST / (name + (".exe" if sys.platform == "win32" else ""))
    if not binary.exists():
        raise SystemExit(f"build finished but {binary} is missing")
    smoke_test(binary)
    built = [binary]
    if sys.platform == "darwin":
        built.append(build_macos_dmg(python, name))

    print()
    for path in built:
        print(f"built {path.relative_to(ROOT)} ({path.stat().st_size / 1024**2:.1f} MiB)")
    return built


def write_checksums() -> None:
    lines = []
    for path in sorted(DIST.iterdir()):
        if path.is_file() and path.name.startswith("zir-"):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}")
    (DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def build_all() -> None:
    gh = shutil.which("gh")
    if gh is None:
        raise SystemExit("--all needs the GitHub CLI (gh): https://cli.github.com")
    try:
        run(gh, "auth", "status", capture_output=True)
        run("git", "-C", ROOT, "remote", "get-url", "origin", capture_output=True)
    except subprocess.CalledProcessError:
        raise SystemExit(
            "--all needs a GitHub repository with this project pushed and "
            "`gh auth login`. Use plain `python scripts/build.py` for this OS."
        ) from None

    branch = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    run(gh, "workflow", "run", WORKFLOW, "--ref", branch, cwd=ROOT)

    # `gh workflow run` does not print the run id; find the run we just queued.
    run_id = None
    for _ in range(30):
        time.sleep(2)
        listing = subprocess.run(
            [gh, "run", "list", "--workflow", WORKFLOW, "--branch", branch,
             "--event", "workflow_dispatch", "--limit", "5",
             "--json", "databaseId,createdAt"],
            capture_output=True, text=True, check=True, cwd=ROOT,
        )
        runs = [row for row in json.loads(listing.stdout) if row["createdAt"] >= started]
        if runs:
            run_id = str(runs[0]["databaseId"])
            break
    if run_id is None:
        raise SystemExit("could not find the queued workflow run")

    run(gh, "run", "watch", run_id, "--exit-status", cwd=ROOT)
    DIST.mkdir(exist_ok=True)
    download = BUILD / "artifacts"
    shutil.rmtree(download, ignore_errors=True)
    run(gh, "run", "download", run_id, "--dir", download, cwd=ROOT)
    for path in download.rglob("zir-*"):
        if path.is_file():
            shutil.copy2(path, DIST / path.name)
    write_checksums()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="build macOS, Linux and Windows on GitHub Actions")
    group.add_argument("--ci", action="store_true", help="build with the current interpreter (CI)")
    args = parser.parse_args()

    if args.all:
        build_all()
    elif args.ci:
        build_local(Path(sys.executable))
    else:
        build_local(ensure_venv())


if __name__ == "__main__":
    main()
