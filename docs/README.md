# OrchKit Documentation

This is the public documentation set for OrchKit. It describes the implemented workflow and its boundaries without treating internal run history as product documentation.

## Start here

- [README](../README.md) — positioning, source-based Quick Start, status, and limitations.
- [Workflow guide](workflow.md) — the operator, worker, verifier, review, approval, and publication flow.
- [Architecture](architecture.md) — durable state and component boundaries.
- [Security model](security-model.md) — workflow controls, trust boundaries, and what OrchKit does not protect.
- [Compatibility](compatibility.md) — verified release-readiness environments and explicit unverified boundaries.
- [Roadmap](../ROADMAP.md) — work that must be completed before broader release or support claims.

## Documentation standards

Public documentation must:

- describe only implemented behavior or clearly labeled limitations;
- distinguish local workflow controls from operating-system security boundaries;
- keep raw task/run identifiers, device details, account metadata, private paths, logs, receipts, and generated state out of source control;
- treat ChatGPT as the primary worker and Codex as an optional review adapter;
- avoid promises about distributions, platforms, performance, integrations, or release dates until verified.

For source changes, see [CONTRIBUTING.md](../CONTRIBUTING.md). For security-sensitive reports, see [SECURITY.md](../SECURITY.md).
