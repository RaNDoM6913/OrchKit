# OrchKit

Durable local orchestration for development work across fresh ChatGPT conversations.

OrchKit is a local control plane for scope-bounded, verifiable development workflows. Each task or repair attempt runs in a new ChatGPT conversation, while durable workflow state lives locally instead of depending on previous chat history.

## Why OrchKit?

Long-running development work becomes fragile when correctness depends on one conversation remembering everything that happened before.

OrchKit separates durable workflow state from the chat itself. It keeps task state, retry context, verification evidence, review state, approvals, and publication state in a local control plane so a fresh ChatGPT conversation can continue from bounded, explicit context.

## How it works

1. A Git project is registered with OrchKit.
2. A bounded task is added with allowed paths, checks, review policy, and publication policy.
3. A fresh ChatGPT conversation claims one task attempt.
4. OrchKit returns the task's bounded durable context.
5. ChatGPT performs the substantive development work.
6. The worker submits its result and relinquishes its write capability.
7. OrchKit independently verifies the actual workspace and registered checks.
8. Optional Codex review can run against verified frozen input when policy requires it.
9. Required owner approval is bound to the verified snapshot.
10. Verified work can be completed or published through guarded Git steps.
11. If more work is ready, the next attempt starts in another fresh ChatGPT conversation.

## Core properties

- Fresh ChatGPT conversation for every task or repair attempt.
- Durable local SQLite ledger for workflow state.
- Bounded context instead of dependency on prior chat history.
- Scope-bounded writes through explicit allowed paths.
- Protected-baseline checks for pre-existing user work.
- Independent local verification before completion or publication.
- Snapshot-bound review and owner approval.
- Optional Codex review; ChatGPT remains the primary worker.
- Guarded Git publication without force-push, reset, clean, or stash.
- Durable reconciliation for uncertain publication outcomes.
- Explicit blocked and paused states instead of treating uncertainty as success.

## Requirements

The current implementation is validated primarily in a macOS-oriented environment.

- Python 3.9 or newer
- Git
- A connected ChatGPT-to-computer bridge for the automated worker workflow

Codex is optional and is only used when review policy enables it.

Broader operating-system support has not yet been established.

## CLI

The command-line entry point is:

```text
orch
```

The verified setup flow begins with:

```sh
orch setup --profile safe
orch doctor
orch rdc bootstrap-prompt
orch rdc show
```

A project can then be registered and audited:

```sh
orch project add /path/to/repository --profile safe
orch project list
orch project audit PROJECT_ID
orch dispatcher render --project PROJECT_ID
```

Installation instructions will be added when the public distribution method is finalized.

## Security boundary

OrchKit is a cooperative local control plane, not an operating-system sandbox.

It verifies workflow state, scope, capabilities, snapshots, checks, approvals, and publication policy. However, a process with broad terminal access under the same operating-system user may still be able to bypass helper-level restrictions.

Strong adversarial isolation would require an additional OS or identity boundary that OrchKit does not currently provide.

## Verification and review

Worker self-report is not treated as independent verification.

OrchKit checks the actual workspace, declared scope, protected content, registered verification authority, and configured checks before a result can advance.

Review policy supports three modes:

- `off`
- `risk_based`
- `required`

The packaged reviewer is Codex, but Codex is not required for OrchKit's core workflow.

## Git publication

When Git publication is enabled, OrchKit is designed to publish only verified snapshot content through guarded steps.

The current implementation includes checks for:

- expected branch and base
- clean pre-existing staging
- exact changed-path staging
- snapshot consistency
- ordinary non-force publication
- remote-ref verification
- reconciliation after uncertain commit or push outcomes

## Documentation

Start with the public documentation index:

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Security model](docs/security-model.md)
- [Public roadmap](ROADMAP.md)

Additional guides for task lifecycle, project setup, verification, Git publication, recovery, and state/backups are being added in staged documentation slices.

## Project status

OrchKit is under active development.

The current implementation has been developed and validated primarily on macOS with Python 3.9+. Public release artifacts, broader platform validation, and additional compatibility claims will be documented only after they are verified.

## Contributing

A public contribution workflow will be documented before the first contributor-focused release.

## License

Apache License 2.0.
