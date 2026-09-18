# ORCH post-v0.8 — pre-claim workspace safety guard

**Date:** 2026-09-18. **State schema:** v4.

## Gap

Before this block, ORCH could admit a task while a project was clean, then later issue a writer capability even if foreign/unprotected workspace bytes or verifier-authority drift appeared before claim. Verification would eventually detect many of those conditions, but the ChatGPT worker had already received authority to edit the project.

## Claim boundary

Immediately before creating a run/capability, ORCH now revalidates the selected task against current workspace state:

- protected baseline files must still exist as regular non-symlink files with the recorded SHA-256;
- independent Git scope census must contain no new non-protected tracked, staged, or untracked changes;
- if a Git claim head was just bound, the census HEAD must still match it, closing a race inside claim preflight;
- bound verifier executable/support/config authority must still validate.

Only unchanged protected pre-existing dirty bytes are subtracted from the census. New bytes are blocked even when they happen to fall inside the task's future `allowed_paths`, because they existed before the worker received authority and therefore cannot safely be attributed to that worker attempt.

A failed guard transitions the task to `BLOCKED`, records a `TASK_BLOCKED_AT_CLAIM` event, returns a structured `BLOCKED` claim result, and creates no capability file.

Non-Git workspaces preserve their existing cooperative limitation: Git census is unavailable, but protected hashes and verifier-authority checks still run.

## Deterministic evidence

Tests prove that a new untracked file blocks before capability creation, unchanged protected pre-existing dirty bytes remain claimable, and check-support drift blocks before capability creation. The full suite remains green after the stricter boundary.
