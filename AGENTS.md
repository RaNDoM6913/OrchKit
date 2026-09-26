# ORCH working rules

- This project is independent from `<protected-project>`. Never modify that project unless <separately-authorized-integration> receives a separate explicit approval.
- Primary worker target is a fresh ordinary ChatGPT conversation using the connected RDC path. Actual ordinary-Chat launch/continuation acceptance is still pending; dispatcher templates do not prove it. Codex is an optional review-only adapter selected by explicit policy; ORCH must work with review mode off. Never silently fall back to ChatGPT Work, Codex execution, paid model APIs, external AI providers, purchased credits, or subscription upgrades.
- Variant B is authoritative: each task/repair attempt runs in a new ChatGPT conversation. Durable context and feedback come from the local ledger, not prior chat history.
- One active writer run. Never expire a lease by time alone. `RESULT_SUBMITTED` is not completion.
- Use task `allowed_paths`; protect recorded sentinel hashes; verifier checks actual bytes and registered commands.
- Use `capability_file`, never expose its contents in prompts or shell arguments.
- Publication requires verified snapshot, empty pre-existing staging, exact changed-path staging, ordinary non-force push, and remote ref verification.
- Do not use reset/clean/stash/force-push to reconcile user work.
- Hooks/plugins/external providers must remain disabled for Codex review. Billing preflight must pass immediately before a model review.


## Immediate P0: bounded execution

- Prioritize a visible bounded-task admission contract first, then checkpoint/pause recovery with writer/process safety, then actual ordinary-Chat/RDC route acceptance.
- Treat time and context budgets as planning and recovery inputs, never as lease expiry, proof of process inactivity, or permission to skip verification.
- Record worker context telemetry with its source and observation time when available; otherwise report `UNKNOWN` rather than inventing a count.
- Current pause behavior blocks new claims only. Quiesce requires `RESULT_SUBMITTED`; safe suspension/resumption of an incomplete attempt is not yet an implemented guarantee.
- Public CLI and documentation must distinguish implemented controls from planned behavior and must not claim route acceptance from a prompt/template alone.
