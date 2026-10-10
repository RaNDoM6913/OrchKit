# Multi-Project Bridge Roadmap

This document defines the post-0.11.0 direction for transport independence and practical multi-project concurrency.

The immediate product target is intentionally small: one operator, one Mac, and two or three independent projects worked on concurrently from separate ChatGPT conversations.

It is not a commitment to multi-user SaaS, large fleets, or concurrent writers inside one Git repository in the first implementation.

## Product outcome

A user should be able to connect OrchKit once, open separate ChatGPT conversations, bind each conversation to a different registered project, and let those workers proceed concurrently without sharing write authority.

Example:

```text
Chat A -> Project A -> Run A -> Writer A
Chat B -> Project B -> Run B -> Writer B
Chat C -> Project C -> Run C -> Writer C
                  |
             one Mac device
                  |
            one OrchKit Bridge
```

The initial supported concurrency target is three active worker sessions on one device.

## Existing foundation

The current core already provides most of the scheduling safety required for this target:
- multiple registered projects can share one ORCH_HOME;
- tasks carry a project_id and a derived writer_key;
- project-scoped claim can select work independently;
- independent workspaces can hold active runs concurrently;
- linked worktrees from the same Git common directory deliberately share a writer_key;
- one writer per writer_key is already enforced;
- project pause, queue state, verification, and publication state are durable.

Therefore P1 should not replace the ledger or writer-lock design. It should expose that design through a multi-session transport.

## Non-goals for the first usable version

The first multi-project release will not:

- run two writers concurrently inside the same Git common directory;
- automatically merge conflicting task branches;
- support multiple human accounts or organizations;
- schedule work across a device fleet;
- provide general remote-desktop or screen-control functionality;
- expose an unrestricted shell as the primary MCP interface;
- weaken current verification, publication, or recovery gates.

## Core authority model

The durable hierarchy is:

```text
operator
  -> device
     -> project
        -> writer_key
           -> task
              -> run / attempt
                 -> worker_session
                    -> operation
```

A transport session never becomes authority by itself.

Every mutating operation must be resolved locally from worker_session -> run -> task -> project -> writer_key -> capability/policy.

## Safety invariants

1. At most one active writer exists for a writer_key.
2. Different writer_key values may execute concurrently.
3. A claimed worker session is permanently bound to one run attempt.
4. A worker cannot change its project binding after claim.
5. The local Bridge validates every path and mutation independently of the relay.
6. Capability-file contents are never returned to ChatGPT or the relay.
7. Unknown process or publication outcomes remain UNKNOWN/BLOCKED.
8. Mutating requests are idempotent and have durable operation IDs.
9. Force push, reset, clean, and stash remain denied unless a future explicit policy changes them.
10. RDC remains available until the native Bridge passes equivalent acceptance tests.

## Session model

A session record should contain only bounded, non-secret routing metadata:

- session_id;
- worker_id;
- run_id;
- task_id;
- project_id;
- writer_key;
- device_id;
- created_at;
- last_seen_at;
- state;
- transport;
- protocol_version.
The session ID is issued by OrchKit. It must not depend on whether ChatGPT exposes a stable conversation identifier.

This makes one connector usable from several chats: each chat receives a different OrchKit worker_session_id and supplies it on subsequent tool calls.

## Device capacity

The first Bridge should protect the user's Mac from accidental overload.

Suggested initial defaults:

- max_parallel_workers = 3;
- max_parallel_processes = 6;
- max_processes_per_worker = 2;
- configurable per device;
- no automatic oversubscription.

Capacity limits are operational limits, not writer authority. A free capacity slot cannot bypass writer_key locking.

## P1-A — concurrency proof on the existing core

Before changing transport architecture, create a disposable acceptance fixture with three independent Git repositories and one disposable ORCH_HOME.

Queue one bounded task in each project.

Acceptance criteria:

- all three projects are registered independently;
- all three tasks have distinct writer_key values;
- three claims can coexist;
- active writer count becomes three;
- each claim is permanently project-scoped;
- pausing Project B does not interrupt A or C;
- verification state is independent;
- completion/publication state is independent;
- attempts cannot read or mutate another project's allowed workspace through OrchKit;
- a linked-worktree negative fixture still serializes on the shared writer_key.

This proves the scheduler before introducing a new network layer.

## P1-B — remove RDC from core semantics

