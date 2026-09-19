from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import (atomic_write_json, read_bounded_json_object,
                     read_bounded_regular_file)
from .core import (PLAN_MAX_BYTES, PLAN_MAX_TASKS, normalize_relative_path,
                   validate_task_definition)
from .git_policy import evaluate_project_git_policy


def build_single_task_plan(
    project: Dict[str, Any], *, task_id: str, goal: str, allowed_paths: Iterable[str],
    risk_tags: Iterable[str] = (), owner_acceptance: bool = False,
    plan_revision: Optional[str] = None, dependencies: Iterable[str] = (),
    max_attempts: int = 2, bind_expected_base: bool = True,
) -> Dict[str, Any]:
    if (
        not isinstance(task_id, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,160}", task_id) is None
    ):
        raise ValueError("invalid_task_id")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal_required")
    deps = []
    for dep in dependencies:
        if not isinstance(dep, str):
            raise ValueError("invalid_dependency")
        value = dep.strip()
        if (
            re.fullmatch(r"[A-Za-z0-9._-]{1,160}", value) is None
            or value == task_id
        ):
            raise ValueError("invalid_dependency")
        if value not in deps:
            deps.append(value)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
        raise ValueError("invalid_max_attempts")
    allowed = []
    for item in allowed_paths:
        if not isinstance(item, str):
            raise ValueError("invalid_allowed_path")
        if not item.strip():
            continue
        allowed.append(normalize_relative_path(item))
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
    normalized_risk_tags = []
    for item in risk_tags:
        if not isinstance(item, str) or not item:
            raise ValueError("invalid_risk_tags")
        normalized_risk_tags.append(item)
    review["risk_tags"] = sorted(set(normalized_risk_tags))
    if plan_revision is not None and (
        not isinstance(plan_revision, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,200}", plan_revision) is None
    ):
        raise ValueError("invalid_plan_revision")
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
    validate_task_definition(task)
    return {"schema_version": 1, "plan_revision": revision, "tasks": [task]}


def existing_plan_initial_base_binding(path: Path) -> Optional[bool]:
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    try:
        data, _ = read_bounded_json_object(
            target,
            max_bytes=PLAN_MAX_BYTES,
            unsafe_error="plan_artifact_not_regular",
            too_large_error="plan_artifact_too_large",
            invalid_error="plan_artifact_invalid_json",
        )
    except FileNotFoundError:
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], dict):
        raise ValueError("plan_artifact_invalid")
    publication = tasks[0].get("publication")
    if not isinstance(publication, dict):
        raise ValueError("plan_artifact_invalid")
    return "expected_base" in publication


def write_plan(path: Path, plan: Dict[str, Any], *, replace: bool = True) -> Dict[str, Any]:
    encoded = (
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > PLAN_MAX_BYTES:
        raise ValueError("plan_artifact_too_large")
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    if target.is_symlink():
        raise ValueError("plan_artifact_not_regular")
    if not replace:
        try:
            existing, _ = read_bounded_json_object(
                target,
                max_bytes=PLAN_MAX_BYTES,
                unsafe_error="plan_artifact_not_regular",
                too_large_error="plan_artifact_too_large",
                invalid_error="plan_artifact_invalid_json",
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != plan:
                raise ValueError("plan_artifact_conflict")
            return {
                "status": "EXISTS", "path": str(target),
                "plan_revision": plan["plan_revision"],
                "task_id": plan["tasks"][0]["id"],
            }
    atomic_write_json(target, plan, mode=0o600)
    return {
        "status": "CREATED", "path": str(target),
        "plan_revision": plan["plan_revision"], "task_id": plan["tasks"][0]["id"],
    }

BATCH_MANIFEST_MAX_BYTES = 256 * 1024


def read_batch_manifest(path: Path) -> Tuple[Dict[str, Any], str, int]:
    try:
        raw, _ = read_bounded_regular_file(
            path,
            max_bytes=BATCH_MANIFEST_MAX_BYTES,
            unsafe_error="batch_manifest_not_regular",
            too_large_error="batch_manifest_too_large",
        )
    except FileNotFoundError as exc:
        raise ValueError("batch_manifest_not_regular") from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_batch_manifest_json") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid_batch_manifest")
    unknown = sorted(set(data) - {"schema_version", "plan_revision", "tasks"})
    if unknown:
        raise ValueError("unknown_batch_manifest_field:" + unknown[0])
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("invalid_batch_manifest")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("empty_batch_manifest")
    if len(tasks) > PLAN_MAX_TASKS:
        raise ValueError("batch_manifest_too_many_tasks")
    return data, digest, len(raw)


def build_batch_plan(
    project: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    source_digest: str,
    source_bytes: Optional[int] = None,
    bind_initial_base: bool = True,
) -> Dict[str, Any]:
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("empty_batch_manifest")
    if len(tasks) > PLAN_MAX_TASKS:
        raise ValueError("batch_manifest_too_many_tasks")
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
        allowed_fields = {
            "id", "goal", "allowed_paths", "dependencies", "risk_tags",
            "owner_acceptance", "max_attempts",
        }
        extra = sorted(set(raw) - allowed_fields)
        if extra:
            raise ValueError("unknown_batch_task_field:" + extra[0])
        task_id = raw.get("id")
        if (
            not isinstance(task_id, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,160}", task_id) is None
        ):
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
            "source_bytes": source_bytes,
            "task_count": len(compiled),
        },
        "tasks": compiled,
    }
