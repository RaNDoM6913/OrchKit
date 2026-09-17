# ORCH working rules

- This project is independent from `<protected-project>`. Never modify that project unless <separately-authorized-integration> receives a separate explicit approval.
- Primary worker is a real ChatGPT scheduled conversation. Codex is an optional review-only adapter selected by policy; ORCH must work with review mode off. No paid model API, external AI provider, purchased credits, or subscription upgrade fallback.
- Variant B is authoritative: each task/repair attempt runs in a new ChatGPT conversation. Durable context and feedback come from the local ledger, not prior chat history.
- One active writer run. Never expire a lease by time alone. `RESULT_SUBMITTED` is not completion.
- Use task `allowed_paths`; protect recorded sentinel hashes; verifier checks actual bytes and registered commands.
- Use `capability_file`, never expose its contents in prompts or shell arguments.
- Publication requires verified snapshot, empty pre-existing staging, exact changed-path staging, ordinary non-force push, and remote ref verification.
- Do not use reset/clean/stash/force-push to reconcile user work.
- Hooks/plugins/external providers must remain disabled for Codex review. Billing preflight must pass immediately before a model review.
