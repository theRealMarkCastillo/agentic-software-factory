import json
import os

import pytest

from factory_core.path_guard import evaluate_payload

from conftest import run_cli


def bash(command, **extra):
    return {"tool_name": "Bash", "tool_input": {"command": command}, **extra}


def denied(payload):
    allowed, reason = evaluate_payload(payload)
    return not allowed


@pytest.fixture
def seal(home):
    path = home / ".factory-holdouts" / "demo-repo" / "suites"
    path.mkdir(parents=True)
    (path / "test_secret.py").write_text("SECRET = 1\n")
    return home / ".factory-holdouts"


@pytest.fixture
def nested_cwd(home, seal, monkeypatch):
    """A directory two levels below HOME, so ../../.factory-holdouts is the sealed directory."""
    path = home / "git" / "project"
    path.mkdir(parents=True)
    monkeypatch.chdir(path)
    return path


# --- direct path arguments ------------------------------------------------------

@pytest.mark.parametrize("key", ["file_path", "path", "target", "filePath", "cwd"])
def test_direct_path_fields_are_blocked(seal, key):
    payload = {"tool_name": "Read", "tool_input": {key: str(seal / "demo-repo" / "suites" / "test_secret.py")}}
    allowed, reason = evaluate_payload(payload)
    assert not allowed and "hold-out" in reason


def test_tilde_path_is_blocked(seal):
    assert denied({"tool_name": "Read", "tool_input": {"file_path": "~/.factory-holdouts/demo-repo/x.py"}})


def test_the_sealed_root_itself_is_blocked(seal):
    assert denied({"tool_name": "Glob", "tool_input": {"path": str(seal)}})


def test_nonexistent_file_inside_seal_is_blocked(seal):
    assert denied({"tool_name": "Write", "tool_input": {"file_path": str(seal / "new" / "file.py")}})


def test_antigravity_field_names_are_blocked(seal):
    assert denied({"tool_name": "view_file", "tool_input": {"AbsolutePath": str(seal / "x")}})
    assert denied({"tool_name": "write_to_file", "tool_input": {"TargetFile": str(seal / "x")}})


def test_nested_path_fields_are_blocked(seal):
    payload = {"tool_name": "MultiEdit", "tool_input": {"edits": [{"file_path": str(seal / "x.py")}]}}
    assert denied(payload)


def test_sibling_with_shared_prefix_is_allowed(home, seal):
    (home / ".factory-holdouts-notes").mkdir()
    assert not denied({"tool_name": "Read", "tool_input": {"file_path": str(home / ".factory-holdouts-notes" / "a.md")}})


def test_search_tool_on_parent_directory_is_blocked(home, seal):
    assert denied({"tool_name": "Grep", "tool_input": {"pattern": "SECRET", "path": str(home)}})
    assert not denied({"tool_name": "Read", "tool_input": {"file_path": str(home / "notes.md")}})


# --- traversal and symlinks -----------------------------------------------------

def test_relative_traversal_is_blocked(nested_cwd):
    assert denied(bash("cat ../../.factory-holdouts/demo-repo/suites/test_secret.py"))


def test_relative_traversal_in_path_field_is_blocked(nested_cwd):
    assert denied({"tool_name": "Read", "tool_input": {"file_path": "../../.factory-holdouts/demo-repo/suites/test_secret.py"}})


