# Agentic Software Factory

A local-first harness that lets coding agents work in isolated Git worktrees and
verifies their output against acceptance tests the agent cannot read. It ships as
native plugins for Claude Code, OpenAI Codex, and Google Antigravity, all backed by
one shared CLI, `factory-core`.

- **Sealed hold-out oracle.** Acceptance tests live in `~/.factory-holdouts/<repo>/`,
  outside the repository. They run in a disposable Docker Compose project against a
  clean copy of `HEAD` plus the agent's patch.
- **Sanitized feedback.** A failing run reports the violated contract and the observed
  behavior. Test source, file paths, and fixture secrets are stripped.
- **Circuit breaker.** Three failed attempts stop the retry loop and hand control back
  to you. Rejected approaches persist in `.factory-state.json` across context compaction.
- **Quiet when green.** Passing checks print one line, `OK (1.2s)`.

## Requirements

Docker Engine with a running daemon and Docker Compose v2 are mandatory. There is no
host-shell fallback: `install.sh`, `factory-core doctor`, `init`, and `holdout-gate`
exit with code 2 when Docker is unavailable. You also need Python 3.11+ and Git 2.30+.

## Quickstart

```bash
git clone https://github.com/theRealMarkCastillo/agentic-software-factory.git
cd agentic-software-factory
./install.sh --core-only        # installs factory-core to ~/.local/bin
factory-core doctor
```

Then, in each repository the factory should manage:

```bash
factory-core init                          # creates ~/.factory-holdouts/<repo>/ from the template
factory-core worktree create my-task       # prints the path of .worktrees/my-task
```

Launch your agent session or workspace rooted inside `.worktrees/my-task` (or use
`claude -w my-task` in Claude Code) so edits, fast checks, and the `Stop` hold-out gate
all operate on the isolated task worktree.

`init` writes an example contract. Replace it with real acceptance tests in
`~/.factory-holdouts/<repo>/suites/`. Your repository needs a `Dockerfile` at its root
that serves the application on port 8080 with a `/health` endpoint, and the image must
contain `curl` for the health check.

Without `uv`, `install.sh` falls back to a virtualenv in `~/.local/share/factory-core`.
You can also install the engine directly:

```bash
uv tool install "git+https://github.com/theRealMarkCastillo/agentic-software-factory#subdirectory=factory-core"
```

## Install a plugin

Install the engine first. The guard and gate hooks fail closed, so a plugin installed
before `factory-core` blocks tool calls until the engine is present.

### From the GitHub marketplace

Claude Code:

```
/plugin marketplace add theRealMarkCastillo/agentic-software-factory
/plugin install agentic-software-factory@agentic-software-factory
```

OpenAI Codex:

```bash
codex plugin marketplace add theRealMarkCastillo/agentic-software-factory
codex plugin add agentic-software-factory@agentic-software-factory
```

Approve the hooks trust prompt on first run. Copy `plugins/codex/agents/*.toml` to
`~/.codex/agents/` and merge what you need from `plugins/codex/config/factory.toml`.

Google Antigravity:

```bash
agy plugin install https://github.com/theRealMarkCastillo/agentic-software-factory/tree/main/plugins/antigravity
agy plugin validate --strict ~/.gemini/config/plugins/agentic-software-factory
```

### From a local clone, one platform at a time

```bash
./install.sh --platform claude
./install.sh --platform codex
./install.sh --platform antigravity
./install.sh --platform all
```

Teams can commit `extraKnownMarketplaces` and `enabledPlugins` in `.claude/settings.json`
to onboard a whole project.

## What runs when

| Moment | Command | Effect |
| --- | --- | --- |
| Before a tool call | `factory-core guard` | Blocks access to the hold-outs, the Docker socket, and `.git/hooks/` |
| After an edit | `factory-core fast-check` | Lint, types, and import-boundary checks, 15 second budget |
| When the agent stops | `factory-core holdout-gate` | Runs the sealed oracle; exit 2 sends the agent back to work |
| After compaction | `factory-core status --compact-summary` | Re-injects attempts and rejected approaches |
| When the critic finishes | `factory-core validate-triage-file` | Requires the three triage sections |

Other commands: `factory-core status [--json|--watch]`, `factory-core mcp` (read-only
stdio MCP server), and `factory-core gc [--dry-run] [--max-age-days N]`.

After the circuit breaker trips, a human resets it by deleting `.factory-state.json` in
the worktree and running `factory-core worktree init-state`. The guard blocks agents from
running that command, and attempts are mirrored in the hold-out directory, so deleting the
state file alone does not reset the count.

The gate only runs in repositories that have a hold-out suite. It skips a worktree that has
not changed since it was created. A run where the verifier never starts (an image fails to
build, or the SUT fails its health check) is reported as an environment failure and does not
use an attempt; three in a row end the retry loop with an `UNVERIFIED` notice. When Docker
itself is unavailable, Claude Code and Codex block the first stop and release the second
with the same notice, so a session cannot loop on a problem the agent cannot fix.

## What the guard does and does not guarantee

The path guard resolves symlinks, `..` traversal, globs, and environment variables, and
it blocks direct `docker` use. It is a deterministic tripwire, not a sandbox. The agent
runs as your OS user, so a command that assembles a path at runtime can evade static
inspection. For a hard boundary, run the agent inside your harness's OS sandbox or a
container with no mount of `~/.factory-holdouts`.

Two repositories with the same directory name share one hold-out directory, because the
slug is the repository's basename.

## Development

```bash
cd factory-core
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Skills are edited in `shared-skills/` and copied into each plugin with
`./scripts/sync-skills.sh`. A test fails if the copies drift.

`docs/ARCHITECTURE.md` is the v2.4 specification. `docs/IMPLEMENTATION_NOTES.md` lists
where the implementation departs from it and why.
