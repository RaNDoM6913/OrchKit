# ORCH post-v0.7 — readiness visibility for verifier authority

**Date:** 2026-09-18. **Scope:** read-only readiness audit extension.

## Gap

Verifier checks are now admission-bound to an absolute executable hash plus relevant support/config file state. Verification correctly blocks if that authority changes before execution, but the project readiness audit previously did not inspect those durable bindings. An operator could therefore see project `READY` even though an already queued task was guaranteed to fail its verifier-authority gate.

## Audit behavior

`orch project audit PROJECT_ID` now reads nonterminal task payloads from SQLite in read-only mode and validates their stored check execution authority without running any command.

For each nonterminal task it checks:

- bound executable path is absolute, regular and non-symlink;
- current executable SHA-256 matches the admission hash;
- every bound authority file still matches its required hash;
- every authority path expected to remain absent is still absent;
- malformed/missing authority metadata fails closed.

Drift is reported through a separate `check_execution_authority` finding with `BLOCKED` status and also makes the ledger readiness state blocked. Terminal historical tasks are ignored because their verifier authority is no longer executable workflow state.

The audit remains read-only: it does not resolve a new executable from PATH, execute checks, repair task payloads, rewrite hashes, or modify the workspace/ledger.

## Deterministic evidence

Tests admit a queued task, then change and commit its support script so the Git worktree is clean. Readiness still blocks on `HASH_CHANGED`. A second test changes and commits a bound executable and readiness blocks on `executable_hash_changed`. These cases prove the finding is independent from ordinary dirty-worktree detection.
