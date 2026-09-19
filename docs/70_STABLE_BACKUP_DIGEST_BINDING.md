# ORCH post-v0.11 — stable backup digest binding

**Date:** 2026-09-19. **Scope:** state-backup database and published archive digests.

## Problem

`orch/state.py` still carried a private legacy `_sha256()` helper after the shared file-hash primitive had been hardened. Backup database-copy hashing and the final archive digest therefore bypassed the no-follow/stable-descriptor guarantees used elsewhere in ORCH. In particular, a same-user path substitution after archive publication could make the final digest read a different file.

## Change

Backup database-copy and final archive hashing now use the shared `sha256_file()` primitive. The duplicate state-specific helper is removed. Backup digesting therefore inherits no-follow regular-file opening plus before/after file identity and metadata checks.

## Evidence

A regression test swaps the published backup output to a symlink immediately after `os.replace()` and proves the final digest fails closed with `hash_file_unsafe` without reading or changing the victim. Existing custom-output, backup-integrity and secret-exclusion tests remain green. The complete deterministic suite passes **230/230** tests.

<separately-authorized-integration> remains disabled and `<protected-project>` is untouched.
