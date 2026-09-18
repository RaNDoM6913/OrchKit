# ORCH v0.4 — project-level queue pause/resume

**Date:** 2026-09-18.

## Problem addressed

The durable queue can run independent projects concurrently, but the only pause control was global. Operational maintenance or a suspected problem in one project therefore required stopping dispatch for every registered project.

## Durable project pause

ORCH now stores project pause state in the durable SQLite settings ledger. Operators can use:

```sh
orch queue pause-project PROJECT_ID --reason "maintenance"
orch queue resume-project PROJECT_ID
```

A project pause affects new dispatch only. It does not abort, revoke, or mutate an already active run. This preserves the existing explicit crash/recovery contract: active work must still finish or be handled with the normal explicit abort/retry flow.

When a project is paused:

- `claim --project PROJECT_ID` and `next --project PROJECT_ID` return `PROJECT_PAUSED`;
- unscoped claim/next skip that project's ready tasks and can continue with independent projects;
- if paused projects are the only remaining runnable queues, unscoped dispatch reports `PROJECTS_PAUSED` rather than misleadingly returning `NO_WORK`;
- `queue list` marks ready tasks as `PAUSED_PROJECT` and reports durable pause details.

The packaged and repository-local dispatcher prompts treat `PROJECT_PAUSED` and `PROJECTS_PAUSED` as intentional stop states and do not self-rearm.

## Safety semantics

Pause/resume never edits target repositories and does not alter task payloads, snapshots, publication journals, writer keys, capabilities, or Git state. The pause key is bounded by the validated project id and reason length and is included automatically in SQLite state backups.

Resuming a project restores its original durable FIFO position; ORCH does not silently reprioritize paused tasks.

## Deterministic coverage

Tests prove that:

- a paused earlier project is skipped while an independent project can still be claimed;
- pause state survives a new `Orchestrator` instance;
- queue UX exposes `PAUSED_PROJECT` and pause metadata;
- resume restores dispatch eligibility;
- pausing does not interrupt an active writer;
- CLI pause/list/resume behavior is wired end to end;
- dispatcher prompts recognize both project-pause stop states.

Full deterministic acceptance after this block: **76/76 PASS**, Python compile PASS, and `git diff --check` PASS.
