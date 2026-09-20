# OrchKit Documentation

This directory contains the public documentation for OrchKit.

## Available now

- [Architecture](architecture.md) — the verified high-level system model and component boundaries.
- [Security model](security-model.md) — what OrchKit protects, what it does not protect, and the cooperative local-control boundary.

## Planned guides

The next documentation slices are intentionally staged rather than published all at once:

- `task-lifecycle.md` — task, attempt, verification, retry, approval, and completion states.
- `project-setup.md` — project registration, profiles, readiness audit, and dispatcher setup.
- `verification-and-review.md` — local verification, frozen snapshots, and optional Codex review.
- `git-publication.md` — guarded staging, commit/push behavior, and publication reconciliation.
- `recovery.md` — pause, abort/retry, state inspection, and uncertain-outcome recovery.
- `state-and-backups.md` — local SQLite state, retention, backup, verification, and restore.

## Documentation principles

Public documentation should:

- describe only behavior that is implemented and verified;
- distinguish local workflow guarantees from cooperative security assumptions;
- avoid machine-specific paths, device identifiers, raw run IDs, account metadata, and private evidence;
- avoid unsupported platform, performance, integration, or security claims;
- treat ChatGPT as the primary worker and Codex as an optional reviewer;
- treat the local OrchKit ledger as durable workflow state rather than relying on previous chat history.

See the repository [README](../README.md) for the project overview and [ROADMAP](../ROADMAP.md) for the staged public-documentation plan.
