# Workflow Guide

OrchKit coordinates development work whose durable authority must outlive a single chat. It keeps local workflow state in a ledger and starts every task or repair attempt in a fresh ChatGPT conversation.

## Roles

- **Operator** configures local state, registers a project, defines policy, and decides whether a task may publish.
- **ChatGPT worker** performs one claimed task attempt using bounded context.
- **Local verifier** checks the actual workspace and registered commands after the worker relinquishes its capability.
- **Codex reviewer** is optional and runs only when enabled by policy.
- **Publisher** completes or pushes the verified snapshot only after the required gates are met.

## Typical lifecycle

1. Initialize a private local state directory with `orch setup --profile safe`.
2. Register a Git workspace with `orch project add`, then inspect the resulting project policy before adding work.
3. Define a task with explicit allowed paths, checks, dependencies, and any review or owner-approval requirement. The CLI exposes both single-task and batch enqueue paths.
4. A fresh ChatGPT conversation claims one ready task. Durable context comes from OrchKit rather than an earlier conversation transcript.
5. The worker edits only the task's scope, submits its receipt, and quiesces. A submitted receipt is an assertion, not verification.
6. The verifier checks workspace scope, protected content, check authority, registered commands, and the snapshot. It blocks on drift or a failed check.
7. When policy requires it, an optional reviewer and the owner act on the verified snapshot. A changed snapshot must pass the relevant gate again.
8. The task completes locally or proceeds through guarded Git publication. Uncertain Git outcomes are reconciled instead of assumed successful.

Use `orch --help` and the relevant subcommand help for exact arguments. The tool intentionally does not infer a task's allowed paths, verification command, or publication policy from a chat request.

## Snapshot-bound repair claims

After a verified run ends in `NEEDS_FIX`, keep its reviewed bytes in place and
explicitly claim its next attempt from a fresh ChatGPT conversation:

```sh
orch --root /absolute/state/home claim --worker NEW_WORKER \
  --project PROJECT_ID --repair-from PREVIOUS_RUN_ID
```

`--repair-from` selects only the previous run's existing task. Admission requires
its latest run to be terminal `NEEDS_FIX` with passing verification and an intact
SHA-bound snapshot manifest for that run, task, workspace and Git base. The
current bytes, sizes, modes and exact dirty-path set must match that snapshot;
staging must be empty. Branch/base, protected sentinels and registered check
authority must still match, and ambiguous file types, symlinks or hardlinks are
rejected. Divergence blocks admission before a fresh run or capability is issued.

Git workspace claims record the current HEAD and branch when available, even
when publication is `none`, including the default safe profile. Capturing that
identity does not enable committing or pushing. A repair requires the original
claim's recorded identity; older runs that did not capture it remain blocked
with `repair_claim_git_identity_missing`. Ordinary non-Git tasks retain their
existing claim and completion behavior. Ordinary claims also preserve detached
HEAD and unborn-repository behavior; snapshot-bound Git repair requires a usable
HEAD and named branch recorded at the original claim.

The same operation can recover a task marked `BLOCKED` only when its latest
task-specific event is `TASK_BLOCKED_AT_CLAIM` for `claim_workspace_dirty`, with no
subsequent divergent state. This covers an ordinary claim refusing the retained
repair bytes. It is not a general unblock command; ordinary claims still require
a clean workspace.

Admission preserves attempt limits, dependency checks, pauses, writer exclusion,
and checks for live capabilities or pending publication. It creates a new run
bound to the previous run and snapshot, preserving prior receipts, verification
and review evidence. Rejected preflight consumes no attempt. After a new run is
committed, a capability-creation failure retains the normal fail-closed aborted
attempt rather than undoing its recorded history. The new worker may then repair
within the existing allowed paths; submission, quiescence, fresh
verification, review/owner gates and publication still follow the normal
lifecycle. A repair claim does not complete the task, replay prior operations or
resume the previous conversation.

## Readmitting a dependent task onto an accepted base

When a verified repair candidate needs newly accepted infrastructure, an operator
can transfer its durable context into a **new task and clean registered workspace**:

```sh
orch --root /absolute/state/home readmit \
  --previous-run PREVIOUS_RUN_ID --dependency-run DEPENDENCY_RUN_ID \
  --plan /absolute/new-single-task-plan.json \
  --operator-authorization-confirmed --process-inactivity-confirmed
```

