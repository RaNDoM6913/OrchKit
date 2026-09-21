# OrchKit — review evidence namespace isolation

**Date:** 2026-09-20. **Scope:** verifier → optional review evidence handoff.

## Problem

Review export copied frozen project/support bytes into `workspace/` and then used a fixed `workspace/verification_evidence/` path for OrchKit-owned evidence. A legitimate project file at the same path could therefore be overwritten in the reviewer snapshot. Separately, verifier check IDs such as `scope` or `authority` could alias system log filenames before export, making distinct exported paths carry already-collided source evidence.

## Change

`prepare_review` now creates a fresh private `.orch-review-evidence-*` directory only after all project and support files are frozen. System scope and check-authority evidence live at that private root, while per-check evidence lives under its `checks/` child.

Verifier check-result logs now use the disjoint source namespace `<run_id>-check-result-<check_id>.json`; system logs retain `<run_id>-scope.json` and `<run_id>-check-authority.json`. The snapshot manifest and review exporter use the same bound filenames.

## Evidence

Regression tests preserve a real project `verification_evidence/scope.json` byte-for-byte in the frozen workspace and run checks named both `scope` and `authority`, independently validating the system and check JSON payloads.
