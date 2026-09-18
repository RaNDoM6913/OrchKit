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
    plan_revision: Optional[str] = None, dependencies: Iterable[str] = (),
    max_attempts: int = 2,
) -> Dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
        raise ValueError("invalid_task_id")
    if not goal.strip():
        raise ValueError("goal_required")
    deps = []
    for dep in dependencies:
        value = str(dep).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or value == task_id:
            raise ValueError("invalid_dependency")
        if value not in deps:
            deps.append(value)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
        raise ValueError("invalid_max_attempts")
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
            "remote_url": git_state["transport"]["canonical_url"],
            "transport_kind": git_state["transport"]["kind"],
            "ref": git_config.get("ref") or git_config.get("branch"),
            "commit_message": f"orch: {task_id}",
        }
        if not deps:
            publication["expected_base"] = git_state["current"].get("head")
    elif git_state["can_commit"]:
        publication = {
            "kind": "git_local",
            "branch": git_config.get("branch"),
            "commit_message": f"orch: {task_id}",
        }
        if not deps:
            publication["expected_base"] = git_state["current"].get("head")
    else:
        publication = {"kind": "none"}
    review = dict(project.get("review") or {"mode": "off", "reviewer": "none", "placement": "pre_publish"})
    review["risk_tags"] = sorted(set(str(item) for item in risk_tags if str(item)))
    revision = plan_revision or f"{project['project_id']}-{task_id.lower()}-{uuid.uuid4().hex[:8]}"
    task = {
        "id": task_id,
        "project_id": project["project_id"],
        "writer_key": project.get("writer_key"),
        "goal": goal.strip(),
        "non_goals": [],
        "workspace": project["root"],
        "dependencies": deps,
        "allowed_paths": allowed,
        "protected_paths": project.get("protected_paths", {}),
        "checks": project.get("checks", []),
        "review": review,
        "owner_acceptance": bool(owner_acceptance),
        "publication": publication,
        "max_attempts": max_attempts,
    }
    return {"schema_version": 1, "plan_revision": revision, "tasks": [task]}


def write_plan(path: Path, plan: Dict[str, Any], *, replace: bool = True) -> Dict[str, Any]:
    target = path.resolve()
    if target.exists() and not replace:
        if target.is_symlink() or not target.is_file():
            raise ValueError("plan_artifact_not_regular")
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != plan:
            raise ValueError("plan_artifact_conflict")
        return {
            "status": "EXISTS", "path": str(target),
            "plan_revision": plan["plan_revision"], "task_id": plan["tasks"][0]["id"],
        }
    atomic_write_json(target, plan, mode=0o600)
    return {
        "status": "CREATED", "path": str(target),
        "plan_revision": plan["plan_revision"], "task_id": plan["tasks"][0]["id"],
    }
