# ORCH v0.4 — project registry identity

**Date:** 2026-09-18.

## Ambiguity addressed

Project ids include the display name plus a hash of the resolved repository root. Before this hardening, registering the same exact workspace root again with a different `--name` created a second project id. Writer isolation still shared the underlying Git authority, but project-level pause, deregistration, and operator queue views could then refer to two aliases for one filesystem workspace.

## Identity rule

One exact resolved workspace root may have only one registered project identity.

`project add` now scans the existing private registry before creating a new config. If another entry already owns the same resolved root, registration fails with `project_root_already_registered:<PROJECT_ID>` instead of creating an alias.

Normal `--replace` of the same computed project identity remains supported, so operators can deliberately update that project's profile/policy without creating a new identity.

## Linked Git worktrees

Distinct linked worktree roots remain valid separate project registrations. Their roots are different, while the independently derived `writer_key` still resolves through Git common-dir, so they share one writer reservation and cannot write concurrently.

This preserves useful per-worktree queue/policy identity without weakening Git-level serialization.

## Fail-closed registry census

While checking uniqueness, ORCH will not silently ignore an unsafe, unreadable, or structurally invalid existing registry entry. Such an entry blocks a new registration until the registry problem is resolved, preventing an ambiguous duplicate from being created around damaged authority metadata.

## Deterministic evidence

Tests prove that:

- registering the same root under a different display name is rejected;
- `--replace` of the same project identity remains allowed;
- a linked worktree with a different resolved root remains registrable;
- linked worktree registrations still share their Git writer identity.

Full deterministic acceptance after this block: **81/81 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.
