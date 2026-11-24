"""Capture tracked edits, deletions, and untracked files as one binary patch."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .common import INTERNAL_FILES, FactoryError, run, runtime_tmp_dir


@dataclass
class CandidatePatch:
    data: bytes
    files: list[str]


def export_patch(repo: str | Path) -> CandidatePatch:
    """Build the patch through a temporary index, leaving the real index untouched."""
    idx_dir = Path(tempfile.mkdtemp(prefix="idx-", dir=runtime_tmp_dir()))
    os.chmod(idx_dir, 0o700)
    index_path = str(idx_dir / "index")
    env = {**os.environ, "GIT_INDEX_FILE": index_path}

    def git_bytes(*args: str) -> bytes:
        proc = run(["git", *args], cwd=repo, env=env, timeout=120)
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()
            raise FactoryError(f"Patch export failed at 'git {args[0]}': {detail}")
        return proc.stdout

    diff_flags = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                  "--src-prefix=a/", "--dst-prefix=b/"]
    try:
        git_bytes("read-tree", "HEAD")
        git_bytes("add", "-A")
        # Drop factory bookkeeping from the temporary index. Exclude pathspecs cannot be
        # used here: git add rejects a pathspec that names an ignored file.
        git_bytes("reset", "-q", "HEAD", "--", *(f":(top){name}" for name in INTERNAL_FILES))
        data = git_bytes("diff", "--cached", "--binary", *diff_flags, "HEAD")
        names = git_bytes("diff", "--cached", "--name-only", "-z", "--no-renames", "HEAD")
    finally:
        shutil.rmtree(idx_dir, ignore_errors=True)
    files = [n.decode(errors="surrogateescape") for n in names.split(b"\0") if n]
    return CandidatePatch(data=data, files=files)
