# ORCH post-v0.7 — verifier check execution authority

**Date:** 2026-09-18. **Scope:** deterministic verifier-command authority hardening.

## Problem

Task checks are intentionally executable commands. Before this block, a task stored only the check argv/cwd. At verification time argv[0] was resolved again through the current PATH, and script/config files consumed by that check could change after task admission. A worker or concurrent process could therefore change what the verifier executes without changing the durable task definition.

## Bound authority

When a plan is loaded, ORCH now prepares each check into durable execution authority:

- argv[0] is resolved once to an absolute executable path;
- the executable SHA-256 is stored in the task payload;
- relative file arguments that resolve to workspace files are bound by SHA-256;
- explicit `authority_paths` and `authority_absent_paths` are supported;
- npm/yarn/pnpm run checks bind `package.json` and relevant package-manager config presence/absence.

Before any check executes, verification revalidates the executable and every authority record. Drift produces `check_authority_changed:<check-id>:...` and blocks the run before executing the changed command. The actual subprocess uses the bound absolute executable rather than resolving argv[0] from PATH again.

Authority evidence is written to the ORCH logs and returned with successful verification results. This remains a cooperative verifier boundary: approved checks are still allowed to execute their declared behavior; the change prevents post-admission substitution of the command or its bound support inputs.

## Deterministic evidence

New tests prove that:

1. a workspace executable is persisted and invoked through its bound absolute path;
2. mutating that executable after plan admission blocks verification before the mutated code executes;
3. mutating a relative support script after admission blocks verification before the check runs.

The prior 147-test baseline passed unchanged before adding these tests. No Codex/model review is needed for this deterministic hardening block.
