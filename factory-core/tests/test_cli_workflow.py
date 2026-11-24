"""init, worktree, fast-check, status, telemetry, and triage validation."""

import json

import pytest
from conftest import git, run_cli

from factory_core import fast_check, status, telemetry, worktree
from factory_core import state_manager as sm
from factory_core.common import FactoryError

TRIAGE = "# Triage\n\n## Root Cause Analysis\nx\n\n## Unmet Behavioral Contract Clause\ny\n\n## Target Modification Strategy\nz\n"


# --- init -----------------------------------------------------------------------

def test_init_populates_holdouts_excludes_and_agents_md(repo, home, healthy, capsys):
    assert worktree.init_repo() == 0
    seal = home / ".factory-holdouts" / "demo-repo"
    for name in ("docker-compose.holdout.yml", "Dockerfile.verifier", "telemetry.jsonl",
                 "suites/test_contract_example.py", "fixtures/golden_example.json"):
        assert (seal / name).is_file(), name
    assert seal.stat().st_mode & 0o777 == 0o700
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".worktrees/" in exclude and ".factory-state.json" in exclude
    assert (repo / "AGENTS.md").read_text().startswith("# Agent Instructions")
    assert ".factory-holdouts" not in capsys.readouterr().out.replace("~/.factory-holdouts", "")


def test_init_is_idempotent_and_preserves_existing_files(repo, home, healthy):
    (repo / "AGENTS.md").write_text("custom rules\n")
    worktree.init_repo()
    suite = home / ".factory-holdouts" / "demo-repo" / "suites" / "test_real.py"
    suite.write_text("def test_real(): pass\n")
    worktree.init_repo()
    assert (repo / "AGENTS.md").read_text() == "custom rules\n"
    assert suite.exists()
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".worktrees/") == 1 and exclude.count(".factory-state.json") == 1


def test_init_fails_closed_when_docker_is_down(repo, home):
    result = run_cli(["init"], cwd=repo, env={"DOCKER_HOST": "unix:///nonexistent.sock"})
    assert result.returncode == 2
    assert not (home / ".factory-holdouts" / "demo-repo").exists()
    assert not (repo / "AGENTS.md").exists()


def test_init_from_a_worktree_uses_the_main_repo_slug(repo, home, healthy, monkeypatch):
    path = worktree.create("t")
    monkeypatch.chdir(path)
    worktree.init_repo()
    assert (home / ".factory-holdouts" / "demo-repo").is_dir()
    assert not (home / ".factory-holdouts" / "t").exists()


# --- worktree -------------------------------------------------------------------

def test_worktree_create_leaves_main_clean_and_prints_only_the_path(repo):
    result = run_cli(["worktree", "create", "task-verification-01"], cwd=repo)
    assert result.returncode == 0
    path = repo / ".worktrees" / "task-verification-01"
    assert result.stdout.strip() == str(path)
    assert path.is_dir()
    assert git(repo, "status", "--porcelain") == ""
    assert git(path, "status", "--porcelain") == ""
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "factory/task-verification-01"
    state = json.loads((path / ".factory-state.json").read_text())
    assert state["task_id"] == "task-verification-01" and state["attempts"] == 0


@pytest.mark.parametrize("task_id", ["../escape", "a b", "-flag", "", "a/b", "x" * 65])
def test_worktree_create_rejects_unsafe_ids(repo, task_id):
    with pytest.raises(FactoryError):
        worktree.create(task_id)
    assert not (repo / ".worktrees").exists() or not any((repo / ".worktrees").iterdir())


def test_worktree_create_refuses_duplicates(repo):
    worktree.create("dup")
    with pytest.raises(FactoryError):
        worktree.create("dup")


def test_init_state_in_existing_worktree_derives_task_id_from_branch(repo):
    path = repo / ".worktrees" / "native"
    git(repo, "worktree", "add", "-q", "-b", "factory/native", str(path), "HEAD")
    result = run_cli(["worktree", "init-state"], cwd=path)
    assert result.returncode == 0
    assert sm.load_state(path)["task_id"] == "native"


