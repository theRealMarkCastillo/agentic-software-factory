# Agentic Software Factory for Claude Code

This plugin binds Claude Code to the shared `factory-core` engine. It adds a path guard
before tool calls, fast lint and boundary checks after edits, and a sealed Docker
Compose hold-out gate when the agent finishes. It also bundles the
`factory-spec-plan`, `factory-implement` and `factory-triage` skills, the
`implementer` and `adversarial-critic` agent profiles, and a read-only MCP server
for factory state.

## Install

```
/plugin install agentic-software-factory --marketplace theRealMarkCastillo/agentic-software-factory
```

## Setup

1. Install the engine once per workstation: `./install.sh --core-only`, then `factory-core doctor`.
   Docker Engine and Docker Compose v2 must be running.
2. Run `factory-core init` in each repository the factory should manage.

The guard fails closed if `factory-core` is missing. Repositories without a hold-out
suite are skipped until you run `factory-core init`, while initialized repositories
(and workspaces with active factory task files) fail closed. See the repository README
and `docs/ARCHITECTURE.md` for the full design.
