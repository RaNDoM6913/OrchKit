# Agent Workflow Orchestrator — v0.8 batch admission and verifier authority

A local, subscription-only coordinator for development performed by real ChatGPT conversations through Remote Desktop Commander (RDC). Each task or repair attempt intentionally uses a **new ChatGPT conversation**. The next conversation receives bounded durable state from SQLite instead of relying on previous chat context.

## Current state

Implemented and locally verified through 2026-09-18:

- SQLite durable plan/task/run/event ledger with immutable plan-revision digests.
- DAG dependency checks, cycle rejection, task-id conflict detection, durable cross-plan FIFO ordering, and atomic project/workspace writer isolation. Writer identity is independently derived from the resolved workspace/Git common directory, so plans cannot spoof isolation; VERIFIED snapshots retain that writer reservation until completion/publication.
- Capability-file based run authority (`0600`) so lease secrets are not put in command arguments; capabilities are revoked at quiesce/abort.
- Bounded context packs (32 KiB) with verifier/Codex feedback carried into a new attempt/chat.
- Receipt validation against task write allowlists.
- Verifier checks use admission-bound execution authority: argv[0] is resolved and hashed once, relevant support/config files are hash/presence-bound, and verification refuses drift before executing a check.
- Cooperative quiescence marker with the direct-RDC residual risk explicitly recorded.
- Independent Git scope census, registered checks, protected-file hashes, content snapshots and stale-snapshot detection. The verifier blocks unreported/out-of-allowlist repository changes, and Git publication repeats the scope census before side effects and before the compare-and-swap ref update so foreign post-verification worktree changes fail closed.
- Snapshot-bound review import; failed verification/review becomes `NEEDS_FIX` and is picked up by a **new** ChatGPT run.
- Codex subscription preflight via the official app-server (`account/read`, `account/rateLimits/read`), hooks disabled, purchased-credit fallback blocked.
- Frozen read-only Codex review export and output schema; model review is only launched by explicit `codex-review --execute` after preflight PASS.
- Exact Git publication: verified bytes/deletions → exact stage → snapshot-bound detached commit → compare-and-swap branch update → sandboxed bound-URL push → sandboxed remote verification. Transport runs from a short-lived ORCH-owned bare repository instead of project Git config, disables global/system config, interactive credential helpers and unsafe protocol fallback, and permits only local file, HTTPS-without-embedded-password, or hardened SSH transports. The durable publication journal records INTENT/STAGED/PREPARED/COMMITTED/PUSHED/REMOTE_VERIFIED so concurrent HEAD changes and uncertain outcomes are reconciled before retry.
- Recovery/status commands do not auto-expire active writers; `recovery inspect` gives secret-free state-specific restart guidance for both durable runs/publications and external home-replacement journals, including byte-accounted unmanaged sibling artifacts without automatic deletion. Explicit global `pause`/`resume`, project-level queue pause/resume, and `abort --retry` remain operator actions. Transactional schema upgrades/history, backup verification/fresh restore/crash-safe replacement+rollback, `state check`, stale-capability pruning, evidence retention controls, and publication reconciliation are implemented. Archive/replacement/replacement-only recovery CLI commands lazily avoid initializing an unrelated command root. Local ORCH authority is private-by-construction: runtime/state directories are `0700`, SQLite/WAL/SHM and durable authority artifacts are `0600`, and symlinked authority paths fail closed. Git inventory/verifier subprocesses neutralize configured clean/process filters, and publication disables repository hooks; changed filtered paths fail closed before staging.
- Owner acceptance is stored separately and bound to the exact `run_id + snapshot_id`.
- Packaged Scheduled ChatGPT dispatcher template at `orch/templates/dispatcher_prompt.txt`; `orch dispatcher render` creates the user-specific prompt. `dispatcher_prompt.txt` is the repo-local development render.

Local deterministic E2E evidence: two dependent fixture tasks completed as two independent runs; both checks passed, two commits were pushed to a local bare remote, local/remote HEAD matched, protected sentinel stayed unchanged, and the queue ended at `NO_WORK`/`CLEAN`.

**Real Scheduled ChatGPT E2E also passed:** the reusable standalone dispatcher ran two separate Scheduled ChatGPT workers (`scheduled-variant-b`) through RDC. `LIVE-1` and dependent `LIVE-2` each produced their own run/snapshot, passed independent checks, and were published as commits `f92c918...` and `5830e6b...` to a local bare remote. Final local/remote HEAD matched, the protected sentinel was unchanged, `orch next` returned `NO_WORK`, `orch reconcile` returned `CLEAN`, and the dispatcher was disabled.

**Autonomous chaining also passed:** in `variant-b-autochain-v1`, `CHAIN-1` completed and re-armed the same native task from inside its Scheduled ChatGPT run. A later fresh Scheduled ChatGPT run automatically claimed dependent `CHAIN-2` with no manual dispatch between them. Both published successfully; final local/remote HEAD matched at `4e571b8...`, the sentinel remained unchanged, the queue ended `NO_WORK`/`CLEAN`, and the native task ended disabled.

## Why Variant B