The two assertions serve separate purposes. The operator must authorize the
source/base transfer and independently confirm that the source and dependency
actors are inactive. Existing run-bound cooperative quiescence must follow any
active or unknown checkpoint. This remains an operator assertion with a
cooperative helper boundary; it does not fence OS processes. Terminal status,
absent capabilities, elapsed time and authorization alone do not prove inactivity.

The source must be its task's latest terminal `NEEDS_FIX` run with passing
verification, an intact review and canonical snapshot, exact current bytes,
modes, lengths, receipts, sentinels and check authority. A narrowly recorded
ordinary dirty-claim refusal can qualify; arbitrary `BLOCKED` states cannot.
The source checkout is left intact. Its run, receipt, snapshot, review and
immutable task payload are retained.

The dependency must have completed independent verification, passing review and
native Git publication. Its actual commit must have the source snapshot's base
as its sole parent and match the published snapshot's changed paths, bytes and
modes. Retained receipt, verification, review and publication evidence must agree.
Local Git publication is sufficient for this transfer; remote publication of the
new task still follows its own configured gates.

The destination must be clean, with empty staging including intent-to-add, and
HEAD exactly at that dependency commit on its configured branch. Its registered
repository origin must match the source and dependency, and it must share the
dependency's Git common directory. Global/project pauses, active writers, live
capability artifacts and pending publication block admission. Fresh project and
Git checks run within the admission transaction and again before insertion.

The one-task plan may retain or narrow allowed paths and add protected paths or
dependencies. It must retain the original dependencies, sentinels, registered
commands, check deadlines/output bounds/actions, executable identity, original
check-authority paths, review and owner requirements, and publication/registered
project permissions. Authority hashes are rebound only to the accepted tracked
dependency bytes. A risk-based retry must explicitly require review so the new
local attempt number cannot erase its retry review gate.

The new task can use at most the original maximum attempts minus all recorded
runs of the source task. For example, one run from a two-attempt task leaves only
one attempt in the new task. Admission atomically inserts the new immutable
plan/task, cancels the old task and records `TASK_READMITTED` with source,
dependency, evidence, remaining budget and both operator assertions. It neither
claims the new task nor issues a capability. Claim it in a fresh conversation
before applying source edits. Its bounded context includes the original feedback,
run/snapshot and dependency proof from the ledger.

At claim, the recorded `TASK_READMITTED.dependency_commit` must equal the actual
claim HEAD, including when the new task uses `publication.kind: none`. A clean
checkout at a newer commit is refused before a run, writer reservation or
capability is issued, without consuming an attempt. After context preparation,
claim checks the workspace, check authority, HEAD and branch again before run
insertion. HEAD or branch drift during preparation is refused for every Git
publication mode. Retrieved context keeps the stored claim HEAD and durable
dependency proof, so subsequent worker edits do not invalidate that context.

An exact request replay returns the same admission, including after the new task
has been claimed and edited. A different plan digest, dependency or admission
identity conflicts without mutation. This is a specific source/base transfer,
not a generic unblock, lease expiry or permission to overwrite a frozen payload.

## Operator overview

`orch overview` is the normal read-only control view for setup and day-to-day operation. It can be run before the ledger exists and summarizes the configured profile, RDC binding state, registered projects, private-state layout health, and explicit `next_steps`. With `--project`, it adds the existing project-readiness audit summary, including task, active-writer, pending-publication, and pause counts.

Use `orch overview --project <project-id>` for a project-specific readiness view. That form requires a valid project-scoped dispatcher before it reports the project as fully ready for ordinary ChatGPT/RDC work. When queued work exists, the overview deliberately points to `orch queue list --project <project-id>` for the authoritative READY/dependency/writer decision instead of duplicating queue-state logic.

The overview path is intentionally inspection-only. It does not initialize a missing ledger, recreate missing runtime directories, repair file modes, or migrate an older SQLite schema. Unsafe or outdated state is reported as `BLOCKED`; use the dedicated state/recovery commands to resolve it. Capability-file contents are never part of the overview.

For the subscription-free core path, run `orch doctor --skip-codex` and register the project with `--review-mode off`. In that mode, the optional Codex reviewer check is reported as skipped rather than keeping an otherwise ready setup in an attention state.

## Local state and project workspaces

The OrchKit state directory contains the ledger and private operational artifacts. Keep it separate from registered repositories. A registered workspace is where a task's allowed edits occur; it is not the source of authority for OrchKit's ledger or policy.

The current workflow uses a connected ChatGPT/RDC bridge for its automated worker path. The connection is an operational prerequisite, not evidence of an OS security boundary.

## Inspecting and reconciling local patch evidence

