# Security Model

OrchKit provides a cooperative local control plane for development workflows. It is not an operating-system sandbox.

## What OrchKit enforces at the workflow level

The current implementation includes controls for:

- bounded task definitions and allowed project paths;
- protected pre-existing workspace content;
- durable task and run state in a local SQLite ledger;
- one active writer reservation per writer identity;
- private capability files for active attempts;
- capability revocation before verification;
- independent workspace-scope verification;
- registered verification commands and verification-authority checks;
- frozen snapshots for review and publication;
- snapshot-bound owner approval when required;
- guarded Git publication without force-push, reset, clean, or stash;
- publication reconciliation when a Git outcome is uncertain;
- private local state and artifact permissions where the host filesystem supports them.

These controls are intended to make cooperative automation durable and inspectable, and to block when important workflow state cannot be proven.

## What OrchKit does not guarantee

OrchKit does **not** provide adversarial isolation from a process with broad terminal access under the same operating-system user.

Helper allowlists and private file modes are not equivalent to a separate operating-system identity. A same-user process with sufficiently broad access may reach files or processes outside the intended OrchKit workflow. Do not describe the current system as a secure sandbox.

Do not use the current architecture to execute hostile workloads or to protect credentials from same-user code. Stronger isolation needs an additional boundary, such as a separate operating-system identity or an isolated execution environment.

## Trust boundaries

### ChatGPT worker

ChatGPT is the primary substantive worker. It receives bounded task context and works through the connected computer bridge. Worker receipts are claims; they do not replace verification.

### Local verifier

The verifier checks workspace state and configured commands after a worker submits and relinquishes its capability. It can block when scope, protected state, Git base, check authority, or snapshot state no longer matches the expected workflow.

### Optional Codex reviewer

Codex is an optional review adapter, not a core requirement. When enabled by policy, it reviews frozen input associated with a verified snapshot. A reviewer finding does not expand task scope or bypass project policy.

### Owner and publisher

Required owner approval is associated with the current verified snapshot. Publication is separate from implementation and is allowed only after the configured verification, review, and approval gates are satisfied.

## Git safety boundary

The publication design avoids destructive reconciliation shortcuts. OrchKit does not use force-push, reset, clean, or stash as automatic recovery mechanisms.

Publication checks include the expected branch and base, pre-existing staging state, verified changed paths, snapshot consistency, and remote state. Remote publication uses ordinary non-force behavior and verifies the resulting remote reference. An uncertain commit or push is recorded for reconciliation rather than blindly retried.

## Local state

OrchKit keeps its durable authority locally, including the SQLite ledger and associated private artifacts. Runtime state, capability material, receipts, logs, review exports, rendered dispatcher data, and backups do not belong in the public source repository.

The automated worker workflow interacts with configured external services through its ChatGPT/RDC connection. “Local” describes OrchKit’s durable control plane; it does not mean no data can ever leave the machine.

For security-sensitive reporting guidance, see [SECURITY.md](../SECURITY.md).
