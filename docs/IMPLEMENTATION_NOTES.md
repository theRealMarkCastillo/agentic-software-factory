# Implementation Notes

`ARCHITECTURE.md` is the v2.4 specification, unmodified. This file records where the
implementation departs from it. Each departure follows what the installed tools accept
(`claude plugin validate`, `agy plugin validate --strict`) or fixes a defect in the
spec's reference code.

## Platform manifests and hooks

| Spec | Implementation | Reason |
| --- | --- | --- |
| Hooks as a flat list of `{event, matcher, command}` | Claude Code and Codex: `{"hooks": {"<Event>": [{"matcher", "hooks": [{"type": "command", ...}]}]}}` | The flat form is not a schema either harness loads. |
| Antigravity `hooks.json` shaped like Claude Code's | Named hooks at the top level; `Stop` takes a flat handler list | `agy` reads a top-level `hooks` key as one hook named "hooks" and skips the file. |
| Antigravity hook exit codes, payload schema, and working directory | Hooks exit `0` with stdout JSON (`{"decision":"deny"}` on `PreToolUse`, `{}` on `PostToolUse`, `{"decision":"continue"}` / `{"decision":"stop"}` on `Stop`) and resolve the workspace from `toolCall.args` / `workspacePaths` | Antigravity runs plugin hooks from `<plugin-root>`, wraps tool calls in `toolCall.{name,args}`, and treats any non-zero exit code as a hook error that fails open. |
| Antigravity `plugin.json` with `skills`, `rules`, `agents`, `hooks`, `mcp` | Those fields removed; `$schema`, `logo`, and `suggestedPrompts` added | `agy` reports unknown component-path fields as ignored, which fails `--strict`. Components are discovered by directory. |
| Claude `"agents": "./agents"` | A list of the two agent files | The field accepts files, not a directory. |
| Codex files under `.codex/` | `hooks/hooks.json`, `agents/*.toml`, `config/factory.toml` | Layout requested for this build. `.agents/plugins/marketplace.json` was added so `codex plugin marketplace add` works. |
| Codex critic `read_only = true` | `sandbox_mode = "read-only"`; the calling agent saves the critic's analysis to `.factory-triage.md` | A read-only agent cannot also write the triage file. |
| `WorktreeCreate` runs `worktree init-state` on an existing worktree | `worktree init-state --hook` creates the worktree named on stdin and prints its path | In Claude Code a `WorktreeCreate` hook replaces built-in creation and must print the path. |
| `PostCompact` re-injects state | `PostCompact` kept; `SessionStart` (`compact\|resume`) added | Claude Code does not pass `PostCompact` output to the model. `SessionStart` output is. |
| `PostToolUse` fast-check exits 1 | Hooks call `fast-check --hook`, which exits 2 on stderr | Exit 1 from a `PostToolUse` hook is shown to the user only. The plain CLI still exits 1. |
| `PostToolBatch` with matcher `Edit\|Write` | No matcher; the command skips batches without an edit | `PostToolBatch` ignores matchers. |
| Hook commands call `factory-core` directly | Guard and gate commands first check that `factory-core` is on `PATH` and exit 2 if not | A missing binary exits 127, which harnesses treat as a non-blocking error. That would skip the gate silently. |

## Engine

- **Verifier command.** The spec's `Dockerfile.verifier` sets `ENTRYPOINT ["pytest"]` while
  the Compose file passes `command: ["pytest", ...]`, which runs `pytest pytest ...` and
  fails with "file or directory not found". The Compose command now holds arguments only.
- **Patch export.** Factory bookkeeping (`.factory-state.json`, `.factory-plan.md`,
  `.factory-triage.md`) is removed from the temporary index, so it never reaches the SUT
  or the tree hash.
- **Path guard.** The reference skips every token starting with `-`, resolves relative
  paths against the hook process rather than the payload's `cwd`, ignores quoting tricks
  (`.factory-hold''outs`), and allows a malformed payload. The implementation closes those
  gaps, also blocks `docker`/`podman` invocations and recursive commands over a parent of
  the hold-outs, and protects `.factory-state.json` from agent writes. It remains a
  tripwire rather than a sandbox; see the README.
- **Sanitizer.** In addition to the reference patterns, it keeps only the verifier's
  output, drops echoed test source lines, and redacts literal secret values read from
  the golden fixtures.
- **Circuit breaker.** The failure that brings `attempts` to `max_retries` prints the
  breaker banner and exits 0, as acceptance step 9 requires. Later runs also exit 0
  without starting Docker.
- **Environment failures.** A missing `Dockerfile`, a patch that does not apply, or a
  stopped daemon exits 2 without consuming an attempt. Candidate SUT build or healthcheck
  failures during `docker compose up` do count toward the circuit breaker (with explicit
  startup diagnostics when `verifier` never started) so broken startup code cannot trap
  `Stop` hooks in an infinite loop.
- **Teardown.** `docker compose down -v --remove-orphans --rmi local`. Removing the
  per-run images keeps one image per commit from accumulating; BuildKit's layer cache
  keeps rebuilds fast.
- **Garbage collection.** A worktree with uncommitted changes is never removed. A
  worktree counts as merged only after the gate has evaluated it. Docker resources
  younger than 60 seconds or belonging to a running project are left alone.
- **Outside a repository.** `holdout-gate` exits 0 with a note when the working directory
  is not a Git repository, so an unrelated session is not trapped by the Stop hook.
- **fast-check.** Checkers that are not installed are listed in the `OK` line as skipped
  rather than reported as passing. Checkers run with the current checkout first on
  `PYTHONPATH`, so a virtualenv shared between worktrees still analyses the right code.
  Banner art is dropped from failure output.
- **Installer.** `uv tool install` is called with `--reinstall`. Without it uv reuses a
  cached build when only source files changed, and `~/.local/bin/factory-core` stays stale.
- **Docker socket.** The guard also covers the sockets of desktop runtimes under the home
  directory (Docker Desktop, OrbStack, Colima, Rancher Desktop) and a `unix://` `DOCKER_HOST`.

- **Gate scope.** A repository without a hold-out suite is not gated (exit 0 with a note on
  stderr) instead of blocking, because plugins are enabled for every repository. A worktree
  whose tree still matches its creation baseline is skipped.
- **Environment failures.** A non-zero Compose exit where the verifier never started does not
  consume an attempt. It is counted separately (`env_failures`) and ends the loop after three
  consecutive runs. Prerequisite failures honour `stop_hook_active` on Claude Code and Codex.
  Unexpected exceptions in the gate or guard block instead of exiting 1.
- **Attempt ledger.** `record_failure` mirrors attempts and rejected approaches to
  `attempts-ledger.json` in the hold-out directory. A missing state file is restored from it.
  `worktree create` and a human `worktree init-state` clear the entry.

## Not verified locally

- Codex has no plugin validator. Its manifest, hooks, and agent TOML files are checked
  for syntax and against the layout of plugins shipped in Codex's own catalog.
- OTLP export is tested against a stub, not a real collector.
