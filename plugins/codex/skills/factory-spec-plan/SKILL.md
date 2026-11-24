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
