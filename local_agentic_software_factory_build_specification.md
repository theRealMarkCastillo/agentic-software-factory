# Local Agentic Software Factory — Multi-Platform Plugin Architecture & Build Specification

**Version:** 2.4 (Final Build-Ready Specification)

**Target Platforms:** Claude Code, OpenAI Codex (CLI & Desktop), Google Antigravity (IDE & CLI)

**Core Isolation Model:** Git Worktrees + Mandatory Ephemeral Docker / Docker Compose Hold-Out Oracles

## 1. Purpose & Architectural Overview

This specification defines a local-first agentic software factory packaged as native plugins/extensions for **Claude Code**, **OpenAI Codex**, and **Google Antigravity**, distributable both via **GitHub-backed plugin marketplaces** and direct local `./install.sh` bootstrap. An AI code builder reading this document must scaffold and generate a fully functional monorepo containing:

1. A shared, platform-agnostic **Factory Core** (`factory-core/` CLI toolchain, prerequisite preflight `doctor`, Git worktree lifecycle manager, path guard inspector, mandatory Docker Compose hold-out oracle runner, patch exporter, report sanitizer, telemetry logger, stdio MCP server, and Open Agent Skills definitions).

2. Three thin, native **Platform Plugins** (`plugins/claude-code/`, `plugins/codex/`, and `plugins/antigravity/`) plus marketplace manifests (`.claude-plugin/marketplace.json`) that bind each target platform's native lifecycle hooks, subagent configurations, workspace rules, and packaging manifests to the shared Factory Core.

```
+-----------------------------------------------------------------------------------+
|                              TARGET PLATFORM HARNESS                              |
|  +-------------------+      +--------------------+      +----------------------+  |
|  |    Claude Code    |      |    OpenAI Codex    |      |  Google Antigravity  |  |
|  |  (.claude-plugin) |      |   (.codex-plugin)  |      |   (.agents/plugins)  |  |
|  +---------+---------+      +---------+----------+      +----------+-----------+  |
+------------|--------------------------|----------------------------|--------------+
             |                          |                            |
             +-------------------+------+----------------------------+
                                 | Native Event Hooks (JSON Stdin/Stdout)
                                 v
+-----------------------------------------------------------------------------------+
|                               SHARED FACTORY CORE                                 |
|  +----------------+  +-------------------+  +------------------+  +------------+  |
|  |  path_guard.py |  | patch_exporter.py |  | report_sanitizer |  | doctor.py  |  |
|  +-------+--------+  +---------+---------+  +--------+---------+  +-----+------+  |
+----------|---------------------|---------------------|------------------|---------+
           |                     |                     |                  |
           v                     v                     v                  v
  +------------------+  +------------------+  +------------------+  +---------------+
  | Path & Shell     |  | Clean SUT Patch  |  | Stripped Secret  |  | Mandatory     |
  | Token Inspection |  | Extraction       |  | & Source Feedback|  | Docker/Compose|
  +------------------+  +--------+---------+  +------------------+  +---------------+
                                 |
                                 v
+-----------------------------------------------------------------------------------+
|                        SEALED HOLD-OUT ORACLE CONTAINER                           |
|  +-----------------------------------+   +-------------------------------------+  |
|  | System Under Test (SUT Container) |   | Verifier Container (pytest/httpx)   |  |
|  | Isolated Code Patch Execution     |<--| Sealed Acceptance & Contract Tests  |  |
|  +-----------------------------------+   +-------------------------------------+  |
|  Host Path: ~/.factory-holdouts/<repo-slug>/docker-compose.holdout.yml            |
+-----------------------------------------------------------------------------------+



```

### Core Invariants

* **Conventions live in Skills and Rules; Guarantees live in Hooks and OS/Container Boundaries:** Prompts guide the model toward quality code; deterministic hooks, `realpath` checks, container isolation, and external verifiers enforce hard safety guarantees, fast checks, and hold-out gates.

* **Mandatory Docker & Docker Compose v2 Runtime:** Docker Engine (active daemon) and Docker Compose v2 (`docker compose`) are **hard prerequisites**. There is no host-shell fallback for hold-out verification. `install.sh`, `factory-core doctor`, `factory-core init`, and `factory-core holdout-gate` verify a responsive Docker daemon and `docker compose` v2 upfront and **fail closed** (exit code `2` with actionable remediation diagnostics) if unavailable.

* **Sealed Hold-Out Oracles:** Hold-out acceptance tests, golden fixtures, and Compose topologies live entirely outside the target repository at `~/.factory-holdouts/<repo-slug>/`. They execute inside disposable Docker/Docker Compose environments against an extracted candidate patch. The agent never possesses file-system read or write access to these hold-out files.

* **Zero-Leakage Failure Feedback:** Failures in hold-out test runs pass through a deterministic sanitizer before reaching the agent context. Sanitization strips file paths, stack frame internals, and raw fixture secrets, exposing only violated behavioral contracts, input parameters, and reproduction conditions.

* **Closed-Loop Triage with Circuit Breakers:** Failed hold-out runs increment a persistent counter in `.factory-state.json`, route execution state to a read-only Adversarial Critic subagent for root-cause diagnosis, and halt automatically when `attempts >= MAX_RETRIES` (default `3`), releasing control back to the human developer.

* **Silent-Success, Verbose-Failure Sensors:** Fast checks and hold-out gates emit minimal single-line output on pass (`OK (1.2s)`) to preserve context tokens, and emit LLM-optimized failure diagnostics with actionable remediation hints on failure.

* **Compaction-Resilient State & Telemetry:** Worktree state persists `rejected_approaches[]` and tree hashes across context compactions, while `factory-core` records structured JSONL/OTel telemetry for factory observability.

* **Unified Cross-Platform UX via `factory-core status` & Local MCP Server:** Exposes a single deterministic status/dashboard CLI (`factory-core status --json|--watch`) and lightweight stdio MCP server (`factory-core mcp`) so Claude Code, Codex, and Antigravity share identical live visibility into worktree state, attempt counters, rejected approaches, and hold-out telemetry.

