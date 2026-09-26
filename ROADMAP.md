# OrchKit Roadmap

OrchKit is published as an early source project. This roadmap records the public work still needed to make broader claims; it is not a delivery schedule.

## Current public baseline

- The project is named OrchKit and exposes the `orch` command.
- Source installation from this repository is documented.
- The public documentation covers the workflow, architecture, and same-user security boundary.
- Codex review is optional. The primary worker model remains a fresh ChatGPT conversation for each task or repair attempt.
- The source is licensed under Apache-2.0.

## Immediate P0: bounded ordinary-Chat execution

The immediate capability work was split into small, dependency-ordered milestones. The runtime/test hardening already integrated on `main` is the baseline; all three milestones below now have public implementation and acceptance evidence.

1. **P0-1 — complete: plan admission budgets and visible CLI.** The runtime admits a versioned advisory execution-budget contract, preserves legacy plans, generates bounded defaults, records acceptance/non-goals and per-check deadline/output/recovery metadata, and exposes a safe contract summary through `orch queue list` before claim. Invalid contracts fail before task admission.
2. **P0-2 — complete: cooperative checkpoint/recovery with writer/process safety.** An active `RUNNING` attempt can persist a checkpoint without releasing its writer reservation or capability. Recovery distinguishes active from unknown external process state, fails closed on malformed checkpoint data, and does not recommend blind retry. A checkpointed attempt may be aborted/retried only after the operator independently confirms process inactivity with `--process-inactivity-confirmed`; that confirmation is recorded as an operator assertion, not OS/process fencing.
3. **P0-3 — complete: ordinary Chat/RDC route acceptance.** A disposable end-to-end acceptance task was claimed and executed from a fresh ordinary ChatGPT conversation through the recorded RDC device with review off. The run recorded write-once per-run route observations (`ordinary_chat` / `rdc`), used no Work/Codex execution/model API/external-provider fallback, changed only the allowed path, passed the registered local check and byte/scope verification, and completed `git_local` publication without push. Route evidence remains observational (`acceptance=NOT_EVALUATED`); acceptance comes from independent trace review rather than self-attestation.

With P0-1 through P0-3 complete, the immediate bounded ordinary-Chat sequence has an independently reviewed end-to-end acceptance trace. Dispatcher/RDC markers and route evidence remain observational building blocks and do not, by themselves, prove any future run. P0-2 still does not provide OS/process fencing, automatic proof of process inactivity, or transparent cross-conversation resume of the same `RUNNING` attempt.

## Before a versioned release

- The `orchkit` distribution name and local sdist/wheel artifact shape are verified; select and execute the actual distribution-channel publication only at the versioned-release gate.
- Expand the documented compatibility matrix only from reproducible OS, architecture, Python, and Git test evidence; the initial macOS/arm64 Python 3.9 and 3.12 evidence is recorded in `docs/compatibility.md`.
- Establish an explicit private vulnerability-reporting channel and response process.
- Add automated compatibility checks only after their supported environments are defined.
- Publish upgrade and release notes only for an actual versioned release.

## Product boundaries

OrchKit remains a local control plane. It does not claim to be an OS sandbox, and it does not require paid model APIs, third-party AI providers, purchased credits, or a subscription upgrade fallback. Any change to those boundaries requires separate public documentation and verification.

For current behavior and limitations, start with the [README](README.md), [workflow guide](docs/workflow.md), and [security model](docs/security-model.md).
