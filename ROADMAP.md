# OrchKit Roadmap

OrchKit is an early open-source project. This roadmap records the public work still needed to make broader claims; it is not a delivery schedule.

## Current public baseline

- The project is named OrchKit and exposes the `orch` command.
- The public `v0.11.0` source tag can be installed; PyPI publication and the matching GitHub Release remain pending.
- The public documentation covers the workflow, architecture, and same-user security boundary.
- Codex review is optional. The primary worker model remains a fresh ChatGPT conversation for each task or repair attempt.
- The source is licensed under Apache-2.0.

## Immediate P0: bounded ordinary-Chat execution

The immediate capability work was split into small, dependency-ordered milestones. The runtime/test hardening already integrated on `main` is the baseline; all three milestones below now have public implementation and acceptance evidence.

1. **P0-1 — complete: plan admission budgets and visible CLI.** The runtime admits a versioned advisory execution-budget contract, preserves legacy plans, generates bounded defaults, records acceptance/non-goals and per-check deadline/output/recovery metadata, and exposes a safe contract summary through `orch queue list` before claim. Invalid contracts fail before task admission.
2. **P0-2 — complete: cooperative checkpoint/recovery with writer/process safety.** An active `RUNNING` attempt can persist a checkpoint without releasing its writer reservation or capability. Recovery distinguishes active from unknown external process state, fails closed on malformed checkpoint data, and does not recommend blind retry. A checkpointed attempt may be aborted/retried only after the operator independently confirms process inactivity with `--process-inactivity-confirmed`; that confirmation is recorded as an operator assertion, not OS/process fencing.
3. **P0-3 — complete: ordinary Chat/RDC route acceptance.** A disposable end-to-end acceptance task was claimed and executed from a fresh ordinary ChatGPT conversation through the recorded RDC device with review off. The run recorded write-once per-run route observations (`ordinary_chat` / `rdc`), used no Work/Codex execution/model API/external-provider fallback, changed only the allowed path, passed the registered local check and byte/scope verification, and completed `git_local` publication without push. Route evidence remains observational (`acceptance=NOT_EVALUATED`); acceptance comes from independent trace review rather than self-attestation.

With P0-1 through P0-3 complete, the immediate bounded ordinary-Chat sequence has an independently reviewed end-to-end acceptance trace. Dispatcher/RDC markers and route evidence remain observational building blocks and do not, by themselves, prove any future run. P0-2 still does not provide OS/process fencing, automatic proof of process inactivity, or transparent cross-conversation resume of the same `RUNNING` attempt.

## Release discipline

- The `orchkit` distribution name and sdist/wheel artifact shape are verified. The intended package channel is PyPI with a matching GitHub Release; publication remains owner-gated and follows `docs/release-process.md`.
- Expand the documented compatibility matrix only from reproducible OS, architecture, Python, and Git test evidence; current macOS/arm64 CI covers CPython 3.9 through 3.14 and is recorded in `docs/compatibility.md`.
- Private vulnerability reporting is enabled and the response process is documented in `SECURITY.md`; keep that channel available for every release.
- Compatibility CI runs on pull requests and `main` for the documented macOS/arm64 CPython 3.9 through 3.14 matrix. Expand it only after the intended environment is named and reproducible evidence is added.
- Publish a versioned changelog and release notes only when the owner explicitly authorizes that release.

## P1: two or three independent projects

Start P1 implementation after completing the 0.11.0 publication gate.

The first target is one operator, one Mac, and up to three independent project workers. Preserve one active writer per existing `writer_key`; linked worktrees that share a Git common directory remain serialized.

1. **P1-A — prove the current scheduler.** Use three disposable repositories and one disposable home. Verify concurrent claims, project-scoped pause, independent verification/publication, and the shared-Git negative case. A synthetic fixture is not evidence of three real ChatGPT sessions.
2. **P1-B — isolate transport semantics.** Introduce executor/transport boundaries while retaining RDC behavior and historical route evidence.
3. **P1-C/D — build the local Bridge and session broker.** Add run-scoped operations, local authorization, owned-process tracking, capacity limits, and explicit reconnect states.
4. **P1-E/F — connect and accept the everyday workflow.** Add the remote MCP route, streaming, and idempotency; then verify independent ordinary-Chat workers on one Mac.
5. **P1-G — compare transports before migration.** Keep RDC available until equivalent fresh acceptance evidence exists.

These are planned milestones, not capabilities shipped in 0.11.0. The [detailed Bridge plan](docs/multi-project-bridge.md) includes acceptance gates and the deferred same-repository, multi-device, and hosted phases.

## Distribution and interfaces

**Now: the `orch` CLI.** Keep installation, updates, diagnostics, and recovery dependable. PyPI plus the matching GitHub Release is the target distribution channel; publication is currently pending, while the source-tag route works. [pipx installation](docs/installation.md) exposes the existing command without a project-local virtual environment activation step. A dedicated OrchKit Homebrew formula is a possible later channel, not an available command today.

**Next: a stable local Bridge contract.** The CLI, future connector, and optional interfaces must use the same task/run authority, writer locks, verification gates, and uncertainty handling. Do not let a UI write directly to SQLite or bypass the core with unrestricted shell commands.

**Optional: an interactive terminal dashboard.** Add this only when real operator use justifies it. It must not become a prerequisite for headless operation or delay the Bridge.

**Later: a native macOS client in Swift/SwiftUI.** Prefer a small menu-bar/status window first: project readiness, connection state, task progress, bounded diagnostics, and explicit controls. A pause control must say that it blocks new claims rather than implying that a writer was terminated. Keep the Python core; do not rewrite its safety rules in Swift. The app should consume a versioned local API and present unknown outcomes honestly.

Before distributing a native app, validate protocol compatibility, least-privilege startup, user-controlled background operation, upgrade/recovery behavior, code signing, notarization, and an uninstall path that preserves workflow state by default. A signed app/DMG is a future deliverable, not part of the Python release.

## Product boundaries

OrchKit remains a local control plane. It does not claim to be an OS sandbox, and it does not require paid model APIs, third-party AI providers, purchased credits, or a subscription upgrade fallback. Any change to those boundaries requires separate public documentation and verification.

For current behavior and limitations, start with the [README](README.md), [workflow guide](docs/workflow.md), and [security model](docs/security-model.md).
