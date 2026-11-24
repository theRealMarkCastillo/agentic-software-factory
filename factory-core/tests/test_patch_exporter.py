import os
import shutil
import subprocess
from unittest.mock import Mock

import pytest
from conftest import git

from factory_core import common, compose_runner
from factory_core.patch_exporter import compute_candidate_tree, export_patch


@pytest.fixture
def changed(repo):
    """Tracked edit, tracked deletion, untracked text file, and untracked binary file."""
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    (repo / "README.md").unlink()
    (repo / "src" / "new_module.py").write_text("def added():\n    return True\n")
    (repo / "assets").mkdir()
    (repo / "assets" / "blob.bin").write_bytes(bytes(range(256)) * 4)
    return repo


def test_captures_tracked_untracked_and_deleted_files(changed):
    patch = export_patch(changed)
    assert sorted(patch.files) == ["README.md", "assets/blob.bin", "src/app.py", "src/new_module.py"]
    text = patch.data.decode(errors="replace")
    assert "+VALUE = 2" in text
    assert "new file mode" in text and "src/new_module.py" in text
    assert "deleted file mode" in text
    assert "GIT binary patch" in text


def test_real_index_and_worktree_are_not_mutated(changed):
    git(changed, "add", "src/app.py")  # Something already staged by the agent.
    before_status = git(changed, "status", "--porcelain")
    before_staged = git(changed, "diff", "--cached", "--name-only")
    before_index = (changed / ".git" / "index").read_bytes()

    export_patch(changed)

    assert git(changed, "status", "--porcelain") == before_status
    assert git(changed, "diff", "--cached", "--name-only") == before_staged == "src/app.py"
    assert (changed / ".git" / "index").read_bytes() == before_index


def test_temporary_index_is_removed(changed):
    tmp_dir = common.runtime_tmp_dir()
    before = {name for name in os.listdir(tmp_dir) if name.startswith("idx-")}
    export_patch(changed)
    assert {name for name in os.listdir(tmp_dir) if name.startswith("idx-")} == before


def test_clean_tree_yields_empty_patch(repo):
    patch = export_patch(repo)
    assert patch.data == b"" and patch.files == []


def test_ignored_and_internal_files_are_excluded(repo):
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("ignored")
    (repo / ".factory-state.json").write_text('{"attempts": 0}')
    (repo / ".factory-plan.md").write_text("# plan")
    (repo / ".factory-triage.md").write_text("# triage")
    patch = export_patch(repo)
    assert patch.files == [".gitignore"]


def test_works_from_a_linked_worktree(repo):
    worktree = repo / ".worktrees" / "t1"
    git(repo, "worktree", "add", "-q", "-b", "factory/t1", str(worktree), "HEAD")
    (worktree / "src" / "feature.py").write_text("X = 1\n")
    assert export_patch(worktree).files == ["src/feature.py"]


def test_linked_worktree_exports_unstaged_binary_bytes_without_changing_either_index(repo):
    original = repo / "asset.bin"
    original.write_bytes(b"\0original\xff")
    git(repo, "add", "asset.bin")
    git(repo, "commit", "-qm", "Add binary asset")
    worktree = repo / ".worktrees" / "binary"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")

    binary = worktree / "asset.bin"
    binary.write_bytes(b"\0staged\xff")
    git(worktree, "add", "asset.bin")
    binary.write_bytes(b"\0unstaged\xff")
    (worktree / "src" / "untracked.bin").write_bytes(b"\0new\xff")
    (repo / "README.md").write_text("staged in the primary checkout\n")
    git(repo, "add", "README.md")
    indexes = [
        path / git(path, "rev-parse", "--git-path", "index")
        for path in (repo, worktree)
    ]
    before = [index.read_bytes() for index in indexes]

    patch = export_patch(worktree)

    assert patch.files == ["asset.bin", "src/untracked.bin"]
    assert [index.read_bytes() for index in indexes] == before
    assert git(worktree, "status", "--porcelain").splitlines() == ["MM asset.bin", "?? src/untracked.bin"]
    sut = compose_runner.prepare_sut(worktree, patch.data)
    try:
        assert (sut / "asset.bin").read_bytes() == b"\0unstaged\xff"
        assert (sut / "src" / "untracked.bin").read_bytes() == b"\0new\xff"
        assert (sut / "README.md").read_text() == "demo\n"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_exports_untracked_symlink_as_a_link(repo, tmp_path):
    target = tmp_path / "outside.bin"
    target.write_bytes(b"\0outside the checkout\xff")
    (repo / "asset-link").symlink_to(target)

    patch = export_patch(repo)

    assert patch.files == ["asset-link"]
    sut = compose_runner.prepare_sut(repo, patch.data)
    try:
        assert os.readlink(sut / "asset-link") == str(target)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_patch_reproduces_the_working_tree(changed):
    patch = export_patch(changed)
    sut = compose_runner.prepare_sut(changed, patch.data)
    try:
        assert sut.name.startswith("sut-") and sut.parent == common.runtime_tmp_dir().resolve()
        assert sut.stat().st_mode & 0o777 == 0o700
        assert (sut / "src" / "app.py").read_text() == "VALUE = 2\n"
        assert (sut / "src" / "new_module.py").read_text().startswith("def added")
        assert (sut / "assets" / "blob.bin").read_bytes() == bytes(range(256)) * 4
        assert not (sut / "README.md").exists()
        assert (sut / "Dockerfile").exists()
        assert not (sut / ".git").exists()
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_prepare_sut_with_empty_patch_is_plain_head(repo):
    sut = compose_runner.prepare_sut(repo, b"")
    try:
        assert (sut / "src" / "app.py").read_text() == "VALUE = 1\n"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_filenames_with_spaces_and_unicode(repo):
    (repo / "src" / "my file é.py").write_text("A = 1\n")
    patch = export_patch(repo)
    assert patch.files == ["src/my file é.py"]
    sut = compose_runner.prepare_sut(repo, patch.data)
    try:
        assert (sut / "src" / "my file é.py").read_text() == "A = 1\n"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_project_name_is_compose_safe():
    assert compose_runner.project_name("Task.Verification_01", "ABC1234") == "holdout-task-verification_01-abc1234"
    assert compose_runner.project_name("///", "abc1234") == "holdout-task-abc1234"


