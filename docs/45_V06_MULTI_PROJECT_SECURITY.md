# ORCH v0.6 — multi-project security release checkpoint

**Date:** 2026-09-18. **Version:** `0.6.0`. **Durable state schema:** v3.

## Release scope

v0.6 keeps the v0.5 recovery/productization guarantees and adds two production blocks:

- sandboxed Git publication transport with durable bound remote URLs and fail-closed transport classification;
- project-scoped Scheduled ChatGPT dispatcher prompts so independent registered projects can use separate fresh-chat dispatchers while durable writer keys remain the concurrency authority.

No GitHub remote was added to the ORCH source repository, and <separately-authorized-integration> remains disabled.

## Source acceptance

The complete deterministic source suite passes **125/125** tests. Python compilation and `git diff --check` pass. Repo-local state remains schema v3 with SQLite `quick_check=ok`, no FK violations, no active writers, no pending publications, and `reconcile=CLEAN`.

## Offline wheel

Built without dependency/network resolution using `python3 -m pip wheel . --no-deps --no-build-isolation -w dist`.

Artifact: `dist/agent_workflow_orchestrator-0.6.0-py3-none-any.whl`

- bytes: `66061`
- SHA-256: `4ad212825e42ae86b36563cf11223693f6f0f8fa46e0be7c5e2e02b380cbe342`

The wheel was installed with `pip install --no-index --no-deps` into a fresh disposable Python 3.9 venv.

## Installed CLI acceptance

The installed console command, not the repo wrapper, proved:

1. `orch 0.6.0` version reporting;
2. fresh ORCH setup and Standard/off-review project registration;
3. rendering a private project-scoped dispatcher prompt with `0600` mode;
4. durable project-filtered enqueue and claim;
5. worker submit/quiesce and independent verifier PASS;
6. sandboxed Git publication to a disposable local bare remote;
7. exact local/remote commit match at `0e7dbcd1b2a6b2e89162bdbc7c3f95f6c26e7f52`;
8. final state `READY`, reconcile `CLEAN`, and project-scoped next state `NO_WORK`.

## Security / product boundaries retained

- ChatGPT remains the primary substantive worker; Codex remains optional review-only policy.
- No paid OpenAI Platform API, purchased credits, external AI provider, Workspace Agents API, LaunchAgent/autostart, or global RDC/Codex configuration change was introduced.
- GitHub is still not connected and the source repository has no remote.
- HTTPS publication intentionally does not execute interactive credential helpers; a future credential adapter must be explicit.
- SSH publication uses the trusted system SSH binary with user SSH config/proxy/local-command execution disabled and strict host-key checking.
- Broad RDC terminal access remains a cooperative boundary rather than an OS sandbox.
- `<protected-project>` remains outside ORCH write scope; <separately-authorized-integration> still requires separate owner approval.
