# Compatibility

This page records compatibility evidence for the current public code and releases rather than promising support for environments that have not been tested.

## Verified compatibility baseline

The release-readiness checks below were run against the tree published on public `main` after package-metadata preparation.

| OS / architecture | Python | Git | Evidence |
| --- | --- | --- | --- |
| macOS 26.6.2 (25G83), arm64 | 3.9.6 | Apple Git 2.50.1 | 332/332 unit tests passed, `compileall` passed, wheel installed in a clean venv, and `orch --version` returned `0.11.0`. |
| macOS 26.6.2 (25G83), arm64 | 3.12.13 | Apple Git 2.50.1 | 332/332 unit tests passed, `compileall` passed, the same wheel installed in a clean venv, and `orch --version` returned `0.11.0`. |

The Python distribution also builds successfully as `orchkit-0.11.0.tar.gz` and `orchkit-0.11.0-py3-none-any.whl`. In addition to the exact local observations above, compatibility CI exercises every CPython minor line from 3.9 through 3.14 on the GitHub-hosted macOS arm64 runner. The runner's exact macOS, Git, and Python patch versions may change over time, so CI evidence is tied to the recorded workflow run rather than treated as a fixed environment claim. Artifact-build evidence alone does not prove that a particular Git tag, GitHub Release, or package-index upload was published.

## Current support boundary

Do not broaden public support claims beyond the macOS/arm64 environment family and Python lines for which release evidence exists. The exact versions above are the observed test points, not a claim that every neighboring OS or Git version has been exercised.

The package metadata currently declares `Requires-Python: >=3.9`. That declaration controls installer eligibility; it is **not** evidence that every Python version from 3.9 onward has passed OrchKit's verification suite.

The following remain unverified unless later compatibility runs provide evidence:

- Linux and Windows;
- x86_64 and other architectures;
- Python 3.15 and later interpreters;
- Git versions other than the observed Apple Git 2.50.1;
- macOS releases other than the observed 26.6.2 environment.

Reports from unverified environments are welcome, but compatibility should not be advertised until reproducible checks pass there.

## Automated package checks

The compatibility workflow runs on pull requests and pushes to `main` for the macOS/arm64 CPython 3.9, 3.10, 3.11, 3.12, 3.13, and 3.14 matrix. Each job runs unit tests and compilation, builds an sdist and a wheel from that sdist, checks both artifacts with `twine check --strict`, and records their SHA-256 hashes in the job log.

The wheel is installed into a separate clean virtual environment. `scripts/check_installed_package.py` runs with Python `-I` outside the checkout, checks installed metadata/runtime/CLI versions against the candidate source, compares dispatcher-template bytes, and renders a dispatcher into disposable storage. A bootstrap prompt alone does not load or verify the packaged dispatcher template.

These package checks do not publish artifacts, expand the support matrix, launch workers, or certify ordinary-Chat/RDC route acceptance. Each release publication still requires the [owner-gated process](release-process.md).

## Expanding the matrix

Add automated compatibility checks only after the intended environment is named explicitly. A new support claim should record the OS/architecture, Python and Git versions, the source revision, the required test command, and whether package build/install smoke checks passed.

See the [roadmap](../ROADMAP.md) for current product and release priorities.