**Historical P1-B1 inventory baseline.** The caller/source references in
this section describe `8dd8c6da7ac6e2f46e5de93af46802df6fe0933f`
(after P1-A3 and the registered-check 600-second prerequisite). P1-B2 and
P1-B3 subsequently extracted pure v1 validation and route-evidence storage;
P1-B4 adds only an in-memory operation contract. None of these packages
introduces a Python executor for ChatGPT's external RDC calls, a Bridge,
or native device/session authorization. The existing RDC route remains in use.

### Current operation and authority sequence (implemented)

1. An operator connects the actual RDC device outside OrchKit. `orch rdc record`
   persists that observed device's ID/name; `rdc show` reads the local marker.
   The marker is not live connector authentication or independent proof that the
   device is still online (`orch/dispatcher.py:53-85`).
2. Register a Git project and inspect its identity and policy. The registry
   derives `writer_key` from the canonical Git common directory, so linked
   worktrees serialize (`orch/project.py:193-236`). A project-scoped rendered
   prompt includes explicit `--project` and a scoped worker ID
   (`orch/dispatcher.py:246-301`; `orch/templates/dispatcher_prompt.txt`).
3. `overview --project`, `project audit`, `recovery inspect`, and `queue list`
   report readiness, dependency, pause, writer and publication state. A prompt
   rendering or RDC marker by itself cannot claim an attempt.
4. In a **fresh ordinary ChatGPT conversation**, the worker invokes the local
   `orch claim --project ...` via RDC. The ledger atomically checks queue,
   project pause, dependencies, max attempts, writer reservation, branch, HEAD
   and workspace preflight, then returns a specific `run_id`,
   `capability_file`, `receipt_file` and bounded context
   (`orch/core.py:1325-1538`). The external ChatGPT/RDC session performs
   scoped file edits and process interactions; no Python `WorkerTransport` or
   `FileOperations` adapter executes those RDC tool calls today.
5. The worker writes a receipt at its returned private path, then calls
   `submit --cap ... --receipt ...` and `quiesce --cap ...`. Receipt identity,
   changed-path allowlist and lease are checked. `RESULT_SUBMITTED` is not
   success. `quiesce` revokes the capability and advances to `VERIFYING`,
   **cooperatively**, without fencing a residual direct RDC shell
   (`orch/core.py:1601-1627,1726-1788`).
6. The local verifier independently checks actual Git/workspace changes,
   protected bytes, Git base, frozen executable/check authority, snapshot and
   registered checks (`orch/core.py:1899-2317`). The local registered check is
   `subprocess.run` with bounded output and deadline
   (`orch/core.py:2095-2140`); **it is not worker transport**. Current admitted
   check timeout maximum is 600 seconds; a timed-out command cannot prove an
   unrelated external RDC process has stopped.
7. `NEEDS_FIX` requires durable feedback and a fresh attempt. `VERIFIED` may
   require review/owner approval; with review off and no required owner
   acceptance it can proceed to `publish`. Publication checks exact branch,
   base, staged scope and verified bytes, then uses local Git and a separate,
   restricted Git network transport with remote-ref readback
   (`orch/core.py:2760-3000`; `orch/git_transport.py:30-196`).
   **Git publication transport is not ChatGPT worker transport.** Unknown
   publication outcomes require `publish-reconcile`, not a blind retry.
8. Checkpoint/recovery preserves the writer on active/unknown work. Pauses
   prevent new claims, not OS processes; elapsed heartbeat/lease time is not
   fencing (`orch/core.py:1661-1724,3206-3240,3358-3421`;
   `orch/state.py:197-427`). This is cooperative orchestration, not a sandbox.

### Current RDC-dependent surface and real callers (implemented)

All references below are source locations, **not** proposed module names.
`read` means a source operation's intent; explicit permission-mode side
effects are distinguished in the compatibility section.

