# Changelog

All notable public-facing changes are documented here. OrchKit has not published a versioned release.

## Unreleased

### Added

- Public project overview, source-based Quick Start, workflow guide, and curated documentation index.
- Contribution and security policies for the early public source stage.
- Verified compatibility documentation, automated compatibility checks across CPython 3.9 through 3.14 on macOS arm64, private vulnerability reporting, and an owner-gated release process.

### Changed

- Prepared Python distribution metadata under the OrchKit package name (`orchkit`) while preserving the `orch` CLI.
- Added package metadata for the Apache-2.0 license and public source, issue, and documentation URLs.
- Made packaged prompt templates explicit package content and added automated sdist/wheel validation plus isolated installed-wheel version, template-byte, and dispatcher-render checks.
- Corrected release smoke instructions to read the actual packaged dispatcher template, keep scratch environments outside the checkout, and recheck the final exact-main candidate before publication.
- Pinned third-party GitHub Actions to immutable commit SHAs and enabled weekly Dependabot updates for workflow actions.
- Replaced legacy internal planning material in the public source tree with concise, English product documentation.
