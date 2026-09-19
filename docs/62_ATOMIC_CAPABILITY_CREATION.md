# ORCH post-v0.11 — atomic capability creation

**Date:** 2026-09-19. **Scope:** claim-bound capability publication only.

## Problem

v0.11 hardened capability reads, but claim creation still wrote the lease JSON directly with `Path.write_text` and then changed its mode. The private claims directory and random run id reduced exposure, but creation did not use the same crash-safe/symlink-safe authority writer as other hardened state.

## Change

Claim now publishes the exact `{"run_id","lease_token"}` capability through the shared atomic JSON writer with mode `0600`. The writer uses an exclusive random same-directory temporary file, fsyncs it, atomically replaces the target, and fsyncs the directory.

The existing failure contract is retained: any capability publication failure aborts the durable run, blocks the task, removes an ordinary partial capability when present, releases writer authority, and returns `capability_create_failed`.

## Evidence

Dedicated tests prove successful exact-path/private-mode capability creation, normal hardened readback, publication failure recovery, symlink/alias rejection, size/mode rejection, and revocation on quiesce/abort. The complete deterministic source suite passes **209/209** tests; Python compilation and `git diff --check` pass.

No Codex/model invocation is used. <separately-authorized-integration> remains disabled and the market-workstation repository is untouched.
