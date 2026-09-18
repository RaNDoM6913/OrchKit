# ORCH state upgrade policy

**Date:** 2026-09-18. **Current durable schema:** v3.

## Safety contract

Persistent SQLite upgrades are forward-only and fail closed on unknown future `PRAGMA user_version` values. ORCH does not downgrade state and does not use destructive reset/clean behavior to recover schema mismatches.

Known upgrades run inside one explicit `BEGIN IMMEDIATE` transaction. Schema DDL, data backfill, queue indexes, migration-history insertion, and the final `user_version` change commit together. If any migration step fails, ORCH rolls the transaction back and leaves the previous schema version authoritative.

The durable `schema_migrations` table records the source version, target version, UTC application time, and bounded JSON details. `orch state migrations` exposes this history; `orch state check` includes the same history alongside SQLite integrity and recovery health.

## v2 → v3

The v3 migration adds `project_id`, `writer_key`, and monotonic `queue_seq` to existing tasks, backfills them from durable payload/workspace data, creates queue/run indexes, and only then advances `user_version` to 3.

A deterministic failure-injection test proves that an invalid legacy writer identity causes the migration to abort with `user_version=2` and none of the task-table DDL persisted.

## Operational policy

- Run `orch state check` before and after upgrades.
- A user-created `orch state backup` remains the portable secret-free backup mechanism when the current binary can open the state safely.
- `orch state verify-backup PATH` validates archive path safety, secret exclusions, manifest/database hash binding, SQLite integrity, and schema compatibility without restoring into the live home.
- `orch state restore-backup PATH --destination NEW_HOME` restores only into a previously absent home through a private sibling staging directory; known older state is migrated and health-checked before one atomic rename publishes the destination.
- Automatic migration does not create a raw SQLite copy because the live database contains run lease material; silently retaining that copy would weaken the existing secret-handling boundary.
- Future schema versions should add a deterministic migration test and an induced-failure rollback test before release.
