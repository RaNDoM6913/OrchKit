# ORCH v0.4 — crash-safe rollback of a replaced ORCH home

**Date:** 2026-09-18.

## Explicit rollback

After `replace-backup` reaches `NEW_ACTIVE`, the old standalone home remains in its journal-bound rollback directory. ORCH can now switch back explicitly:

```sh
orch state replace-reconcile --destination /path/to/home --rollback
```

Rollback is never automatic. The journal records the old root filesystem device/inode before replacement, and a rollback directory is accepted only when that identity still matches. This proves the command is returning the exact pre-replacement home rather than an arbitrary sibling directory.

## Rollback phases

Rollback uses additional durable phases:

- `ROLLBACK_PREPARED` — rollback intent is durable while the new home is still active;
- `ROLLBACK_NEW_MOVED` — the new home has moved to a journal-bound `failed_home`, destination is temporarily absent, and the old rollback home has not yet returned;
- `ROLLED_BACK` — the old home is active again; the replaced new home remains intact as a forward-copy for inspection;
- `ROLLBACK_FINALIZE_PENDING_DELETE` — explicit finalize has moved the failed new home to a discard path before deletion.

`replace-reconcile` can adopt both rename-before-journal-update gaps. `--rollback` resumes a recoverable rollback state; heartbeat age is irrelevant.

After `ROLLED_BACK`, the new forward-copy is deliberately retained until:

```sh
orch state replace-reconcile --destination /path/to/home --finalize
```

Finalization is also journaled around the discard rename/delete window. Completion writes `replacement-receipt.json` with `outcome=ROLLED_BACK`, removes the failed new home, and removes the external journal.

## Deterministic and live evidence

Tests prove successful rollback, adoption of a journal gap after moving the new home, resume after failure while restoring the old home, and recovery from a rollback-finalize journal gap. A real CLI smoke replaced a Safe-profile home with a Standard-profile backup, verified the Standard state, rolled back to the original Safe profile, passed `state check`, finalized, and recorded `outcome=ROLLED_BACK`.

Full deterministic acceptance after this block: **103/103 PASS**, Python compile PASS, `git diff --check` PASS, current state `READY`, reconcile `CLEAN`. The live disposable CLI smoke proved `safe → standard replacement → explicit rollback → safe`, then finalized with `replacement-receipt.json` recording `outcome=ROLLED_BACK`.
