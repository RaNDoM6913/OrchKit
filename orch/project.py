from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (HOME_CONFIG_MAX_BYTES, atomic_write_json,
                     ensure_private_dir, read_bounded_json_object)
from .core import sha256_file
from .git_transport import inspect_transport_url
from .review_policy import MODES, REVIEWERS

PROJECT_CONFIG_MAX_BYTES = 2 * 1024 * 1024
PACKAGE_JSON_MAX_BYTES = 2 * 1024 * 1024


PROFILE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "safe": {
        "git": {"allow_commit": False, "allow_push": False, "allow_force_push": False},
        "review": {"mode": "risk_based", "reviewer": "codex", "placement": "pre_publish"},
        "approval": {"default": "required"},
    },
    "standard": {
        "git": {"allow_commit": True, "allow_push": True, "allow_force_push": False},
        "review": {"mode": "risk_based", "reviewer": "codex", "placement": "pre_publish"},
        "approval": {"default": "policy"},
    },
    "autonomous": {
        "git": {"allow_commit": True, "allow_push": True, "allow_force_push": False},
        "review": {"mode": "risk_based", "reviewer": "codex", "placement": "pre_publish"},
        "approval": {"default": "only_required_gates"},
    },
}


def _git_env() -> Dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    return env


def _git_base(repo: Path) -> List[str]:
    return [
        "git", "--no-pager",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "diff.external=",
        "-c", "core.hooksPath=/dev/null",
        "-C", str(repo),
    ]


def _git_filter_overrides(repo: Path) -> List[str]:
    env = _git_env()
    prefix = _git_base(repo)
    listed = subprocess.run(
        prefix + ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        env=env, capture_output=True, text=False, timeout=20, check=False,
    )
    if listed.returncode != 0:
        return []
    paths = [os.fsdecode(item) for item in listed.stdout.split(b"\0") if item]
    drivers = set()
    for offset in range(0, len(paths), 200):
        batch = paths[offset:offset + 200]
        probe = subprocess.run(
            prefix + ["check-attr", "-z", "filter", "--", *batch],
            env=env, capture_output=True, text=False, timeout=15, check=False,
        )
        if probe.returncode != 0:
            raise ValueError("git_filter_attribute_probe_failed")
        fields = probe.stdout.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 3:
            raise ValueError("git_filter_attribute_probe_malformed")
        for index in range(0, len(fields), 3):
            value = os.fsdecode(fields[index + 2])
            if value not in {"unspecified", "unset"}:
                drivers.add(value)
    cat_binary = "/bin/cat" if Path("/bin/cat").is_file() else "/usr/bin/cat"
    if not Path(cat_binary).is_file():
        raise ValueError("trusted_cat_binary_missing")
    overrides: List[str] = []
    for driver in sorted(drivers):
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", driver) is None:
            raise ValueError("unsafe_git_filter_driver")
        overrides.extend([
            "-c", f"filter.{driver}.process=",
            "-c", f"filter.{driver}.clean={cat_binary}",
            "-c", f"filter.{driver}.smudge={cat_binary}",
            "-c", f"filter.{driver}.required=false",
        ])
    return overrides


