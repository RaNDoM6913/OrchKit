from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Iterable
from urllib.parse import unquote, urlsplit


_ALLOWED_NETWORK_SCHEMES = {"https", "ssh"}
_SCP_LIKE = re.compile(r"^(?:[^/@:\\s]+@)?[^/:\\s]+:.+$")


def _trusted_binary(name: str, preferred: str) -> str:
    candidate = Path(preferred)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise ValueError(f"trusted_{name}_binary_missing")
    resolved = Path(found).resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"trusted_{name}_binary_missing")
    return str(resolved)


def inspect_transport_url(raw_url: str, workspace: Path) -> Dict[str, Any]:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return {"status": "BLOCKED", "reason": "remote_url_missing"}
    value = raw_url.strip()
    if "\x00" in value or "\n" in value or "\r" in value:
        return {"status": "BLOCKED", "reason": "remote_url_control_character"}

    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.netloc not in {"", "localhost"} or not parsed.path:
            return {"status": "BLOCKED", "reason": "remote_file_host_not_local"}
        target = Path(unquote(parsed.path)).expanduser().resolve()
        return {
            "status": "READY", "kind": "file",
            "canonical_url": str(target), "original_url": value,
        }

    if "://" in value:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_NETWORK_SCHEMES:
            reason = "remote_transport_insecure" if scheme in {"http", "git"} else "remote_transport_unsupported"
            return {"status": "BLOCKED", "reason": reason, "scheme": scheme}
        if not parsed.hostname:
            return {"status": "BLOCKED", "reason": "remote_host_missing", "scheme": scheme}
        if parsed.password is not None:
            return {"status": "BLOCKED", "reason": "remote_embedded_password", "scheme": scheme}
        if parsed.fragment:
            return {"status": "BLOCKED", "reason": "remote_url_fragment", "scheme": scheme}
        return {
            "status": "READY", "kind": scheme,
            "canonical_url": value, "original_url": value,
        }

    if value.startswith(("ext::", "git::")):
        return {"status": "BLOCKED", "reason": "remote_transport_unsupported"}
    if _SCP_LIKE.fullmatch(value):
        if value.startswith("-"):
            return {"status": "BLOCKED", "reason": "remote_url_invalid"}
        return {
            "status": "READY", "kind": "ssh",
            "canonical_url": value, "original_url": value,
        }

    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        return {"status": "BLOCKED", "reason": "remote_transport_unsupported"}

    target = Path(value).expanduser()
    if not target.is_absolute():
        target = (workspace.resolve() / target).resolve()
    else:
        target = target.resolve()
    return {
        "status": "READY", "kind": "file",
        "canonical_url": str(target), "original_url": value,
    }


def _transport_env(kind: str) -> Dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "SSH_ASKPASS": "/usr/bin/false",
        "GIT_PROTOCOL_FROM_USER": "0",
    })
    if kind == "ssh":
        ssh = _trusted_binary("ssh", "/usr/bin/ssh")
        env["GIT_SSH_COMMAND"] = (
            f"{ssh} -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes "
            "-o ProxyCommand=none -o PermitLocalCommand=no -o ClearAllForwardings=yes"
        )
        env["GIT_SSH_VARIANT"] = "ssh"
    return env


def _transport_prefix(git_dir: Path, kind: str) -> list[str]:
    git = _trusted_binary("git", "/usr/bin/git")
    return [
        git, "--no-pager",
        "-c", "protocol.allow=never",
        "-c", f"protocol.{kind}.allow=always",
        "-c", "protocol.ext.allow=never",
        "-c", "credential.helper=",
        "-c", "core.gitProxy=",
        "-c", "core.hooksPath=/dev/null",
        "--git-dir", str(git_dir),
    ]


def _source_object_dir(workspace: Path) -> Path:
    git = _trusted_binary("git", "/usr/bin/git")
    env = _transport_env("file")
    probe = subprocess.run(
        [git, "--no-pager", "-c", "core.hooksPath=/dev/null",
         "-C", str(workspace.resolve()), "rev-parse", "--git-common-dir"],
        env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    if probe.returncode or not probe.stdout.strip():
        raise ValueError("git_common_dir_unavailable")
    raw = Path(probe.stdout.strip())
    common = raw.resolve() if raw.is_absolute() else (workspace.resolve() / raw).resolve()
    objects = common / "objects"
    if not objects.is_dir() or objects.is_symlink():
        raise ValueError("git_object_store_unavailable")
    return objects


def run_sandboxed_transport(
    workspace: Path,
    runtime: Path,
    remote_url: str,
    args: Iterable[str],
    *,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    policy = inspect_transport_url(remote_url, workspace)
    if policy["status"] != "READY":
        raise ValueError("git_transport_blocked:" + policy["reason"])

    transport_root = runtime / "git-transport"
    transport_root.mkdir(parents=True, exist_ok=True)
    os.chmod(transport_root, 0o700)
    with tempfile.TemporaryDirectory(prefix="transport-", dir=str(transport_root)) as tmp:
        git_dir = Path(tmp)
        (git_dir / "objects" / "info").mkdir(parents=True)
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/orch-transport\n", encoding="utf-8")
        (git_dir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
            encoding="utf-8",
        )
        source_objects = _source_object_dir(workspace)
        (git_dir / "objects" / "info" / "alternates").write_text(
            str(source_objects) + "\n", encoding="utf-8"
        )
        for item in (git_dir / "HEAD", git_dir / "config", git_dir / "objects" / "info" / "alternates"):
            os.chmod(item, 0o600)
        command = _transport_prefix(git_dir, policy["kind"]) + list(args)
        return subprocess.run(
            command,
            env=_transport_env(policy["kind"]),
            capture_output=True,
            text=not binary,
            timeout=30,
            check=False,
        )
