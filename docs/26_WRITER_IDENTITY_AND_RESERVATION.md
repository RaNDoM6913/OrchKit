# ORCH v0.4 — writer identity and snapshot reservation hardening

**Date:** 2026-09-18.

## Writer identity is derived, not declared

A task can no longer choose an arbitrary `writer_key` and thereby bypass project/workspace isolation.

ORCH derives writer identity from the actual workspace:

- for a Git worktree, the identity is a hash of `git rev-parse --git-common-dir`;
- for a non-Git workspace, the identity is a hash of the resolved workspace path.

An explicit writer key carried by a registered-project plan is accepted only if it exactly matches the independently derived identity. A mismatch fails plan loading with `writer_key_mismatch`.

This also makes linked Git worktrees share one writer lane even though their filesystem roots differ.

## Verified snapshots retain the writer reservation

The substantive ChatGPT worker is quiesced before verification, but a verified snapshot is not terminal: it may still be waiting for owner approval or exact publication.

ORCH therefore now keeps the writer reservation for run state `VERIFIED` until the run becomes terminal through `complete` or successful publication. Another independent task for the same writer identity remains `BUSY`, while unrelated projects can still proceed concurrently.

This prevents later work in the same repository from invalidating or racing a frozen verified snapshot before publication.

`reconcile` and `state check` distinguish active worker runs from broader `writer_locks`; a verified reservation produces `ATTENTION` even though no ChatGPT worker lease remains active.

## Deterministic coverage

Tests prove that:

- two linked Git worktrees derive the same Git writer identity and cannot be claimed concurrently;
- a manual plan cannot spoof a different writer key;
- after verifier PASS, a second task in the same workspace remains blocked;
- after the first verified run completes, the next task can be claimed;
- reconciliation exposes a verified writer reservation without incorrectly calling it an active worker run.
