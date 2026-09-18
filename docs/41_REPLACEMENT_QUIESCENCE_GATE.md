# ORCH v0.4 — replacement quiescence gate

**Date:** 2026-09-18.

## Defect addressed

`state READY`, `reconcile CLEAN`, and `recovery inspect CLEAN` prove that no writer/publication recovery is active, but they do not mean the queue is empty. A home can contain `PLANNED`, `READY`, `NEEDS_FIX`, `BLOCKED`, or other unresolved tasks without an active run.

Replacing such a live home from another backup would discard durable queued work even though no writer lock existed.

## Strong replacement precondition

Existing-home replacement now requires **all** durable tasks in the live home to be terminal: only `DONE` and explicitly `CANCELLED` tasks are acceptable. Any other task status fails closed with `replacement_live_nonterminal_tasks:<TASK_ID>` before the candidate backup is restored or any filesystem rename occurs.

Global or project-level durable pause settings also block replacement with `replacement_live_pause_present`. A pause is an operator control and must not be bypassed simply because there is no active worker.

This gate composes with the existing requirements: standalone-home inventory, state `READY`, reconcile `CLEAN`, recovery `CLEAN`, and verified backup.

## Deterministic evidence

Tests prove that a `PLANNED` task blocks replacement even while state/reconcile/recovery individually look clean, a globally paused home blocks replacement, and the previous active-writer refusal remains intact.

Full deterministic acceptance after this block: **118/118 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.
