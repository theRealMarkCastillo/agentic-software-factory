"""The sealed hold-out gate: cache, circuit breaker, oracle run, sanitized feedback."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import compose_runner, doctor, patch_exporter, report_sanitizer, state_manager, telemetry
from .common import (
    EXIT_BLOCK,
    EXIT_OK,
    INTERNAL_FILES,
    WORKTREES_DIR,
    FactoryError,
    emit_block,
    git,
    holdout_dir,
    hook_cwd,
    is_antigravity_payload,
    main_repo_root,
    porcelain_paths,
    run,
    worktree_root,
)
from .worktree import _task_id_from_branch

SUMMARY_LIMIT = 300
_NORMAL_STOP_REASONS = {"", "model_stop", "no_tool_call"}


# Stdin can be read once; the crash handler in main() needs the payload too.
_last_payload: dict = {}


def _read_hook_payload() -> dict:
    global _last_payload
    _last_payload = {}
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        _last_payload = parsed if isinstance(parsed, dict) else {}
    except (ValueError, OSError):
        pass
    return _last_payload


def _agy_allow_stop(**extra: object) -> int:
    print(json.dumps({"decision": "stop", **extra}))
    return EXIT_OK


def _agy_block_stop(reason: str, continuation_active: bool = False, **extra: object) -> int:
    print(reason, file=sys.stderr)
    if continuation_active:
        return _agy_allow_stop(reason=reason, **extra)
    print(json.dumps({"decision": "continue", "reason": reason, **extra}))
    return EXIT_OK


def _summary(sanitized: str, contracts: list[str], timed_out: bool) -> str:
    if timed_out:
        return "Hold-out gate timed out before the verifier finished."
    for line in sanitized.splitlines():
        if line.startswith("E "):
            return line[1:].strip()[:SUMMARY_LIMIT]
    if contracts:
        return f"Contract failed: {contracts[0]}"[:SUMMARY_LIMIT]
    tail = [line for line in sanitized.splitlines() if line.strip()]
    return (tail[-1] if tail else "Hold-out environment failed before the verifier ran.")[:SUMMARY_LIMIT]


def _resolve_gate_root(root: Path, seal: Path) -> tuple[Path, str | None]:
    """If invoked on clean main while .worktrees/ has unverified work, redirect or block."""
    try:
        main_root = main_repo_root(root)
    except FactoryError:
        return root, None
    if root != main_root:
        return root, None
    worktrees_base = main_root / WORKTREES_DIR
    if not worktrees_base.is_dir():
        return root, None
    changes = porcelain_paths(main_root)
    if changes is None:
        return root, "Could not inspect the main checkout."
    if changes:
        return root, None
    try:
        main_hash = state_manager.compute_tree_hash(main_root)
        oracle_hash = state_manager.suite_hash(seal)
    except FactoryError as exc:
        return root, exc.message

    unverified: list[Path] = []
    listing = run(["git", "worktree", "list", "--porcelain", "-z"], cwd=root)
    if listing.returncode:
        return root, "Could not enumerate task worktrees."
    for field in listing.stdout.split(b"\0"):
        if not field.startswith(b"worktree "):
            continue
        candidate = Path(os.fsdecode(field.removeprefix(b"worktree ")))
        if candidate.parent != worktrees_base or not candidate.is_dir():
            continue
        try:
            wt_hash = state_manager.compute_tree_hash(candidate)
            wt_state = state_manager.load_state(candidate)
        except FactoryError as exc:
            return root, f"Could not inspect task worktree {candidate.name}: {exc.message}"
        if wt_state and int(wt_state["attempts"]) == 0 and not int(wt_state.get("env_failures") or 0) and state_manager.is_pristine(candidate, wt_state, wt_hash):
            continue
        if wt_hash == main_hash:
            continue
        if wt_state is not None and state_manager.breaker_tripped(wt_state):
            continue
        if wt_state is None or not state_manager.is_verified(wt_state, wt_hash, oracle_hash):
            unverified.append(candidate)

    if len(unverified) == 1:
        return unverified[0], None
    if len(unverified) > 1:
        names = ", ".join(f"{WORKTREES_DIR}/{p.name}" for p in unverified)
        return root, (
            f"Multiple unverified task worktrees exist ({names}) while running from the main checkout. "
            "Run `factory-core holdout-gate` from the target worktree."
        )
    return root, None


def _committed_files_touched(root: Path) -> list[str]:
    try:
        main_root = main_repo_root(root)
    except FactoryError:
        return []
    if root == main_root:
        return []
    main_head = run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=main_root).stdout.decode().strip()
    if not main_head:
        return []
    base = run(["git", "merge-base", "HEAD", main_head], cwd=root)
    if base.returncode != 0 or not base.stdout.strip():
        return []
    diff = run(["git", "diff", "--name-only", "-z", "--no-renames", base.stdout.decode().strip(), "HEAD"], cwd=root)
    if diff.returncode != 0:
        return []
    return [
        name.decode(errors="surrogateescape")
        for name in diff.stdout.split(b"\0")
        if name and name.decode(errors="surrogateescape") not in INTERNAL_FILES
    ]


def main(platform: str | None = None) -> int:
    """Run the gate. Any unexpected error blocks: a crash must never read as a pass."""
    try:
        return _run(platform)
    except Exception as exc:
        reason = (
            "Hold-out gate hit an internal error and could not verify the candidate "
            f"({type(exc).__name__}: {exc}). Fix the cause or ask the human operator, then retry."
        )
        if is_antigravity_payload(_last_payload, platform):
            return _agy_block_stop(
                reason, continuation_active=bool(_last_payload.get("continuationActive")), status="error"
            )
        emit_block(reason, status="error")


def _run(platform: str | None = None) -> int:
    hook_payload = _read_hook_payload()
    antigravity = is_antigravity_payload(hook_payload, platform)
    continuation_active = bool(hook_payload.get("continuationActive"))
    # Claude Code and Codex set this when the agent is already continuing because a Stop hook blocked.
    stop_hook_active = bool(hook_payload.get("stop_hook_active"))

    if antigravity:
        if hook_payload.get("fullyIdle") is False:
            return _agy_allow_stop()
        reason_code = str(hook_payload.get("terminationReason", "")).strip().lower()
        if reason_code and reason_code not in _NORMAL_STOP_REASONS:
            return _agy_allow_stop()

    def block_env(reason: str, **extra: object) -> int:
        if antigravity:
            return _agy_block_stop(reason, continuation_active=continuation_active, **extra)
        emit_block(reason, **extra)

    target_cwd = hook_cwd(hook_payload) if hook_payload else None
    try:
        root = worktree_root(target_cwd)
    except FactoryError:
        # Outside a Git repository there is no candidate to verify.
        print("factory-core holdout-gate: not a Git repository, nothing to verify.", file=sys.stderr)
        return _agy_allow_stop() if antigravity else EXIT_OK

    try:
        seal = holdout_dir(root)
    except FactoryError as exc:
        return block_env(exc.message)
    if not (seal / compose_runner.COMPOSE_FILE).is_file():
        if seal.exists() or seal.is_symlink():
            return block_env(
                f"Hold-out suite at ~/.factory-holdouts/{seal.name} is missing {compose_runner.COMPOSE_FILE}. "
                "Restore it or rerun `factory-core init`.",
                status="error",
            )
        wt_dir = main_repo_root(root) / WORKTREES_DIR
        has_worktrees = wt_dir.is_dir() and any(p.is_dir() for p in wt_dir.iterdir())
        if has_worktrees or any((root / name).exists() for name in INTERNAL_FILES):
            return block_env(
                "Factory task artifacts are present, but no hold-out suite is initialized for this repository "
                "on this host. Run `factory-core init` (with Docker running), then retry.",
                status="uninitialized",
            )
        # The plugin is enabled for every repository; only ones that ran `factory-core init` are gated.
        print(
            "factory-core holdout-gate: no hold-out suite for this repository, nothing to verify. "
            "Run `factory-core init` to enable the gate.",
            file=sys.stderr,
        )
        return _agy_allow_stop(status="uninitialized") if antigravity else EXIT_OK

    if run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root).returncode == 0:
        root, ambiguity_error = _resolve_gate_root(root, seal)
        if ambiguity_error:
            return block_env(ambiguity_error, status="ambiguous_worktree")

    checks = doctor.run_checks()
    if not all(check.ok for check in checks):
        summary = doctor.failure_summary(checks)
        if any(not check.ok and check.name in ("docker", "compose") for check in checks):
            summary += "\nStart Docker Engine, then retry."
        if stop_hook_active and not antigravity:
            # The agent cannot repair the host. Blocking again would loop until a human interrupts.
            print(
                f"{summary}\nUNVERIFIED: the hold-out gate could not run. Allowing the stop so the "
                "session does not loop; fix the prerequisites and rerun `factory-core holdout-gate`.",
                file=sys.stderr,
            )
            return EXIT_OK
        return block_env(summary, status="error")

    if run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root).returncode != 0:
        return block_env("The repository has no commits yet. Create an initial commit, then retry.")

    git_dir = Path(git("rev-parse", "--absolute-git-dir", cwd=root))
    try:
        with state_manager.exclusive_lock(git_dir / "factory-gate.lock", wait=False):
            return _verify(root, seal, antigravity, block_env)
    except FactoryError as exc:
        return block_env(f"{exc.message} {exc.remediation}".strip(), status="error")


def _verify(root: Path, seal: Path, antigravity: bool, block_env: Callable[..., int]) -> int:
    state = state_manager.restore_or_init(root, _task_id_from_branch(root))
    patch = patch_exporter.export_patch(root)
    tree_hash = patch.tree_hash
    oracle_hash = state_manager.suite_hash(seal)

    if state_manager.is_verified(state, tree_hash, oracle_hash):
        if state.get("last_evaluated_sha") != patch.head_sha:
            state["last_evaluated_sha"] = patch.head_sha
            state_manager.save_state(root, state)
        if antigravity:
            return _agy_allow_stop(status="cached")
        print("Verification cached.")
        return EXIT_OK
    if state_manager.breaker_tripped(state):
        banner = state_manager.breaker_banner(state)
        if antigravity:
            print(banner, file=sys.stderr)
            return _agy_allow_stop(status="circuit_breaker_tripped", reason=banner)
        print(banner)
        return EXIT_OK
    if (
        int(state["attempts"]) == 0
        and not int(state.get("env_failures") or 0)
        and state_manager.is_pristine(root, state, tree_hash)
    ):
        # Nothing has changed since the worktree was created (a planning or question turn).
        if antigravity:
            return _agy_allow_stop(status="no_changes")
        print("No candidate changes yet; nothing to verify.")
        return EXIT_OK

    head_sha = patch.head_sha
    task_id = str(state.get("task_id") or _task_id_from_branch(root))
    started = time.monotonic()
    try:
        result = compose_runner.run_holdout(root, patch, seal, task_id, head_sha[:8])
    except FactoryError as exc:
        # Environment problems are not a rejected approach; do not consume an attempt.
        return block_env(f"Hold-out gate could not run: {exc.message} {exc.remediation}".strip(), status="error")
    duration_ms = int((time.monotonic() - started) * 1000)

    if state_manager.suite_hash(seal) != oracle_hash:
        return block_env("The hold-out suite changed during verification. Retry with the updated suite.", status="error")

    if result.exit_code == 0 and result.verifier_ran:
        if state_manager.compute_tree_hash(root) != tree_hash:
            return block_env("The candidate changed during verification. Retry with the updated candidate.", status="error")
        state_manager.mark_verified(root, state, tree_hash, head_sha, oracle_hash)
        telemetry.record(
            seal, task_id, int(state["attempts"]), duration_ms, "pass", None, tree_hash,
            platform="antigravity" if antigravity else None,
        )
        if antigravity:
            return _agy_allow_stop(status="ok", duration_ms=duration_ms)
        print(f"OK ({duration_ms / 1000:.1f}s)")
        return EXIT_OK

    secrets = report_sanitizer.fixture_secrets(seal / "fixtures")
    sanitized = report_sanitizer.sanitize_output(result.output, seal.name, secrets)

    if not result.verifier_ran:
        # The oracle never ran, so no approach was tested: do not spend an attempt on it.
        env_failures = state_manager.record_env_failure(root, state)
        telemetry.record(
            seal, task_id, int(state["attempts"]), duration_ms, "error", None, tree_hash,
            platform="antigravity" if antigravity else None,
        )
        limit = state_manager.ENV_FAILURE_LIMIT
        env_report = (
            f"HOLD-OUT ENVIRONMENT FAILED ({env_failures}/{limit}, not counted as an attempt, "
            f"{duration_ms / 1000:.1f}s)\n{sanitized}\n"
            "The verifier container did not start: an image failed to build or pull, or the SUT did not pass "
            "its :8080/health check. Make sure the Dockerfile builds, includes curl, and serves HTTP 200 on /health."
        )
        if env_failures >= limit:
            env_report += (
                f"\nUNVERIFIED: the hold-out environment failed {limit} times in a row. "
                "Halting the retry loop and passing control back to the human operator."
            )
            if antigravity:
                print(env_report, file=sys.stderr)
                return _agy_allow_stop(status="environment_failed", reason=env_report)
            print(env_report)
            return EXIT_OK
        if antigravity:
            print(env_report, file=sys.stderr)
            print(json.dumps({
                "decision": "continue",
                "reason": env_report,
                "status": "environment_failed",
                "env_failures": env_failures,
                "env_failure_limit": limit,
            }))
            return EXIT_OK
        emit_block(env_report, status="environment_failed", env_failures=env_failures, env_failure_limit=limit)

    contracts = report_sanitizer.failed_contracts(result.output, secrets)
    failed_contract = contracts[0] if contracts else None
    touched = sorted(set(patch.files) | set(_committed_files_touched(root)))
    attempts = state_manager.record_failure(
        root, state, head_sha, _summary(sanitized, contracts, result.timed_out), touched, failed_contract
    )
    telemetry.record(
        seal, task_id, attempts, duration_ms, "fail", failed_contract, tree_hash,
        platform="antigravity" if antigravity else None,
    )

    header = f"HOLD-OUT GATE FAILED (attempt {attempts}/{state['max_retries']}, {duration_ms / 1000:.1f}s)"
    hint = (
        "Remediation: follow the factory-triage skill. Delegate diagnosis to the adversarial-critic agent, "
        "write .factory-triage.md, then apply a targeted fix. Do not repeat a rejected approach."
    )
    report = f"{header}\n{sanitized}\n{hint}"
    if state_manager.breaker_tripped(state):
        banner = state_manager.breaker_banner(state)
        # The final allowed attempt failed: stop the loop instead of blocking again.
        if antigravity:
            print(f"{report}\n{banner}", file=sys.stderr)
            return _agy_allow_stop(status="circuit_breaker_tripped", reason=f"{report}\n{banner}")
        print(report)
        print(banner)
        return EXIT_OK
    if antigravity:
        print(report, file=sys.stderr)
        print(json.dumps({
            "decision": "continue",
            "reason": report,
            "attempt": attempts,
            "max_retries": state["max_retries"],
            "failed_contracts": contracts,
        }))
        return EXIT_OK
    payload = {
        "decision": "deny",
        "reason": report,
        "attempt": attempts,
        "max_retries": state["max_retries"],
        "failed_contracts": contracts,
    }
    print(report, file=sys.stderr)
    print(json.dumps(payload))
    return EXIT_BLOCK
