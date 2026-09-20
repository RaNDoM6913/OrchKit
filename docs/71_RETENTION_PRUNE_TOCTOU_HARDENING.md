# ORCH post-v0.11 — retention prune TOCTOU hardening

**Date:** 2026-09-20. **Scope:** local evidence/backup retention only; <separately-authorized-integration> remains disabled.

## Problem

Retention previously inventoried managed paths and later deleted by pathname. A file/tree/backup could therefore disappear, be replaced, become a symlink or non-regular object, or change contents between census and deletion. Stale accounting could then delete more evidence/backups than necessary, and backup races could reduce the live set too far.

## Change

Managed regular files now bind descriptor-derived identity plus SHA-256; review-export trees bind every regular file and directory. Reads reject symlinks/non-regular objects and use nonblocking regular-file opens so FIFO substitution cannot hang pruning.

Destructive pruning reopens trusted ORCH roots through no-follow directory descriptors, revalidates the exact inventoried object, renames it to an unpredictable same-directory quarantine name, and validates again before deletion. Review trees are removed through descriptor-relative traversal with per-entry binding and explicit partial-delete accounting.

Evidence byte budgets are recomputed after missing artifacts, including bytes already deleted in the same run. Backup policy rebinds the complete live inventoried set before each deletion, reconciles disappeared backups, and requires an exact bound survivor again after the candidate is quarantined.

Changed or failed objects are preserved/fail closed and surfaced as `ATTENTION` with `skipped_changed`, `failed`, `already_missing`, and partial deletion evidence rather than broadening deletion scope.

## Review and residual boundary

Four subscription Codex read-only review passes were run after billing preflight PASS; purchased credits were disabled/zero and Codex never edited the repository. Reproduced findings were converted into deterministic regressions. The final Codex review returned PASS for the current cooperative same-UID threat boundary.

A malicious same-UID process can still target the tiny interval between the final quarantine-path validation and `unlink`: macOS/Python provides no compare-and-unlink or unlink-by-open-file-descriptor primitive. This remains within the repository's already documented broad-RDC cooperative residual and is not represented as OS-enforced isolation.

**Evidence:** `python3 -m compileall -q orch tests` passed, the fresh deterministic suite passed **246/246**, and `git diff --check` passed. `<protected-project>` was not modified.
