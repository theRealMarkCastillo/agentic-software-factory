import json
import os

import pytest
from conftest import git, run_cli

from factory_core import compose_runner, doctor, holdout_gate, telemetry
from factory_core import state_manager as sm

FAIL_LOG = """\
verifier-1  | /holdout/suites/test_contract_example.py:34: in test_health_endpoint_reports_ok
verifier-1  |     assert response.json() == expected["body"]
verifier-1  | E   AssertionError: assert {'status': 'down', 'key': 's3cr3t-fixture-value'} == {'status': 'ok'}
verifier-1  | FAILED ../holdout/suites/test_contract_example.py::test_health_endpoint_reports_ok - AssertionError
"""


class FakeOracle:
    def __init__(self, exit_code=0, output=""):
        self.exit_code, self.output, self.calls = exit_code, output, []

    def __call__(self, repo, patch, holdout_dir, task_id, short_sha, timeout=None):
        self.calls.append({"patch": patch, "task_id": task_id, "short_sha": short_sha, "holdout_dir": holdout_dir})
        return compose_runner.GateRun(self.exit_code, self.output, 1234, "holdout-x-abc")


@pytest.fixture
def oracle(monkeypatch):
    fake = FakeOracle()
    monkeypatch.setattr(compose_runner, "run_holdout", fake)
    return fake


def run_gate(capsys):
    try:
        code = holdout_gate.main()
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_pass_records_hash_and_telemetry(repo, initialized, healthy, oracle, capsys):
    (repo / "src" / "new.py").write_text("X = 1\n")
    code, out, err = run_gate(capsys)
    assert code == 0
    assert out.startswith("OK (") and out.strip().endswith("s)") and out.count("\n") == 1
    assert err == ""
    state = sm.load_state(repo)
    assert state["verified_tree_hash"] == sm.compute_tree_hash(repo)
    assert state["attempts"] == 0
    assert b"src/new.py" in oracle.calls[0]["patch"]
    events = telemetry.read_events(initialized)
    assert len(events) == 1 and events[0]["gate_result"] == "pass" and events[0]["failed_contract_id"] is None


def test_unchanged_tree_uses_cache_and_skips_the_oracle(repo, initialized, healthy, oracle, capsys):
    run_gate(capsys)
    code, out, _ = run_gate(capsys)
    assert (code, out.strip()) == (0, "Verification cached.")
    assert len(oracle.calls) == 1
    (repo / "src" / "app.py").write_text("VALUE = 9\n")
    run_gate(capsys)
    assert len(oracle.calls) == 2


def test_failure_blocks_on_both_streams_and_persists_rejection(repo, initialized, healthy, oracle, capsys):
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    (repo / "src" / "new.py").write_text("X = 1\n")
    code, out, err = run_gate(capsys)
    assert code == 2
    payload = json.loads(out)
    assert payload["decision"] == "deny" and payload["attempt"] == 1 and payload["max_retries"] == 3
    assert payload["failed_contracts"] == ["test_contract_example.py::test_health_endpoint_reports_ok"]
    assert payload["reason"] == err.strip()
    assert "HOLD-OUT GATE FAILED (attempt 1/3" in err and "factory-triage" in err
    state = sm.load_state(repo)
    assert state["attempts"] == 1 and state["verified_tree_hash"] is None
    entry = state["rejected_approaches"][0]
    assert entry["attempt"] == 1
    assert entry["files_touched"] == ["src/new.py"]
    assert entry["failed_contract"] == "test_contract_example.py::test_health_endpoint_reports_ok"
    assert entry["summary"].startswith("AssertionError")
    event = telemetry.read_events(initialized)[0]
    assert set(event) >= {"task_id", "platform", "attempt", "duration_ms", "gate_result", "failed_contract_id", "tree_hash"}
    assert event["gate_result"] == "fail" and event["attempt"] == 1


def test_failure_output_is_sanitized(repo, initialized, healthy, oracle, capsys):
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    code, out, err = run_gate(capsys)
    combined = out + err + (repo / ".factory-state.json").read_text()
    assert "s3cr3t-fixture-value" not in combined
    assert 'expected["body"]' not in combined
    assert "/holdout/suites" not in combined and ".py:34" not in combined
    assert "[REDACTED_SECRET]" in err


