# ORCH post-v0.5 — Git transport execution authority

**Date:** 2026-09-18. **Scope:** publication transport hardening only; no GitHub remote is added.

## Problem

Publication previously disabled repository hooks and content filters, but `git push <remote>` / `git ls-remote <remote>` still ran from the project repository. Git transport behavior can be influenced by repository/global configuration such as alternate push URLs, URL rewriting, credential helpers, SSH command settings, or external transport helpers. Those controls are outside the task file allowlist and therefore should not become implicit execution authority during autonomous publication.

## Hardened transport boundary

Git network/file transport now runs from a short-lived ORCH-owned bare repository under the private runtime directory. The transport repository references the already-created commit objects through a read-only Git alternates file; it does not use the project's `.git/config` for push or remote verification.

The transport subprocess disables system/global Git config, interactive terminal/askpass prompting, repository hooks, credential helpers, Git proxy commands, and default protocol fallback. Only the transport class explicitly derived from the durable bound URL is enabled.

Supported publication transports are:

- local path / local `file://` → canonical absolute local path;
- `https://` without an embedded password;
- `ssh://` and SCP-style SSH URLs, using the trusted system SSH binary in batch mode with user SSH config disabled, proxy/local-command execution disabled, and strict host-key checking.

Plain `http://`, `git://`, `ext::`, unknown schemes, remote `file://host/...`, control characters, and embedded-password HTTPS/SSH URLs fail closed. Interactive HTTPS credential helpers are intentionally not execution authority; authenticated HTTPS that requires one will fail until a separate explicit credential adapter exists.

## Durable remote binding

Registered projects retain the observed remote URL. Git policy blocks push when that registered URL changes. New Git publication tasks persist a canonical `remote_url` and transport kind in the durable plan. Publication uses that literal bound URL and commit id, not a mutable remote name or `HEAD` refspec.

This means changing `remote.origin.pushurl` after verification cannot redirect publication. The local branch/ref CAS and workspace revalidation from v0.5 remain unchanged.

## Deterministic evidence

New tests cover unsafe URL classification, unsupported-transport fallback to local-commit policy, registered remote URL mutation blocking future push plans, and post-verification `pushurl` mutation failing to redirect a bound publication. The existing local bare-remote publication and disabled pre-push-hook E2E continues to pass through the sandboxed transport.

The complete deterministic suite after this block passes **122/122** tests. No Codex/model review was used. A direct test that attempted to inject an `insteadOf` configuration entry was refused by the RDC security gateway; that gateway was not bypassed. The transport implementation itself does not load project/global Git configuration, so URL rewrite configuration is outside the transport subprocess authority.