def test_worktree_create_hook_creates_and_prints_path_last(repo):
    result = run_cli(["worktree", "init-state", "--hook"], cwd=repo,
                     stdin=json.dumps({"hook_event_name": "WorktreeCreate", "name": "bold-oak-a3f2"}))
    assert result.returncode == 0
    path = repo / ".worktrees" / "bold-oak-a3f2"
    assert result.stdout.strip().splitlines()[-1] == str(path)
    assert sm.load_state(path)["task_id"] == "bold-oak-a3f2"
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("payload", ["", "{", "[]", "{}", '{"name": 42}', '{"name": " "}'])
def test_worktree_create_hook_requires_a_name_and_leaves_main_untouched(repo, payload):
    result = run_cli(["worktree", "init-state", "--hook"], cwd=repo, stdin=payload)
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not (repo / ".factory-state.json").exists()
    assert not (repo / ".worktrees").exists()


def test_worktree_create_hook_failure_is_non_zero(repo):
    worktree.create("taken")
    result = run_cli(["worktree", "init-state", "--hook"], cwd=repo, stdin=json.dumps({"name": "taken"}))
    assert result.returncode != 0 and result.stdout.strip() == ""


# --- fast-check -----------------------------------------------------------------

def test_fast_check_clean_tree_is_single_line_ok(repo):
    result = run_cli(["fast-check"], cwd=repo)
    assert result.returncode == 0
    assert result.stdout.strip() == "OK (0.0s)" and result.stderr == ""


def make_checker(repo, name, script):
    """Install a fake checker in the project's virtualenv."""
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\n{script}\n")
    tool.chmod(0o755)
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")


def test_fast_check_pass_is_silent_single_line(repo, monkeypatch):
    monkeypatch.setattr(fast_check.shutil, "which", lambda name: None)
    make_checker(repo, "ruff", "echo 'All checks passed!'; exit 0")
    make_checker(repo, "mypy", "echo 'Success: no issues found in 1 source file'; exit 0")
    assert fast_check.main() == 0


