# ORCH v0.7 — readiness and ledger-authority release checkpoint

**Date:** 2026-09-18. **Version:** `0.7.0`. **Durable state schema:** v3.

## Release scope

v0.7 keeps the v0.6 multi-project transport/dispatcher guarantees and adds:

- a read-only registered-project readiness/security audit;
- fail-closed operational use of the authoritative SQLite ledger;
- fresh-home-only ledger bootstrap rules;
- readiness-gated CLI task admission for queue enqueue and make-plan.

The trading project remains outside write scope and <separately-authorized-integration> is still disabled.

## Source acceptance

The complete deterministic suite passes **147/147** tests. Python compilation and `git diff --check` pass. Repo-local ORCH state remains schema v3 `READY` with no active writer locks or pending publications, and reconcile is `CLEAN`.

## Offline wheel

Built without dependency/network resolution using the existing offline wheel flow.

Artifact: `dist/agent_workflow_orchestrator-0.7.0-py3-none-any.whl`

- bytes: `71958`
- SHA-256: `7a287fdf6ddd526a485bf84ab8feb71b75760d0775747ed159b92c986eb6e7f6`

The wheel was installed with `pip install --no-index --no-deps` into a fresh disposable Python 3.9 venv.

## Installed CLI acceptance

The installed console command proved:

1. `orch 0.7.0` version reporting;
2. Standard/off-review project registration and RDC marker recording;
3. private project-scoped dispatcher rendering;
4. project audit with `--require-dispatcher` reporting `READY`;
5. a new foreign untracked byte causing queue admission `BLOCKED` before plan creation;
6. clean project queue admission returning `ENQUEUED` and project-scoped `next=READY`.

The failure-path portion then physically displaced the disposable authoritative SQLite file while a READY queued task existed. After that loss:

- `claim` returned `state_ledger_missing_or_unsafe`;
- `state check` returned `state_ledger_missing_or_unsafe`;
- explicit `init` refused the non-fresh home;
- `project audit` returned `BLOCKED`;
- adding another project refused to bootstrap a new ledger;
- `.runtime/orch.sqlite3` remained absent throughout — no false empty state was manufactured.

This is the installed proof that Variant B cannot convert authoritative-ledger loss into a false `NO_WORK` through the normal CLI.

## Boundaries retained

No GitHub remote/push, paid OpenAI Platform API, purchased credits, external AI provider, Workspace Agents API, LaunchAgent/autostart, or global RDC/Codex configuration change was introduced. Codex was not used for these deterministic changes. Broad RDC shell remains a cooperative boundary rather than an OS sandbox.
