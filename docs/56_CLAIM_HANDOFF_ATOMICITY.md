# ORCH post-v0.9 — claim handoff atomicity

**Date:** 2026-09-18. **State schema:** v4.

## Defect

Claim previously persisted a `RUNNING` run and wrote its capability file before constructing the bounded context pack returned to ChatGPT. If the context exceeded the 32 KiB limit, the call failed after durable writer authority already existed. A capability filesystem write failure could similarly leave a RUNNING run without a usable handoff.

## Hardened claim sequence

ORCH now constructs and validates the exact context pack inside the claim transaction before inserting a run. Oversized context fails closed as task `BLOCKED`, records `TASK_BLOCKED_CONTEXT`, and creates neither a run nor a capability.

After a run is persisted, capability creation is guarded. A filesystem/write failure removes any safe partial capability, marks the run `ABORTED`, marks the task `BLOCKED`, records `RUN_CAPABILITY_CREATE_FAILED`, and returns a structured `BLOCKED` result instead of leaving an active writer reservation.

The context builder is shared by claim preflight and later `orch context RUN_ID`, so both paths enforce the same 32 KiB durable handoff contract and previous-feedback selection.

This cannot make SQLite plus filesystem publication mathematically atomic across power loss; existing recovery/capability census remains responsible for crash gaps. It does remove deterministic application-level failures that previously stranded authority.

## Evidence

A deterministic oversized-goal test proves there are zero run rows and zero capability files after context rejection. A synthetic capability-write failure test proves the run becomes `ABORTED`, the task becomes `BLOCKED`, no capability remains, and reconcile is `CLEAN`. Full suite: **169/169 PASS**.
