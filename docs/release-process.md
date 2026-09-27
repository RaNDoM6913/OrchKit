# Release Process

OrchKit is still pre-release. This document defines the publication gate for the first versioned release; merging it does **not** authorize a tag, GitHub Release, or package-index upload.

## Target distribution

- Python distribution name: `orchkit`.
- Installed CLI: `orch`.
- Planned package index: PyPI.
- Release record: a GitHub Release tied to the exact Git tag.
- Release artifacts: source distribution plus the universal Python wheel produced by the current package configuration.

The current source version is `0.11.0`, but that metadata value alone is not release authorization. The repository owner must explicitly confirm the release version and publication before the irreversible steps below.

## Required release preflight

Use a fresh checkout of the exact candidate commit. Before tagging or uploading, require all of the following:

1. The candidate commit is on public `main` and the worktree is clean.
2. The compatibility workflow is successful for the exact candidate commit on the documented macOS/arm64 Python 3.9 and 3.12 matrix.
3. `setup.cfg` and `orch.__version__` report the same intended release version.
4. `CHANGELOG.md` contains release notes for that version and date rather than claiming an unpublished version is released.
5. GitHub private vulnerability reporting remains enabled.
6. The PyPI project name is rechecked immediately before upload; an earlier availability check is not a reservation.
7. The repository owner explicitly authorizes publication of the selected version.

## Build and artifact verification

Create release artifacts from the exact candidate commit in an isolated release environment:

```sh
python3 -m venv .release-tools
.release-tools/bin/python -m pip install --upgrade build twine
.release-tools/bin/python -m build --sdist --wheel
.release-tools/bin/python -m twine check dist/*
```

Then install the wheel into a separate clean environment and verify the installed CLI and packaged dispatcher template:

```sh
python3 -m venv .release-smoke
.release-smoke/bin/python -m pip install --no-index dist/orchkit-<version>-py3-none-any.whl
.release-smoke/bin/orch --version
.release-smoke/bin/orch rdc bootstrap-prompt >/dev/null
```

Record the SHA-256 digest of each final artifact. Do not rebuild different bytes under the same published version after upload.

## Publication sequence

Only after the owner gate and exact-candidate checks pass:

1. Commit the final changelog/version state and let compatibility CI pass on that exact commit.
2. Create a new annotated `v<version>` tag that points to the exact verified release commit. Never move an existing release tag.
3. Build and verify the sdist and wheel from the tagged commit.
4. Upload the verified artifacts to PyPI using owner-authorized release credentials.
5. Create the matching GitHub Release for the same tag, publish the release notes, and attach or reference the verified artifacts and checksums.
6. In a fresh environment, install the published PyPI version and confirm `orch --version` plus the dispatcher-template smoke check.

## Uncertain or partial publication

Treat release publication as append-only.

- If an upload result is uncertain, inspect PyPI and GitHub before retrying.
- Never overwrite or silently replace an already published version with different bytes.
- Never force-move a public release tag.
- If a published release requires a fix, prepare a new version and run the full gate again.
- Keep credentials and tokens out of Git history, logs, release notes, issue text, and handoff documents.

## Evidence to retain

For each versioned release, retain a concise, secret-free record of:

- exact release commit and tag;
- successful compatibility workflow run;
- Python package name and version;
- sdist and wheel filenames plus SHA-256 hashes;
- `twine check` result;
- clean-install smoke result;
- PyPI version URL and GitHub Release URL;
- any known compatibility limitations from [compatibility.md](compatibility.md).

Support claims must remain bounded by reproducible evidence. The current verified support baseline is documented in [compatibility.md](compatibility.md), and the security reporting process is documented in [../SECURITY.md](../SECURITY.md).