ORCH-001 proved standalone Scheduled ChatGPT → RDC → Mac, but the available automation surface did not prove an observable chat ID or same-chat context continuation. The owner explicitly approved Variant B: a new real ChatGPT conversation per task/repair, with durable handoff through this coordinator.

This removes same-chat continuation from the acceptance contract while preserving the important requirements: ChatGPT remains the substantive worker, RDC remains the Mac bridge, Codex is an optional review-only adapter, and no paid API/external-AI fallback is allowed.

## Installable CLI and multi-project setup

Version 0.8 keeps the v0.7 readiness/ledger guarantees and adds admission-bound verifier execution authority, atomic provenance-bound batch task admission, dynamic Git base binding behind unresolved project work, and readiness visibility for queued verifier-authority drift. The installed `orch` command uses `$ORCH_HOME` or `~/.orch` by default; the repository `bin/orch` wrapper keeps the historical repo-local runtime for development/evidence.

Build a shareable wheel without network access on the proven macOS/Python 3.9 environment:

```sh
python3 -m pip wheel . --no-deps --no-build-isolation -w dist
```

The current agent_workflow_orchestrator-0.8.0-py3-none-any.whl is built offline and validated from a clean disposable installation. The installed acceptance covers atomic batch DAG admission, sequential publication of pre-queued Git tasks across an advancing branch, and read-only detection of queued verifier-authority drift. Exact wheel size, SHA-256, commits and installed evidence are recorded in docs/52_V08_BATCH_VERIFIER_AUTHORITY.md.

First-run flow for another user:

```sh
orch setup --profile safe
orch doctor
orch rdc bootstrap-prompt
# run the returned bootstrap once in the user's own ChatGPT with RDC
orch rdc show
orch project add /absolute/path/to/repo --profile standard --review-mode risk_based
orch project list
orch project audit PROJECT_ID
orch git policy PROJECT_ID
orch dispatcher render
```

`project add` is read-only toward the target repository. It inventories root/branch/HEAD/upstream/origin/status/hooks/AGENTS/package scripts, records existing dirty/staged/untracked regular-file bytes as protected baselines, and stores policy in the user's ORCH home rather than editing the project. `safe`, `standard`, and `autonomous` profiles are available; `safe` denies commit/push by default. Force push/reset/clean/stash remain denied.

Create a bounded one-task plan from the registered project instead of hand-writing ORCH internals:

```sh
orch project make-plan PROJECT_ID \
  --task-id TASK-001 \
  --goal "Implement the approved bounded change" \
  --allowed-path src/example.py \
  --risk-tag architecture \
  --owner-approval \
  --output ~/task-001.json
orch load-plan ~/task-001.json
```

For routine durable work, enqueue directly from the registered project without hand-writing a plan:

```sh
orch queue enqueue PROJECT_ID \
  --task-id TASK-002 \
  --goal "Implement the next bounded change" \
  --allowed-path src/next.py \
  --depends TASK-001
```

For a bounded multi-task DAG, use: orch queue enqueue-batch PROJECT_ID /absolute/path/to/tasks.json. The source manifest is read-only, size-bounded, SHA-256 provenance is retained, and the whole DAG is admitted transactionally.

The compiled plan is retained under the ORCH home with mode `0600`. Dependencies may refer to tasks loaded by earlier plan revisions. Git publication bases are admission-bound only when safe: tasks queued behind unresolved work, explicit dependencies, and later tasks in a batch bind their exact Git HEAD during verification so prior ORCH publications do not create false stale-base failures.

Pause only one project queue without blocking independent projects:

```sh
orch queue pause-project PROJECT_ID --reason "maintenance"
orch queue resume-project PROJECT_ID
```

A project pause affects only new dispatch. Existing active runs keep their explicit recovery semantics, and resumed tasks keep their original durable FIFO position.

`orch project remove PROJECT_ID` is fail-closed while that project has unresolved durable work or writer/publication reservations. CLI registration creates the authoritative SQLite ledger up front; deregistration refuses to manufacture an empty ledger if that state file is missing or unsafe. Cancel or complete unresolved tasks first; successful deregistration clears project pause state but preserves historical ledger evidence.

The registry also enforces one project identity per exact resolved workspace root: the same root cannot be registered again under a different name. Distinct linked Git worktrees remain separate project roots but share the same Git writer isolation.

The project audit command performs a read-only readiness/security census before new work is entrusted to a registered project. It validates registry/root/writer identity, durable ledger integrity, queue/recovery reservations, protected and newly foreign workspace bytes, Git/publication transport policy, bound verifier executable/support-file authority for nonterminal tasks, RDC binding, and optional project-scoped dispatcher state. Use --require-dispatcher when autonomous Scheduled ChatGPT dispatch is part of the readiness contract. The audit never creates a missing ledger or edits the target repository.

Operational commands never bootstrap missing durable state. Only explicit `init` or first project registration may create a ledger, and only in a genuinely fresh home with no prior registry/runtime authority. If an established home loses its ledger, claim/next/queue/state operations fail closed until recovery. Queue enqueue and CLI make-plan also enforce the read-only readiness audit before writing a plan; BLOCKED readiness stops task admission while ATTENTION remains queueable.

