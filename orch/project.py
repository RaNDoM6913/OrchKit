from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import atomic_write_json
from .core import sha256_file
from .review_policy import MODES, REVIEWERS

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


def _safe_git(repo: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    cmd = [
        "git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        "-c", "diff.external=", "-C", str(repo), *args,
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)



def _safe_git_bytes(repo: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    cmd = [
        "git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        "-c", "diff.external=", "-C", str(repo), *args,
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=False, timeout=timeout, check=False)

def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return normalized or "project"


def _detect_commands(root: Path) -> Dict[str, Any]:
    detected: Dict[str, Any] = {"package_manager": None, "suggested_checks": []}
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = package.get("scripts") or {}
            manager = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
            detected["package_manager"] = manager
            run_prefix = [manager, "run"] if manager != "npm" else ["npm", "run"]
            for name in ("test", "typecheck", "lint", "build"):
                if name in scripts:
                    detected["suggested_checks"].append({"id": name, "argv": run_prefix + [name], "cwd": "."})
        except (OSError, json.JSONDecodeError):
            detected["package_json_error"] = True
    if (root / "pyproject.toml").is_file() and (root / "tests").is_dir():
        detected["suggested_checks"].append({"id": "python-tests", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], "cwd": "."})
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
        "origin_url": remote.stdout.strip() if remote.returncode == 0 else None,
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
        self.projects_dir = self.home / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

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
            if global_config.is_file():
                try:
                    profile = json.loads(global_config.read_text(encoding="utf-8")).get("default_profile", "safe")
                except (OSError, json.JSONDecodeError):
                    profile = "safe"
            else:
                profile = "safe"
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
                item = json.loads(path.read_text(encoding="utf-8"))
                rows.append({"project_id": item.get("project_id"), "name": item.get("name"), "root": item.get("root"), "profile": item.get("profile")})
            except (OSError, json.JSONDecodeError):
                rows.append({"project_id": path.stem, "status": "UNREADABLE"})
        return rows

    def get(self, project_id: str) -> Dict[str, Any]:
        path = self._path(project_id)
        if not path.is_file():
            raise ValueError("unknown_project")
        config = json.loads(path.read_text(encoding="utf-8"))
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
        if not path.is_file():
            raise ValueError("unknown_project")
        path.unlink()
        return {"status": "REMOVED", "project_id": project_id}
