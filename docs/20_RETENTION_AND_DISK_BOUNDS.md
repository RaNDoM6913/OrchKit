# ORCH v0.4 — evidence retention and disk bounds

**Date:** 2026-09-18.

## Managed retention surface

ORCH now inventories only artifacts it can bind to durable state:

- `.runtime/logs/<run_id>-*.json` verifier/scope logs;
- `.runtime/worker_receipts/<run_id>.json`;
- `.runtime/review_exports/<run_id>/` frozen reviewer exports;
- `backups/orch-state-*.zip` secret-free ORCH backups.

Capability/claim files are never part of retention pruning. Unknown files, unknown run ids, non-regular artifacts, and symlink-containing review exports are reported as unmanaged and are not deleted.

## Commands

```sh
orch state retention
orch state prune-retention \
  --older-than-days 30 \
  --max-evidence-mb 256 \
  --keep-recent-runs 20 \
  --keep-backups 5 \
  --max-backup-mb 512
```

`state retention` is read-only. It reports managed bytes, protected run evidence, backup usage, and unmanaged artifacts.

`prune-retention` only deletes evidence for runs that are no longer writer/review active, are not `VERIFIED` awaiting completion/publication/approval, are not in owner/review/publication wait states, and have no unfinished publication journal. Age-based pruning uses durable run timestamps. Under byte pressure, the oldest safely terminal runs can be pruned while preserving the configured number of newest terminal attempts.

Backup pruning only touches regular non-symlink files matching the ORCH backup naming convention, keeps at least one managed backup, and never deletes arbitrary files from the backup directory.

The result reports `bounded=false` instead of broadening deletion scope when unmanaged artifacts or protected data make the requested budget impossible to satisfy safely.

## Deterministic safety coverage

Tests prove that terminal evidence is pruned while active-run evidence survives, unknown owner artifacts are preserved and reported, and backup retention removes only managed ORCH backup files. No retention test relies on model review or external services.