| Surface and callers | Inputs / output and authority | Mutation, failure and recovery |
| --- | --- | --- |
| `orch/cli.py:62-94,321-360` — `rdc record/show/bootstrap-prompt`, `route record/show`, `dispatcher render` | Explicit `--root`; device ID/name supplied from external RDC discovery. Route record requires a **real ledger run lookup** before passing exact `run_id`/`task_id`. JSON results; no connector session is created by this CLI. | Record/render write private files; show may repair file mode; missing/invalid marker or unknown run does not authorize a route. |
| `orch/dispatcher.py:25-85` — `validate_rdc_marker`, `record_rdc`, `read_rdc` | v1 `rdc-bootstrap.json` inside ORCH home; device ID/name, timestamp, fixed source. Callers: CLI, `run_doctor`, route recorder/reader, `render_dispatcher`. | Atomic **replaceable** marker, not append-only or a live identity challenge. Missing -> `UNVERIFIED`; unsafe/oversized/invalid -> error; `read_rdc` explicitly repairs mode to `0600`. |
| `orch/dispatcher.py:88-233` — `validate_route_evidence`, `record_route_evidence`, `read_route_evidence` | Fixed v1 `ordinary_chat`/`rdc` observation per run. Callers: CLI route commands and dispatcher tests. Device ID/name are copied from recorded marker; no ChatGPT conversation ID is required or proven. | New private `route-evidence/<run_id>.json` **once** via exclusive create; duplicate -> error. Historical read remains available; if current marker differs, `STALE_DEVICE_BINDING` and `acceptance=NOT_EVALUATED`, not a new assertion of authority. |
| `orch/dispatcher.py:236-311` and `orch/templates/dispatcher_prompt.txt` — renderer/bootstrap | Optional project ID validated against read-only registry; `ORCH_EXECUTABLE` override, installed executable or Python fallback; returns target/scope/worker ID. Template tells the external worker to claim and use exact ledger context. | Writes `0600` prompt; rendering does not start ChatGPT or RDC, grant capability, or prove task acceptance. Scheduled rearming text is a template, **not** permission to schedule work. |
| `orch/overview.py:148-175,231-493` — `_read_rdc`, `operator_overview` | Reads marker with v1 validator; also state layout and project audit. CLI `overview` consumer. | **Inspection only**: missing -> `UNVERIFIED`; unsafe, wrong mode, or invalid -> `BLOCKED` without chmod, state migration or repair. |
| `orch/readiness.py:473-948` — `audit_project`, RDC and scoped-dispatcher checks at 786-914 | Reads same marker and a bounded scoped dispatcher; validates registry, writer identity, check authority, ledger health and pauses. Called by CLI project audit/plan/enqueue and overview. | **Inspection only**: missing marker -> `ATTENTION`; unsafe/schema/mode mismatch -> `BLOCKED`; valid `0600` marker -> `PASS`. Project dispatcher missing is `ATTENTION` when required, otherwise optional. |
| `orch/doctor.py:17-73` — `run_doctor` | Uses `read_rdc` for RDC check; checks Python, Git, home and optional Codex setup. CLI `doctor` caller. | Unlike overview, initializes/checks private home via `ensure_home`; read_rdc may chmod. Reports `RECORDED`, `UNVERIFIED` or `BLOCKED` check, not live RDC availability. |
| `orch/cli.py:196-227,671-721`, `orch/core.py:1325-1788,2192-2317` | Project-scoped `claim`, bounded context, capability path, receipt, submit/quiesce/verify. This is ledger authority invoked **through** RDC, not implemented by RDC. | Mutating lifecycle; fail-closed identity, path and writer checks; `NEEDS_FIX`/recovery when checks or observations fail. |
| `orch/core.py:2095-2140`, `orch/readiness.py:207-345` | Admitted `argv`, `cwd`, frozen executable/digest, timeout/output limit/action. Registry/check-authority inspection and local verifier. | Local subprocess, **not** external RDC execution. Failed/timeout checks return `NEEDS_FIX`; drift blocks. A full suite is not proven by a single-case test. |
| `orch/project.py:193-236`, `orch/git_policy.py`, `orch/core.py:2602-3000`, `orch/git_transport.py` | Git identity, policy, exact verified snapshot and publication URL; isolated, restricted Git transport (`file`, HTTPS, SSH where permitted). | Separate publisher: non-force commit/push, remote-ref check, durable reconcile on unknown outcome; not a worker adapter. |
| `orch/state.py:197-427` and `orch/core.py:1661-1724,3358-3421` | Ledger run ID, checkpoint and process-state claims; external worker/process status may be unknown. | Readback is not process fencing. Unknown activity remains blocked; explicit independent inactivity confirmation is an operator assertion, not automatic cleanup. |

The only code paths with **RDC-specific format literals** are the CLI,
dispatcher, overview/readiness, doctor and their tests. Source Git and local
check subprocess calls are *adjacent execution/publication surfaces*, not
implementations or hidden callers of a Python RDC tool client. External
`Remote Desktop Commander` `read_file` / terminal / process-polling /
write/edit calls happen in the ChatGPT tool layer outside this repository.
There is no current project-scoped native worker file/process API, broker,
Bridge, relay, device online attestation, or generic operation journal.

### On-disk v1 compatibility and fail-closed behavior (implemented)