def test_forced_ignored_file_uses_worktree_bytes_and_preserves_real_index(repo):
    (repo / ".gitignore").write_text("*.bin\n.factory-state.json\n")
    (repo / ".factory-state.json").write_text("private")
    forced = repo / "allowed.bin"
    forced.write_bytes(b"staged")
    git(repo, "add", "-f", "--", forced.name)
    forced.write_bytes(b"current")
    (repo / "ignored.bin").write_bytes(b"ignore")
    with_index = (repo / ".git/index").read_bytes()

    patch = export_patch(repo)
    assert patch.files == [".gitignore", "allowed.bin"]
    assert git(repo, "show", f"{patch.tree_hash}:allowed.bin") == "current"
    assert git(repo, "show", ":allowed.bin") == "staged"
    assert (repo / ".git/index").read_bytes() == with_index


def test_read_only_tree_hash_keeps_objects_and_index_unchanged(repo, monkeypatch):
    (repo / "new.txt").write_text("new candidate")
    index = (repo / ".git/index").read_bytes()
    before_objects = sorted((repo / ".git/objects").rglob("*"))
    monkeypatch.setenv("FACTORY_TMPDIR", str(repo / "no-runtime-cache"))
    tree = compute_candidate_tree(repo)
    assert sorted((repo / ".git/objects").rglob("*")) == before_objects
    assert (repo / ".git/index").read_bytes() == index
    assert not (repo / "no-runtime-cache").exists()
    assert export_patch(repo).tree_hash == tree


@pytest.mark.parametrize("legacy_bytes", [False, True])
def test_archive_attributes_keep_files_and_literal_contents(repo, legacy_bytes):
    (repo / ".gitattributes").write_text("README.md export-ignore\nsrc/app.py export-subst\n")
    (repo / "src/app.py").write_text('REV = "$Format:%H$"\n')
    (repo / ".factory-plan.md").write_text("private")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Set archive attributes and track internal file")
    (repo / "README.md").write_text("include this edit\n")
    patch = export_patch(repo)

    assert patch.files == ["README.md"]
    sut = compose_runner.prepare_sut(repo, patch.data if legacy_bytes else patch)
    try:
        assert (sut / "README.md").read_text() == "include this edit\n"
        assert (sut / "src/app.py").read_text() == 'REV = "$Format:%H$"\n'
        assert not (sut / ".factory-plan.md").exists()
    finally:
        shutil.rmtree(sut)


def test_build_uses_captured_contents_after_worktree_and_head_advance(repo):
    (repo / "src/app.py").write_text("captured\n")
    patch = export_patch(repo)
    (repo / "src/app.py").write_text("changed again\n")
    git(repo, "add", "src/app.py")
    git(repo, "commit", "-qm", "Advance HEAD")
    assert patch.head_sha != git(repo, "rev-parse", "HEAD")
    sut = compose_runner.prepare_sut(repo, patch)
    try:
        assert (sut / "src/app.py").read_text() == "captured\n"
    finally:
        shutil.rmtree(sut)


@pytest.mark.parametrize(("inspect_code", "started_at", "ran"), [
    (0, b"2026-11-23T00:00:00.000000000Z", True),
    (0, b"0001-01-01T00:00:00Z", False),
    (1, b"", False),
])
def test_timeout_checks_verifier_and_cleans_only_its_project(tmp_path, monkeypatch, inspect_code, started_at, ran):
    holdout = tmp_path / "holdout"
    holdout.mkdir()
    downs = []

    def prepare(*_):
        sut = tmp_path / "sut"
        sut.mkdir()
        (sut / "Dockerfile").touch()
        return sut

    def run(args, **kwargs):
        if "down" in args:
            downs.append(args[args.index("-p") + 1])
            return subprocess.CompletedProcess(args, 0, b"")
        if "ps" in args:
            return subprocess.CompletedProcess(args, 0, b"verifier-id")
        assert args[:2] == ["docker", "inspect"]
        return subprocess.CompletedProcess(args, inspect_code, started_at)

    def launch(*args, **kwargs):
        proc = Mock(returncode=-9, pid=123)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("compose", 1), (b"", None)]
        return proc

    monkeypatch.setattr(compose_runner, "prepare_sut", prepare)
    monkeypatch.setattr(compose_runner.subprocess, "Popen", launch)
    monkeypatch.setattr(compose_runner.os, "killpg", lambda *args: None)
    monkeypatch.setattr(compose_runner, "run", run)
    first, second = [compose_runner.run_holdout(tmp_path, b"", holdout, "task", "abc", timeout=1) for _ in range(2)]
    assert first.exit_code == second.exit_code == 124
    assert first.timed_out and second.timed_out
    assert first.verifier_ran is ran and second.verifier_ran is ran
    assert first.project != second.project and downs == [first.project, second.project]
