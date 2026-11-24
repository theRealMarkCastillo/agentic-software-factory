"""Zero-dependency stdio MCP server exposing read-only factory state."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from . import __version__, path_guard, status, telemetry
from .common import PLAN_FILE, WORKTREES_DIR, FactoryError, holdout_dir, main_repo_root, worktree_root
from .worktree import TASK_ID

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_PLAN_CHARS = 20000

_TASK_ARG = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "Optional task id under .worktrees/. Defaults to the active worktree.",
        },
        "repo_path": {
            "type": "string",
            "description": "Optional path to the Git repository or worktree when the MCP server runs outside it.",
        },
    },
    "additionalProperties": False,
}


def _is_plugin_bundle_dir(path: Path) -> bool:
    return (
        (path / "plugin.json").is_file()
        or (path / ".claude-plugin" / "plugin.json").is_file()
        or (path / ".codex-plugin" / "plugin.json").is_file()
    )


def _target(arguments: dict[str, Any]) -> Path:
    """Resolve the worktree to inspect. Protected paths are never reachable."""
    repo_arg = arguments.get("repo_path")
    base: str | Path | None = None
    if repo_arg is not None:
        if not isinstance(repo_arg, str) or not repo_arg.strip():
            raise FactoryError("repo_path must be a non-empty path string.")
        label = path_guard.violation_for(repo_arg, os.getcwd())
        if label:
            raise FactoryError(f"Access to '{repo_arg}' denied: {label}.")
        base = os.path.expanduser(repo_arg)
    else:
        env_repo = os.environ.get("FACTORY_REPO_PATH") or os.environ.get("CLAUDE_PROJECT_DIR")
        if env_repo and env_repo.strip():
            label = path_guard.violation_for(env_repo, os.getcwd())
            if label:
                raise FactoryError(f"Access to '{env_repo}' denied: {label}.")
            base = os.path.expanduser(env_repo)
        else:
            cwd_path = Path.cwd()
            if _is_plugin_bundle_dir(cwd_path):
                raise FactoryError(
                    "MCP server is running inside the plugin directory; pass 'repo_path' pointing to your Git repository or worktree."
                )
    task_id = arguments.get("task_id")
    if task_id is None:
        root = worktree_root(base).resolve()
        if path_guard.violation_for(str(root), os.getcwd()):
            raise FactoryError("Access to the protected repository is denied.")
        return root
    if not isinstance(task_id, str) or not TASK_ID.match(task_id):
        raise FactoryError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]*")
    root = main_repo_root(base).resolve()
    path = root / WORKTREES_DIR / task_id
    if path.resolve() != path or path_guard.violation_for(str(path), str(root)):
        raise FactoryError("Access to the requested worktree is denied.")
    if not path.is_dir():
        raise FactoryError(f"No worktree for task '{task_id}'.")
    return path


def _status(arguments: dict[str, Any]) -> Any:
    return status.collect(_target(arguments))


def _plan(arguments: dict[str, Any]) -> Any:
    path = _target(arguments) / PLAN_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"present": False, "content": None}
    except OSError as exc:
        raise FactoryError("Unable to open the plan as a regular worktree file.") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FactoryError("The plan must be a regular worktree file.")
        with os.fdopen(fd, encoding="utf-8", errors="replace", closefd=False) as stream:
            content = stream.read(MAX_PLAN_CHARS + 1)
    finally:
        os.close(fd)
    return {"present": True, "truncated": len(content) > MAX_PLAN_CHARS, "content": content[:MAX_PLAN_CHARS]}


def _rejected(arguments: dict[str, Any]) -> Any:
    data = status.collect(_target(arguments))
    return {
        "attempts": data.get("attempts"),
        "max_retries": data.get("max_retries"),
        "circuit_breaker_tripped": data.get("circuit_breaker_tripped"),
        "rejected_approaches": data.get("rejected_approaches", []),
    }


def _telemetry(arguments: dict[str, Any]) -> Any:
    return telemetry.summarize(holdout_dir(_target(arguments)))


TOOLS: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]] = {
    "factory_status": (
        "Worktree branch, attempt counter, verification state, and last fast-check result. Pass repo_path if running outside the repo.",
        _status,
    ),
    "factory_plan_read": (
        "Read .factory-plan.md for the current task. Pass repo_path if running outside the repo.",
        _plan,
    ),
    "factory_rejected_approaches": (
        "Approaches already rejected by the hold-out gate. Check before retrying. Pass repo_path if running outside the repo.",
        _rejected,
    ),
    "factory_telemetry_summary": (
        "Aggregate hold-out pass/fail metrics and most frequent failing contract ids. Pass repo_path if running outside the repo.",
        _telemetry,
    ),
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": _TASK_ARG,
            "annotations": {"readOnlyHint": True},
        }
        for name, (description, _) in TOOLS.items()
    ]


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: Any) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Notifications return None."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        request_id = message.get("id") if isinstance(message, dict) else None
        return _error(request_id, -32600, "Invalid Request")
    method, request_id = message["method"], message.get("id")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if "id" not in message:
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "factory-core", "version": __version__},
        })
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return _error(request_id, -32602, f"Unknown tool: {name}")
        raw_args = params.get("arguments")
        arguments: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        try:
            payload = TOOLS[name][1](arguments)
        except FactoryError as exc:
            return _result(request_id, {"content": [{"type": "text", "text": exc.message}], "isError": True})
        except Exception as exc:  # A tool failure must not take the server down.
            return _result(request_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True})
        return _result(request_id, {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": False,
        })
    return _error(request_id, -32601, f"Method not found: {method}")


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Newline-delimited JSON-RPC 2.0 over stdio. Stdout carries protocol messages only."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response: dict[str, Any] | None = _error(None, -32700, "Parse error")
        else:
            response = handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0
