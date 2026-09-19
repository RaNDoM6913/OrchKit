# ORCH post-v0.11 — stable no-follow file hashing

**Date:** 2026-09-19. **Scope:** shared SHA-256 binding used by protected paths, snapshots, verifier execution authority and readiness.

## Problem

`sha256_file()` was a shared security primitive but used ordinary `Path.open()`. Callers generally checked symlinks before hashing, yet that left a check/open race and did not detect an in-place file identity/metadata change while bytes were being hashed.

## Change

The shared hash primitive now opens the target with `O_NOFOLLOW` where available, requires a regular file through `fstat`, hashes through the opened descriptor, and compares device/inode/size/mtime/ctime before and after the read. It also verifies that the number of bytes read matches the opening size. Unsafe targets fail closed as `hash_file_unsafe`; concurrent mutation fails as `hash_file_changed_during_read`.

This automatically strengthens every existing caller, including protected baselines, snapshot verification, verifier executable/support authority, project registration and readiness checks.

## Evidence

A deterministic regression test proves the normal digest, rejects a symlink target, and injects a post-read identity change to prove mutation detection. Existing executable-drift, support-drift, protected-file and readiness tests remain green. The complete deterministic suite passes **229/229** tests.

<separately-authorized-integration> remains disabled and `<protected-project>` is untouched.
