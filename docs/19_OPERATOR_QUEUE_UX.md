# ORCH v0.4 — operator queue UX

**Date:** 2026-09-18.

## New commands

```sh
orch queue list [--project PROJECT_ID] [--limit N]
orch queue cancel TASK_ID --reason "..."
```

`queue list` is bounded (`1..500`, default 100) and reports both durable task status and computed queue state. Queue-state explanations include `READY`, `ACTIVE`, `WAITING_WRITER`, `WAITING_DEPENDENCY`, `EXHAUSTED`, plus terminal/task states such as `DONE`, `BLOCKED`, or `CANCELLED`.

The output includes per-state counts, active writer locks, attempt/max-attempt counters, dependency blockers, project identity, writer identity, and FIFO sequence. A project filter reuses the same durable SQLite queue rather than creating independent state silos.

## Safe cancellation

Cancellation is intentionally narrow. ORCH only allows cancellation while a task is still `PLANNED`, `READY`, `NEEDS_FIX`, or `BLOCKED`, and refuses cancellation when an active run exists. Publication/owner-waiting states are not cancellable through this queue command because they already have durable result/snapshot semantics that must be reconciled explicitly.

Cancellation is durable (`CANCELLED`) and evented (`TASK_CANCELLED`). Repeating cancellation is idempotent. Dependents are not silently cascaded; they remain visibly blocked on the cancelled dependency so an operator must make the follow-up decision explicitly.

Deterministic tests cover writer-lock/dependency explanations, project filtering, bounded output, cancellation, active-run refusal, and idempotency.
