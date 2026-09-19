# ORCH post-v0.11 — bounded project package inventory

**Date:** 2026-09-19. **Scope:** read-only project inspection used to infer JavaScript checks.

## Problem

Project inspection previously used `package_json.is_file()` followed by a separate unbounded `read_text()`. A package manifest could therefore be replaced between those operations, a leaf symlink could make ORCH inspect bytes outside the selected repository, and an arbitrarily large manifest could be loaded before parsing.

## Change

`package.json` is now opened through the shared no-follow regular-file reader with a 2 MiB cap before JSON parsing. The target repository is never chmodded or otherwise modified. Unsafe, oversized, invalid, or structurally invalid package metadata records an inventory error and does not synthesize npm/yarn/pnpm checks from untrusted bytes.

Normal manifests still detect `test`, `typecheck`, `lint`, and `build` scripts as before; successful inventory also records exact manifest byte count.

## Evidence

Dedicated tests prove no-follow symlink handling with the external file unchanged, pre-parse size refusal, normal bounded npm check detection, and continued queue compilation from registered projects. The complete deterministic source suite passes **214/214** tests; Python compilation and `git diff --check` pass.

No Codex/model invocation is used. <separately-authorized-integration> remains disabled and `<protected-project>` is untouched.
