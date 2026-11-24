import io
import json
import os
import subprocess
import sys

import pytest

from factory_core import mcp_server, state_manager as sm, telemetry, worktree

from conftest import REPO_ROOT

TOOL_NAMES = {"factory_status", "factory_plan_read", "factory_rejected_approaches", "factory_telemetry_summary"}


def rpc(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(name, arguments=None):
    response = mcp_server.handle(rpc("tools/call", {"name": name, "arguments": arguments or {}}))
    result = response["result"]
    return result, json.loads(result["content"][0]["text"]) if not result["isError"] else result["content"][0]["text"]


def test_initialize_negotiates_protocol_and_advertises_tools():
    result = mcp_server.handle(rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}))["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "factory-core"
    assert "tools" in result["capabilities"]


def test_initialize_falls_back_for_unknown_protocol():
    result = mcp_server.handle(rpc("initialize", {"protocolVersion": "1999-01-01"}))["result"]
    assert result["protocolVersion"] == mcp_server.SUPPORTED_PROTOCOLS[0]


def test_notifications_get_no_response():
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_exactly_the_read_only_tools():
    tools = mcp_server.handle(rpc("tools/list"))["result"]["tools"]
    assert {t["name"] for t in tools} == TOOL_NAMES
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["description"]


def test_ping():
    assert mcp_server.handle(rpc("ping"))["result"] == {}


def test_unknown_method_and_tool_errors():
    assert mcp_server.handle(rpc("resources/list"))["error"]["code"] == -32601
    assert mcp_server.handle(rpc("tools/call", {"name": "factory_delete_everything"}))["error"]["code"] == -32602
    assert mcp_server.handle({"id": 1, "method": "ping"})["error"]["code"] == -32600
    assert mcp_server.handle("nonsense")["error"]["code"] == -32600


def test_factory_status_reports_state(repo):
    state = sm.init_state(repo, "task-9")
    sm.record_failure(repo, state, "sha", "assert 500 == 200", ["src/app.py"], "t.py::test_x")
    result, data = call("factory_status")
    assert result["isError"] is False
    assert data["task_id"] == "task-9" and data["attempts"] == 1 and data["max_retries"] == 3
    assert data["verification"] == "unverified" and data["branch"] == "main"


def test_factory_plan_read(repo):
    _, data = call("factory_plan_read")
    assert data == {"present": False, "content": None}
    (repo / ".factory-plan.md").write_text("# Plan\n1. Problem Statement\n")
    _, data = call("factory_plan_read")
    assert data["present"] is True and data["content"].startswith("# Plan") and data["truncated"] is False


def test_factory_rejected_approaches(repo):
    state = sm.init_state(repo, "t")
    for index in range(3):
        sm.record_failure(repo, state, "sha", f"failure {index}", ["a.py"], "t.py::test_x")
    _, data = call("factory_rejected_approaches")
    assert data["attempts"] == 3 and data["circuit_breaker_tripped"] is True
    assert [r["summary"] for r in data["rejected_approaches"]] == ["failure 0", "failure 1", "failure 2"]


def test_factory_telemetry_summary(repo, initialized):
    telemetry.record(initialized, "t", 0, 1000, "pass", None, "h1")
    telemetry.record(initialized, "t", 1, 3000, "fail", "t.py::test_x", "h2")
    telemetry.record(initialized, "t", 2, 2000, "fail", "t.py::test_x", "h3")
    _, data = call("factory_telemetry_summary")
    assert (data["runs"], data["passed"], data["failed"]) == (3, 1, 2)
    assert data["pass_rate"] == 0.333 and data["avg_duration_ms"] == 2000 and data["first_pass_count"] == 1
    assert data["top_failed_contracts"] == [["t.py::test_x", 2]]


def test_task_id_argument_selects_a_worktree(repo):
    path = worktree.create("task-b")
    (path / ".factory-plan.md").write_text("worktree plan")
    _, data = call("factory_plan_read", {"task_id": "task-b"})
    assert data["content"] == "worktree plan"
    _, status = call("factory_status", {"task_id": "task-b"})
    assert status["task_id"] == "task-b" and status["branch"] == "factory/task-b"


def test_repo_path_argument_works_when_mcp_server_runs_from_plugin_dir(repo, home, tmp_path, monkeypatch):
    seal = home / ".factory-holdouts" / "demo-repo"
    seal.mkdir(parents=True)
    path = worktree.create("task-ext")
    (path / ".factory-plan.md").write_text("external plan")
    plugin_root = tmp_path / "plugin-root"
    plugin_root.mkdir()
    monkeypatch.chdir(plugin_root)

    _, data = call("factory_plan_read", {"repo_path": str(repo), "task_id": "task-ext"})
    assert data["content"] == "external plan"
    denied_res, text = call("factory_plan_read", {"repo_path": str(seal)})
    assert denied_res["isError"] is True and "hold-out" in text


@pytest.mark.parametrize("task_id", ["../../etc", "/etc/passwd", "a/b", "", 7, "~/.factory-holdouts"])
def test_task_id_cannot_escape_the_worktrees_directory(repo, task_id):
    result, text = call("factory_plan_read", {"task_id": task_id})
    assert result["isError"] is True


def test_tool_failure_is_reported_not_raised(tmp_path, home, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, text = call("factory_plan_read")
    assert result["isError"] is True and "not inside a Git repository" in text


def test_responses_never_expose_holdout_paths(repo, initialized):
    telemetry.record(initialized, "t", 1, 10, "fail", "t.py::test_x", "h")
    for name in TOOL_NAMES:
        text = json.dumps(mcp_server.handle(rpc("tools/call", {"name": name, "arguments": {}})))
        assert ".factory-holdouts" not in text


def test_serve_handles_parse_errors_and_blank_lines():
    stdin = io.StringIO('\n{bad json\n' + json.dumps(rpc("ping", request_id=7)) + "\n")
    stdout = io.StringIO()
    assert mcp_server.serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_stdio_session_end_to_end(repo):
    sm.init_state(repo, "stdio-task")
    requests = [
        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}, 1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc("tools/list", request_id=2),
        rpc("tools/call", {"name": "factory_status", "arguments": {}}, 3),
    ]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "factory-core" / "src")}
    proc = subprocess.run(
        [sys.executable, "-m", "factory_core.cli", "mcp"], cwd=repo, env=env, timeout=30,
        input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True,
    )
    assert proc.returncode == 0
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert all(r["jsonrpc"] == "2.0" for r in responses)
    assert {t["name"] for t in responses[1]["result"]["tools"]} == TOOL_NAMES
    assert json.loads(responses[2]["result"]["content"][0]["text"])["task_id"] == "stdio-task"
