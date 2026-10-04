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

Introduce internal transport/executor boundaries without changing behavior.

Candidate interfaces:

- DeviceIdentityProvider;
- WorkerTransport;
- FileOperations;
- ProcessOperations;
- GitOperations;
- RouteEvidenceProvider;
- OperationJournal.

The existing RDC path becomes an adapter behind these interfaces.

Route evidence must evolve from an RDC-only transport value to a versioned transport record while remaining able to read historical v1 evidence.

Acceptance criteria:

- current tests continue to pass;
- RDC behavior is unchanged;
- no task, verification, recovery, or publication rule depends on Desktop Commander-specific IDs;
- project/run authority remains in OrchKit.

## P1-C — local OrchKit Bridge MVP

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
