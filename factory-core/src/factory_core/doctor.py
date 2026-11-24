"""Mandatory prerequisite preflight. There is no fallback when a check fails."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .common import EXIT_BLOCK, EXIT_OK, holdouts_root, run

MIN_PYTHON = (3, 11)
MIN_GIT = (2, 30)
OK_MESSAGE = "OK: All factory prerequisites verified (Docker + Compose v2 active)"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    remediation: str = ""


def _probe(args: list[str], timeout: float = 15) -> tuple[bool, str]:
    try:
        proc = run(args, timeout=timeout)
    except FileNotFoundError:
        return False, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(args)} timed out after {timeout:.0f}s"
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    return proc.returncode == 0, out


def check_python() -> Check:
    version = sys.version_info[:3]
    text = ".".join(map(str, version))
    if version[:2] >= MIN_PYTHON:
        return Check("python", True, text)
    return Check("python", False, f"{text} is older than 3.11", "Install Python 3.11+: uv python install 3.11")


def check_git() -> Check:
    remedy = "Install Git 2.30+: https://git-scm.com/downloads"
    ok, out = _probe(["git", "--version"])
    if not ok:
        return Check("git", False, out, remedy)
    match = re.search(r"(\d+)\.(\d+)", out)
    if not match or (int(match[1]), int(match[2])) < MIN_GIT:
        return Check("git", False, f"{out} is older than 2.30", remedy)
    return Check("git", True, f"{match[1]}.{match[2]}")


def check_docker() -> Check:
    ok, out = _probe(["docker", "info", "--format", "{{.ServerVersion}}"])
    if ok and out and "error" not in out.lower():
        return Check("docker", True, out.splitlines()[0])
    return Check(
        "docker",
        False,
        f"Docker daemon is not responding: {out.splitlines()[-1] if out else 'no output'}",
        "Start Docker Engine (or Docker Desktop), then rerun: factory-core doctor",
    )


def check_compose() -> Check:
    remedy = "Install Docker Compose v2: https://docs.docker.com/compose/install/"
    ok, out = _probe(["docker", "compose", "version", "--short"])
    if not ok:
        return Check("compose", False, f"docker compose unavailable: {out}", remedy)
    match = re.search(r"(\d+)\.\d+", out)
    if not match or int(match[1]) < 2:
        return Check("compose", False, f"Compose {out} is older than v2", remedy)
    return Check("compose", True, out.lstrip("v"))


def check_path() -> Check:
    local_bin = os.path.realpath(os.path.expanduser("~/.local/bin"))
    entries = {os.path.realpath(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p}
    if local_bin in entries:
        return Check("path", True, "~/.local/bin on PATH")
    return Check(
        "path",
        False,
        "~/.local/bin is not on PATH",
        'Add to your shell profile: export PATH="$HOME/.local/bin:$PATH"',
    )


def check_holdouts() -> Check:
    root: Path = holdouts_root()
    remedy = f"Fix ownership and mode: mkdir -p {root} && chmod 700 {root}"
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root):
            pass
    except OSError as exc:
        return Check("holdouts", False, f"{root} is not writable: {exc}", remedy)
    if root.stat().st_mode & 0o077:
        return Check("holdouts", False, f"{root} is accessible by other users", remedy)
    return Check("holdouts", True, "writable, mode 700")


def run_checks() -> list[Check]:
    checks = [check_python(), check_git(), check_docker()]
    docker_ok = checks[-1].ok
    # Compose still gets probed when the daemon is down: it reports client state.
    checks.append(check_compose())
    checks.extend([check_path(), check_holdouts()])
    if not docker_ok:
        for check in checks:
            if check.name == "compose" and not check.ok:
                check.remediation = check.remediation or "Start Docker Engine."
    return checks


def failure_summary(checks: list[Check]) -> str:
    lines = ["Factory prerequisites not met:"]
    for check in checks:
        if not check.ok:
            lines.append(f"  - {check.name}: {check.detail}")
            if check.remediation:
                lines.append(f"    Fix: {check.remediation}")
    return "\n".join(lines)


def main(as_json: bool = False) -> int:
    checks = run_checks()
    payload = [asdict(c) for c in checks]
    if all(c.ok for c in checks):
        if as_json:
            print(json.dumps({"status": "ok", "message": OK_MESSAGE, "checks": payload}))
        else:
            print(OK_MESSAGE)
        return EXIT_OK
    summary = failure_summary(checks)
    print(summary, file=sys.stderr)
    print(json.dumps({"status": "error", "decision": "deny", "reason": summary, "checks": payload}))
    return EXIT_BLOCK
