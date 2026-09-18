# ORCH v0.4 — atomic restore into a fresh ORCH home

**Date:** 2026-09-18.

## Scope

This restore flow deliberately does not overwrite the current live ORCH home. A verified backup is restored into a **new destination that must not already exist**.

```sh
orch state restore-backup /path/to/orch-state-....zip \
  --destination /path/to/new-orch-home
```

The destination's parent may already exist, but the destination itself must be absent and must not be a symlink.

## Two-phase restore

Restore is prepared in a private sibling staging directory on the same filesystem as the requested destination. The archive has already passed `state verify-backup`, then only known ORCH-owned members are streamed into staging. No claim/capability authority is restored.

`dispatcher-prompt.txt` is intentionally treated as derived state and is not restored because it embeds an ORCH home/command path. The restore receipt tells the operator to regenerate it with `orch dispatcher render` from the restored home.

After extraction, every restored directory is forced to `0700` and restored files to `0600`. The staged home is then opened through the normal `Orchestrator`, so known older schemas are migrated using the same transactional migration path as a normal startup.

Before publication, staged state must not be `BLOCKED`, must contain no active writer run, and must not require a missing capability. A verified snapshot reservation or unfinished publication may still legitimately produce `ATTENTION`; those durable states are preserved and can be inspected with `recovery inspect` after restore.

Only after all preparation succeeds is the staging directory renamed to the requested destination. Because staging and destination share the same parent, the publish step is one filesystem rename. If the rename fails, staging is removed and the destination remains absent.

## Restore evidence

The restored home receives `restore-receipt.json` (`0600`) recording archive SHA-256, source manifest/schema, restored schema, destination, health summary, and the dispatcher regeneration requirement. Future backups include this receipt as provenance.

## Deterministic evidence

Tests prove that:

- a real secret-free backup restores into a fresh home and preserves backed-up config/log evidence;
- claim storage is recreated empty rather than restored from the archive;
- the external parent directory's permissions are not modified;
- an existing destination is untouched and rejected;
- a synthetic failure during the final atomic rename leaves no destination and no staging residue;
- a verified schema-v2 archive is migrated to schema v3 before the destination is published.

This is intentionally not an in-place restore/swap of an existing ORCH home. In-place promotion requires a separate rollback/journal contract and remains a later block.

Full deterministic acceptance after this block: **93/93 PASS**, Python compile PASS, `git diff --check` PASS. A real CLI smoke restored the current repo-local state into a disposable home, where `state check` returned `READY`, `recovery inspect` returned `CLEAN`, private modes were correct, and `dispatcher render` regenerated the derived prompt successfully.
