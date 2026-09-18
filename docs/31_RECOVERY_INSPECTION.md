# ORCH v0.4 — read-only recovery inspection

**Date:** 2026-09-18.

## Problem addressed

ORCH deliberately never expires a writer lease just because a heartbeat is old. That prevents unsafe duplicate writers, but after a process/Mac/ChatGPT restart the operator previously had to correlate `state check`, capabilities, run state, and publication reconciliation manually.

## Recovery inspector

A new read-only command provides one bounded recovery view:

```sh
orch recovery inspect
orch recovery inspect --run-id RUN_ID
orch recovery inspect --project PROJECT_ID
```

The inspector reads durable SQLite state and capability-file presence without reading or exposing capability contents. It reports heartbeat age only as informational evidence and explicitly sets `automatic_expiry=false`.

## State-specific guidance

The output classifies unresolved work and provides only state-compatible next steps:

- `WORKER_MAY_STILL_BE_ACTIVE`: observe the external worker; explicit `abort --retry` remains an operator decision;
- `CAPABILITY_MISSING`: never recreate lease authority; resolve through explicit abort/retry;
- `RESULT_AWAITING_QUIESCE`: quiesce using the existing capability, then verify;
- `VERIFY_RESUMABLE`: rerun deterministic verification;
- `REVIEW_DECISION_REQUIRED`: inspect review policy and use only the configured reviewer when permitted;
- `OWNER_DECISION_REQUIRED`: the verified reservation remains held pending explicit owner action;
- `PUBLICATION_READY` / `COMPLETION_READY`: continue the verified result through its configured terminal path;
- `PUBLICATION_RECONCILIATION_REQUIRED`: use `publish-reconcile` before any retry, with `--resume` only when reconciliation explicitly exposes it.

An unfinished publication journal always takes precedence over generic run-state guidance.

## Safety properties

`recovery inspect` performs no mutation, never creates or repairs a capability, never aborts a run, never infers death from elapsed time, and never calls a model. Its output contains run/task/project metadata and capability presence only; lease tokens are excluded.

This complements, rather than replaces, `state check`, `reconcile`, `abort --retry`, and `publish-reconcile`.

## Deterministic coverage

Tests cover secret-free RUNNING inspection, submitted/quiesced/verified stage classification, missing-capability handling without recreation, and publication-journal precedence. The real repo-local CLI also reports `CLEAN` on the current no-work baseline.

Full deterministic acceptance after this block: **85/85 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`, and repo-local `recovery inspect` reports `CLEAN` with no unresolved items.
