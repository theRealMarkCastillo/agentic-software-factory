"""Run the sealed hold-out oracle in a disposable Docker Compose project."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .common import FactoryError, run

COMPOSE_FILE = "docker-compose.holdout.yml"
DEFAULT_TIMEOUT = 600


@dataclass
class GateRun:
    exit_code: int
    output: str
    duration_ms: int
    project: str
    timed_out: bool = False


def project_name(task_id: str, short_sha: str) -> str:
    """Compose project names allow only lowercase alphanumerics, '-' and '_'."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", task_id.lower()).strip("-_") or "task"
    return f"holdout-{slug[:40]}-{short_sha.lower()}"


def prepare_sut(repo: str | Path, patch: bytes) -> Path:
    """Extract HEAD into /tmp/sut-<uuid> and apply the candidate patch."""
    sut_dir = Path(f"/tmp/sut-{uuid.uuid4()}")
    sut_dir.mkdir(mode=0o700)
    try:
        archive = run(["git", "archive", "--format=tar", "HEAD"], cwd=repo, timeout=120)
        if archive.returncode != 0:
            raise FactoryError(f"git archive failed: {archive.stderr.decode(errors='replace').strip()}")
        untar = run(["tar", "-xf", "-", "-C", str(sut_dir)], input_bytes=archive.stdout, timeout=120)
        if untar.returncode != 0:
            raise FactoryError(f"Could not extract archive: {untar.stderr.decode(errors='replace').strip()}")
        if patch.strip():
            # GIT_CEILING_DIRECTORIES keeps git from adopting an enclosing repository.
            env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(sut_dir.parent)}
            applied = run(
                ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
                cwd=sut_dir, env=env, input_bytes=patch, timeout=120,
            )
            if applied.returncode != 0:
                raise FactoryError(f"Candidate patch did not apply: {applied.stderr.decode(errors='replace').strip()}")
    except BaseException:
        shutil.rmtree(sut_dir, ignore_errors=True)
        raise
    return sut_dir


def _compose_base(project: str, compose_file: Path) -> list[str]:
    return ["docker", "compose", "--ansi", "never", "-p", project, "-f", str(compose_file)]


def run_holdout(
    repo: str | Path,
    patch: bytes,
    holdout_dir: str | Path,
    task_id: str,
    short_sha: str,
    timeout: float | None = None,
) -> GateRun:
    """Build, run, and always tear down the oracle. Never publishes host ports."""
    compose_file = Path(holdout_dir) / COMPOSE_FILE
    project = project_name(task_id, short_sha)
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
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raw, _ = process.communicate()
        output = raw.decode(errors="replace")
        if timed_out:
            output += f"\nHold-out gate timed out after {limit:.0f}s."
        return GateRun(
            exit_code=124 if timed_out else process.returncode,
            output=output,
            duration_ms=int((time.monotonic() - started) * 1000),
            project=project,
            timed_out=timed_out,
        )
    finally:
        env.setdefault("CLEAN_REPO_DIR", str(sut_dir or "/nonexistent"))
        try:
            run([*base, "down", "-v", "--remove-orphans", "--rmi", "local"],
                cwd=holdout_dir, env=env, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
        if sut_dir is not None:
            shutil.rmtree(sut_dir, ignore_errors=True)