* **Marketplace-Ready Distribution:** Structured for GitHub-backed plugin/marketplace installation across Claude Code, OpenAI Codex, and Google Antigravity, paired with fail-closed diagnostics if a plugin runs before `factory-core` host bootstrap or `factory-core init`.

## 2. Deep Architectural Review & Hardened Fixes (v1 -> v2.4)

| Area | Earlier Weakness / Risk | v2.4 Hardened Solution | 
| ----- | ----- | ----- | 
| **1. Patch Extraction & Untracked Files** | `git diff HEAD` omits newly created untracked files (`src/new_module.py`), causing missing-module errors during the SUT Docker build. | Create a temporary index via `GIT_INDEX_FILE`, stage all files (`git add -A`), and generate a binary patch (`git diff --cached --binary HEAD`). Captures tracked edits, deletions, and untracked files without mutating the agent's active index. | 
| **2. Compose Build Cache & Dependency Speed** | Rebuilding Docker images and re-running package installation inside `verifier` on every gate run adds 30–90s overhead. | Pre-build a base verifier image (`Dockerfile.verifier`) containing framework test dependencies. Use BuildKit caching for `sut`, and reserve Compose for contract gates while delegating fast checks to `fast-check`. | 
| **3. Hook Input Parsing & Bypass Vectors** | Regex matching on Bash command strings is bypassed via relative path traversal (`../../.factory-holdouts`) or symlinks. | Tokenize shell commands with `shlex`, extract path arguments, and resolve canonical realpaths (`os.path.realpath`) against incoming `tool_input` JSON in `path_guard.py`, paired with OS/container sandboxes. | 
| **4. Stop Hook Infinite Loops** | Returning a blocking exit code (`exit 2`) on `Stop` without checking attempt limits or unchanged tree hashes traps agents in retry loops. | Store `verified_tree_hash` and `attempts` in `.factory-state.json`. If tree hash is unchanged since a passing run, exit `0`. If `attempts >= MAX_RETRIES`, print an escalation banner and exit `0`. | 
| **5. Multi-Platform & Marketplace Packaging** | Scattering disjoint configs causes drift, while marketplace-installed plugins fail silently if `factory-core` isn't bootstrapped on the host. | Package standalone plugin bundles (`plugins/claude-code`, `plugins/codex`, `plugins/antigravity`) + `.claude-plugin/marketplace.json` backed by one shared `factory-core` binary (`~/.local/bin`), with fail-closed initialization checks. | 
| **6. Parallel Worktree Port & Compose Collisions** | Running multiple worktrees in parallel causes host port conflicts and Docker Compose network/container collisions. | Omit all `ports:` bindings in `docker-compose.holdout.yml`, route strictly over internal Compose DNS (`http://sut:8080`), and namespace projects via `-p holdout-${TASK_ID}-${SHORT_SHA}`. | 
| **7. Context Token Flood on Passing Checks** | Verbose linter/Docker logs pollute model context on every edit. | Enforce silent-success (`OK (<duration>)`) vs. structured failure + remediation hints in `fast-check` and `holdout-gate`. | 
| **8. Context Compaction Amnesia** | Long loops trigger context compaction, causing agents to repeat rejected approaches. | Persist `rejected_approaches[]` in `.factory-state.json` and require `factory-triage` / `factory-implement` to reconcile state with `git status` before retrying. | 
| **9. Architectural Drift & Layer Violations** | Agents pass unit tests while violating module boundaries or security wrappers. | Integrate deterministic architectural fitness checks (`import-linter`, `dependency-cruiser`, or AST boundary rules) into `fast-check`. | 
| **10. Monolithic Rule Dilution** | Dumping every domain rule into root `AGENTS.md` / `CLAUDE.md` dilutes attention. | Use progressive disclosure with path-scoped rules (`.claude/rules/`, `.agent/rules/`, nested `AGENTS.md`) loaded only when touching matching subtrees. | 
| **11. Blind Factory Optimization** | No visibility into first-pass hold-out rates, retry distributions, or failing contracts across harnesses. | Emit structured JSONL telemetry (`~/.factory-holdouts/<repo-slug>/telemetry.jsonl`) and optional OTel spans per run. | 
| **12. Harness Entropy & Suite Staleness** | Abandoned worktrees, orphaned Compose networks, and memorized hold-out cases accumulate over time. | Provide `factory-core gc` for automated cleanup and a hold-out promotion workflow to graduate recurring failures into public regression tests while rotating sealed edge cases. | 
| **13. Strict Platform Manifest & Hook Schemas** | Misplaced `hooks.json` or non-standard manifest paths fail strict plugin validators (`claude plugin validate`, `agy plugin validate --strict`). | Follow exact strict layouts: Claude Code (`.claude-plugin/plugin.json` + `hooks/hooks.json`), Codex (`.codex-plugin/plugin.json` + `.codex/hooks.json` + `.codex/agents/*.toml`), Antigravity (`plugin.json` + root `hooks.json` + `mcp_config.json` + `rules/` with `trigger: always_on`). | 
| **14. Dual-Stream Hook Compatibility** | Claude Code reads `stderr` on exit code `2` for blocking errors, whereas Codex and Antigravity parse structured `stdout` JSON. | `factory-core guard`, `doctor`, and `holdout-gate` emit blocking reasons to **both `stderr` and structured JSON `stdout`** on exit code `2`. | 
| **15. Mandatory Docker & Compose Enforcement** | Optional or lazily checked Docker dependencies allow unisolated host execution or opaque mid-run failures. | `factory-core doctor` enforces Python 3.11+, Git, active Docker Engine (`docker info`), and Docker Compose v2 (`docker compose version`) before installation, repo init, or gate execution. | 

## 3. Repository & Package Monorepo Structure