Source builds with the local intent-journal package can inspect one recorded
operation without opening a worker session:

```sh
orch --root /absolute/state/home bridge operation inspect \
  --run-id RUN_ID --operation-id OPERATION_ID
```

`RECORDED` means that validated intent metadata was observed. `ABSENT` means
that the requested record was absent beneath valid inspected anchors; `UNKNOWN`
means that unsafe, incomplete or unreadable evidence prevents a conclusion.
The command returns no payload or capability and does not create directories,
repair permissions, release a writer, apply a patch or recommend replay.

Trusted local preparation requires current run authority and original-content
preflight for a new ID. Matching repeated IDs return existing intent metadata;
changed requests conflict. Independent content observations distinguish the
original, replacement, other content and unavailable observations without
claiming who wrote a file or whether an operation completed. This API is not a
connected Bridge worker route.

`orch recovery inspect` also surfaces terminal/orphaned or invalid intents.
Journal evidence causes an attention state until its meaning is reconciled; a
fresh chat or stale heartbeat does not resolve it. Backup/verification/restore
preserve these records, refuse incomplete coverage, and retain the prohibition
on backing up active runs. Ordinary evidence pruning does not delete intents.
The journal persists metadata only; it cannot reconstruct replacement bytes.

Source builds also expose separate execution inspection and explicit local
reconciliation:

```sh
orch --root /absolute/state/home bridge execution inspect \
  --run-id RUN_ID --operation-id OPERATION_ID
orch --root /absolute/state/home bridge execution reconcile \
  --run-id RUN_ID --operation-id OPERATION_ID --cap /absolute/run/capability
```

Execution inspection validates the append-once `dispatch`, `publication` and
`result` chain against its intent. `RECORDED` includes whether a terminal result
exists; an existing `APPLIED` or `NOT_APPLIED` receipt is historical evidence,
not a claim about current bytes. Terminal presence describes validated record
structure; it does not acknowledge durability for a service reply or run
closure. Inspection creates no state, opens no session and performs no fsync,
cleanup or content-based recovery.

Reconciliation takes its request only from validated stored evidence and opens
a fresh session with current `RUNNING` run/capability authority. It never
replays replacement bytes, dispatches, stages or renames a patch. Exact recorded
inode/content/metadata observations may close the ID as `APPLIED` or
`NOT_APPLIED`; ambiguity stays `UNKNOWN`. A scratch file without proven recorded
ownership is preserved. Every terminal ID stays closed, including `NOT_APPLIED`.
Keep capability contents private; supply only the file path.

Trusted local `LocalBoundedFilePatcher.apply(request, replacement)` supplies a
complete replacement of at most 65,536 bytes for one existing, allowed regular
file whose original is also bounded. The target must be current-user owned and
single-link; native macOS checks preserve ordinary uid/gid/mode and bounded
xattrs, while unsupported metadata is refused. Apply, reconciliation and core
submit/quiesce/abort use the same private runtime directory descriptor lock.
Unresolved or unsafe execution evidence blocks those transitions and preserves
writer/capability authority. Historical service replies and closure of runs with
terminal execution evidence require renewed acknowledgement under that lock:
fsync every existing phase record, its linked intent and all pinned directories
through home, then independently reread exact bytes and identities. A failed
renewal stays `UNKNOWN` for the service and refuses core closure. The records
are preserved, and later workspace edits remain untouched. Backups preserve
exact execution evidence and its intent linkage; ordinary retention does not
prune it or patch scratch files.

There is no replacement-byte CLI, remote patch endpoint or accepted ordinary
ChatGPT Bridge route. Cooperative locks and descriptor checks do not establish
OS compare-and-swap, same-user isolation or power-loss guarantees. Native byte
verification and Git publication remain independent gates; fault fixtures alone
do not prove real power-loss recovery.

## Bounded execution and current limits

The current CLI admits and displays a versioned bounded-execution contract, but its time/context budget remains advisory and does not enforce worker suspension or termination.

