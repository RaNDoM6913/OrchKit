from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .core import sha256_file
from .project import inspect_project


def evaluate_project_git_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    current = inspect_project(Path(config["root"]))
    policy = config.get("git") or {}
    safety_blockers: List[str] = []
    push_blockers: List[str] = []
    expected_branch = policy.get("branch")
    if expected_branch and current.get("branch") != expected_branch:
        safety_blockers.append("branch_mismatch")
    if current.get("staged_paths"):
        safety_blockers.append("staging_not_empty")
    protected = config.get("protected_paths") or {}
    changed_protected: List[str] = []
    for relative, expected in protected.items():
        path = Path(config["root"]) / relative
        actual = sha256_file(path) if path.is_file() and not path.is_symlink() else None
        if actual != expected:
            changed_protected.append(relative)
    if changed_protected:
        safety_blockers.append("protected_baseline_changed")
    if policy.get("allow_push") and not current.get("origin_url"):
        push_blockers.append("remote_missing")
    can_commit = bool(policy.get("allow_commit")) and not safety_blockers
    can_push = bool(policy.get("allow_push")) and can_commit and not push_blockers
    return {
        "project_id": config.get("project_id"),
        "status": "BLOCKED" if safety_blockers else "READY",
        "can_commit": can_commit,
        "can_push": can_push,
        "force_push_allowed": False,
        "safety_blockers": safety_blockers,
        "push_blockers": push_blockers,
        "changed_protected_paths": changed_protected,
        "current": current,
    }
