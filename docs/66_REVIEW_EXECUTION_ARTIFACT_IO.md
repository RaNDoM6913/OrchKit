# ORCH post-v0.11 — review execution artifact I/O

**Date:** 2026-09-19. **Scope:** optional Codex review execution artifacts only; no model invocation is required by this patch.

## Problem

The verified review export was snapshot-bound, but `run_review` still reopened generated prompt/schema with ordinary unbounded file APIs and captured reviewer stdout in memory before rewriting it as an events file. Pre-existing output paths also needed explicit fail-closed treatment.

## Change

Generated review prompt and schema now receive exact SHA-256/byte bindings at preparation time and are re-read through bounded no-follow regular-file I/O before review execution. Tampering between preparation and execution fails closed.

Reviewer event stdout is streamed directly to a new exclusive `0600` file rather than captured unbounded in memory. The resulting event artifact is no-follow checked and capped before it is accepted as evidence. Pre-existing report or events targets are rejected, timeout is returned as a bounded BLOCKED result, and report ingestion remains delegated to the existing bounded no-follow snapshot-bound importer.

## Evidence

Deterministic tests cover prompt tampering, schema tampering, an events symlink trap, a report symlink trap, oversized event output, a mocked successful review/import, and timeout handling. The complete deterministic source suite passes **226/226** tests.

No real Codex/model call is made. <separately-authorized-integration> remains disabled and `<protected-project>` is untouched.
