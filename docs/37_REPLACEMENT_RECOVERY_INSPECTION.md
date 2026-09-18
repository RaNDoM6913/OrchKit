# ORCH v0.4 — replacement-aware recovery inspection

**Date:** 2026-09-18.

## One read-only recovery surface

Run/publication recovery and home-replacement recovery now share the operator inspection flow:

```sh
orch recovery inspect --replacement-home /path/to/orch-home
```

The replacement inspector is strictly read-only. It reads the external replacement journal and sibling filesystem state but does not adopt journal gaps, rename directories, create a missing destination, expire a worker, or initialize an unrelated command root.

It can also be combined with normal run/project filters when an ORCH ledger is available; the top-level recovery status becomes `ATTENTION` or `BLOCKED` if either durable-run recovery or replacement recovery requires action.

## Classifications

The inspector distinguishes prepared/old-moved/new-active replacement phases, finalize gaps, rollback intent/new-moved/old-restored gaps, rolled-back forward-copy retention, and rollback-finalize gaps. Each recognized state returns only compatible explicit commands such as `--resume`, `--rollback`, or `--finalize`.

Ambiguous filesystem+journal combinations return `BLOCKED` with no suggested mutating command.

`automatic_action=false` is explicit in actionable replacement states.

## Missing destination safety

Replacement-only recovery is a state-independent CLI path. This matters during `OLD_MOVED` or `ROLLBACK_NEW_MOVED`, when the destination is intentionally absent: the inspector must not instantiate a new empty SQLite home at that path.

## Deterministic evidence

Tests prove read-only inspection of `NEW_ACTIVE`, inspection of `OLD_MOVED` while destination remains absent, inspection after successful rollback with a retained forward-copy, unchanged journal bytes, and replacement-only CLI operation without creation of a phantom command root.

Full deterministic acceptance after this block: **109/109 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.
