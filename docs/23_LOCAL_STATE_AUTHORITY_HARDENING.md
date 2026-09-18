# ORCH v0.4 — local state authority hardening

**Date:** 2026-09-18.

## Defect addressed

The repo-local ORCH runtime had inherited ordinary process umask permissions: `.runtime` was `0755` and SQLite, WAL, and SHM files were `0644`. The SQLite ledger contains run lease tokens, so those modes exposed orchestration authority to other local users on a multi-user machine.

## Private state boundary

ORCH now enforces the following modes whenever state is opened:

- private runtime/state directories: `0700`;
- SQLite database, WAL, and SHM: `0600`;
- capability files, registered project configs, durable plan artifacts, and state backup archives: `0600`;
- projects, plans, backups, logs, worker receipts, claims, and review-export directories: `0700`.

Existing permissive modes are repaired when ORCH opens the state. The protection does not depend on the caller's umask.

Directory and file authorities fail closed on symlinks. In particular, a symlinked `.runtime` is rejected instead of followed, and symlinked project registry entries are reported unsafe and cannot be read or removed through the registry.

## Health reporting

`orch state check` now includes a permission-health census. Missing required private state, symlinks, or mode drift makes state health `BLOCKED`. Optional state directories are checked when present.

This is deliberately local hardening only. It does not change global RDC/Codex configuration, install an agent, connect GitHub, enable <separately-authorized-integration>, or alter any target repository policy.

## Deterministic coverage

Tests cover fresh private modes, repair of legacy `0755/0644` state, SQLite sidecar modes, rejection of a symlinked runtime, rejection/reporting of a symlinked project config, private project/plan directories, and `0600` backup archives.
