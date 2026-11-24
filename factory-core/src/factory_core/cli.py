"""factory-core command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import __version__, doctor, fast_check, gc, holdout_gate, mcp_server, path_guard, status, worktree
from .common import EXIT_BLOCK, EXIT_OK, TRIAGE_FILE, FactoryError, hook_cwd, worktree_root

TRIAGE_SECTIONS = ("Root Cause Analysis", "Unmet Behavioral Contract Clause", "Target Modification Strategy")
CRITIC_AGENT = "adversarial-critic"
PLATFORMS = ("claude", "codex", "antigravity")


def validate_triage_file() -> int:
    """SubagentStop check. Only the adversarial critic is required to leave a triage file."""
    payload: dict = {}
    if not sys.stdin.isatty():
        try:
            parsed = json.loads(sys.stdin.read() or "{}")
            payload = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            payload = {}
        agent = payload.get("agent_type")
        if isinstance(agent, str) and agent and CRITIC_AGENT not in agent:
            return EXIT_OK
    try:
        path = worktree_root(hook_cwd(payload) if payload else None) / TRIAGE_FILE
    except FactoryError as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_OK
    if not path.is_file():
        problem = f"{TRIAGE_FILE} was not written."
    else:
        text = path.read_text(errors="replace")
        missing = [s for s in TRIAGE_SECTIONS if not re.search(rf"(?im)^\s*(#+|[-*]|\d+\.)?\s*\**{re.escape(s)}", text)]
        if not missing:
            print("OK")
            return EXIT_OK
        problem = f"{TRIAGE_FILE} is missing required sections: {', '.join(missing)}."
    reason = f"{problem} Write {TRIAGE_FILE} with sections: {', '.join(TRIAGE_SECTIONS)}."
    print(reason, file=sys.stderr)
    print(json.dumps({"decision": "deny", "reason": reason}))
    return EXIT_BLOCK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory-core", description="Local agentic software factory engine.")
    parser.add_argument("--version", action="version", version=f"factory-core {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="Verify Python, Git, Docker Engine, Compose v2, and host paths.")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("init", help="Initialize the factory for a repository.")
    p.add_argument("--repo", default=None)

    p = sub.add_parser("worktree", help="Manage task worktrees.")
    wt = p.add_subparsers(dest="worktree_command", required=True)
    wt.add_parser("create", help="Create .worktrees/<task-id>.").add_argument("task_id")
    wt_init = wt.add_parser("init-state", help="Initialize .factory-state.json in the current worktree.")
    wt_init.add_argument("--hook", action="store_true",
                         help="Run as a WorktreeCreate hook: create the worktree named on stdin and print its path.")

    p = sub.add_parser("guard", help="PreToolUse path guard (hook payload on stdin).")
    p.add_argument("--platform", choices=PLATFORMS, default=None)

    p = sub.add_parser("fast-check", help="Run fast lint, type, and boundary checks.")
    p.add_argument("--hook", action="store_true", help="Report failures on stderr with exit 2 so the agent sees them.")
    p.add_argument("--post-invocation", action="store_true",
                   help="Antigravity PostInvocation hook: emit a pending failed fast-check as injectSteps.")
    p.add_argument("--platform", choices=PLATFORMS, default=None)

    p = sub.add_parser("holdout-gate", help="Run the sealed hold-out oracle against the candidate patch.")
    p.add_argument("--platform", choices=PLATFORMS, default=None)

    p = sub.add_parser("status", help="Show worktree state and hold-out metrics.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--compact-summary", action="store_true")

    sub.add_parser("validate-triage-file", help="Check .factory-triage.md has the required sections.")
    sub.add_parser("mcp", help="Run the stdio MCP server.")

    p = sub.add_parser("gc", help="Prune stale worktrees, Docker resources, and triage files.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-age-days", type=float, default=gc.DEFAULT_MAX_AGE_DAYS)
    return parser


def dispatch(args: argparse.Namespace) -> int:
    command = args.command
    if command == "doctor":
        return doctor.main(as_json=args.json)
    if command == "init":
        return worktree.init_repo(args.repo)
    if command == "worktree":
        if args.worktree_command == "create":
            print(worktree.create(args.task_id))
            return EXIT_OK
        if args.hook:
            return worktree.hook_create()
        root = worktree.init_state(reset=True)
        print(root)
        return EXIT_OK
    if command == "guard":
        return path_guard.main(platform=args.platform)
    if command == "fast-check":
        return fast_check.main(hook=args.hook, platform=args.platform, post_invocation=args.post_invocation)
    if command == "holdout-gate":
        return holdout_gate.main(platform=args.platform)
    if command == "status":
        return status.main(as_json=args.json, watch=args.watch, compact=args.compact_summary)
    if command == "validate-triage-file":
        return validate_triage_file()
    if command == "mcp":
        return mcp_server.serve()
    if command == "gc":
        return gc.main(dry_run=args.dry_run, max_age_days=args.max_age_days)
    raise FactoryError(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code = dispatch(args)
        sys.stdout.flush()
        return code
    except FactoryError as exc:
        print(f"factory-core: {exc.message}" + (f"\n  Fix: {exc.remediation}" if exc.remediation else ""), file=sys.stderr)
        return 1
    except BrokenPipeError:
        # The reader closed the pipe (for example `| head`). Keep the interpreter quiet at exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
