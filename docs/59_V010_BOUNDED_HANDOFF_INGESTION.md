# ORCH v0.10 — bounded handoff ingestion release checkpoint

**Date:** 2026-09-18. **Version:** `0.10.0`. **Durable state schema:** v4.

## Release scope

v0.10 keeps the v0.9 claim-boundary guarantees and hardens both external durable handoff inputs:

- worker receipts are claim-bound to an exact private path, opened no-follow, byte/field/path-count bounded, normalized to receipt schema v1, and raw SHA-256/byte evidence is recorded;
- reviewer reports are accepted only from the frozen per-run review export path, opened no-follow, capped at 256 KiB, structurally bounded, snapshot-bound, and raw SHA-256/byte evidence is recorded;
- `verify` now returns canonical `review_report_file` when a run enters REVIEWING, so manual review import never needs to reconstruct path aliases.

`<protected-project>` remains outside write scope and <separately-authorized-integration> remains disabled.

## Source acceptance

The complete deterministic source suite passes **178/178** tests. Python compilation and `git diff --check` pass. Repo-local state remains schema v4 `READY`, with no active writers or pending publications; reconcile is `CLEAN`.

## Offline wheel

Artifact: `dist/agent_workflow_orchestrator-0.10.0-py3-none-any.whl`

- bytes: `81018`
- SHA-256: `614e44df20eb7ae4e5bad14f6f75c3f57cc19a9d58ca6b3c1e74e2aa08801bb6`

Built with `python3 -m pip wheel . --no-deps --no-build-isolation -w dist` and installed into a fresh disposable Python 3.9 venv with `pip install --no-index --no-deps`.

## Installed receipt acceptance

A Safe/off-review disposable project queued and claimed a task. The installed claim returned its exact `receipt_file`. Submission from an alternate file failed with `invalid_receipt_path`. Submission from the exact returned file succeeded and returned:

- receipt bytes: `151`;
- receipt SHA-256: `1711c788c06c68725ad60b74e018adec19f2b301271b75e4f0917ac44f60fcbf`.

The task then quiesced, verified and completed normally.

## Installed review acceptance

A second Safe project used required Codex review policy, but no Codex/model invocation was performed. After normal worker submit/quiesce, installed `verify` returned REVIEWING plus the exact canonical `review_report_file`.

Import from an alternate report file failed with `invalid_review_report_path`. A manually written PASS report at the exact returned path imported successfully and returned:

- report bytes: `178`;
- report SHA-256: `1e606fd4cd68b171c560df40d168ce8eb9decdfaf213cc85dd42771b1baca86a`.

The run then completed normally. Both disposable ORCH homes ended `state=READY` and `reconcile=CLEAN`.

## Commits in this release slice

- `88e51bc` — bound worker receipt ingestion;
- `8187742` — bound review report ingestion;
- release/API evidence changes are committed separately after final acceptance.

## Boundaries retained

No GitHub remote or push was created for the ORCH source repository. No paid OpenAI Platform API, purchased credits, external AI provider, Workspace Agents API, LaunchAgent/autostart, or global RDC/Codex configuration change was introduced. Broad RDC shell remains a cooperative boundary rather than an OS sandbox.
