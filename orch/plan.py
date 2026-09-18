from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import atomic_write_json
from .core import normalize_relative_path
from .git_policy import evaluate_project_git_policy


def build_single_task_plan(
    project: Dict[str, Any], *, task_id: str, goal: str, allowed_paths: Iterable[str],
    risk_tags: Iterable[str] = (), owner_acceptance: bool = False,
    plan_revision: Optional[str] = None, dependencies: Iterable[str] = (),
    max_attempts: int = 2, bind_expected_base: bool = True,
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
        if bind_expected_base and not deps:
            publication["expected_base"] = git_state["current"].get("head")
    elif git_state["can_commit"]:
        publication = {
            "kind": "git_local",
            "branch": git_config.get("branch"),
            "commit_message": f"orch: {task_id}",
        }
        if bind_expected_base and not deps:
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


def existing_plan_initial_base_binding(path: Path) -> Optional[bool]:
    target = path.expanduser().resolve()
    if not target.exists():
        return None
    if target.is_symlink() or not target.is_file():
        raise ValueError("plan_artifact_not_regular")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("plan_artifact_invalid_json") from exc
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], dict):
        raise ValueError("plan_artifact_invalid")
    publication = tasks[0].get("publication")
    if not isinstance(publication, dict):
        raise ValueError("plan_artifact_invalid")
    return "expected_base" in publication


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

BATCH_MANIFEST_MAX_BYTES = 256 * 1024


def read_batch_manifest(path: Path) -> Tuple[Dict[str, Any], str]:
    source = path.expanduser()
    if source.is_symlink() or not source.is_file():
        raise ValueError("batch_manifest_not_regular")
    size = source.stat().st_size
    if size > BATCH_MANIFEST_MAX_BYTES:
        raise ValueError("batch_manifest_too_large")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_batch_manifest_json") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("invalid_batch_manifest")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("empty_batch_manifest")
    return data, digest


def build_batch_plan(
    project: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    source_digest: str,
    bind_initial_base: bool = True,
) -> Dict[str, Any]:
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("empty_batch_manifest")
    ids: List[str] = []
    compiled: List[Dict[str, Any]] = []
    revision = manifest.get("plan_revision")
    if revision is not None and (
        not isinstance(revision, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", revision)
    ):
        raise ValueError("invalid_plan_revision")
    revision = revision or (
        f"{project['project_id']}-batch-{source_digest[:12]}-{uuid.uuid4().hex[:8]}"
    )

    for index, raw in enumerate(tasks):
        if not isinstance(raw, dict):
            raise ValueError("invalid_batch_task")
        task_id = raw.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
            raise ValueError("invalid_task_id")
        if task_id in ids:
            raise ValueError("duplicate_batch_task_id:" + task_id)
        ids.append(task_id)
        goal = raw.get("goal")
        allowed_paths = raw.get("allowed_paths")
        dependencies = raw.get("dependencies", [])
        risk_tags = raw.get("risk_tags", [])
        owner_acceptance = raw.get("owner_acceptance", False)
        max_attempts = raw.get("max_attempts", 2)
        if not isinstance(goal, str):
            raise ValueError("goal_required")
        if not isinstance(allowed_paths, list):
            raise ValueError("allowed_paths_required")
        if not isinstance(dependencies, list):
            raise ValueError("invalid_dependency")
        if not isinstance(risk_tags, list):
            raise ValueError("invalid_risk_tags")
        if not isinstance(owner_acceptance, bool):
            raise ValueError("invalid_owner_acceptance")
        single = build_single_task_plan(
            project,
            task_id=task_id,
            goal=goal,
            allowed_paths=allowed_paths,
            risk_tags=risk_tags,
            owner_acceptance=owner_acceptance,
            plan_revision=revision,
            dependencies=dependencies,
            max_attempts=max_attempts,
            bind_expected_base=bool(bind_initial_base and index == 0),
        )
        compiled.append(single["tasks"][0])

    return {
        "schema_version": 1,
        "plan_revision": revision,
        "adapter": {
            "kind": "batch_manifest_v1",
            "source_sha256": source_digest,
            "task_count": len(compiled),
        },
        "tasks": compiled,
    }
