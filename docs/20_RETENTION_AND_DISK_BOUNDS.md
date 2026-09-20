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

Deletion is bound to the inventoried object rather than trusting a later pathname lookup. Managed regular files carry stable descriptor-derived identity plus SHA-256; review-export trees bind every regular file and directory. Destructive work reopens trusted ORCH directories with no-follow directory descriptors, quarantines the exact candidate inside its managed directory, revalidates the bound bytes, and fails closed on drift. Missing evidence is reconciled before further budget-driven deletion, and backup pruning rebinds the full live backup set before each decision and refuses to delete the last bound survivor.

Backup pruning only touches regular non-symlink files matching the ORCH backup naming convention, keeps at least one managed backup, and never deletes arbitrary files from the backup directory.

The result reports `bounded=false` instead of broadening deletion scope when unmanaged artifacts or protected data make the requested budget impossible to satisfy safely.

## Deterministic safety coverage

Tests prove that terminal evidence is pruned while active-run evidence survives, unknown owner artifacts are preserved and reported, and backup retention removes only managed ORCH backup files. Adversarial regressions cover inventory-to-delete replacement, symlinked managed anchors, FIFO substitution, same-inode content mutation, nested review trees, partial deletion, late missing evidence, backup disappearance, and last-survivor protection. Deterministic retention tests do not rely on model review or external services.
