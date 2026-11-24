import json
import os
import re

import pytest
from conftest import git

from factory_core import state_manager as sm
from factory_core.common import FactoryError


def test_initial_state_matches_schema(repo):
    state = sm.init_state(repo, "task-1")
    on_disk = json.loads((repo / ".factory-state.json").read_text())
    assert on_disk == state
    assert list(state) == ["task_id", "attempts", "max_retries", "verified_tree_hash",
                           "last_evaluated_sha", "rejected_approaches", "updated_at"]
    assert state["task_id"] == "task-1"
    assert (state["attempts"], state["max_retries"]) == (0, 3)
    assert state["verified_tree_hash"] is None and state["last_evaluated_sha"] is None
    assert state["rejected_approaches"] == []
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", state["updated_at"])


def test_init_state_preserves_existing_state(repo):
    state = sm.init_state(repo, "task-1")
    sm.record_failure(repo, state, "abc", "boom", ["a.py"], "t.py::test_a")
    again = sm.init_state(repo, "other-name")
    assert again["task_id"] == "task-1" and again["attempts"] == 1


def test_load_missing_state_is_none(repo):
    assert sm.load_state(repo) is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"attempts": 0}', '{"attempts": "x", "max_retries": 3, "rejected_approaches": []}'])
def test_corrupt_state_raises_with_remediation(repo, content):
    (repo / ".factory-state.json").write_text(content)
    with pytest.raises(FactoryError) as excinfo:
        sm.load_state(repo)
    assert "init-state" in excinfo.value.remediation


def test_save_is_atomic_and_leaves_no_temp_files(repo):
    sm.init_state(repo, "t")
    assert [n for n in os.listdir(repo) if n.startswith(".factory-state-")] == []


# --- tree hash ------------------------------------------------------------------

def test_tree_hash_is_git_tree_id_and_stable(repo):
    first = sm.compute_tree_hash(repo)
    assert first == git(repo, "rev-parse", "HEAD^{tree}")
    assert sm.compute_tree_hash(repo) == first


def test_tree_hash_changes_on_tracked_edit(repo):
    before = sm.compute_tree_hash(repo)
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    assert sm.compute_tree_hash(repo) != before


def test_tree_hash_changes_on_untracked_file_and_reverts(repo):
    before = sm.compute_tree_hash(repo)
    extra = repo / "src" / "new.py"
    extra.write_text("X = 1\n")
    assert sm.compute_tree_hash(repo) != before
    extra.unlink()
    assert sm.compute_tree_hash(repo) == before


def test_tree_hash_changes_on_deletion_and_rename(repo):
    before = sm.compute_tree_hash(repo)
    (repo / "README.md").rename(repo / "README.rst")
    renamed = sm.compute_tree_hash(repo)
    assert renamed != before
    (repo / "README.rst").unlink()
    assert sm.compute_tree_hash(repo) not in (before, renamed)


def test_tree_hash_changes_on_executable_bit(repo):
    before = sm.compute_tree_hash(repo)
    (repo / "src" / "app.py").chmod(0o755)
    assert sm.compute_tree_hash(repo) != before


def test_tree_hash_ignores_gitignored_and_factory_files(repo):
    (repo / ".gitignore").write_text("*.log\n")
    before = sm.compute_tree_hash(repo)
    (repo / "debug.log").write_text("noise")
    (repo / ".factory-state.json").write_text("{}")
    (repo / ".factory-plan.md").write_text("plan")
    (repo / ".factory-triage.md").write_text("triage")
    assert sm.compute_tree_hash(repo) == before


def test_tree_hash_is_independent_of_staging(repo):
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    unstaged = sm.compute_tree_hash(repo)
    git(repo, "add", "-A")
    assert sm.compute_tree_hash(repo) == unstaged


def test_tree_hash_distinguishes_content_moved_between_files(repo):
    (repo / "a.txt").write_text("one")
    (repo / "b.txt").write_text("two")
    first = sm.compute_tree_hash(repo)
    (repo / "a.txt").write_text("two")
    (repo / "b.txt").write_text("one")
    assert sm.compute_tree_hash(repo) != first


# --- verification cache ---------------------------------------------------------

