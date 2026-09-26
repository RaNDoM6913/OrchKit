# Compatibility

OrchKit is still pre-release. This page records compatibility evidence for the current public source rather than promising support for environments that have not been tested.

## Verified release-readiness baseline

The release-readiness checks below were run against the tree published on public `main` after package-metadata preparation.

| OS / architecture | Python | Git | Evidence |
| --- | --- | --- | --- |
| macOS 26.6.2 (25G83), arm64 | 3.9.6 | Apple Git 2.50.1 | 332/332 unit tests passed, `compileall` passed, wheel installed in a clean venv, and `orch --version` returned `0.11.0`. |
| macOS 26.6.2 (25G83), arm64 | 3.12.13 | Apple Git 2.50.1 | 332/332 unit tests passed, `compileall` passed, the same wheel installed in a clean venv, and `orch --version` returned `0.11.0`. |

The Python distribution also builds successfully as `orchkit-0.11.0.tar.gz` and `orchkit-0.11.0-py3-none-any.whl`. No Git tag, GitHub release, or package-index upload is implied by that build evidence.

## Current support boundary

For the first versioned release, do not broaden public support claims beyond the macOS/arm64 environment family and Python lines for which release evidence exists. The exact versions above are the observed test points, not a claim that every neighboring OS or Git version has been exercised.

The package metadata currently declares `Requires-Python: >=3.9`. That declaration controls installer eligibility; it is **not** evidence that every Python version from 3.9 onward has passed OrchKit's verification suite.

The following remain unverified unless later compatibility runs provide evidence:

- Linux and Windows;
- x86_64 and other architectures;
- Python 3.10, 3.11, 3.13, and later interpreters;
- Git versions other than the observed Apple Git 2.50.1;
- macOS releases other than the observed 26.6.2 environment.

Reports from unverified environments are welcome, but compatibility should not be advertised until reproducible checks pass there.

## Expanding the matrix

Add automated compatibility checks only after the intended environment is named explicitly. A new support claim should record the OS/architecture, Python and Git versions, the source revision, the required test command, and whether package build/install smoke checks passed.

See the [roadmap](../ROADMAP.md) for the remaining pre-release gates.
