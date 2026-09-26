# OrchKit Roadmap

OrchKit is published as an early source project. This roadmap records the public work still needed to make broader claims; it is not a delivery schedule.

## Current public baseline

- The project is named OrchKit and exposes the `orch` command.
- Source installation from this repository is documented.
- The public documentation covers the workflow, architecture, and same-user security boundary.
- Codex review is optional. The primary worker model remains a fresh ChatGPT conversation for each task or repair attempt.
- The source is licensed under Apache-2.0.

## Immediate P0: bounded ordinary-Chat execution

The next capability work is intentionally split into small, dependency-ordered milestones. The runtime/test hardening already integrated on `main` is the baseline; this section describes what is still planned.

1. **Plan admission budgets and visible CLI.** Admit and display a bounded execution contract with acceptance criteria, non-goals, owned paths, check deadlines/output bounds, and explicit context/usage provenance or `UNKNOWN`. Failed admission must create no run/capability side effects.
2. **Checkpoint/pause recovery with writer/process safety.** Preserve incomplete work without releasing an uncertain writer. Timeouts or missing telemetry must not expire leases. Recovery must remain visibly blocked when process inactivity cannot be proven.
3. **Ordinary Chat/RDC route acceptance.** Demonstrate a real fresh ordinary ChatGPT conversation executing a bounded task through RDC with review off, deterministic local verification, truthful route/usage evidence, and no Work/Codex/API fallback.

Current dispatcher and RDC support are useful building blocks, but they do not by themselves prove ordinary-Chat creation/continuation. Safe suspension/resumption of an incomplete attempt is also not yet an implemented guarantee.

## Before a versioned release

- Select and verify a package name, distribution channel, and release artifact.
- Define supported operating systems, Python versions, Git versions, and architectures from test evidence.
- Establish an explicit private vulnerability-reporting channel and response process.
- Add automated compatibility checks only after their supported environments are defined.
- Publish upgrade and release notes only for an actual versioned release.

## Product boundaries

OrchKit remains a local control plane. It does not claim to be an OS sandbox, and it does not require paid model APIs, third-party AI providers, purchased credits, or a subscription upgrade fallback. Any change to those boundaries requires separate public documentation and verification.

For current behavior and limitations, start with the [README](README.md), [workflow guide](docs/workflow.md), and [security model](docs/security-model.md).
