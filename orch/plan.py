from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .config import atomic_write_json
from .core import normalize_relative_path
from .git_policy import evaluate_project_git_policy


def build_single_task_plan(
    project: Dict[str, Any], *, task_id: str, goal: str, allowed_paths: Iterable[str],
    risk_tags: Iterable[str] = (), owner_acceptance: bool = False,
    plan_revision: Optional[str] = None,
) -> Dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
        raise ValueError("invalid_task_id")
    if not goal.strip():
        raise ValueError("goal_required")
    allowed = []
    for item in allowed_paths:
        if not str(item).strip():
            continue
        allowed.append(normalize_relative_path(str(item)))
    if not allowed:
        raise ValueError("allowed_paths_required")
    git_state = evaluate_project_git_policy(project)
    if git_state["status"] != "READY":
        raise ValueError("project_git_policy_blocked:" + ",".join(git_state["safety_blockers"]))
    git_config = project.get("git") or {}
    if git_state["can_push"]:
        publication = {
            "kind": "git",
            "branch": git_config.get("branch"),
            "remote": git_config.get("remote") or "origin",
            "ref": git_config.get("ref") or git_config.get("branch"),
            "expected_base": git_state["current"].get("head"),
            "commit_message": f"orch: {task_id}",
        }
    elif git_state["can_commit"]:
        publication = {
            "kind": "git_local",
            "branch": git_config.get("branch"),
            "expected_base": git_state["current"].get("head"),
            "commit_message": f"orch: {task_id}",
        }
    else:
        publication = {"kind": "none"}
    review = dict(project.get("review") or {"mode": "off", "reviewer": "none", "placement": "pre_publish"})
    review["risk_tags"] = sorted(set(str(item) for item in risk_tags if str(item)))
    revision = plan_revision or f"{project['project_id']}-{task_id.lower()}-{uuid.uuid4().hex[:8]}"
    task = {
        "id": task_id,
        "goal": goal.strip(),
        "non_goals": [],
        "workspace": project["root"],
        "dependencies": [],
        "allowed_paths": allowed,
        "protected_paths": project.get("protected_paths", {}),
        "checks": project.get("checks", []),
        "review": review,
        "owner_acceptance": bool(owner_acceptance),
        "publication": publication,
        "max_attempts": 2,
    }
    return {"schema_version": 1, "plan_revision": revision, "tasks": [task]}


def write_plan(path: Path, plan: Dict[str, Any]) -> Dict[str, Any]:
    atomic_write_json(path.resolve(), plan, mode=0o600)
    return {"status": "CREATED", "path": str(path.resolve()), "plan_revision": plan["plan_revision"], "task_id": plan["tasks"][0]["id"]}
