# OrchKit Roadmap

OrchKit is published as an early source project. This roadmap records the public work still needed to make broader claims; it is not a delivery schedule.

## Current public baseline

- The project is named OrchKit and exposes the `orch` command.
- Source installation from this repository is documented.
- The public documentation covers the workflow, architecture, and same-user security boundary.
- Codex review is optional. The primary worker model remains a fresh ChatGPT conversation for each task or repair attempt.
- The source is licensed under Apache-2.0.

## Before a versioned release

- Select and verify a package name, distribution channel, and release artifact.
- Define supported operating systems, Python versions, Git versions, and architectures from test evidence.
- Establish an explicit private vulnerability-reporting channel and response process.
- Add automated compatibility checks only after their supported environments are defined.
- Publish upgrade and release notes only for an actual versioned release.

## Product boundaries

OrchKit remains a local control plane. It does not claim to be an OS sandbox, and it does not require paid model APIs, third-party AI providers, purchased credits, or a subscription upgrade fallback. Any change to those boundaries requires separate public documentation and verification.

For current behavior and limitations, start with the [README](README.md), [workflow guide](docs/workflow.md), and [security model](docs/security-model.md).
