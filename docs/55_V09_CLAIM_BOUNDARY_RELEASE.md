# ORCH v0.9 — claim boundary hardening release checkpoint

**Date:** 2026-09-18. **Version:** `0.9.0`. **Durable state schema:** v4.

## Release scope

v0.9 keeps the v0.8 batch/verifier-authority guarantees and adds:

- run-specific durable `claim_git_head` binding for every new Git publication attempt;
- transactional state migration v3→v4;
- claim-time branch/explicit-base validation before capability issuance;
- pre-claim protected baseline, independent workspace census, and verifier-authority validation;
- dispatcher fail-closed handling for claim status `BLOCKED`.

`<protected-project>` remains outside write scope and <separately-authorized-integration> remains disabled.

## Source acceptance

The full deterministic suite passes **167/167** tests. Python compilation and `git diff --check` pass. The repo-local ORCH ledger migrated from schema v3 to v4 transactionally and reports `READY`; `reconcile` reports `CLEAN`, with zero active writers and zero pending publications.

## Offline wheel

Artifact: `dist/agent_workflow_orchestrator-0.9.0-py3-none-any.whl`

- bytes: `78285`
- SHA-256: `b6fa8237bd5cca9d37997b663bf363d9134671fa47f89211296a1aff060b669c`

Built with `python3 -m pip wheel . --no-deps --no-build-isolation -w dist` and installed into a fresh disposable Python 3.9 venv using `pip install --no-index --no-deps`.

## Installed acceptance — claim-time base

A Standard/off-review disposable project queued two independent Git tasks before either ran. The first task claimed the initial HEAD, verified, and published commit `9a658ba0a73069529ed138fdf54f7e6f1ccaf13c`.

The already-queued second task then claimed and persisted exactly that first published commit as its `claim_git_head`. After the worker submitted/quiesced, a separate foreign commit advanced HEAD to `a4883a5d1c83ac592af3ef66e29ef3c5c79483fd`. Verification returned `BLOCKED` with `workspace_base_changed` and retained the original claim head in evidence. Final state was schema v4 `READY` and reconcile `CLEAN`.

## Installed acceptance — pre-claim guard

A second disposable project admitted a task while clean, then received a new untracked `foreign.txt` before claim. Installed `orch claim` returned `BLOCKED` with `claim_workspace_dirty:foreign.txt`; the claims directory remained empty, proving no writer capability was issued. State remained schema v4 `READY`.

The installed project-scoped dispatcher was also rendered and verified to treat `BUSY, BLOCKED, PAUSED` as no-write/no-rearm claim states.

## Release commits

- `470cac5` — bind Git base at claim time and migrate state to v4;
- `c20b4a9` — guard workspace/protected/check authority before capability;
- release metadata/evidence is committed after final acceptance.

## Boundaries retained

No GitHub remote/push, paid OpenAI Platform API, purchased credits, external AI provider, Workspace Agents API, LaunchAgent/autostart, or global RDC/Codex configuration change was introduced. Codex was not used. Broad RDC shell remains a cooperative boundary rather than an OS sandbox.
