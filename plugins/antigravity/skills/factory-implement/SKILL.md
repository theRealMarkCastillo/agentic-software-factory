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
