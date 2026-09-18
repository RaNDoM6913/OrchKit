# ORCH post-v0.6 — read-only project readiness audit

**Date:** 2026-09-18. **Scope:** registered-project safety/readiness inspection only. No target-project writes are performed.

## Command

```sh
orch project audit PROJECT_ID
orch project audit PROJECT_ID --require-dispatcher
```

The audit is intentionally fail-closed and does not construct an `Orchestrator`, migrate SQLite, repair permissions, create a missing ledger, create a dispatcher, or mutate the registered Git workspace. SQLite is opened in read-only mode.

The output is machine-readable JSON with top-level `READY`, `ATTENTION`, or `BLOCKED`, plus individual checks, blockers, attention items, and a compact project/queue summary.

## Authority and integrity checks

The audit verifies:

- private project-registry structure/config mode and unique exact-root identity;
- the project-id root-hash suffix against the currently configured root;
- configured writer identity against the independently observed Git common directory;
- durable ledger existence, schema version, SQLite quick/FK checks, and critical ORCH-owned permissions;
- ledger task writer identities against the current project writer key;
- global/project pause state, writer reservations, pending publications, capability anomalies, and non-nominal task states;
- Git branch/staging/protected-baseline policy and forbidden force/reset/clean/stash authority;
- registered remote drift and transport safety;
- new dirty/staged/untracked paths that were not part of the registered protected baseline;
- review policy validity;
- RDC binding presence/shape/private mode;
- optional or required project-scoped dispatcher presence, private mode, and actual project-bound prompt fragments.

A new unprotected workspace change is a blocker even when ordinary Git policy would otherwise remain `READY`. This closes the gap where owner/concurrent bytes appeared after registration but before task creation.

`ATTENTION` is used for recoverable operational states such as an active writer, project/global pause, missing expected capability, pending publication, repair-needed task, or a required dispatcher that has not yet been rendered. Integrity/authority drift is `BLOCKED`.

## Deterministic evidence

A dedicated 13-test suite covers clean readiness, no-side-effect reads, missing-ledger refusal without SQLite creation, CLI no-init behavior, foreign workspace bytes, writer/root/remote drift, ledger writer drift, active writer and project pause attention, required scoped dispatcher readiness, and stale dispatcher rejection.

After this block the complete source suite passes **138/138** tests. Python compilation and `git diff --check` pass. No Codex/model review is required for this deterministic block.