- **Marker v1:** `<home>/rdc-bootstrap.json` JSON object with exactly allowed
  keys `schema_version=1` (integer, not bool), `device_id`,
  `device_name`, `recorded_at`, `source="chatgpt_rdc_bootstrap"`.
  IDs/name use nonblank UTF-8 strings at most 512 bytes each; timestamp at
  most 128 bytes. Newlines/CR/NUL are rejected. A read is bounded to 64 KiB,
  no-follow regular file, with schema/unknown-key rejection
  (`orch/dispatcher.py:19-85`, `orch/config.py:37-110`).
  `record_rdc` is an atomic `0600` replacement, allowing explicit
  **rebinding**; it is not write-once. It must never be mistaken for an online
  device-authentication result.
- **Route evidence v1:** `<home>/route-evidence/<run_id>.json`, 64 KiB
  maximum, created `0600` with exclusive no-follow open. Allowed fields:
  `schema_version=1`, `run_id`, `task_id` (each ASCII
  `[A-Za-z0-9._-]{1,200}`), fixed `surface="ordinary_chat"` and
  `transport="rdc"`, `device_id`, `device_name`, `model`,
  `reasoning`, `usage`, `observed_at`, `source`, and the four
  `*_used` observations: `work_used`, `codex_execution_used`,
  `model_api_used`, `external_provider_used`. Their values are
  `yes|no|unknown`; `UNKNOWN` strings in telemetry are allowed and
  must never be upgraded into fabricated observations. Validator rejects
  unknown keys, unknown version, transport and surface
  (`orch/dispatcher.py:88-233`). `route record` checks the ledger run
  before calling the recorder (`orch/cli.py:330-355`); direct function
  calls do not themselves check run existence.
- **Write/read distinction:** recording a route is write-once per run:
  `route_evidence_already_recorded` must never trigger a destructive
  overwrite. Missing record -> `UNVERIFIED`; mismatched stored run ID ->
  error. Reading an old route after a marker rebind preserves its bytes,
  returns `STALE_DEVICE_BINDING` and
  `rdc_binding_matches=false`; it never rewrites history.
  Both `read_rdc` and `read_route_evidence` pass
  `repair_mode=0o600` to the bounded reader; that is a
  **permission-changing read** via `fchmod`. In contrast,
  `overview._read_rdc`, `audit_project` and `state_permission_findings`
  inspect and report unsafe modes without repair. Symlinks, nonregular
  files, size violations and malformed records must fail closed.
- **Evidence is observation, not admission:** a successfully recorded v1
  route always returns `acceptance=NOT_EVALUATED`; even a binding match
  neither certifies current ChatGPT UI/tool settings nor unlocks writer,
  verification, review, approval or Git publication gates. Unknown transport
  values fail validation in the **current** v1 schema; an unverified or
  stale device binding cannot authorize work. V1 only checks nonblank device
  ID/name strings (a literal `UNKNOWN` is not cryptographically rejected),
  so callers must not promote them to attested identity. Future versions must
  choose an explicit version-aware policy and keep v1 historical reads.
  No automatic conversion of v1 to an imagined v2 is implemented.
- **Trust and recovery:** a marker can be re-recorded by a same-user process;
  the evidence is not a cryptographic signature. A lost response to a
  write-once recording requires `route show` readback before retry, and a
  process/tool timeout requires independent process-status readback before
  release or re-execution. Neither a changed marker nor an adapter may
  silently transfer a claimed run to a different device or project.

### Proposed internal interfaces (NOT implemented in P1-B1)

| Proposed boundary | Contract; authoritative side |
| --- | --- |
| `DeviceIdentityProvider` | Return device identity and freshness/UNKNOWN separately from saved marker. Match actual device against the bound run/workspace; deny silent rebind. A saved marker alone never authenticates a worker. |
| `RouteEvidenceStore` and `RouteEvidenceCodec` | Read historical v1 exactly; write-once v1 while active, with explicit version dispatch for future observations. Keep `NOT_EVALUATED` and unknowns. Storage layer must not become admission authority. |
| `WorkerTransport` / `WorkerFileOperations` | Future run-bound relative-path read/list/patch with local path canonicalization, allowlist, identity checks and bounded output; current RDC calls remain *external ChatGPT actions*, not an instantiated Python adapter. |
| `WorkerProcessOperations` | Future run-bound start/readback/terminate with owned PIDs, deadlines, cursor/operation IDs and an explicit UNKNOWN when process liveness cannot be proven. The current direct RDC shell must not be inferred to have this contract. |
| `RegisteredCheckExecutor` | Preserve the existing **local** `_run_check` execution/authority/result semantics; do not route verifier tests through an external worker connector. |
| `PublicationGitTransport` | Keep restricted Git publication and compare-and-readback outside the worker transport. Existing `git_transport.py` is a publication helper, not a candidate RDC adapter. |
| `OperationJournal` | Future durable idempotency for remote mutations only, with unknown-outcome readback. The existing publication journal is narrower and must not be called a generic worker operation journal. |