def test_third_failure_trips_breaker_and_exits_0(repo, initialized, healthy, oracle, capsys):
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    codes = [run_gate(capsys) for _ in range(3)]
    assert [c[0] for c in codes] == [2, 2, 0]
    final_out = codes[2][1]
    assert "CIRCUIT BREAKER TRIPPED: Maximum verification attempts (3) reached." in final_out
    assert "HOLD-OUT GATE FAILED (attempt 3/3" in final_out
    assert sm.load_state(repo)["attempts"] == 3

    code, out, _ = run_gate(capsys)  # A fourth invocation must not run the oracle again.
    assert code == 0 and "CIRCUIT BREAKER TRIPPED" in out
    assert len(oracle.calls) == 3
    assert sm.load_state(repo)["attempts"] == 3


def test_uninitialized_repo_is_not_gated(repo, home, oracle, monkeypatch, capsys):
    # The plugin is enabled everywhere; a repository that never ran `factory-core init` must not block,
    # and must not even need Docker.
    monkeypatch.setattr(doctor, "run_checks", lambda: [doctor.Check("docker", False, "down", "Start Docker")])
    code, out, err = run_gate(capsys)
    assert code == 0 and out == ""
    assert "factory-core init" in err
    assert oracle.calls == [] and sm.load_state(repo) is None


def test_docker_down_fails_closed_before_anything_else(repo, initialized, oracle, monkeypatch, capsys):
    checks = [doctor.Check("docker", False, "Docker daemon is not responding", "Start Docker Engine")]
    monkeypatch.setattr(doctor, "run_checks", lambda: checks)
    code, out, err = run_gate(capsys)
    assert code == 2 and "Start Docker Engine" in err
    assert json.loads(out)["decision"] == "deny"
    assert oracle.calls == [] and sm.load_state(repo) is None


def test_environment_error_does_not_consume_an_attempt(repo, initialized, healthy, monkeypatch, capsys):
    from factory_core.common import FactoryError

    def broken(*args, **kwargs):
        raise FactoryError("The repository has no Dockerfile at its root.", "Add a Dockerfile.")
    monkeypatch.setattr(compose_runner, "run_holdout", broken)
    code, out, err = run_gate(capsys)
    assert code == 2 and "no Dockerfile" in err
    assert sm.load_state(repo)["attempts"] == 0


def test_corrupt_state_fails_closed(repo, initialized, healthy, oracle, capsys):
    (repo / ".factory-state.json").write_text("{broken")
    code, _, err = run_gate(capsys)
    assert code == 2 and "corrupt" in err and oracle.calls == []


def test_task_id_and_holdout_dir_come_from_the_main_repo(repo, initialized, healthy, oracle, capsys, monkeypatch):
    from factory_core import worktree
    path = worktree.create("task-a")
    monkeypatch.chdir(path)
    (path / "src" / "new.py").write_text("X = 1\n")
    capsys.readouterr()
    code, _, _ = run_gate(capsys)
    assert code == 0
    assert oracle.calls[0]["task_id"] == "task-a"
    assert oracle.calls[0]["holdout_dir"] == initialized
    assert sm.load_state(path)["verified_tree_hash"] is not None
    assert sm.load_state(repo) is None


def test_outside_a_git_repository_is_a_no_op(tmp_path, home):
    result = run_cli(["holdout-gate"], cwd=tmp_path)
    assert result.returncode == 0 and "not a Git repository" in result.stderr


