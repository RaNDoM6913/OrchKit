# ORCH post-v0.7 — atomic batch task adapter

**Date:** 2026-09-18. **Scope:** generic registered-project task admission; no market-workstation integration.

## Command

```sh
orch queue enqueue-batch PROJECT_ID /absolute/path/to/tasks.json
```

The source manifest is read-only input. It must be a regular non-symlink JSON file, schema version 1, and no larger than 256 KiB. ORCH computes its SHA-256 and embeds provenance in the compiled durable plan.

Manifest tasks support `id`, `goal`, `allowed_paths`, `dependencies`, `risk_tags`, `owner_acceptance`, and `max_attempts`. An optional top-level `plan_revision` may be supplied.

## Atomic admission

Before reading task work into the queue, the existing project readiness audit must not be `BLOCKED`. ORCH compiles all manifest tasks into one plan artifact and loads the entire DAG in one SQLite transaction.

Unknown dependencies, duplicate task ids, dependency cycles, invalid task definitions, or ledger conflicts fail without partially adding tasks. If ORCH created the plan artifact for that attempt, it removes that artifact when transactional loading fails.

## Git base binding across durable queues

This block also fixes an existing queue defect. Multiple independent Git tasks can be admitted before earlier tasks execute. Persisting the same admission-time `expected_base` on every task makes later tasks stale after an earlier ORCH publication advances the branch.

Admission now uses these rules:

- if a project has no unresolved durable tasks, the first newly admitted Git task may bind the current admission HEAD;
- if unresolved project work already exists, a new task uses `dynamic_at_verify` base binding;
- in a batch, only the first task may bind the admission HEAD when the prior project queue is empty; later batch tasks bind their exact base during verification;
- explicit dependencies retain dynamic verification-time binding as before.

The verifier still freezes the exact observed Git HEAD into the snapshot, and publication still uses compare-and-swap protection. Dynamic admission therefore removes false stale-base failures without weakening publication-time race protection.

## Deterministic evidence

Tests cover a two-task batch DAG, source SHA provenance, duplicate ids, unknown dependencies, dependency cycles, symlink-manifest refusal, and atomic cleanup. A real disposable Git E2E pre-queues two independent tasks, publishes the first, then verifies and publishes the second against the advanced HEAD; the second remote commit becomes the exact bare-remote branch head.
