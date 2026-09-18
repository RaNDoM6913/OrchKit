# ORCH v0.4 — crash-safe replacement of a standalone ORCH home

**Date:** 2026-09-18.

## Scope and hard boundary

This workflow replaces an existing **standalone ORCH state home** from a verified backup. It is explicitly not allowed to replace a source repository or any home containing unknown top-level owner files.

```sh
orch state replace-backup /path/to/state.zip --destination /path/to/existing-orch-home
orch state replace-reconcile --destination /path/to/existing-orch-home
```

Before any side effect, the existing home must have only known ORCH-owned top-level paths, a valid SQLite state file, `state check=READY`, `reconcile=CLEAN`, and `recovery inspect=CLEAN`. Active writers, verified reservations, pending publications, damaged capability state, symlinks, or foreign top-level files fail closed.

## Journaled two-home swap

The candidate backup is first restored into a private sibling prepared home using the fresh-home restore contract. A private replacement journal is written in the destination parent before the existing home moves.

Replacement phases are:

1. `PREPARED` — old destination is still live; fully validated new home exists beside it.
2. `OLD_MOVED` — old home has been atomically renamed to a journal-bound rollback directory; new prepared home has not yet become active.
3. `NEW_ACTIVE` — prepared home has been atomically renamed into the requested destination; old home remains intact as rollback.
4. `FINALIZE_PENDING_DELETE` — explicit finalization has atomically moved the rollback directory to a journal-bound discard path before deletion.

`replace-reconcile` observes filesystem reality and can adopt a rename that happened before its journal update. `--resume` is required to advance a recoverable `PREPARED` or `OLD_MOVED` operation. Heartbeat/lease expiry is not involved.

After `NEW_ACTIVE`, the command returns `REPLACED_ROLLBACK_AVAILABLE`; rollback material is deliberately retained until a separate explicit finalization.

```sh
orch state replace-reconcile --destination /path/to/home --finalize
```

Finalization first renames rollback to a discard directory, journals that phase, then deletes it. A crash before or after discard deletion is classifiable and resumable. Successful finalization writes `replacement-receipt.json` into the active home, deletes the external journal, and future backups preserve that receipt.

## Deterministic evidence

Tests prove standalone-home enforcement, active-state refusal, retained rollback on success, resume after failure while activating the new home, adoption of a filesystem rename that occurred before the journal update, and recovery from a finalize journal gap. A real CLI smoke replaced a disposable Safe-profile home with a Standard-profile backup, verified the new state, observed the retained rollback, and finalized successfully.

This block retains rollback but does not yet automate switching back from `NEW_ACTIVE`; explicit rollback is implemented as a separate recovery capability.

Full deterministic acceptance after this block: **99/99 PASS**, Python compile PASS, `git diff --check` PASS, current state `READY`, reconcile `CLEAN`. The disposable CLI replacement smoke also passed through prepare → `NEW_ACTIVE` → observed rollback → explicit finalize.
