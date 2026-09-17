# ORCH-013 — Variant B decision and implemented MVP

**Date:** 2026-09-17. **Status:** IMPLEMENTED_FOR_DISPOSABLE_MVP. **Owner decision:** "делай вариант Б".

## Contract change

The same-chat requirement from ORCH-000/001 is superseded for the orchestrator MVP. A task attempt and every bounded repair run execute in a new standalone Scheduled ChatGPT conversation. `run_id`, task context, snapshots, verification findings and Codex feedback are durable Mac state. No hidden previous-chat context is required.

This does not weaken the ChatGPT-first requirement: ChatGPT writes the substantive output through RDC; Codex can only review a frozen export. A local deterministic coordinator owns queue state, checks and publication.

## Implemented components

- `orch/core.py`: SQLite ledger, plan load, DAG/single-writer claim, capability files, context, submit/quiesce, verifier/snapshot, review binding, completion/publication, next/reconcile.
- `orch/codex_review.py`: subscription/billing preflight, hooks-off frozen export, dry-run and explicit model review execution.
- `orch/cli.py` + `bin/orch`: operational CLI.
- `dispatcher_prompt.txt`: native Scheduled ChatGPT bootstrap/new-chat chaining.
- `tests/test_orchestrator.py`: deterministic negative/positive core tests.

## Verification performed

1. Python compilation passed for the implementation modules.
2. Unit suite: 7/7 passed.
3. Local Git fixture task DEMO-1: verifier PASS; commit `2ef9c87a4e963e662d6e668e78b4cdcfa20fc41c`; local bare remote matched.
4. Dependent DEMO-2: verifier PASS; commit `5a7085125ae7948fb362d668427413cc690926fc`; local bare remote matched.
5. Protected sentinel SHA-256 remained `b0c49c30a353a34f3d674af0ffb7efbe8b56044e72061ba41b257be0dbe38fb0`.
6. End state: `orch claim` → `NO_WORK`; `orch reconcile` → `CLEAN`.
7. Codex preflight: ChatGPT auth, plan `prolite`, purchased credits false/balance 0, limit not reached; observed weekly used percent 96. No Codex model review was spent on implementation because the remaining included allowance was intentionally conserved.

## Live ChatGPT E2E

A separate two-task `variant-b-live-v1` fixture and one-time `ORCH Variant B Dispatcher` were created to validate actual Scheduled ChatGPT → RDC → coordinator → publication → next standalone ChatGPT chaining. Raw evidence remains under the orchestrator runtime/fixture directories and is not the market project.

## Remaining boundaries

- Scheduled task runtime does not expose a reliable ChatGPT chat URL/ID to the worker. Variant B does not depend on it; local `run_id` is the authoritative workflow identity.
- Native schedule timing may be delayed; correctness depends on claim/ledger state, not exact wall-clock delivery.
- Broad RDC shell is cooperative, not OS-enforced isolation.
- The Codex adapter's account preflight is live-tested; the model review path is implemented but a new real review is optional/usage-gated because the observed included weekly allowance is already heavily used.
- Main-project write integration is NOT enabled. <separately-authorized-integration> still requires separate approval/current-state reconciliation.
