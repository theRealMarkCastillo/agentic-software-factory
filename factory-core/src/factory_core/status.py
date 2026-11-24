"""Deterministic status view shared by every harness."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from . import fast_check, state_manager, telemetry
from .common import PLAN_FILE, TRIAGE_FILE, FactoryError, holdout_dir, run, worktree_root


def collect(path: str | Path | None = None) -> dict[str, Any]:
    """Gather status. Never raises for an uninitialized worktree."""
    try:
        root = worktree_root(path)
    except FactoryError as exc:
        return {"initialized": False, "error": exc.message}

    branch = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=root).stdout.decode().strip()
    data: dict[str, Any] = {
        "initialized": False,
        "worktree": str(root),
        "branch": branch or "(detached)",
        "plan_present": (root / PLAN_FILE).is_file(),
        "triage_present": (root / TRIAGE_FILE).is_file(),
        "last_fast_check": fast_check.load_result(root),
    }
    try:
        state = state_manager.load_state(root)
    except FactoryError as exc:
        data["error"] = exc.message
        state = None

    if state is not None:
        attempts, limit = int(state["attempts"]), int(state["max_retries"])
        try:
            current = state_manager.compute_tree_hash(root)
        except FactoryError:
            current = None
        if state.get("verified_tree_hash") is None:
            verification = "unverified"
        elif state["verified_tree_hash"] == current:
            verification = "verified"
        else:
            verification = "stale (tree changed since last pass)"
        data.update({
            "initialized": True,
            "task_id": state.get("task_id"),
            "attempts": attempts,
            "max_retries": limit,
            "circuit_breaker_tripped": attempts >= limit,
            "verification": verification,
            "verified_tree_hash": state.get("verified_tree_hash"),
            "current_tree_hash": current,
            "rejected_approaches": state.get("rejected_approaches", []),
            "updated_at": state.get("updated_at"),
        })
    try:
        data["telemetry"] = telemetry.summarize(holdout_dir(root))
    except (FactoryError, OSError):
        data["telemetry"] = None
    return data


def _rejected_lines(rejected: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in rejected:
        files = ", ".join(item.get("files_touched") or []) or "none"
        lines.append(f"  #{item.get('attempt')} [{item.get('failed_contract') or 'no contract id'}] {item.get('summary')}")
        lines.append(f"     files: {files}")
    return lines


def render(data: dict[str, Any]) -> str:
    if "worktree" not in data:
        return f"factory status: {data.get('error', 'unavailable')}"
    lines = [f"Worktree:      {data['worktree']}", f"Branch:        {data['branch']}"]
    if not data["initialized"]:
        lines.append(f"State:         {data.get('error') or 'no .factory-state.json (run: factory-core worktree init-state)'}")
    else:
        breaker = "  ** CIRCUIT BREAKER TRIPPED **" if data["circuit_breaker_tripped"] else ""
        lines += [
            f"Task:          {data['task_id']}",
            f"Attempts:      {data['attempts']}/{data['max_retries']}{breaker}",
            f"Verification:  {data['verification']}",
        ]
    check = data.get("last_fast_check")
    lines.append(f"Fast-check:    {check['result']} ({check['duration_s']}s at {check['checked_at']})" if check else "Fast-check:    not run")
    lines.append(f"Plan/Triage:   plan {'present' if data['plan_present'] else 'missing'}, "
                 f"triage {'present' if data['triage_present'] else 'absent'}")
    rejected = data.get("rejected_approaches") or []
    lines.append(f"Rejected approaches: {len(rejected)}")
    lines += _rejected_lines(rejected)
    tel = data.get("telemetry")
    if tel and tel["runs"]:
        lines.append(f"Hold-out runs: {tel['runs']} ({tel['passed']} pass / {tel['failed']} fail, "
                     f"pass rate {tel['pass_rate']:.0%}, avg {tel['avg_duration_ms']}ms)")
        for contract, count in tel["top_failed_contracts"]:
            lines.append(f"  {count}x {contract}")
    else:
        lines.append("Hold-out runs: none recorded")
    return "\n".join(lines)


def render_compact(data: dict[str, Any]) -> str:
    """State recap for re-injection after context compaction. Empty when there is no state."""
    if not data.get("initialized"):
        return ""
    lines = [
        (
            f"[factory state] task={data['task_id']} branch={data['branch']} "
            f"attempts={data['attempts']}/{data['max_retries']} verification={data['verification']}"
        ),
    ]
    if data["circuit_breaker_tripped"]:
        lines.append("Circuit breaker tripped: stop retrying and hand control back to the human operator.")
    rejected = data.get("rejected_approaches") or []
    if rejected:
        lines.append("Rejected approaches (do not repeat):")
        lines += _rejected_lines(rejected)
    lines.append("Reconcile with `git status` and .factory-plan.md before making further changes.")
    return "\n".join(lines)


def main(as_json: bool = False, watch: bool = False, compact: bool = False, interval: float = 2.0) -> int:
    def output() -> str:
        data = collect()
        if compact:
            return render_compact(data)
        return json.dumps(data, indent=2) if as_json else render(data)

    if not watch:
        text = output()
        if text:
            print(text)
        return 0
    try:
        while True:
            text = output()
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")
            print(text)
            print(f"\n(refreshing every {interval:g}s, Ctrl-C to exit)", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
