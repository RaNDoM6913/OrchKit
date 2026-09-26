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

## Local state and project workspaces

The OrchKit state directory contains the ledger and private operational artifacts. Keep it separate from registered repositories. A registered workspace is where a task's allowed edits occur; it is not the source of authority for OrchKit's ledger or policy.

The current workflow uses a connected ChatGPT/RDC bridge for its automated worker path. The connection is an operational prerequisite, not evidence of an OS security boundary.

## Bounded execution and current limits

The current CLI admits and displays a versioned bounded-execution contract, but its time/context budget remains advisory and does not enforce worker suspension or termination.

- A bounded task records one clear goal, explicit allowed paths, acceptance criteria, non-goals, registered checks, and recovery metadata. Generated tasks receive conservative defaults, while admitted legacy plans remain compatible.
- `orch queue list` exposes a bounded operator-facing summary before claim: goal, acceptance, non-goals, allowed paths, advisory execution budget, and each check's timeout, output-tail bound, and timeout action. Executable bindings and check-authority evidence are not included in that summary.
- Context or usage telemetry must be attributed to the active worker with a source and observation time when available; missing telemetry remains `UNKNOWN`.
- A timeout or context threshold never expires a writer reservation or proves an external/direct-RDC process has stopped.
- Project/global pause prevents new claims; it does not suspend the current writer. A `RUNNING` worker can persist an `orch checkpoint` without releasing its writer reservation or capability. Recovery reports checkpointed active/unknown process state and remains blocked while external/direct-RDC activity is uncertain. For checkpointed work, abort/retry requires independent process-inactivity confirmation via `--process-inactivity-confirmed`; this is an operator assertion, not OS/process fencing. Quiesce remains cooperative and requires `RESULT_SUBMITTED`. Automatic process fencing and transparent cross-conversation resume of the same `RUNNING` attempt are not implemented.
- `orch route record/show` can persist bounded, secret-free observations for a real ledger run after RDC binding. The evidence is per-run, device-bound and write-once; unavailable model/reasoning/usage telemetry may remain `UNKNOWN`, and the record explicitly does not evaluate acceptance. Dispatcher prompts, RDC markers and route evidence still do not by themselves prove that a fresh ordinary ChatGPT conversation was launched or executed the task. The real end-to-end trace remains a separate acceptance gate.
- The core workflow must not silently substitute ChatGPT Work, Codex execution, paid model APIs, or external providers for the intended worker path.

See the [roadmap](../ROADMAP.md) for the ordered P0 milestones.

## Review and publication

Review can be disabled, risk-based, or required by policy. Codex is an optional reviewer and must not be used as a prerequisite for the core workflow.

When publication is allowed, OrchKit guards the expected branch and base, requires empty pre-existing staging, stages only verified changed paths, uses ordinary non-force Git behavior, and confirms the remote reference. These checks improve workflow integrity; they do not protect against a process that already has broad same-user access.

Read the [architecture](architecture.md) for component relationships and the [security model](security-model.md) before applying OrchKit to sensitive work.
