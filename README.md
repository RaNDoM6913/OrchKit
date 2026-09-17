# Agent Workflow Orchestrator — Variant B MVP

A local, subscription-only coordinator for development performed by real ChatGPT conversations through Remote Desktop Commander (RDC). Each task or repair attempt intentionally uses a **new ChatGPT conversation**. The next conversation receives bounded durable state from SQLite instead of relying on previous chat context.

## Current state

Implemented and locally verified on 2026-09-17:

- SQLite durable plan/task/run/event ledger with immutable plan-revision digests.
- DAG dependency checks and atomic single-writer claims.
- Capability-file based run authority (`0600`) so lease secrets are not put in command arguments.
- Bounded context packs (32 KiB) with verifier/Codex feedback carried into a new attempt/chat.
- Receipt validation against task write allowlists.
- Cooperative quiescence marker with the direct-RDC residual risk explicitly recorded.
- Independent registered checks, protected-file hashes, content snapshots and stale-snapshot detection.
- Snapshot-bound review import; failed verification/review becomes `NEEDS_FIX` and is picked up by a **new** ChatGPT run.
- Codex subscription preflight via the official app-server (`account/read`, `account/rateLimits/read`), hooks disabled, purchased-credit fallback blocked.
- Frozen read-only Codex review export and output schema; model review is only launched by explicit `codex-review --execute` after preflight PASS.
- Exact Git publication: verified bytes → exact stage → one commit → ordinary push → `ls-remote` verification. Pre-existing staging blocks publication.
- Recovery/status commands do not auto-expire active writers.
- Static Scheduled ChatGPT dispatcher prompt at `dispatcher_prompt.txt`.

Local E2E evidence: two dependent fixture tasks completed as two independent runs; both checks passed, two commits were pushed to a local bare remote, local/remote HEAD matched, protected sentinel stayed unchanged, and the queue ended at `NO_WORK`/`CLEAN`.

## Why Variant B

ORCH-001 proved standalone Scheduled ChatGPT → RDC → Mac, but the available automation surface did not prove an observable chat ID or same-chat context continuation. The owner explicitly approved Variant B: a new real ChatGPT conversation per task/repair, with durable handoff through this coordinator.

This removes same-chat continuation from the acceptance contract while preserving the important requirements: ChatGPT remains the substantive worker, RDC remains the Mac bridge, Codex remains review-only, and no paid API/external-AI fallback is allowed.

## Run locally

```sh
cd <orchkit-root>
./bin/orch init
./bin/orch load-plan /absolute/path/to/approved-plan.json
./bin/orch next
./bin/orch status
./bin/orch reconcile
```

A scheduled ChatGPT worker claims exactly one attempt:

```sh
./bin/orch claim --worker scheduled-variant-b
```

The claim response contains `run_id`, `capability_file`, and the bounded task context. The worker edits only `allowed_paths`, writes a receipt, then:

```sh
./bin/orch submit --run-id RUN --cap CAP_FILE --receipt RECEIPT.json
./bin/orch quiesce --run-id RUN --cap CAP_FILE
./bin/orch verify --run-id RUN
```

For a normal verified task use `publish` when publication kind is `git`, otherwise `complete`. When review is required:

```sh
./bin/orch codex-preflight
./bin/orch codex-review --run-id RUN          # prepare/dry-run only
./bin/orch codex-review --run-id RUN --execute
```

`codex-review --execute` refuses to run when the official account preflight is not a ChatGPT-authenticated, non-exhausted subscription path or purchased-credit availability is detected.

## Scheduled dispatcher

`dispatcher_prompt.txt` is the durable bootstrap. A standalone Scheduled Task uses it to claim one task, do the work via RDC, verify/publish, call `orch next`, and — only when `READY` — create exactly one new one-time standalone Scheduled Task using the same prompt. Thus each block receives a clean ChatGPT conversation automatically.

The launcher is intentionally platform-native. The local Python program does **not** hold or repurpose OpenAI OAuth tokens and does not call a model API.

## Security boundary

The current RDC configuration has broad terminal access. Therefore helper leases and allowlists are a cooperative control, not an OS sandbox against a malicious worker. The MVP is suitable for the disposable fixtures proven here. Connecting `<protected-project>` for writes is **not enabled** and remains <separately-authorized-integration> with a separate approval and stronger repository-specific contract.

## Tests

```sh
cd <orchkit-root>
PYTHONPATH=. python3 -m unittest discover -s tests -v
python3 -m py_compile orch/*.py
```

The unit suite covers DAG ordering, single writer, scope escape, protected-file tamper, failed verifier → new-attempt feedback, stale review rejection and plan-revision digest conflicts.
