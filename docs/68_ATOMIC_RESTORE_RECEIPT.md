# ORCH post-v0.11 — atomic restore receipt

**Date:** 2026-09-19. **Scope:** backup restore staging/publish boundary.

## Problem

A verified backup was restored into a private staging home and published by one directory rename, but `restore-receipt.json` was still written with a direct truncate/write/fsync/chmod sequence. A process failure during that write could leave a partially written durable receipt inside an orphaned staging tree rather than using the same atomic authority-write primitive as the rest of ORCH.

## Change

Restore receipt publication now uses the shared atomic private JSON writer inside the staging home. The destination directory is renamed into place only after the receipt has been fully written, fsynced and atomically replaced as a `0600` authority artifact.

## Evidence

Existing fresh-restore, migration and atomic-directory-publish tests remain green. A new failure-injection test forces the restore receipt write to fail and proves that the destination is never published and the temporary restore staging tree is cleaned. The complete deterministic suite passes **228/228** tests.

<separately-authorized-integration> remains disabled and `<protected-project>` is untouched.
