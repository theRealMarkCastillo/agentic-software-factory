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


def runtime_tmp_dir() -> Path:
    """Private staging directory under $HOME/.cache (or FACTORY_TMPDIR) for SUT builds and temp indexes."""
    path = Path(os.path.expanduser(os.environ.get("FACTORY_TMPDIR") or "~/.cache/factory-core/tmp"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


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


def porcelain_paths(cwd: str | Path, include_internal: bool = False) -> list[str] | None:
    """Return paths reported by `git status --porcelain -z`, or None if git status fails."""
    proc = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=cwd, timeout=60)
    if proc.returncode != 0:
        return None
    tokens = proc.stdout.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4:
            continue
        status_xy = entry[:2].decode(errors="replace")
        path_str = entry[3:].decode(errors="surrogateescape")
        if include_internal or path_str not in INTERNAL_FILES:
            paths.append(path_str)
        if ("R" in status_xy or "C" in status_xy) and index < len(tokens):
            old_path = tokens[index].decode(errors="surrogateescape")
            index += 1
            if include_internal and old_path and old_path not in INTERNAL_FILES:
                paths.append(old_path)
    return paths


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


def is_antigravity_payload(payload: dict[str, Any], platform: str | None = None) -> bool:
    """Detect Antigravity's camelCase hook payloads or explicit --platform antigravity."""
    if platform == "antigravity" or os.environ.get("FACTORY_PLATFORM") == "antigravity":
        return True
    return any(
        key in payload
        for key in ("toolCall", "workspacePaths", "artifactDirectoryPath", "fullyIdle", "terminationReason")
    )


def extract_tool_call(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract (lowercased tool_name, tool_args) across Claude/Codex and Antigravity schemas."""
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        name = str(tool_call.get("name", "")).lower()
        args = tool_call.get("args")
        return name, args if isinstance(args, dict) else {}
    name = str(payload.get("tool_name", "")).lower()
    args = payload.get("tool_input")
    return name, args if isinstance(args, dict) else {}


def hook_cwd(payload: dict[str, Any], prefer_tool_target: bool = False) -> str:
    """Resolve the workspace/worktree directory from a hook payload.

    Antigravity executes plugin hooks with process cwd set to <plugin-root> rather than
    the active workspace, passing workspacePaths (and optional toolCall.args.Cwd /
    TargetFile) on stdin instead.
    """
    _, tool_args = extract_tool_call(payload)
    lower_args = {str(k).lower(): v for k, v in tool_args.items()}
    if prefer_tool_target:
        for key in (
            "targetfile", "absolutepath", "file_path", "filepath", "path", "target",
            "notebook_path", "directorypath", "searchpath", "searchdirectory", "dir", "directory",
        ):
            val = lower_args.get(key)
            if isinstance(val, str) and val.strip():
                expanded = Path(os.path.expanduser(val))
                if expanded.is_absolute():
                    parent = expanded if expanded.is_dir() else expanded.parent
                    if parent.is_dir():
                        return str(parent)
    for key in ("cwd", "workdir"):
        val = lower_args.get(key)
        if isinstance(val, str) and val.strip():
            return os.path.expanduser(val)
    top_cwd = payload.get("cwd")
    if isinstance(top_cwd, str) and top_cwd.strip():
        return os.path.expanduser(top_cwd)
    workspaces = payload.get("workspacePaths")
    if isinstance(workspaces, list):
        for entry in workspaces:
            if isinstance(entry, str) and entry.strip():
                return os.path.expanduser(entry)
    return os.getcwd()


def emit_block(reason: str, **extra: Any) -> NoReturn:
    """Report a blocking failure on stderr and stdout JSON, then exit 2."""
    print(reason, file=sys.stderr)
    print(json.dumps({"decision": "deny", "reason": reason, **extra}))
    sys.stdout.flush()
    raise SystemExit(EXIT_BLOCK)