def test_antigravity_stop_hook_contract_and_workspace_resolution(repo, initialized, healthy, oracle, monkeypatch, capsys, tmp_path):
    import io

    plugin_root = tmp_path / "plugin-root"
    plugin_root.mkdir()
    monkeypatch.chdir(plugin_root)
    (repo / "src" / "new.py").write_text("X = 1\n")

    def run_agy_stop(payload, exit_code=0, output=""):
        oracle.exit_code, oracle.output = exit_code, output
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        code = holdout_gate.main(platform="antigravity")
        captured = capsys.readouterr()
        return code, json.loads(captured.out), captured.err

    # 1. Contract failure -> exit 0 with {"decision": "continue"} so Antigravity re-enters the loop
    code, body, err = run_agy_stop(
        {"executionNum": 1, "terminationReason": "model_stop", "fullyIdle": True, "workspacePaths": [str(repo)]},
        exit_code=1, output=FAIL_LOG,
    )
    assert code == 0
    assert body["decision"] == "continue" and body["attempt"] == 1
    assert "HOLD-OUT GATE FAILED" in body["reason"] and "s3cr3t-fixture-value" not in body["reason"]
    assert sm.load_state(repo)["attempts"] == 1

    # 2. User cancellation -> allow stop without running oracle
    calls_before = len(oracle.calls)
    code, body, _ = run_agy_stop(
        {"executionNum": 2, "terminationReason": "USER_CANCELED", "fullyIdle": True, "workspacePaths": [str(repo)]},
        exit_code=1, output=FAIL_LOG,
    )
    assert code == 0 and body == {"decision": "stop"} and len(oracle.calls) == calls_before

    # 3. Passing fix -> exit 0 with {"decision": "stop", "status": "ok"}
    code, body, _ = run_agy_stop(
        {"executionNum": 3, "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "workspacePaths": [str(repo)]},
        exit_code=0, output="",
    )
    assert code == 0 and body["decision"] == "stop" and body["status"] == "ok"
    assert sm.load_state(repo)["verified_tree_hash"] is not None
    assert telemetry.read_events(initialized)[-1]["platform"] == "antigravity"


def test_missing_sut_dockerfile_is_reported(repo, initialized, monkeypatch):
    (repo / "Dockerfile").unlink()
    from factory_core.common import FactoryError, runtime_tmp_dir
    from factory_core.patch_exporter import export_patch
    with pytest.raises(FactoryError) as excinfo:
        compose_runner.run_holdout(repo, export_patch(repo).data, initialized, "t", "abc1234")
    assert "no Dockerfile" in excinfo.value.message
    tmp_dir = runtime_tmp_dir()
    assert not [n for n in os.listdir(tmp_dir) if n.startswith("sut-") and (repo.name in n)]


def test_gate_from_clean_main_resolves_single_unverified_worktree(repo, initialized, healthy, oracle, capsys):
    from factory_core import worktree
    wt = worktree.create("task-wt")
    (wt / "src" / "new.py").write_text("X = 1\n")
    code, out, _ = run_gate(capsys)
    assert code == 0 and out.startswith("OK (")
    assert oracle.calls[0]["task_id"] == "task-wt"
    assert sm.load_state(wt)["verified_tree_hash"] == sm.compute_tree_hash(wt)
    assert sm.load_state(repo) is None


def test_committed_worktree_failure_records_branch_files_touched(repo, initialized, healthy, oracle, capsys, monkeypatch):
    from factory_core import worktree
    wt = worktree.create("committed")
    (wt / "src" / "feature.py").write_text("X = 1\n")
    git(wt, "add", "src/feature.py")
    git(wt, "commit", "-q", "-m", "commit feature")
    monkeypatch.chdir(wt)
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    code, _, _ = run_gate(capsys)
    assert code == 2
    state = sm.load_state(wt)
    assert state["rejected_approaches"][0]["files_touched"] == ["src/feature.py"]


def gate_with_payload(monkeypatch, capsys, payload):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return run_gate(capsys)


def test_docker_down_blocks_once_then_releases_on_stop_hook_active(repo, initialized, oracle, monkeypatch, capsys):
    checks = [doctor.Check("docker", False, "Docker daemon is not responding", "Start Docker Engine")]
    monkeypatch.setattr(doctor, "run_checks", lambda: checks)
    code, _, _ = gate_with_payload(monkeypatch, capsys, {"cwd": str(repo), "stop_hook_active": False})
    assert code == 2
    code, out, err = gate_with_payload(monkeypatch, capsys, {"cwd": str(repo), "stop_hook_active": True})
    assert code == 0 and out == "" and "UNVERIFIED" in err
    assert oracle.calls == []


def test_oracle_failure_still_blocks_when_stop_hook_active(repo, initialized, healthy, oracle, monkeypatch, capsys):
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    code, _, _ = gate_with_payload(monkeypatch, capsys, {"cwd": str(repo), "stop_hook_active": True})
    assert code == 2 and sm.load_state(repo)["attempts"] == 1


def test_pristine_worktree_is_not_gated_until_it_changes(repo, initialized, healthy, oracle, capsys, monkeypatch):
    from factory_core import worktree
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    path = worktree.create("task-plan")
    monkeypatch.chdir(path)
    (path / ".factory-plan.md").write_text("# Plan\n")  # Planning writes only bookkeeping.
    capsys.readouterr()
    for _ in range(4):
        code, out, _ = run_gate(capsys)
        assert code == 0 and "nothing to verify" in out
    assert oracle.calls == [] and sm.load_state(path)["attempts"] == 0

    (path / "src" / "app.py").write_text("VALUE = 2\n")
    assert run_gate(capsys)[0] == 2
    assert len(oracle.calls) == 1 and sm.load_state(path)["attempts"] == 1

    # Committing the work still counts as a change from the baseline.
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "work")
    assert run_gate(capsys)[0] == 2 and len(oracle.calls) == 2


