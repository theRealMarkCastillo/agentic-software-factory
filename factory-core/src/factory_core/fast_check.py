"""Fast lint, type, and architectural boundary checks with silent success."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .common import (
    EXIT_BLOCK, EXIT_FAIL, EXIT_OK, FactoryError,
    git, hook_cwd, is_antigravity_payload, run, worktree_root,
)

TIMEOUT_SECONDS = 15.0
MAX_OUTPUT_LINES = 40
RESULT_FILE = "factory-last-fast-check.json"


@dataclass
class Step:
    name: str
    argv: list[str]
    hint: str


def _tool(root: Path, name: str, local_dirs: tuple[str, ...]) -> str | None:
    for local in local_dirs:
        candidate = root / local / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def _first_existing(root: Path, names: tuple[str, ...]) -> bool:
    return any((root / name).exists() for name in names)


def plan_steps(root: Path) -> tuple[list[Step], list[str]]:
    """Return runnable steps and the names of checkers that are not installed."""
    steps: list[Step] = []
    missing: list[str] = []

    def add(name: str, tool: str, local: tuple[str, ...], args: list[str], hint: str) -> None:
        path = _tool(root, tool, local)
        if path:
            steps.append(Step(name, [path, *args], hint))
        else:
            missing.append(tool)

    py_dirs = (".venv/bin", "venv/bin")
    node_dirs = ("node_modules/.bin",)

    if _first_existing(root, ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")):
        add("ruff", "ruff", py_dirs, ["check", "."],
            "Fix the reported lint rules; `ruff check . --fix` resolves the auto-fixable ones.")
        if (root / "src").is_dir():
            mypy_args = ["--config-file", "pyproject.toml", "src/"] if (root / "pyproject.toml").exists() else ["src/"]
            add("mypy", "mypy", py_dirs, mypy_args,
                "Correct the type errors at the listed locations; do not silence them with `# type: ignore`.")
        if _first_existing(root, (".importlinter", "importlinter.ini")):
            add("import-linter", "lint-imports", py_dirs, [],
                "An import crosses a declared layer boundary. Depend on the allowed layer or move the code.")

    if _first_existing(root, ("package.json", "tsconfig.json")):
        if (root / "tsconfig.json").exists():
            add("tsc", "tsc", node_dirs, ["--noEmit"], "Fix the TypeScript errors at the listed locations.")
        add("eslint", "eslint", node_dirs, ["."], "Fix the reported lint rules; `eslint . --fix` resolves some.")
        if any(root.glob(".dependency-cruiser.*")):
            add("dependency-cruiser", "depcruise", node_dirs, ["src"],
                "A module dependency violates a declared boundary rule. Import through the permitted module.")

    makefile = root / "Makefile"
    if makefile.is_file() and re.search(r"^fast-check\s*:", makefile.read_text(errors="replace"), re.MULTILINE):
        add("make fast-check", "make", (), ["fast-check"], "Fix the failures reported by the project's fast-check target.")

    return steps, missing


def changed_files(root: Path) -> list[str]:
    out = git("status", "--porcelain", cwd=root)
    return [line[3:] for line in out.splitlines() if len(line) > 3]


def _run_step(step: Step, root: Path, deadline: float) -> tuple[int, str, bool]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 124, "", True
    # Put this checkout first on the import path. A shared virtualenv may have another
    # worktree installed in editable mode, and boundary checkers analyse what they import.
    env = dict(os.environ)
    local = [str(root / "src"), str(root)] if (root / "src").is_dir() else [str(root)]
    env["PYTHONPATH"] = os.pathsep.join([*local, *filter(None, [env.get("PYTHONPATH")])])
    process = subprocess.Popen(
        step.argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        raw, _ = process.communicate(timeout=remaining)
        return process.returncode, raw.decode(errors="replace"), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raw, _ = process.communicate()
        return 124, raw.decode(errors="replace"), True


def _is_decoration(line: str) -> bool:
    """Banner art and rules cost tokens and carry no diagnosis."""
    return not any(ch.isalnum() for ch in line) or sum(0x2500 <= ord(ch) <= 0x25FF for ch in line) > 3


def _trim(output: str) -> str:
    lines = [line.rstrip() for line in output.strip().splitlines() if line.strip() and not _is_decoration(line)]
    if len(lines) > MAX_OUTPUT_LINES:
        hidden = len(lines) - MAX_OUTPUT_LINES
        lines = [*lines[:MAX_OUTPUT_LINES], f"... {hidden} more lines omitted"]
    return "\n".join(lines)


def _save_result(root: Path, result: str, duration: float) -> None:
    try:
        git_dir = Path(git("rev-parse", "--absolute-git-dir", cwd=root))
        (git_dir / RESULT_FILE).write_text(json.dumps({
            "result": result,
            "duration_s": round(duration, 2),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))
    except (FactoryError, OSError):
        pass


def load_result(root: Path) -> dict | None:
    try:
        git_dir = Path(git("rev-parse", "--absolute-git-dir", cwd=root))
        return json.loads((git_dir / RESULT_FILE).read_text())
    except (FactoryError, OSError, ValueError):
        return None


def main(hook: bool = False, platform: str | None = None) -> int:
    """Exit 0 on pass, 1 on failure. In hook mode failures exit 2 on stderr (or {} exit 0 on Antigravity)."""
    started = time.monotonic()
    payload: dict = {}
    if hook and not sys.stdin.isatty():
        # PostToolBatch has no matcher, so skip batches that edited nothing.
        try:
            parsed = json.loads(sys.stdin.read() or "{}")
            payload = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            payload = {}
        if payload.get("hook_event_name") == "PostToolBatch":
            calls = json.dumps(payload.get("tool_calls", payload))
            if not re.search(r'"(Edit|Write|MultiEdit|NotebookEdit)"', calls):
                return EXIT_OK

    antigravity = hook and is_antigravity_payload(payload, platform)
    target_cwd = hook_cwd(payload, prefer_tool_target=True) if payload else None
    try:
        root = worktree_root(target_cwd)
    except FactoryError:
        if antigravity:
            print("{}")
        else:
            print("OK (0.0s) [not a Git repository]")
        return EXIT_OK
    if run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root).returncode == 0 and not changed_files(root):
        if antigravity:
            print("{}")
        else:
            print("OK (0.0s)")
        return EXIT_OK

    steps, missing = plan_steps(root)
    deadline = started + TIMEOUT_SECONDS
    for step in steps:
        code, output, timed_out = _run_step(step, root, deadline)
        if code == 0:
            continue
        duration = time.monotonic() - started
        if timed_out:
            title = f"FAST-CHECK TIMEOUT: {step.name} exceeded the {TIMEOUT_SECONDS:.0f}s budget"
            hint = "Narrow the checker's scope or fix its configuration so it finishes within the budget."
        else:
            title = f"FAST-CHECK FAILED: {step.name} (exit {code}, {duration:.1f}s)"
            hint = step.hint
        report = "\n".join(part for part in (title, _trim(output), f"Remediation: {hint}") if part)
        _save_result(root, f"failed: {step.name}", duration)
        if antigravity:
            print(report, file=sys.stderr)
            print("{}")
            return EXIT_OK
        print(report, file=sys.stderr if hook else sys.stdout)
        return EXIT_BLOCK if hook else EXIT_FAIL

    duration = time.monotonic() - started
    suffix = f" [skipped, not installed: {', '.join(missing)}]" if missing else ""
    _save_result(root, "ok", duration)
    if antigravity:
        print("{}")
    else:
        print(f"OK ({duration:.1f}s){suffix}")
    return EXIT_OK