def test_fast_check_pass_output_format(repo):
    make_checker(repo, "ruff", "echo 'All checks passed!'; exit 0")
    make_checker(repo, "mypy", "exit 0")
    result = run_cli(["fast-check"], cwd=repo, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("OK (") and "All checks passed" not in result.stdout


def test_fast_check_failure_is_structured_with_remediation(repo):
    make_checker(repo, "ruff", "echo 'src/app.py:1:8: F401 `os` imported but unused'; exit 1")
    result = run_cli(["fast-check"], cwd=repo, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 1
    assert result.stdout.startswith("FAST-CHECK FAILED: ruff (exit 1")
    assert "F401" in result.stdout and "Remediation:" in result.stdout


def test_fast_check_architecture_violation_hint(repo):
    make_checker(repo, "ruff", "exit 0")
    make_checker(repo, "mypy", "exit 0")
    make_checker(repo, "lint-imports", "echo 'demo.api is not allowed to import demo.db'; exit 1")
    (repo / ".importlinter").write_text("[importlinter]\nroot_package = demo\n")
    result = run_cli(["fast-check"], cwd=repo, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 1
    assert "FAST-CHECK FAILED: import-linter" in result.stdout
    assert "not allowed to import" in result.stdout and "layer boundary" in result.stdout


def test_fast_check_checkers_import_this_checkout_first(repo):
    make_checker(repo, "ruff", 'echo "PYTHONPATH=$PYTHONPATH"; exit 1')
    result = run_cli(["fast-check"], cwd=repo, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/elsewhere"})
    line = next(item for item in result.stdout.splitlines() if item.startswith("PYTHONPATH="))
    assert line.split("=", 1)[1].split(":")[:2] == [str(repo / "src"), str(repo)]


def test_fast_check_drops_banner_art_from_failures(repo):
    make_checker(repo, "ruff", "printf '╔══╗──▶╔╗\\n=========\\nsrc/app.py:1: F401 unused\\n'; exit 1")
    result = run_cli(["fast-check"], cwd=repo, env={"PATH": "/usr/bin:/bin"})
    assert "F401" in result.stdout and "╔" not in result.stdout and "=====" not in result.stdout


def test_fast_check_hook_mode_reports_on_stderr_with_exit_2(repo):
    make_checker(repo, "ruff", "echo 'E501 line too long'; exit 1")
    result = run_cli(["fast-check", "--hook"], cwd=repo, env={"PATH": "/usr/bin:/bin"},
                     stdin=json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit"}))
    assert result.returncode == 2 and "FAST-CHECK FAILED" in result.stderr and result.stdout == ""


def test_fast_check_antigravity_hook_resolves_workspace_from_plugin_root(repo, tmp_path):
    plugin_root = tmp_path / "plugin-root"
    plugin_root.mkdir()
    make_checker(repo, "ruff", "echo 'E501 line too long'; exit 1")
    payload = {
        "toolCall": {"name": "write_to_file", "args": {"TargetFile": str(repo / "src" / "app.py")}},
        "workspacePaths": [str(repo)],
    }
    result = run_cli(["fast-check", "--hook", "--platform", "antigravity"], cwd=plugin_root,
                     env={"PATH": "/usr/bin:/bin"}, stdin=json.dumps(payload))
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert "FAST-CHECK FAILED: ruff" in result.stderr
    assert fast_check.load_result(repo)["result"] == "failed: ruff"


def test_fast_check_hook_skips_batches_without_edits(repo):
    make_checker(repo, "ruff", "exit 1")
    payload = {"hook_event_name": "PostToolBatch", "tool_calls": [{"tool_name": "Read"}, {"tool_name": "Grep"}]}
    result = run_cli(["fast-check", "--hook"], cwd=repo, env={"PATH": "/usr/bin:/bin"}, stdin=json.dumps(payload))
    assert result.returncode == 0
    payload["tool_calls"].append({"tool_name": "Edit"})
    result = run_cli(["fast-check", "--hook"], cwd=repo, env={"PATH": "/usr/bin:/bin"}, stdin=json.dumps(payload))
    assert result.returncode == 2


def test_fast_check_reports_missing_checkers_instead_of_claiming_coverage(repo):
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
    result = run_cli(["fast-check"], cwd=repo, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0
    assert "skipped, not installed: ruff, mypy" in result.stdout


def test_fast_check_enforces_timeout(repo, monkeypatch):
    make_checker(repo, "ruff", "sleep 30")
    monkeypatch.setattr(fast_check, "TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(fast_check.shutil, "which", lambda name: None)
    assert fast_check.main() == 1


def test_fast_check_makefile_target(repo):
    (repo / "Makefile").write_text("fast-check:\n\t@echo boundary violation; exit 3\n")
    result = run_cli(["fast-check"], cwd=repo)
    assert result.returncode == 1 and "make fast-check" in result.stdout and "boundary violation" in result.stdout


def test_node_project_plans_typescript_and_boundary_checks(repo, monkeypatch):
    (repo / "package.json").write_text("{}")
    (repo / "tsconfig.json").write_text("{}")
    (repo / ".dependency-cruiser.cjs").write_text("module.exports = {}")
    monkeypatch.setattr(fast_check, "_tool", lambda root, name, local: f"/fake/{name}")
    steps, missing = fast_check.plan_steps(repo)
    assert [s.argv for s in steps] == [["/fake/tsc", "--noEmit"], ["/fake/eslint", "."], ["/fake/depcruise", "src"]]
    assert missing == []


def test_python_project_plans_spec_commands(repo, monkeypatch):
    (repo / "pyproject.toml").write_text("")
    (repo / ".importlinter").write_text("")
    monkeypatch.setattr(fast_check, "_tool", lambda root, name, local: name)
    steps, _ = fast_check.plan_steps(repo)
    assert [s.argv for s in steps] == [["ruff", "check", "."], ["mypy", "--config-file", "pyproject.toml", "src/"], ["lint-imports"]]


# --- status and telemetry -------------------------------------------------------

def test_status_json(repo, initialized):
    state = sm.init_state(repo, "task-s")
    sm.record_failure(repo, state, "sha", "assert 500 == 200", ["src/app.py"], "t.py::test_health")
    telemetry.record(initialized, "task-s", 1, 2500, "fail", "t.py::test_health", "h")
    result = run_cli(["status", "--json"], cwd=repo)
    data = json.loads(result.stdout)
    assert result.returncode == 0
    assert data["task_id"] == "task-s" and data["attempts"] == 1 and data["max_retries"] == 3
    assert data["circuit_breaker_tripped"] is False and data["verification"] == "unverified"
    assert data["rejected_approaches"][0]["failed_contract"] == "t.py::test_health"
    assert data["telemetry"]["failed"] == 1


def test_status_verification_states(repo, initialized):
    state = sm.init_state(repo, "t")
    sm.mark_verified(repo, state, sm.compute_tree_hash(repo), "sha", sm.suite_hash(initialized))
    assert status.collect(repo)["verification"] == "verified"
    (repo / "src" / "app.py").write_text("VALUE = 5\n")
    assert status.collect(repo)["verification"].startswith("stale")


def test_status_text_and_last_fast_check(repo):
    sm.init_state(repo, "t")
    make_checker(repo, "ruff", "exit 1")
    run_cli(["fast-check"], cwd=repo, env={"PATH": "/usr/bin:/bin"})
    text = run_cli(["status"], cwd=repo).stdout
    assert "Attempts:      0/3" in text and "Fast-check:    failed: ruff" in text


def test_compact_summary_lists_rejected_approaches(repo):
    state = sm.init_state(repo, "task-c")
    sm.record_failure(repo, state, "sha", "assert 500 == 200", ["src/app.py"], "t.py::test_health")
    out = run_cli(["status", "--compact-summary"], cwd=repo).stdout
    assert out.startswith("[factory state] task=task-c") and "attempts=1/3" in out
    assert "Rejected approaches (do not repeat):" in out
    assert "#1 [t.py::test_health] assert 500 == 200" in out and "files: src/app.py" in out
    assert len(out.splitlines()) <= 6


def test_compact_summary_is_empty_without_state(repo):
    result = run_cli(["status", "--compact-summary"], cwd=repo)
    assert result.returncode == 0 and result.stdout == ""


def test_compact_summary_flags_tripped_breaker(repo):
    state = sm.init_state(repo, "t")
    for _ in range(3):
        sm.record_failure(repo, state, "sha", "x", [], None)
    assert "Circuit breaker tripped" in status.render_compact(status.collect(repo))


def test_status_outside_repository_does_not_crash(tmp_path, home):
    result = run_cli(["status", "--json"], cwd=tmp_path)
    assert result.returncode == 0 and json.loads(result.stdout)["initialized"] is False


def test_telemetry_appends_jsonl_records(initialized, monkeypatch):
    monkeypatch.setenv("FACTORY_PLATFORM", "codex")
    telemetry.record(initialized, "t", 1, 1500, "fail", "t.py::test_a", "hash1")
    telemetry.record(initialized, "t", 1, 900, "pass", None, "hash2")
    lines = (initialized / "telemetry.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {**first, "task_id": "t", "platform": "codex", "attempt": 1, "duration_ms": 1500,
                     "gate_result": "fail", "failed_contract_id": "t.py::test_a", "tree_hash": "hash1"}
    assert "timestamp" in first


def test_telemetry_otlp_export_failure_never_raises(initialized, monkeypatch):
    monkeypatch.setenv("FACTORY_OTLP_ENDPOINT", "http://127.0.0.1:1")
    event = telemetry.record(initialized, "t", 0, 10, "pass", None, "h")
    assert event["gate_result"] == "pass" and len(telemetry.read_events(initialized)) == 1


def test_telemetry_otlp_payload_shape(initialized, monkeypatch):
    sent = {}

    class Response:
        def read(self):
            return b""

    def fake_urlopen(request, timeout):
        sent["url"], sent["body"] = request.full_url, json.loads(request.data)
        return Response()

    monkeypatch.setenv("FACTORY_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(telemetry.urllib.request, "urlopen", fake_urlopen)
    telemetry.record(initialized, "t", 2, 500, "fail", "t.py::test_a", "h")
    span = sent["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert sent["url"] == "http://collector:4318/v1/traces"
    assert span["name"] == "holdout-gate" and span["status"]["code"] == 2
    assert {"key": "factory.failed_contract_id", "value": {"stringValue": "t.py::test_a"}} in span["attributes"]


def test_telemetry_tolerates_corrupt_lines(initialized):
    (initialized / "telemetry.jsonl").write_text('{"gate_result": "pass", "attempt": 0}\nnot json\n[]\n')
    assert telemetry.summarize(initialized)["runs"] == 1


# --- validate-triage-file -------------------------------------------------------

def test_triage_file_valid(repo):
    (repo / ".factory-triage.md").write_text(TRIAGE)
    result = run_cli(["validate-triage-file"], cwd=repo, stdin=json.dumps({"agent_type": "adversarial-critic"}))
    assert result.returncode == 0


def test_triage_file_missing_blocks_on_both_streams(repo):
    result = run_cli(["validate-triage-file"], cwd=repo, stdin=json.dumps({"agent_type": "plugin:adversarial-critic"}))
    assert result.returncode == 2 and "was not written" in result.stderr
    assert json.loads(result.stdout)["decision"] == "deny"


def test_triage_file_missing_section(repo):
    (repo / ".factory-triage.md").write_text(TRIAGE.replace("## Target Modification Strategy", "## Next steps"))
    result = run_cli(["validate-triage-file"], cwd=repo)
    assert result.returncode == 2 and "Target Modification Strategy" in result.stderr


def test_triage_accepts_list_style_sections(repo):
    (repo / ".factory-triage.md").write_text(
        "- **Root Cause Analysis**: a\n- **Unmet Behavioral Contract Clause**: b\n- **Target Modification Strategy**: c\n")
    assert run_cli(["validate-triage-file"], cwd=repo).returncode == 0


def test_triage_check_ignores_other_subagents(repo):
    result = run_cli(["validate-triage-file"], cwd=repo, stdin=json.dumps({"agent_type": "implementer"}))
    assert result.returncode == 0


def test_init_excludes_plan_and_triage_files(repo, home, healthy):
    assert worktree.init_repo() == 0
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".factory-plan.md" in exclude and ".factory-triage.md" in exclude


def test_fast_check_changed_files_preserves_leading_space_and_short_names(repo):
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    (repo / "x").write_text("one\n")
    git(repo, "add", "x")
    git(repo, "commit", "-q", "-m", "add single char file")
    (repo / "x").write_text("two\n")
    assert sorted(fast_check.changed_files(repo)) == ["src/app.py", "x"]


def test_flat_layout_python_project_plans_mypy(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='flat'\nversion='0'\n")
    monkeypatch.setattr(fast_check, "_tool", lambda root, name, local: name)
    steps, _ = fast_check.plan_steps(tmp_path)
    assert [s.argv for s in steps] == [["ruff", "check", "."], ["mypy", "--config-file", "pyproject.toml", "."]]


def test_antigravity_post_invocation_delivers_pending_fast_check_once(repo, tmp_path):
    plugin_root = tmp_path / "plugin-root"
    plugin_root.mkdir()
    make_checker(repo, "ruff", "echo 'E501 line too long'; exit 1")
    payload = {
        "toolCall": {"name": "write_file", "args": {"Path": str(repo / "src" / "app.py")}},
        "workspacePaths": [str(repo)],
    }
    post_tool = run_cli(["fast-check", "--hook", "--platform", "antigravity"], cwd=plugin_root,
                        env={"PATH": "/usr/bin:/bin"}, stdin=json.dumps(payload))
    assert post_tool.returncode == 0 and json.loads(post_tool.stdout) == {}

    post_inv = run_cli(["fast-check", "--post-invocation", "--platform", "antigravity"], cwd=plugin_root,
                       stdin=json.dumps({"workspacePaths": [str(repo)]}))
    assert post_inv.returncode == 0
    injected = json.loads(post_inv.stdout)
    assert "FAST-CHECK FAILED: ruff" in injected["injectSteps"][0]["systemMessage"]["systemMessage"]

    second_inv = run_cli(["fast-check", "--post-invocation", "--platform", "antigravity"], cwd=plugin_root,
                         stdin=json.dumps({"workspacePaths": [str(repo)]}))
    assert second_inv.returncode == 0 and json.loads(second_inv.stdout) == {}
