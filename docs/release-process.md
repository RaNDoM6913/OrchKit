# Release Process

OrchKit is still pre-release. This document defines the publication gate for the first versioned release; merging it does **not** authorize a tag, GitHub Release, or package-index upload.

## Target distribution

- Python distribution name: `orchkit`.
- Installed CLI: `orch`.
- Planned package index: PyPI.
- Release record: a GitHub Release tied to the exact Git tag.
- Release artifacts: source distribution plus the pure-Python (`py3-none-any`) wheel produced by the current package configuration.

The current source version is `0.11.0`, but that metadata value alone is not release authorization. The repository owner must explicitly confirm the release version and publication before the irreversible steps below.

## Required release preflight

Use a fresh checkout of the exact candidate commit. Before tagging or uploading, require all of the following:

1. The candidate commit is on public `main` and the worktree is clean.
2. The push-triggered compatibility workflow is successful for the exact candidate commit on public `main`, on the documented macOS/arm64 Python 3.9 and 3.12 matrix, including package-artifact checks.
3. `setup.cfg` and `orch.__version__` report the same intended release version.
4. `CHANGELOG.md` contains release notes for that version and date rather than claiming an unpublished version is released.
5. GitHub private vulnerability reporting remains enabled.
6. The PyPI project name is rechecked immediately before upload; an earlier availability check is not a reservation.
7. The repository owner explicitly authorizes publication of the selected version.

## Build and artifact verification

Run these commands from the fresh exact-candidate checkout. Keep tool environments and final artifacts outside the source tree, in a new directory for each attempt:

```sh
set -eu
SOURCE_ROOT=$(pwd -P)
RELEASE_WORK=$(mktemp -d "${TMPDIR:-/tmp}/orchkit-release.XXXXXX")
python3 -m venv "$RELEASE_WORK/tools"
"$RELEASE_WORK/tools/bin/python" -m pip install --upgrade build twine
"$RELEASE_WORK/tools/bin/python" -m build --outdir "$RELEASE_WORK/dist" "$SOURCE_ROOT"
"$RELEASE_WORK/tools/bin/python" -m twine check --strict "$RELEASE_WORK/dist"/*
```

The default `build` operation constructs a source distribution and then builds the wheel from that distribution. This checks that the sdist contains the files required to build the wheel.

Install the wheel into a separate clean environment. Run the shared installed-package check in isolated Python mode and outside the checkout, so source imports cannot mask missing packaged files:

```sh
python3 -m venv "$RELEASE_WORK/smoke"
"$RELEASE_WORK/smoke/bin/python" -m pip install --no-index "$RELEASE_WORK/dist"/*.whl
(
  cd "$RELEASE_WORK"
  "$RELEASE_WORK/smoke/bin/python" -I "$SOURCE_ROOT/scripts/check_installed_package.py" "$SOURCE_ROOT"
)
shasum -a 256 "$RELEASE_WORK/dist"/*
```

The check verifies installed package/runtime/CLI versions against candidate metadata, compares the installed dispatcher template with the candidate bytes, and exercises `dispatcher render` using only a disposable home and output directory. `rdc bootstrap-prompt` is not a dispatcher-template check: it does not load that resource. These checks do not launch workers, claim tasks, or certify ordinary-Chat/RDC route acceptance.

Retain the exact verified artifacts and their SHA-256 digests from `$RELEASE_WORK/dist`. These commands do not upload anything. Do not rebuild different bytes under the same published version after upload.

## Publication sequence

Prepare final changelog/version changes through the normal PR process. After merge, rerun all preflight gates against the resulting exact `main` commit. Earlier CI or authorization for a different candidate does not satisfy that gate.

Only after the owner explicitly authorizes that commit, version, and publication destination, and the exact-candidate checks pass:

1. Create a new annotated `v<version>` tag that points to the exact verified release commit. Never move an existing release tag.
2. Build and verify the sdist and wheel from the tagged commit using the isolated checks above.
3. Upload those verified artifacts to PyPI using owner-authorized release credentials.
4. Create the matching GitHub Release for the same tag, publish the release notes, and attach or reference those same artifacts and checksums.
5. In a fresh environment, install the published PyPI version and run the shared installed-package check against a checkout of that exact tag.

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
