# ORCH v0.4 — installed-wheel acceptance

**Date:** 2026-09-18.

A release smoke test was run entirely from disposable paths and without network/package-index access.

## Build and install

The source tree produced:

`agent_workflow_orchestrator-0.4.0-py3-none-any.whl`

using:

```sh
python3 -m pip wheel . --no-deps --no-build-isolation
```

The wheel was installed into a newly created Python 3.9 virtual environment with `pip install --no-index --no-deps`.

## Installed CLI acceptance

The installed console entry point, not `bin/orch`, successfully:

- reported `orch 0.4.0`;
- initialized a fresh safe-profile ORCH home;
- opened schema v3 with permission health `READY`;
- registered a fresh disposable Git repository under Standard/off review policy;
- compiled and durably enqueued a task through `queue enqueue`;
- reported that task as `READY` through the project-filtered queue view;
- rendered the dispatcher from packaged template data.

Fresh installed state used private `0700` runtime/project directories and `0600` SQLite/WAL/SHM files.

The wheel and venv were deleted after the smoke test. No Git remote was created for the ORCH source repository and the source worktree remained clean.
