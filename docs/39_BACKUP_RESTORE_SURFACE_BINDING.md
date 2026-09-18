# ORCH v0.4 — backup verifier bound to restore surface

**Date:** 2026-09-18.

## Contract tightening

`state verify-backup` now validates not only ZIP path safety and SQLite integrity, but also the exact set of archive member classes that `restore-backup` understands.

A path-safe arbitrary `files/...` member no longer passes verification. Unknown files return `backup_member_not_restorable` and the archive remains `BLOCKED`.

The allowed restore surface remains intentionally narrow: ORCH config/RDC markers and provenance receipts, registered project configs, compiled plans, run-bound logs/receipts/review exports, plus the SQLite database and manifest. Capability/claim material remains forbidden.

This means `status=VERIFIED` now implies the archive is structurally acceptable to the restore implementation rather than merely being a well-formed ZIP with a valid database.

## Deterministic evidence

Tests prove that a current ORCH backup remains verified, path traversal and claims stay blocked, and a path-safe but unknown `files/manual-owner-note.json` member is rejected before restore.

Full deterministic acceptance after this block: **113/113 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.
