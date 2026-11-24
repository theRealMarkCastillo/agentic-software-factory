"""Live Docker Compose integration test for the sealed hold-out gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import git

from factory_core import doctor, holdout_gate, worktree
from factory_core import state_manager as sm

REAL_HOME = Path.home()


def _docker_available() -> bool:
    return doctor.check_docker().ok and doctor.check_compose().ok


DOCKERFILE = """\
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py .
EXPOSE 8080
CMD ["python3", "server.py"]
"""

SERVER_FAIL = """\
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "down", "echo": "example-holdout-key-replace-me"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""

SERVER_OK = """\
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason="Docker Engine + Compose v2 required")
def test_live_docker_compose_holdout_gate_end_to_end(tmp_path, home, monkeypatch, capsys):
    if (REAL_HOME / ".docker").is_dir() and not os.environ.get("DOCKER_CONFIG"):
        monkeypatch.setenv("DOCKER_CONFIG", str(REAL_HOME / ".docker"))
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", f"{local_bin}:{os.environ.get('PATH', '')}")

    repo = (tmp_path / "work" / "live-docker-demo").resolve()
    repo.mkdir(parents=True)
    (repo / "Dockerfile").write_text(DOCKERFILE)
    (repo / "server.py").write_text(SERVER_FAIL)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial failing sut")

    monkeypatch.chdir(repo)
    assert worktree.init_repo() == 0
    capsys.readouterr()

    wt = worktree.create("live-task")
    monkeypatch.chdir(wt)

    # 1. Failing SUT: gate exits 2, redacts fixture secret, increments attempts, cleans up containers
    try:
        code = holdout_gate.main()
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    assert code == 2
    assert "HOLD-OUT GATE FAILED (attempt 1/3" in captured.err
    assert "example-holdout-key-replace-me" not in (captured.out + captured.err)
    assert "[REDACTED_SECRET]" in captured.err
    assert sm.load_state(wt)["attempts"] == 1

    # 2. Fix SUT in an untracked/modified file: gate exits 0, caches tree hash, cleans up
    (wt / "server.py").write_text(SERVER_OK)
    assert holdout_gate.main() == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("OK (")
    assert sm.load_state(wt)["verified_tree_hash"] == sm.compute_tree_hash(wt)

    # 3. Cached run skips Docker
    assert holdout_gate.main() == 0
    assert capsys.readouterr().out.strip() == "Verification cached."

    # 4. No leftover holdout-live-task containers
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=holdout-live-task", "--format", "{{.Names}}"],
        check=False, capture_output=True, text=True,
    )
    assert ps.stdout.strip() == ""
