"""The sealed hold-out gate: cache, circuit breaker, oracle run, sanitized feedback."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import compose_runner, doctor, patch_exporter, report_sanitizer, state_manager, telemetry
from .common import (
    EXIT_BLOCK, EXIT_OK, FactoryError, emit_block,
    git, holdout_dir, hook_cwd, is_antigravity_payload, run, worktree_root,
)
from .worktree import _task_id_from_branch

SUMMARY_LIMIT = 300
_NORMAL_STOP_REASONS = {"", "model_stop", "no_tool_call"}


def _read_hook_payload() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, OSError):
        return {}


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


def main(platform: str | None = None) -> int:
    hook_payload = _read_hook_payload()
    antigravity = is_antigravity_payload(hook_payload, platform)
    continuation_active = bool(hook_payload.get("continuationActive"))

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

    checks = doctor.run_checks()
    if not all(check.ok for check in checks):
        return block_env(doctor.failure_summary(checks) + "\nStart Docker Engine, then retry.", status="error")

    try:
        seal = holdout_dir(root)
    except FactoryError as exc:
        return block_env(exc.message)
    if not (seal / compose_runner.COMPOSE_FILE).is_file():
        return block_env(
            "Hold-out gate is not initialized for this repository. "
            "Run `factory-core init` in the repository (with Docker running), then retry.",
            status="uninitialized",
        )
    if run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root).returncode != 0:
        return block_env("The repository has no commits yet. Create an initial commit, then retry.")

    try:
        state = state_manager.load_state(root) or state_manager.init_state(root, _task_id_from_branch(root))
        tree_hash = state_manager.compute_tree_hash(root)
    except FactoryError as exc:
        return block_env(f"{exc.message} {exc.remediation}".strip())

    if state_manager.is_verified(state, tree_hash):
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

    head_sha = git("rev-parse", "HEAD", cwd=root)
    task_id = str(state.get("task_id") or _task_id_from_branch(root))
    started = time.monotonic()
    try:
        patch = patch_exporter.export_patch(root)
        result = compose_runner.run_holdout(root, patch.data, seal, task_id, head_sha[:8])
    except FactoryError as exc:
        # Environment problems are not a rejected approach; do not consume an attempt.
        return block_env(f"Hold-out gate could not run: {exc.message} {exc.remediation}".strip(), status="error")
    duration_ms = int((time.monotonic() - started) * 1000)

    if result.exit_code == 0:
        state_manager.mark_verified(root, state, tree_hash, head_sha)
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
    contracts = report_sanitizer.failed_contracts(result.output)
    failed_contract = contracts[0] if contracts else None
    attempts = state_manager.record_failure(
        root, state, head_sha, _summary(sanitized, contracts, result.timed_out), patch.files, failed_contract
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
