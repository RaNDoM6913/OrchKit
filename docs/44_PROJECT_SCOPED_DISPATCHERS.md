# ORCH post-v0.5 — project-scoped Scheduled ChatGPT dispatchers

**Date:** 2026-09-18. **Scope:** multi-project dispatch UX; Variant B remains unchanged.

## Gap

The durable queue already permits concurrent active writers when projects have independent writer keys, and `claim --project` / `next --project` already exist. The packaged Scheduled ChatGPT prompt, however, always used the global claim/next commands. One reusable global Scheduled Task therefore serialized practical dispatch even when the ledger could safely run independent projects concurrently.

## Scoped prompt rendering

`orch dispatcher render --project PROJECT_ID` now renders a prompt permanently bound to one registered project. The rendered worker identity includes the project id, and both dispatch commands are project-filtered:

- `claim --worker scheduled-variant-b-PROJECT_ID --project PROJECT_ID`
- `next --project PROJECT_ID`

The prompt explicitly forbids claiming work from another project. Unknown/invalid project identities fail before a prompt is produced.

The default output for a scoped prompt is `ORCH_HOME/dispatchers/PROJECT_ID.txt`; the directory is `0700` and the prompt is `0600`. The original global `dispatcher render` remains available and backward compatible.

## Concurrency semantics

An owner can create separate native Scheduled ChatGPT tasks from different project-scoped prompts. Independent Git repositories may then be claimed by separate fresh ChatGPT conversations at the same time. ORCH's durable writer key still provides the safety boundary: linked worktrees sharing one Git common directory cannot obtain simultaneous writer reservations.

A scoped dispatcher that encounters `BUSY` remains fail-closed and does not spin/re-arm automatically. This avoids an unbounded scheduler retry loop around a crashed/stuck writer; operator recovery remains explicit. For independent projects there is no shared-writer BUSY condition.

## Deterministic evidence

Tests prove scoped claim/next rendering, distinct worker identity, private prompt permissions, unknown-project refusal, and backward-compatible global rendering. No native Scheduled Task was created or modified in this block, and no LaunchAgent/autostart was introduced.
