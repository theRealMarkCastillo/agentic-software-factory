"""PreToolUse guard: canonical path and shell token inspection.

This is a deterministic tripwire, not a sandbox. It runs as the same OS user as
the agent, so a sufficiently obfuscated command (for example a path assembled at
runtime from encoded bytes) cannot be detected by static inspection.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import shlex
import sys
from collections.abc import Iterator
from typing import Any

from .common import (
    EXIT_BLOCK,
    EXIT_OK,
    STATE_FILE,
    TRIAGE_FILE,
    FactoryError,
    extract_tool_call,
    is_antigravity_payload,
    worktree_root,
)

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
CMD_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
REDIRECT_OUT = {">", ">>", ">|", "&>", "&>>"}
MUTATING_PROGS = {"rm", "unlink", "mv", "tee", "truncate", "dd", "ln", "install", "patch", "sponge", "chmod", "chown"}
INTERPRETER_PROGS = {"node", "perl", "ruby", "sh", "bash", "zsh"}
STATE_SCRIPT_WRITES = re.compile(
    r"(?:open\s*\([^)]*['\"][wax+]|write_text|write_bytes|writefile|unlink|remove|rename|replace|rmtree|truncate|os\.open)"
    r"|(?<![A-Za-z0-9_])>(?![>=])",
    re.IGNORECASE,
)
CRITIC_READONLY_PROGS = {
    "git", "factory-core", "ls", "cat", "head", "tail", "wc", "pwd",
    "rg", "grep", "egrep", "fgrep", "find", "stat", "diff", "true", "echo", "printf", "cd",
}
CRITIC_GIT_READONLY = {"status", "diff", "log", "show", "rev-parse", "ls-files", "branch", "blame", "shortlog", "describe"}
CRITIC_GIT_MUTATING_BRANCH_FLAGS = {"-d", "-D", "-m", "-M", "-f", "--delete", "--move", "--force", "--copy", "-c", "-C"}
# Options of command wrappers that consume the next token as their value.
WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"},
    "nice": {"-n"},
    "env": {"-u", "-C", "-S"},
    "xargs": {"-n", "-I", "-L", "-P", "-s", "-d", "-E", "-a"},
    "time": {"-o", "-f"},
    "exec": {"-a"},
}
# Wrappers that run their remaining arguments as a command, so a container CLI named anywhere after them counts.
EXEC_WRAPPERS = {"sudo", "env", "nohup", "time", "xargs", "nice", "exec"}
SHELL_PROGS = {"sh", "bash", "zsh", "dash", "ksh"}
MAX_NESTED_SHELL_DEPTH = 4
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-x", "--exec", "-X", "--exec-batch"}
# Search-tool arguments that hold a glob rather than a path.
GLOB_KEYS = {"glob", "include", "includes"}
PATTERN_IS_GLOB_TOOLS = {"glob", "find_by_name", "list_dir"}
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


def _glob_parts(path: str) -> list[str]:
    return [part for part in _fold(path).split(os.sep) if part]


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    options = [opt.strip() for opt in match[1].split(",")]
    if 1 < len(options) <= 8:
        expanded: list[str] = []
        prefix, suffix = pattern[:match.start()], pattern[match.end():]
        for opt in options:
            expanded.extend(_expand_braces(f"{prefix}{opt}{suffix}"))
            if len(expanded) >= 16:
                return expanded[:16]
        return expanded
    collapsed = pattern
    while True:
        next_pat = re.sub(r"\{[^{}]*\}", "*", collapsed)
        if next_pat == collapsed:
            return [collapsed]
        collapsed = next_pat


def _single_pattern_violation(pattern: str, base: str) -> str | None:
    """Return the protected resource one expanded search glob can enumerate, or None."""
    folded = _normalize_for_scan(pattern)
    for marker, label in RAW_MARKERS:
        if marker in folded:
            return label
    expanded = os.path.expanduser(os.path.expandvars(pattern))
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.path.realpath(base), expanded)
    parts = _glob_parts(os.path.normpath(expanded))

    for idx in range(len(parts) - 1):
        if (
            parts[idx].startswith(".")
            and fnmatch.fnmatchcase(".git", parts[idx])
            and ("**" in parts[idx + 1] or fnmatch.fnmatchcase("hooks", parts[idx + 1]))
        ):
            return "Git hooks directory"

    # A symlink in the literal prefix may lead into a protected location. Keep the original
    # case here: the folded parts above are only for comparison.
    literal: list[str] = []
    for part in (piece for piece in os.path.normpath(expanded).split(os.sep) if piece):
        if glob.has_magic(part):
            break
        literal.append(part)
    prefix = os.sep + os.sep.join(literal)
    prefix_label = violation_for(prefix, base, recursive=any("**" in part for part in parts[len(literal):]))
    if prefix_label:
        return prefix_label

    for protected, protected_label in protected_paths():
        for candidate in {protected, os.path.realpath(protected)}:
            target = _glob_parts(candidate)
            for index, part in enumerate(parts):
                if "**" in part:
                    return f"{protected_label} (recursive glob through a parent directory)"
                if index >= len(target):
                    return protected_label  # The pattern descends below the protected path.
                if not fnmatch.fnmatchcase(target[index], part):
                    break
    return None


def _pattern_violation(pattern: str, base: str) -> str | None:
    """Return the protected resource a search glob can enumerate, or None.

    The check is structural, so it does not depend on what currently exists on disk.
    """
    if not pattern or "\0" in pattern:
        return None
    for candidate in _expand_braces(pattern):
        hit = _single_pattern_violation(candidate, base)
        if hit:
            return hit
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


def _is_redirect_out(segment: list[str], index: int) -> tuple[bool, int]:
    """Return (is_output_redirect, target_token_index)."""
    tok = segment[index]
    if tok in REDIRECT_OUT:
        return True, index + 1
    if tok.isdigit() and index + 1 < len(segment) and segment[index + 1] in REDIRECT_OUT:
        return True, index + 2
    return False, index


def _redirect_targets_ignored(segment: list[str], target_idx: int) -> bool:
    if target_idx >= len(segment):
        return True
    target = segment[target_idx]
    return target in {"/dev/null", "&"} or target.startswith("&")


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in CMD_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [seg for seg in segments if seg]


def _segment_program(segment: list[str]) -> tuple[str, list[str]] | None:
    """Return (program_basename, args) for one command segment, skipping env, wrappers, and redirects."""
    index = 0
    wrapper = ""
    while index < len(segment):
        is_redir, target_idx = _is_redirect_out(segment, index)
        if is_redir:
            index = target_idx + 1
            continue
        word = segment[index]
        if _is_operator(word):
            index += 1
            continue
        base = os.path.basename(word)
        if base in COMMAND_WRAPPERS:
            wrapper = base
            index += 1
            continue
        if word.startswith("-"):
            # `sudo -u root docker` must not read `root` as the program.
            index += 2 if word in WRAPPER_VALUE_FLAGS.get(wrapper, ()) else 1
            continue
        if re.match(r"^[A-Za-z_]\w*=", word):
            index += 1
            continue
        return base, segment[index + 1:]
    return None


def _wrapped_container_cli(segment: list[str]) -> str | None:
    """A container CLI named anywhere after an exec-style wrapper (covers `env -S "docker ps"`)."""
    for index, token in enumerate(segment):
        if os.path.basename(token) in EXEC_WRAPPERS:
            for later in segment[index + 1:]:
                for word in later.split():
                    if os.path.basename(word) in CONTAINER_CLIS:
                        return os.path.basename(word)
            return None
    return None


def _nested_script(program: str, args: list[str]) -> str | None:
    """The shell source a segment hands to another shell: `sh -c '<script>'` or `eval <words>`."""
    if program == "eval":
        return " ".join(a for a in args if not _is_operator(a)) or None
    if program in SHELL_PROGS:
        for index, arg in enumerate(args[:-1]):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg):
                return args[index + 1]
    return None


def _git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-C", "-c", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg, args[index + 1:]
    return "", []


def _resets_factory_state(program: str, args: list[str]) -> str | None:
    """Commands that delete or re-create the ignored state file without naming it."""
    if program == "git":
        sub, sub_args = _git_subcommand(args)
        if sub == "clean" and any(re.match(r"^-[A-Za-z]*[xX]", a) for a in sub_args):
            return f"'git clean' with -x/-X deletes ignored files, including {STATE_FILE}. Use 'git clean -fd'."
        if sub == "stash":
            skip_next = False
            for arg in sub_args:
                if skip_next:
                    skip_next = False
                    continue
                if arg in {"-m", "--message", "--pathspec-from-file"}:
                    skip_next = True
                    continue
                if arg == "--":
                    break
                if arg == "--all" or re.match(r"^-[ukqSp]*a[ukqSp]*$", arg):
                    return f"'git stash --all' removes ignored files, including {STATE_FILE}. Use 'git stash -u'."
    if program == "factory-core" and "init-state" in args:
        return "Resetting factory state is reserved for the human operator."
    return None


def _path_candidates(token: str) -> Iterator[str]:
    yield token
    for part in re.split(r"[=:,]", token):
        if part and part != token:
            yield part


def _normalize_for_scan(command: str) -> str:
    return re.sub(r"[\"'\\]", "", command).casefold()


def _touches_state_file(token: str, cwd: str) -> bool:
    if not token or "\0" in token or token == "/dev/null":
        return False
    if STATE_FILE in _normalize_for_scan(token):
        return True
    if os.path.basename(_resolve(token, cwd)) == STATE_FILE:
        return True
    expanded = os.path.expanduser(os.path.expandvars(token))
    if glob.has_magic(expanded):
        if fnmatch.fnmatch(STATE_FILE, os.path.basename(expanded)):
            return True
        pattern = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
        if any(os.path.basename(m) == STATE_FILE for m in glob.glob(pattern, include_hidden=True)):
            return True
    return False


def _cd_destination(args: list[str]) -> str:
    after_dash = False
    for arg in args:
        if arg == "--" and not after_dash:
            after_dash = True
            continue
        if not after_dash and arg.startswith("-"):
            continue
        if _is_operator(arg):
            break
        return arg
    return "~"


def _critic_allows_segment(program: str, args: list[str]) -> str | None:
    if program not in CRITIC_READONLY_PROGS:
        return f"The adversarial-critic agent is read-only; command '{program}' is not permitted."
    if program == "git":
        subcmd: str | None = None
        i = 0
        while i < len(args):
            arg = args[i]
            if _is_operator(arg):
                break
            if arg in {"-C", "-c", "--git-dir", "--work-tree"}:
                i += 2
                continue
            if arg.startswith("-"):
                i += 1
                continue
            subcmd = arg
            sub_args = args[i + 1:]
            break
        if subcmd not in CRITIC_GIT_READONLY:
            return f"The adversarial-critic agent is read-only; 'git {subcmd or ''}' is not permitted.".strip()
        if subcmd == "branch" and any(a in CRITIC_GIT_MUTATING_BRANCH_FLAGS for a in sub_args):
            return "The adversarial-critic agent is read-only; mutating 'git branch' flags are not permitted."
    elif program == "factory-core":
        subcmd = next((a for a in args if not a.startswith("-") and not _is_operator(a)), "")
        if subcmd not in {"status", "doctor"}:
            return f"The adversarial-critic agent is read-only; 'factory-core {subcmd}' is not permitted."
    elif program == "find" and any(a in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for a in args):
        return "The adversarial-critic agent is read-only; mutating 'find' actions are not permitted."
    return None


def _segment_mutates_state(program: str, args: list[str], seg_cwd: str) -> bool:
    non_op_args = [a for a in args if not _is_operator(a)]
    touches = any(
        _touches_state_file(cand, seg_cwd)
        for arg in non_op_args
        if not (arg.startswith("-") and "=" not in arg)
        for cand in _path_candidates(arg)
    )
    if not touches:
        return False
    if program in MUTATING_PROGS:
        return True
    if program in {"find", "fd"} and any(a in FIND_ACTIONS for a in non_op_args):
        return True
    if program == "cp":
        positional = [a for a in non_op_args if not a.startswith("-")]
        if positional and any(_touches_state_file(c, seg_cwd) for c in _path_candidates(positional[-1])):
            return True
        for i, a in enumerate(non_op_args):
            if a in {"-t", "--target-directory"} and i + 1 < len(non_op_args) and _touches_state_file(non_op_args[i + 1], seg_cwd):
                return True
        return False
    if program in {"sed", "perl", "ruby"} and any(a.startswith(("-i", "--in-place")) for a in non_op_args):
        return True
    if program in {"curl", "wget"} and any(
        a in {"-o", "-O", "--output", "--output-document"}
        or a.startswith(("-o", "-O", "--output=", "--output-document="))
        for a in non_op_args
    ):
        return True
    if program.startswith("python") or program in INTERPRETER_PROGS:
        return bool(STATE_SCRIPT_WRITES.search(" ".join(non_op_args)))
    return False


def _check_command(command: str, cwd: str, critic: bool = False, depth: int = 0) -> str | None:
    normalized = _normalize_for_scan(command)
    for marker, label in RAW_MARKERS:
        if marker in normalized:
            return f"Command references protected location: {label}."

    tokens = _tokenize(command)
    if tokens is None:
        if critic:
            return "The adversarial-critic agent is read-only and cannot run unparsable shell commands."
        if STATE_FILE in normalized and STATE_WRITERS.search(normalized):
            return f"{STATE_FILE} is managed by factory-core and may only be read."
        return None  # Raw marker scan above already covered unparsable input.

    current_cwd = cwd
    for segment in _split_segments(tokens):
        parsed = _segment_program(segment)
        program, args = parsed if parsed else ("", [])

        container = program if program in CONTAINER_CLIS else _wrapped_container_cli(segment)
        if container:
            return f"Direct '{container}' use is blocked because it can mount the sealed hold-outs. The gate runs containers for you."

        reset_reason = _resets_factory_state(program, args)
        if reset_reason:
            return reset_reason

        nested = _nested_script(program, args)
        if nested:
            if depth >= MAX_NESTED_SHELL_DEPTH:
                return "Shell commands nested this deeply cannot be inspected."
            nested_reason = _check_command(nested, current_cwd, critic=critic, depth=depth + 1)
            if nested_reason:
                return nested_reason

        if critic and program:
            critic_err = _critic_allows_segment(program, args)
            if critic_err:
                return critic_err

        # Check output redirections (both critic read-only enforcement and .factory-state.json protection).
        idx = 0
        while idx < len(segment):
            is_redir, target_idx = _is_redirect_out(segment, idx)
            if is_redir:
                if not _redirect_targets_ignored(segment, target_idx):
                    target = segment[target_idx]
                    if critic:
                        return "The adversarial-critic agent is read-only and may not redirect shell output to files."
                    if _touches_state_file(target, current_cwd):
                        return f"{STATE_FILE} is managed by factory-core and may only be read."
                idx = target_idx + 1
                continue
            idx += 1

        has_flag = any(re.match(r"^-[A-Za-z]*[rR]", a) or a == "--recursive" for a in args)
        recursive = program in RECURSIVE_CLIS or (program in RECURSIVE_WITH_FLAG and has_flag)

        for token in segment:
            if _is_operator(token) or (token.startswith("-") and "=" not in token):
                continue
            for candidate in _path_candidates(token):
                hit_label = violation_for(candidate, current_cwd, recursive=recursive)
                if hit_label:
                    return f"Command token '{token}' points to protected location: {hit_label}."

        if program and _segment_mutates_state(program, args, current_cwd):
            return f"{STATE_FILE} is managed by factory-core and may only be read."

        if program in {"cd", "pushd"}:
            dest = _cd_destination(args)
            cd_label = violation_for(dest, current_cwd, recursive=False)
            if cd_label:
                return f"Command token '{dest}' points to protected location: {cd_label}."
            current_cwd = _resolve(dest, current_cwd)

    return None


def evaluate_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    tool_name, tool_input = extract_tool_call(payload)
    raw_cwd = payload.get("cwd")
    base_cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd.strip() else None
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
    agent = (
        payload.get("agent_type")
        or payload.get("agentName")
        or payload.get("subagentName")
        or os.environ.get("FACTORY_AGENT", "")
    )
    critic = isinstance(agent, str) and "adversarial-critic" in agent
    allowed_triage: str | None = None
    if critic:
        try:
            allowed_triage = os.path.realpath(os.path.join(str(worktree_root(cwd)), TRIAGE_FILE))
        except FactoryError:
            allowed_triage = os.path.realpath(os.path.join(cwd, TRIAGE_FILE))

    recursive_tool = tool_name in SEARCH_TOOLS
    for key, value in fields:
        if key in PATH_KEYS:
            if (
                critic
                and tool_name in WRITE_TOOLS
                and (allowed_triage is None or _fold(_resolve(value, cwd)) != _fold(allowed_triage))
            ):
                return False, f"The adversarial-critic agent is read-only and may write only {TRIAGE_FILE}."
            label = violation_for(value, cwd, recursive=recursive_tool)
            if label:
                return False, f"Access to protected path argument '{value}' denied: {label}."
            if tool_name in WRITE_TOOLS and os.path.basename(_resolve(value, cwd)) == STATE_FILE:
                return False, f"{STATE_FILE} is managed by factory-core and may only be read."

    if recursive_tool:
        search_bases = [_resolve(v, cwd) for k, v in fields if k in PATH_KEYS and k not in {"cwd", "workdir"}]
        if not search_bases:
            # No explicit path: the tool searches the working directory itself.
            label = violation_for(cwd, cwd, recursive=True)
            if label:
                return False, f"Search from '{cwd}' denied: {label}."
            search_bases = [cwd]
        for key, value in fields:
            if key in GLOB_KEYS or (key == "pattern" and tool_name in PATTERN_IS_GLOB_TOOLS):
                for base in search_bases:
                    label = _pattern_violation(value, base)
                    if label:
                        return False, f"Search pattern '{value}' denied: {label}."

    for key, value in fields:
        if key not in COMMAND_KEYS:
            continue
        if tool_name == "apply_patch" or value.lstrip().startswith("*** Begin Patch"):
            for path in PATCH_HEADER.findall(value):
                path = path.strip()
                if critic and (allowed_triage is None or _fold(_resolve(path, cwd)) != _fold(allowed_triage)):
                    return False, f"The adversarial-critic agent is read-only and may write only {TRIAGE_FILE}."
                label = violation_for(path, cwd)
                if label:
                    return False, f"Patch target '{path}' denied: {label}."
                if os.path.basename(_resolve(path, cwd)) == STATE_FILE:
                    return False, f"{STATE_FILE} is managed by factory-core and may only be read."
            continue
        reason = _check_command(value, cwd, critic=critic)
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
        try:
            allowed, reason = evaluate_payload(payload)
        except Exception as exc:  # A guard that crashes must not allow the call.
            allowed, reason = False, f"Guard could not inspect this call ({type(exc).__name__}: {exc})."

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