def _safe_git(repo: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    env = _git_env()
    base = _git_base(repo)
    cmd = base[:-2] + _git_filter_overrides(repo) + base[-2:] + list(args)
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _safe_git_bytes(repo: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    env = _git_env()
    base = _git_base(repo)
    cmd = base[:-2] + _git_filter_overrides(repo) + base[-2:] + list(args)
    return subprocess.run(
        cmd, env=env, capture_output=True, text=False, timeout=timeout, check=False
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return normalized or "project"


def _detect_commands(root: Path) -> Dict[str, Any]:
    detected: Dict[str, Any] = {
        "package_manager": None,
        "suggested_checks": [],
    }
    package_json = root / "package.json"
    try:
        package, package_meta = read_bounded_json_object(
            package_json,
            max_bytes=PACKAGE_JSON_MAX_BYTES,
            unsafe_error="package_json_unsafe",
            too_large_error="package_json_too_large",
            invalid_error="package_json_invalid",
        )
    except FileNotFoundError:
        package = None
    except ValueError as exc:
        package = None
        detected["package_json_error"] = str(exc)
    if package is not None:
        scripts = package.get("scripts") or {}
        if not isinstance(scripts, dict):
            detected["package_json_error"] = "package_json_scripts_invalid"
        else:
            manager = (
                "pnpm"
                if (root / "pnpm-lock.yaml").exists()
                else "yarn"
                if (root / "yarn.lock").exists()
                else "npm"
            )
            detected["package_manager"] = manager
            detected["package_json_bytes"] = package_meta["bytes"]
            run_prefix = (
                [manager, "run"]
                if manager != "npm"
                else ["npm", "run"]
            )
            for name in ("test", "typecheck", "lint", "build"):
                if name in scripts:
                    detected["suggested_checks"].append({
                        "id": name,
                        "argv": run_prefix + [name],
                        "cwd": ".",
                    })
    if (root / "pyproject.toml").is_file() and (root / "tests").is_dir():
        detected["suggested_checks"].append({
            "id": "python-tests",
            "argv": [
                "python3", "-m", "unittest",
                "discover", "-s", "tests", "-v",
            ],
            "cwd": ".",
        })
    return detected


def inspect_project(path: Path) -> Dict[str, Any]:
    requested = path.expanduser().resolve()
    if not requested.is_dir():
        raise ValueError("project_directory_not_found")
    root_probe = _safe_git(requested, "rev-parse", "--show-toplevel")
    if root_probe.returncode != 0:
        raise ValueError("not_a_git_repository")
    root = Path(root_probe.stdout.strip()).resolve()
    branch = _safe_git(root, "branch", "--show-current")
    head = _safe_git(root, "rev-parse", "HEAD")
    remote = _safe_git(root, "remote", "get-url", "origin")
    origin_url = remote.stdout.strip() if remote.returncode == 0 else None
    transport = inspect_transport_url(origin_url, root) if origin_url else None
    upstream = _safe_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    status = _safe_git(root, "status", "--porcelain=v2", "--branch", "--untracked-files=normal")
    staged = _safe_git_bytes(root, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--name-only", "-z")
    dirty = _safe_git_bytes(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z")
    untracked = _safe_git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    hooks = _safe_git(root, "config", "--get", "core.hooksPath")
    common_probe = _safe_git(root, "rev-parse", "--git-common-dir")
    if common_probe.returncode == 0 and common_probe.stdout.strip():
        common_raw = Path(common_probe.stdout.strip())
        common_dir = common_raw.resolve() if common_raw.is_absolute() else (root / common_raw).resolve()
    else:
        common_dir = (root / ".git").resolve()
    writer_key = "git:" + hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:32]
    return {
        "requested_path": str(requested),
        "root": str(root),
        "git_common_dir": str(common_dir),
        "writer_key": writer_key,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "origin_url": origin_url,
        "transport": transport,
        "upstream": upstream.stdout.strip() if upstream.returncode == 0 else None,
        "status_porcelain_v2": status.stdout.splitlines() if status.returncode == 0 else [],
        "staged_paths": [os.fsdecode(item) for item in staged.stdout.split(b"\0") if item] if staged.returncode == 0 else [],
        "dirty_tracked_paths": [os.fsdecode(item) for item in dirty.stdout.split(b"\0") if item] if dirty.returncode == 0 else [],
        "untracked_paths": [os.fsdecode(item) for item in untracked.stdout.split(b"\0") if item] if untracked.returncode == 0 else [],
        "hooks_path": hooks.stdout.strip() if hooks.returncode == 0 else None,
        "agents_file": str(root / "AGENTS.md") if (root / "AGENTS.md").is_file() else None,
        "detected": _detect_commands(root),
    }


class ProjectRegistry:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        self.projects_dir = ensure_private_dir(self.home / "projects")

    def _path(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9._-]+", project_id):
            raise ValueError("invalid_project_id")
        return self.projects_dir / f"{project_id}.json"

    def add(self, path: Path, *, name: Optional[str] = None, profile: Optional[str] = None,
            review_mode: Optional[str] = None, reviewer: Optional[str] = None,
            allow_commit: Optional[bool] = None, allow_push: Optional[bool] = None,
            replace: bool = False) -> Dict[str, Any]:
        if profile is None:
            global_config = self.home / "config.json"
            try:
                home_config, _ = read_bounded_json_object(
                    global_config,
                    max_bytes=HOME_CONFIG_MAX_BYTES,
                    unsafe_error="home_config_unsafe",
                    too_large_error="home_config_too_large",
                    invalid_error="home_config_invalid_json",
                )
            except FileNotFoundError:
                profile = "safe"
            else:
                profile = home_config.get("default_profile", "safe")
        if profile not in PROFILE_DEFAULTS:
            raise ValueError("invalid_profile")
        inventory = inspect_project(path)
        root = Path(inventory["root"])
        display_name = name or root.name
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
        project_id = f"{_slug(display_name)}-{digest}"
        target = self._path(project_id)
        if target.exists() and not replace:
            raise ValueError("project_already_registered")
        for candidate in sorted(self.projects_dir.glob("*.json")):
            if candidate == target:
                continue
            try:
                existing_config, _ = read_bounded_json_object(
                    candidate,
                    max_bytes=PROJECT_CONFIG_MAX_BYTES,
                    unsafe_error="project_config_unsafe",
                    too_large_error="project_config_too_large",
                    invalid_error="project_config_invalid_json",
                    repair_mode=0o600,
                )
            except FileNotFoundError as exc:
                raise ValueError(
                    f"project_registry_unreadable:{candidate.stem}"
                ) from exc
            except ValueError as exc:
                if str(exc) == "project_config_unsafe":
                    raise ValueError(
                        f"project_registry_unsafe:{candidate.stem}"
                    ) from exc
                raise ValueError(
                    f"project_registry_unreadable:{candidate.stem}"
                ) from exc
            existing_root = existing_config.get("root")
            if not isinstance(existing_root, str) or not existing_root:
                raise ValueError(f"project_registry_invalid:{candidate.stem}")
            if Path(existing_root).expanduser().resolve() == root:
                owner = existing_config.get("project_id") or candidate.stem
                raise ValueError(f"project_root_already_registered:{owner}")
        defaults = json.loads(json.dumps(PROFILE_DEFAULTS[profile]))
        if review_mode is not None:
            if review_mode not in MODES:
                raise ValueError("invalid_review_mode")
            defaults["review"]["mode"] = review_mode
        if reviewer is not None:
            if reviewer not in REVIEWERS:
                raise ValueError("invalid_reviewer")
            defaults["review"]["reviewer"] = reviewer
        if allow_commit is not None:
            defaults["git"]["allow_commit"] = allow_commit
        if allow_push is not None:
            defaults["git"]["allow_push"] = allow_push
        if defaults["review"]["mode"] == "off":
            defaults["review"]["reviewer"] = "none"
        elif defaults["review"]["reviewer"] == "none":
            raise ValueError("reviewer_required_for_enabled_review")
        protected_paths = {}
        for relative in sorted(set(inventory["staged_paths"] + inventory["dirty_tracked_paths"] + inventory["untracked_paths"])):
            candidate = root / relative
            if candidate.is_file() and not candidate.is_symlink():
                protected_paths[relative] = sha256_file(candidate)
        config = {
            "schema_version": 1,
            "project_id": project_id,
            "name": display_name,
            "root": str(root),
            "writer_key": inventory["writer_key"],
            "profile": profile,
            "git": {
                **defaults["git"],
                "branch": inventory["branch"],
                "remote": "origin" if inventory["origin_url"] else None,
                "remote_url": inventory["origin_url"],
                "ref": inventory["branch"],
                "allow_reset": False,
                "allow_clean": False,
                "allow_stash": False,
            },
            "review": defaults["review"],
            "approval": defaults["approval"],
            "checks": inventory["detected"]["suggested_checks"],
            "protected_paths": protected_paths,
            "inventory_at_registration": inventory,
        }
        atomic_write_json(target, config)
        return {"status": "REGISTERED", "project": config, "config_path": str(target)}

    def list(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for path in sorted(self.projects_dir.glob("*.json")):
            try:
                item, _ = read_bounded_json_object(
                    path,
                    max_bytes=PROJECT_CONFIG_MAX_BYTES,
                    unsafe_error="project_config_unsafe",
                    too_large_error="project_config_too_large",
                    invalid_error="project_config_invalid_json",
                    repair_mode=0o600,
                )
                rows.append({
                    "project_id": item.get("project_id"),
                    "name": item.get("name"),
                    "root": item.get("root"),
                    "profile": item.get("profile"),
                })
            except FileNotFoundError:
                rows.append({"project_id": path.stem, "status": "UNREADABLE"})
            except ValueError as exc:
                status = (
                    "UNSAFE"
                    if str(exc) == "project_config_unsafe"
                    else "UNREADABLE"
                )
                rows.append({"project_id": path.stem, "status": status})
        return rows

    def get(self, project_id: str) -> Dict[str, Any]:
        path = self._path(project_id)
        try:
            config, _ = read_bounded_json_object(
                path,
                max_bytes=PROJECT_CONFIG_MAX_BYTES,
                unsafe_error="project_config_unsafe",
                too_large_error="project_config_too_large",
                invalid_error="project_config_invalid_json",
                repair_mode=0o600,
            )
        except FileNotFoundError as exc:
            raise ValueError("unknown_project") from exc
        if not config.get("writer_key") and config.get("root"):
            root = Path(config["root"]).expanduser().resolve()
            try:
                config["writer_key"] = inspect_project(root)["writer_key"]
            except ValueError:
                config["writer_key"] = "workspace:" + hashlib.sha256(
                    str(root).encode("utf-8")
                ).hexdigest()[:32]
        return config

    def inspect(self, project_id: str) -> Dict[str, Any]:
        config = self.get(project_id)
        return {"project": config, "current": inspect_project(Path(config["root"]))}

    def remove(self, project_id: str) -> Dict[str, Any]:
        path = self._path(project_id)
        if path.is_symlink():
            raise ValueError("project_config_unsafe")
        if not path.is_file():
            raise ValueError("unknown_project")
        path.unlink()
        return {"status": "REMOVED", "project_id": project_id}
