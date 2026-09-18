# ORCH v0.4 — multi-project durable queue

**Date:** 2026-09-18. **Scope:** independent `agent-workflow-orchestrator` only. <separately-authorized-integration> remains disabled.

## Problem addressed

v0.3 could register multiple projects, but execution still behaved like one global writer lane. Every plan restarted task `ordinal` at zero, so tasks loaded from separate plans were ordered by task id rather than durable enqueue order. Any active run also returned global `BUSY`, even when the next task belonged to an independent repository.

## Queue and writer model

State schema v3 adds durable task metadata: `project_id`, `writer_key`, and monotonic `queue_seq`.

- `queue_seq` is assigned transactionally when a new task is loaded, preserving FIFO order across independently loaded plans.
- Registered-project plans carry their `project_id` and a stable Git writer key derived from `git rev-parse --git-common-dir`.
- Legacy/manual plans without a writer key receive a stable workspace-derived key.
- Claim remains atomic under SQLite `BEGIN IMMEDIATE`.
- A writer lock is now per writer key rather than global: independent repositories may have simultaneous active runs, while tasks sharing one Git common directory remain mutually exclusive.
- `claim --project PROJECT_ID` and `next --project PROJECT_ID` allow a dispatcher/operator to scope work without creating a separate database.

## Migration behavior

Opening an existing schema-v2 home migrates tasks in place to schema v3. Existing rows retain deterministic queue order based on their durable row order, receive project identity from their payload when present, and receive a derived writer key otherwise. Unknown future schema versions remain fail-closed.

## Deterministic verification

Coverage added for:

- FIFO ordering across separately loaded plans whose task ids would otherwise sort differently;
- simultaneous claims in two independent workspaces;
- blocking two workspaces that intentionally share one writer key;
- project-scoped claim/next behavior;
- registered-project plan writer identity;
- schema-v2 task migration to v3.

The dispatcher remains compatible without a project filter: it simply claims the oldest runnable task whose writer key is currently free. Existing Variant-B semantics remain unchanged: every attempt is still a fresh ChatGPT conversation with durable handoff through ORCH/RDC.
