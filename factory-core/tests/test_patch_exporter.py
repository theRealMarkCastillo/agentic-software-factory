import os
import shutil

import pytest

from factory_core import compose_runner
from factory_core.patch_exporter import export_patch

from conftest import git


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
    before = {name for name in os.listdir("/tmp") if name.startswith("idx-")}
    export_patch(changed)
    assert {name for name in os.listdir("/tmp") if name.startswith("idx-")} == before


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


def test_patch_reproduces_the_working_tree(changed):
    patch = export_patch(changed)
    sut = compose_runner.prepare_sut(changed, patch.data)
    try:
        assert sut.name.startswith("sut-") and str(sut.parent) == "/tmp"
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
