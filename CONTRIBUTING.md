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
