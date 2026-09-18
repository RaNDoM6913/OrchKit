# ORCH v0.4 — pre-publication workspace scope guard

**Date:** 2026-09-18.

## Defect addressed

Verification performs an independent Git scope census, then ORCH may wait for review or owner approval before publication. Writer reservations prevent another ORCH task from entering the same repository, but they cannot stop an unrelated local process from changing the worktree during that wait.

Before this hardening, publication revalidated the verified files, protected hashes, branch/base, and exact staging, but an unrelated new dirty or untracked path could exist without blocking publication as long as it was not included in the commit.

## New publication guard

Git publication now repeats the independent workspace census before the first Git side effect and again after the exact snapshot commit object is prepared, immediately before the compare-and-swap branch update.

The guard requires:

- verified snapshot files/deletions still match the worktree;
- protected hashes still match;
- the current dirty/staged/untracked path set is exactly the verified snapshot scope after subtracting unchanged protected pre-existing paths;
- no newly dirty path appears merely because it is inside the task allowlist;
- Git HEAD still matches the verifier-bound publication base.

Each guard writes bounded evidence under `.runtime/logs/<run_id>-publication-scope-<phase>.json`, so retention treats it as run-bound evidence.

## Crash recovery

`publish-reconcile` applies the same guard before resuming from `STAGED` or `PREPARED` while the branch still points at the verified base. If foreign workspace state appears after a crash, reconciliation returns `BLOCKED` and does not move the branch. After the foreign state is removed and the verified snapshot is again exact, the same prepared commit can resume without recommitting.

Once the compare-and-swap branch update has succeeded, recovery continues from the proven commit object; unrelated later worktree dirt does not change the already frozen commit being pushed or remotely verified.

## Deterministic evidence

Three new tests prove that publication blocks:

- a new untracked path outside the allowlist after verification;
- a new path inside the allowlist that was not part of the verified snapshot;
- foreign workspace state introduced after commit preparation, including across restart/reconciliation.

After cleanup, the prepared-commit recovery test resumes the exact same commit successfully. Full deterministic acceptance after this patch: **72/72 PASS**, Python compile PASS, and `git diff --check` PASS.
