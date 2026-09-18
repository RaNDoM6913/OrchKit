# ORCH v0.4 — registered-project task enqueue adapter

**Date:** 2026-09-18.

## Problem addressed

The durable multi-project queue previously still required an operator to create/load plan JSON for each new piece of work, and a plan could only depend on tasks declared in that same file. That prevented natural incremental DAG construction across plan revisions.

## Direct enqueue

`orch queue enqueue PROJECT_ID` now compiles one bounded task from the registered project policy and loads it into the durable queue. It supports task id, goal, allowed paths, dependencies, risk tags, owner approval, max attempts, and an optional explicit plan revision.

The compiled plan is persisted under `<ORCH_HOME>/plans/<plan_revision>.json` with mode `0600`. Exact replay of the same explicit revision is idempotent; different content for an existing artifact fails with `plan_artifact_conflict` rather than overwriting audit evidence. A newly created artifact is removed again if durable plan loading fails.

Persisted plans are included in secret-free state backups.

## Cross-plan DAG

A new plan may depend on immutable task ids already present in the SQLite ledger. Unknown external dependencies are rejected inside the same SQLite transaction as plan loading, so no partial tasks are queued. Same-plan cycle detection remains intact; external durable dependencies cannot create a backward cycle because task ids are globally immutable and a prior task could not have referenced a not-yet-existing id.

For a dependent Git task, the adapter intentionally omits a stale registration-time `expected_base`. The verifier later binds the exact Git HEAD after predecessor completion, and race-safe publication uses that frozen base.

## Deterministic coverage

Tests cover cross-plan execution order, atomic rejection of unknown dependencies, direct registered-project enqueue, `0600` plan persistence, dependent dynamic-base compilation, max-attempt propagation, idempotent replay, and conflicting revision preservation.
