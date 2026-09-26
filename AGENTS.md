# ORCH working rules

- This project is independent from `<protected-project>`. Never modify that project unless <separately-authorized-integration> receives a separate explicit approval.
- Primary worker target is a fresh ordinary ChatGPT conversation using the connected RDC path. A disposable end-to-end ordinary-Chat/RDC acceptance trace has completed; route evidence remains per-run observation and must not be treated as self-certifying future launches. Codex is an optional review-only adapter selected by explicit policy; ORCH must work with review mode off. Never silently fall back to ChatGPT Work, Codex execution, paid model APIs, external AI providers, purchased credits, or subscription upgrades.
- Variant B is authoritative: each task/repair attempt runs in a new ChatGPT conversation. Durable context and feedback come from the local ledger, not prior chat history.
- One active writer run. Never expire a lease by time alone. `RESULT_SUBMITTED` is not completion.
- Use task `allowed_paths`; protect recorded sentinel hashes; verifier checks actual bytes and registered commands.
- Use `capability_file`, never expose its contents in prompts or shell arguments.
- Publication requires verified snapshot, empty pre-existing staging, exact changed-path staging, ordinary non-force push, and remote ref verification.
- Do not use reset/clean/stash/force-push to reconcile user work.
- Hooks/plugins/external providers must remain disabled for Codex review. Billing preflight must pass immediately before a model review.


## Immediate P0: bounded execution

- The immediate P0 sequence is complete: bounded-task admission, cooperative checkpoint/recovery with writer/process safety, and actual ordinary-Chat/RDC route acceptance. Preserve these boundaries before adding broader automation.
- Treat time and context budgets as planning and recovery inputs, never as lease expiry, proof of process inactivity, or permission to skip verification.
- Record worker context telemetry with its source and observation time when available; otherwise report `UNKNOWN` rather than inventing a count.
- Current pause behavior blocks new claims only. A `RUNNING` attempt may persist a durable checkpoint without releasing its writer reservation/capability; recovery must stay blocked while external process activity is unknown. Releasing a checkpointed attempt requires explicit independent process-inactivity confirmation, recorded as an operator assertion rather than OS/process fencing. Quiesce still requires `RESULT_SUBMITTED`; automatic fencing and transparent cross-conversation resume of the same `RUNNING` attempt are not implemented.
- Public CLI and documentation must distinguish implemented controls from planned behavior and must not claim route acceptance from a prompt/template alone.
