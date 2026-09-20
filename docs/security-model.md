# Security Model

OrchKit provides a cooperative local control plane for development workflows. It is not an operating-system sandbox.

This distinction is central to the design.

## What OrchKit enforces at the workflow level

The current implementation includes controls for:

- bounded task definitions and allowed project paths;
- protected pre-existing workspace content;
- durable task/run state in a local SQLite ledger;
- one active writer reservation per writer identity;
- private capability files for active attempts;
- capability revocation before verification;
- independent workspace-scope verification;
- registered verification commands and verification-authority checks;
- frozen snapshots used for review and publication;
- snapshot-bound owner approval when required;
- guarded Git publication without force-push, reset, clean, or stash;
- publication reconciliation when a Git outcome is uncertain;
- private local state/artifact permissions where supported by the host filesystem.

These controls are intended to make normal cooperative automation durable, inspectable, and fail-closed when important state cannot be proven.

## What OrchKit does not guarantee

OrchKit does **not** currently provide adversarial isolation from a process running with broad terminal access as the same operating-system user.

In particular, helper-level allowlists and private file modes are not equivalent to a separate OS security principal. A same-user process with sufficiently broad access may be able to reach files or processes outside the intended OrchKit workflow.

Do not describe the current system as a secure sandbox.

## Trust boundaries

### ChatGPT worker

ChatGPT is the primary substantive worker. It receives bounded task context and works through the connected computer bridge.

Worker-produced receipts are claims about completed work. They do not replace independent verification.

### Local verifier

The verifier checks actual workspace state and configured commands after the worker has submitted and relinquished its OrchKit capability.

Verification can block when scope, protected state, Git base, check authority, or snapshot state no longer matches the expected workflow.

### Optional Codex reviewer

Codex is an optional review adapter. It is not required for the core workflow.

When enabled by policy, review operates on frozen input associated with a verified snapshot. A reviewer finding is not itself permission to expand task scope or bypass project policy.

### Owner approval

When a task requires owner approval, that approval is associated with the current verified snapshot. A changed snapshot requires the workflow to satisfy the relevant gate again.

### Publisher

The publisher has a separate role from the worker. Publication is allowed only after the configured verification, review, and owner gates are satisfied.

## Git safety boundary

The current publication design intentionally avoids destructive reconciliation shortcuts.

OrchKit does not use force-push, reset, clean, or stash as automatic recovery mechanisms.

Publication checks include the expected branch/base, pre-existing staging state, verified changed paths, snapshot consistency, and configured remote state. Remote publication uses ordinary non-force behavior and verifies the resulting remote reference.

When the outcome of a commit or push cannot be proven, the workflow records the uncertain state and requires reconciliation instead of blindly repeating the operation.

## Local state

Durable workflow authority is stored locally by OrchKit, including the SQLite ledger and associated private artifacts.

Runtime state, capability material, receipts, logs, review exports, rendered dispatcher data, and backups should not be committed to the public source repository.

This does not imply that all task data remains exclusively on the local machine: the ChatGPT/RDC workflow necessarily interacts with the configured external services. OrchKit's local-state claim applies to its own durable workflow control plane.

## Stronger isolation

A stronger adversarial model would require an additional boundary such as a separate operating-system identity or isolated execution environment in which:

- the worker cannot access OrchKit control-plane state directly;
- publisher credentials are unavailable to the worker;
- filesystem and process capabilities are independently enforced;
- verifier and publisher authority are isolated from worker authority.

That stronger model is not part of the current verified implementation.

## Reporting security issues

A public vulnerability-reporting process has not yet been finalized. A dedicated `SECURITY.md` should be added before the first contributor-focused release with the actual supported versions and reporting channel.
