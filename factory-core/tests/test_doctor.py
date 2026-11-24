import json
import shutil

import pytest

from factory_core import doctor

from conftest import run_cli


def fake_probe(responses):
    def probe(args, timeout=15):
        for prefix, result in responses.items():
            if " ".join(args).startswith(prefix):
                return result
        return True, ""
    return probe


GOOD = {
    "git --version": (True, "git version 2.54.0 (Apple Git-157)"),
    "docker info": (True, "29.8.2"),
    "docker compose version": (True, "5.5.0"),
}


@pytest.fixture
def on_path(home, monkeypatch):
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{local_bin}:/usr/bin:/bin")


def test_all_checks_pass(on_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_probe", fake_probe(GOOD))
    assert doctor.main() == 0
    out = capsys.readouterr()
    assert out.out.strip() == doctor.OK_MESSAGE
    assert out.err == ""


def test_json_success_payload(on_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_probe", fake_probe(GOOD))
    assert doctor.main(as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert {c["name"] for c in payload["checks"]} == {"python", "git", "docker", "compose", "path", "holdouts"}


def test_creates_holdouts_root_with_private_mode(on_path, home, monkeypatch):
    monkeypatch.setattr(doctor, "_probe", fake_probe(GOOD))
    doctor.main()
    root = home / ".factory-holdouts"
    assert root.is_dir()
    assert root.stat().st_mode & 0o777 == 0o700


def test_stopped_daemon_exits_2_on_both_streams(on_path, monkeypatch, capsys):
    responses = {**GOOD, "docker info": (False, "Cannot connect to the Docker daemon at unix:///nonexistent.sock")}
    monkeypatch.setattr(doctor, "_probe", fake_probe(responses))
    assert doctor.main() == 2
    out = capsys.readouterr()
    assert "docker" in out.err and "Start Docker Engine" in out.err
    payload = json.loads(out.out)
    assert payload["status"] == "error"
    assert payload["decision"] == "deny"
    failed = [c for c in payload["checks"] if not c["ok"]]
    assert [c["name"] for c in failed] == ["docker"]
    assert failed[0]["remediation"]


def test_docker_info_error_text_with_zero_exit_is_a_failure(on_path, monkeypatch):
    monkeypatch.setattr(doctor, "_probe", fake_probe({**GOOD, "docker info": (True, "error during connect")}))
    assert doctor.check_docker().ok is False


@pytest.mark.parametrize("version,ok", [("5.5.0", True), ("v2.24.1", True), ("1.29.2", False), ("", False)])
def test_compose_requires_v2(monkeypatch, version, ok):
    monkeypatch.setattr(doctor, "_probe", fake_probe({"docker compose version": (bool(version), version)}))
    assert doctor.check_compose().ok is ok


def test_compose_plugin_missing(monkeypatch):
    monkeypatch.setattr(doctor, "_probe", fake_probe({"docker compose version": (False, "docker: 'compose' is not a docker command")}))
    check = doctor.check_compose()
    assert check.ok is False and "compose/install" in check.remediation


@pytest.mark.parametrize("output,ok", [
    ("git version 2.54.0", True), ("git version 2.30.0", True), ("git version 2.29.9", False), ("git version 1.9.1", False),
])
def test_git_minimum_version(monkeypatch, output, ok):
    monkeypatch.setattr(doctor, "_probe", fake_probe({"git --version": (True, output)}))
    assert doctor.check_git().ok is ok


def test_old_python_fails(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 4, "final", 0))
    assert doctor.check_python().ok is False


def test_local_bin_missing_from_path(home, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    check = doctor.check_path()
    assert check.ok is False and "export PATH" in check.remediation


def test_holdouts_not_writable(home, monkeypatch):
    root = home / ".factory-holdouts"
    root.mkdir(mode=0o500)
    try:
        assert doctor.check_holdouts().ok is False
    finally:
        root.chmod(0o700)


def test_holdouts_open_to_other_users_fails(home):
    root = home / ".factory-holdouts"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    assert doctor.check_holdouts().ok is False


def test_missing_binary_is_reported_not_raised():
    ok, detail = doctor._probe(["definitely-not-a-real-binary-xyz"])
    assert ok is False and "not found" in detail


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_bad_docker_host_fails_closed_end_to_end(home, tmp_path):
    """The real CLI against an unreachable daemon: exit 2, stderr text, stdout JSON."""
    result = run_cli(["doctor"], cwd=tmp_path, env={"DOCKER_HOST": "unix:///nonexistent.sock"})
    assert result.returncode == 2
    assert "docker" in result.stderr.lower()
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert any(c["name"] == "docker" and not c["ok"] for c in payload["checks"])