- A bounded task records one clear goal, explicit allowed paths, acceptance criteria, non-goals, registered checks, and recovery metadata. Generated tasks receive conservative defaults, while admitted legacy plans remain compatible.
- `orch queue list` exposes a bounded operator-facing summary before claim: goal, acceptance, non-goals, allowed paths, advisory execution budget, and each check's timeout, output-tail bound, and timeout action. Executable bindings and check-authority evidence are not included in that summary.
- Context or usage telemetry must be attributed to the active worker with a source and observation time when available; missing telemetry remains `UNKNOWN`.
- A timeout or context threshold never expires a writer reservation or proves an external/direct-RDC process has stopped.
- Project/global pause prevents new claims; it does not suspend the current writer. A `RUNNING` worker can persist an `orch checkpoint` without releasing its writer reservation or capability. Recovery reports checkpointed active/unknown process state and remains blocked while external/direct-RDC activity is uncertain. For checkpointed work, abort/retry requires independent process-inactivity confirmation via `--process-inactivity-confirmed`; this is an operator assertion, not OS/process fencing. Quiesce remains cooperative and requires `RESULT_SUBMITTED`. Automatic process fencing and transparent cross-conversation resume of the same `RUNNING` attempt are not implemented.
- `orch route record/show` can persist bounded, secret-free observations for a real ledger run after RDC binding. The evidence is per-run, device-bound and write-once; unavailable model/reasoning/usage telemetry may remain `UNKNOWN`, and the record explicitly does not evaluate acceptance. A disposable fresh ordinary-Chat/RDC task has now completed the separate end-to-end acceptance gate with review off, registered verification, and local Git publication. The route record itself remains `acceptance=NOT_EVALUATED` and does not self-certify future runs.
- The core workflow must not silently substitute ChatGPT Work, Codex execution, paid model APIs, or external providers for the intended worker path.

See the [roadmap](../ROADMAP.md) for the ordered P0 milestones.

The trusted local owned-process source API is a separate bounded executor.
Operator profiles freeze executable/arguments, project-root cwd, sanitized
environment, output budget and deadline; worker requests use profile IDs and
opaque run/session handles. Its supervisor drains bounded output and enforces
deadlines without polling. A signal is not completion: only retained direct-child
reaping and settled output produce linked exit evidence. Dispatch and exit
metadata live in SQLite events; output and live handles are volatile.

Ordinary polling cannot consume an admitted child's reserved terminate and
proven-terminal readback slots. The 64-entry outcome cache counts these safety
reservations when admitting new work. A capacity refusal or pending terminal
poll is not exit proof; pending/invalid observations do not spend the final slot.

Unresolved dispatches block submit, quiesce and abort. An operator inactivity
assertion does not waive owned-process evidence. Restart never replays spawn or
signals a saved PID. Native closure validates the exact exit chain and commits
its acknowledgement with the authority transition using FULL synchronous
durability. Backup preserves process metadata, not live ownership or output.
Trusted profiles must avoid descendants and secrets; this is no OS sandbox or
ordinary-Chat Bridge acceptance.

## Review and publication

Review can be disabled, risk-based, or required by policy. Codex is an optional reviewer and must not be used as a prerequisite for the core workflow.

The native review adapter locates the legacy ChatGPT app executable or the
current `codex-cli/CodexCLI.app/Contents/MacOS/codex` bundle. It does not search
arbitrary PATH executables, change the installation, or create compatibility
symlinks. Missing, non-executable, writable or symlink leaves fail closed.
Use explicit reviewer routing; the adapter never infers the reviewer model from a default:

```bash
orch --root /absolute/state/home codex-preflight \
  --model gpt-6-astra --reasoning-effort high
orch --root /absolute/state/home codex-review --run-id RUN_ID \
  --model gpt-6-astra --reasoning-effort high --execute
```

Preflight checks the signed-in ChatGPT subscription, known rate/credit evidence
and the available model/effort catalog. Missing evidence, purchased credits,
provider environment overrides or unsupported routing block execution. Diagnostic
output contains the decision and binary/routing identity, not usage percentages,
credit balances or account identifiers. Preflight runs again immediately before
each review; a prior successful check is not a lasting billing permission.

Review retains frozen export/schema/report bindings and uses `read-only`,
`--ignore-user-config`, disabled hooks/plugins, explicit OpenAI routing and no
interactive approval escalation. The returned route describes explicit command
selection and catalog evidence; it is not self-attestation of a hidden backend
model identity or ordinary ChatGPT route acceptance. Same-user changes after the
last check remain outside the cooperative boundary. Required review/owner gates
still apply. See [review-adapter-recovery-plan.md](review-adapter-recovery-plan.md)
for observed compatibility, rollout and remaining release gates.

When publication is allowed, OrchKit guards the expected branch and base, requires empty pre-existing staging, stages only verified changed paths, uses ordinary non-force Git behavior, and confirms the remote reference. These checks improve workflow integrity; they do not protect against a process that already has broad same-user access.

Read the [architecture](architecture.md) for component relationships and the [security model](security-model.md) before applying OrchKit to sensitive work.
