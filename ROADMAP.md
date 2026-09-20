# OrchKit Public Roadmap

OrchKit's public release is being prepared in small, reviewable stages.

## Milestone 1 — Public-safe baseline

- Keep the public identity consistent as OrchKit with the `orch` CLI.
- Keep Apache-2.0 licensing.
- Publish only generalized documentation and public-safe examples.
- Exclude local runtime state, receipts, logs, backups, generated prompts, build output, machine-specific paths, device identifiers, raw workflow IDs, and unrelated project references.
- Describe the security boundary accurately: cooperative local control, not an OS sandbox.
- Describe current validation as macOS-oriented with Python 3.9+.

Status: in progress.

## Milestone 2 — README v1 and project metadata

- Refine the public README.
- Maintain a public documentation index.
- Publish architecture and security documentation.
- Add the task-lifecycle guide.
- Decide the public package name, first release version, installation method, and supported platform matrix.
- Add repository metadata only when supported by verified project facts.

Status: in progress.

## Milestone 3 — Guides and release workflow

Planned documentation includes:

- project setup and readiness audit;
- task lifecycle and retries;
- verification and optional model review;
- guarded Git publication and reconciliation;
- recovery and operator controls;
- local state, retention, backup, and restore;
- a public security policy;
- a contribution guide;
- release notes and upgrade guidance.

Status: planned.

## Source publication gate

Implementation source should be published only after an exact clean source revision is selected and reviewed for public release.

Before that step:

1. Resolve local uncommitted work.
2. Select the exact source revision.
3. Run the verification suite against that revision.
4. Audit the candidate tree for private or generated development material.
5. Re-check public documentation claims against that revision.
6. Decide the public version and distribution method.

## Open decisions

The following remain intentionally unresolved until verified:

- first public version;
- public Python distribution name;
- installation channel;
- supported macOS versions and architectures;
- non-macOS support status;
- minimum Git version, if required;
- public ChatGPT/RDC prerequisite wording;
- contribution policy;
- security-reporting channel;
- CI matrix;
- authoritative release artifact.

Unknowns should remain documented as unknown rather than being filled with assumptions.
