from __future__ import annotations

import re
import shlex
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import HOME_CONFIG_MAX_BYTES, read_bounded_json_object
from .dispatcher import RDC_MARKER_MAX_BYTES, validate_rdc_marker
from .project import (
    PROJECT_CONFIG_MAX_BYTES,
    project_root_identity_matches,
    validate_project_schema,
)
from .readiness import audit_project, state_permission_findings


def _command(home: Path, suffix: str) -> str:
    return f"orch --root {shlex.quote(str(home))} {suffix}"


def _step(
    step_id: str,
    *,
    reason: str,
    command: Optional[str] = None,
    instruction: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"id": step_id, "reason": reason}
    if command is not None:
        result["command"] = command
    if instruction is not None:
        result["instruction"] = instruction
    return result


def _read_config(home: Path) -> Optional[Dict[str, Any]]:
    if home.is_symlink() or (home.exists() and not home.is_dir()):
        raise ValueError("orch_home_unsafe")
    path = home / "config.json"
    try:
        config, meta = read_bounded_json_object(
            path,
            max_bytes=HOME_CONFIG_MAX_BYTES,
            unsafe_error="home_config_unsafe",
            too_large_error="home_config_too_large",
            invalid_error="home_config_invalid_json",
        )
    except FileNotFoundError:
        return None
    if meta["mode"] != 0o600:
        raise ValueError(f"home_config_mode:{oct(meta['mode'])}")
    if config.get("schema_version") != 1:
        raise ValueError("home_config_schema_unsupported")
    return {
        "status": "CONFIGURED",
        "path": str(path),
        "profile": config.get("default_profile"),
        "variant": config.get("variant"),
    }


def _existing_authority_without_config(home: Path) -> Optional[str]:
    projects = home / "projects"
    if projects.is_symlink():
        return "project_registry_missing_or_unsafe"
    if projects.exists():
        if not projects.is_dir():
            return "project_registry_missing_or_unsafe"
        if any(projects.iterdir()):
            return "home_config_missing_with_existing_project_authority"

    runtime = home / ".runtime"
    if runtime.is_symlink():
        return "state_runtime_unsafe"
    if runtime.exists():
        if not runtime.is_dir():
            return "state_runtime_unsafe"
        if any(runtime.iterdir()):
            return "home_config_missing_with_existing_state"
    return None


def _read_projects(home: Path) -> List[Dict[str, Any]]:
    projects_dir = home / "projects"
    try:
        info = projects_dir.lstat()
    except FileNotFoundError as exc:
        raise ValueError("project_registry_missing_or_unsafe") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("project_registry_missing_or_unsafe")

    projects: List[Dict[str, Any]] = []
    for config_path in sorted(projects_dir.glob("*.json")):
        try:
            config, meta = read_bounded_json_object(
                config_path,
                max_bytes=PROJECT_CONFIG_MAX_BYTES,
                unsafe_error="project_config_unsafe",
                too_large_error="project_config_too_large",
                invalid_error="project_config_invalid_json",
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"project_registry_unreadable:{config_path.stem}"
            ) from exc
        try:
            validate_project_schema(config)
        except ValueError as exc:
            raise ValueError(
                f"project_registry_invalid:{config_path.stem}"
            ) from exc
        if meta["mode"] != 0o600:
            raise ValueError(
                f"project_registry_permission:{config_path.stem}"
            )
        project_id = config.get("project_id")
        root = config.get("root")
        if (
            project_id != config_path.stem
            or not isinstance(root, str)
            or not root
        ):
            raise ValueError(
                f"project_registry_invalid:{config_path.stem}"
            )
        root_path = Path(root).expanduser()
        if (
            not root_path.is_absolute()
            or not project_root_identity_matches(project_id, root_path)
        ):
            raise ValueError(
                f"project_registry_invalid:{config_path.stem}"
            )
        projects.append({
            "project_id": project_id,
            "name": config.get("name"),
            "profile": config.get("profile"),
            "root": str(root_path.resolve()),
        })
    return projects


