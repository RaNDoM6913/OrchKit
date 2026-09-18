# ORCH v0.8 — batch admission and verifier authority release checkpoint

**Date:** 2026-09-18. **Version:** `0.8.0`. **Durable state schema:** v3.

## Release scope

v0.8 keeps the v0.7 readiness and authoritative-ledger guarantees and adds three production blocks:

- admission-bound verifier execution authority: absolute executable path/hash plus support/config file presence/hash binding, revalidated before checks execute;
- atomic provenance-bound batch task admission through `queue enqueue-batch`, including DAG validation and no partial queue load on failure;
- readiness visibility for queued verifier-authority drift, plus dynamic Git base binding for tasks admitted behind unresolved project work.

The trading project remains outside write scope and <separately-authorized-integration> is still disabled.

## Source acceptance

The complete deterministic suite passes **160/160** tests. Python compilation and `git diff --check` pass. Repo-local ORCH state remains schema v3 `READY` with SQLite `quick_check=ok`, zero FK violations, zero active writer locks, zero pending publications, and `reconcile=CLEAN`.

## Offline wheel

Built without dependency/network resolution using `python3 -m pip wheel . --no-deps --no-build-isolation -w dist`.

Artifact: `dist/agent_workflow_orchestrator-0.8.0-py3-none-any.whl`

- bytes: `77122`
- SHA-256: `1e65ab626ff431f543577ec68a3c99d44d55614ffaa86f3cf299f3678fc11ade`

The wheel was installed with `pip install --no-index --no-deps` into a fresh disposable Python 3.9 venv.

## Installed CLI acceptance — batch publication

The installed console command proved:

1. `orch 0.8.0` version reporting;
2. Standard/off-review project registration against a disposable repository and local bare remote;
3. atomic admission of a two-task JSON batch with the first task admission-bound to the initial HEAD and the second task intentionally lacking a stale `expected_base`;
4. first task verify + sandboxed publication to commit `940c3e30c878bbac60af1a6379c4b6d411838373`;
5. second already-queued task verify against the advanced HEAD + sandboxed publication to commit `0b2e08e891506f9b92dc7fced8e31ca7584ee102`;
6. exact bare-remote branch head equal to the second commit;
7. final state `READY`, reconcile `CLEAN`, and project-scoped next state `NO_WORK`.

## Installed CLI acceptance — readiness authority drift

A second disposable installed project used an automatically detected npm test check. ORCH admitted a queued task and bound the check executable plus `package.json` authority. The fixture then changed `package.json`, committed that change, and returned to a clean Git worktree.

`orch project audit PROJECT_ID` returned top-level `BLOCKED`; the dedicated `check_execution_authority` finding was also `BLOCKED` and reported `package.json` with `HASH_CHANGED`. No verifier command was executed by the readiness audit.

## Local commits in this release slice

- `f82fcca` — bind verifier check execution authority;
- `b624e32` — add atomic batch task admission and advancing-base queue behavior;
- `57eb986` — audit queued verifier authority drift;
- the release metadata/evidence is committed separately after final acceptance.

## Boundaries retained

No GitHub remote/push, paid OpenAI Platform API, purchased credits, external AI provider, Workspace Agents API, LaunchAgent/autostart, or global RDC/Codex configuration change was introduced. Codex was not used for these deterministic blocks. Broad RDC shell remains a cooperative boundary rather than an OS sandbox. `<protected-project>` was not used for implementation or tests.
