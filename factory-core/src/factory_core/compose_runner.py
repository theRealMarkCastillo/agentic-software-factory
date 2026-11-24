"""Run the sealed hold-out oracle in a disposable Docker Compose project."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .common import FactoryError, run, runtime_tmp_dir
from .patch_exporter import CandidatePatch, materialize_candidate

COMPOSE_FILE = "docker-compose.holdout.yml"
DEFAULT_TIMEOUT = 600


@dataclass
class GateRun:
    exit_code: int
    output: str
    duration_ms: int
    project: str
    timed_out: bool = False
    verifier_ran: bool = True


def project_name(task_id: str, short_sha: str) -> str:
    """Compose project names allow only lowercase alphanumerics, '-' and '_'."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", task_id.lower()).strip("-_") or "task"
    return f"holdout-{slug[:40]}-{short_sha.lower()}"


def prepare_sut(repo: str | Path, patch: CandidatePatch | bytes) -> Path:
    """Prepare a private build context from the captured tree or a legacy patch."""
    sut_dir = Path(tempfile.mkdtemp(prefix="sut-", dir=runtime_tmp_dir()))
    try:
        materialize_candidate(repo, patch, sut_dir)
    except BaseException:
        shutil.rmtree(sut_dir, ignore_errors=True)
        raise
    return sut_dir


def _compose_base(project: str, compose_file: Path) -> list[str]:
    return ["docker", "compose", "--ansi", "never", "-p", project, "-f", str(compose_file)]


def _verifier_started(base: list[str], holdout_dir: str | Path, env: dict[str, str]) -> bool:
    ps = run([*base, "ps", "-a", "-q", "verifier"], cwd=holdout_dir, env=env, timeout=15)
    if ps.returncode != 0 or not ps.stdout.strip():
        return False
    cid = ps.stdout.decode(errors="replace").strip().splitlines()[0]
    insp = run(["docker", "inspect", "--format", "{{.State.StartedAt}}", cid], env=env, timeout=15)
    started_at = insp.stdout.decode(errors="replace").strip()
    return insp.returncode == 0 and re.match(r"^(?!0001-)\d{4}-\d{2}-\d{2}T", started_at) is not None


def run_holdout(
    repo: str | Path,
    patch: CandidatePatch | bytes,
    holdout_dir: str | Path,
    task_id: str,
    short_sha: str,
    timeout: float | None = None,
) -> GateRun:
    """Build, run, and always tear down the oracle. Never publishes host ports."""
    compose_file = Path(holdout_dir) / COMPOSE_FILE
    project = f"{project_name(task_id, short_sha)}-{uuid.uuid4().hex}"
    limit = timeout if timeout is not None else float(os.environ.get("FACTORY_GATE_TIMEOUT", DEFAULT_TIMEOUT))
    base = _compose_base(project, compose_file)
    sut_dir: Path | None = None
    env = dict(os.environ)
    started = time.monotonic()
    try:
        sut_dir = prepare_sut(repo, patch)
        if not (sut_dir / "Dockerfile").is_file():
            raise FactoryError(
                "The repository has no Dockerfile at its root, so the system under test cannot be built.",
                "Add a Dockerfile that serves the application on port 8080 with a /health endpoint.",
            )
        env["CLEAN_REPO_DIR"] = str(sut_dir)
        env.setdefault("DOCKER_BUILDKIT", "1")
        process = subprocess.Popen(
            [*base, "up", "--build", "--abort-on-container-exit", "--exit-code-from", "verifier"],
            cwd=str(holdout_dir), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        timed_out = False
        try:
            raw, _ = process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            raw, _ = process.communicate()
        output = raw.decode(errors="replace")
        if timed_out:
            output += f"\nHold-out gate timed out after {limit:.0f}s."
        try:
            verifier_ran = _verifier_started(base, holdout_dir, env)
        except (OSError, subprocess.SubprocessError):
            verifier_ran = False
        return GateRun(
            exit_code=124 if timed_out else process.returncode,
            output=output,
            duration_ms=int((time.monotonic() - started) * 1000),
            project=project,
            timed_out=timed_out,
            verifier_ran=verifier_ran,
        )
    finally:
        env.setdefault("CLEAN_REPO_DIR", str(sut_dir or "/nonexistent"))
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            run([*base, "down", "-v", "--remove-orphans", "--rmi", "local"],
                cwd=holdout_dir, env=env, timeout=120)
        if sut_dir is not None:
            shutil.rmtree(sut_dir, ignore_errors=True)
