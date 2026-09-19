# ORCH post-v0.11 — atomic publication scope evidence

**Date:** 2026-09-19. **Scope:** Git publication workspace-guard evidence.

## Problem

Publication preflight and recovery guards produced security-relevant scope evidence with ordinary `Path.write_text()` followed by a chmod-style privacy repair. A leaf symlink at the predictable evidence path could therefore redirect the write into an unrelated same-user file before the guard reported the publication result.

## Change

Both success/block paths of `_publication_workspace_guard` now publish their JSON evidence through the shared atomic private writer. Existing symlink targets fail closed before replacement, evidence is created as `0600`, and the publication flow does not proceed to staging or journal creation when evidence authority cannot be written safely.

## Evidence

A dedicated regression test plants a publication-scope symlink to an owner-controlled victim, verifies that publishing fails with the atomic target safety error, confirms the victim bytes and symlink remain unchanged, and proves there is no publication journal or staged content. Existing foreign-workspace, unsnapshotted-path, and prepared-recovery publication tests remain green. The complete deterministic suite passes **227/227** tests.

<separately-authorized-integration> remains disabled and `<protected-project>` is untouched.
