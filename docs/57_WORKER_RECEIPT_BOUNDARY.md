# ORCH post-v0.9 — bounded worker receipt ingestion

**Date:** 2026-09-18. **Scope:** worker-to-ledger handoff hardening.

## Problem

`submit` previously accepted a JSON file from any filesystem path and read it without a byte bound or symlink-safe open. The receipt identity and changed-path allowlist were validated after parsing, but a worker could still point ORCH at an unrelated file, submit an oversized JSON document, or expand durable SQLite state with unbounded metadata.

## Claim-bound receipt authority

Every successful claim now returns an exact `receipt_file` path under the private `ORCH_HOME/.runtime/worker_receipts` directory. The same path is included in the bounded context pack.

`submit` accepts only that exact run-bound path. The file is opened with no-follow semantics when supported and must be a regular file. Symlinked, missing, or alternate receipt paths fail before any run-state transition.

## Bounds and schema

Receipt ingestion is bounded to 64 KiB. New dispatcher receipts explicitly use schema version 1. For compatibility, a legacy receipt with no `schema_version` is normalized to v1; an explicitly different version fails closed.

Allowed fields are `schema_version`, `run_id`, `task_id`, `changed_paths`, and optional `summary`. Unknown fields fail closed. Summary is limited to 4 KiB UTF-8. `changed_paths` is limited to 256 entries, each path is normalized, duplicate normalized paths are rejected, and the existing task allowlist remains authoritative.

The exact raw receipt SHA-256 and byte length are recorded in the `RESULT_SUBMITTED` event and returned from `submit`; the canonical normalized receipt remains stored in the run row.

## Dispatcher contract

The packaged Variant-B dispatcher no longer constructs a receipt location independently. It writes to the exact `receipt_file` returned by claim and must not substitute a symlink or another path.

## Deterministic evidence

Tests prove successful exact-path submission with digest/byte evidence and path canonicalization, plus fail-closed handling for alternate paths, symlinked receipts, receipts above 64 KiB, future schema versions, unknown fields, and more than 256 changed paths. Existing path-escape behavior remains externally compatible as `path_not_allowed:<path>`.