```
agentic-software-factory/
├── README.md
├── .claude-plugin/
│   └── marketplace.json                     # GitHub-backed Claude Code marketplace catalog
├── install.sh                               # Verifies Docker + Compose v2, installs factory-core to ~/.local/bin, installs selected plugin(s): --platform <claude|codex|antigravity|all>
├── factory-core/                            # Shared deterministic engine (Python 3.11+)
│   ├── pyproject.toml
│   ├── tests/
│   │   ├── test_doctor.py
│   │   ├── test_path_guard.py
│   │   ├── test_patch_exporter.py
│   │   ├── test_report_sanitizer.py
│   │   ├── test_state_manager.py
│   │   └── test_mcp_server.py
│   └── src/factory_core/
│       ├── __init__.py
│       ├── cli.py                           # CLI entrypoint: doctor, init, worktree, guard, fast-check, holdout-gate, status, mcp, gc
│       ├── doctor.py                        # Mandatory prerequisite preflight (Python, Git, Docker Engine, docker compose v2, PATH)
│       ├── patch_exporter.py                # Captures tracked + untracked changes cleanly into candidate patch
│       ├── path_guard.py                    # Canonical realpath + shell token inspector for PreToolUse hooks
│       ├── compose_runner.py                # Runs ephemeral Docker Compose oracle with healthchecks & teardown
│       ├── report_sanitizer.py              # Strips test paths, source lines, and secrets into contract failures
│       ├── state_manager.py                 # Manages .factory-state.json, SHA caching, rejected approaches, and circuit breaker
│       ├── telemetry.py                     # Emits JSONL/OTel events for run metrics and contract failures
│       ├── status.py                        # Live worktree state, attempt counter, and telemetry CLI dashboard
│       ├── mcp_server.py                    # Stdio MCP server exposing read-only factory state tools
│       └── gc.py                            # Prunes stale worktrees, docker networks, and triage artifacts
├── shared-skills/                           # Canonical Open Agent Skills (bundled into all plugins)
│   ├── factory-spec-plan/
│   │   └── SKILL.md
│   ├── factory-implement/
│   │   └── SKILL.md
│   └── factory-triage/
│       └── SKILL.md
├── plugins/
│   ├── claude-code/                         # Claude Code Plugin
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── hooks/
│   │   │   └── hooks.json                   # WorktreeCreate, PreToolUse, PostToolUse, PostToolBatch, PostCompact, SubagentStop, Stop
│   │   ├── agents/
│   │   │   ├── implementer.md
│   │   │   └── adversarial-critic.md
│   │   ├── .mcp.json                        # Registers factory-core mcp server
│   │   └── skills/                          # Bundled from shared-skills/
│   ├── codex/                               # OpenAI Codex Plugin
│   │   ├── .codex-plugin/
│   │   │   └── plugin.json                  # Codex plugin manifest
│   │   ├── .codex/
│   │   │   ├── hooks.json                   # PreToolUse, PostToolUse, Stop
│   │   │   └── agents/
│   │   │       ├── implementer.toml         # Custom subagent TOML spec
│   │   │       └── adversarial-critic.toml  # Read-only critic subagent TOML spec
│   │   ├── .mcp.json                        # Registers factory-core mcp server
│   │   └── skills/                          # Bundled from shared-skills/
│   └── antigravity/                         # Google Antigravity Plugin (strict layout)
│       ├── plugin.json                      # Root discovery manifest
│       ├── hooks.json                       # Root hooks file (PreToolUse, PostToolUse, Stop)
│       ├── mcp_config.json                  # Root MCP configuration for factory-core mcp
│       ├── rules/
│       │   └── factory-invariants.md        # YAML frontmatter (trigger: always_on)
│       ├── agents/
│       │   ├── implementer.md
│       │   └── adversarial-critic.md
│       └── skills/                          # Bundled from shared-skills/
└── templates/
    └── holdout-repo-template/               # Initialized in ~/.factory-holdouts/<repo-slug>/
        ├── docker-compose.holdout.yml
        ├── Dockerfile.verifier
        ├── telemetry.jsonl                  # Append-only telemetry log
        ├── suites/
        │   └── test_contract_example.py
        └── fixtures/
            └── golden_example.json



```

## 4. Shared Factory Core Specification (`factory-core`)

The `factory-core` package provides a unified command-line interface. It handles deterministic prerequisite validation, file integrity checks, state persistence, and container execution.

### CLI Subcommands and Behavioral Contracts

#### 1. `factory-core doctor [--json]`

* Hard prerequisite preflight verifying:

  1. **Python:** `>= 3.11`

  2. **Git:** `git --version` available

  3. **Docker Engine (Mandatory):** `docker info` succeeds and daemon is running

  4. **Docker Compose v2 (Mandatory):** `docker compose version` succeeds and reports v2.x+

  5. **Host Paths:** `~/.local/bin` on `PATH` and `~/.factory-holdouts/` writable

* On pass, prints `OK: All factory prerequisites verified (Docker + Compose v2 active)` (or JSON `{"status": "ok", ...}`) and exits `0`.

* On failure, prints exact missing dependency and remediation command to **both `stderr` and structured JSON `stdout`** and exits with code `2`.

#### 2. `factory-core init [--repo <path>]`

* Executes `factory-core doctor` first; fails closed (`exit 2`) if Docker Engine or Docker Compose v2 is missing or stopped.

* Calculates the repository slug from `basename $(git rev-parse --show-toplevel)`.

* Verifies existence of `~/.factory-holdouts/<repo-slug>/`. If absent, populates it using `templates/holdout-repo-template/`.

* Appends `.worktrees/` and `.factory-state.json` to `.git/info/exclude` to prevent internal state from polluting repository status.

* Installs a default `AGENTS.md` file in the repository root if one is not present.

#### 3. `factory-core worktree create <task-id>` & `factory-core worktree init-state`

