# ORCH v0.4 — backup archive verification

**Date:** 2026-09-18.

## Scope

`orch state backup` already creates a consistent secret-free SQLite archive, but there was no first-class way to prove that a candidate archive was safe and internally consistent before any future restore workflow.

This block adds verification only. It does **not** overwrite or restore the current ORCH home.

```sh
orch state verify-backup /absolute/path/to/orch-state-....zip
```

## Archive boundary

The verifier requires exactly the ORCH archive namespace: `state/orch.sqlite3`, `state/manifest.json`, and optional `files/...` members. Duplicate names, absolute/path-traversal/backslash paths, symlink members, unknown top-level paths, and claim/capability-secret paths fail closed.

The total advertised uncompressed size and streamed SQLite payload are bounded before validation, limiting accidental or hostile expansion.

## Manifest and SQLite binding

The manifest must retain the secret-exclusion contract for claims, capability files, and provider credentials. Its recorded database SHA-256 must exactly match the streamed SQLite member.

The database is materialized only into a private temporary verification directory and opened read-only/immutable. Verification then checks:

- SQLite `quick_check`;
- foreign-key integrity;
- `PRAGMA user_version`;
- exact manifest-schema ↔ database-schema binding;
- compatibility with the current ORCH schema.

Current schema returns `compatibility=CURRENT`; an older structurally valid backup can be identified as `UPGRADE_REQUIRED`; an unknown future schema fails closed as `FUTURE_UNSUPPORTED`.

The ZIP CRC is also checked after structural and database validation. No archive member is extracted into the live ORCH home.

## Deterministic evidence

Tests prove successful validation of an actual `backup_state` archive and rejection of:

- manifest/database hash mismatch;
- ZIP path traversal;
- forbidden `.runtime/claims` material;
- an invalid/non-ZIP archive.

A future restore command should consume only an archive that passes this verifier and must still perform its own destination/active-writer/atomic-swap checks.

Full deterministic acceptance after this block: **89/89 PASS**, Python compile PASS, `git diff --check` PASS. A real repo-local backup smoke verified as `VERIFIED` / `CURRENT` with matching SHA-256, SQLite `quick_check=ok`, zero foreign-key violations; state remained `READY` and reconcile `CLEAN`.
