# Contributing to OrchKit

Thanks for considering a contribution. OrchKit is an early source project, so clear scope and reproducible evidence matter more than broad speculative changes.

## Before you start

1. Read the [README](README.md), [workflow guide](docs/workflow.md), and [security model](docs/security-model.md).
2. For a focused source change, install from a checkout using the [Quick Start](README.md#quick-start-from-source).
3. Run the relevant tests before proposing a change:

   ```sh
   python3 -m unittest discover -s tests -v
   ```

4. Discuss a large behavior, interface, or policy change before investing in a broad implementation.

## Disposable multi-project acceptance

Run the three-project concurrency and lifecycle checks from a source checkout with
Python 3.9–3.14 and Git available. No installed OrchKit runtime is required:

```sh
python3 -B scripts/run_multi_project_acceptance.py --json-report /tmp/orchkit-p1-a3-report.json
```

Choose a **new** report filename each time; existing reports are never overwritten.
The default total deadline is 120 seconds; use `--timeout-seconds 180` on a slower
machine. For one named test use `--case test_three_scoped_claims_race_and_survive_reopen`.
The JSON v1 report contains the source HEAD, platform/Python, UTC start, duration,
per-test names and status, run/passed/failed/error/skip counts, exit code and cleanup
status. Failure diagnostics contain only the exception type and a relative test
source line, not exception text, captured output, credentials or private paths.

`PASS` (exit 0) requires all selected tests to pass, consistent results and
confirmed cleanup. `FAIL` (exit 1) means an assertion failure; `ERROR` (nonzero)
means a test or runner error. `INCOMPLETE` (nonzero, timeout exit 124) means skipped,
interrupted or unconfirmed work; a timed-out run reports unknown counts as `null`
rather than fabricating success. The runner spawns a fresh process group, uses
separate disposable Git repositories with **no remotes** and a private OrchKit
home, and removes only its own temporary files after verifying owned processes
have stopped. Unknown process state prevents a cleanup-success claim and retains
private temporary evidence for manual investigation. Never use this fixture on
production project repositories or `ORCH_HOME`.

The multi-process barrier checks concurrent **OS-process writer reservations**,
not execution by three real ChatGPT conversations, a multi-session broker, an
OS-level sandbox, or a production Git publication workflow. These are future
separate milestones; a passing fixture is not evidence that they exist.

## Contribution expectations

- Keep changes focused and explain the user-visible behavior they add or correct.
- Preserve task scope, protected-content, verification, review, and Git-publication boundaries.
- Do not make Codex mandatory, add paid model APIs, external AI providers, purchased credits, or subscription-upgrade fallbacks.
- Do not add claims about platform support, release availability, performance, or security without evidence.
- Keep private state out of commits: no capability files, receipts, logs, backups, device data, local paths, account metadata, credentials, or generated runtime state.
- Add or update tests when behavior changes, and include the command and result in the proposed change.

## Submitting changes

Use a focused branch and a clear commit message. In the change description, include:

- the problem and intended outcome;
- affected paths and any compatibility considerations;
- verification performed;
- security or publication-policy impact, if any.

Changes that affect authority boundaries, task admission, verification, local-state handling, or Git publication need especially careful review.

## License

By submitting a contribution, you agree that it may be distributed under the repository’s [Apache License 2.0](LICENSE).

## Security reports

Do not disclose suspected vulnerabilities in a public issue. Follow [SECURITY.md](SECURITY.md).
