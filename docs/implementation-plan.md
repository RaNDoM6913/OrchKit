# Implementation Plan

This is the delivery sequence for the [roadmap](../ROADMAP.md), not a list of shipped capabilities. The detailed transport design remains in [Multi-Project Bridge](multi-project-bridge.md).

## Outcome and boundaries

The first everyday-use target is one operator, one Mac, and two or three independent project workers. The existing `orch` CLI remains the core interface. A native connector and an optional macOS client must use the same local authority, not replace it.

Keep one active writer per `writer_key`. Linked worktrees sharing a Git common directory remain serialized. Use only disposable projects for acceptance until real-project integration receives separate owner approval. Do not introduce model API execution, automatic conversation switching, or a second implementation of the ledger in a UI.

## 0. Close the frozen 0.11.0 release

**Release gate: CLOSED on 2026-10-08.** The immutable `v0.11.0` tag was created on October 5, 2026, from commit `00468b52f8bc55462951036577e8774074ef7529`. The frozen distributions were published on [PyPI](https://pypi.org/project/orchkit/0.11.0/) and the matching [GitHub Release](https://github.com/RaNDoM6913/OrchKit/releases/tag/v0.11.0) on October 8, 2026. The sequence below records the completed closure checks; P1 implementation is not included.

1. Resolve the package-index authorization condition out of band. Do not ask for a token in chat, print credentials, or retry an unchanged refusal.
2. Read back the public tag, package-index project/version, GitHub Releases, exact-tag CI, and retained artifact hashes.
3. Reuse the exact retained wheel and sdist for the frozen tag. Documentation changes on `main` are not permission to rebuild 0.11.0 or move its tag.
4. After an authorized successful upload, match the published filenames and SHA-256 digests to the retained artifacts.
5. Create the matching GitHub Release with the same files, checksums, release notes, and compatibility limitations.
6. Install the published version in a fresh environment and run the shared installed-package check against the exact source tag.
7. Update the pending-publication wording and package links through a separate documentation PR; keep the source-tag history unchanged.

**Exit evidence (PASS):** unchanged tag/source identity, all six jobs in [exact-source CI](https://github.com/RaNDoM6913/OrchKit/actions/runs/36434444420), frozen wheel/sdist SHA-256 matches against published PyPI files, strict artifact validation, verified GitHub Release assets, and fresh published-package install/template/dispatcher smoke. Future releases still follow [release-process.md](release-process.md); uncertain publication outcomes require readback before retry.

## 1. P1-A: prove existing multi-project behavior

Start implementation only after the release gate above is closed. Split this into bounded changes rather than a Bridge rewrite.

### P1-A1 — fixture and writer reservations

Create three disposable independent Git repositories and one disposable OrchKit home. Register the projects and enqueue one bounded task per project through supported core/CLI paths, not direct SQL.

Acceptance:
- Project-scoped claims select the intended tasks even when another project's task was enqueued first.
- Three active claims coexist with three distinct writer keys and an active writer count of three.
- A linked-worktree negative case shares its parent's writer key and cannot acquire another writer while that authority is busy.
- Pausing Project B blocks new B claims without releasing its current writer or interrupting A/C; no timer expires a lease.

Use a synchronization barrier when testing simultaneous processes. Distinguish concurrent reservation coverage from actual parallel worker execution; do not infer either from a sequential test's name.

### P1-A2 — independent outcomes and negative cases

Extend the same disposable fixture with independent submit/quiesce/verify/completion paths and local-only Git publication.

Acceptance:
- A failure or checkpoint in B does not alter A/C verification or completion state.
- A capability/receipt from one attempt cannot authorize another attempt's supported operation.
- Supported scope-aware operations reject out-of-scope paths, including applicable path/symlink escape cases.
- All owned test processes are accounted for before cleanup; unknown activity is reported, not treated as an inactive writer.
- The fixture does not contact a production remote, change an installed runtime, or reuse an established state home.

Inspect the actual core API before naming helper operations in tests. Record unsupported surfaces as gaps for P1-C rather than claiming that today's broad same-user shell access is isolated.

### P1-A3 — reproducible acceptance record

Provide a documented fixture command, machine-readable pass/fail evidence, regression tests, and cleanup rules. Record the tested source and test environment. Preserve failed evidence for diagnosis without committing private paths or capability contents.

**Exit gate:** the three-project scheduler behavior and shared-Git negative case are reproducible. A synthetic fixture is not proof of three genuine ChatGPT conversations or a multi-session connector.

## 2. P1-B: transport boundary without behavior drift

The P1-A three-project fixture and reproducible acceptance runner are complete
(PRs #35–#37). The separate registered-check timeout prerequisite was completed
in PR #38; the source-pinned development CLI admits the requested 600-second
full regression without changing the published 0.11.0 release. The initial
exact-main Python 3.9 barrier failure and successful same-SHA rerun remain
recorded as a caveat, not proof that the fixture cannot be timing-sensitive.

**P1-B1 — source inventory and contracts (documentation-only package).**
[Multi-Project Bridge: P1-B](multi-project-bridge.md#p1-b--remove-rdc-from-core-semantics)
records the concrete current caller/surface map, current marker and route
evidence v1 JSON/permission/error contracts, identity and run authority, mode
repair versus inspection, independent verifier/Git publisher, compatibility
tests and proposed interfaces. A ChatGPT worker still makes RDC tool calls
outside OrchKit Python; neither local registered checks nor Git's publication
transport should be relabeled as a worker RDC adapter. Do not implement an
adapter or new evidence version in P1-B1.

**P1-B implementation sequence (one scoped lifecycle per package):**

1. **P1-B2 — completed, PR #40:** pure version-1 marker and route-evidence
   validators in `orch/transport_contracts.py`, retaining dispatcher import,
   errors and serialization compatibility. Verified and merged with exact-main
   CI on October 9, 2026.
2. **P1-B3 — completed, PR #41:** `RouteEvidenceCodec` and
   `RouteEvidenceStore` in `orch/route_evidence_store.py` isolate version-aware
   storage from dispatcher. Historical v1 write-once, permission repair,
   device rebinding and `NOT_EVALUATED` were regression tested. Verified and
   merged with exact-main CI on October 9, 2026.
3. **P1-B4 — completed, PR #42:** `orch/worker_operations.py`
   defines validated, in-memory v1 run binding,
   file/process operation requests, explicit outcome states and structural
   interfaces tested with **fake-only adapters**. It neither performs worker
   I/O nor adds authorization; `UNKNOWN` requires independent readback,
   not an assumed failure or automatic retry. The local registered-check
   subprocess and guarded Git publication remain independent and unchanged.
   A direct ChatGPT-to-RDC tool call still has no native OrchKit session
   authority. Acceptance requires all configured lifecycle/PR/exact-main gates.

**P1-C1 (this scoped package):** add a local, in-process run/session
preflight backed by the existing ledger, claim capability and registered
project/Git identity. Session IDs are volatile routing metadata; the same
capability and RUNNING ledger state are required at every recheck. The gate
does not authorize or execute file/process operations, attest an RDC device,
or provide an OS fence. **P1-C2 and later** must add canonicalized file scope,
symlink/escape defenses, owned processes, bounded responses, durable operation
IDs and restart readback before connecting any real adapter. P1-B4 lexical
validation remains non-authorizing.

**Acceptance for each package:** existing release/P1-A and full registered
regression tests pass; version-1 historical evidence stays readable, unknown
transport or device trust stays fail-closed, and writer keys, capability,
verification, checkpoint/recovery, review/approval and publication gates
remain unchanged. Check exact allowed paths and Git staging, then use the real
OrchKit receipt/quiesce/verification/publication and guarded PR/CI lifecycle.
Do not introduce a daemon, relay, broker, MCP or model API in the same PR.

## 3. P1-C: local Bridge

**P1-C1 — local preflight contract (no Bridge service):**
`orch/bridge_authorization.py` checks the existing RUNNING ledger record,
IN_PROGRESS task, claim capability lease, registered project root,
Git-common-dir writer key, branch and claim HEAD. A same-process authority
tracks volatile random session IDs; opening and checking a session require
live validation, and closing or recreating the authority loses the mapping.
IDs are not bearer permissions and a pause never revokes a running claim.
Invalid mode, symlink, foreign/revoked capability, cross-project/attempt,
branch/HEAD/writer drift and unknown session fail closed in disposable tests.
Checks are point-in-time only: another same-user process is not fenced.
This authority alone provides **no operation dispatch**, file I/O, canonical path access,
process ownership, operation journal, external device trust or service.
The existing verifier and Git publisher remain separate.

**P1-C2 — first bounded file-scope foundation:**
`orch/bridge_file_scope.py` offers a trusted local caller a context-managed,
read-only descriptor for an **existing regular-file** `file.read` target only.
It rechecks the P1-C1 live session/capability and ledger task `allowed_paths`
(exact file entries), uses the registered canonical project root and a pinned
root device/inode, walks all components via directory-relative, no-follow
opens, and verifies the opened object identities. It never returns a reusable
approved pathname, creates missing parents/leaves, or reads content itself.
Unsupported safe OS primitives, symlinks, scope/identity drift and missing or
nonregular files fail closed. The scope owns its root descriptor; the returned
file descriptor is closed by its context even on errors. These are point-in-time
checks, not an OS sandbox: same-user races, hard links and already-granted fd
access remain residual limitations. The scope is not thread-safe.

**P1-C2 — second bounded package (`file.read` content):**
`orch/bridge_file_read.py` adds a **local in-process** `LocalBoundedFileReader`
using an already-live `LocalFileScope`, not arbitrary path or shell access.
An authorized read returns version-1 `WorkerOperationOutcome` bytes only after
EOF within the request's maximum (1–65,536 bytes), unchanged file metadata,
and a second no-follow descriptor acquisition that rechecks session,
capability, run, allowlist, root and leaf identity. Reading is limited to the
requested byte ceiling plus one sentinel byte; oversized/changed files return
no partial bytes, and I/O errors do not disclose raw system exceptions.
The reader dispatches no `file.list`, `file.patch` or process operation. This synchronous
in-memory adapter is **not** a remote endpoint, durable operation journal or
OS sandbox; malicious same-user races, hard links and changes after readback
remain possible, and the original scope is not thread-safe.

**P1-C2 — third bounded package (local `file.list`):**
`orch/bridge_file_list.py` adds a trusted, in-process
`LocalBoundedFileLister` using a live `LocalFileScope`.
The listed target must be an **existing directory** with its own literal
trailing-slash entry (for example `docs/`) in the task's live
`allowed_paths`; a parent grant or a grant to a child file is insufficient.
`open_listing` anchors each directory component with no-follow,
descriptor-relative opens and rechecks the pinned project root and
session/capability/ledger scope. The adapter enumerates names only, never
opens child entries, bounds its complete compact sorted JSON response by
`max_output_bytes` (1–65,536), and emits no partial output on overflow,
directory change or revoked authority. Symlinks *inside* the listed directory
are names only; symlink ancestors and the directory target are refused.
A fresh descriptor acquisition and unchanged directory metadata check are
required before returning bytes. An unsupported descriptor-scanning OS is
refused; scanner errors are sanitized. This is not a remote API, durable
journal, atomic directory snapshot or same-user OS sandbox.

**P1-C2 — fourth bounded package (plan directory grants):**
`build_single_task_plan` now retains one trailing `/` after normalizing an
explicit directory entry; `build_batch_plan` delegates to the same helper.
File entries never acquire a directory marker by inference. Existing schema
validation still rejects normalized duplicates, including `docs` plus
`docs/`, traversal, absolute paths and malformed entries. A single string is
rejected rather than interpreted as an iterable of one-character file grants.
A generated `docs/` grant has the **existing** subtree permission semantics
for file reads and changed-path scope; it is not a new list-only permission.
Listing still requires that directory's own literal entry, never just a
parent or child grant. Tests cover generated plan round-trip/native admission
and all three local scope/read/list surfaces without the former plan rewrite.
No schema, core normalizer, adapter, authority or installed runtime changes;
previously admitted plans are not migrated.

**P1-C2 — fifth bounded package (original-content patch preflight):**
`orch/bridge_patch_preflight.py` provides a trusted local caller with
`FilePatchBaseline` (canonical SHA-256 and byte length) and
`LocalFilePatchPreflight.check`. It accepts the unchanged **file.read** request
and uses `LocalBoundedFileReader` twice: first compare the expected original,
then independently reobserve the bytes through renewed live authority. Both
observations must match; success returns no contents and grants no permission
to write. Each read uses the existing request ceiling of at most 65,536 bytes.
This preflight helper refuses actual `file.patch` requests. The companion input is local only;
v1 fields, kinds, protocols, serialization and capability semantics do not
change. This is original-content observation, not patch-payload validation,
atomic compare-and-swap, durable replay or an OS fence.

Repeated operation IDs recompute through live authority; no result is cached.
Crash/restart discards the observation and requires a new live session and
fresh checks. A later mutating package must first establish durable request
fingerprints/operation IDs, crash-safe publication and independent readback of
uncertain outcomes; it cannot consume this success as an apply token. Positive,
limit, mismatch, revocation, race and compatibility tests use disposable real
ledger/session/capability fixtures. Verifier and publisher remain independent.

**P1-C2 — sixth bounded package (durable patch intent and readback):**
`orch/bridge_patch_intent.py` adds a versioned, bounded local replacement
companion and a trusted `LocalFilePatchIntentService`. It accepts `file.patch`
metadata without changing request v1 or implementing the patch adapter.
Preparation checks fresh run/session/capability authority, then looks up a
durable ID before preflighting a new original. Identical duplicates remain
`EXISTING` when target bytes later change; conflicting fingerprints refuse.
Only a new intent performs the existing original-content preflight and a final
live authority/scope check before publication.

`orch/bridge_operation_journal.py` stores immutable `INTENT_RECORDED` metadata
under the private runtime. Hashed run/operation storage names, strict bounded
records and descriptor-relative no-follow checks protect lookup and publication.
An owned temporary file is fsynced, published without replacement, unlinked from
its temporary alias, and followed by directory fsync. Participating creators
use a bounded advisory directory lock; contention or uncertain publication
returns `UNKNOWN`, never automatic replay. Final records are preserved after
publication failure. Payload bytes and capabilities are not stored. Unsafe or
incomplete evidence remains visible rather than being silently repaired.

The service independently observes a recorded target twice through renewed
existing file-read authority. `ORIGINAL_MATCH`, `REPLACEMENT_MATCH`,
`OTHER_CONTENT` and `UNAVAILABLE` describe content only; none proves that this
executor applied a patch, completes a run, or permits a retry. Equal original
and replacement descriptions are rejected. Historical operator inspection is
available through `orch bridge operation inspect`; it neither opens a live
session nor creates or repairs state.

Backup selection, archive verification and restore preserve exact journal
records. Initial/final bounded census checks refuse changed or incomplete
coverage; the existing active-run backup prohibition remains. Retention does
not prune these records as ordinary log evidence. Read-only recovery surfaces
live, terminal, orphaned and invalid intent evidence while preserving writer,
capability, native verification and Git publication authority. Existing homes
without a journal retain their previous behavior. The intent-only package is a
local trusted API, not a daemon/transport, workspace mutator, exactly-once
executor or OS sandbox.

**P1-C2 — seventh bounded package (local existing-file replacement):**
`LocalBoundedFilePatcher(authority, session_id, binding, capability_file)` exposes
local `apply(request, FilePatchReplacement)` and `reconcile(request)`. Apply
replaces one existing allowed regular file; both original and complete
replacement are at most 65,536 bytes. It refuses missing targets/parents,
symlinks, nonregular files, hardlinks, foreign uid, special mode bits and file
flags. Native macOS descriptor APIs verify ordinary uid/gid/mode and bounded
xattrs, including resource forks (32 names, 4,096 name bytes, 65,536 aggregate
value bytes); unsupported ACL/inheritance or unprovable preservation refuses.
One owned random sibling scratch file is internal staging for that exact target,
not authority to create or delete workspace targets.

One nonblocking advisory lock on the existing private runtime directory fd
serializes participating apply/reconcile and core submit/quiesce/abort. Unresolved
execution evidence or an unsafe complete census blocks those state transitions,
retaining writer and capability authority. Strict append-once execution records
in `.runtime/patch_execution` bind dispatch, publication and result to the
immutable intent and predecessor hashes. Dispatch is durable before staging;
publication is durable before rename. An `APPLIED` receipt requires parent fsync
and independent inode/content/metadata readback. Payload, capability and raw
xattr values are never persisted. Uncertain publication/rename/fsync/readback
returns `UNKNOWN` and preserves evidence.

`orch bridge execution inspect` reads validated metadata only; the existing
`orch bridge operation inspect` intent view is unchanged. Explicit
`orch bridge execution reconcile --run-id RUN --operation-id OP --cap FILE`
rebuilds the validated stored request under a fresh active session. It performs
no dispatch, staging, replacement-byte replay or rename. Exact staged-inode
readback can append `APPLIED`; exact unchanged original plus proven owned-stage
cleanup, or dispatch-only strict scratch absence, can append `NOT_APPLIED`.
Missing/substituted/ambiguous scratch stays `UNKNOWN` and is preserved. Terminal
IDs cannot be reused; returned existing receipts are historical, even if later
content changes. A historical service reply or core closure requires renewed
acknowledgement of every existing phase record, linked intent and pinned
directory through home under the same runtime lock, followed by independent
exact-byte and identity readback. Failure preserves evidence and returns
`UNKNOWN` or refuses closure; it never replays a patch or rewrites current
workspace content. Structural terminal inspection, recovery and
backup/verify/restore do not renew acknowledgement. Backup/verify/restore
preserve the chain and exact intent linkage; retention leaves execution
evidence and scratch intact, and active-run backups remain prohibited.

This is a trusted local source API with a pending product Bridge endpoint.
Cooperative provenance is not OS compare-and-swap or same-user fencing;
process/fault fixtures do not establish actual power-loss recovery. Native
verifier/publisher and current source/PR/merge acceptance gates remain separate.

**Still not implemented:** general write/create/delete adapters, external content
transport, owned processes, general operation replay, daemon, multi-session broker,
relay/MCP or device trust. Finish those as separately scoped gates before any
first ordinary-Chat route without RDC; do not relabel this foundation as a
working Bridge or relax P1-C1/verifier/publisher authority.

Define a versioned local protocol and a bounded, explicitly authorized daemon lifecycle before adding remote connectivity. The CLI and future UI must consume the same protocol.

Build in separate packages:
- Run/session binding, local authorization, and bounded file operations.
- Path canonicalization, symlink/escape rejection, and allowed-path tests.
- Owned-process start, output limits, deadlines, exit readback, and safe termination.
- Durable operation IDs, idempotent retry, and restart/readback semantics before exposing remote mutations.

**Acceptance:** a session cannot change its project/run authority; unsupported operations are refused; secrets/capability contents never enter model-facing responses; failures are durable and attributable. Restart is not evidence that an uncertain write failed or that a process stopped. Do not expose unrestricted machine shell as the normal interface.

The existing core is not an OS sandbox. Any stronger isolation claim requires an independently verified OS boundary, not just a new endpoint.

## 4. P1-D: three-session broker on one Mac

Add independent session lifecycles and device capacity controls over the local Bridge. Initial proposed limits are three workers, six owned processes total, and two per worker; validate these as configurable operational defaults, not new writer permissions.

**Acceptance:** three independent projects make progress; one disconnect does not cancel the others; project pause is isolated; session impersonation and shared-Git concurrent writers are denied; capacity exhaustion is explicit; restart requires honest recovery readback. Overview must distinguish active, checkpointed, unknown, and complete states.

## 5. P1-E: remote MCP route

Only after local operations and reconnect semantics are tested, add the outbound device connection and the remote relay/connector. Any hosting, domain, account, or paid infrastructure change requires separate owner approval.

The relay routes requests; local policy remains authoritative. Use bounded run-scoped tools, operation IDs, and resumable output cursors. Do not forward capability-file contents or treat a relay session ID as sufficient filesystem authority.

**Acceptance:** authentication/authorization separation, duplicate mutation requests, lost responses, reconnect, backpressure, and device-offline behavior are tested. RDC remains available; do not silently substitute it for failed Bridge acceptance.

## 6. P1-F/G: everyday acceptance, then migration

Run the same bounded disposable scenarios through RDC and the native Bridge. Compare scope, changed bytes, verification, recovery, route observations, and Git outcomes.

Perform fresh acceptance with separate ordinary ChatGPT conversations bound to different projects. The operator may open the conversations manually; do not introduce browser automation or other execution providers to manufacture this evidence.

**Exit gate:** two or three real project workers work independently, uncertainty and recovery are visible, and no cross-project authority is observed through supported operations. Only then prefer the Bridge, while retaining the tested RDC fallback. Production rollout is a separately approved canary, not part of disposable acceptance.

## 7. Distribution and macOS experience

**CLI now:** maintain the isolated source/package installation route, useful overview/doctor output, explicit state selection, deliberate upgrades, and state-preserving uninstall guidance. The CLI stays usable without a GUI.

**Terminal dashboard optional:** build only if operator use justifies it; do not make it a prerequisite or delay the Bridge for it.

**Swift/SwiftUI after the local API stabilizes:** start with a read-only menu-bar/status client for projects, connection state, tasks, and bounded diagnostics. Then add explicit actions through the same local API. Never edit SQLite directly or duplicate core safety policy in Swift. A pause button must not imply that an active process was stopped.

**Native distribution gate:** verify protocol/version compatibility, user-controlled startup, upgrade and recovery, signing/notarization, and an uninstall flow that preserves workflow state by default. A DMG or dedicated OrchKit Homebrew formula is not a current deliverable.

## Deferred work

Same-repository parallel writers require isolated task worktrees, a frozen base, independent verification, conflict detection, and serialized integration. They are not enabled by changing writer keys. Multi-device scheduling and team/hosted scale follow demonstrated single-device demand, not the first three-project milestone.

## Per-package completion and handoff

For each package: read the current source and handoff; define a small allowed scope; implement in isolation; run relevant positive/negative tests; inspect the exact diff; push non-force; verify the remote head; require all current PR checks; merge with an exact-head guard; then check the exact merge commit's CI. Preserve public release tags and artifacts.

Record the source/branch, changed files, completed checks, unresolved gates, owned process state, and one bounded next task. Use a fresh conversation for the next task/repair attempt. A successful earlier CI run, elapsed time, or an optimistic worker report never substitutes for current evidence.
