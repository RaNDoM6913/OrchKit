# ORCH v0.11 — authority I/O hardening release checkpoint

**Date:** 2026-09-19. **Version:** `0.11.0`. **Durable state schema:** v4.

## Release scope

v0.11 keeps the v0.10 bounded handoff guarantees and closes authority-path gaps found during a fresh post-v0.10 audit.

- Generated single/batch tasks now pass the same bounded task-definition validation as direct `load-plan`.
- Batch manifests are opened no-follow from one fd, capped before JSON parsing, reject unknown fields, cap task count, and retain exact source byte provenance.
- Generated plan artifacts are capped before write; plan artifact reads and CLI `load-plan` preserve the leaf path so symlinks are rejected rather than resolved early.
- Local config, project registry, RDC marker, dispatcher, capability and replacement-journal reads use bounded no-follow regular-file handling.
- Capability reads are bound to the exact claim path, require `0600`, reject aliases/symlinks, and cap authority JSON before parsing.
- Atomic JSON/text writes use exclusive random temp files instead of predictable PID temp names; existing leaf symlinks fail closed.
- Custom dispatcher output no longer chmods its parent directory; only ORCH-owned default dispatcher directories are forced to `0700`.
- Backup output rejects leaf symlinks and publishes a securely-created temporary ZIP atomically.
- Backup verification freezes the source archive first and records SHA-256/byte evidence for that frozen input.
- Restore freezes the source a second time and requires the verified archive SHA-256 before extracting, blocking verify→restore source replacement.

`<protected-project>` remains outside write scope and <separately-authorized-integration> remains disabled.

## Regression coverage

Dedicated regressions cover:

- batch oversize/unknown-field/task-count admission and generated-task bounds;
- direct and CLI plan symlink preservation plus generated plan-size refusal;
- predictable-temp symlink traps for atomic authority JSON;
- exact-path, symlink, mode and size enforcement for capability files;
- home/project config and RDC marker no-follow/size behavior;
- dispatcher custom-parent mode preservation plus direct/CLI symlink refusal;
- replacement-journal size/mode enforcement;
- backup output symlink and predictable-temp traps;
- backup verifier symlink refusal and verify→restore source-change fault injection.

No Codex/model review is required for this release slice; deterministic checks are the primary acceptance evidence.

## Acceptance

Final source acceptance on macOS/Python 3.9.6:

- complete deterministic suite: **208/208 PASS**;
- `python3 -m compileall -q orch tests`: PASS;
- `git diff --check`: PASS;
- repo-local durable state: schema v4 `READY`, SQLite `quick_check=ok`, zero foreign-key violations, zero active runs/writer locks/pending publications;
- `orch reconcile`: `CLEAN`;
- no Codex/model invocation was used.

Offline wheel:

- artifact: `dist/agent_workflow_orchestrator-0.11.0-py3-none-any.whl`;
- bytes: `86896`;
- SHA-256: `21986bdcc1b3cbe722398c3cfc42402f54d20c4b960f210a39b75337c81dee9e`.

Fresh installed-wheel smoke used `pip install --no-index --no-deps` in a disposable venv and proved `orch 0.11.0`. CLI plan, backup-output, and dispatcher-output symlink probes all failed closed while preserving their external victims. A normal installed backup returned `BACKED_UP`, verification returned `VERIFIED`, and fresh restore returned `RESTORED`. Verify and restore were bound to the identical archive SHA-256 `b125ad78eac986410653ce74f387cc5acb7baf6212411de3e23547d1881ff034`; the phantom command root remained absent.

## Boundaries retained

No paid OpenAI Platform API, purchased credits, external AI provider, Workspace Agents API, LaunchAgent/autostart, or global RDC/Codex configuration change is introduced. The source repository remains local unless a separate publication permission is granted. Broad RDC shell access remains a cooperative boundary rather than an OS sandbox.

## Next checkpoint

After v0.11 is frozen, continue with the next self-contained hardening/productization task from the local repository. Do not start <separately-authorized-integration> without separate explicit owner approval.
