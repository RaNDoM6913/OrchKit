from __future__ import annotations

import re
import shlex
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import HOME_CONFIG_MAX_BYTES, read_bounded_json_object
from .bridge_local_broker import LocalRunBroker, _validate_overview_limit
from .bridge_process_store import (_MAX_ACK_BYTES, _MAX_RECORD_BYTES,
                                   _chains, _ledger_binding)
from .core import STATE_SCHEMA_VERSION
from .dispatcher import RDC_MARKER_MAX_BYTES, validate_rdc_marker
from .project import (
    PROJECT_CONFIG_MAX_BYTES,
    project_root_identity_matches,
    validate_project_schema,
)
from .readiness import (_open_ledger_read_only, audit_project,
                        state_permission_findings)
from .worker_operations import WorkerRunBinding


_RECORDED_RUN_STATES = frozenset({
    "RUNNING", "RESULT_SUBMITTED", "QUIESCING", "VERIFYING", "REVIEWING",
    "VERIFIED", "NEEDS_FIX", "BLOCKED", "ABORTED", "COMPLETE",
})
_RECORDED_TASK_STATES = frozenset({
    "PLANNED", "READY", "NEEDS_FIX", "BLOCKED", "IN_PROGRESS",
    "WAITING_REVIEW", "READY_TO_PUBLISH", "WAITING_OWNER", "DONE", "CANCELLED",
})
_PROCESS_EVENT_KINDS = (
    "PROCESS_DISPATCH_INTENT", "PROCESS_EXIT_OBSERVED", "PROCESS_CLOSURE_ACKNOWLEDGED",
)