def _read_rdc(home: Path) -> Dict[str, Any]:
    path = home / "rdc-bootstrap.json"
    try:
        marker, meta = read_bounded_json_object(
            path,
            max_bytes=RDC_MARKER_MAX_BYTES,
            unsafe_error="rdc_marker_unsafe",
            too_large_error="rdc_marker_too_large",
            invalid_error="rdc_marker_invalid_json",
        )
    except FileNotFoundError:
        return {"status": "UNVERIFIED"}
    except ValueError as exc:
        return {"status": "BLOCKED", "reason": str(exc)}
    if meta["mode"] != 0o600:
        return {
            "status": "BLOCKED",
            "reason": f"rdc_marker_mode:{oct(meta['mode'])}",
        }
    try:
        validate_rdc_marker(marker)
    except ValueError as exc:
        return {"status": "BLOCKED", "reason": str(exc)}
    return {
        "status": "RECORDED",
        "device_name": marker["device_name"],
        "recorded_at": marker.get("recorded_at"),
    }


def _state_summary(home: Path) -> Dict[str, Any]:
    runtime = home / ".runtime"
    if runtime.is_symlink():
        return {"status": "BLOCKED", "blocked_check_count": 1}
    if not runtime.exists():
        return {"status": "NOT_INITIALIZED"}
    if not runtime.is_dir():
        return {"status": "BLOCKED", "blocked_check_count": 1}
    findings = state_permission_findings(home)
    blocked = [
        item for item in findings
        if item.get("status") == "BLOCKED"
    ]
    return {
        "status": "BLOCKED" if blocked else "READY",
        "blocked_check_count": len(blocked),
    }


def _audit_summary(audit: Dict[str, Any]) -> Dict[str, Any]:
    summary = audit.get("summary") or {}
    return {
        "status": audit.get("status"),
        "blocker_count": len(audit.get("blockers") or []),
        "attention_count": len(audit.get("attention") or []),
        "profile": summary.get("profile"),
        "workspace": summary.get("workspace"),
        "task_count": summary.get("task_count"),
        "active_writer_count": summary.get("writer_lock_count"),
        "pending_publication_count": summary.get(
            "pending_publication_count"
        ),
        "global_pause": summary.get("global_pause"),
        "project_pause": summary.get("project_pause"),
        "checks": [
            {"id": item.get("id"), "status": item.get("status")}
            for item in audit.get("checks") or []
        ],
    }


def _check_status(
    audit: Optional[Dict[str, Any]],
    check_id: str,
) -> Optional[str]:
    if audit is None:
        return None
    for item in audit.get("checks") or []:
        if item.get("id") == check_id:
            return item.get("status")
    return None


