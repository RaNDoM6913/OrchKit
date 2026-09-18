# ORCH v0.4 — Git executable-authority hardening

**Date:** 2026-09-18.

## Threat addressed

Git read/write commands are not inherently passive. A repository or Git configuration can attach executable behavior through clean/process filters and hooks. A normal `git diff` or `git status` may execute a configured clean filter, while `git push` may execute `pre-push`.

ORCH must treat repository contents/configuration as data and policy input, not as implicit executable authority.

## Read-only Git census

Project inventory/policy probes and verifier workspace census now discover active `filter` attributes without executing the filter command. Discovered filter drivers are overridden for the ORCH Git subprocess with:

- empty `filter.<driver>.process`;
- trusted absolute `/bin/cat` (or `/usr/bin/cat`) passthrough for clean/smudge;
- `required=false`.

This preserves a conservative raw-byte comparison while preventing project-defined filter commands from executing during ORCH inventory/status/diff operations. Unsupported or malformed filter driver identities fail closed.

Verifier scope evidence records the filter drivers it neutralized.

## Publication boundary

Every ORCH publication Git subprocess sets `core.hooksPath` to an ORCH-owned empty private directory. Repository hooks, including `pre-push`, therefore do not execute during publication.

Publication also checks the frozen changed paths with `git check-attr filter`. If any changed path has an active filter driver, publication fails before staging with `publication_filtered_path_not_supported`. ORCH does not silently reinterpret filtered bytes.

The hooks-guard directory is part of private state and is required to remain empty and mode `0700`.

## Deterministic evidence

Tests use real executable fixtures:

- a clean filter whose shell command writes a sentinel file; project inventory, policy evaluation, and verifier census complete without creating the sentinel, and publication blocks before the filter can run;
- an executable `pre-push` hook that writes a sentinel and exits 97; ORCH publication to a disposable bare remote succeeds and the sentinel is never created.

This hardening is process-local. It does not edit repository Git config or global Git/Codex/RDC configuration.
