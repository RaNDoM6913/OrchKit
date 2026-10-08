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

First inventory RDC-specific assumptions, device identity, file/process execution, and route-evidence validation. Introduce small interfaces behind the existing behavior, with the RDC adapter remaining the default.

Suggested increments: interface inventory and contracts; adapter extraction; versioned route-evidence compatibility. Choose actual module names after inspecting the current source.

**Acceptance:** the release-baseline and P1-A tests continue to pass; historical evidence remains readable; unknown transport/identity states fail closed; no writer, verification, recovery, or publication rule is weakened. Do not add a daemon or relay in the same PR.

## 3. P1-C: local Bridge

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