The conceptual worker binding is `device -> project -> writer_key -> task
-> run/attempt -> worker_session -> operation`. Current durable authority
covers project/writer/task/run/capability, **not** a native
`worker_session`. An eventual session ID or transport token is *never*
sufficient authorization without local run/ledger/capability validation.
These interfaces must not relocate or weaken those decisions.

### Existing coverage and required future contract checks

- **Existing:** `tests/test_dispatcher.py` covers real-run CLI lookup,
  marker prerequisite, write-once route bytes, stale-device readback and
  `NOT_EVALUATED`, plus scoped prompt/unsafe-registry cases.
  `tests/test_productization.py` checks oversized/no-follow marker,
  doctor and read-only overview, publication/Git safety and dispatcher
  guards. `tests/test_readiness.py` covers marker and dispatcher bounds,
  unsafe identity, read-only audit and immutable state layout.
  `tests/test_multi_project_acceptance.py` tests three distinct writer
  keys, linked-worktree serialization, project pause, independent outcomes,
  capability/receipt cross-attempt rejection and symlink scope negatives.
  `tests/test_check_timeout.py` pins timeout 600/defaults/failure behavior.
  This fixture is **not** evidence of genuine simultaneous ChatGPT chats.
- **Add as small contract tests (positive/negative):** v1 marker and evidence
  round-trip by schema/version with exact bytes; historical v1 read
  unchanged after rebind; reject unknown schema, transport, surface, keys,
  malformed/overlong identity, bad tri-state observation, wrong run ID,
  mismatched current device, duplicate write and symlink/nonregular/
  oversize/permission-unsafe files. Assert that inspection does not chmod
  while explicit `read_rdc` may repair `0600`.
- **Authority negatives:** refuse unregistered run, foreign/stale run
  capability, changed Git common-dir writer binding, changed project ID,
  dirty/staged/out-of-scope/escaped paths, unknown external process,
  stale snapshot, executable/check drift, or uncertain publication result.
  Tests for new transport must prove these denials without using a
  generated prompt or `NOT_EVALUATED` record as permission.
- **Separation checks:** inject a fake *future* worker file/process adapter
  to establish bounded errors/unknown outcomes without invoking the local
  verifier's subprocess or the Git publisher. Preserve exact registered
  test argv/cwd/executable binding, 600-second ceiling, timeout action and
  bounded output; test published-ref readback independently.
  Avoid a real network, production credentials, Codex or another provider.
- **Verification discipline:** run targeted compatibility tests and the
  complete admitted regression suite through OrchKit verification. Check
  exact changed paths and Markdown links; a subset or clean HEAD without a
  clean index/worktree cannot satisfy the full acceptance gate.

### P1-B bounded packages and delivered boundaries

