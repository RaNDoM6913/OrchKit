# ORCH v0.4 — race-safe Git publication

**Date:** 2026-09-18.

## Defect addressed

The previous publisher checked `HEAD == expected_base` before staging/commit, then used normal `git commit`. A foreign process could advance the branch after the preflight check but before `git commit`; ORCH would then create its commit on top of the foreign commit and only discover the wrong parent afterward. The verifier prevented many foreign changes, but this remaining time-of-check/time-of-use window was inside publication itself.

## New publication primitive

Verification now records the observed Git HEAD in the frozen snapshot manifest. When a plan does not know an `expected_base` in advance (for example, a dependent task whose predecessor will publish first), that verifier-bound HEAD becomes the publication base.

Publication performs:

1. exact snapshot/current-worktree validation;
2. expected branch + exact base validation;
3. empty-index precondition;
4. exact staging and staged snapshot proof;
5. `git write-tree`;
6. `git commit-tree <tree> -p <verified-base>` to create a detached commit object without moving the branch;
7. proof that the detached commit contains exactly the verified snapshot and exact parent;
8. durable journal state `PREPARED` with the commit id;
9. compare-and-swap `git update-ref refs/heads/<branch> <commit> <verified-base>`;
10. only after CAS success, journal `COMMITTED`, followed by ordinary push/remote verification when configured.

## Recovery

The publication journal now has a PREPARED phase between staging and branch movement. After a restart, reconciliation can prove the prepared commit and resume the exact branch update without creating a second commit. If the branch no longer matches the verified base, reconciliation blocks and requires operator attention.

The Scheduled ChatGPT dispatcher recognizes PREPARED_PENDING_REF_UPDATE and routes it through publication reconciliation.

Deterministic coverage includes dynamic base binding, base drift detected before verification, branch movement during publication, and restart recovery from PREPARED.