def test_environment_failure_is_not_an_attempt_and_stops_after_the_limit(repo, initialized, healthy, monkeypatch, capsys):
    calls = []

    def never_started(repo_, patch, holdout_dir, task_id, short_sha, timeout=None):
        calls.append(1)
        return compose_runner.GateRun(1, "sut-1  | dependency failed to start", 900, "holdout-x", verifier_ran=False)
    monkeypatch.setattr(compose_runner, "run_holdout", never_started)

    results = [run_gate(capsys) for _ in range(3)]
    assert [r[0] for r in results] == [2, 2, 0]
    assert "HOLD-OUT ENVIRONMENT FAILED (1/3, not counted as an attempt" in results[0][2]
    assert "UNVERIFIED" in results[2][1]
    state = sm.load_state(repo)
    assert state["attempts"] == 0 and state["rejected_approaches"] == [] and state["env_failures"] == 3
    assert [e["gate_result"] for e in telemetry.read_events(initialized)] == ["error"] * 3

    # A run where the verifier does start clears the streak.
    monkeypatch.setattr(compose_runner, "run_holdout", FakeOracle(1, FAIL_LOG))
    assert run_gate(capsys)[0] == 2
    state = sm.load_state(repo)
    assert state["attempts"] == 1 and state["env_failures"] == 0


def test_unexpected_exception_blocks_instead_of_failing_open(repo, initialized, healthy, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(compose_runner, "run_holdout", boom)
    code, out, err = run_gate(capsys)
    assert code == 2 and "internal error" in err and "disk on fire" in err
    assert json.loads(out)["decision"] == "deny"

    monkeypatch.setenv("FACTORY_PLATFORM", "antigravity")
    code, out, _ = run_gate(capsys)
    assert code == 0 and json.loads(out)["decision"] == "continue"


def test_unreadable_file_blocks_with_a_remediation(repo, initialized, healthy, oracle, capsys):
    secret = repo / "locked.txt"
    secret.write_text("x")
    secret.chmod(0)
    try:
        code, _, err = run_gate(capsys)
    finally:
        secret.chmod(0o600)
    assert code == 2 and "Cannot read locked.txt" in err and oracle.calls == []


def test_deleting_the_state_file_does_not_reset_the_breaker(repo, initialized, healthy, oracle, capsys):
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    run_gate(capsys)
    run_gate(capsys)
    (repo / ".factory-state.json").unlink()
    code, out, _ = run_gate(capsys)
    assert code == 0 and "CIRCUIT BREAKER TRIPPED" in out and "attempt 3/3" in out
    state = sm.load_state(repo)
    assert state["attempts"] == 3 and len(state["rejected_approaches"]) == 3


def test_human_reset_clears_the_mirrored_attempts(repo, initialized, healthy, oracle, capsys):
    oracle.exit_code, oracle.output = 1, FAIL_LOG
    run_gate(capsys)
    (repo / ".factory-state.json").unlink()
    assert run_cli(["worktree", "init-state"], cwd=repo).returncode == 0
    assert sm.load_state(repo)["attempts"] == 0
    run_gate(capsys)
    assert sm.load_state(repo)["attempts"] == 1
