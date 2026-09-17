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
2. Final deterministic unit suite: **11/11 PASS**, including pause/resume, abort/retry, snapshot-bound owner approval, review-support export and verifier-evidence export.
3. Local Git fixture task DEMO-1: verifier PASS; commit `2ef9c87a4e963e662d6e668e78b4cdcfa20fc41c`; local bare remote matched.
4. Dependent DEMO-2: verifier PASS; commit `5a7085125ae7948fb362d668427413cc690926fc`; local bare remote matched.
5. Protected sentinel SHA-256 remained `b0c49c30a353a34f3d674af0ffb7efbe8b56044e72061ba41b257be0dbe38fb0`.
6. End state: `orch claim` → `NO_WORK`; `orch reconcile` → `CLEAN`.
7. Codex preflight remained ChatGPT-authenticated with purchased credits false/balance 0 and the included limit not reached. A real subscription Codex review was then executed on a frozen fixture. The first review correctly returned `BLOCKED` because our export omitted the approved check support; ChatGPT fixed the adapter. A fresh second snapshot received a real Codex `PASS` with exit 0. Last observed included weekly usage after the review cycle: 97%.

## Live ChatGPT E2E — PASS

A separate two-task `variant-b-live-v1` fixture validated the actual path, not a fake adapter. The reusable native standalone task executed two separate Scheduled ChatGPT workers with `worker_id=scheduled-variant-b`; each run claimed exactly one coordinator task through RDC.

- `LIVE-1` → run `LIVE-1-A1-130329c72f` → verifier PASS → snapshot `sha256:a1af8210469c74f6f12cfd6ef3acd34ab8151caa9035311e9647ec6d0ed5a0c2` → commit `f92c918...`.
- `LIVE-2`, dependent on `LIVE-1` → run `LIVE-2-A1-31f5608103` → verifier PASS → snapshot `sha256:44e58cb92149ae3e53c0f2f6c24b2a3986574ffca8b180eb9b2aa7b36e6b3847` → commit `5830e6b...`.
- Final fixture local HEAD and local-bare remote HEAD both equal `5830e6b7d7bbfe50795d4eb3a5d23abec1c9555a`.
- Protected `owner-note.txt` remained SHA-256 `e8d0aed7fada89824a8f8410aab60d21e7def979c7a256c1edecb0f6de3f789e`.
- Fixture Git status is clean; `orch next` returned `NO_WORK`; `orch reconcile` returned `CLEAN`; the dispatcher was explicitly disabled.

The launcher implementation uses one reusable standalone Scheduled Task object. When the ledger reports `READY`, that task is re-armed for a later run. The next model run intentionally receives no previous-chat dependency and claims fresh bounded state from SQLite/RDC. When the ledger reports `NO_WORK`, it stays disabled.

## Remaining boundaries

- Scheduled task runtime does not expose a reliable ChatGPT chat URL/ID to the worker. Variant B does not depend on it; local `run_id` is the authoritative workflow identity.
- Native schedule timing may be delayed; correctness depends on claim/ledger state, not exact wall-clock delivery.
- Broad RDC shell is cooperative, not OS-enforced isolation.
- The Codex model-review path is now live-tested. One real review exposed a genuine export defect; after ChatGPT repaired it, a second real review returned PASS. A minor uncertainty from that second review (verification evidence path visibility) was then fixed deterministically and covered by the final unit test without spending a third model review.
- Main-project write integration is NOT enabled. <separately-authorized-integration> still requires separate approval/current-state reconciliation.
- The reusable scheduler itself is a platform-owned wake-up mechanism; the local coordinator deliberately does not store or reuse OpenAI OAuth credentials.
- Detailed machine-readable evidence is recorded in `docs/14_VARIANT_B_LIVE_EVIDENCE.json`.
