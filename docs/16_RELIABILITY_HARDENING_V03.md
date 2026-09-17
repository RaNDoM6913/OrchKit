# ORCH v0.3 — reliability hardening

**Date:** 2026-09-18. **Scope:** independent `agent-workflow-orchestrator` only. The trading project remains outside write scope.

## What changed

v0.3 keeps Variant B: every task/repair attempt runs in a fresh ChatGPT conversation and receives durable context through ORCH/RDC.

Hardening added in this slice:

- Plan load rejects dependency cycles instead of silently producing `NO_WORK`.
- Task definitions validate allowed/protected paths, check argv/cwd/timeouts, hashes, and attempt limits before execution.
- Reusing one `task_id` from a different immutable plan revision is rejected explicitly.
- Run capability files are revoked at quiesce/abort instead of lingering after write authority ends.
- Protected-path/snapshot safety violations transition the run/task to `BLOCKED`; they no longer leave a writer stuck in `VERIFYING`.
- Snapshots and exact Git publication now support verified tracked-file deletions.
- Git workspaces receive an independent diff census before snapshot/checks; unreported or out-of-allowlist tracked/untracked changes block the run, while unchanged protected pre-existing dirty files are subtracted explicitly.
- Scope evidence is persisted and included in frozen Codex review exports.

## Durable state and recovery

The SQLite state schema is now `user_version=2` and includes a publication journal.

New commands:

```sh
orch state check
orch state backup
orch state prune-capabilities
orch publish-reconcile --run-id RUN
orch publish-reconcile --run-id RUN --resume
```

`state check` runs SQLite quick/foreign-key checks, reports active runs, missing/orphan capability files, and unfinished publication operations. Backups use SQLite's consistent backup API and include policy/project configuration plus local evidence/logs, while deliberately excluding claim/capability secrets and provider credentials.

Publication now records `INTENT → STAGED → COMMITTED → PUSHED → REMOTE_VERIFIED → COMPLETE`. A repeated `publish` is refused when an unfinished journal exists. Reconciliation first observes local HEAD, exact staged bytes, the verified snapshot and remote ref; it can safely retry only when no side effect occurred, adopt a proven commit after a journal gap, or resume an exact staged/remote-pending operation. Unexpected staging or remote advancement blocks automation.

## Verification

- Deterministic suite: **42/42 PASS**.
- `python3 -m py_compile orch/*.py`: PASS.
- `git diff --check`: PASS.
- Repo-local `orch state check`: `READY`; SQLite `quick_check=ok`, zero FK violations, zero active runs, zero pending publications, zero orphan capabilities.
- Repo-local `orch reconcile`: `CLEAN`.
- Secret-free local state backup created successfully; stale capability files from historical fixtures were detected and pruned only after confirming no active runs.
- Offline wheel build: `agent_workflow_orchestrator-0.3.0-py3-none-any.whl`, SHA-256 `b0574dd710e6d92866f1720e0a310d1e46fddf325bb204b2ae5aebc6dfd05fbf`.
- Wheel installed into a clean temporary Python venv without runtime dependencies; installed `orch 0.3.0` initialized fresh state and reported schema v2 `READY`.

## Remaining boundaries

Broad RDC terminal access remains a cooperative rather than OS-enforced sandbox. ChatGPT Scheduled timing/chat URL identity limitations are unchanged and intentionally irrelevant to Variant B correctness. The main `<protected-project>` is still not connected for ORCH writes. No Git remote is configured for the ORCH source repository yet; its commit history remains local on the Mac.
