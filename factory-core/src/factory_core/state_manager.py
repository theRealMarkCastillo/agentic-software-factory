"""Persistent per-worktree state: hashes, attempts, and rejected approaches."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import patch_exporter
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
        with exclusive_lock(path.with_suffix(".lock")):
            data = _ledger_read(path)
            if entry is None:
                if key not in data:
                    return
                del data[key]
            else:
                data[key] = entry
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".attempts-ledger-")
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(data, handle, indent=2)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
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


@contextmanager
def exclusive_lock(path: Path, *, wait: bool = True) -> Iterator[None]:
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise FactoryError("A hold-out gate is already running for this worktree.", "Retry after it finishes.") from exc
        yield


def compute_tree_hash(root: str | Path) -> str:
    return patch_exporter.compute_candidate_tree(root)


def is_pristine(root: Path, state: dict[str, Any], tree_hash: str) -> bool:
    baseline = state.get("base_tree_hash")
    if baseline == tree_hash:
        return True
    if state.get("base_tree_format") or not isinstance(baseline, str) or len(baseline) != 64:
        return False
    # Existing worktrees recorded the SHA-256 of raw working files at creation.
    # Only this baseline comparison uses their original encoding; passes must be reverified.
    listing = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root)
    if listing.returncode:
        raise FactoryError("Cannot read the legacy worktree baseline.")
    digest = hashlib.sha256()
    excluded = {os.fsencode(name) for name in INTERNAL_FILES}
    for name in sorted(set(listing.stdout.split(b"\0")) - excluded - {b""}):
        path = root / os.fsdecode(name)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        digest.update(b"P\0" + name + b"\0")
        if stat.S_ISLNK(mode):
            digest.update(b"L\0" + os.fsencode(os.readlink(path)) + b"\0")
        elif stat.S_ISREG(mode):
            digest.update(b"X\0" if mode & 0o111 else b"F\0")
            with path.open("rb") as handle:
                digest.update(hashlib.file_digest(handle, "sha256").digest())
        else:
            digest.update(b"D\0")
    return digest.hexdigest() == baseline


def suite_hash(seal: Path) -> str:
    """Fingerprint local oracle inputs, excluding the factory's mutable bookkeeping."""
    digest = hashlib.sha256()
    ignored = {LEDGER_FILE, Path(LEDGER_FILE).with_suffix(".lock").name, "telemetry.jsonl", ".git"}

    def visit(directory: Path, ancestors: set[Path]) -> None:
        for path in sorted(directory.iterdir()):
            if directory == seal and (path.name in ignored or path.name.startswith(".attempts-ledger-")):
                continue
            digest.update(os.fsencode(path.relative_to(seal)) + b"\0")
            if path.is_symlink():
                digest.update(b"L" + os.fsencode(os.readlink(path)) + b"\0")
            if path.is_dir():
                resolved = path.resolve()
                if resolved in ancestors:
                    raise FactoryError("Cannot fingerprint a cycle in the hold-out suite.")
                digest.update(b"D")
                visit(path, ancestors | {resolved})
            elif path.is_file():
                digest.update((path.stat().st_mode & 0o777).to_bytes(2, "big"))
                with path.open("rb") as handle:
                    digest.update(hashlib.file_digest(handle, "sha256").digest())
            else:
                raise FactoryError("Cannot fingerprint an unreadable or special file in the hold-out suite.")

    try:
        visit(seal, {seal.resolve()})
    except OSError as exc:
        raise FactoryError("Could not read hold-out verification inputs.", "Check the suite permissions and retry.") from exc
    return digest.hexdigest()


def is_verified(state: dict[str, Any], tree_hash: str, oracle_hash: str | None = None) -> bool:
    return state.get("verified_tree_hash") == tree_hash and state.get("verified_suite_hash") == oracle_hash


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


def mark_verified(root: str | Path, state: dict[str, Any], tree_hash: str, head_sha: str, oracle_hash: str | None = None) -> None:
    state["verified_tree_hash"] = tree_hash
    state["verified_suite_hash"] = oracle_hash
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