def operator_overview(
    home: Path,
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_home = home.expanduser().resolve()
    if (
        project_id is not None
        and re.fullmatch(r"[a-z0-9._-]+", project_id) is None
    ):
        raise ValueError("invalid_project_id")

    try:
        config = _read_config(resolved_home)
    except ValueError as exc:
        return {
            "schema_version": 1,
            "status": "BLOCKED",
            "read_only": True,
            "home": str(resolved_home),
            "reason": str(exc),
            "next_steps": [],
        }

    if config is None:
        authority_problem = (
            _existing_authority_without_config(resolved_home)
            if resolved_home.exists()
            else None
        )
        if authority_problem is not None:
            return {
                "schema_version": 1,
                "status": "BLOCKED",
                "read_only": True,
                "home": str(resolved_home),
                "config": {"status": "MISSING"},
                "reason": authority_problem,
                "next_steps": [],
            }
        return {
            "schema_version": 1,
            "status": "SETUP_REQUIRED",
            "read_only": True,
            "home": str(resolved_home),
            "config": {"status": "MISSING"},
            "rdc": {"status": "UNVERIFIED"},
            "projects": [],
            "state": {"status": "NOT_INITIALIZED"},
            "next_steps": [
                _step(
                    "setup",
                    command=_command(
                        resolved_home,
                        "setup --profile safe",
                    ),
                    reason=(
                        "Initialize the private OrchKit home first."
                    ),
                )
            ],
        }

    try:
        projects = _read_projects(resolved_home)
    except ValueError as exc:
        return {
            "schema_version": 1,
            "status": "BLOCKED",
            "read_only": True,
            "home": str(resolved_home),
            "config": config,
            "reason": str(exc),
            "next_steps": [],
        }

    selected = None
    if project_id is not None:
        selected = next(
            (
                item for item in projects
                if item["project_id"] == project_id
            ),
            None,
        )
        if selected is None:
            raise ValueError("unknown_project")

    rdc = _read_rdc(resolved_home)
    state = _state_summary(resolved_home)
    project_audit = None
    if project_id is not None:
        project_audit = _audit_summary(
            audit_project(
                resolved_home,
                project_id,
                require_dispatcher=True,
            )
        )

    blocked = (
        rdc.get("status") == "BLOCKED"
        or state.get("status") == "BLOCKED"
        or (
            project_audit is not None
            and project_audit.get("status") == "BLOCKED"
        )
    )
    attention = (
        rdc.get("status") != "RECORDED"
        or not projects
        or (
            project_audit is not None
            and project_audit.get("status") == "ATTENTION"
        )
    )

    steps: List[Dict[str, Any]] = []
    if rdc.get("status") == "UNVERIFIED":
        steps.append(_step(
            "connect_rdc",
            command=_command(
                resolved_home,
                "rdc bootstrap-prompt",
            ),
            reason=(
                "Record the real ChatGPT-to-RDC device binding "
                "before dispatching work."
            ),
        ))

    if not projects:
        steps.append(_step(
            "register_project",
            command=_command(
                resolved_home,
                "project add /absolute/path/to/repository "
                "--review-mode off",
            ),
            reason=(
                "Register one Git workspace. Review mode off is "
                "the subscription-free core path."
            ),
        ))
    elif project_id is None:
        steps.append(_step(
            "inspect_project",
            command=_command(
                resolved_home,
                "overview --project "
                + shlex.quote(projects[0]["project_id"]),
            ),
            reason=(
                "Inspect one registered project before dispatching "
                "work."
            ),
        ))
    elif project_audit is not None:
        if project_audit.get("status") == "BLOCKED":
            steps.append(_step(
                "inspect_blockers",
                command=_command(
                    resolved_home,
                    "project audit "
                    + shlex.quote(project_id)
                    + " --require-dispatcher",
                ),
                reason=(
                    "Project readiness is blocked; inspect the "
                    "read-only audit before changing state."
                ),
            ))
        else:
            dispatcher_status = _check_status(
                project_audit,
                "project_dispatcher",
            )
            if dispatcher_status != "PASS":
                steps.append(_step(
                    "render_dispatcher",
                    command=_command(
                        resolved_home,
                        "dispatcher render --project "
                        + shlex.quote(project_id),
                    ),
                    reason=(
                        "Create the project-scoped prompt used by a "
                        "fresh ordinary ChatGPT worker."
                    ),
                ))

            task_count = project_audit.get("task_count")
            active = project_audit.get("active_writer_count") or 0
            pending = (
                project_audit.get("pending_publication_count") or 0
            )
            paused = bool(
                project_audit.get("global_pause")
                or project_audit.get("project_pause")
            )
            if task_count == 0:
                steps.append(_step(
                    "add_work",
                    command=_command(
                        resolved_home,
                        "queue enqueue --help",
                    ),
                    reason=(
                        "No task is queued. Define a bounded goal "
                        "and allowed paths first."
                    ),
                ))
            elif active or pending or paused:
                steps.append(_step(
                    "inspect_recovery",
                    command=_command(
                        resolved_home,
                        "recovery inspect --project "
                        + shlex.quote(project_id),
                    ),
                    reason=(
                        "Active, paused, or publication state needs "
                        "operator inspection before new work."
                    ),
                ))
            else:
                steps.append(_step(
                    "inspect_queue",
                    command=_command(
                        resolved_home,
                        "queue list --project "
                        + shlex.quote(project_id),
                    ),
                    instruction=(
                        "If the queue reports a READY task, use the "
                        "rendered dispatcher in a fresh ordinary "
                        "ChatGPT conversation over RDC."
                    ),
                    reason=(
                        "Queued work exists; inspect the durable "
                        "queue state before launching a worker."
                    ),
                ))

    return {
        "schema_version": 1,
        "status": (
            "BLOCKED"
            if blocked
            else "ATTENTION"
            if attention
            else "READY"
        ),
        "read_only": True,
        "home": str(resolved_home),
        "config": config,
        "rdc": rdc,
        "state": state,
        "projects": projects,
        "selected_project": selected,
        "project_audit": project_audit,
        "next_steps": steps,
    }
