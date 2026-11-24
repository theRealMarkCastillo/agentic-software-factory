"""Persistent per-worktree state: hashes, attempts, and rejected approaches."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import INTERNAL_FILES, STATE_FILE, FactoryError, holdout_dir, run

DEFAULT_MAX_RETRIES = 3
# Consecutive hold-out environment failures tolerated before the gate stops blocking.
ENV_FAILURE_LIMIT = 3
# Attempt history mirrored inside the sealed hold-out directory, which agents cannot reach.
LEDGER_FILE = "attempts-ledger.json"
BREAKER_WIDTH = 80


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_state(task_id: str, max_retries: int = DEFAULT_MAX_RETRIES) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "attempts": 0,
        "max_retries": max_retries,
        "verified_tree_hash": None,
        "last_evaluated_sha": None,
        "rejected_approaches": [],
        "updated_at": utc_now(),
    }


def state_path(root: str | Path) -> Path:
    return Path(root) / STATE_FILE


def load_state(root: str | Path) -> dict[str, Any] | None:
    path = state_path(root)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError("not an object")
        int(state["attempts"])
        int(state["max_retries"])
        if not isinstance(state["rejected_approaches"], list):
            raise ValueError("rejected_approaches is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FactoryError(
            f"{STATE_FILE} is corrupt: {exc}",
            f"Inspect {path}, then restore it or delete it and run: factory-core worktree init-state",
        ) from exc
    return state


def save_state(root: str | Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    path = state_path(root)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".factory-state-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def init_state(root: str | Path, task_id: str) -> dict[str, Any]:
    """Create state if missing; an existing valid state is preserved."""
    existing = load_state(root)
    if existing is not None:
        return existing
    state = new_state(task_id)
    save_state(root, state)
    return state


def _ledger_path(root: str | Path) -> Path | None:
    """The ledger lives next to the sealed suite. No hold-out directory means no ledger."""
    try:
        seal = holdout_dir(root)
    except FactoryError:
        return None
    return seal / LEDGER_FILE if seal.is_dir() else None


def _ledger_read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ledger_update(root: str | Path, entry: dict[str, Any] | None) -> None:
    path = _ledger_path(root)
    if path is None:
        return
    key = os.path.realpath(str(root))
    try:
        data = _ledger_read(path)
        if entry is None:
            if key not in data:
                return
            del data[key]
        else:
            data[key] = entry
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".attempts-ledger-")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # The ledger is a safety net; the worktree state file stays authoritative.


def ledger_clear(root: str | Path) -> None:
    """Forget the mirrored history. Called for a new worktree and for a human reset."""
    _ledger_update(root, None)


def restore_or_init(root: str | Path, task_id: str) -> dict[str, Any]:
    """Create state for a worktree, carrying over attempts if its state file was deleted."""
    existing = load_state(root)
    if existing is not None:
        return existing
    path = _ledger_path(root)
    saved = _ledger_read(path).get(os.path.realpath(str(root))) if path else None
    if not isinstance(saved, dict):
        return init_state(root, task_id)
    state = new_state(str(saved.get("task_id") or task_id))
    try:
        state["attempts"] = int(saved.get("attempts", 0))
        state["max_retries"] = int(saved.get("max_retries", DEFAULT_MAX_RETRIES))
    except (TypeError, ValueError):
        pass
    if isinstance(saved.get("rejected_approaches"), list):
        state["rejected_approaches"] = saved["rejected_approaches"]
    save_state(root, state)
    return state


def compute_tree_hash(root: str | Path) -> str:
    """SHA-256 over paths, modes, and contents of tracked + non-ignored untracked files."""
    proc = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, timeout=60)
    if proc.returncode != 0:
        raise FactoryError(f"Could not list files: {proc.stderr.decode(errors='replace').strip()}")
    names = sorted({n for n in proc.stdout.split(b"\0") if n})
    digest = hashlib.sha256()
    base = os.fsencode(str(root))
    internal = {name.encode() for name in INTERNAL_FILES}
    for name in names:
        if name in internal:
            continue
        full = os.path.join(base, name)
        try:
            info = os.lstat(full)
        except FileNotFoundError:
            continue  # Deleted from the working tree: absence changes the hash.
        digest.update(b"P\0" + name + b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"L\0" + os.readlink(full) + b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"X\0" if info.st_mode & 0o111 else b"F\0")
            file_digest = hashlib.sha256()
            try:
                with open(full, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        file_digest.update(chunk)
            except OSError as exc:
                raise FactoryError(
                    f"Cannot read {os.fsdecode(name)}: {exc.strerror or exc}",
                    "Fix its permissions, or remove or ignore the file.",
                ) from exc
            digest.update(file_digest.digest())
        else:
            digest.update(b"D\0")  # Submodule or directory entry.
    return digest.hexdigest()


def is_verified(state: dict[str, Any], tree_hash: str) -> bool:
    return state.get("verified_tree_hash") == tree_hash


def breaker_tripped(state: dict[str, Any]) -> bool:
    return int(state["attempts"]) >= int(state["max_retries"])


def breaker_banner(state: dict[str, Any]) -> str:
    rule = "=" * BREAKER_WIDTH
    return (
        f"{rule}\n"
        f"CIRCUIT BREAKER TRIPPED: Maximum verification attempts ({state['max_retries']}) reached.\n"
        "Halting automated retry loop. Passing control back to human operator.\n"
        f"{rule}"
    )


def mark_verified(root: str | Path, state: dict[str, Any], tree_hash: str, head_sha: str) -> None:
    state["verified_tree_hash"] = tree_hash
    state["last_evaluated_sha"] = head_sha
    state["env_failures"] = 0
    save_state(root, state)


def record_env_failure(root: str | Path, state: dict[str, Any]) -> int:
    """Count a run where the oracle never started. It is not an attempt and rejects no approach."""
    state["env_failures"] = int(state.get("env_failures") or 0) + 1
    save_state(root, state)
    return int(state["env_failures"])


def record_failure(
    root: str | Path,
    state: dict[str, Any],
    head_sha: str,
    summary: str,
    files_touched: list[str],
    failed_contract: str | None,
) -> int:
    state["attempts"] = int(state["attempts"]) + 1
    state["env_failures"] = 0
    state["verified_tree_hash"] = None
    state["last_evaluated_sha"] = head_sha
    state["rejected_approaches"].append(
        {
            "attempt": state["attempts"],
            "summary": summary,
            "files_touched": files_touched,
            "failed_contract": failed_contract,
        }
    )
    save_state(root, state)
    _ledger_update(root, {
        "task_id": state.get("task_id"),
        "attempts": state["attempts"],
        "max_retries": state["max_retries"],
        "rejected_approaches": state["rejected_approaches"],
    })
    return int(state["attempts"])


def dumps(state: dict[str, Any]) -> str:
    return json.dumps(state, indent=2)
