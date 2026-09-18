# ORCH v0.4 — project registry / ledger authority binding

**Date:** 2026-09-18.

## Problem addressed

After CLI state initialization became lazy, `project add` could create a registry entry without creating SQLite state. More importantly, `project remove` was ledger-dependent and could previously instantiate a brand-new empty database if the authoritative ledger had been lost or moved. An empty replacement ledger would make durable work appear absent and could incorrectly permit deregistration.

## Registration contract

A CLI `project add` now initializes the ORCH ledger before writing the project registry entry. Every newly registered project therefore has an authoritative durable state database from the beginning, even before the first task is queued.

This changes only ORCH-owned state. Project registration remains read-only toward the target Git repository.

## Deregistration contract

`project remove` performs its ledger preflight before constructing an `Orchestrator`. If `<ORCH_HOME>/.runtime/orch.sqlite3` is missing, symlinked, or not a regular file, removal returns `BLOCKED` with `project_state_ledger_missing_or_unsafe` and does not create a replacement database.

The operator is directed to restore/reconcile the authoritative state rather than use `orch init` to manufacture an empty ledger around the guard.

When the authoritative ledger exists, the existing durable project-removal guard still requires all project work to be terminal before removing the registry config.

## Deterministic evidence

Tests prove that CLI project registration creates the ledger, a registry with a missing ledger cannot be removed and no SQLite file appears as a side effect, and an immediately registered project with no queued work can be safely deregistered through its existing empty ledger.

Full deterministic acceptance after this block: **112/112 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.
