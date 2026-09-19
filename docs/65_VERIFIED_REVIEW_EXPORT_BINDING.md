# ORCH post-v0.11 — verified review export binding

**Date:** 2026-09-19. **Scope:** verifier → optional Codex review handoff only; <separately-authorized-integration> remains disabled.

## Problem

A run entered `REVIEWING` with a snapshot-bound review result, but export preparation still recopied workspace/support bytes and verifier logs from mutable paths without proving they still matched the verified state. A check could also mutate snapshot bytes after the initial snapshot was taken. Check IDs were not constrained to unique filename-safe values, allowing verifier evidence log collisions.

## Change

Verification now writes scope/check-authority/check logs atomically, records their exact SHA-256 and byte lengths in the snapshot manifest, re-checks bound check authority after checks execute, revalidates snapshot bytes after checks, and derives the final snapshot ID only after verifier evidence is bound.

Review export now refuses workspace, support-file, or verifier-evidence drift; copies only bytes that match the stored snapshot/authority bindings; validates evidence filenames and metadata; writes generated review artifacts atomically/private; and removes a partial export on failure. Admitted check IDs are filename-safe and unique so evidence files cannot alias each other.

## Evidence

Dedicated tests cover review workspace drift, support drift, verifier-evidence tampering, check mutation of snapshotted output, deleted-file handling, evidence inclusion, and unsafe/duplicate check IDs. Python compilation and `git diff --check` pass; the complete deterministic suite passes **219/219** tests.

No Codex/model invocation is required for these deterministic checks. `<protected-project>` is untouched.
