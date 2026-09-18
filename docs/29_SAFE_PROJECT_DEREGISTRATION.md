# ORCH v0.4 — safe project deregistration

**Date:** 2026-09-18.

## Defect addressed

Registered-project policy is stored separately from already compiled durable tasks. Previously, `orch project remove PROJECT_ID` deleted the registry file even when that project's queue still contained runnable work or a verified snapshot reservation. Removing the registry therefore did not actually stop already loaded work, creating a dangerous mismatch between operator intent and dispatcher behavior.

## Removal guard

`project remove` is now fail-closed against the SQLite ledger. Before deleting the registry entry ORCH reports and checks:

- nonterminal durable tasks;
- active or verified writer reservations;
- unfinished publication journal entries;
- project-level pause state;
- historical task count.

Only `DONE` and explicitly `CANCELLED` tasks are terminal for deregistration. `BLOCKED`, `NEEDS_FIX`, `WAITING_REVIEW`, `WAITING_OWNER`, `READY_TO_PUBLISH`, active runs, and other unresolved states prevent removal.

A blocked remove is non-destructive and returns `status=BLOCKED` with the durable blockers. The registry config remains intact.

## Successful removal

Once all project work is terminal, ORCH removes the registry entry, clears any durable project-pause setting, and records `PROJECT_DEREGISTERED` in the event ledger. Historical task/run/snapshot evidence remains in SQLite and continues to follow normal retention policy; deregistration is not history deletion.

The intended retirement flow is therefore explicit:

```sh
orch queue list --project PROJECT_ID
orch queue cancel TASK_ID --reason "owner retired project"   # for unresolved queued work
orch project remove PROJECT_ID
```

## Deterministic coverage

Tests prove that a queued task blocks deregistration, an exact `VERIFIED` snapshot reservation blocks deregistration, cancellation makes the queued case removable, the project config is preserved on blocked attempts, and successful removal clears project-pause state without deleting ledger history.

Full deterministic acceptance after this block: **78/78 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.
