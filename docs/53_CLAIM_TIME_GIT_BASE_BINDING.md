# ORCH post-v0.8 — claim-time Git base binding

**Date:** 2026-09-18. **Durable state schema:** v4.

## Problem

Dynamic Git tasks intentionally omit an admission-time `expected_base` when they are queued behind unresolved project work. Previously those tasks did not bind a Git base until verification. That correctly avoided stale bases after earlier ORCH publications, but left a race: a foreign process could commit after the worker claimed the task and before verification, and the verifier could treat that newer HEAD as the task base.

## Run-specific base authority

Schema v4 adds nullable `runs.claim_git_head`.

For every new `git` or `git_local` run, ORCH now probes the workspace before issuing a capability file:

- current branch must match the publication branch;
- Git HEAD must be readable and structurally valid;
- an explicit plan `expected_base`, when present, must still equal current HEAD;
- the observed HEAD is persisted on the run as `claim_git_head`.

A stale explicit base blocks the task at claim with no run capability created. Dynamic tasks persist the then-current HEAD, so they still follow completed predecessor publications without freezing an old admission-time base.

Verification now requires the workspace Git HEAD to equal the run's persisted `claim_git_head`. A foreign commit after claim therefore produces the existing fail-closed `workspace_base_changed` verification result. The snapshot/publication CAS protections remain unchanged and operate on the same proven base.

Legacy runs migrated from schema v3 have a null claim head; verification preserves compatibility by falling back to an explicit publication `expected_base` when one exists.

## Migration

The v3→v4 migration is transactional and only adds the nullable run column. Existing task/queue data is unchanged. Direct upgrades from older schemas still apply the prior queue migration plus the new run column before setting `PRAGMA user_version=4`.

## Dispatcher behavior

A claim-time safety failure returns `BLOCKED`. The packaged Variant-B dispatcher now treats `BLOCKED` like other no-write terminal dispatch states: it must not edit a project and must not re-arm automatically.

## Deterministic evidence

Tests prove that a dynamic Git task persists the exact HEAD at claim, a foreign commit after claim blocks verification and preserves the original claim head in evidence, a stale explicit base blocks before capability creation, and schema v3 state migrates to v4 with the new column. The pre-existing transactional migration rollback test remains green.
