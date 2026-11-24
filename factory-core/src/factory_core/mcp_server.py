"""Zero-dependency stdio MCP server exposing read-only factory state."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__, status, telemetry
from .common import PLAN_FILE, WORKTREES_DIR, FactoryError, holdout_dir, main_repo_root, worktree_root
from .worktree import TASK_ID

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_PLAN_CHARS = 20000

_TASK_ARG = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "Optional task id under .worktrees/. Defaults to the server's working directory.",
        }
    },
    "additionalProperties": False,
}


def _target(arguments: dict[str, Any]) -> Path:
    """Resolve the worktree to inspect. Only .worktrees/<task-id> is reachable."""
    task_id = arguments.get("task_id")
    if task_id is None:
        return worktree_root()
    if not isinstance(task_id, str) or not TASK_ID.match(task_id):
        raise FactoryError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]*")
    path = main_repo_root() / WORKTREES_DIR / task_id
    if not path.is_dir():
        raise FactoryError(f"No worktree for task '{task_id}'.")
    return path


def _status(arguments: dict[str, Any]) -> Any:
    return status.collect(_target(arguments))


def _plan(arguments: dict[str, Any]) -> Any:
    path = _target(arguments) / PLAN_FILE
    if not path.is_file():
        return {"present": False, "content": None}
    content = path.read_text(errors="replace")
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
    "factory_status": ("Worktree branch, attempt counter, verification state, and last fast-check result.", _status),
    "factory_plan_read": ("Read .factory-plan.md for the current task.", _plan),
    "factory_rejected_approaches": ("Approaches already rejected by the hold-out gate. Check before retrying.", _rejected),
    "factory_telemetry_summary": ("Aggregate hold-out pass/fail metrics and most frequent failing contract ids.", _telemetry),
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
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
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
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
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
