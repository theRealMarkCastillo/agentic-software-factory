"""PreToolUse guard: canonical path and shell token inspection.

This is a deterministic tripwire, not a sandbox. It runs as the same OS user as
the agent, so a sufficiently obfuscated command (for example a path assembled at
runtime from encoded bytes) cannot be detected by static inspection.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import sys
from typing import Any, Iterator

from .common import EXIT_BLOCK, EXIT_OK, STATE_FILE, TRIAGE_FILE, extract_tool_call, hook_cwd, is_antigravity_payload

PATH_KEYS = {
    "file_path", "path", "target", "filepath", "cwd", "notebook_path",
    "absolutepath", "targetfile", "searchpath", "directorypath",
    "searchdirectory", "workdir", "dir", "directory",
}
COMMAND_KEYS = {"command", "commandline", "cmd"}
SEARCH_TOOLS = {"grep", "glob", "grep_search", "find_by_name", "list_dir"}
WRITE_TOOLS = {
    "write", "edit", "multiedit", "notebookedit", "apply_patch",
    "write_file", "edit_file",
    "write_to_file", "replace_file_content", "multi_replace_file_content",
}
CONTAINER_CLIS = {"docker", "docker-compose", "podman", "nerdctl"}
COMMAND_WRAPPERS = {"sudo", "env", "command", "exec", "nohup", "time", "xargs", "nice"}
RECURSIVE_CLIS = {"find", "rg", "ag", "fd", "tree", "tar", "zip", "rsync", "du"}
RECURSIVE_WITH_FLAG = {"grep", "egrep", "fgrep", "ls", "cp", "chmod", "chown", "rm"}
STATE_WRITERS = re.compile(
    r"(>|\btee\b|\bsed\s+-i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bdd\b|\bln\b|\bpython\d*\b|\bnode\b|\bperl\b|\bruby\b)"
)
PATCH_HEADER = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE)
SHELL_OPERATORS = set("();<>|&")
RAW_MARKERS = (
    (".factory-holdouts", "sealed hold-out directory ~/.factory-holdouts"),
    ("docker.sock", "Docker socket"),
    (".git/hooks", "Git hooks directory"),
)


def protected_paths() -> list[tuple[str, str]]:
    home = os.path.expanduser("~")
    paths = [(os.path.join(home, ".factory-holdouts"), "sealed hold-out directory ~/.factory-holdouts")]
    sockets = ["/var/run/docker.sock", "/run/docker.sock"]
    # Desktop runtimes keep the real socket under the home directory.
    sockets += [os.path.join(home, rel) for rel in (
        ".docker/run/docker.sock", ".orbstack/run/docker.sock", ".colima/default/docker.sock",
        ".rd/docker.sock", ".local/share/containers/podman/machine/podman.sock",
    )]
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host.startswith("unix://"):
        sockets.append(docker_host[len("unix://"):])
    return paths + [(socket, "Docker socket") for socket in sockets]


def _fold(path: str) -> str:
    # APFS and NTFS are case-insensitive by default; compare conservatively.
    return os.path.normcase(path).casefold()


def _resolve(target: str, cwd: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(target))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.realpath(expanded)


def _is_within(path: str, parent: str) -> bool:
    path, parent = _fold(path), _fold(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _in_git_hooks(resolved: str) -> bool:
    parts = _fold(resolved).split(os.sep)
    return any(parts[i] == ".git" and parts[i + 1] == "hooks" for i in range(len(parts) - 1))


def violation_for(target: str, cwd: str, recursive: bool = False) -> str | None:
    """Return the protected resource a target touches, or None."""
    if not target or "\0" in target:
        return None
    candidates = [_resolve(target, cwd)]
    expanded = os.path.expanduser(os.path.expandvars(target))
    if glob.has_magic(expanded):
        pattern = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
        candidates.extend(os.path.realpath(m) for m in glob.glob(pattern, include_hidden=True))
    for resolved in candidates:
        if _in_git_hooks(resolved):
            return "Git hooks directory"
        if os.path.basename(resolved) == "docker.sock":
            return "Docker socket"
        for protected, label in protected_paths():
            real = os.path.realpath(protected)
            for candidate in {protected, real}:
                if _is_within(resolved, candidate):
                    return label
                if recursive and _is_within(candidate, resolved):
                    return f"{label} (recursive access through a parent directory)"
    return None


def _walk_fields(value: Any, key: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _walk_fields(child, str(child_key).lower())
    elif isinstance(value, list):
        for child in value:
            yield from _walk_fields(child, key)
    elif isinstance(value, str):
        yield key, value


def _tokenize(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _is_operator(token: str) -> bool:
    return bool(token) and set(token) <= SHELL_OPERATORS


def _command_words(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Split tokens into (program, args) segments, skipping env and wrappers."""
    segments: list[list[str]] = [[]]
    for token in tokens:
        if _is_operator(token):
            segments.append([])
        else:
            segments[-1].append(token)
    words: list[tuple[str, list[str]]] = []
    for segment in segments:
        index = 0
        while index < len(segment):
            word = segment[index]
            base = os.path.basename(word)
            if re.match(r"^[A-Za-z_]\w*=", word) or base in COMMAND_WRAPPERS or word.startswith("-"):
                index += 1
                continue
            words.append((base, segment[index + 1:]))
            break
    return words


