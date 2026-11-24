"""Prune stale worktrees, orphaned hold-out Docker resources, and old triage files.

Worktrees with uncommitted changes are never removed.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import state_manager
from .common import (
    STATE_FILE,
    TRIAGE_FILE,
    WORKTREES_DIR,
    FactoryError,
    is_private_runtime_dir,
    main_repo_root,
    porcelain_paths,
    run,
    runtime_tmp_path,
)

MANAGED_LABEL = "io.factory-core.holdout=true"
DEFAULT_MAX_AGE_DAYS = 14
# Resources younger than this may belong to a gate that is still starting.
MIN_DOCKER_AGE_SECONDS = 60
TEMP_MAX_AGE_SECONDS = 24 * 3600
DAY = 86400.0


@dataclass
class Report:
    dry_run: bool
    worktrees: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    triage_files: list[str] = field(default_factory=list)
    temp_dirs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reclaimed_bytes: int = 0


def _tree_size(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(base, name)).st_size
    return total


def _format_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{count} B"


def _is_dirty(path: Path) -> bool:
    paths = porcelain_paths(path, include_internal=False)
    if paths is None:
        return True  # Unknown state is treated as unsafe to delete.
    return bool(paths)


def _activity_files(path: Path) -> list[Path]:
    """Files whose mtime moves when someone works in the worktree."""
    files = [path, path / STATE_FILE]
    proc = run(["git", "rev-parse", "--absolute-git-dir"], cwd=path)
    if proc.returncode == 0:
        git_dir = Path(proc.stdout.decode().strip())
        files += [git_dir / "index", git_dir / "HEAD", git_dir / "logs" / "HEAD"]
    return files


def _last_activity(path: Path) -> float:
    times = []
    for candidate in _activity_files(path):
        with contextlib.suppress(OSError):
            times.append(candidate.stat().st_mtime)
    return max(times, default=0.0)


def _is_merged(main_root: Path, path: Path) -> bool:
    """Merged means the gate verified this exact worktree after commits were made, and its tip is in main."""
    try:
        state = state_manager.load_state(path)
    except FactoryError:
        return False
    if not state:
        return False
    verified_hash = state.get("verified_tree_hash")
    last_sha = state.get("last_evaluated_sha")
    if not isinstance(verified_hash, str) or not verified_hash or not isinstance(last_sha, str) or not last_sha:
        return False
    tip = run(["git", "rev-parse", "HEAD"], cwd=path).stdout.decode().strip()
    if not tip or tip != last_sha:
        return False
    try:
        if state_manager.compute_tree_hash(path) != verified_hash:
            return False
    except FactoryError:
        return False
    reflog_shas = [
        sha for sha in run(["git", "log", "-g", "--format=%H", "HEAD"], cwd=path).stdout.decode().splitlines() if sha
    ]
    if reflog_shas and len(set(reflog_shas)) < 2:
        return False
    return run(["git", "merge-base", "--is-ancestor", tip, "HEAD"], cwd=main_root).returncode == 0


def _gc_worktrees(main_root: Path, max_age_days: float, report: Report) -> None:
    base = main_root / WORKTREES_DIR
    if base.is_symlink() or not base.is_dir():
        return
    now = time.time()
    for path in sorted(p for p in base.iterdir() if not p.is_symlink() and p.is_dir()):
        label = f"{WORKTREES_DIR}/{path.name}"
        merged = _is_merged(main_root, path)
        stale = (now - _last_activity(path)) >= max_age_days * DAY
        if not (merged or stale):
            continue
        if _is_dirty(path):
            report.skipped.append(f"{label} (uncommitted changes)")
            continue
        branch = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=path).stdout.decode().strip()
        size = _tree_size(path)
        report.worktrees.append(f"{label} ({'merged' if merged else 'stale'})")
        report.reclaimed_bytes += size
        if merged and branch.startswith("factory/"):
            report.branches.append(branch)
        if report.dry_run:
            continue
        removed = run(["git", "worktree", "remove", "--force", str(path)], cwd=main_root)
        if removed.returncode != 0:
            report.notes.append(f"could not remove {label}: {removed.stderr.decode(errors='replace').strip()}")
            report.worktrees.pop()
            report.reclaimed_bytes -= size
            continue
        if merged and branch.startswith("factory/"):
            run(["git", "branch", "-d", branch], cwd=main_root)
    if not report.dry_run:
        run(["git", "worktree", "prune"], cwd=main_root)


def _docker_lines(args: list[str]) -> list[str] | None:
    try:
        proc = run(["docker", *args], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.decode(errors="replace").splitlines() if line.strip()]


def parse_docker_time(text: str) -> float | None:
    """Parse RFC 3339 ("2026-11-23T18:36:23-08:00") and Go ("2026-11-23 18:34:12.28 -0800 PST") stamps."""
    match = re.match(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?", text.strip())
    if not match:
        return None
    offset = match[3] or "+00:00"
    if offset == "Z":
        offset = "+00:00"
    elif ":" not in offset:
        offset = f"{offset[:3]}:{offset[3:]}"
    try:
        return datetime.fromisoformat(f"{match[1]}T{match[2]}{offset}").timestamp()
    except ValueError:
        return None


def _created_age(kind: str, name: str) -> float:
    """Seconds since creation. Unknown ages read as 0 so the resource is kept."""
    lines = _docker_lines([kind, "inspect", "--format", "{{.Created}}" if kind != "volume" else "{{.CreatedAt}}", name])
    created = parse_docker_time(lines[0]) if lines else None
    return time.time() - created if created is not None else 0.0


def _gc_docker(report: Report) -> None:
    running = _docker_lines(["ps", "--filter", "label=com.docker.compose.project",
                             "--format", '{{.Label "com.docker.compose.project"}}'])
    if running is None:
        report.notes.append("Docker is not available; skipped container, network, and volume cleanup.")
        return
    active = {project for project in running if project.startswith("holdout-")}

    stopped = _docker_lines(["ps", "-a", "--filter", "status=exited", "--filter", "status=created",
                             "--filter", f"label={MANAGED_LABEL}",
                             "--format", '{{.ID}} {{.Names}} {{.Label "com.docker.compose.project"}}']) or []
    for line in stopped:
        parts = line.split()
        if len(parts) != 3 or not parts[2].startswith("holdout-") or parts[2] in active:
            continue
        if _created_age("container", parts[0]) < MIN_DOCKER_AGE_SECONDS:
            continue
        if report.dry_run or run(["docker", "rm", "-v", parts[0]]).returncode == 0:
            report.containers.append(parts[1])

    for kind, extra_filters in (("network", []), ("volume", ["--filter", "dangling=true"])):
        resources = _docker_lines([kind, "ls", *extra_filters, "--filter", f"label={MANAGED_LABEL}",
                                   "--format", "{{.ID}} {{.Name}}" if kind == "network" else "{{.Name}}"])
        for entry in resources or []:
            fields = entry.split()
            resource_id, name = fields[0], fields[-1]
            project = _docker_lines([kind, "inspect", "--format", '{{index .Labels "com.docker.compose.project"}}',
                                     resource_id])
            if not project or not project[0].startswith("holdout-") or project[0] in active:
                continue
            if _created_age(kind, resource_id) < MIN_DOCKER_AGE_SECONDS:
                continue
            if kind == "network" and _docker_lines([kind, "inspect", "--format", "{{len .Containers}}", resource_id]) != ["0"]:
                continue
            if report.dry_run or run(["docker", kind, "rm", resource_id]).returncode == 0:
                (report.networks if kind == "network" else report.volumes).append(name)


def _gc_triage(main_root: Path, max_age_days: float, report: Report) -> None:
    candidates = [main_root / TRIAGE_FILE]
    base = main_root / WORKTREES_DIR
    if not base.is_symlink() and base.is_dir():
        candidates += [p / TRIAGE_FILE for p in base.iterdir() if not p.is_symlink() and p.is_dir()]
    now = time.time()
    for path in candidates:
        try:
            info = path.stat()
        except OSError:
            continue
        if now - info.st_mtime < max_age_days * DAY:
            continue
        tracked = run(["git", "ls-files", "--cached", "--", TRIAGE_FILE], cwd=path.parent)
        if tracked.returncode or tracked.stdout:
            continue
        report.triage_files.append(str(path.relative_to(main_root)))
        report.reclaimed_bytes += info.st_size
        if not report.dry_run:
            path.unlink(missing_ok=True)


def _gc_temp(report: Report) -> None:
    now = time.time()
    uid = os.getuid()
    base_dir = runtime_tmp_path()
    if not is_private_runtime_dir(base_dir):
        return
    for path in base_dir.iterdir():
        if not path.name.startswith(("sut-", "idx-")) or path.is_symlink() or not path.is_dir():
            continue
        try:
            info = path.lstat()
            if info.st_uid != uid or now - info.st_mtime < TEMP_MAX_AGE_SECONDS:
                continue
            size = _tree_size(path)
            if not report.dry_run:
                shutil.rmtree(path)
        except OSError as exc:
            report.notes.append(f"Could not clean {path}: {exc}")
            continue
        report.reclaimed_bytes += size
        report.temp_dirs.append(str(path))


def collect(dry_run: bool = False, max_age_days: float = DEFAULT_MAX_AGE_DAYS, repo: str | Path | None = None) -> Report:
    report = Report(dry_run=dry_run)
    try:
        main_root: Path | None = main_repo_root(repo)
    except FactoryError:
        main_root = None
        report.notes.append("Not inside a Git repository; skipped worktree and triage cleanup.")
    if main_root is not None:
        _gc_triage(main_root, max_age_days, report)
        _gc_worktrees(main_root, max_age_days, report)
    _gc_docker(report)
    _gc_temp(report)
    return report


def render(report: Report) -> str:
    verb = "Would remove" if report.dry_run else "Removed"
    lines = [f"factory-core gc{' (dry run)' if report.dry_run else ''}"]
    sections = (
        ("worktrees", report.worktrees), ("worktree branches", report.branches),
        ("containers", report.containers), ("Docker networks", report.networks),
        ("Docker volumes", report.volumes), ("triage files", report.triage_files),
        ("temp directories", report.temp_dirs),
    )
    for label, items in sections:
        lines.append(f"{verb} {len(items)} {label}" + (":" if items else ""))
        lines += [f"  - {item}" for item in items]
    if report.skipped:
        lines.append(f"Skipped {len(report.skipped)} worktrees:")
        lines += [f"  - {item}" for item in report.skipped]
    lines.append(f"{'Reclaimable' if report.dry_run else 'Reclaimed'} disk space: {_format_bytes(report.reclaimed_bytes)}")
    lines += [f"Note: {note}" for note in report.notes]
    return "\n".join(lines)


def main(dry_run: bool = False, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> int:
    print(render(collect(dry_run=dry_run, max_age_days=max_age_days)))
    return 0
