# ORCH v0.5 — recovery productization release checkpoint

**Date:** 2026-09-18. **Version:** `0.5.0`. **Durable state schema:** v3.

## Release scope

v0.5 builds on the v0.4 multi-project queue/writer-isolation foundation and hardens the path from verified work through publication, project lifecycle, durable backup, restart recovery, state-home replacement, and rollback.

Major additions in this slice include:

- full Git workspace scope revalidation immediately before publication side effects and ref CAS;
- project-level queue pause/resume, safe deregistration, unique exact-root registry identity, and project-registry/ledger authority binding;
- read-only run/publication recovery inspection with no timeout-based lease expiry;
- backup archive verification bound to the exact restore surface;
- atomic restore into a fresh ORCH home with transactional schema migration before publication;
- crash-safe standalone-home replacement with external journal, retained rollback, explicit finalize, and inode-bound rollback;
- replacement-aware read-only recovery inspection and external sibling artifact census;
- lazy CLI state initialization so archive/replacement recovery does not create phantom state homes;
- replacement quiescence checks that refuse active, paused, or nonterminal queued state.

## Deterministic acceptance

After the v0.5 version bump, the complete source suite passed **118/118** tests. `python3 -m py_compile orch/*.py` and `git diff --check` passed. The repo-local ORCH state remained schema v3 `READY` with SQLite `quick_check=ok`, zero FK violations, zero active writer locks, zero pending publications, and `reconcile=CLEAN`.

No Codex/model review was used for this release slice because deterministic tests covered the implemented contracts.

## Offline wheel

Built with:

```sh
python3 -m pip wheel . --no-deps --no-build-isolation -w dist
```

Artifact: `dist/agent_workflow_orchestrator-0.5.0-py3-none-any.whl`

- bytes: `62524`
- SHA-256: `84fbe9e65f984fae2435493da56dab1f579e6b21b068b95dada9f578c4252940`

The wheel was installed with `pip install --no-index --no-deps` into a fresh disposable Python 3.9 venv.

## Installed CLI acceptance

The installed console entrypoint, not the repo wrapper, proved:

1. `orch 0.5.0` version reporting;
2. Safe setup plus Standard/off-review project registration with immediate authoritative ledger creation;
3. durable `queue enqueue` and project-filtered `queue list` reporting `READY`;
4. secret-free state backup and `verify-backup` reporting `VERIFIED/CURRENT` while a phantom command root remained absent;
5. fresh restore into an explicit destination preserving the queued task and allowing packaged dispatcher regeneration;
6. standalone-home replacement from a Standard-profile backup while the replacement-only command root remained absent;
7. replacement-aware recovery inspection reporting `REPLACEMENT_ROLLBACK_AVAILABLE`;
8. explicit rollback to the original Safe-profile home followed by explicit finalize;
9. `replacement-receipt.json` recording `outcome=ROLLED_BACK` and final restored state reporting `READY`.

All installed acceptance paths were disposable and removed afterward. The installed acceptance was repeated after the final README/package-metadata build; the recorded SHA-256 above is the exact wheel that passed the final smoke.

## Boundaries retained

- Source repository Git remains local; no remote was added and no GitHub push occurred.
- `<protected-project>` remains outside ORCH write scope; <separately-authorized-integration> is still not enabled.
- No paid OpenAI Platform API, extra credits, external AI provider, Workspace Agents API, LaunchAgent, or global RDC/Codex configuration change was introduced.
- Broad RDC terminal access remains a cooperative control rather than an OS sandbox.
- Additional Git transport execution-authority hardening remains a future security item; a defensive implementation attempt was blocked by the tool security gateway and was not bypassed.