def _path_candidates(token: str) -> Iterator[str]:
    yield token
    for part in re.split(r"[=:,]", token):
        if part and part != token:
            yield part


def _normalize_for_scan(command: str) -> str:
    return re.sub(r"[\"'\\]", "", command).casefold()


def _check_command(command: str, cwd: str) -> str | None:
    normalized = _normalize_for_scan(command)
    for marker, label in RAW_MARKERS:
        if marker in normalized:
            return f"Command references protected location: {label}."

    tokens = _tokenize(command)
    if tokens is None:
        return None  # Raw marker scan above already covered unparsable input.

    words = _command_words(tokens)
    for program, _ in words:
        if program in CONTAINER_CLIS:
            return f"Direct '{program}' use is blocked because it can mount the sealed hold-outs. The gate runs containers for you."

    recursive = False
    for program, args in words:
        has_flag = any(re.match(r"^-[A-Za-z]*[rR]", a) or a == "--recursive" for a in args)
        if program in RECURSIVE_CLIS or (program in RECURSIVE_WITH_FLAG and has_flag):
            recursive = True

    for token in tokens:
        if _is_operator(token) or (token.startswith("-") and "=" not in token):
            continue
        for candidate in _path_candidates(token):
            label = violation_for(candidate, cwd, recursive=recursive)
            if label:
                return f"Command token '{token}' points to protected location: {label}."

    if STATE_FILE in normalized and STATE_WRITERS.search(normalized):
        return f"{STATE_FILE} is managed by factory-core and may only be read."
    return None


def evaluate_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    tool_name, tool_input = extract_tool_call(payload)
    base_cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd").strip() else None
    if base_cwd is None:
        workspaces = payload.get("workspacePaths")
        if isinstance(workspaces, list):
            base_cwd = next((os.path.expanduser(w) for w in workspaces if isinstance(w, str) and w.strip()), None)
    cwd = os.path.expanduser(base_cwd) if base_cwd else os.getcwd()
    fields = list(_walk_fields(tool_input))

    for key, value in fields:
        if key == "cwd" or key == "workdir":
            cwd_label = violation_for(value, cwd)
            if cwd_label:
                return False, f"Access to protected path argument '{value}' denied: {cwd_label}."
            cwd = _resolve(value, cwd)

    # Where the harness reports the calling subagent, hold the critic to its one output file.
    agent = payload.get("agent_type")
    critic = isinstance(agent, str) and "adversarial-critic" in agent

    recursive_tool = tool_name in SEARCH_TOOLS
    for key, value in fields:
        if key in PATH_KEYS:
            if critic and tool_name in WRITE_TOOLS and os.path.basename(value) != TRIAGE_FILE:
                return False, f"The adversarial-critic agent is read-only and may write only {TRIAGE_FILE}."
            label = violation_for(value, cwd, recursive=recursive_tool)
            if label:
                return False, f"Access to protected path argument '{value}' denied: {label}."
            if tool_name in WRITE_TOOLS and os.path.basename(value) == STATE_FILE:
                return False, f"{STATE_FILE} is managed by factory-core and may only be read."

    for key, value in fields:
        if key not in COMMAND_KEYS:
            continue
        if tool_name == "apply_patch" or value.lstrip().startswith("*** Begin Patch"):
            for path in PATCH_HEADER.findall(value):
                path = path.strip()
                label = violation_for(path, cwd)
                if label:
                    return False, f"Patch target '{path}' denied: {label}."
                if os.path.basename(path) == STATE_FILE:
                    return False, f"{STATE_FILE} is managed by factory-core and may only be read."
            continue
        reason = _check_command(value, cwd)
        if reason:
            return False, reason

    return True, "Access granted."


def main(platform: str | None = None) -> int:
    payload: dict[str, Any] = {}
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("payload must be an object")
        payload = parsed
    except (ValueError, OSError):
        # A security hook that cannot read its input must not allow the call.
        allowed, reason = False, "Malformed hook payload."
    else:
        allowed, reason = evaluate_payload(payload)

    antigravity = is_antigravity_payload(payload, platform)
    if allowed:
        print(json.dumps({"decision": "allow"}))
        return EXIT_OK
    message = f"BLOCKED by Factory Security Guard: {reason}"
    print(message, file=sys.stderr)
    print(json.dumps({"decision": "deny", "reason": message}))
    # Antigravity treats any non-zero hook exit code as an error and fails open;
    # exit 0 with {"decision": "deny"} enforces the hard block.
    return EXIT_OK if antigravity else EXIT_BLOCK
