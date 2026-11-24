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
