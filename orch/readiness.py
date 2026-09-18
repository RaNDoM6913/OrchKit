from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import (STATE_SCHEMA_VERSION, WRITER_LOCK_RUN_STATES,
                   safe_workspace_path, sha256_file)
from .git_policy import evaluate_project_git_policy
from .project import PROFILE_DEFAULTS, inspect_project
from .review_policy import normalize_review_policy


_TERMINAL_TASK_STATES = {"DONE", "CANCELLED"}
_EXPECTED_PRIVATE_PATHS = (
    (".runtime", 0o700, True),
    (".runtime/logs", 0o700, True),
    (".runtime/worker_receipts", 0o700, True),
    (".runtime/claims", 0o700, True),
    (".runtime/review_exports", 0o700, True),
    (".runtime/git-hooks-disabled", 0o700, True),
    (".runtime/orch.sqlite3", 0o600, True),
    ("projects", 0o700, True),
)


def _finding(
    finding_id: str,
    status: str,
    detail: Any,
    *,
    severity: str = "info",
) -> Dict[str, Any]:
    return {
        "id": finding_id,
        "status": status,
        "severity": severity,
        "detail": detail,
    }


def _read_json_file(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe_or_missing_file")
    return json.loads(path.read_text(encoding="utf-8"))


def _registry_snapshot(home: Path, project_id: str) -> Dict[str, Any]:
    projects = home / "projects"
    target = projects / f"{project_id}.json"
    if projects.is_symlink() or not projects.is_dir():
        raise ValueError("project_registry_missing_or_unsafe")
    if target.is_symlink() or not target.is_file():
        raise ValueError("project_config_missing_or_unsafe")

    configs: List[Dict[str, Any]] = []
    for path in sorted(projects.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"project_registry_unsafe:{path.stem}")
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"project_registry_unreadable:{path.stem}") from exc
        root = item.get("root")
        if (
            not isinstance(item.get("project_id"), str)
            or not isinstance(root, str)
            or not root
        ):
            raise ValueError(f"project_registry_invalid:{path.stem}")
        configs.append({"path": path, "config": item})

    selected = next(
        (item for item in configs if item["path"] == target),
        None,
    )
    if selected is None:
        raise ValueError("unknown_project")
    config = selected["config"]
    if config.get("project_id") != project_id:
        raise ValueError("project_config_identity_mismatch")

    root = Path(config.get("root", "")).expanduser()
    if not root.is_absolute():
        raise ValueError("project_root_not_absolute")
    resolved_root = root.resolve()
    aliases = [
        item["config"].get("project_id")
        for item in configs
        if Path(item["config"]["root"]).expanduser().resolve() == resolved_root
    ]
    if aliases != [project_id]:
        raise ValueError("project_root_alias_conflict:" + ",".join(sorted(aliases)))
    return {
        "path": target,
        "mode": target.stat().st_mode & 0o777,
        "config": config,
        "resolved_root": resolved_root,
        "registry_count": len(configs),
    }


def _permission_findings(home: Path) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for relative, expected, required in _EXPECTED_PRIVATE_PATHS:
        path = home / relative
        if not path.exists():
            if required:
                findings.append(_finding(
                    "state_permission",
                    "BLOCKED",
                    {
                        "path": str(path),
                        "reason": "missing",
                        "expected_mode": oct(expected),
                    },
                    severity="blocker",
                ))
            continue
        if path.is_symlink():
            findings.append(_finding(
                "state_permission",
                "BLOCKED",
                {"path": str(path), "reason": "unsafe_symlink"},
                severity="blocker",
            ))
            continue
        actual = path.stat().st_mode & 0o777
        if actual != expected:
            findings.append(_finding(
                "state_permission",
                "BLOCKED",
                {
                    "path": str(path),
                    "reason": "mode_mismatch",
                    "expected_mode": oct(expected),
                    "actual_mode": oct(actual),
                },
                severity="blocker",
            ))
    return findings