def _bridge_recorded_projection(home: Path, limit: int) -> tuple[dict, dict]:
    """Inspect existing native data, including current WAL, without repair.

    SQLite mode=ro can maintain its SHM bookkeeping index. This is logically
    read-only native data, not a universal filesystem-immutability guarantee.
    The instruction budget also bounds the frozen chain validator's JSON
    cross-binding scan, whose work is not bounded by its returned-row LIMIT.
    """
    native = {"status": "UNKNOWN", "coverage": "UNKNOWN", "runs": []}
    processes = {
        "status": "UNKNOWN", "coverage": "UNKNOWN", "runs": [],
        "count_scope": "VALIDATED_VISIBLE_CHAINS_ONLY",
        "observed_unresolved_dispatch_count": None,
    }
    native_loaded = False

    def blocked(reason):
        native.update(status="BLOCKED", coverage="UNKNOWN", reason=reason)
        processes.update(status="BLOCKED", coverage="UNKNOWN",
                         observed_unresolved_dispatch_count=None)
        return native, processes

    try:
        if home.is_symlink() or (home.exists() and not home.is_dir()):
            return blocked("state_home_unsafe")
        runtime = home / ".runtime"
        if not runtime.exists() and not runtime.is_symlink():
            native["status"] = "NOT_INITIALIZED"
            return native, processes
        if any(item.get("status") == "BLOCKED"
               for item in state_permission_findings(home)):
            return blocked("state_permissions_unsafe")

        with closing(_open_ledger_read_only(runtime / "orch.sqlite3")) as conn:
            budget = 0

            def bounded_sql():
                nonlocal budget
                budget += 1
                return int(budget >= 1000)

            conn.set_progress_handler(bounded_sql, 1000)
            # One read transaction keeps run/task/chain observations coherent.
            conn.execute("BEGIN")
            if conn.execute("PRAGMA user_version").fetchone()[0] != STATE_SCHEMA_VERSION:
                return blocked("state_schema_mismatch")
            required_columns = {
                "runs": {"run_id", "task_id", "attempt", "state"},
                "tasks": {"task_id", "project_id", "writer_key", "status"},
                "events": {"seq", "kind", "task_id", "run_id", "payload_json"},
            }
            for table, required in required_columns.items():
                actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if not required <= actual:
                    return blocked("state_schema_mismatch")

            # Direct-column octet_length reads the stored byte count without
            # loading the value. Require UTF-8 so it matches the frozen chain
            # validator's UTF-8 limits. Missing octet_length (SQLite <3.43) is
            # handled by the existing fail-closed sqlite3.Error path; no fallback.
            if conn.execute("PRAGMA encoding").fetchone()[0] != "UTF-8":
                return blocked("state_encoding_unsupported")
            unsafe_native = conn.execute(
                "SELECT 1 FROM runs WHERE "
                "typeof(run_id)<>'text' OR octet_length(run_id)>200 OR "
                "typeof(task_id)<>'text' OR octet_length(task_id)>200 OR "
                "typeof(attempt)<>'integer' OR attempt NOT BETWEEN 1 AND 1000000 OR "
                "typeof(state)<>'text' OR octet_length(state)>128 "
                "UNION ALL SELECT 1 FROM tasks WHERE "
                "typeof(task_id)<>'text' OR octet_length(task_id)>200 OR "
                "typeof(project_id)<>'text' OR octet_length(project_id)>160 OR "
                "typeof(writer_key)<>'text' OR octet_length(writer_key)>200 OR "
                "typeof(status)<>'text' OR octet_length(status)>128 LIMIT 1"
            ).fetchone()
            if unsafe_native is not None:
                return blocked("native_record_bytes_unsafe")

            run_rows = conn.execute(
                "SELECT run_id FROM runs ORDER BY run_id LIMIT ?", (limit + 1,),
            ).fetchall()
            native["coverage"] = "TRUNCATED" if len(run_rows) > limit else "COMPLETE"
            native["status"] = "UNKNOWN" if len(run_rows) > limit else "RECORDED"
            bindings = {}
            for row in run_rows[:limit]:
                try:
                    recorded, binding = _ledger_binding(conn, row["run_id"])
                    binding = asdict(WorkerRunBinding(**binding))
                except (TypeError, ValueError):
                    native.update(status="BLOCKED", reason="native_binding_invalid")
                    continue
                bindings[row["run_id"]] = binding
                native["runs"].append({
                    "binding": binding,
                    "run_state": (recorded["state"]
                                  if recorded["state"] in _RECORDED_RUN_STATES else "UNKNOWN"),
                    "task_state": (recorded["task_state"]
                                   if recorded["task_state"] in _RECORDED_TASK_STATES else "UNKNOWN"),
                })
            native_loaded = True

            # The frozen validator can JSON-scan records for any run, including
            # malformed/foreign row bindings. Gate all process-event metadata
            # before DISTINCT IDs, JSON parsing or payload materialization.
            unsafe_process = conn.execute(
                "SELECT 1 FROM events WHERE kind IN (?,?,?) AND ("
                "typeof(run_id)<>'text' OR octet_length(run_id)>200 OR "
                "typeof(task_id)<>'text' OR octet_length(task_id)>200 OR "
                "typeof(payload_json)<>'text' OR octet_length(payload_json)>"
                "CASE WHEN kind=? THEN ? ELSE ? END) LIMIT 1",
                (*_PROCESS_EVENT_KINDS, "PROCESS_CLOSURE_ACKNOWLEDGED",
                 _MAX_ACK_BYTES, _MAX_RECORD_BYTES),
            ).fetchone()
            if unsafe_process is not None:
                processes.update(status="BLOCKED", coverage="UNKNOWN",
                                 reason="process_record_bytes_unsafe",
                                 observed_unresolved_dispatch_count=None)
                return native, processes

            # Include inactive/orphan process-bearing runs rather than treating
            # only native writers or only locally routed runs as capacity.
            process_rows = conn.execute(
                "SELECT DISTINCT run_id FROM events WHERE kind IN (?,?,?) "
                "ORDER BY run_id LIMIT ?", (*_PROCESS_EVENT_KINDS, limit + 1),
            ).fetchall()
            candidates = list(dict.fromkeys(
                [row["run_id"] for row in process_rows] + list(bindings)
            ))
            processes["coverage"] = (
                "UNKNOWN" if native["status"] == "BLOCKED" else
                "TRUNCATED" if (native["coverage"] == "TRUNCATED"
                                or len(process_rows) > limit or len(candidates) > limit)
                else "COMPLETE"
            )
            processes["status"] = "RECORDED"
            unresolved = 0
            for run_id in candidates[:limit]:
                binding = bindings.get(run_id)
                try:
                    _, ledger_binding = _ledger_binding(conn, run_id)
                    binding = asdict(WorkerRunBinding(**ledger_binding))
                    intents, exits = _chains(conn, run_id, binding)
                except (TypeError, ValueError, RecursionError):
                    processes["status"] = "BLOCKED"
                    item = {"status": "BLOCKED", "reason": "process_records_invalid"}
                    if binding is not None:
                        item["binding"] = binding
                    processes["runs"].append(item)
                    continue
                pending = len(intents) - len(exits)
                unresolved += pending
                processes["runs"].append({
                    "binding": binding,
                    "status": ("UNKNOWN" if pending else "RECORDED_EXITS"
                               if intents else "NO_PROCESS_RECORDS"),
                    "recorded_dispatch_count": len(intents),
                    "recorded_exit_count": len(exits),
                    "unresolved_dispatch_count": pending,
                })
            if processes["status"] != "BLOCKED":
                processes["observed_unresolved_dispatch_count"] = unresolved
                if unresolved or processes["coverage"] != "COMPLETE":
                    processes["status"] = "UNKNOWN"
            return native, processes
    except sqlite3.Error:
        if native_loaded:
            processes.update(status="BLOCKED", coverage="UNKNOWN",
                             reason="state_query_unavailable",
                             observed_unresolved_dispatch_count=None)
            return native, processes
        return blocked("state_query_unavailable")
    except (OSError, ValueError, TypeError):
        return blocked("state_ledger_unavailable")


def operator_overview(
    home: Path,
    *,
    project_id: Optional[str] = None,
    broker: Optional[LocalRunBroker] = None,
    limit: int = 32,
) -> Dict[str, Any]:
    """Keep legacy setup/readiness semantics and add independent bridge facts.

    A supplied broker is a trusted in-process object, used only by its owner.
    Recorded states and metadata do not certify current capability authority,
    a connected ChatGPT worker, a physical process state or safe global capacity.
    """
    _validate_overview_limit(limit)
    if broker is not None and not isinstance(broker, LocalRunBroker):
        raise ValueError("bridge_overview_invalid_broker")
    routing = (broker.overview_projection(home=home, limit=limit)
               if broker is not None else {
                   "scope": "THIS_BROKER_ONLY", "status": "UNKNOWN",
                   "coverage": "UNKNOWN", "unlisted_route_state": "UNKNOWN",
                   "external_connectivity": "UNKNOWN", "contexts": [],
               })
    result = _operator_overview(home, project_id=project_id)
    native, processes = _bridge_recorded_projection(home.expanduser(), limit)
    result["bridge"] = {
        "schema_version": 1, "read_only": True,
        "external_connectivity": "UNKNOWN",
        "native": native, "routing": routing, "processes": processes,
    }
    return result


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


def _operator_overview(
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
