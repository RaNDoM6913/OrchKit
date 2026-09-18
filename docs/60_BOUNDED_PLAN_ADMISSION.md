# ORCH post-v0.10 — bounded direct plan admission

**Date:** 2026-09-18. **Scope:** direct `load-plan` ingestion and task-definition validation.

## Problem

Queue adapters already produce bounded ORCH-owned plans, but the lower-level `load-plan` command could still read an arbitrary-size JSON document through a symlink and accepted loosely bounded task metadata. Invalid task ids or oversized/unknown payload fields could therefore enter the ledger and fail only later at claim/context time, or unnecessarily enlarge durable state.

## Plan file boundary

Direct plan files are now opened no-follow when supported and must be regular files. Input is capped at 1 MiB. The SHA-256 stored for immutable plan-revision conflict detection is computed over the exact same raw bytes that were parsed; load results also report `plan_bytes`.

Plan schema remains version 1. Top-level fields are limited to `schema_version`, `plan_revision`, `tasks`, and optional `adapter`. Plan revisions are bounded safe identifiers and a plan may contain at most 512 tasks.

## Task bounds

Task ids are admitted only as bounded `[A-Za-z0-9._-]` identifiers. Unknown task fields fail closed. Admission now bounds goal/non-goals, workspace identity, dependencies, allowed/protected paths, check count/argv/cwd/authority metadata, review fields/thresholds, owner/review booleans, publication metadata and max attempts.

Check definitions accept only their documented input authority fields; derived `executable_path`, hashes and authority records are still created internally by `prepare_task_payload` rather than trusted from a plan. Publication kinds remain `none`, `git_local`, or `git`, with bounded metadata and structurally valid expected Git object ids.

Workspace directories must exist, be absolute and not be symlinks at admission. This matches registered-project/queue behavior and prevents a direct plan from deferring workspace identity until execution.

## Compatibility and layered bounds

The 32 KiB context-pack guard remains independent. Its deterministic test now creates a valid plan with many bounded allowed paths so admission succeeds and the later claim handoff still proves context overflow is blocked without a run/capability.

## Deterministic evidence

Dedicated tests cover exact raw digest/byte evidence, symlink refusal, >1 MiB refusal before JSON parsing, unsafe revision/task ids, unknown plan/task/check/publication fields, goal/argv bounds, and the 512-task limit. The complete suite passes 185/185 tests.
