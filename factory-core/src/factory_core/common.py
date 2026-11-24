"""Helpers shared by factory-core modules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

STATE_FILE = ".factory-state.json"
PLAN_FILE = ".factory-plan.md"
TRIAGE_FILE = ".factory-triage.md"
WORKTREES_DIR = ".worktrees"
# Factory bookkeeping never belongs in a candidate patch or the tree hash.
INTERNAL_FILES = (STATE_FILE, PLAN_FILE, TRIAGE_FILE)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BLOCK = 2


class FactoryError(Exception):
    """An expected failure that should be reported with remediation."""

    def __init__(self, message: str, remediation: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation


def holdouts_root() -> Path:
    return Path(os.path.expanduser("~/.factory-holdouts"))


def run(
    args: list[str],
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = 30,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command without a shell, capturing bytes."""
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=timeout,
        input=input_bytes,
        stdin=subprocess.DEVNULL if input_bytes is None else None,
        capture_output=True,
        check=False,
    )


def git(*args: str, cwd: str | Path, env: dict[str, str] | None = None) -> str:
    """Run git and return stripped stdout, raising FactoryError on failure."""
    proc = run(["git", *args], cwd=cwd, env=env, timeout=120)
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise FactoryError(f"git {args[0]} failed: {detail}")
    return proc.stdout.decode(errors="replace").strip()


def worktree_root(path: str | Path | None = None) -> Path:
    """Return the top level of the current checkout (linked worktree or main)."""
    start = Path(path) if path else Path.cwd()
    proc = run(["git", "rev-parse", "--show-toplevel"], cwd=start)
    if proc.returncode != 0:
        raise FactoryError(
            f"{start} is not inside a Git repository.",
            "Run this command from a Git repository.",
        )
    return Path(proc.stdout.decode().strip())


def main_repo_root(path: str | Path | None = None) -> Path:
    """Return the primary checkout, even when called from a linked worktree."""
    top = worktree_root(path)
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=top)
    common_path = Path(common)
    return common_path.parent if common_path.name == ".git" else top


def repo_slug(path: str | Path | None = None) -> str:
    """Slug of the primary repository, so all worktrees share one hold-out suite."""
    return main_repo_root(path).name


def holdout_dir(path: str | Path | None = None) -> Path:
    return holdouts_root() / repo_slug(path)


def emit_block(reason: str, **extra: Any) -> NoReturn:
    """Report a blocking failure on stderr and stdout JSON, then exit 2."""
    print(reason, file=sys.stderr)
    print(json.dumps({"decision": "deny", "reason": reason, **extra}))
    sys.stdout.flush()
    raise SystemExit(EXIT_BLOCK)
