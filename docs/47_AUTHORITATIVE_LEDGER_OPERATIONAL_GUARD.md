# ORCH post-v0.6 — authoritative ledger operational guard

**Date:** 2026-09-18. **Scope:** CLI bootstrap/operational state authority and task-admission gating.

## Defect

Before this block the CLI eagerly constructed `Orchestrator(root)` for most operational commands. If an existing ORCH home lost or moved `.runtime/orch.sqlite3`, commands such as `claim`, `next`, `queue enqueue`, or `state check` could create a fresh empty database. In Variant B that could turn durable-work loss into an apparently valid `NO_WORK`, masking a serious recovery event.

## Ledger creation authority

Ledger creation is now explicit and fresh-home only:

- `orch init` may create a ledger only when no prior registry/runtime authority is present;
- `orch project add` may create/initialize the authoritative ledger only for first registration in a fresh home.

If registry entries or residual runtime authority already exist while the ledger is missing, both bootstrap paths fail closed and require restore/recovery.

All operational state commands require an already-existing regular, non-symlink SQLite ledger. Missing/unsafe state returns `state_ledger_missing_or_unsafe` before `Orchestrator` construction. This includes queue, worker/run, publication, normal recovery, state-health/maintenance, status/reconcile, and Codex-review operations.

Archive verification/restore/replacement-only recovery remains intentionally state-independent and continues to avoid creating an unrelated command root.

## Task admission readiness gate

`queue enqueue` and CLI `project make-plan` now run the read-only project audit before writing a plan artifact. `ATTENTION` does not prevent queueing (for example an active writer or missing optional dispatcher), but `BLOCKED` readiness prevents task admission.

This means newly foreign dirty/untracked bytes, writer/root/remote drift, unsafe state authority, or other readiness blockers stop the operation before a new durable plan is written.

Direct library plan compilation remains available for deterministic/internal use; the operator-facing CLI enforces the stronger admission contract.

## Deterministic evidence

A dedicated 9-test suite proves:

- `claim` and `state check` do not create a missing ORCH home/ledger;
- registry-only `queue enqueue` does not create runtime state or a plan;
- explicit `init` still bootstraps genuinely fresh state and operational commands then work;
- `init` and `project add` refuse pre-existing registry authority without a ledger;
- a symlinked ledger is refused without touching its target;
- queue enqueue and CLI make-plan block newly foreign workspace bytes before plan creation.

The previous queue-enqueue fixture was updated to initialize its ledger explicitly, documenting the new contract. The complete deterministic suite passes **147/147** tests. Python compilation and `git diff --check` pass.