def test_traversal_uses_payload_cwd_not_process_cwd(home, seal, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = home / "git" / "project"
    project.mkdir(parents=True)
    payload = {"tool_name": "Read", "cwd": str(project), "tool_input": {"file_path": "../../.factory-holdouts/x"}}
    assert denied(payload)


def test_symlink_to_seal_is_blocked(seal, tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(seal)
    assert denied({"tool_name": "Read", "tool_input": {"file_path": str(link / "demo-repo" / "suites" / "test_secret.py")}})
    assert denied(bash(f"cat {link}/demo-repo/suites/test_secret.py"))


def test_symlink_chain_is_blocked(seal, tmp_path):
    first = tmp_path / "a"
    first.symlink_to(seal / "demo-repo")
    second = tmp_path / "b"
    second.symlink_to(first)
    assert denied({"tool_name": "Read", "tool_input": {"file_path": str(second / "suites")}})


def test_relative_symlink_inside_worktree_is_blocked(nested_cwd, seal):
    os.symlink(seal, nested_cwd / "vendor")
    assert denied(bash("ls vendor/demo-repo"))


def test_dotdot_after_symlink_is_resolved_from_the_symlink_target(nested_cwd, seal):
    (nested_cwd / "vendor").symlink_to(seal / "demo-repo", target_is_directory=True)
    allowed, reason = evaluate_payload({
        "tool_name": "Write",
        "cwd": str(nested_cwd),
        "tool_input": {"file_path": "vendor/../new.py"},
    })
    assert not allowed and "hold-out" in reason


# --- shell command inspection ---------------------------------------------------

@pytest.mark.parametrize("command", [
    "cat ~/.factory-holdouts/test.py",
    "cat $HOME/.factory-holdouts/test.py",
    "cat ${HOME}/.factory-holdouts/test.py",
    "cat ~/.factory-hold''outs/test.py",
    'cat ~/.factory-"holdouts"/test.py',
    "cat ~/.factory-hold\\outs/test.py",
    "cat ~/.FACTORY-HOLDOUTS/test.py",
    "cd ~/.factory-holdouts && ls",
    "echo hi; cat ~/.factory-holdouts/test.py",
    "cat<~/.factory-holdouts/test.py",
    "python3 -c \"print(open('/x/.factory-holdouts/t').read())\"",
    "tar --directory=$HOME/.factory-holdouts -c .",
    "cat 'unterminated ~/.factory-holdouts/x",
])
def test_holdout_references_in_commands_are_blocked(seal, command):
    assert denied(bash(command))


@pytest.mark.parametrize("argument", ["'work tree'/new.py", r"work\ tree/new.py"])
@pytest.mark.parametrize("alias_points_into_seal", [False, True])
def test_shell_parses_space_in_path_after_redirection(nested_cwd, seal, argument, alias_points_into_seal):
    alias = nested_cwd / "work tree"
    if alias_points_into_seal:
        alias.symlink_to(seal, target_is_directory=True)
    else:
        alias.mkdir()

    allowed, reason = evaluate_payload(bash(f"printf '%s' '# text' >{argument}"))
    assert allowed is not alias_points_into_seal
    if alias_points_into_seal:
        assert "hold-out" in reason


def test_glob_expanding_into_seal_is_blocked(home, seal, monkeypatch):
    monkeypatch.chdir(home)
    assert denied(bash("cat .factory-hold*/demo-repo/suites/test_secret.py"))
    assert denied(bash("cat .f?ctory-holdouts/demo-repo/suites/test_secret.py"))


@pytest.mark.parametrize("command", ["grep -r SECRET ~", "find ~ -name '*.py'", "rg SECRET $HOME", "ls -R ~", "tar -cf /tmp/x.tar ~"])
def test_recursive_commands_over_a_parent_are_blocked(seal, command):
    assert denied(bash(command))


@pytest.mark.parametrize("command", [
    "curl --unix-socket /var/run/docker.sock http://localhost/containers/json",
    "ls -l /var/run/docker.sock",
    "docker run -v ~/.factory-holdouts:/x alpine ls /x",
    "docker ps",
    "sudo docker compose ls",
    "FOO=1 docker ps",
    "echo ok && podman ps",
])
def test_docker_socket_and_container_clis_are_blocked(command):
    assert denied(bash(command))


def test_docker_socket_path_fields_cover_desktop_runtimes(home, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/engine.sock")
    for path in ("/var/run/docker.sock", str(home / ".orbstack" / "run" / "docker.sock"),
                 str(home / ".docker" / "run" / "docker.sock"), "/custom/engine.sock", "/somewhere/else/docker.sock"):
        assert denied({"tool_name": "Read", "tool_input": {"file_path": path}}), path
    assert denied(bash("nc -U /custom/engine.sock"))


@pytest.mark.parametrize("command", ["echo x > .git/hooks/pre-commit", "chmod +x .git/hooks/post-checkout", "ls .git/hooks/"])
def test_git_hooks_are_blocked(repo, command):
    assert denied(bash(command))


def test_git_hooks_path_field_is_blocked(repo):
    assert denied({"tool_name": "Write", "tool_input": {"file_path": str(repo / ".git" / "hooks" / "pre-commit")}})


@pytest.mark.parametrize("command", [
    "ls -la", "git status --porcelain", "pytest -q tests/", "cat src/app.py", "grep -rn TODO src",
    "factory-core status --json", "factory-core holdout-gate", "cat .factory-state.json",
    "echo 'docker is mentioned in prose only'", "git log --oneline -5 -- src/",
])
def test_ordinary_commands_are_allowed(repo, command):
    assert evaluate_payload(bash(command)) == (True, "Access granted.")


def test_ordinary_file_tools_are_allowed(repo):
    assert not denied({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "app.py")}})
    assert not denied({"tool_name": "Read", "tool_input": {"file_path": str(repo / ".factory-state.json")}})


# --- factory state protection ---------------------------------------------------

@pytest.mark.parametrize("command", [
    "echo '{}' > .factory-state.json", "rm .factory-state.json", "sed -i '' s/3/0/ .factory-state.json",
    "python3 -c \"open('.factory-state.json','w').write('{}')\"", "mv /tmp/s .factory-state.json",
])
def test_state_file_cannot_be_written_from_shell(repo, command):
    assert denied(bash(command))


def test_state_file_cannot_be_written_with_file_tools(repo):
    assert denied({"tool_name": "Write", "tool_input": {"file_path": str(repo / ".factory-state.json")}})
    assert denied({"tool_name": "Edit", "tool_input": {"file_path": ".factory-state.json"}})


def test_apply_patch_targets_are_inspected(repo, seal):
    def patch(path):
        return {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch\n"}}
    assert denied(patch(".factory-state.json"))
    assert denied(patch(".git/hooks/pre-commit"))
    assert denied(patch(str(seal / "x.py")))
    assert not denied(patch("src/app.py"))


def test_patch_body_mentioning_docker_is_not_a_command(repo):
    body = "*** Begin Patch\n*** Add File: docs/run.md\n+Run docker ps to list containers.\n*** End Patch\n"
    assert not denied({"tool_name": "apply_patch", "tool_input": {"command": body}})


def test_critic_may_write_only_the_triage_file(repo):
    def write(path):
        return {"tool_name": "Write", "agent_type": "agentic-software-factory:adversarial-critic",
                "tool_input": {"file_path": str(repo / path)}}
    assert denied(write("src/app.py"))
    assert not denied(write(".factory-triage.md"))
    assert not denied({"tool_name": "Write", "agent_type": "implementer", "tool_input": {"file_path": str(repo / "src/app.py")}})


# --- CLI contract ---------------------------------------------------------------

def test_cli_deny_writes_stderr_and_stdout_json_with_exit_2(repo, seal):
    payload = json.dumps(bash("cat ~/.factory-holdouts/test.py"))
    result = run_cli(["guard"], cwd=repo, stdin=payload)
    assert result.returncode == 2
    assert result.stderr.startswith("BLOCKED by Factory Security Guard:")
    body = json.loads(result.stdout)
    assert body["decision"] == "deny"
    assert body["reason"] == result.stderr.strip()


@pytest.mark.parametrize(("target", "label"), [
    ("~/.factory-holdouts/new.py", "hold-out"),
    ("/var/run/docker.sock", "Docker socket"),
    ("src/../.git/hooks/pre-commit", "Git hooks directory"),
])
def test_cli_denies_native_patch_move_target_on_both_output_streams(repo, seal, target, label):
    command = (
        f"*** Begin Patch\n*** Update File: src/app.py\n*** Move to: {target}\n"
        "@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch\n"
    )
    payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "cwd": str(repo),
               "tool_input": {"command": command}}

    result = run_cli(["guard"], cwd=repo, stdin=json.dumps(payload))

    assert result.returncode == 2
    diagnostic, = result.stderr.splitlines()
    assert diagnostic.startswith("BLOCKED by Factory Security Guard:")
    assert target in diagnostic and label in diagnostic
    assert json.loads(result.stdout) == {"decision": "deny", "reason": diagnostic}


def test_cli_traversal_from_nested_directory(nested_cwd):
    payload = json.dumps(bash("cat ../../.factory-holdouts/test.py"))
    result = run_cli(["guard"], cwd=nested_cwd, stdin=payload)
    assert result.returncode == 2
    assert json.loads(result.stdout)["decision"] == "deny"
    assert result.stderr.strip()


def test_cli_allow(repo):
    result = run_cli(["guard"], cwd=repo, stdin=json.dumps(bash("ls")))
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"decision": "allow"}
    assert result.stderr == ""


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]"])
def test_malformed_payload_fails_closed(repo, stdin):
    result = run_cli(["guard"], cwd=repo, stdin=stdin)
    assert result.returncode == 2
    diagnostic, = result.stderr.splitlines()
    assert "Malformed hook payload" in diagnostic
    assert json.loads(result.stdout) == {"decision": "deny", "reason": diagnostic}
