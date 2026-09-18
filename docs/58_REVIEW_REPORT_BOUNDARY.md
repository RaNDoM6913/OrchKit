# ORCH post-v0.9 — bounded review report ingestion

**Date:** 2026-09-18. **Scope:** reviewer-to-ledger handoff hardening.

## Problem

`review-import` previously accepted a report from any filesystem path and parsed it without a byte bound or no-follow open. Snapshot identity and verdict were checked, but manual/imported reviewer output could still redirect ORCH to an unrelated file or place unbounded/loosely structured findings into durable feedback state.

## Frozen report authority

A review report is now accepted only from the exact frozen export path `ORCH_HOME/.runtime/review_exports/<run_id>/review.json`. `prepare_review` exposes that path explicitly as `report`.

The file is opened with no-follow semantics when supported, must be regular, and is limited to 256 KiB. Alternate paths, symlinks, missing files and oversized reports fail before any review state transition.

## Structural bounds

Only `run_id`, `snapshot_id`, `verdict`, `findings`, and `uncertainty` are allowed. Snapshot ids must be canonical `sha256:<64 hex>`. Verdict remains one of PASS / NEEDS_FIX / BLOCKED.

Findings are limited to 100 and each must contain exactly `severity`, `path`, `evidence`, and `impact`, with bounded UTF-8 field sizes. Uncertainty is limited to 100 bounded strings. The existing exact run+snapshot stale-review guard remains authoritative after structural validation.

Raw report SHA-256 and byte length are recorded in the `REVIEW_IMPORTED` event and returned from import. Review feedback stored in the ledger uses the validated normalized structure.

## Deterministic evidence

Tests prove exact frozen-path snapshot-bound import with digest evidence and fail-closed handling for alternate paths, symlinked reports, reports above 256 KiB, unknown top-level fields, and findings above the configured count bound. The complete source suite passes 178/178 tests with the packaged Codex dry-run/export path unchanged.
