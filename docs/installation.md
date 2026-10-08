# Install and Update OrchKit

OrchKit is a Python command-line application. The distribution is `orchkit`; the installed command is `orch`. A separate terminal UI or macOS app is not required.

**Distribution status:** `orchkit==0.11.0` is published on [PyPI](https://pypi.org/project/orchkit/0.11.0/) with a matching [GitHub Release](https://github.com/RaNDoM6913/OrchKit/releases/tag/v0.11.0), both published on October 8, 2026. The immutable `v0.11.0` source tag was created on October 5, 2026, and remains an alternative source-install route.

Release verification covers macOS arm64 and CPython 3.9 through 3.14. Other combinations are unverified; see [compatibility](compatibility.md). Installation does not initialize a ledger, start workers, or register a project.

## Recommended: an isolated CLI installation

[pipx](https://pipx.pypa.io/) installs a CLI in its own environment and exposes its commands on your `PATH`. On a Mac with Homebrew:

```sh
brew install pipx
pipx ensurepath
```

A supported Python interpreter must be available; Git is needed only for optional source-tag installs. Open a new terminal, then install the published package:

```sh
pipx install 'orchkit==0.11.0'
orch --version
orch --help
```

Current pipx needs Python 3.10 or newer. This is a pipx requirement, not a change to OrchKit's Python 3.9 baseline. Use the virtual-environment route below when appropriate. See the upstream [pipx installation guide](https://pipx.pypa.io/latest/how-to/install-pipx.html).

The source-tag pipx route was checked with pipx 1.17.11 and CPython 3.12.13 on macOS arm64. The broader core compatibility matrix is separate. Use pipx's `--python` option to select a supported interpreter when needed.

If a command is missing or resolves to an older installation, run `command -v orch` and `pipx list`. Do not replace another active runtime or use `sudo pip install` to work around a path conflict.

## Published package and source-tag alternative

The recommended pipx command above installs the exact released PyPI version. The published wheel, source archive, SHA256SUMS, and notes are also available from the [GitHub Release](https://github.com/RaNDoM6913/OrchKit/releases/tag/v0.11.0). To install directly from the frozen source tag instead:

```sh
pipx install 'git+https://github.com/RaNDoM6913/OrchKit.git@v0.11.0'
orch --version
```

## Alternative: a dedicated virtual environment

```sh
python3 -m venv "$HOME/.local/share/orchkit-venv"
"$HOME/.local/share/orchkit-venv/bin/python" -m pip install 'orchkit==0.11.0'
"$HOME/.local/share/orchkit-venv/bin/orch" --version
```

Activate that environment when you want the short `orch` command:

```sh
source "$HOME/.local/share/orchkit-venv/bin/activate"
```

Keep the application environment separate from the workflow state directory.

## Install from a release tag or current source

```sh
git clone https://github.com/RaNDoM6913/OrchKit.git
cd OrchKit
git checkout --detach v0.11.0
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/orch --version
```

The tag selects the immutable source for the published 0.11.0 release. Omit the checkout command only when intentionally evaluating current development source. A version string alone does not identify the commit or prove that development source matches released artifacts.

## First setup

Choose a private state directory outside any Git repository. For a new installation:

```sh
export ORCH_HOME="$HOME/.local/share/orchkit-state"
orch overview
orch setup --profile safe
orch doctor --skip-codex
orch overview
```

Before setup, `overview` is read-only and reports the missing setup without creating it. The safe profile denies publication by default. A missing RDC connection is an expected onboarding item, not a reason to enable an optional reviewer.

Follow the [Quick Start](../README.md#quick-start) to bind RDC and register a project. Use the same explicitly chosen `ORCH_HOME` in every terminal; do not initialize a second home accidentally. Existing users should inspect their established home rather than running this new-install example against it blindly.

## Updating and removing the application

Before an update, confirm that no active worker depends on the environment being changed, inspect the release notes, and review `orch state backup --help` and back up the established state through that explicit command. See the [workflow guide](workflow.md) for the surrounding safety gates. Pause blocks new claims; it is not proof that an existing writer or process stopped.

A Git-tag installation stays pinned to that source. Likewise, do not assume `pipx upgrade orchkit` changes a pinned `orchkit==0.11.0` installation to another version. Once no worker depends on the installed runtime, remove the managed application with `pipx uninstall orchkit` and install the deliberately selected version or tag. Keep the workflow state separate and retained.

For an unpinned PyPI-managed installation, `pipx upgrade orchkit` can select a newer package; review the available version first. For an isolated virtual environment, use its Python executable with `-m pip install --upgrade` and the deliberately selected tag or published package version.

`pipx uninstall orchkit` removes the managed application, not the separately chosen workflow state directory. Retain state and evidence unless intentionally retiring that installation. Do not downgrade a live ledger without a verified compatibility/recovery plan.

## Other interfaces

A Homebrew formula for OrchKit itself, an interactive terminal dashboard, and a native SwiftUI macOS application are roadmap items, not distributions included in 0.11.0. `brew install pipx` installs the package manager; it does not imply that `brew install orchkit` is available.

The intended order is a dependable CLI, a local multi-session Bridge, and then optional interfaces over the same core. See the [roadmap](../ROADMAP.md).
