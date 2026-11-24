import os
import subprocess
import sys
from pathlib import Path

import pytest

from factory_core import doctor

REPO_ROOT = Path(__file__).resolve().parents[2]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def run_cli(args, cwd, stdin="", env=None):
    """Run the CLI in a subprocess, the way a harness hook does."""
    full_env = {**os.environ, **(env or {})}
    full_env["PYTHONPATH"] = str(REPO_ROOT / "factory-core" / "src")
    return subprocess.run(
        [sys.executable, "-m", "factory_core.cli", *args],
        cwd=cwd, input=stdin, env=full_env, capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HOME so tests never touch the real ~/.factory-holdouts."""
    path = (tmp_path / "home").resolve()
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("FACTORY_PLATFORM", "FACTORY_OTLP_ENDPOINT", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.delenv(key, raising=False)
    return path


@pytest.fixture
def repo(tmp_path, home, monkeypatch):
    """A committed Git repository named demo-repo; the test runs from inside it."""
    path = (tmp_path / "work" / "demo-repo").resolve()
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text("VALUE = 1\n")
    (path / "README.md").write_text("demo\n")
    (path / "Dockerfile").write_text("FROM scratch\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def initialized(repo, home):
    """A repository with a hold-out directory, as `factory-core init` leaves it."""
    seal = home / ".factory-holdouts" / repo.name
    (seal / "fixtures").mkdir(parents=True)
    (seal / "docker-compose.holdout.yml").write_text("services: {}\n")
    (seal / "fixtures" / "golden.json").write_text('{"auth": {"api_key": "s3cr3t-fixture-value"}}')
    return seal


@pytest.fixture
def healthy(monkeypatch):
    """Make the prerequisite preflight pass without Docker."""
    checks = [doctor.Check(name, True, "ok") for name in ("python", "git", "docker", "compose", "path", "holdouts")]
    monkeypatch.setattr(doctor, "run_checks", lambda: checks)
    return checks
