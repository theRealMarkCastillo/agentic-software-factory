"""Repository initialization and Git worktree lifecycle."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from . import doctor, state_manager
from .common import (
    EXIT_BLOCK, EXIT_OK, STATE_FILE, WORKTREES_DIR, FactoryError,
    git, holdouts_root, main_repo_root, repo_slug, run, worktree_root,
)

TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXCLUDES = (f"{WORKTREES_DIR}/", STATE_FILE)
TEMPLATE_NAME = "holdout-repo-template"

DEFAULT_AGENTS_MD = """# Agent Instructions

This repository is managed by the local agentic software factory.

- Write `.factory-plan.md` before editing application code (`factory-spec-plan` skill).
- Keep changes inside the scope of the plan and add unit tests under `tests/`.
- Work for a task happens in its own worktree: `factory-core worktree create <task-id>`.
- A sealed hold-out gate runs when you finish. On failure, follow the `factory-triage` skill
  and read `rejected_approaches` in `.factory-state.json` before retrying.
- `.factory-state.json` is read-only for agents. `~/.factory-holdouts`, the Docker socket,
  and `.git/hooks/` are off limits.

Keep this file short. Put module-specific rules in a nested `AGENTS.md` next to the code.
"""


def template_dir() -> Path:
    packaged = Path(__file__).parent / "_templates" / TEMPLATE_NAME
    if packaged.is_dir():
        return packaged
    source = Path(__file__).resolve().parents[3] / "templates" / TEMPLATE_NAME
    if source.is_dir():
        return source
    raise FactoryError("Hold-out template is missing from this factory-core installation.",
                       "Reinstall with ./install.sh --core-only")


def ensure_excludes(main_root: Path) -> None:
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=main_root))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    lines = existing.splitlines()
    additions = [entry for entry in EXCLUDES if entry not in lines]
    if additions:
        prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
        exclude.write_text(prefix + "\n".join(additions) + "\n")


def init_repo(repo: str | None = None) -> int:
    if doctor.main(as_json=False) != EXIT_OK:
        return EXIT_BLOCK
    try:
        main_root = main_repo_root(repo)
    except FactoryError as exc:
        print(f"{exc.message} {exc.remediation}".strip(), file=sys.stderr)
        print(json.dumps({"decision": "deny", "reason": exc.message}))
        return EXIT_BLOCK

    slug = repo_slug(main_root)
    target = holdouts_root() / slug
    created = not target.exists()
    if created:
        shutil.copytree(template_dir(), target)
        target.chmod(0o700)
    ensure_excludes(main_root)

    agents = main_root / "AGENTS.md"
    wrote_agents = not agents.exists()
    if wrote_agents:
        agents.write_text(DEFAULT_AGENTS_MD)

    print(f"Initialized factory for '{slug}':")
    print(f"  hold-out suite: {'created from template' if created else 'already present'}")
    print(f"  git excludes:   {', '.join(EXCLUDES)}")
    print(f"  AGENTS.md:      {'created (commit it)' if wrote_agents else 'already present'}")
    if created:
        print("Next: replace the example hold-out suite with real contracts for this repository.")
    return EXIT_OK


def _task_id_from_branch(root: Path) -> str:
    branch = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=root).stdout.decode().strip()
    name = branch.removeprefix("factory/") if branch else root.name
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "task"
    return slug[:64]


def create(task_id: str, quiet_stdout: bool = False) -> Path:
    """Create .worktrees/<task-id> on branch factory/<task-id> from HEAD."""
    if not TASK_ID.match(task_id):
        raise FactoryError(
            f"Invalid task id '{task_id}'.",
            "Use letters, digits, '.', '_' or '-', starting with a letter or digit (max 64 chars).",
        )
    main_root = main_repo_root()
    if run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=main_root).returncode != 0:
        raise FactoryError("The repository has no commits yet.", "Create an initial commit first.")
    ensure_excludes(main_root)
    path = main_root / WORKTREES_DIR / task_id
    if path.exists():
        raise FactoryError(f"Worktree {WORKTREES_DIR}/{task_id} already exists.",
                           "Pick another task id or remove it with: factory-core gc")
    git("worktree", "add", "-b", f"factory/{task_id}", str(path), "HEAD", cwd=main_root)
    state_manager.init_state(path, task_id)
    return path


def init_state(root: Path | None = None) -> Path:
    root = root or worktree_root()
    state_manager.init_state(root, _task_id_from_branch(root))
    return root


def hook_create() -> int:
    """WorktreeCreate hook: this replaces the harness's own worktree creation.

    The harness sends {"name": "<slug>"} on stdin and reads the worktree path from
    the last non-empty stdout line.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    name = payload.get("name") if isinstance(payload, dict) else None
    try:
        if isinstance(name, str) and name:
            task_id = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:64]
            path = create(task_id)
        else:
            path = init_state()
    except FactoryError as exc:
        print(f"{exc.message} {exc.remediation}".strip(), file=sys.stderr)
        return 1
    print(path)
    return EXIT_OK