Publication is derived from project policy: no publication for Safe, `git_local` when local commits are allowed but no usable remote exists, and exact commit + ordinary push + remote-ref verification when both commit and push are allowed.

## Optional Review Policy Engine

Codex is **not** a mandatory ORCH component. Task/project policy supports:

- `off`: verifier → approval/publication with no model reviewer.
- `risk_based`: review only when configured signals fire (risk tags, sensitive paths, large diffs, retry attempts).
- `required`: every successful verification goes through the selected reviewer.

The current packaged reviewer adapter is `codex`; core review decisions are independent from the Codex implementation so additional adapters can be added later. Legacy `required_review=true` plans remain compatible and normalize to `mode=required, reviewer=codex`. A task that does not require review never runs Codex preflight or consumes Codex usage.

## Run locally

```sh
cd <orchkit-root>
./bin/orch init
./bin/orch load-plan /absolute/path/to/approved-plan.json
./bin/orch next
./bin/orch status
./bin/orch reconcile
./bin/orch recovery inspect
./bin/orch recovery inspect --replacement-home /absolute/path/to/orch-home
./bin/orch state check
./bin/orch state migrations
./bin/orch state retention
./bin/orch state backup
./bin/orch state verify-backup /absolute/path/to/orch-state-....zip
./bin/orch state restore-backup /absolute/path/to/orch-state-....zip \
  --destination /absolute/path/to/new-orch-home
./bin/orch state replace-backup /absolute/path/to/orch-state-....zip \
  --destination /absolute/path/to/existing-standalone-orch-home
./bin/orch state replace-reconcile \
  --destination /absolute/path/to/existing-standalone-orch-home
# while rollback is retained:
./bin/orch state replace-reconcile \
  --destination /absolute/path/to/existing-standalone-orch-home --rollback
# discard retained rollback/forward-copy only after explicit decision:
./bin/orch state replace-reconcile \
  --destination /absolute/path/to/existing-standalone-orch-home --finalize
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

### Publication crash recovery

`publish` writes a durable publication intent before the first Git side effect. If a commit/push result becomes uncertain, do not blindly repeat it:

```sh
orch publish-reconcile --run-id RUN
# only when the observed state reports resume_available=true:
orch publish-reconcile --run-id RUN --resume
```

Reconciliation can prove a staged snapshot, adopt a commit that happened before the journal update, verify an already-pushed remote commit, or return `SAFE_TO_RETRY` only when no Git side effect is observed. Unexpected staging or remote advancement blocks automatic continuation.

## Scheduled dispatcher

The dispatcher template packaged with ORCH is the durable bootstrap. The default dispatcher render command substitutes the user's ORCH home and executable into a reusable standalone Scheduled Task prompt. One task claims one queued attempt, works through RDC, verifies/reviews/publishes it, calls next, and only when READY re-arms itself for one later fresh ChatGPT conversation. When the queue reaches NO_WORK, the task is left disabled.

For multi-project operation, dispatcher render --project PROJECT_ID creates a private prompt permanently scoped to that registered project. Separate native Scheduled ChatGPT tasks can use separate scoped prompts so independent repositories can be worked concurrently; ORCH writer keys still serialize linked worktrees or any projects sharing one Git authority. The original unscoped/global renderer remains the default.

The launcher is intentionally platform-native. The local Python program does not hold or repurpose OpenAI OAuth tokens and does not call a model API.

## Real Codex review evidence

The subscription reviewer path is not only mocked. With account preflight showing ChatGPT auth, purchased credits disabled/balance 0 and the included limit available, a real Codex review was executed against a frozen disposable fixture. The first review correctly blocked because the export omitted its approved check support. ChatGPT repaired `orch/codex_review.py`; a fresh second snapshot received a real Codex `PASS` with exit code 0. The final adapter also places verifier evidence inside the exported workspace and has deterministic test coverage. No external AI or paid API fallback was used.

## Security boundary

The current RDC configuration has broad terminal access. Therefore helper leases and allowlists are a cooperative control, not an OS sandbox against a malicious worker. The MVP is suitable for the disposable fixtures proven here. Connecting `<protected-project>` for writes is **not enabled** and remains <separately-authorized-integration> with a separate approval and stronger repository-specific contract.

## Tests

```sh
cd <orchkit-root>
PYTHONPATH=. python3 -m unittest discover -s tests -v
python3 -m py_compile orch/*.py
```

The current **160-test** deterministic suite covers orchestration/recovery/review, durable cross-plan FIFO ordering, project/workspace writer isolation and lifecycle controls, plan graph validation, capability revocation, protected-file safety blocking, verified deletions, setup profiles, optional review policy, project registration/ledger authority, dirty-byte protection, Git policy, sandboxed/bound Git transport, publication crash reconciliation, transactional schema migration, bounded evidence retention, backup verification/fresh restore, crash-safe state-home replacement/rollback, replacement-aware recovery/artifact census, dispatcher retry guards, dotfile/path-scope safety, and doctor behavior.
