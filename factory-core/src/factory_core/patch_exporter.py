"""Capture tracked edits, deletions, and untracked files as one Git tree."""

from __future__ import annotations

import io
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .common import INTERNAL_FILES, FactoryError, run, runtime_tmp_dir


@dataclass(frozen=True)
class CandidatePatch:
    data: bytes
    files: list[str]
    tree_hash: str
    head_sha: str


def _git(
    repo: str | Path,
    args: list[str],
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    # Temporary index maintenance must not run working-copy hooks or create a
    # shared index in the real repository.
    cmd = [
        "git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
        "-c", "core.splitIndex=false", "-c", "core.fsmonitor=false", *args,
    ]
    proc = run(cmd, cwd=repo, env=env, input_bytes=input_bytes, timeout=120)
    if proc.returncode:
        detail = proc.stderr.decode(errors="replace").strip()
        denied = re.search(r'open\("(.*?)"\): Permission denied', detail)
        if denied:
            raise FactoryError(f"Cannot read {denied[1]}: Permission denied", "Fix its permissions, or remove or ignore the file.")
        raise FactoryError(f"Candidate snapshot failed at 'git {args[0]}': {detail}")
    return proc.stdout


@contextmanager
def _temporary_index(repo: str | Path, private_objects: bool = False) -> Iterator[dict[str, str]]:
    tmp_base = Path(tempfile.gettempdir()) if private_objects else runtime_tmp_dir()
    if tmp_base.resolve().is_relative_to(Path(repo).resolve()):
        tmp_base = Path("/tmp")
        if tmp_base.resolve().is_relative_to(Path(repo).resolve()):
            raise FactoryError("Candidate snapshots require a temporary directory outside the checkout.")
    with tempfile.TemporaryDirectory(prefix="idx-", dir=tmp_base) as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if private_objects:
            objects = _git(repo, ["rev-parse", "--path-format=absolute", "--git-path", "objects"]).rstrip(b"\n")
            temporary_objects = Path(tmp) / "objects"
            info = temporary_objects / "info"
            info.mkdir(parents=True)
            (info / "alternates").write_bytes(objects + b"\n")
            env["GIT_OBJECT_DIRECTORY"] = str(temporary_objects)
        yield env


def _capture(repo: str | Path, env: dict[str, str]) -> str:
    # Seed from the real index to retain force-added ignored files. Import entries
    # without cached stat data so git add reads the current worktree contents.
    entries = _git(repo, ["ls-files", "--stage", "-z"])
    _git(repo, ["update-index", "-z", "--index-info"], env, entries)
    # A literal exclusion naming an ignored file makes git add fail. A glob
    # matching its initial dot avoids that special handling.
    excluded = [f":(top,exclude,glob)[.]{name[1:]}" for name in INTERNAL_FILES]
    _git(repo, ["add", "-A", "--", ".", *excluded], env)
    _git(repo, ["rm", "-rf", "--cached", "--ignore-unmatch", "--", *INTERNAL_FILES], env)
    return _git(repo, ["write-tree"], env).decode().strip()


def compute_candidate_tree(repo: str | Path) -> str:
    """Compute the candidate Git tree without storing objects in the repository."""
    with _temporary_index(repo, private_objects=True) as env:
        return _capture(repo, env)


def export_patch(repo: str | Path) -> CandidatePatch:
    """Pin the candidate tree and base commit before deriving the patch and paths."""
    with _temporary_index(repo) as env:
        head = _git(repo, ["rev-parse", "--verify", "HEAD"], env).decode().strip()
        tree = _capture(repo, env)
        paths = ["--", ".", *(f":(top,exclude){name}" for name in INTERNAL_FILES)]
        flags = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/"]
        data = _git(repo, ["diff", "--binary", *flags, head, tree, *paths], env)
        names = _git(repo, ["diff", "--name-only", "-z", "--no-renames", head, tree, *paths], env)
    files = [os.fsdecode(name) for name in names.split(b"\0") if name]
    return CandidatePatch(data=data, files=files, tree_hash=tree, head_sha=head)


def materialize_candidate(repo: str | Path, patch: CandidatePatch | bytes, dest: Path) -> None:
    """Write raw tree blobs so archive attributes and checkout filters cannot alter build inputs."""
    with _temporary_index(repo, private_objects=isinstance(patch, bytes)) as env:
        if isinstance(patch, bytes):
            _git(repo, ["read-tree", "HEAD"], env)
            if patch.strip():
                _git(repo, ["apply", "--cached", "--binary", "--whitespace=nowarn", "-"], env, patch)
            _git(repo, ["rm", "-rf", "--cached", "--ignore-unmatch", "--", *INTERNAL_FILES], env)
            tree = _git(repo, ["write-tree"], env).decode().strip()
        else:
            tree = patch.tree_hash

        listing = _git(repo, ["ls-tree", "-rz", tree], env)
        files = []
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            relative = Path(os.fsdecode(name))
            if relative.is_absolute() or ".." in relative.parts or ".git" in (part.lower() for part in relative.parts):
                raise FactoryError("Invalid path in the captured candidate tree.")
            path = dest / relative
            # Create every directory before creating any symlinks. Exclusive file
            # creation below also catches names that alias on the staging filesystem.
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == b"commit":  # A gitlink has no checked-out submodule contents.
                path.mkdir(parents=True, exist_ok=True)
            else:
                files.append((path, mode, oid))

        objects = b"".join(oid + b"\n" for _, _, oid in files)
        blobs = io.BytesIO(_git(repo, ["cat-file", "--batch"], env, objects))
        for path, mode, oid in files:
            header = blobs.readline().rstrip(b"\n").split()
            if len(header) != 3 or header[:2] != [oid, b"blob"]:
                raise FactoryError("Could not read a file from the captured candidate tree.")
            content = blobs.read(int(header[2]))
            if blobs.read(1) != b"\n":
                raise FactoryError("Incomplete file in the captured candidate tree.")
            if mode == b"120000":
                path.symlink_to(os.fsdecode(content))
            else:
                with path.open("xb") as handle:
                    handle.write(content)
                    os.fchmod(handle.fileno(), 0o755 if mode == b"100755" else 0o644)
