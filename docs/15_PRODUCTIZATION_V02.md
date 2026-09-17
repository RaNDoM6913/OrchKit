# ORCH v0.2 — productization slice

**Date:** 2026-09-17. **Scope:** independent `<orchkit-root>` only. The trading project remains outside write scope.

## Product decision

ORCH Core must not depend on Codex. The primary worker remains a real Scheduled ChatGPT conversation using RDC. Independent local verification is mandatory. Model review is a policy-selected adapter and may be disabled entirely.

Supported review modes in v0.2 are `off`, `risk_based`, and `required`. The packaged reviewer is `codex`; enabled reviews remain frozen-snapshot/read-only and fail closed on unknown billing/quota/provider state. Review placement is represented separately (`post_verify` / `pre_publish`) so reviewer selection is not hard-coded into the task state model. Pre-work plan review is a later adapter phase, not silently emulated.

## P01 — installable package

- Version `0.2.0`.
- Python >=3.9, no runtime Python dependencies.
- `setup.cfg` + minimal `setup.py` keep wheel builds compatible with the Mac's offline setuptools 58.0.4; PEP-621-only packaging was rejected after an actual `UNKNOWN-0.0.0` wheel failure.
- Console entrypoint: `orch`.
- Dispatcher template is package data under `orch/templates/`.
- Installed default state home: `$ORCH_HOME` or `~/.orch`; repo `bin/orch` explicitly selects repo-local state for development evidence.

A clean venv installation from the generated wheel passed without network access.

## P02 — setup / doctor / RDC bootstrap

`orch setup --profile safe|standard|autonomous` initializes user-owned state and zero-model-API billing policy. `orch doctor` checks Python, Git, state-home writability, optional Codex availability/subscription preflight, and RDC evidence.

The local CLI does not claim it can prove a ChatGPT connector. `orch rdc bootstrap-prompt` generates a one-time prompt for the user's own ChatGPT. That ChatGPT discovers RDC and records the exact observed device with `orch rdc record`. `doctor` remains `ATTENTION`/RDC `UNVERIFIED` until this happens.

## P03 — project registry and task-plan adapter

`orch project add PATH` performs read-only Git/project inventory and writes only ORCH-owned configuration under its state home. It records branch, HEAD, upstream/origin, porcelain status, hooks path, AGENTS presence, package manager/scripts, suggested checks, and content hashes for regular dirty/staged/untracked files already present at registration.

Profiles:

- `safe`: commit off, push off, risk-based reviewer available but not mandatory for tasks whose policy is off.
- `standard`: commit/push policy enabled subject to actual Git readiness and gates.
- `autonomous`: same Git safety bans, fewer default owner gates; it is not a bypass mode.

`project make-plan` generates a bounded ORCH plan with exact workspace, allowed paths, protected hashes, checks, review policy, current expected HEAD, publication policy and max attempts. Existing source backlog integration remains a later project adapter.

## P04 — Git / Review Policy Engine

`orch git inspect PROJECT_ID` refreshes read-only Git facts. `orch git policy PROJECT_ID` distinguishes safety blockers from publication capability. Branch mismatch, pre-existing staging, or changed protected baseline block publication. Missing remote blocks push but not an otherwise authorized local commit.

Publication modes are now:

- `none` — verified result completes without Git publication;
- `git_local` — exact verified paths are staged and committed locally, no push;
- `git` — exact stage + commit + ordinary non-force push + fresh `ls-remote` verification.

Force push, reset, clean and stash are not enabled by any profile.

Risk-based review signals currently include configured task risk tags, sensitive path patterns, large diff thresholds and retry attempts. `Codex=off` goes directly from verifier PASS to approval/publication. Legacy required-review tasks remain compatible.

## Verification completed

- 24 deterministic unit/integration tests PASS, including dotfile/path traversal scope checks and binary staged-byte publication.
- Python compile PASS.
- `git diff --check` PASS before commit.
- Offline wheel build/install into a clean temporary venv PASS.
- Installed `orch --version`, `setup`, `project add/list`, protected dirty baseline and packaged dispatcher render PASS.
- Existing Variant-B real Scheduled ChatGPT/RDC/autochain and real subscription Codex-review evidence remains in docs 13–14; v0.2 does not rewrite that evidence.

## Not yet a public release

Before public release: choose an explicit software license, add CI across supported macOS/Python versions, sign/notarize any future macOS GUI/DMG, add upgrade/migration policy for persistent state, and complete a fresh-user Scheduled Task onboarding test on a second account/Mac. GitHub MCP remains an optional future remote-evidence/publisher adapter rather than a launcher dependency.