def _open_ledger_read_only(db_path: Path) -> sqlite3.Connection:
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError("state_ledger_missing_or_unsafe")
    uri = "file:" + str(db_path.resolve()) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _task_check_authority(
    workspace: Path,
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    task_results: List[Dict[str, Any]] = []
    blocked = False
    for row in rows:
        if row["status"] in _TERMINAL_TASK_STATES:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            task_results.append({
                "task_id": row["task_id"],
                "status": "BLOCKED",
                "errors": ["invalid_task_payload_json"],
                "checks": [],
            })
            blocked = True
            continue

        check_results: List[Dict[str, Any]] = []
        task_errors: List[str] = []
        checks = payload.get("checks", [])
        if not isinstance(checks, list):
            checks = []
            task_errors.append("invalid_checks")

        for check in checks:
            check_id = str(check.get("id", "unnamed")) if isinstance(check, dict) else "unnamed"
            errors: List[str] = []
            if not isinstance(check, dict):
                errors.append("invalid_check")
                check_results.append({
                    "id": check_id, "status": "BLOCKED", "errors": errors,
                })
                task_errors.extend(f"{check_id}:{item}" for item in errors)
                continue

            executable_value = check.get("executable_path")
            expected_hash = check.get("executable_sha256")
            actual_hash = None
            executable = (
                Path(executable_value)
                if isinstance(executable_value, str)
                else None
            )
            if (
                executable is None
                or not executable.is_absolute()
                or executable.is_symlink()
                or not executable.is_file()
            ):
                errors.append("executable_missing_or_unsafe")
            elif (
                not isinstance(expected_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            ):
                errors.append("executable_hash_missing")
            else:
                actual_hash = sha256_file(executable)
                if actual_hash != expected_hash:
                    errors.append("executable_hash_changed")

            authority_results: List[Dict[str, Any]] = []
            authority_files = check.get("authority_files")
            if not isinstance(authority_files, list):
                errors.append("authority_files_missing")
                authority_files = []
            for record in authority_files:
                if not isinstance(record, dict):
                    errors.append("authority_record_invalid")
                    continue
                relative = record.get("path")
                expected = record.get("expected")
                expected_file_hash = record.get("sha256")
                state = "PASS"
                actual_file_hash = None
                try:
                    path = safe_workspace_path(
                        workspace, relative, must_exist=False
                    )
                except (TypeError, ValueError):
                    state = "INVALID_PATH"
                    path = None
                if path is not None:
                    if path.is_symlink():
                        state = "UNSAFE_SYMLINK"
                    elif expected == "file":
                        if not path.is_file():
                            state = "MISSING"
                        else:
                            actual_file_hash = sha256_file(path)
                            if actual_file_hash != expected_file_hash:
                                state = "HASH_CHANGED"
                    elif expected == "absent":
                        if path.exists():
                            state = "UNEXPECTED_PRESENT"
                    else:
                        state = "INVALID_EXPECTATION"
                authority_results.append({
                    "path": relative,
                    "expected": expected,
                    "status": state,
                    "expected_sha256": expected_file_hash,
                    "actual_sha256": actual_file_hash,
                })
                if state != "PASS":
                    errors.append(f"authority_file:{relative}:{state}")

            check_results.append({
                "id": check_id,
                "status": "BLOCKED" if errors else "PASS",
                "executable_path": (
                    str(executable) if executable is not None else None
                ),
                "expected_executable_sha256": expected_hash,
                "actual_executable_sha256": actual_hash,
                "authority_files": authority_results,
                "errors": errors,
            })
            task_errors.extend(f"{check_id}:{item}" for item in errors)

        if task_errors:
            blocked = True
        task_results.append({
            "task_id": row["task_id"],
            "task_status": row["status"],
            "status": "BLOCKED" if task_errors else "PASS",
            "errors": task_errors,
            "checks": check_results,
        })

    return {
        "status": "BLOCKED" if blocked else "PASS",
        "tasks": task_results,
    }


def _ledger_snapshot(
    home: Path,
    project_id: str,
    writer_key: str,
    workspace: Path,
) -> Dict[str, Any]:
    db_path = home / ".runtime" / "orch.sqlite3"
    with _open_ledger_read_only(db_path) as conn:
        quick = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        foreign = [list(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != STATE_SCHEMA_VERSION:
            return {
                "status": "BLOCKED",
                "reason": "state_schema_mismatch",
                "schema_version": version,
                "expected_schema_version": STATE_SCHEMA_VERSION,
                "quick_check": quick,
                "foreign_key_violations": foreign,
            }

        task_rows = [
            dict(row) for row in conn.execute(
                "SELECT task_id,status,queue_seq,writer_key,updated_at,payload_json "
                "FROM tasks WHERE project_id=? ORDER BY queue_seq,task_id",
                (project_id,),
            ).fetchall()
        ]
        tasks = [
            {key: value for key, value in row.items() if key != "payload_json"}
            for row in task_rows
        ]
        check_authority = _task_check_authority(workspace, task_rows)
        lock_states = tuple(sorted(WRITER_LOCK_RUN_STATES))
        placeholders = ",".join("?" for _ in lock_states)
        locks = [
            dict(row) for row in conn.execute(
                "SELECT r.run_id,r.task_id,r.state,r.heartbeat_at,"
                "t.project_id,t.writer_key "
                "FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                f"WHERE r.state IN ({placeholders}) AND t.writer_key=? "
                "ORDER BY r.started_at",
                (*lock_states, writer_key),
            ).fetchall()
        ]
        pending_publications = [
            dict(row) for row in conn.execute(
                "SELECT p.run_id,p.status,p.commit_id,p.remote_commit,p.error,p.updated_at "
                "FROM publications p JOIN runs r ON r.run_id=p.run_id "
                "JOIN tasks t ON t.task_id=r.task_id "
                "WHERE t.project_id=? "
                "AND p.status NOT IN ('COMPLETE','ABANDONED') "
                "ORDER BY p.updated_at",
                (project_id,),
            ).fetchall()
        ]
        global_pause = conn.execute(
            "SELECT value_json FROM settings WHERE key='paused'"
        ).fetchone()
        project_pause = conn.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (f"project_paused:{project_id}",),
        ).fetchone()
        run_states = {
            row["run_id"]: row["state"]
            for row in conn.execute("SELECT run_id,state FROM runs").fetchall()
        }

    claims = home / ".runtime" / "claims"
    present = (
        {
            path.stem
            for path in claims.glob("*.json")
            if path.is_file() and not path.is_symlink()
        }
        if claims.is_dir() and not claims.is_symlink()
        else set()
    )
    expected_caps = {
        run_id
        for run_id, state in run_states.items()
        if state in {"RUNNING", "RESULT_SUBMITTED"}
    }
    missing_caps = sorted(expected_caps - present)
    orphan_caps = sorted(
        run_id
        for run_id in present
        if run_states.get(run_id) not in {"RUNNING", "RESULT_SUBMITTED"}
    )
    non_nominal_tasks = [
        item for item in tasks
        if item["status"] not in {"PLANNED", "READY", "DONE", "CANCELLED"}
    ]
    return {
        "status": (
            "BLOCKED"
            if quick != ["ok"] or foreign or check_authority["status"] == "BLOCKED"
            else "ATTENTION"
            if (
                missing_caps
                or orphan_caps
                or locks
                or pending_publications
                or global_pause
                or project_pause
                or non_nominal_tasks
            )
            else "READY"
        ),
        "schema_version": version,
        "expected_schema_version": STATE_SCHEMA_VERSION,
        "quick_check": quick,
        "foreign_key_violations": foreign,
        "tasks": tasks,
        "check_authority": check_authority,
        "non_nominal_tasks": non_nominal_tasks,
        "writer_locks": locks,
        "pending_publications": pending_publications,
        "global_pause": json.loads(global_pause["value_json"]) if global_pause else None,
        "project_pause": json.loads(project_pause["value_json"]) if project_pause else None,
        "capabilities": {
            "missing": missing_caps,
            "orphan": orphan_caps,
        },
    }


def audit_project(
    home: Path,
    project_id: str,
    *,
    require_dispatcher: bool = False,
) -> Dict[str, Any]:
    resolved_home = home.expanduser().resolve()
    if re.fullmatch(r"[a-z0-9._-]+", project_id) is None:
        raise ValueError("invalid_project_id")

    findings: List[Dict[str, Any]] = []
    try:
        registry = _registry_snapshot(resolved_home, project_id)
    except Exception as exc:
        return {
            "schema_version": 1,
            "status": "BLOCKED",
            "project_id": project_id,
            "read_only": True,
            "blockers": [str(exc)],
            "attention": [],
            "checks": [
                _finding(
                    "registry",
                    "BLOCKED",
                    str(exc),
                    severity="blocker",
                )
            ],
        }

    config = registry["config"]
    expected_root_suffix = hashlib.sha256(
        str(registry["resolved_root"]).encode("utf-8")
    ).hexdigest()[:8]
    actual_suffix = project_id.rsplit("-", 1)[-1] if "-" in project_id else ""
    if actual_suffix != expected_root_suffix:
        findings.append(_finding(
            "project_root_identity",
            "BLOCKED",
            {
                "project_id_suffix": actual_suffix,
                "expected_root_suffix": expected_root_suffix,
                "root": str(registry["resolved_root"]),
            },
            severity="blocker",
        ))
    else:
        findings.append(_finding(
            "project_root_identity",
            "PASS",
            {
                "root_digest": expected_root_suffix,
                "root": str(registry["resolved_root"]),
            },
        ))

    config = registry["config"]
    findings.append(_finding(
        "registry",
        "PASS",
        {
            "config_path": str(registry["path"]),
            "config_mode": oct(registry["mode"]),
            "registry_count": registry["registry_count"],
            "root": str(registry["resolved_root"]),
        },
    ))
    if registry["mode"] != 0o600:
        findings.append(_finding(
            "project_config_permission",
            "BLOCKED",
            {
                "path": str(registry["path"]),
                "expected_mode": "0o600",
                "actual_mode": oct(registry["mode"]),
            },
            severity="blocker",
        ))

    findings.extend(_permission_findings(resolved_home))

    profile = config.get("profile")
    if profile not in PROFILE_DEFAULTS:
        findings.append(_finding(
            "profile",
            "BLOCKED",
            {"profile": profile, "reason": "invalid_profile"},
            severity="blocker",
        ))
    else:
        findings.append(_finding("profile", "PASS", {"profile": profile}))

    git_config = config.get("git")
    if not isinstance(git_config, dict):
        findings.append(_finding(
            "git_authority",
            "BLOCKED",
            "missing_git_policy",
            severity="blocker",
        ))
    else:
        forbidden_enabled = sorted(
            key
            for key in ("allow_force_push", "allow_reset", "allow_clean", "allow_stash")
            if bool(git_config.get(key))
        )
        if forbidden_enabled:
            findings.append(_finding(
                "git_authority",
                "BLOCKED",
                {"forbidden_enabled": forbidden_enabled},
                severity="blocker",
            ))
        else:
            findings.append(_finding(
                "git_authority",
                "PASS",
                {
                    "allow_commit": bool(git_config.get("allow_commit")),
                    "allow_push": bool(git_config.get("allow_push")),
                    "forbidden_enabled": [],
                },
            ))

    try:
        review = normalize_review_policy(config)
        findings.append(_finding("review_policy", "PASS", review))
    except ValueError as exc:
        findings.append(_finding(
            "review_policy",
            "BLOCKED",
            str(exc),
            severity="blocker",
        ))


    current: Optional[Dict[str, Any]] = None
    try:
        current = inspect_project(registry["resolved_root"])
        configured_writer = config.get("writer_key")
        if configured_writer != current.get("writer_key"):
            findings.append(_finding(
                "writer_identity",
                "BLOCKED",
                {
                    "configured": configured_writer,
                    "current": current.get("writer_key"),
                },
                severity="blocker",
            ))
        else:
            findings.append(_finding(
                "writer_identity",
                "PASS",
                {
                    "writer_key": configured_writer,
                    "git_common_dir": current.get("git_common_dir"),
                },
            ))

        git_policy = evaluate_project_git_policy(config)
        if git_policy["status"] != "READY":
            findings.append(_finding(
                "git_policy",
                "BLOCKED",
                {
                    "safety_blockers": git_policy["safety_blockers"],
                    "push_blockers": git_policy["push_blockers"],
                },
                severity="blocker",
            ))
        else:
            security_push_blockers = [
                item
                for item in git_policy["push_blockers"]
                if item == "remote_url_changed"
                or item.startswith("remote_transport_blocked:")
            ]
            if security_push_blockers:
                findings.append(_finding(
                    "publication_transport",
                    "BLOCKED",
                    {"push_blockers": security_push_blockers},
                    severity="blocker",
                ))
            elif git_policy["push_blockers"]:
                findings.append(_finding(
                    "publication_transport",
                    "ATTENTION",
                    {"push_blockers": git_policy["push_blockers"]},
                    severity="attention",
                ))
            else:
                findings.append(_finding(
                    "publication_transport",
                    "PASS",
                    {
                        "can_commit": git_policy["can_commit"],
                        "can_push": git_policy["can_push"],
                        "transport": git_policy.get("transport"),
                    },
                ))

        current_changes = set(
            current.get("staged_paths", [])
            + current.get("dirty_tracked_paths", [])
            + current.get("untracked_paths", [])
        )
        protected = set((config.get("protected_paths") or {}).keys())
        unprotected = sorted(current_changes - protected)
        if unprotected:
            findings.append(_finding(
                "workspace_baseline",
                "BLOCKED",
                {
                    "unprotected_changes": unprotected,
                    "protected_preexisting": sorted(current_changes & protected),
                },
                severity="blocker",
            ))
        else:
            findings.append(_finding(
                "workspace_baseline",
                "PASS",
                {
                    "protected_preexisting": sorted(current_changes & protected),
                    "unprotected_changes": [],
                },
            ))
    except Exception as exc:
        findings.append(_finding(
            "workspace_git",
            "BLOCKED",
            str(exc),
            severity="blocker",
        ))

    ledger: Optional[Dict[str, Any]] = None
    writer_key = (
        current.get("writer_key")
        if current is not None
        else config.get("writer_key")
    )
    try:
        if not isinstance(writer_key, str) or not writer_key:
            raise ValueError("writer_key_unavailable")
        ledger = _ledger_snapshot(
            resolved_home, project_id, writer_key, registry["resolved_root"]
        )
        ledger_writer_mismatches = [
            item
            for item in ledger.get("tasks", [])
            if item.get("writer_key") != writer_key
        ]
        if ledger_writer_mismatches:
            findings.append(_finding(
                "ledger_writer_identity",
                "BLOCKED",
                {
                    "expected_writer_key": writer_key,
                    "tasks": ledger_writer_mismatches,
                },
                severity="blocker",
            ))
        else:
            findings.append(_finding(
                "ledger_writer_identity",
                "PASS",
                {
                    "expected_writer_key": writer_key,
                    "task_count": len(ledger.get("tasks", [])),
                },
            ))
        authority = ledger.get("check_authority") or {
            "status": "PASS", "tasks": []
        }
        if authority["status"] == "BLOCKED":
            findings.append(_finding(
                "check_execution_authority",
                "BLOCKED",
                authority,
                severity="blocker",
            ))
        else:
            findings.append(_finding(
                "check_execution_authority",
                "PASS",
                authority,
            ))
        if ledger["status"] == "BLOCKED":
            findings.append(_finding(
                "state_ledger",
                "BLOCKED",
                ledger,
                severity="blocker",
            ))
        else:
            findings.append(_finding(
                "state_ledger",
                "PASS" if ledger["status"] == "READY" else "ATTENTION",
                ledger,
                severity="info" if ledger["status"] == "READY" else "attention",
            ))
    except Exception as exc:
        findings.append(_finding(
            "state_ledger",
            "BLOCKED",
            str(exc),
            severity="blocker",
        ))


    rdc_path = resolved_home / "rdc-bootstrap.json"
    if rdc_path.is_symlink():
        findings.append(_finding(
            "rdc_binding",
            "BLOCKED",
            "rdc_marker_unsafe_symlink",
            severity="blocker",
        ))
    elif not rdc_path.is_file():
        findings.append(_finding(
            "rdc_binding",
            "ATTENTION",
            "rdc_marker_unverified",
            severity="attention",
        ))
    else:
        try:
            marker_mode = rdc_path.stat().st_mode & 0o777
            if marker_mode != 0o600:
                raise ValueError(f"rdc_marker_mode:{oct(marker_mode)}")
            marker = json.loads(rdc_path.read_text(encoding="utf-8"))
            if not marker.get("device_id") or not marker.get("device_name"):
                raise ValueError("rdc_marker_invalid")
            findings.append(_finding(
                "rdc_binding",
                "PASS",
                {
                    "device_id": marker["device_id"],
                    "device_name": marker["device_name"],
                    "recorded_at": marker.get("recorded_at"),
                    "mode": "0o600",
                },
            ))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            findings.append(_finding(
                "rdc_binding",
                "BLOCKED",
                str(exc),
                severity="blocker",
            ))

    dispatcher = resolved_home / "dispatchers" / f"{project_id}.txt"
    if dispatcher.is_symlink():
        findings.append(_finding(
            "project_dispatcher",
            "BLOCKED",
            {"path": str(dispatcher), "reason": "unsafe_symlink"},
            severity="blocker",
        ))
    elif dispatcher.is_file():
        mode = dispatcher.stat().st_mode & 0o777
        try:
            text = dispatcher.read_text(encoding="utf-8")
        except OSError as exc:
            text = ""
            dispatcher_error = str(exc)
        else:
            dispatcher_error = None
        required_fragments = (
            f"--project {project_id}",
            f"scheduled-variant-b-{project_id}",
            f"permanently scoped to project_id={project_id}",
        )
        missing_fragments = [
            fragment for fragment in required_fragments if fragment not in text
        ]
        if mode != 0o600 or dispatcher_error or missing_fragments:
            findings.append(_finding(
                "project_dispatcher",
                "BLOCKED",
                {
                    "path": str(dispatcher),
                    "expected_mode": "0o600",
                    "actual_mode": oct(mode),
                    "read_error": dispatcher_error,
                    "missing_scope_fragments": missing_fragments,
                },
                severity="blocker",
            ))
        else:
            findings.append(_finding(
                "project_dispatcher",
                "PASS",
                {
                    "path": str(dispatcher),
                    "mode": "0o600",
                    "scope_bound": True,
                },
            ))
    elif require_dispatcher:
        findings.append(_finding(
            "project_dispatcher",
            "ATTENTION",
            {
                "path": str(dispatcher),
                "reason": "project_dispatcher_missing",
            },
            severity="attention",
        ))
    else:
        findings.append(_finding(
            "project_dispatcher",
            "OPTIONAL_MISSING",
            {"path": str(dispatcher)},
        ))

    blockers = [
        item["id"] + ":" + str(item["detail"])
        for item in findings
        if item["status"] == "BLOCKED"
    ]
    attention = [
        item["id"] + ":" + str(item["detail"])
        for item in findings
        if item["status"] == "ATTENTION"
    ]
    status = "BLOCKED" if blockers else "ATTENTION" if attention else "READY"
    return {
        "schema_version": 1,
        "status": status,
        "project_id": project_id,
        "read_only": True,
        "require_dispatcher": require_dispatcher,
        "blockers": blockers,
        "attention": attention,
        "summary": {
            "profile": profile,
            "workspace": str(registry["resolved_root"]),
            "writer_key": writer_key,
            "task_count": len(ledger.get("tasks", [])) if ledger else None,
            "writer_lock_count": len(ledger.get("writer_locks", [])) if ledger else None,
            "pending_publication_count": (
                len(ledger.get("pending_publications", [])) if ledger else None
            ),
            "global_pause": ledger.get("global_pause") if ledger else None,
            "project_pause": ledger.get("project_pause") if ledger else None,
        },
        "checks": findings,
    }