1. **P1-B1 — completed (PR #39):** source-grounded inventory and historical
   v1 schema/permission/error contracts, without implementation changes.
2. **P1-B2 — completed (PR #40):** `orch/transport_contracts.py` contains
   pure marker and route-evidence v1 validators. Dispatcher compatibility
   exports, old error codes and schema bytes remain unchanged.
3. **P1-B3 — completed (PR #41):** `orch/route_evidence_store.py` contains
   version-aware v1 route storage and codec with historical exclusive
   write-once, no-follow reads, mode repair, rebinding and
   `acceptance=NOT_EVALUATED` preserved.
4. **P1-B4 — completed, PR #42:** `orch/worker_operations.py`
   defines in-memory v1 run identity, operation/outcome metadata and
   structural file/process adapter protocols. The **only adapters** are
   fake objects in `tests/test_worker_operations.py`. This is neither
   an active execution endpoint nor a new native worker session.

### P1-B4 contract semantics and explicit non-authority

- **Metadata:** `WorkerRunBinding` contains project, writer key, task,
  run and attempt. `WorkerOperationRequest` adds a bounded operation ID,
  an exact allowed kind, a lexical target and an output-byte ceiling.
  An operation ID is not a durable idempotency journal or permission token.
- **Six kinds:** `file.read`, `file.list`, `file.patch`, `process.start`,
  `process.poll`, `process.terminate`. File targets are syntactically
  relative; no filesystem symlink or allowlist check is performed here.
  Process-start targets are symbolic future *registered profile identifiers*,
  not executable strings; poll/terminate targets are opaque handles. No
  process is started, polled or terminated by this module.
- **Binding checks:** `require_matching_worker_run` compares the presented
  binding with separately supplied expected values and denies mismatches.
  Callers must independently obtain authoritative identity from the ledger,
  check active capability, device trust and run/session authority, and enforce
  canonical paths, process ownership and resource limits before *any* I/O.
  A forged expected binding, route evidence or saved RDC marker cannot
  supply authority. These authorizing mechanisms remain P1-C work.
- **Outcome checks:** `SUCCEEDED`, `REFUSED`, `FAILED`, `UNKNOWN`
  are distinct and validated; results correlate to the operation ID and
  declared output bound. An `UNKNOWN` result requires independent
  readback and must **not** become success, proven failure or an automatic
  replay of a mutation. Durable retry/operation reconciliation is **not**
  implemented by this pure module.
- **Separation:** no production file/process adapter, RDC client, Bridge,
  background daemon, relay, broker or MCP server is introduced.
  `orch/core.py` still executes registered checks locally via bounded
  `subprocess.run`; guarded Git publication remains in its original
  publisher and `orch/git_transport.py`. Neither is routed through this
  worker protocol. Existing writer, capability, recovery, review/approval,
  evidence and publisher gates do not move.
- **Tests:** fake adapters exercise the six methods, strict schema and
  identifiers, cross-project/writer/task/run/attempt denials, unsafe target
  syntax, bounded output, explicit failure/refusal, unknown/lost-response
  readback and non-import of subprocess/Git publisher surfaces; the full
  registered regression and exact PR/main CI remain mandatory.

The first **P1-C1** package adds only local in-process ledger/capability
session preflight; this is still not a native Bridge transport. **P1-C2**
addresses canonical path allowlists and symlink/escape protection before
file I/O. Owned processes, durable operation IDs, timeout/reconnect and
restart semantics remain separate later gates. P1-D through P1-G and
multi-device/hosted extensions remain separately gated.

## P1-C — local OrchKit Bridge MVP

### P1-C1 — in-process run/session preflight (implemented boundary)

- `orch/bridge_authorization.py` exposes `LocalRunSessionAuthority` to a
  **trusted local caller**; there is no endpoint, daemon, protocol server or
  permission to perform an operation. `open_session` generates a volatile
  random local routing ID only after validating a `WorkerRunBinding` against
  an active `RUNNING` ledger claim, `IN_PROGRESS` task and the existing
  bounded no-follow `0600` capability file with exact lease match.
- `require_session` rechecks capability and ledger on **each** call. The
  current project registry, workspace identity, Git common-dir writer key,
  task publication branch and claimed HEAD must match independently.
  Foreign project/writer/task/run/attempt, foreign/revoked/symlink/mis-moded
  capability, changed branch/HEAD, closed/unknown session and stale attempt
  are refused. Global/project pause still only prevents new claims.
- The same run cannot open two sessions in one authority instance; close
  deletes its mapping, and a new instance does **not** inherit old sessions.
  This is not a durable or cross-process broker; a same-user process that can
  read the live capability is not OS-isolated. A session ID alone is never
  a bearer credential. Checks are **point-in-time**, not OS/process fencing.
- No `WorkerOperationRequest` or `WorkerOperationOutcome` is dispatched by
  this preflight. **No file/path or process operation is authorized yet**;
  even `file.read` requires separate future canonical scope enforcement,
  symlink-resistant descriptor I/O and active revalidation. P1-B4 route
  evidence, markers and syntactic request checks remain non-authorizing.
  The independent local registered-check executor and guarded Git publisher
  are untouched. Tests use disposable Git/ledger/capability fixtures only.

### P1-C2 — bounded descriptor-based file scope (first foundation)

- `LocalFileScope` operates only for a trusted local caller with a live P1-C1
  session, active capability and matching ledger/project/writer authority. Its
  `file.read` scope is sourced from the task's existing `allowed_paths`, not a
  second ACL or a supplied expected run. Only exact allowed **existing regular
  files** currently receive a descriptor. `file.list`, `file.patch`, process
  operations and file-content transfer remain unsupported.
- The registered canonical project root is pinned by a no-follow directory fd
  (device/inode). Each directory component is opened relative to an owned fd
  using `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`; the leaf uses read-only,
  nonblocking, no-follow flags and must be regular. Identity checks compare
  the opened fds with no-follow directory-relative entries and independently
  reopen/check the registered root. OS primitives unavailable or ineffective
  for this contract must fail closed; lexical `resolve()` or prefix tests are
  not permission grants.
- All parents and the target must already exist; neither missing parents nor
  missing leaves are created. Symlink parents/leaves, traversal, absolute
  requests, wrong task allowlist, unknown/stale session/capability and root
  substitution are refused. Deterministic injected swaps test path changes
  between checks and opens. The scope owns and closes its root descriptor;
  the caller borrows the opened file fd only within a context, including
  automatic cleanup on exceptions. Never cache a checked pathname for later I/O.
- **Residual limitations:** same-user malicious processes are not fenced;
  root identity is pinned only when a local scope instance is created, not
  durably attested at project registration. Hard links and adversarial races
  after the last recheck are not an OS sandbox guarantee. An already-open fd
  cannot be revoked retroactively by a ledger transition. No safe concurrent
  use of one scope instance across threads is claimed. At this foundation
  stage bounded reads were not included; a separate local read package follows
  below. List/patch, writable file semantics, process ownership, durable
  uncertain-outcome reconciliation and device trust remain future work.

### P1-C2 — bounded in-process file.read content (second package)

- `orch/bridge_file_read.py` offers `LocalBoundedFileReader` to a **trusted
  local caller** already holding a live `LocalFileScope`. Only `file.read`
  requests are supported. The existing P1-C1 session/capability/ledger and
  P1-C2 no-follow descriptor walk determine access; there is no second ACL.
- A version-1 `WorkerOperationOutcome` returns bytes only after bounded EOF,
  metadata comparison before/after reading and an independent second scoped
  descriptor open/identity check just before returning. Reads are capped by
  `max_output_bytes` (up to 64 KiB), plus one excess-byte sentinel; an
  oversized, growing, changed or newly unauthorized file produces no partial
  bytes. Read failures are sanitized; no raw filesystem exception or lease
  token is sent in the outcome. No content is stored in a durable journal.
- `file.list`, `file.patch`, and process operations remain unsupported. This
  does not add external content transfer, network connectivity, a daemon,
  relay/MCP, device authentication, or an arbitrary shell endpoint. Scope
  authorization and descriptor checks remain point-in-time; same-user
  tampering, hard links and changes after the final check are not OS-fenced.
  The scope remains not thread-safe, and an existing fd is not retroactively
  revoked by a state transition.

Only after further owned-process, durable-operation and transport gates should
a real Bridge endpoint be considered. These local packages are **not** an RDC
replacement, shell, relay, daemon or MCP.

Add a local daemon controlled by the CLI.

Proposed operator surface:

```text
orch bridge install
orch bridge start
orch bridge stop
orch bridge status
orch bridge doctor
```
The daemon should own only the minimum executor surface needed by OrchKit.

Initial operations:

- project/status readback;
- bounded file read/list;
- bounded patch/write;
- Git status/diff/rev inspection;
- start an owned process;
- receive process events/output;
- stop an owned process;
- submit/checkpoint/quiesce through normal OrchKit state transitions.

Do not start with a general arbitrary-machine shell tool.

## P1-D — local multi-session broker

The Bridge must multiplex several worker sessions over one long-lived device process.

One Bridge process should safely host three workers such as:

```text
session A -> project-a -> writer_key A
session B -> project-b -> writer_key B
session C -> project-c -> writer_key C
```

The broker maintains session lifecycle independently, so failure or reconnect of session B does not cancel A or C.

Required states should include at least:

- CREATED;
- CLAIMED;
- RUNNING;
- CHECKPOINTED;
- RESULT_SUBMITTED;
- VERIFYING;
- COMPLETE;
- BLOCKED;
- CLOSED.
The exact source of truth for run/task state remains the OrchKit ledger; session state is routing/runtime state and must reconcile against it.

Acceptance criteria:

- three independent projects run concurrently on one Mac;
- capacity limits are enforced;
- writer locks are enforced;
- one session cannot impersonate another session ID;
- project pause is isolated;
- daemon restart yields explicit reconnect/recovery state rather than silent continuation;
- `orch overview` can summarize active sessions by project.

## P1-E — remote MCP relay and OrchKit connector

Create a remote MCP endpoint usable by ChatGPT while the local Bridge establishes an outbound authenticated connection.

The Mac should not require an inbound public port.

Conceptual route:

```text
ChatGPT conversations
       |
       v
OrchKit Remote MCP
       |
       v
OrchKit Relay
       |
       v
outbound secure connection
       |
       v
OrchKit Bridge on Mac
```

The relay is routing infrastructure, not authorization authority.

The local Bridge revalidates every mutating call.

## MCP tool design

Prefer project/run-scoped tools over raw-machine tools.
Candidate initial MCP surface:

- orch_overview;
- orch_projects;
- orch_session_start(project_id);
- orch_session_status(session_id);
- orch_claim(session_id);
- orch_read(session_id, relative_path);
- orch_patch(session_id, relative_path, patch);
- orch_git_status(session_id);
- orch_git_diff(session_id);
- orch_process_start(session_id, registered_or_bounded_command);
- orch_process_events(session_id, cursor);
- orch_checkpoint(session_id);
- orch_submit(session_id);
- orch_quiesce(session_id).

Absolute arbitrary filesystem paths should not be the normal model-facing interface.

## Streaming instead of polling

Long processes should produce an event stream or cursor-based event feed.

The goal is to avoid the current pattern of repeated remote polling for process output.

Events should include bounded records such as:

- STARTED;
- STDOUT/STDERR chunks;
- HEARTBEAT;
- EXITED;
- TIMED_OUT;
- TERMINATED;
- CONNECTION_LOST.

A reconnect resumes from a cursor instead of rerunning the process.

## Idempotency and uncertain outcomes

Every mutating operation receives an operation_id.

The local agent keeps a bounded operation journal:
- operation_id;
- session_id;
- run_id;
- operation type;
- requested_at;
- state;
- completion metadata;
- safe result digest/readback information.

A repeated operation_id returns the known original disposition and does not execute the mutation twice.

Git publication and other irreversible operations require explicit readback after transport loss.

## P1-F — first usable multi-project product milestone

This is the first milestone intended for everyday use.

Scope:

- one operator;
- one Mac;
- one OrchKit Bridge;
- one OrchKit connector;
- two or three simultaneous ChatGPT worker conversations;
- each chat bound to a different project;
- current one-writer-per-writer_key safety;
- no concurrent writers inside one repository.

User experience target:

```text
Chat A: connect OrchKit -> select Project A -> work
Chat B: connect OrchKit -> select Project B -> work
Chat C: connect OrchKit -> select Project C -> work
```

No separate local connector installation should be needed per project.

## P1-G — RDC parity and migration

Run the same disposable acceptance suite through both transports:

A. RDC adapter.
B. Native OrchKit Bridge/MCP.
Compare:

- task/plan bytes;
- writer behavior;
- changed paths;
- verification result;
- checkpoint/recovery;
- route evidence;
- Git publication result;
- uncertain-outcome handling.

Only after parity and fresh ordinary-Chat acceptance:

- make OrchKit Bridge the preferred transport;
- keep RDC as an optional compatibility fallback;
- remove Desktop Commander as a required dependency.

## Future P2 — safe concurrency inside one repository

Do not implement this by simply assigning different writer locks to linked Git worktrees.

Safe same-repository concurrency requires:

- OrchKit-owned isolated task worktrees or clones;
- a frozen common base;
- unique task branches;
- per-task verification;
- overlap/conflict detection;
- serialized integration;
- re-verification after integration drift;
- deterministic cleanup/recovery rules.

Until this exists, all linked worktrees sharing one Git common directory should continue to share one writer_key.

## Future P3 — multiple devices

After one-device multi-session operation is stable, add:

- multiple paired Macs/servers;
- project preferred-device binding;
- device health and capacity;
- device capability labels;
- explicit operator routing;
- reconnect/failover policy.
A task must never silently fail over to another device if doing so changes workspace identity or authority.

## Future P4 — hosted/team scale

Possible later work:

- multiple human accounts;
- organizations/teams;
- role-based access;
- shared project ownership;
- hosted relay clusters;
- audit retention controls;
- usage accounting;
- billing;
- geographically distributed devices.

None of this is required for the initial 2–3 project target.

## Implementation sequence

1. Finish/freeze the 0.11.0 release separately.
2. P1-A: three-project concurrency acceptance on current core.
3. P1-B: transport/executor abstraction and RDC adapter parity.
4. P1-C: local Bridge MVP.
5. P1-D: three-session broker on one Mac.
6. P1-E: remote MCP relay/connector with streaming and idempotency.
7. P1-F: everyday 2–3 project acceptance.
8. P1-G: RDC parity and preferred-transport migration.
9. P2: same-repository isolated task worktrees.
10. P3/P4 only when real usage requires them.

## Delivery discipline

Each phase should be a sequence of small PRs with exact scope, tests, readback, CI, guarded merge, and handoff updates.

The native Bridge must not be introduced as one large replacement of RDC.

The first useful goal is deliberately modest: reliable concurrent work on two or three independent projects from separate conversations on one Mac.
