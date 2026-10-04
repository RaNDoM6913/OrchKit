# Changelog

All notable public-facing changes are documented here.

## Unreleased

### Documentation

- Refined the project landing page with compatibility, package, and license navigation.
- Added isolated CLI installation, update, troubleshooting, and removal guidance.
- Published the staged three-project Bridge roadmap and the CLI-first/native-client interface strategy, with planned work clearly separated from shipped features.

## 0.11.0 - 2026-10-05 (source tag)

The source tag is available. Package-index publication and the matching GitHub Release remain pending; this is not a completed distribution release. The tagged source retains the earlier changelog preparation date.

### Added

- Public project overview, package/source Quick Start, workflow guide, and curated documentation index.
- Contribution and security policies for the early public project stage.
- Verified compatibility documentation, automated compatibility checks across CPython 3.9 through 3.14 on macOS arm64, private vulnerability reporting, and an owner-gated release process.
- Added `orch overview`, a read-only operator control view with setup, RDC, project-readiness, state-health, active-writer/publication, and guided next-step summaries.

### Changed

- Prepared Python distribution metadata under the OrchKit package name (`orchkit`) while preserving the `orch` CLI.
- Added package metadata for the Apache-2.0 license and public source, issue, and documentation URLs.
- Made packaged prompt templates explicit package content and added automated sdist/wheel validation plus isolated installed-wheel version, template-byte, and dispatcher-render checks.
- Corrected release smoke instructions to read the actual packaged dispatcher template, keep scratch environments outside the checkout, and recheck the final exact-main candidate before publication.
- Pinned third-party GitHub Actions to immutable commit SHAs and enabled weekly Dependabot updates for workflow actions.
- Made `doctor --skip-codex` treat the optional Codex reviewer check as explicitly skipped, and exposed the registered `project_id` at the top level of `project add` output for easier onboarding.
- Replaced legacy internal planning material in the public source tree with concise, English product documentation.

### Fixed

- Enabled the trusted GitHub HTTPS credential helper for isolated publication, with non-GitHub and non-HTTPS negative coverage.
