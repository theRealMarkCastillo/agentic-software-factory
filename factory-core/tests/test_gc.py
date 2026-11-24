import os
import time

import pytest
from conftest import git, run_cli

from factory_core import common, fast_check, gc, worktree
from factory_core import state_manager as sm

OLD = time.time() - 30 * 86400


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    """Worktree and file tests must not depend on a Docker daemon."""
    monkeypatch.setattr(gc, "_gc_docker", lambda report: None)
    monkeypatch.setattr(gc, "_gc_temp", lambda report: None)


def age(path, when=OLD):
    for target in gc._activity_files(path):
        if target.exists():
            os.utime(target, (when, when))


def branches(repo):
    return git(repo, "branch", "--format=%(refname:short)").split()


def test_fresh_worktree_is_kept(repo):
    path = worktree.create("fresh")
    report = gc.collect(repo=repo)
    assert report.worktrees == [] and path.is_dir()


def test_stale_clean_worktree_is_removed_but_unmerged_branch_kept(repo, monkeypatch):
    path = worktree.create("old")
    (path / "src" / "feature.py").write_text("X = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "work")
    age(path)
    report = gc.collect(max_age_days=14, repo=repo)
    assert report.worktrees == [".worktrees/old (stale)"]
    assert not path.exists()
    assert "factory/old" in branches(repo)
    assert report.branches == []
    assert report.reclaimed_bytes > 0


def test_dirty_worktree_is_never_removed(repo):
    path = worktree.create("dirty")
    (path / "src" / "wip.py").write_text("unsaved work\n")
    age(path)
    report = gc.collect(max_age_days=0, repo=repo)
    assert path.is_dir() and (path / "src" / "wip.py").exists()
    assert report.worktrees == []
    assert report.skipped == [".worktrees/dirty (uncommitted changes)"]


def test_factory_bookkeeping_files_do_not_count_as_dirty(repo):
    path = worktree.create("bookkeeping")
    (path / ".factory-plan.md").write_text("# plan\n")
    (path / ".factory-triage.md").write_text("# triage\n")
    report = gc.collect(max_age_days=0, repo=repo)
    assert report.worktrees == [".worktrees/bookkeeping (stale)"]


def test_merged_worktree_and_branch_are_removed(repo):
    path = worktree.create("done")
    (path / "src" / "feature.py").write_text("X = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "feature")
    state = sm.load_state(path)
    sm.mark_verified(path, state, sm.compute_tree_hash(path), git(path, "rev-parse", "HEAD"))
    git(repo, "merge", "-q", "--ff-only", "factory/done")

    report = gc.collect(repo=repo)
    assert report.worktrees == [".worktrees/done (merged)"]
    assert report.branches == ["factory/done"]
    assert not path.exists() and "factory/done" not in branches(repo)
    assert (repo / "src" / "feature.py").exists()


def test_never_evaluated_worktree_is_not_treated_as_merged(repo):
    path = worktree.create("untouched")
    report = gc.collect(repo=repo)
    assert path.is_dir() and report.worktrees == []


def test_dry_run_reports_without_deleting(repo):
    path = worktree.create("old")
    age(path)
    triage = repo / ".factory-triage.md"
    triage.write_text("# old triage\n")
    os.utime(triage, (OLD, OLD))
    report = gc.collect(dry_run=True, max_age_days=0, repo=repo)
    assert report.worktrees == [".worktrees/old (stale)"]
    assert ".factory-triage.md" in report.triage_files
    assert path.is_dir() and triage.exists()
    text = gc.render(report)
    assert "dry run" in text and "Would remove 1 worktrees" in text and "Reclaimable disk space" in text


def test_expired_triage_files_are_removed_and_recent_ones_kept(repo):
    old_wt = worktree.create("a")
    new_wt = worktree.create("b")
    (old_wt / "src" / "keep.py").write_text("dirty so the worktree stays\n")
    (new_wt / "src" / "keep.py").write_text("dirty so the worktree stays\n")
    expired = old_wt / ".factory-triage.md"
    recent = new_wt / ".factory-triage.md"
    expired.write_text("# old\n")
    recent.write_text("# new\n")
    os.utime(expired, (OLD, OLD))
    report = gc.collect(max_age_days=7, repo=repo)
    assert report.triage_files == [".worktrees/a/.factory-triage.md"]
    assert not expired.exists() and recent.exists()


def test_max_age_days_threshold(repo):
    path = worktree.create("mid")
    age(path, time.time() - 3 * 86400)
    assert gc.collect(dry_run=True, max_age_days=5, repo=repo).worktrees == []
    assert gc.collect(dry_run=True, max_age_days=2, repo=repo).worktrees == [".worktrees/mid (stale)"]


def test_outside_a_repository_is_a_note_not_an_error(tmp_path, home):
    report = gc.collect(repo=tmp_path)
    assert any("Not inside a Git repository" in note for note in report.notes)


def test_render_summary_lists_every_category(repo):
    report = gc.Report(dry_run=False, worktrees=[".worktrees/x (stale)"], networks=["holdout-x_holdout-net"],
                       containers=["holdout-x-sut-1"], volumes=["holdout-x_data"], reclaimed_bytes=2048)
    text = gc.render(report)
    assert "Removed 1 worktrees:" in text and "Removed 1 Docker networks:" in text
    assert "Removed 1 containers:" in text and "Removed 1 Docker volumes:" in text
    assert "Reclaimed disk space: 2.0 KB" in text


def test_docker_cleanup_skips_active_and_young_resources(monkeypatch):
    monkeypatch.undo()
    removed = []
    listings = {
        "ps --filter label": ["holdout-live-aaa"],
        "ps -a": ["c1 holdout-dead-bbb-sut-1 holdout-dead-bbb", "c2 holdout-live-aaa-sut-1 holdout-live-aaa", "c3 other-app-1 other"],
        "network ls": ["dead-id holdout-dead-bbb_holdout-net", "live-id custom_svc_network",
                       "young-id holdout-young-ccc_holdout-net", "unknown-id holdout-other_holdout-net"],
        "network inspect": ["0"],
        "volume ls": ["holdout-dead-bbb_data"],
    }

    def fake_lines(args):
        if args[:2] in (["network", "ls"], ["volume", "ls"], ["ps", "-a"]):
            assert "label=io.factory-core.holdout=true" in args
        if args[1] == "inspect" and "com.docker.compose.project" in args[3]:
            projects = {
                "dead-id": "holdout-dead-bbb", "live-id": "holdout-live-aaa",
                "young-id": "holdout-young-ccc", "holdout-dead-bbb_data": "holdout-dead-bbb",
            }
            return [projects.get(args[-1], "")]
        joined = " ".join(args)
        for prefix, lines in listings.items():
            if joined.startswith(prefix):
                return lines
        return []

    monkeypatch.setattr(gc, "_docker_lines", fake_lines)
    monkeypatch.setattr(gc, "_created_age", lambda kind, name: 5.0 if "young" in name else 3600.0)

    class Done:
        returncode = 0

    monkeypatch.setattr(gc, "run", lambda args, **kw: removed.append(args[1:]) or Done())
    report = gc.Report(dry_run=False)
    gc._gc_docker(report)
    assert report.containers == ["holdout-dead-bbb-sut-1"]
    assert report.networks == ["holdout-dead-bbb_holdout-net"]
    assert report.volumes == ["holdout-dead-bbb_data"]
    assert ["network", "rm", "dead-id"] in removed
    assert ["network", "rm", "live-id"] not in removed


def test_temp_cleanup_only_removes_private_staging_directories(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setenv("FACTORY_TMPDIR", str(tmp_path))
    shared = tmp_path / "sut-other-tool"
    shared.mkdir()
    os.utime(shared, (OLD, OLD))
    report = gc.Report(dry_run=True)
    gc._gc_temp(report)
    assert not common.runtime_tmp_path().exists()
    base = common.runtime_tmp_dir()
    old = base / "idx-test"
    old.mkdir()
    os.utime(old, (OLD, OLD))
    link = base / "sut-link"
    link.symlink_to(shared, target_is_directory=True)
    fresh = base / "sut-fresh"
    fresh.mkdir()
    report = gc.Report(dry_run=False)
    gc._gc_temp(report)
    assert report.temp_dirs == [str(old)]
    assert not old.exists()
    assert shared.is_dir() and link.is_symlink() and fresh.is_dir()


@pytest.mark.parametrize("destination", [".factory-plan.md", ".factory-triage.md"])
def test_staged_rename_into_bookkeeping_is_dirty(repo, destination):
    path = worktree.create("renamed")
    git(path, "mv", "README.md", destination)
    assert common.porcelain_paths(path) == ["README.md"]
    assert common.porcelain_paths(path, include_internal=True) == [destination, "README.md"]
    assert fast_check.changed_files(path) == ["README.md"]
    report = gc.collect(max_age_days=0, repo=repo)
    assert (path / destination).read_text() == "demo\n" and not report.worktrees


def test_unknown_git_status_runs_fast_checks_and_retains_worktree(repo, monkeypatch):
    path = worktree.create("unknown")
    original_run = common.run

    def fail_status(args, **kwargs):
        if args[:2] == ["git", "status"]:
            raise OSError("git status failed")
        return original_run(args, **kwargs)

    monkeypatch.setattr(common, "run", fail_status)
    planned = []
    monkeypatch.setattr(fast_check, "plan_steps", lambda root: (planned.append(root) or [], []))
    assert fast_check.main() == 0
    assert planned == [repo]
    report = gc.collect(max_age_days=0, repo=repo)
    assert path.is_dir() and not report.worktrees


def test_docker_unavailable_is_reported(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(gc, "_docker_lines", lambda args: None)
    report = gc.Report(dry_run=False)
    gc._gc_docker(report)
    assert any("Docker is not available" in note for note in report.notes)


def test_cli_flags(repo):
    worktree.create("cli")
    result = run_cli(["gc", "--dry-run", "--max-age-days", "0"], cwd=repo, env={"DOCKER_HOST": "unix:///nonexistent.sock"})
    assert result.returncode == 0
    assert "factory-core gc (dry run)" in result.stdout and ".worktrees/cli (stale)" in result.stdout
    assert (repo / ".worktrees" / "cli").is_dir()


@pytest.mark.parametrize("stamp,expected", [
    ("2026-11-23T18:36:23-08:00", 1795487783),
    ("2026-11-23 18:34:12.285010647 -0800 PST", 1795487652),
    ("2026-11-24T02:36:23.123456Z", 1795487783),
    ("2026-11-24T02:36:23", 1795487783),
    ("0001-01-01T00:00:00Z", None),
    ("not a time", None),
])
def test_parse_docker_time_formats(stamp, expected):
    parsed = gc.parse_docker_time(stamp)
    if expected is None:
        assert parsed is None or parsed < 0
    else:
        assert int(parsed) == expected


def test_unknown_creation_time_keeps_the_resource(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(gc, "_docker_lines", lambda args: ["garbage"])
    assert gc._created_age("network", "x") == 0.0


def test_failed_gate_worktree_at_base_sha_is_not_deleted_as_merged(repo):
    path = worktree.create("failed-base")
    state = sm.load_state(path)
    sm.record_failure(path, state, git(path, "rev-parse", "HEAD"), "boom", ["src/app.py"], "t.py::test_a")
    report = gc.collect(repo=repo)
    assert path.is_dir() and "factory/failed-base" in branches(repo)
    assert report.worktrees == []


def test_untouched_verified_worktree_with_no_commits_is_not_deleted_as_merged(repo):
    path = worktree.create("untouched-pass")
    state = sm.load_state(path)
    sm.mark_verified(path, state, sm.compute_tree_hash(path), git(path, "rev-parse", "HEAD"))
    report = gc.collect(repo=repo)
    assert path.is_dir() and "factory/untouched-pass" in branches(repo)
    assert report.worktrees == []


def test_reverted_worktree_after_pass_is_not_deleted_as_merged(repo):
    path = worktree.create("reverted")
    (path / "src" / "feature.py").write_text("X = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "feature")
    (path / "src" / "app.py").write_text("VALUE = 2\n")
    state = sm.load_state(path)
    sm.mark_verified(path, state, sm.compute_tree_hash(path), git(path, "rev-parse", "HEAD"))
    (path / "src" / "app.py").write_text("VALUE = 1\n")
    git(repo, "merge", "-q", "--ff-only", "factory/reverted")
    report = gc.collect(repo=repo)
    assert path.is_dir() and "factory/reverted" in branches(repo)
    assert report.worktrees == []


def test_worktree_committed_after_passing_uncommitted_is_cleaned_up_when_merged(repo):
    path = worktree.create("post-pass-commit")
    (path / "src" / "feature.py").write_text("X = 1\n")
    state = sm.load_state(path)
    # Verified while edits were uncommitted (last_evaluated_sha == pre-commit HEAD).
    sm.mark_verified(path, state, sm.compute_tree_hash(path), git(path, "rev-parse", "HEAD"), "oracle")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "commit verified tree")
    git(repo, "merge", "-q", "--ff-only", "factory/post-pass-commit")

    report = gc.collect(repo=repo)
    assert report.worktrees == [".worktrees/post-pass-commit (merged)"]
    assert report.branches == ["factory/post-pass-commit"]
    assert not path.exists() and "factory/post-pass-commit" not in branches(repo)
