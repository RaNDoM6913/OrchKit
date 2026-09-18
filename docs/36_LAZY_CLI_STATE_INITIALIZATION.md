# ORCH v0.4 — lazy CLI state initialization

**Date:** 2026-09-18.

## Defect addressed

The CLI previously constructed `Orchestrator(root)` before dispatching every command. That means even commands whose input is an archive or an explicit replacement destination could create `.runtime/orch.sqlite3` under the command's `--root` as a side effect.

This is especially unsafe during home-replacement recovery: while `OLD_MOVED` is durable, the intended destination may temporarily be absent. A read-only/reconciliation invocation must not create an empty state home simply because its command root does not exist.

## Lazy state boundary

CLI state is now initialized only for commands that actually operate on the selected ORCH ledger.

Archive/replacement commands are state-independent with respect to the command root:

- `state verify-backup`;
- `state restore-backup`;
- `state replace-backup`;
- `state replace-reconcile`.

Setup/doctor/RDC/dispatcher/Git-policy/Codex-preflight and non-removal project-registry commands also avoid opening SQLite unless their own implementation requires it. Queue/run/publication/recovery commands still initialize the selected ORCH ledger normally.

`project remove` remains ledger-dependent because its safety contract must prove that no unresolved durable project work exists.

## Deterministic evidence

Tests prove that `state verify-backup` with a nonexistent `--root` leaves that path absent, and that `state restore-backup` with a nonexistent command root creates only the explicitly requested destination. The restored destination still contains a valid private ORCH runtime and SQLite state.

Full deterministic acceptance after this block: **105/105 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.