* `worktree create <task-id>` executes `git worktree add -b factory/<task-id> .worktrees/<task-id> HEAD` and initializes `.factory-state.json`.

* `worktree init-state` initializes `.factory-state.json` inside an already-created worktree (used by Claude Code's `WorktreeCreate` hook).

* Initial `.factory-state.json` schema:

```
{
  "task_id": "<task-id>",
  "attempts": 0,
  "max_retries": 3,
  "verified_tree_hash": null,
  "last_evaluated_sha": null,
  "rejected_approaches": [],
  "updated_at": "2026-11-23T00:00:00Z"
}



```

* Prints the absolute path of the generated worktree directory to `stdout`.

#### 4. `factory-core guard`

* Accepts hook payload JSON from `stdin` containing tool call context:

```
{
  "tool_name": "Bash",
  "tool_input": {
    "command": "cat ../../.factory-holdouts/my-repo/suites/test_secret.py"
  }
}



```

* Normalizes path fields (`file_path`, `path`, `target`, `filePath`, `cwd`) and tokenizes shell commands (`command`).

* Uses `os.path.realpath()` to resolve all path targets, bypassing symlinks, parent directory navigation (`..`), and environment variable expansions.

* Blocks access if any target path intersects:

  * `~/.factory-holdouts/`

  * `/var/run/docker.sock`

  * `.git/hooks/`

* **Standardized Deny Payload** (emits plain text to `stderr` and structured JSON to `stdout`, exit code `2`):

```
{
  "decision": "deny",
  "reason": "BLOCKED by Factory Security Guard: Access to sealed hold-out directory ~/.factory-holdouts is prohibited."
}



```

* On pass, outputs `{"decision": "allow"}` and exits with code `0`.

```
# Implementation Reference: path_guard.py
import json
import os
import sys
import shlex

PROTECTED_PATHS = [
    os.path.expanduser("~/.factory-holdouts"),
    "/var/run/docker.sock",
    ".git/hooks",
]

def check_path_violation(target_path: str) -> bool:
    try:
        resolved = os.path.realpath(os.path.expanduser(target_path))
        for protected in PROTECTED_PATHS:
            protected_resolved = os.path.realpath(protected)
            if resolved == protected_resolved or resolved.startswith(protected_resolved + os.sep):
                return True
    except Exception:
        pass
    return False

def evaluate_payload(payload: dict) -> tuple[bool, str]:
    tool_input = payload.get("tool_input", {})

    # Direct path properties
    for key in ["file_path", "path", "target", "filePath", "cwd"]:
        if key in tool_input and isinstance(tool_input[key], str):
            if check_path_violation(tool_input[key]):
                return False, f"Access to protected path argument '{tool_input[key]}' denied."

    # Tokenized shell command inspection
    command = tool_input.get("command")
    if command and isinstance(command, str):
        try:
            tokens = shlex.split(command)
            for token in tokens:
                if token.startswith("-"):
                    continue
                if check_path_violation(token):
                    return False, f"Command token '{token}' points to protected location."
        except Exception:
            if "~/.factory-holdouts" in command or ".factory-holdouts" in command:
                return False, "Command contains references to protected hold-out paths."

    return True, "Access granted."

def main():
    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input) if raw_input.strip() else {}
    except Exception:
        sys.exit(0)

    allowed, reason = evaluate_payload(payload)
    if not allowed:
        msg = f"BLOCKED by Factory Security Guard: {reason}"
        print(msg, file=sys.stderr)
        print(json.dumps({"decision": "deny", "reason": msg}))
        sys.exit(2)
    else:
        print(json.dumps({"decision": "allow"}))
        sys.exit(0)

if __name__ == "__main__":
    main()



```

#### 5. `factory-core fast-check`

* Identifies modified and untracked files via `git status --porcelain`.

* Detects the project ecosystem and runs the appropriate fast checker & **Architectural Fitness & Boundary Checks**:

  * **Python:** `ruff check . && mypy --config-file pyproject.toml src/` (plus `lint-imports` if `.importlinter` exists)

  * **Node.js/TypeScript:** `tsc --noEmit && eslint .` (plus `depcruise src` if `.dependency-cruiser.*` exists)

  * **Generic:** `make fast-check` (if `Makefile` defines the target).

* Imposes a strict 15-second execution timeout.

* **Silent-Success Output:** On success (exit code `0`), emits single-line output `OK (<duration>)` to minimize context token usage. On failure (exit code `1`), emits structured failure diagnostics with actionable remediation hints.

#### 6. `factory-core holdout-gate`

* **Fail-Closed Preflight:** Verifies Docker Engine + Docker Compose v2 (`doctor`) and confirms `~/.factory-holdouts/<repo-slug>/docker-compose.holdout.yml` exists. If uninitialized or Docker is down, emits remediation guidance (`Run factory-core init` / `Start Docker Engine`) to **both `stderr` and structured JSON `stdout`** and exits with code `2`.

* Computes a SHA-256 tree hash across tracked and non-ignored untracked files.

* Reads `.factory-state.json`. If `verified_tree_hash` equals the current tree hash, prints `Verification cached.` and exits `0`.

* Evaluates `attempts`. If `attempts >= max_retries`, outputs the circuit breaker notice and exits `0`:

```
================================================================================
CIRCUIT BREAKER TRIPPED: Maximum verification attempts (3) reached.
Halting automated retry loop. Passing control back to human operator.
================================================================================



```

* **Patch Generation:** Creates a temporary Git index file (`GIT_INDEX_FILE=/tmp/idx-<uuid>`), runs `git add -A`, and exports the binary patch (`git diff --cached --binary HEAD`).

* **Clean SUT Preparation:** Extracts a clean archive (`git archive HEAD`) into `/tmp/sut-<uuid>` and applies the exported binary patch.

* **Compose Execution:** Launches the verification environment using:

```
docker compose \
  -p "holdout-${TASK_ID}-${SHORT_SHA}" \
  -f "$HOME/.factory-holdouts/${REPO_SLUG}/docker-compose.holdout.yml" \
  up --build --abort-on-container-exit --exit-code-from verifier



```

* **Cleanup:** Runs `docker compose down -v --remove-orphans` and deletes temporary workspace directories inside a `finally` block.

* **Outcome Handling:**

  * **Success (exit code `0`):** Sets `verified_tree_hash` to current tree hash in `.factory-state.json`, records passing telemetry to `~/.factory-holdouts/<repo-slug>/telemetry.jsonl`, and returns exit code `0` with single-line `OK (<duration>)`.

  * **Failure (non-zero exit code):** Increments `attempts`, appends structured failure details to `rejected_approaches[]` (including `attempt`, `summary`, `files_touched`, and `failed_contract`), appends telemetry event (`task_id`, `platform`, `attempt`, `duration_ms`, `gate_result`, `failed_contract_id`, `tree_hash`) to `~/.factory-holdouts/<repo-slug>/telemetry.jsonl` (and optional OTLP endpoint), runs `report_sanitizer.py` over log output, prints sanitized failure output to **both `stderr` and structured JSON `stdout`**, and returns exit code `2`.

```
# Implementation Reference: report_sanitizer.py
import re
import sys

def sanitize_output(raw_text: str, repo_slug: str = "default") -> str:
    # Remove file system paths referencing hold-out directory
    sanitized = re.sub(r'/(?:[^/\s]+/)*\.factory-holdouts/[^\s:]+', '[SEALED_HOLDOUT_FILE]', raw_text)
    sanitized = re.sub(r'/tmp/sut-[a-f0-9-]+/', '', sanitized)

    # Redact raw traceback execution frames inside test suites
    sanitized = re.sub(
        r'File ".*holdout/suites/([^"]+)", line \d+, in (\w+)',
        r'In Behavioral Contract Check (\1 -> \2)',
        sanitized,
    )

    # Strip common golden fixture secret fields
    sanitized = re.sub(
        r'(?i)(api_key|password|secret|bearer_token)\s*=\s*["\'][^"\']+["\']',
        r'\1 = "[REDACTED_SECRET]"',
        sanitized,
    )

    return sanitized

def main():
    raw_log = sys.stdin.read()
    repo_slug = sys.argv[1] if len(sys.argv) > 1 else "default"
    sys.stdout.write(sanitize_output(raw_log, repo_slug))

if __name__ == "__main__":
    main()



```

#### 7. `factory-core gc [--dry-run] [--max-age-days <N>]`

* Prunes merged or stale `.worktrees/` directories.

* Removes orphaned `holdout-*` Docker Compose networks and volumes.

* Cleans expired `.factory-triage.md` artifacts older than specified threshold.

* Prints a summary of reclaimed disk space, removed containers, and cleaned worktree branches.

#### 8. `factory-core status [--json] [--watch] [--compact-summary]`

* Displays active worktree branch, current `attempts`/`max_retries`, `verified_tree_hash` status, last `fast-check` result, `rejected_approaches[]` summary, and aggregate pass/fail metrics from `telemetry.jsonl`.

* With `--compact-summary`, outputs a concise state recap designed for injection after context compaction (`PostCompact`).

#### 9. `factory-core validate-triage-file`

* Validates that `.factory-triage.md` exists and contains required sections (`Root Cause Analysis`, `Unmet Behavioral Contract Clause`, `Target Modification Strategy`) when `adversarial-critic` finishes (`SubagentStop`).

#### 10. `factory-core mcp`

* Runs a zero-dependency stdio Model Context Protocol (MCP) server exposing read-only factory tools (`factory_status`, `factory_plan_read`, `factory_rejected_approaches`, `factory_telemetry_summary`) so any MCP-capable harness can query factory state natively without touching hold-out paths.

## 5. Shared Agent Skills & Subagent Profiles

### Open Agent Skills (`shared-skills/`)

#### `shared-skills/factory-spec-plan/SKILL.md`

```
---
name: factory-spec-plan
description: Defines operational rules for phase 1 of software development. Requires creating a baseline implementation specification before modifying code.
---
# Specification & Planning Protocol

Before writing or editing code in this workspace, you must create `.factory-plan.md` in the root directory.

## Required Plan Structure
1. **Problem Statement:** Concise summary of target capability or bug fix.
2. **Behavioral Acceptance Criteria:** Observable functional conditions required for completion.
3. **Edge Cases & Failure Modes:** Boundary conditions, input validation, and system error scenarios.
4. **Files to Modify/Create:** Explicit list of target file paths.
5. **Visible Unit Test Plan:** List of unit tests to add under `tests/`.

Do not edit application source code until `.factory-plan.md` is fully written and saved.



```

#### `shared-skills/factory-implement/SKILL.md`

```
---
name: factory-implement
description: Defines guidelines for executing code changes and local unit test creation.
---
# Implementation Protocol

1. Upon session resume or post-compaction, read `.factory-state.json` (`rejected_approaches[]`) and `git status` to reconcile state before retrying.
2. Adhere strictly to the scope defined in `.factory-plan.md`.
3. Write corresponding unit tests in `tests/` for every feature addition or modification.
4. Keep changes focused and atomic.
5. Execute local fast checks using visible environment test tools prior to completion.
6. Never attempt to inspect external hold-out paths or bypass path guard restrictions.



```

#### `shared-skills/factory-triage/SKILL.md`

```
---
name: factory-triage
description: Enforces structured root-cause diagnosis when hold-out contract verification fails.
---
# Contract Failure Triage Protocol

When hold-out gate verification returns a non-zero exit code:
1. Read `.factory-state.json` (`rejected_approaches[]`) and `git status` to ensure previous failed attempts are not repeated post-compaction.
2. Do not immediately apply random fixes to application code.
3. Delegate analysis to the `adversarial-critic` agent role. Pass the current `git diff` and sanitized failure logs.
4. Document findings in `.factory-triage.md`, specifying:
   - Root Cause Analysis
   - Unmet Behavioral Contract Clause
   - Target Modification Strategy
5. Apply targeted corrections based on the documented triage analysis.



```

### Agent Roles & Hold-Out Rotation

* **Hold-Out Promotion & Rotation Loop:** To prevent hold-out suite staleness and model memorization, developers or scheduled CI workflows periodically audit `~/.factory-holdouts/<repo-slug>/telemetry.jsonl`. Recurring contract failures are promoted into open repository test suites under `tests/` as permanent public regressions, while fresh boundary conditions and edge cases are generated and rotated into the sealed hold-out oracle environment.

* **`implementer` Agent Profile:**

  * **Capabilities:** Full read, write, edit, and command execution capabilities within the worktree workspace.

  * **Restrictions:** Disallowed from reading, parsing, or interacting with `~/.factory-holdouts/`, `/var/run/docker.sock`, or `.git/hooks/`.

* **`adversarial-critic` Agent Profile:**

  * **Capabilities:** Read-only analysis tools (`Read`, `Grep`, `Glob`, `git diff`, `git log`).

  * **Restrictions:** Denied write access to application code. Evaluates proposals and logs against `.factory-plan.md` and writes structured diagnosis to `.factory-triage.md`.

## 6. Platform-Specific Plugin Implementation Specs

### 6.1 Claude Code Plugin (`plugins/claude-code/`)

#### Manifest (`plugins/claude-code/.claude-plugin/plugin.json`)

```
{
  "name": "agentic-software-factory",
  "version": "2.4.0",
  "description": "Multi-platform local agentic software factory harness for Claude Code.",
  "skills": "./skills",
  "agents": "./agents",
  "hooks": "./hooks/hooks.json",
  "mcpServers": "./.mcp.json"
}



```

*Progressive Disclosure Rule Structure:* Keeps root instructions lean and delegates path-scoped rules to `.claude/rules/*.md`, loaded dynamically when touching relevant subtrees.

#### Hooks Configuration (`plugins/claude-code/hooks/hooks.json`)

```
{
  "hooks": [
    {
      "event": "WorktreeCreate",
      "command": "factory-core worktree init-state"
    },
    {
      "event": "PreToolUse",
      "matcher": "Read|Grep|Glob|Bash|Edit|Write",
      "command": "factory-core guard"
    },
    {
      "event": "PostToolUse",
      "matcher": "Edit|Write",
      "command": "factory-core fast-check"
    },
    {
      "event": "PostToolBatch",
      "matcher": "Edit|Write",
      "command": "factory-core fast-check"
    },
    {
      "event": "PostCompact",
      "command": "factory-core status --compact-summary"
    },
    {
      "event": "SubagentStop",
      "command": "factory-core validate-triage-file"
    },
    {
      "event": "Stop",
      "command": "factory-core holdout-gate"
    }
  ]
}



```

### 6.2 OpenAI Codex Plugin (`plugins/codex/`)

#### Manifest (`plugins/codex/.codex-plugin/plugin.json`)

```
{
  "name": "agentic-software-factory",
  "version": "2.4.0",
  "description": "OpenAI Codex integration bundle for local agentic software factory.",
  "skills": "./skills",
  "hooks": "./.codex/hooks.json",
  "mcpServers": "./.mcp.json"
}



```

#### Hooks Configuration (`plugins/codex/.codex/hooks.json`)

```
{
  "hooks": [
    {
      "event": "PreToolUse",
      "matcher": "^Bash$|^apply_patch$",
      "command": "factory-core guard"
    },
    {
      "event": "PostToolUse",
      "matcher": "^apply_patch$",
      "command": "factory-core fast-check"
    },
    {
      "event": "Stop",
      "command": "factory-core holdout-gate"
    }
  ]
}



```

*Note:* Codex hooks support `command` and `mcp_tool` handlers, with first-run trust approval required for hook execution.

#### Custom Agent Configurations (`plugins/codex/.codex/agents/adversarial-critic.toml`)

```
name = "adversarial-critic"
description = "Read-only adversarial diagnostic agent for root-cause failure triage."
developer_instructions = """
Analyze git diff and sanitized test failure logs against .factory-plan.md.
You are in read-only mode. Write analysis strictly to .factory-triage.md.
Do not execute code modifications or shell state mutations.
"""
read_only = true



```

### 6.3 Google Antigravity Plugin (`plugins/antigravity/`)

#### Manifest (`plugins/antigravity/plugin.json`)

```
{
  "name": "agentic-software-factory",
  "version": "2.4.0",
  "description": "Google Antigravity IDE and CLI harness extension.",
  "skills": "./skills",
  "rules": "./rules",
  "agents": "./agents",
  "hooks": "./hooks.json",
  "mcp": "./mcp_config.json"
}



```

*Note:* Installed to `.agents/plugins/agentic-software-factory/` (workspace) or `~/.gemini/config/plugins/agentic-software-factory/` (global), with `hooks.json` and `mcp_config.json` placed directly at the plugin root to satisfy `agy plugin validate --strict`.

#### Workspace Rules (`plugins/antigravity/rules/factory-invariants.md`)

```
---
trigger: always_on
---
# Antigravity Factory Invariants

- You are operating inside an agentic software factory environment.
- Always check for or create `.factory-plan.md` prior to code modifications.
- Access to `~/.factory-holdouts` is strictly forbidden and protected by systemic OS-level guards.
- If verification fails during a `/goal` execution, invoke the `adversarial-critic` subagent before attempting further code alterations.



```

*Progressive Disclosure Rule Structure:* Root `factory-invariants.md` remains minimal, referencing nested `AGENTS.md` files across module directories.

#### Hooks Configuration (`plugins/antigravity/hooks.json`)

```
{
  "hooks": [
    {
      "event": "PreToolUse",
      "matcher": "run_command|view_file|write_to_file|replace_file_content|multi_replace_file_content|grep_search|find_by_name",
      "command": "factory-core guard"
    },
    {
      "event": "PostToolUse",
      "matcher": "write_to_file|replace_file_content|multi_replace_file_content",
      "command": "factory-core fast-check"
    },
    {
      "event": "Stop",
      "command": "factory-core holdout-gate"
    }
  ]
}



```

#### MCP Configuration (`plugins/antigravity/mcp_config.json`)

```
{
  "mcpServers": {
    "factory-core": {
      "command": "factory-core",
      "args": ["mcp"]
    }
  }
}



```

*Note:* Antigravity automatically maps `.factory-plan.md` and `.factory-triage.md` to native Artifact side-panels.

### 6.4 Unified Cross-Platform UX & Single-Platform Quickstart Matrix

| Feature / Surface | Claude Code | OpenAI Codex | Google Antigravity | 
| ----- | ----- | ----- | ----- | 
| **Single-Platform Install** | `./install.sh --platform claude` | `./install.sh --platform codex` | `./install.sh --platform antigravity` | 
| **Plugin Manifest & Hooks** | `.claude-plugin/plugin.json` + `hooks/hooks.json` | `.codex-plugin/plugin.json` + `.codex/hooks.json` | Root `plugin.json` + root `hooks.json` + `mcp_config.json` | 
| **Specification & Plan** | Root `.factory-plan.md` validated via `factory-spec-plan` skill. | Root `.factory-plan.md` validated via skill protocol. | Root `.factory-plan.md` rendered automatically in native Artifact panel. | 
| **Triage Reports** | `.factory-triage.md` written by `adversarial-critic`; checked on `SubagentStop`. | `.factory-triage.md` generated by TOML-configured critic subagent. | `.factory-triage.md` rendered as interactive triage Artifact side-panel. | 
| **Parallel Worktrees** | `factory-core worktree create <id>` or native `--worktree` (`WorktreeCreate`). | `factory-core worktree create <id>` managed via workspace settings. | `factory-core worktree create <id>` mapped to isolated IDE threads. | 
| **Live Status Dashboard** | `factory-core status` CLI & MCP tool query. | `factory-core status` CLI & stdio MCP server tools. | `factory-core status` CLI, MCP, and IDE status widgets. | 

### 6.5 GitHub Marketplace & Team Distribution Spec

To make distribution across teams effortless, the monorepo is structured as a **GitHub-backed plugin marketplace** paired with a lightweight host CLI bootstrap:

#### Marketplace Catalog (`.claude-plugin/marketplace.json`)

```
{
  "name": "agentic-software-factory",
  "owner": {
    "name": "factory-maintainers"
  },
  "plugins": [
    {
      "name": "agentic-software-factory",
      "source": "./plugins/claude-code",
      "description": "Local-first agentic software factory with sealed Docker Compose hold-out oracles.",
      "version": "2.4.0"
    }
  ]
}



```

#### Two-Step Team Onboarding Flow

1. **Host Machine Bootstrap (Once per Workstation):**
   Install `factory-core` onto `PATH` (`~/.local/bin`) and verify mandatory Docker Engine + Docker Compose v2:

   ```
   git clone https://github.com/<owner>/agentic-software-factory.git
   cd agentic-software-factory && ./install.sh --core-only
   factory-core doctor
   
   ```

   *(Alternatively, `uv tool install git+https://github.com/<owner>/agentic-software-factory#subdirectory=factory-core`)*

2. **GitHub-Based Plugin Install in Target Platform:**

   * **Claude Code:**

     ```
     /plugin marketplace add <owner>/agentic-software-factory
     /plugin install agentic-software-factory@agentic-software-factory
     
     ```

     *(Teams can commit `extraKnownMarketplaces` and `enabledPlugins` in `.claude/settings.json` for automatic project-wide onboarding.)*

   * **OpenAI Codex:**
     Install `plugins/codex` from `<owner>/agentic-software-factory` via Codex plugin installer / `.codex/` bundle and approve the `.codex/hooks.json` trust prompt on first run.

   * **Google Antigravity:**
     Install `plugins/antigravity` from `<owner>/agentic-software-factory` into `.agents/plugins/agentic-software-factory/` or `~/.gemini/config/plugins/agentic-software-factory/` and run `agy plugin validate --strict`.

3. **Fail-Closed Safety Guarantee:**
   If a developer installs the plugin via GitHub marketplace before running `factory-core init` in a repository (or with Docker stopped), `factory-core holdout-gate` immediately exits with code `2` and instructs the user/agent to start Docker and run `factory-core init` rather than silently skipping hold-out checks.

## 7. Docker Compose Hold-Out Oracle Template

The files below define the hold-out environment structure initialized at `~/.factory-holdouts/<repo-slug>/`:

### Docker Compose Oracle Setup (`templates/holdout-repo-template/docker-compose.holdout.yml`)

```
services:
  sut:
    build:
      context: ${CLEAN_REPO_DIR}
      dockerfile: Dockerfile
    read_only: true
    tmpfs:
      - /tmp
      - /run
    networks:
      - holdout-net
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 2s
      timeout: 2s
      retries: 5

  verifier:
    build:
      context: .
      dockerfile: Dockerfile.verifier
    depends_on:
      sut:
        condition: service_healthy
    volumes:
      - ./suites:/holdout/suites:ro
      - ./fixtures:/holdout/fixtures:ro
    environment:
      - SUT_BASE_URL=http://sut:8080
    networks:
      - holdout-net
    command: ["pytest", "-q", "--tb=short", "/holdout/suites"]

networks:
  holdout-net:
    driver: bridge
    internal: true



```

### Base Verifier Dockerfile (`templates/holdout-repo-template/Dockerfile.verifier`)

```
FROM python:3.11-slim
WORKDIR /holdout
RUN pip install --no-cache-dir \
    pytest==8.0.2 \
    httpx==0.27.0 \
    jsonschema==4.21.1
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["pytest"]
CMD ["-q", "--tb=short", "/holdout/suites"]



```

## 8. AI Code Builder Acceptance Checklist & Verification Plan

An AI code builder completing implementation of this spec must execute and validate the following 11 steps:

```
+-----------------------------------------------------------------------------------+
|                            SYSTEM ACCEPTANCE VERIFICATION                         |
+-----------------------------------------------------------------------------------+
| [1] Preflight & Install--> Run ./install.sh & verify factory-core doctor          |
| [2] Initialization     --> Execute factory-core init in target repo               |
| [3] Security Guard     --> Verify realpath traversal & dual stderr/stdout output  |
| [4] Isolation Test     --> Verify worktree creation leaves main clean             |
| [5] Silent Fast-Check  --> Assert OK (1.2s) output vs architectural error hints   |
| [6] Compose Hold-Out   --> Verify untracked patch, no host ports, & teardown     |
| [7] State Persistence  --> Confirm rejected_approaches[] saved in state file      |
| [8] Telemetry & GC     --> Verify telemetry.jsonl emission & factory-core gc run  |
| [9] Circuit Breaker    --> Confirm attempts counter trips at MAX_RETRIES (3)      |
| [10] Status & MCP UX   --> Test factory-core status & factory-core mcp responses  |
| [11] Marketplace/Hooks --> Validate marketplace.json & Claude/Codex/agy plugins   |
+-----------------------------------------------------------------------------------+



```

### Step-by-Step Validation Sequence

 1. **Environment Installation & Mandatory Docker/Compose Preflight:**

    Execute `./install.sh --platform all` and `factory-core doctor`. Confirm that `factory-core` is available in `~/.local/bin`, Docker Engine + `docker compose` v2 checks pass, `~/.factory-holdouts/` is created, and `--core-only` / `--platform claude|codex|antigravity` flags install only the requested targets. Also test `DOCKER_HOST=unix:///nonexistent.sock factory-core doctor` to confirm fail-closed exit code `2`.

 2. **Repository Initialization:**

    Run `factory-core init` within a test Git repository. Confirm that `.git/info/exclude` contains `.worktrees/` and `.factory-state.json`, and that `~/.factory-holdouts/<repo-slug>/` is populated.

 3. **Security Guard Verification:**

    Execute the path guard directly with standard inputs to ensure path traversal attempts are blocked (`exit code 2`) on both `stderr` and `stdout` JSON:

    ```
    echo '{"tool_name":"Bash","tool_input":{"command":"cat ~/.factory-holdouts/test.py"}}' | factory-core guard
    # Expect exit code 2, stderr message, & stdout JSON payload containing "decision": "deny"
    
    echo '{"tool_name":"Bash","tool_input":{"command":"cat ../../.factory-holdouts/test.py"}}' | factory-core guard
    # Expect exit code 2, stderr message, & stdout JSON payload containing "decision": "deny"
    
    
    
    ```

 4. **Worktree Isolation Check:**

    Execute `factory-core worktree create task-verification-01`. Confirm creation of `.worktrees/task-verification-01` and verify `git status` on the primary branch remains clean.

 5. **Silent Fast-Check & Architectural Guard Validation:**

    Run `factory-core fast-check` on clean code to confirm single-line `OK (<duration>)` output. Introduce an invalid cross-module import or lint error to verify structured failure diagnostics with actionable remediation hints.

 6. **Hold-Out Oracle Gate & Sanitizer Validation:**

    Add a temporary untracked file to `.worktrees/task-verification-01/src/` and run `factory-core holdout-gate`. Confirm that:

    * The patch exporter captures both modified and untracked files.

    * Container execution runs via `docker compose` without host port collisions.

    * All ephemeral containers and volumes are cleaned up (`down -v`) on completion.

    * Failure logs output to standard terminal streams redact absolute host paths, fixture details, and inner stack frames.

 7. **State Persistence & Compaction Check:**

    Inspect `.factory-state.json` after a failing hold-out gate execution. Confirm that `rejected_approaches[]` contains an entry detailing `attempt`, `summary`, `files_touched`, and `failed_contract`, and verify `factory-core status --compact-summary` renders it cleanly.

 8. **Telemetry & Garbage Collection Test:**

    Verify that `~/.factory-holdouts/<repo-slug>/telemetry.jsonl` appends structured run records. Execute `factory-core gc --dry-run` followed by `factory-core gc` to confirm cleanup of stale worktrees, Docker networks, and triage files.

 9. **Circuit Breaker Trip Check:**

    Trigger three sequential failures in `factory-core holdout-gate`. Confirm that on attempt `3`, `attempts` reaches `max_retries`, the circuit breaker escalation message prints, and the process returns `exit code 0`.

10. **Status & MCP Server Verification:**

    Run `factory-core status --json` and send a JSON-RPC `tools/list` + `tools/call` request to `factory-core mcp`.

11. **Marketplace Manifest & Multi-Platform Plugin Bindings:**

    * Validate `.claude-plugin/marketplace.json` and confirm fail-closed exit code `2` when `factory-core holdout-gate` is invoked in an uninitialized repo.

    * **Claude Code:** Run `claude plugin validate ./plugins/claude-code` and launch `claude --plugin-dir ./plugins/claude-code` to verify hook, MCP, and skill registration.

    * **OpenAI Codex:** Verify `plugins/codex/.codex-plugin/plugin.json`, `plugins/codex/.codex/hooks.json`, `plugins/codex/.mcp.json`, and `plugins/codex/.codex/agents/adversarial-critic.toml` parse cleanly.

    * **Google Antigravity:** Run `agy plugin validate --strict ./plugins/antigravity` to confirm `plugin.json`, root `hooks.json`, `mcp_config.json`, `trigger: always_on` rules, and native tool matchers pass strict validation.