def test_mark_verified_caches_until_the_tree_changes(repo):
    state = sm.init_state(repo, "t")
    tree = sm.compute_tree_hash(repo)
    assert not sm.is_verified(state, tree)
    sm.mark_verified(repo, state, tree, "headsha")
    reloaded = sm.load_state(repo)
    assert sm.is_verified(reloaded, sm.compute_tree_hash(repo))
    assert reloaded["last_evaluated_sha"] == "headsha"
    (repo / "src" / "app.py").write_text("VALUE = 3\n")
    assert not sm.is_verified(reloaded, sm.compute_tree_hash(repo))


def test_null_hash_never_counts_as_verified():
    assert not sm.is_verified(sm.new_state("t"), "abc")


# --- rejected approaches and circuit breaker ------------------------------------

def test_record_failure_appends_structured_entry(repo):
    state = sm.init_state(repo, "t")
    sm.mark_verified(repo, state, "oldhash", "sha0")
    attempts = sm.record_failure(repo, state, "sha1", "assert 500 == 200", ["src/app.py", "src/new.py"], "test_api.py::test_health")
    assert attempts == 1
    reloaded = sm.load_state(repo)
    assert reloaded["attempts"] == 1
    assert reloaded["verified_tree_hash"] is None
    assert reloaded["last_evaluated_sha"] == "sha1"
    assert reloaded["rejected_approaches"] == [{
        "attempt": 1,
        "summary": "assert 500 == 200",
        "files_touched": ["src/app.py", "src/new.py"],
        "failed_contract": "test_api.py::test_health",
    }]


def test_rejected_approaches_accumulate_in_order(repo):
    state = sm.init_state(repo, "t")
    for index in range(1, 4):
        sm.record_failure(repo, state, f"sha{index}", f"failure {index}", [], None)
    reloaded = sm.load_state(repo)
    assert [r["attempt"] for r in reloaded["rejected_approaches"]] == [1, 2, 3]
    assert [r["summary"] for r in reloaded["rejected_approaches"]] == ["failure 1", "failure 2", "failure 3"]


def test_circuit_breaker_trips_at_max_retries(repo):
    state = sm.init_state(repo, "t")
    for expected_tripped in (False, False, True):
        sm.record_failure(repo, state, "sha", "x", [], None)
        assert sm.breaker_tripped(state) is expected_tripped
    assert state["attempts"] == state["max_retries"] == 3


def test_circuit_breaker_honours_custom_limit_and_stays_tripped():
    state = sm.new_state("t", max_retries=1)
    assert not sm.breaker_tripped(state)
    state["attempts"] = 1
    assert sm.breaker_tripped(state)
    state["attempts"] = 5
    assert sm.breaker_tripped(state)


def test_breaker_banner_text():
    banner = sm.breaker_banner(sm.new_state("t")).splitlines()
    assert banner[0] == "=" * 80 == banner[-1]
    assert banner[1] == "CIRCUIT BREAKER TRIPPED: Maximum verification attempts (3) reached."
    assert banner[2] == "Halting automated retry loop. Passing control back to human operator."


def test_concurrent_tasks_preserve_recovery_entries(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ledger = tmp_path / sm.LEDGER_FILE
    monkeypatch.setattr(sm, "_ledger_path", lambda root: ledger)
    start = Barrier(8)

    def update(index):
        start.wait()
        sm._ledger_update(tmp_path / str(index), {"attempts": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(8)))
    assert sorted(item["attempts"] for item in sm._ledger_read(ledger).values()) == list(range(8))


def test_existing_worktree_keeps_its_legacy_creation_baseline(repo):
    import hashlib

    legacy = hashlib.sha256()
    for name in ("Dockerfile", "README.md", "src/app.py"):
        legacy.update(b"P\0" + name.encode() + b"\0F\0")
        legacy.update(hashlib.sha256((repo / name).read_bytes()).digest())
    state = {"base_tree_hash": legacy.hexdigest()}
    assert sm.is_pristine(repo, state, sm.compute_tree_hash(repo))
    (repo / "src/app.py").write_text("new work")
    assert not sm.is_pristine(repo, state, sm.compute_tree_hash(repo))
