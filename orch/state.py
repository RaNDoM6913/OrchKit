from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat as statmod
import sqlite3
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import zipfile

from .config import (atomic_write_json, ensure_private_dir,
                     read_bounded_json_object, regular_file_read_flags)
from .core import (ACTIVE_RUN_STATES, STATE_SCHEMA_VERSION, WRITER_LOCK_RUN_STATES,
                   Orchestrator, sha256_file, utc_now)

REPLACEMENT_JOURNAL_MAX_BYTES = 64 * 1024
RESTORE_RECEIPT_MAX_BYTES = 64 * 1024



def migration_history(orch: Orchestrator) -> Dict[str, Any]:
    with orch.connect() as conn:
        rows = conn.execute(
            "SELECT version,from_version,applied_at,details_json FROM schema_migrations ORDER BY version"
        ).fetchall()
    migrations = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        migrations.append(item)
    return {
        "status": "OK",
        "current_version": STATE_SCHEMA_VERSION,
        "migrations": migrations,
    }

def capability_health(orch: Orchestrator) -> Dict[str, Any]:
    claims = orch.runtime / "claims"
    file_identities: Dict[str, List[int]] = {}
    directory_identity: Optional[List[int]] = None
    if claims.is_symlink():
        directory_status = "UNSAFE_SYMLINK"
    elif not claims.exists():
        directory_status = "MISSING"
    elif not claims.is_dir():
        directory_status = "NOT_DIRECTORY"
    else:
        try:
            claims_fd = _open_claims_directory(orch)
        except ValueError:
            directory_status = "CHANGED"
        else:
            try:
                info = os.fstat(claims_fd)
                with os.scandir(claims_fd) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json"):
                            continue
                        file_info = os.stat(
                            entry.name, dir_fd=claims_fd,
                            follow_symlinks=False,
                        )
                        if statmod.S_ISREG(file_info.st_mode):
                            file_identities[Path(entry.name).stem] = (
                                _stat_identity(file_info)
                            )
                directory_identity = [int(info.st_dev), int(info.st_ino)]
                directory_status = "READY"
            except OSError:
                file_identities = {}
                directory_status = "CHANGED"
            finally:
                os.close(claims_fd)
    with orch.connect() as conn:
        rows = conn.execute("SELECT run_id,state FROM runs").fetchall()
    states = {row["run_id"]: row["state"] for row in rows}
    expected = {rid for rid, state in states.items() if state in {"RUNNING", "RESULT_SUBMITTED"}}
    present = set(file_identities)
    orphan = sorted(rid for rid in present if states.get(rid) not in {"RUNNING", "RESULT_SUBMITTED"})
    missing = sorted(expected - present)
    return {
        "status": "ATTENTION" if orphan or missing or directory_status != "READY" else "READY",
        "directory_status": directory_status,
        "directory_identity": directory_identity,
        "orphan_capabilities": orphan,
        "orphan_file_identities": {
            run_id: file_identities[run_id] for run_id in orphan
        },
        "missing_capabilities": missing,
        "expected_live_capabilities": sorted(expected),
    }


def permission_health(orch: Orchestrator) -> Dict[str, Any]:
    targets = [
        (orch.runtime, 0o700, True, "directory"),
        (orch.runtime / "logs", 0o700, True, "directory"),
        (orch.runtime / "worker_receipts", 0o700, True, "directory"),
        (orch.runtime / "claims", 0o700, True, "directory"),
        (orch.runtime / "review_exports", 0o700, True, "directory"),
        (orch.runtime / "git-hooks-disabled", 0o700, True, "directory"),
        (orch.db_path, 0o600, True, "file"),
        (orch.db_path.with_name(orch.db_path.name + "-wal"), 0o600, False, "file"),
        (orch.db_path.with_name(orch.db_path.name + "-shm"), 0o600, False, "file"),
        (orch.root / "projects", 0o700, False, "directory"),
        (orch.root / "plans", 0o700, False, "directory"),
        (orch.root / "backups", 0o700, False, "directory"),
    ]
    rows: List[Dict[str, Any]] = []
    blocked = False
    for path, expected, required, kind in targets:
        if path.is_symlink():
            blocked = True
            rows.append({
                "path": str(path), "status": "UNSAFE_SYMLINK",
                "expected_mode": oct(expected), "actual_mode": None,
            })
            continue
        if not path.exists():
            if required:
                blocked = True
                rows.append({
                    "path": str(path), "status": "MISSING",
                    "expected_mode": oct(expected), "actual_mode": None,
                })
            continue
        actual = path.stat().st_mode & 0o777
        if kind == "directory" and not path.is_dir():
            status = "NOT_DIRECTORY"
        elif kind == "file" and not path.is_file():
            status = "NOT_FILE"
        else:
            status = "PASS" if actual == expected else "MODE_MISMATCH"
        blocked = blocked or status != "PASS"
        rows.append({
            "path": str(path), "status": status,
            "expected_mode": oct(expected), "actual_mode": oct(actual),
        })
    return {"status": "BLOCKED" if blocked else "READY", "paths": rows}


def check_state(orch: Orchestrator) -> Dict[str, Any]:
    with orch.connect() as conn:
        quick = conn.execute("PRAGMA quick_check").fetchall()
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        active = [dict(row) for row in conn.execute(
            "SELECT r.run_id,r.task_id,r.state,r.heartbeat_at,t.project_id,t.writer_key "
            "FROM runs r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.state IN (?,?,?,?,?) ORDER BY r.started_at",
            tuple(ACTIVE_RUN_STATES),
        )]
        lock_placeholders = ",".join("?" for _ in WRITER_LOCK_RUN_STATES)
        writer_locks = [dict(row) for row in conn.execute(
            "SELECT r.run_id,r.task_id,r.state,r.heartbeat_at,t.project_id,t.writer_key "
            "FROM runs r JOIN tasks t ON t.task_id=r.task_id "
            f"WHERE r.state IN ({lock_placeholders}) ORDER BY r.started_at",
            tuple(sorted(WRITER_LOCK_RUN_STATES)),
        )]
        pending_publications = [dict(row) for row in conn.execute(
            "SELECT run_id,status,commit_id,remote_commit,error,updated_at FROM publications "
            "WHERE status NOT IN ('COMPLETE','ABANDONED') ORDER BY updated_at"
        )]
    quick_values = [row[0] for row in quick]
    caps = capability_health(orch)
    permissions = permission_health(orch)
    blocked = (
        quick_values != ["ok"] or bool(foreign)
        or user_version != STATE_SCHEMA_VERSION
        or permissions["status"] != "READY"
        or caps["directory_status"] != "READY"
    )
    attention = bool(writer_locks) or caps["status"] != "READY" or bool(pending_publications)
    history = migration_history(orch)["migrations"]
    return {
        "status": "BLOCKED" if blocked else "ATTENTION" if attention else "READY",
        "schema_version": user_version,
        "expected_schema_version": STATE_SCHEMA_VERSION,
        "migration_history": history,
        "quick_check": quick_values,
        "foreign_key_violations": [list(row) for row in foreign],
        "journal_mode": journal_mode,
        "active_runs": active,
        "writer_locks": writer_locks,
        "pending_publications": pending_publications,
        "capabilities": caps,
        "permissions": permissions,
    }


def recovery_inspect(
    orch: Orchestrator, *, run_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    import re
    import time

    if run_id is not None and re.fullmatch(r"[A-Za-z0-9._-]+", run_id) is None:
        raise ValueError("invalid_run_id")
    if project_id is not None and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
        raise ValueError("invalid_project_id")

    with orch.connect() as conn:
        query = (
            "SELECT r.run_id,r.task_id,r.attempt,r.worker_id,r.state,r.started_at,"
            "r.heartbeat_at,r.submitted_at,r.snapshot_id,r.verify_status,r.review_status,"
            "r.completed_at,r.error,t.status AS task_status,t.project_id,t.writer_key,"
            "t.payload_json FROM runs r JOIN tasks t ON t.task_id=r.task_id"
        )
        clauses: List[str] = []
        params: List[str] = []
        if run_id is not None:
            clauses.append("r.run_id=?")
            params.append(run_id)
        if project_id is not None:
            clauses.append("t.project_id=?")
            params.append(project_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY r.started_at"
        rows = conn.execute(query, tuple(params)).fetchall()
        if run_id is not None and not rows:
            raise ValueError("unknown_run")
        publications = {
            row["run_id"]: dict(row)
            for row in conn.execute(
                "SELECT run_id,status,kind,expected_base,commit_id,remote_commit,error,updated_at "
                "FROM publications ORDER BY updated_at"
            ).fetchall()
        }
        approvals = {
            row["run_id"]: row["snapshot_id"]
            for row in conn.execute("SELECT run_id,snapshot_id FROM approvals").fetchall()
        }
        checkpoints: Dict[str, Dict[str, Any]] = {}
        for checkpoint_row in conn.execute(
            "SELECT key,value_json FROM settings WHERE key LIKE 'run_checkpoint:%'"
        ).fetchall():
            checkpoint_run_id = checkpoint_row["key"][len("run_checkpoint:"):]
            try:
                checkpoint_value = json.loads(checkpoint_row["value_json"])
            except (TypeError, json.JSONDecodeError):
                checkpoints[checkpoint_run_id] = {"status": "INVALID"}
                continue
            reason = (
                checkpoint_value.get("reason")
                if isinstance(checkpoint_value, dict) else None
            )
            process_state = (
                checkpoint_value.get("process_state")
                if isinstance(checkpoint_value, dict) else None
            )
            recorded_at = (
                checkpoint_value.get("recorded_at")
                if isinstance(checkpoint_value, dict) else None
            )
            if (
                not isinstance(checkpoint_value, dict)
                or checkpoint_value.get("run_id") != checkpoint_run_id
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 500
                or process_state not in {"active", "unknown"}
                or not isinstance(recorded_at, str)
                or not recorded_at
            ):
                checkpoints[checkpoint_run_id] = {"status": "INVALID"}
                continue
            checkpoints[checkpoint_run_id] = {
                "status": "RECORDED",
                "reason": reason,
                "process_state": process_state,
                "recorded_at": recorded_at,
                "writer_reservation_released": False,
                "capability_revoked": False,
                "safe_to_resume_elsewhere": False,
            }

    caps = capability_health(orch)
    claims_ready = caps["directory_status"] == "READY"
    now = time.time()
    items: List[Dict[str, Any]] = []
    for row in rows:
        publication = publications.get(row["run_id"])
        unresolved_publication = (
            publication is not None
            and publication["status"] not in {"COMPLETE", "ABANDONED"}
        )
        if (
            run_id is None
            and row["state"] not in WRITER_LOCK_RUN_STATES
            and not unresolved_publication
        ):
            continue

        payload = json.loads(row["payload_json"])
        capability = orch.runtime / "claims" / f"{row['run_id']}.json"
        capability_present = (
            claims_ready and capability.exists()
            and capability.is_file() and not capability.is_symlink()
        )
        heartbeat_ts = _parse_utc(row["heartbeat_at"])
        heartbeat_age = (
            max(0, int(now - heartbeat_ts)) if heartbeat_ts is not None else None
        )
        commands: List[str] = []
        classification = "TERMINAL"
        checkpoint = checkpoints.get(row["run_id"])

        if unresolved_publication:
            classification = "PUBLICATION_RECONCILIATION_REQUIRED"
            commands = [
                f"orch publish-reconcile --run-id {row['run_id']}",
                "Use --resume only when reconciliation reports resume_available=true.",
            ]
        elif row["state"] == "RUNNING":
            if checkpoint is not None:
                if checkpoint.get("process_state") == "active":
                    classification = "CHECKPOINTED_WORKER_ACTIVE"
                    commands = [
                        "Writer reservation is retained; continue only in the same observed worker.",
                        "Do not abort/retry or start another worker while external/direct-RDC activity is active.",
                    ]
                else:
                    classification = "CHECKPOINTED_PROCESS_STATE_UNKNOWN"
                    commands = [
                        "Verify the external ChatGPT/RDC process state; checkpoint does not prove inactivity.",
                        "Do not abort/retry or start another worker until process inactivity is independently proven.",
                    ]
            elif capability_present:
                classification = "WORKER_MAY_STILL_BE_ACTIVE"
                commands = [
                    "Verify the external ChatGPT/RDC worker state; heartbeat age is informational only.",
                    "Do not abort/retry or start another worker until process inactivity is independently proven.",
                ]
            else:
                classification = "CAPABILITY_MISSING"
                commands = [
                    "Do not recreate a lease/capability file.",
                    "Do not abort/retry or start another worker until external process inactivity is independently proven.",
                ]
        elif row["state"] == "RESULT_SUBMITTED":
            if capability_present:
                classification = "RESULT_AWAITING_QUIESCE"
                commands = [
                    f"orch quiesce --run-id {row['run_id']} --cap <capability_file>",
                    f"orch verify --run-id {row['run_id']}",
                ]
            else:
                classification = "CAPABILITY_MISSING"
                commands = [
                    "Do not recreate a lease/capability file.",
                    f"orch abort --run-id {row['run_id']} --reason <reason> --retry",
                ]
        elif row["state"] == "VERIFYING":
            classification = "VERIFY_RESUMABLE"
            commands = [f"orch verify --run-id {row['run_id']}"]
        elif row["state"] == "REVIEWING":
            classification = "REVIEW_DECISION_REQUIRED"
            commands = [
                f"orch review-decision --run-id {row['run_id']}",
                "Run the configured reviewer only when policy/billing preflight permits it.",
            ]
        elif row["state"] == "VERIFIED":
            approved = approvals.get(row["run_id"]) == row["snapshot_id"]
            if payload.get("owner_acceptance") and not approved:
                classification = "OWNER_DECISION_REQUIRED"
                commands = [
                    f"orch approve --run-id {row['run_id']} --note <note>",
                    "Do not release the verified writer reservation without an explicit disposition.",
                ]
            elif payload.get("publication", {}).get("kind", "none") in {"git", "git_local"}:
                classification = "PUBLICATION_READY"
                commands = [f"orch publish --run-id {row['run_id']}"]
            else:
                classification = "COMPLETION_READY"
                commands = [f"orch complete --run-id {row['run_id']}"]

        item = {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "project_id": row["project_id"],
            "attempt": row["attempt"],
            "worker_id": row["worker_id"],
            "run_state": row["state"],
            "task_status": row["task_status"],
            "writer_key": row["writer_key"],
            "started_at": row["started_at"],
            "heartbeat_at": row["heartbeat_at"],
            "heartbeat_age_seconds": heartbeat_age,
            "capability_expected": row["state"] in {"RUNNING", "RESULT_SUBMITTED"},
            "capability_present": capability_present,
            "snapshot_id": row["snapshot_id"],
            "classification": classification,
            "checkpoint": checkpoint,
            "publication": publication,
            "safe_next_steps": commands,
        }
        items.append(item)

    attention = any(
        item["classification"] != "TERMINAL" for item in items
    ) or caps["status"] != "READY"
    return {
        "status": "ATTENTION" if attention else "CLEAN",
        "run_id": run_id,
        "project_id": project_id,
        "automatic_expiry": False,
        "rule": (
            "Heartbeat age never expires a writer automatically. Observe durable state and "
            "use only the explicit next step that matches it."
        ),
        "items": items,
        "capabilities": caps,
    }


def prune_capabilities(orch: Orchestrator) -> Dict[str, Any]:
    health = check_state(orch)
    if health["active_runs"]:
        raise ValueError("active_runs_present")
    directory_status = health["capabilities"]["directory_status"]
    if directory_status != "READY":
        raise ValueError("capability_directory_unsafe:" + directory_status)
    try:
        claims_fd = _open_claims_directory(orch)
    except ValueError as exc:
        raise ValueError("capability_directory_unsafe:CHANGED") from exc
    pruned: List[str] = []
    try:
        info = os.fstat(claims_fd)
        if health["capabilities"]["directory_identity"] != [
            int(info.st_dev), int(info.st_ino)
        ]:
            raise ValueError("capability_directory_unsafe:CHANGED")
        for run_id in health["capabilities"]["orphan_capabilities"]:
            name = f"{run_id}.json"
            expected_identity = health["capabilities"][
                "orphan_file_identities"
            ].get(run_id)
            try:
                _validate_child_name(name)
                info = os.stat(
                    name, dir_fd=claims_fd, follow_symlinks=False
                )
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"capability_artifact_changed:{run_id}"
                ) from exc
            if (
                not statmod.S_ISREG(info.st_mode)
                or _stat_identity(info) != expected_identity
            ):
                raise ValueError(f"capability_artifact_changed:{run_id}")
            for _ in range(32):
                quarantine = ".orch-cap-prune-" + secrets.token_hex(16) + ".json"
                if not _entry_exists_at(claims_fd, quarantine):
                    break
            else:
                raise ValueError("capability_quarantine_name_unavailable")
            try:
                os.rename(
                    name, quarantine,
                    src_dir_fd=claims_fd, dst_dir_fd=claims_fd,
                )
            except OSError as exc:
                raise ValueError(
                    f"capability_artifact_changed:{run_id}"
                ) from exc
            try:
                quarantined = os.stat(
                    quarantine, dir_fd=claims_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ValueError(
                    f"capability_artifact_changed:{run_id}"
                ) from exc
            if (
                not statmod.S_ISREG(quarantined.st_mode)
                or _stat_identity(quarantined)[:5] != expected_identity[:5]
            ):
                _restore_quarantine_at(claims_fd, quarantine, name)
                raise ValueError(f"capability_artifact_changed:{run_id}")
            try:
                os.unlink(quarantine, dir_fd=claims_fd)
            except OSError as exc:
                _restore_quarantine_at(claims_fd, quarantine, name)
                raise ValueError(
                    f"capability_delete_failed:{run_id}"
                ) from exc
            pruned.append(run_id)
    finally:
        os.close(claims_fd)
    return {"status": "PRUNED", "count": len(pruned), "run_ids": pruned}



def _parse_utc(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    import datetime
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _stat_identity(info: os.stat_result) -> List[int]:
    return [
        int(info.st_dev), int(info.st_ino), int(info.st_mode),
        int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns),
    ]


def _regular_file_flags() -> int:
    return regular_file_read_flags()


def _record_open_regular_fd(fd: int) -> Optional[Dict[str, Any]]:
    before = os.fstat(fd)
    if not statmod.S_ISREG(before.st_mode):
        return None
    digest = hashlib.sha256()
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    after = os.fstat(fd)
    if _stat_identity(after) != _stat_identity(before):
        return None
    return {
        "bytes": int(before.st_size),
        "identity": _stat_identity(before),
        "sha256": digest.hexdigest(),
    }


def _regular_file_record(path: Path) -> Optional[Dict[str, Any]]:
    target = Path(os.path.abspath(str(path)))
    if not hasattr(os, "O_NOFOLLOW") and target.is_symlink():
        return None
    try:
        fd = os.open(str(target), _regular_file_flags())
    except OSError:
        return None
    try:
        return _record_open_regular_fd(fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _regular_file_size(path: Path) -> Optional[int]:
    record = _regular_file_record(path)
    return record["bytes"] if record is not None else None


def _safe_tree_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        root_info = path.lstat()
    except FileNotFoundError:
        return None
    if not statmod.S_ISDIR(root_info.st_mode):
        return None
    total = 0
    identity: List[Dict[str, Any]] = [
        {"path": ".", "stat": _stat_identity(root_info)}
    ]
    for item in sorted(path.rglob("*")):
        try:
            info = item.lstat()
        except FileNotFoundError:
            return None
        if statmod.S_ISLNK(info.st_mode):
            return None
        if not (statmod.S_ISREG(info.st_mode) or statmod.S_ISDIR(info.st_mode)):
            return None
        relative = item.relative_to(path).as_posix()
        if statmod.S_ISREG(info.st_mode):
            record = _regular_file_record(item)
            if record is None:
                return None
            identity.append({
                "path": relative,
                "stat": record["identity"],
                "sha256": record["sha256"],
            })
            total += int(record["bytes"])
        else:
            identity.append({
                "path": relative, "stat": _stat_identity(info)
            })
    return {"bytes": total, "identity": identity}


def _safe_tree_size(path: Path) -> Optional[int]:
    record = _safe_tree_record(path)
    return record["bytes"] if record is not None else None


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _validate_child_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or name in {"", ".", ".."}
        or "/" in name
        or "\x00" in name
    ):
        raise ValueError("retention_entry_name_unsafe")


def _open_directory_path(path: Path) -> int:
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise ValueError("retention_anchor_unsafe")
    try:
        fd = os.open(str(path), _directory_flags())
    except OSError as exc:
        raise ValueError("retention_anchor_unsafe") from exc
    info = os.fstat(fd)
    if not statmod.S_ISDIR(info.st_mode):
        os.close(fd)
        raise ValueError("retention_anchor_unsafe")
    return fd


def _open_directory_at(parent_fd: int, name: str) -> int:
    _validate_child_name(name)
    try:
        fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError("retention_anchor_unsafe") from exc
    info = os.fstat(fd)
    if not statmod.S_ISDIR(info.st_mode):
        os.close(fd)
        raise ValueError("retention_anchor_unsafe")
    return fd


def _open_claims_directory(orch: Orchestrator) -> int:
    root_fd = _open_directory_path(orch.root)
    try:
        runtime_fd = _open_directory_at(root_fd, ".runtime")
        try:
            return _open_directory_at(runtime_fd, "claims")
        finally:
            os.close(runtime_fd)
    finally:
        os.close(root_fd)


def _regular_file_record_at(
    parent_fd: int, name: str,
) -> Optional[Dict[str, Any]]:
    _validate_child_name(name)
    try:
        fd = os.open(
            name, _regular_file_flags(), dir_fd=parent_fd
        )
    except OSError:
        return None
    try:
        return _record_open_regular_fd(fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _tree_record_from_fd(root_fd: int) -> Optional[Dict[str, Any]]:
    root_info = os.fstat(root_fd)
    if not statmod.S_ISDIR(root_info.st_mode):
        return None
    identity: List[Dict[str, Any]] = [
        {"path": ".", "stat": _stat_identity(root_info)}
    ]
    total = 0

    def walk(directory_fd: int, prefix: str) -> bool:
        nonlocal total
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            return False
        for entry in entries:
            name = entry.name
            try:
                _validate_child_name(name)
                info = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                return False
            if statmod.S_ISLNK(info.st_mode):
                return False
            relative = name if not prefix else prefix + "/" + name
            if statmod.S_ISREG(info.st_mode):
                record = _regular_file_record_at(directory_fd, name)
                if record is None:
                    return False
                identity.append({
                    "path": relative,
                    "stat": record["identity"],
                    "sha256": record["sha256"],
                })
                total += int(record["bytes"])
                continue
            if not statmod.S_ISDIR(info.st_mode):
                return False
            try:
                child_fd = _open_directory_at(directory_fd, name)
            except ValueError:
                return False
            try:
                child_info = os.fstat(child_fd)
                identity.append({
                    "path": relative,
                    "stat": _stat_identity(child_info),
                })
                if not walk(child_fd, relative):
                    return False
            finally:
                os.close(child_fd)
        return True

    if not walk(root_fd, ""):
        return None
    return {"bytes": total, "identity": identity}


def _tree_record_at(
    parent_fd: int, name: str,
) -> Optional[Dict[str, Any]]:
    try:
        fd = _open_directory_at(parent_fd, name)
    except ValueError:
        return None
    try:
        return _tree_record_from_fd(fd)
    finally:
        os.close(fd)


def _retention_anchor_path(orch: Orchestrator, kind: str) -> Path:
    if kind == "log":
        return orch.logs
    if kind == "worker_receipt":
        return orch.runtime / "worker_receipts"
    if kind == "review_export":
        return orch.runtime / "review_exports"
    if kind == "backup":
        return orch.root / "backups"
    raise ValueError("retention_artifact_kind_invalid")


def _open_retention_anchor(orch: Orchestrator, kind: str) -> int:
    root_fd = _open_directory_path(orch.root)
    try:
        if kind == "backup":
            return _open_directory_at(root_fd, "backups")
        runtime_fd = _open_directory_at(root_fd, ".runtime")
        try:
            child = {
                "log": "logs",
                "worker_receipt": "worker_receipts",
                "review_export": "review_exports",
            }.get(kind)
            if child is None:
                raise ValueError("retention_artifact_kind_invalid")
            return _open_directory_at(runtime_fd, child)
        finally:
            os.close(runtime_fd)
    finally:
        os.close(root_fd)


def _entry_exists_at(parent_fd: int, name: str) -> bool:
    _validate_child_name(name)
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _artifact_expected_record(artifact: Dict[str, Any]) -> Dict[str, Any]:
    record = {
        "bytes": artifact.get("bytes"),
        "identity": artifact.get("identity"),
    }
    if "sha256" in artifact:
        record["sha256"] = artifact.get("sha256")
    return record


def _artifact_record_at(
    anchor_fd: int, kind: str, name: str,
) -> Optional[Dict[str, Any]]:
    if kind == "review_export":
        return _tree_record_at(anchor_fd, name)
    return _regular_file_record_at(anchor_fd, name)


def _artifact_binding(
    orch: Orchestrator, artifact: Dict[str, Any],
) -> Dict[str, Any]:
    kind = artifact.get("kind")
    path_value = artifact.get("path")
    if not isinstance(kind, str) or not isinstance(path_value, str):
        return {"status": "CHANGED", "reason": "invalid_binding"}
    try:
        anchor_path = _retention_anchor_path(orch, kind)
    except ValueError:
        return {"status": "CHANGED", "reason": "invalid_kind"}
    path = Path(path_value)
    if (
        Path(os.path.abspath(str(path.parent)))
        != Path(os.path.abspath(str(anchor_path)))
    ):
        return {"status": "CHANGED", "reason": "anchor_mismatch"}
    name = path.name
    try:
        _validate_child_name(name)
        anchor_fd = _open_retention_anchor(orch, kind)
    except ValueError:
        return {"status": "CHANGED", "reason": "unsafe_anchor"}
    try:
        exists = _entry_exists_at(anchor_fd, name)
        if not exists:
            return {"status": "MISSING", "name": name}
        current = _artifact_record_at(anchor_fd, kind, name)
    finally:
        os.close(anchor_fd)
    expected = _artifact_expected_record(artifact)
    if current is None or current != expected:
        return {
            "status": "CHANGED",
            "name": name,
            "reason": "identity_mismatch",
        }
    return {"status": "MATCH", "name": name}


def _record_matches_after_quarantine(
    kind: str,
    expected: Dict[str, Any],
    observed: Optional[Dict[str, Any]],
) -> bool:
    if observed is None or observed.get("bytes") != expected.get("bytes"):
        return False
    expected_identity = expected.get("identity")
    observed_identity = observed.get("identity")
    if kind != "review_export":
        if (
            not isinstance(expected_identity, list)
            or not isinstance(observed_identity, list)
            or len(expected_identity) != 6
            or len(observed_identity) != 6
        ):
            return False
        return (
            expected_identity[:5] == observed_identity[:5]
            and observed.get("sha256") == expected.get("sha256")
        )
    if (
        not isinstance(expected_identity, list)
        or not isinstance(observed_identity, list)
        or len(expected_identity) != len(observed_identity)
    ):
        return False
    for before, after in zip(expected_identity, observed_identity):
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        if before.get("path") != after.get("path"):
            return False
        if before.get("sha256") != after.get("sha256"):
            return False
        before_stat = before.get("stat")
        after_stat = after.get("stat")
        if (
            not isinstance(before_stat, list)
            or not isinstance(after_stat, list)
            or len(before_stat) != 6
            or len(after_stat) != 6
        ):
            return False
        if before.get("path") == ".":
            if before_stat[:5] != after_stat[:5]:
                return False
        elif before_stat != after_stat:
            return False
    return True


def _quarantine_name_at(parent_fd: int) -> str:
    for _ in range(32):
        name = ".orch-prune-" + secrets.token_hex(16)
        if not _entry_exists_at(parent_fd, name):
            return name
    raise ValueError("retention_quarantine_name_exhausted")


def _restore_quarantine_at(
    parent_fd: int, quarantine: str, original: str,
) -> bool:
    if _entry_exists_at(parent_fd, original):
        return False
    try:
        os.rename(
            quarantine, original,
            src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
        )
        return True
    except OSError:
        return False


def _same_directory_object(
    info: os.stat_result, expected_stat: List[int],
) -> bool:
    if not isinstance(expected_stat, list) or len(expected_stat) != 6:
        return False
    current = _stat_identity(info)
    return current[:3] == expected_stat[:3]


def _open_relative_directory(
    root_fd: int, parts: Tuple[str, ...],
) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            child_fd = _open_directory_at(fd, part)
            os.close(fd)
            fd = child_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _delete_tree_from_record(
    anchor_fd: int,
    name: str,
    expected: Dict[str, Any],
) -> Dict[str, Any]:
    deleted_bytes = 0
    deleted_paths: List[str] = []

    def outcome(
        status: str,
        reason: Optional[str] = None,
        relative: Optional[str] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": status}
        if reason is not None:
            result["reason"] = reason
        if relative is not None:
            result["relative"] = relative
        if deleted_bytes:
            result["deleted_bytes"] = deleted_bytes
        if deleted_paths:
            result["deleted_paths"] = list(deleted_paths)
        return result

    try:
        root_fd = _open_directory_at(anchor_fd, name)
    except ValueError:
        return outcome("CHANGED", "tree_root_changed")
    try:
        current = _tree_record_from_fd(root_fd)
        if current != expected:
            return outcome("CHANGED", "tree_changed")
        identities = expected.get("identity")
        if not isinstance(identities, list) or not identities:
            return outcome("CHANGED", "tree_binding_invalid")
        entries = []
        for record in identities:
            if not isinstance(record, dict):
                return outcome("CHANGED", "tree_binding_invalid")
            relative = record.get("path")
            stat_record = record.get("stat")
            if relative == ".":
                continue
            if (
                not isinstance(relative, str)
                or not isinstance(stat_record, list)
                or len(stat_record) != 6
            ):
                return outcome("CHANGED", "tree_binding_invalid")
            parts = Path(relative).parts
            if (
                not parts
                or Path(relative).is_absolute()
                or any(part in {"", ".", ".."} for part in parts)
            ):
                return outcome("CHANGED", "tree_binding_invalid")
            entries.append((
                relative, parts, stat_record, record.get("sha256")
            ))
        entries.sort(
            key=lambda item: (len(item[1]), item[0]), reverse=True
        )
        for relative, parts, stat_record, expected_sha in entries:
            try:
                parent_fd = _open_relative_directory(root_fd, parts[:-1])
            except ValueError:
                return outcome(
                    "CHANGED", "tree_parent_changed", relative
                )
            try:
                child = parts[-1]
                mode = int(stat_record[2])
                if statmod.S_ISREG(mode):
                    record = _regular_file_record_at(parent_fd, child)
                    if (
                        record is None
                        or record.get("identity") != stat_record
                        or record.get("sha256") != expected_sha
                    ):
                        return outcome(
                            "CHANGED", "tree_file_changed", relative
                        )
                    try:
                        os.unlink(child, dir_fd=parent_fd)
                    except OSError:
                        return outcome(
                            "FAILED", "tree_file_delete_failed", relative
                        )
                    deleted_bytes += int(stat_record[3])
                    deleted_paths.append(relative)
                    continue
                if not statmod.S_ISDIR(mode):
                    return outcome(
                        "CHANGED", "tree_entry_type_invalid", relative
                    )
                try:
                    child_fd = _open_directory_at(parent_fd, child)
                except ValueError:
                    return outcome(
                        "CHANGED", "tree_directory_changed", relative
                    )
                try:
                    info = os.fstat(child_fd)
                    if not _same_directory_object(info, stat_record):
                        return outcome(
                            "CHANGED", "tree_directory_changed", relative
                        )
                    with os.scandir(child_fd) as iterator:
                        if next(iterator, None) is not None:
                            return outcome(
                                "CHANGED",
                                "tree_directory_not_empty",
                                relative,
                            )
                finally:
                    os.close(child_fd)
                try:
                    os.rmdir(child, dir_fd=parent_fd)
                except OSError:
                    return outcome(
                        "FAILED", "tree_directory_delete_failed", relative
                    )
                deleted_paths.append(relative + "/")
            finally:
                os.close(parent_fd)
        with os.scandir(root_fd) as iterator:
            if next(iterator, None) is not None:
                return outcome("CHANGED", "tree_root_not_empty")
    finally:
        os.close(root_fd)
    try:
        os.rmdir(name, dir_fd=anchor_fd)
    except OSError:
        return outcome("FAILED", "tree_root_delete_failed")
    return outcome("DELETED")


def _bound_backup_survivor_exists_at(
    anchor_fd: int,
    survivors: List[Dict[str, Any]],
) -> bool:
    for artifact in survivors:
        path_value = artifact.get("path")
        if not isinstance(path_value, str):
            continue
        name = Path(path_value).name
        try:
            _validate_child_name(name)
        except ValueError:
            continue
        current = _regular_file_record_at(anchor_fd, name)
        if current == _artifact_expected_record(artifact):
            return True
    return False


def _delete_bound_artifact(
    orch: Orchestrator,
    artifact: Dict[str, Any],
    *,
    backup_survivors: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    binding = _artifact_binding(orch, artifact)
    if binding["status"] != "MATCH":
        return binding
    kind = str(artifact["kind"])
    path = Path(str(artifact["path"]))
    name = binding["name"]
    expected = _artifact_expected_record(artifact)
    anchor_path = _retention_anchor_path(orch, kind)
    try:
        anchor_fd = _open_retention_anchor(orch, kind)
    except ValueError:
        return {"status": "CHANGED", "reason": "unsafe_anchor"}
    quarantine = ""
    try:
        current = _artifact_record_at(anchor_fd, kind, name)
        if current != expected:
            if not _entry_exists_at(anchor_fd, name):
                return {"status": "MISSING", "name": name}
            return {
                "status": "CHANGED",
                "reason": "identity_changed_before_quarantine",
            }
        try:
            quarantine = _quarantine_name_at(anchor_fd)
        except ValueError:
            return {
                "status": "FAILED",
                "reason": "quarantine_name_unavailable",
            }
        try:
            os.rename(
                name, quarantine,
                src_dir_fd=anchor_fd, dst_dir_fd=anchor_fd,
            )
        except FileNotFoundError:
            return {"status": "MISSING", "name": name}
        except OSError:
            return {
                "status": "FAILED",
                "reason": "quarantine_rename_failed",
            }
        post = _artifact_record_at(anchor_fd, kind, quarantine)
        if not _record_matches_after_quarantine(kind, expected, post):
            restored = _restore_quarantine_at(
                anchor_fd, quarantine, name
            )
            return {
                "status": "CHANGED",
                "reason": "identity_changed_during_quarantine",
                "restored": restored,
                "quarantine_path": None if restored else str(
                    anchor_path / quarantine
                ),
            }
        if kind == "backup":
            if backup_survivors is None:
                restored = _restore_quarantine_at(
                    anchor_fd, quarantine, name
                )
                return {
                    "status": "FAILED",
                    "reason": "backup_survivor_binding_missing",
                    "restored": restored,
                    "quarantine_path": None if restored else str(
                        anchor_path / quarantine
                    ),
                }
            if not _bound_backup_survivor_exists_at(
                anchor_fd, backup_survivors
            ):
                restored = _restore_quarantine_at(
                    anchor_fd, quarantine, name
                )
                return {
                    "status": "CHANGED",
                    "reason": "last_backup_guard",
                    "restored": restored,
                    "quarantine_path": None if restored else str(
                        anchor_path / quarantine
                    ),
                }
        if kind == "review_export":
            removed = _delete_tree_from_record(
                anchor_fd, quarantine, post
            )
            if removed["status"] != "DELETED":
                removed["quarantine_path"] = str(
                    anchor_path / quarantine
                )
                return removed
        else:
            if _regular_file_record_at(anchor_fd, quarantine) != post:
                restored = _restore_quarantine_at(
                    anchor_fd, quarantine, name
                )
                return {
                    "status": "CHANGED",
                    "reason": "identity_changed_after_quarantine",
                    "restored": restored,
                    "quarantine_path": None if restored else str(
                        anchor_path / quarantine
                    ),
                }
            try:
                os.unlink(quarantine, dir_fd=anchor_fd)
            except OSError:
                restored = _restore_quarantine_at(
                    anchor_fd, quarantine, name
                )
                return {
                    "status": "FAILED",
                    "reason": "artifact_delete_failed",
                    "restored": restored,
                    "quarantine_path": None if restored else str(
                        anchor_path / quarantine
                    ),
                }
        try:
            os.fsync(anchor_fd)
        except OSError:
            pass
        return {
            "status": "DELETED",
            "path": str(path),
            "bytes": int(artifact["bytes"]),
        }
    finally:
        os.close(anchor_fd)


def _retention_inventory(orch: Orchestrator) -> Dict[str, Any]:
    with orch.connect() as conn:
        runs = [dict(row) for row in conn.execute(
            "SELECT r.run_id,r.task_id,r.state,r.started_at,r.heartbeat_at,r.completed_at,t.status AS task_status "
            "FROM runs r JOIN tasks t ON t.task_id=r.task_id ORDER BY r.started_at"
        )]
        pending = {
            row["run_id"] for row in conn.execute(
                "SELECT run_id FROM publications WHERE status NOT IN ('COMPLETE','ABANDONED')"
            )
        }
    managed: Dict[str, Dict[str, Any]] = {}
    known_run_ids = {row["run_id"] for row in runs}
    for row in runs:
        run_id = row["run_id"]
        artifacts = []
        for log in sorted(orch.logs.glob(f"{run_id}-*.json")):
            record = _regular_file_record(log)
            if record is None:
                continue
            artifacts.append({
                "kind": "log", "path": str(log), **record,
            })
        receipt = orch.runtime / "worker_receipts" / f"{run_id}.json"
        receipt_record = _regular_file_record(receipt)
        if receipt_record is not None:
            artifacts.append({
                "kind": "worker_receipt", "path": str(receipt),
                **receipt_record,
            })
        export = orch.runtime / "review_exports" / run_id
        export_record = _safe_tree_record(export)
        if export_record is not None:
            artifacts.append({
                "kind": "review_export", "path": str(export),
                **export_record,
            })
        managed[run_id] = {
            **row,
            "protected": (
                row["state"] in ACTIVE_RUN_STATES
                or row["state"] == "VERIFIED"
                or row["task_status"] in {"WAITING_REVIEW", "READY_TO_PUBLISH", "WAITING_OWNER"}
                or run_id in pending
            ),
            "pending_publication": run_id in pending,
            "reference_ts": (
                _parse_utc(row["completed_at"])
                or _parse_utc(row["heartbeat_at"])
                or _parse_utc(row["started_at"])
                or 0.0
            ),
            "artifacts": artifacts,
            "bytes": sum(item["bytes"] for item in artifacts),
        }

    unmanaged: List[Dict[str, Any]] = []
    for log in sorted(orch.logs.glob("*")):
        if not log.is_file() or log.is_symlink():
            if log.exists() or log.is_symlink():
                unmanaged.append({"path": str(log), "reason": "non_regular_or_symlink"})
            continue
        if not any(log.name.startswith(run_id + "-") for run_id in known_run_ids):
            unmanaged.append({"path": str(log), "bytes": log.stat().st_size, "reason": "unknown_run"})
    receipts = orch.runtime / "worker_receipts"
    if receipts.is_dir():
        for item in sorted(receipts.iterdir()):
            if item.is_symlink() or not item.is_file():
                unmanaged.append({"path": str(item), "reason": "non_regular_or_symlink"})
            elif item.stem not in known_run_ids:
                unmanaged.append({"path": str(item), "bytes": item.stat().st_size, "reason": "unknown_run"})
    exports = orch.runtime / "review_exports"
    if exports.is_dir():
        for item in sorted(exports.iterdir()):
            if item.name not in known_run_ids:
                size = _safe_tree_size(item) if item.is_dir() and not item.is_symlink() else _regular_file_size(item)
                record = {"path": str(item), "reason": "unknown_run"}
                if size is not None:
                    record["bytes"] = size
                unmanaged.append(record)
            elif _safe_tree_size(item) is None:
                unmanaged.append({"path": str(item), "reason": "unsafe_review_export"})

    backups_dir = orch.root / "backups"
    backups: List[Dict[str, Any]] = []
    unmanaged_backups: List[Dict[str, Any]] = []
    if backups_dir.is_symlink():
        unmanaged_backups.append({"path": str(backups_dir), "reason": "unsafe_backup_directory"})
    elif backups_dir.is_dir():
        for item in sorted(backups_dir.iterdir()):
            if item.name.startswith("orch-state-") and item.suffix == ".zip":
                record = _regular_file_record(item)
                if record is None:
                    unmanaged_backups.append({
                        "path": str(item),
                        "reason": "unmanaged_backup_artifact",
                    })
                    continue
                backups.append({
                    "path": str(item),
                    **record,
                    "mtime": record["identity"][4] / 1_000_000_000,
                })
            elif item.exists() or item.is_symlink():
                unmanaged_backups.append({"path": str(item), "reason": "unmanaged_backup_artifact"})
    return {
        "runs": managed,
        "unmanaged": unmanaged,
        "backups": backups,
        "unmanaged_backups": unmanaged_backups,
    }


def retention_status(
    orch: Orchestrator, *, max_evidence_bytes: int = 256 * 1024 * 1024,
    max_backup_bytes: int = 512 * 1024 * 1024, keep_backups: int = 5,
) -> Dict[str, Any]:
    if max_evidence_bytes < 0 or max_backup_bytes < 0 or keep_backups < 1:
        raise ValueError("invalid_retention_policy")
    inventory = _retention_inventory(orch)
    runs = list(inventory["runs"].values())
    evidence_bytes = sum(item["bytes"] for item in runs)
    protected_bytes = sum(item["bytes"] for item in runs if item["protected"])
    backup_bytes = sum(item["bytes"] for item in inventory["backups"])
    unmanaged_bytes = sum(int(item.get("bytes", 0)) for item in inventory["unmanaged"])
    return {
        "status": "ATTENTION" if (
            evidence_bytes > max_evidence_bytes
            or backup_bytes > max_backup_bytes
            or len(inventory["backups"]) > keep_backups
            or inventory["unmanaged"]
            or inventory["unmanaged_backups"]
        ) else "READY",
        "policy": {
            "max_evidence_bytes": max_evidence_bytes,
            "max_backup_bytes": max_backup_bytes,
            "keep_backups": keep_backups,
        },
        "evidence": {
            "bytes": evidence_bytes,
            "run_count": sum(1 for item in runs if item["bytes"]),
            "protected_bytes": protected_bytes,
            "protected_runs": sorted(item["run_id"] for item in runs if item["protected"] and item["bytes"]),
            "unmanaged_bytes": unmanaged_bytes,
            "unmanaged": inventory["unmanaged"],
        },
        "backups": {
            "bytes": backup_bytes,
            "count": len(inventory["backups"]),
            "unmanaged": inventory["unmanaged_backups"],
        },
    }


def prune_retention(
    orch: Orchestrator, *, older_than_days: int = 30,
    max_evidence_bytes: int = 256 * 1024 * 1024, keep_recent_runs: int = 20,
    keep_backups: int = 5, max_backup_bytes: int = 512 * 1024 * 1024,
) -> Dict[str, Any]:
    if (
        older_than_days < 0 or max_evidence_bytes < 0 or keep_recent_runs < 0
        or keep_backups < 1 or max_backup_bytes < 0
    ):
        raise ValueError("invalid_retention_policy")
    import time
    inventory = _retention_inventory(orch)
    runs = list(inventory["runs"].values())
    terminal = sorted(
        (item for item in runs if not item["protected"] and item["bytes"] > 0),
        key=lambda item: (item["reference_ts"], item["run_id"]),
    )
    newest_keep = {
        item["run_id"] for item in sorted(
            terminal, key=lambda item: (item["reference_ts"], item["run_id"]), reverse=True
        )[:keep_recent_runs]
    }
    total = sum(item["bytes"] for item in runs)
    cutoff = time.time() - older_than_days * 86400
    deleted_runs: List[Dict[str, Any]] = []
    deleted_backups: List[Dict[str, Any]] = []
    skipped_changed: List[Dict[str, Any]] = []
    failed_deletions: List[Dict[str, Any]] = []
    already_missing: List[Dict[str, Any]] = []
    budget_pruning_allowed = True

    def issue_record(
        scope: str,
        artifact: Dict[str, Any],
        result: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        record = {
            "scope": scope,
            "kind": artifact["kind"],
            "path": artifact["path"],
            "status": result["status"],
        }
        if run_id is not None:
            record["run_id"] = run_id
        for field in (
            "reason", "relative", "restored", "quarantine_path",
            "deleted_bytes", "deleted_paths",
        ):
            if field in result:
                record[field] = result[field]
        return record

    for item in terminal:
        age_due = item["reference_ts"] <= cutoff
        budget_due = (
            budget_pruning_allowed
            and total > max_evidence_bytes
            and item["run_id"] not in newest_keep
        )
        if not age_due and not budget_due:
            continue

        matched: List[Dict[str, Any]] = []
        changed: List[Dict[str, Any]] = []
        for artifact in item["artifacts"]:
            binding = _artifact_binding(orch, artifact)
            if binding["status"] == "MATCH":
                matched.append(artifact)
                continue
            if binding["status"] == "MISSING":
                total = max(0, total - int(artifact["bytes"]))
                already_missing.append(issue_record(
                    "run", artifact, binding, run_id=item["run_id"]
                ))
                continue
            changed.append(issue_record(
                "run", artifact, binding, run_id=item["run_id"]
            ))
        if changed:
            skipped_changed.extend(changed)
            budget_pruning_allowed = False
            continue
        if not age_due and total <= max_evidence_bytes:
            continue

        removed = 0
        paths: List[str] = []
        for artifact in matched:
            result = _delete_bound_artifact(orch, artifact)
            if result["status"] == "DELETED":
                removed += int(artifact["bytes"])
                paths.append(str(artifact["path"]))
                continue
            if result["status"] == "MISSING":
                total = max(0, total - int(artifact["bytes"]))
                already_missing.append(issue_record(
                    "run", artifact, result, run_id=item["run_id"]
                ))
                if (
                    not age_due
                    and max(0, total - removed) <= max_evidence_bytes
                ):
                    break
                continue
            partial_bytes = int(result.get("deleted_bytes", 0))
            if partial_bytes:
                removed += partial_bytes
                for relative in result.get("deleted_paths", []):
                    paths.append(
                        str(Path(artifact["path"]) / relative.rstrip("/"))
                    )
            record = issue_record(
                "run", artifact, result, run_id=item["run_id"]
            )
            if result["status"] == "CHANGED":
                skipped_changed.append(record)
            else:
                failed_deletions.append(record)
            budget_pruning_allowed = False
            break
        if removed:
            total = max(0, total - removed)
            deleted_runs.append({
                "run_id": item["run_id"],
                "bytes": removed,
                "paths": paths,
            })

    backups = sorted(
        inventory["backups"],
        key=lambda item: (item["mtime"], item["path"]),
    )
    backup_artifacts = [
        {"kind": "backup", **item} for item in backups
    ]
    deleted_backup_paths = set()
    reported_missing_backup_paths = set()
    for artifact in backup_artifacts:
        live_backups: List[Dict[str, Any]] = []
        backup_total = 0
        reconciliation_changed: List[Dict[str, Any]] = []
        for candidate in backup_artifacts:
            candidate_path = str(candidate["path"])
            if candidate_path in deleted_backup_paths:
                continue
            binding = _artifact_binding(orch, candidate)
            if binding["status"] == "MATCH":
                live_backups.append(candidate)
                backup_total += int(candidate["bytes"])
                continue
            if binding["status"] == "MISSING":
                if candidate_path not in reported_missing_backup_paths:
                    already_missing.append(issue_record(
                        "backup", candidate, binding
                    ))
                    reported_missing_backup_paths.add(candidate_path)
                continue
            reconciliation_changed.append(issue_record(
                "backup", candidate, binding
            ))
        if reconciliation_changed:
            skipped_changed.extend(reconciliation_changed)
            break
        if (
            len(live_backups) <= keep_backups
            and backup_total <= max_backup_bytes
        ):
            break
        if len(live_backups) <= 1:
            break
        live_paths = {
            str(candidate["path"]) for candidate in live_backups
        }
        if str(artifact["path"]) not in live_paths:
            continue
        result = _delete_bound_artifact(
            orch,
            artifact,
            backup_survivors=[
                candidate
                for candidate in live_backups
                if str(candidate["path"]) != str(artifact["path"])
            ],
        )
        if result["status"] == "DELETED":
            deleted_backup_paths.add(str(artifact["path"]))
            deleted_backups.append({
                "path": str(artifact["path"]),
                "bytes": int(artifact["bytes"]),
            })
            continue
        if result["status"] == "MISSING":
            candidate_path = str(artifact["path"])
            if candidate_path not in reported_missing_backup_paths:
                already_missing.append(issue_record(
                    "backup", artifact, result
                ))
                reported_missing_backup_paths.add(candidate_path)
            continue
        record = issue_record("backup", artifact, result)
        if result["status"] == "CHANGED":
            skipped_changed.append(record)
        else:
            failed_deletions.append(record)
        break

    after = retention_status(
        orch,
        max_evidence_bytes=max_evidence_bytes,
        max_backup_bytes=max_backup_bytes,
        keep_backups=keep_backups,
    )
    if skipped_changed or failed_deletions:
        after["status"] = "ATTENTION"
    elif deleted_runs or deleted_backups:
        after["status"] = "PRUNED"
    after["pruned"] = {
        "runs": deleted_runs,
        "run_count": len(deleted_runs),
        "evidence_bytes": sum(item["bytes"] for item in deleted_runs),
        "backups": deleted_backups,
        "backup_count": len(deleted_backups),
        "backup_bytes": sum(item["bytes"] for item in deleted_backups),
        "skipped_changed": skipped_changed,
        "skipped_changed_count": len(skipped_changed),
        "failed": failed_deletions,
        "failed_count": len(failed_deletions),
        "already_missing": already_missing,
        "already_missing_count": len(already_missing),
    }
    after["bounded"] = (
        after["evidence"]["bytes"] + after["evidence"]["unmanaged_bytes"]
        <= max_evidence_bytes
        and after["backups"]["bytes"] <= max_backup_bytes
        and after["backups"]["count"] <= keep_backups
        and not after["evidence"]["unmanaged"]
        and not after["backups"]["unmanaged"]
        and not skipped_changed
        and not failed_deletions
    )
    return after


def _backup_members(orch: Orchestrator) -> List[Path]:
    members: List[Path] = []
    for relative in (
        "config.json", "rdc-bootstrap.json", "dispatcher-prompt.txt",
        "restore-receipt.json", "replacement-receipt.json",
    ):
        path = orch.root / relative
        if path.is_file() and not path.is_symlink():
            members.append(path)
    for base in (
        orch.root / "projects", orch.root / "plans",
        orch.runtime / "logs", orch.runtime / "worker_receipts",
    ):
        if base.is_dir():
            members.extend(
                path for path in base.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
    review_root = orch.runtime / "review_exports"
    if review_root.is_dir():
        members.extend(
            path for path in review_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    return sorted(set(members))


def _stream_backup_member(
    archive: zipfile.ZipFile,
    orch: Orchestrator,
    path: Path,
) -> Tuple[str, Dict[str, Any]]:
    try:
        relative = path.relative_to(orch.root).as_posix()
    except ValueError as exc:
        raise ValueError("backup_member_outside_root") from exc
    arcname = "files/" + relative
    flags = regular_file_read_flags()
    parts = Path(relative).parts
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("backup_member_unsafe:" + relative)
    parent_fd = -1
    try:
        for name in parts:
            _validate_child_name(name)
        parent_fd = _open_directory_path(Path(orch.root.anchor))
        root_parts = orch.root.relative_to(orch.root.anchor).parts
        for name in root_parts + parts[:-1]:
            child_fd = _open_directory_at(parent_fd, name)
            os.close(parent_fd)
            parent_fd = child_fd
        fd = os.open(parts[-1], flags, dir_fd=parent_fd)
    except (OSError, ValueError, IndexError) as exc:
        raise ValueError("backup_member_unsafe:" + relative) from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    try:
        before = os.fstat(fd)
        if not statmod.S_ISREG(before.st_mode):
            raise ValueError("backup_member_unsafe:" + relative)
        digest = hashlib.sha256()
        written = 0
        with os.fdopen(fd, "rb", closefd=True) as source:
            fd = -1
            with archive.open(arcname, "w", force_zip64=True) as dest:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    written += len(block)
                    dest.write(block)
            after = os.fstat(source.fileno())
        identity_before = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if identity_after != identity_before or written != before.st_size:
            raise ValueError("backup_member_changed_during_read:" + relative)
        return arcname, {
            "sha256": digest.hexdigest(),
            "bytes": written,
        }
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def _frozen_backup_source(path: Path, *, max_archive_bytes: int):
    source = Path(os.path.abspath(os.path.expanduser(str(path))))
    flags = regular_file_read_flags()
    if not hasattr(os, "O_NOFOLLOW") and source.is_symlink():
        raise ValueError("backup_not_regular_file")
    try:
        fd = os.open(str(source), flags)
    except OSError as exc:
        raise ValueError("backup_not_regular_file") from exc
    try:
        info = os.fstat(fd)
        if not statmod.S_ISREG(info.st_mode):
            raise ValueError("backup_not_regular_file")
        if info.st_size > max_archive_bytes:
            raise ValueError("backup_archive_size_limit_exceeded")
        with tempfile.TemporaryDirectory(
            prefix="orch-backup-source-"
        ) as tmp:
            snapshot = Path(tmp) / "source.zip"
            sha = hashlib.sha256()
            written = 0
            with os.fdopen(fd, "rb", closefd=True) as source_handle:
                fd = -1
                with snapshot.open("xb") as dest:
                    while True:
                        block = source_handle.read(1024 * 1024)
                        if not block:
                            break
                        written += len(block)
                        if written > max_archive_bytes:
                            raise ValueError(
                                "backup_archive_size_limit_exceeded"
                            )
                        sha.update(block)
                        dest.write(block)
                    dest.flush()
                    os.fsync(dest.fileno())
            os.chmod(snapshot, 0o600)
            yield {
                "source": source,
                "snapshot": snapshot,
                "sha256": sha.hexdigest(),
                "bytes": written,
            }
    finally:
        if fd >= 0:
            os.close(fd)


def verify_backup_archive(
    path: Path, *, max_uncompressed_bytes: int = 1024 * 1024 * 1024,
) -> Dict[str, Any]:
    if max_uncompressed_bytes <= 0:
        raise ValueError("invalid_backup_verify_limit")
    source = Path(os.path.abspath(os.path.expanduser(str(path))))
    try:
        with _frozen_backup_source(
            path, max_archive_bytes=max_uncompressed_bytes
        ) as frozen:
            result = _verify_frozen_backup_archive(
                frozen["snapshot"],
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
            result["path"] = str(source)
            result["archive_sha256"] = frozen["sha256"]
            result["archive_bytes"] = frozen["bytes"]
            return result
    except ValueError as exc:
        return {
            "status": "BLOCKED",
            "path": str(source),
            "errors": [str(exc)],
            "warnings": [],
        }


def _verify_frozen_backup_archive(
    path: Path, *, max_uncompressed_bytes: int = 1024 * 1024 * 1024,
) -> Dict[str, Any]:
    from pathlib import PurePosixPath
    import re

    target = path.expanduser().resolve()
    base = {
        "status": "BLOCKED",
        "path": str(target),
        "errors": [],
        "warnings": [],
    }
    if max_uncompressed_bytes <= 0:
        raise ValueError("invalid_backup_verify_limit")
    if path.is_symlink() or not target.is_file():
        base["errors"].append("backup_not_regular_file")
        return base

    try:
        with zipfile.ZipFile(target, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                base["errors"].append("duplicate_backup_member")

            total = 0
            unsafe: List[str] = []
            forbidden: List[str] = []
            nonrestorable: List[str] = []
            for info in infos:
                name = info.filename
                total += int(info.file_size)
                pure = PurePosixPath(name)
                if (
                    not name
                    or "\x00" in name
                    or "\\" in name
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or name.startswith("/")
                    or name != pure.as_posix()
                ):
                    unsafe.append(name)
                    continue
                if not _restorable_backup_member(name):
                    nonrestorable.append(name)
                if name.startswith("files/.runtime/claims/") or name == "files/.runtime/claims":
                    forbidden.append(name)
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == 0o120000:
                    unsafe.append(name)
            if unsafe:
                base["errors"].append("unsafe_backup_member")
                base["unsafe_members"] = sorted(set(unsafe))
            if forbidden:
                base["errors"].append("forbidden_secret_member")
                base["forbidden_members"] = sorted(set(forbidden))
            if nonrestorable:
                base["errors"].append("backup_member_not_restorable")
                base["nonrestorable_members"] = sorted(set(nonrestorable))
            if total > max_uncompressed_bytes:
                base["errors"].append("backup_uncompressed_limit_exceeded")
            base["member_count"] = len(infos)
            base["uncompressed_bytes"] = total

            required = {"state/orch.sqlite3", "state/manifest.json"}
            missing = sorted(required - set(names))
            if missing:
                base["errors"].append("backup_required_member_missing")
                base["missing_members"] = missing
            if base["errors"]:
                return base

            try:
                manifest = json.loads(
                    archive.read("state/manifest.json").decode("utf-8")
                )
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
                base["errors"].append("backup_manifest_invalid")
                return base
            if not isinstance(manifest, dict):
                base["errors"].append("backup_manifest_invalid")
                return base
            digest = manifest.get("database_sha256")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                base["errors"].append("backup_manifest_database_hash_invalid")
                return base
            excluded = manifest.get("excluded_secret_classes")
            required_exclusions = {
                "claims", "capability_files", "provider_credentials",
            }
            if (
                not isinstance(excluded, list)
                or not required_exclusions.issubset(set(excluded))
            ):
                base["errors"].append(
                    "backup_secret_exclusion_contract_missing"
                )
                return base

            file_evidence = manifest.get("file_evidence")
            if file_evidence is None:
                base["warnings"].append("backup_file_evidence_missing")
            elif not isinstance(file_evidence, dict):
                base["errors"].append("backup_file_evidence_invalid")
                return base
            else:
                expected_files = {
                    name for name in names if name.startswith("files/")
                }
                if set(file_evidence) != expected_files:
                    base["errors"].append(
                        "backup_file_evidence_members_mismatch"
                    )
                    return base
                evidence_total = 0
                info_by_name = {info.filename: info for info in infos}
                for name in sorted(expected_files):
                    record = file_evidence.get(name)
                    if not isinstance(record, dict):
                        base["errors"].append(
                            "backup_file_evidence_invalid"
                        )
                        return base
                    expected_sha = record.get("sha256")
                    expected_bytes = record.get("bytes")
                    if (
                        set(record) != {"sha256", "bytes"}
                        or not isinstance(expected_sha, str)
                        or re.fullmatch(r"[0-9a-f]{64}", expected_sha)
                        is None
                        or not isinstance(expected_bytes, int)
                        or isinstance(expected_bytes, bool)
                        or expected_bytes < 0
                    ):
                        base["errors"].append(
                            "backup_file_evidence_invalid"
                        )
                        return base
                    if expected_bytes != int(
                        info_by_name[name].file_size
                    ):
                        base["errors"].append(
                            "backup_file_evidence_mismatch"
                        )
                        base["file_evidence_mismatch"] = name
                        return base
                    actual_sha = hashlib.sha256()
                    actual_bytes = 0
                    try:
                        with archive.open(name, "r") as source:
                            while True:
                                block = source.read(1024 * 1024)
                                if not block:
                                    break
                                actual_sha.update(block)
                                actual_bytes += len(block)
                                evidence_total += len(block)
                                if evidence_total > max_uncompressed_bytes:
                                    base["errors"].append(
                                        "backup_file_evidence_limit_exceeded"
                                    )
                                    return base
                    except (
                        OSError, RuntimeError, zipfile.BadZipFile,
                    ):
                        base["errors"].append(
                            "backup_file_evidence_read_failed"
                        )
                        return base
                    if (
                        actual_bytes != expected_bytes
                        or actual_sha.hexdigest() != expected_sha
                    ):
                        base["errors"].append(
                            "backup_file_evidence_mismatch"
                        )
                        base["file_evidence_mismatch"] = name
                        return base

            with tempfile.TemporaryDirectory(prefix="orch-backup-verify-") as tmp:
                db_copy = Path(tmp) / "orch.sqlite3"
                sha = hashlib.sha256()
                written = 0
                try:
                    with archive.open("state/orch.sqlite3", "r") as source, db_copy.open("wb") as dest:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            written += len(block)
                            if written > max_uncompressed_bytes:
                                base["errors"].append("backup_database_limit_exceeded")
                                return base
                            sha.update(block)
                            dest.write(block)
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    base["errors"].append("backup_database_read_failed")
                    return base
                os.chmod(db_copy, 0o600)
                actual_digest = sha.hexdigest()
                if actual_digest != digest:
                    base["errors"].append("backup_database_hash_mismatch")
                    base["database_sha256"] = actual_digest
                    base["manifest_database_sha256"] = digest
                    return base

                try:
                    uri = db_copy.resolve().as_uri() + "?mode=ro&immutable=1"
                    conn = sqlite3.connect(uri, uri=True)
                    try:
                        quick = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
                        foreign = [list(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
                        db_schema = conn.execute("PRAGMA user_version").fetchone()[0]
                    finally:
                        conn.close()
                except sqlite3.DatabaseError:
                    base["errors"].append("backup_database_invalid")
                    return base

            manifest_schema = manifest.get("schema_version")
            if not isinstance(manifest_schema, int) or isinstance(manifest_schema, bool):
                base["errors"].append("backup_manifest_schema_invalid")
            elif manifest_schema != db_schema:
                base["errors"].append("backup_schema_binding_mismatch")
            if quick != ["ok"]:
                base["errors"].append("backup_database_quick_check_failed")
            if foreign:
                base["errors"].append("backup_database_foreign_key_violation")
            if db_schema > STATE_SCHEMA_VERSION:
                base["errors"].append("backup_future_schema_unsupported")
                compatibility = "FUTURE_UNSUPPORTED"
            elif db_schema < STATE_SCHEMA_VERSION:
                compatibility = "UPGRADE_REQUIRED"
            else:
                compatibility = "CURRENT"

            base.update({
                "manifest": manifest,
                "database_sha256": actual_digest,
                "quick_check": quick,
                "foreign_key_violations": foreign,
                "schema_version": db_schema,
                "current_schema_version": STATE_SCHEMA_VERSION,
                "compatibility": compatibility,
            })
            if base["errors"]:
                return base
            bad_crc = archive.testzip()
            if bad_crc is not None:
                base["errors"].append("backup_crc_failure")
                base["crc_member"] = bad_crc
                return base
            base["status"] = "VERIFIED"
            return base
    except (OSError, zipfile.BadZipFile):
        base["errors"].append("backup_zip_invalid")
        return base


def _restorable_backup_member(name: str) -> bool:
    if name in {
        "state/orch.sqlite3",
        "state/manifest.json",
        "files/config.json",
        "files/rdc-bootstrap.json",
        "files/dispatcher-prompt.txt",
        "files/restore-receipt.json",
        "files/replacement-receipt.json",
    }:
        return True
    return any(
        name.startswith(prefix)
        for prefix in (
            "files/projects/",
            "files/plans/",
            "files/.runtime/logs/",
            "files/.runtime/worker_receipts/",
            "files/.runtime/review_exports/",
        )
    )


def _write_streamed_member(
    archive: zipfile.ZipFile, member: str, target: Path,
    *, max_bytes: int,
) -> int:
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    written = 0
    with archive.open(member, "r") as source, target.open("wb") as dest:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            written += len(block)
            if written > max_bytes:
                raise ValueError("backup_restore_member_limit_exceeded")
            dest.write(block)
        dest.flush()
        os.fsync(dest.fileno())
    os.chmod(target, 0o600)
    return written


def restore_backup_archive(
    path: Path, destination: Path, *,
    max_uncompressed_bytes: int = 1024 * 1024 * 1024,
) -> Dict[str, Any]:
    verified = verify_backup_archive(
        path, max_uncompressed_bytes=max_uncompressed_bytes
    )
    if verified["status"] != "VERIFIED":
        errors = ",".join(verified.get("errors") or ["unknown"])
        raise ValueError("backup_not_verified:" + errors)

    dest = destination.expanduser().resolve()
    if destination.is_symlink() or dest.exists():
        raise ValueError("restore_destination_exists")
    parent = dest.parent
    if parent.is_symlink():
        raise ValueError("restore_parent_unsafe")
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.is_dir():
        raise ValueError("restore_parent_not_directory")

    staging = Path(tempfile.mkdtemp(
        prefix=f".{dest.name}.restore-", dir=str(parent)
    ))
    os.chmod(staging, 0o700)
    published = False
    source_archive_path = Path(
        os.path.abspath(os.path.expanduser(str(path)))
    )
    source_archive_sha256 = verified["archive_sha256"]
    try:
        runtime = staging / ".runtime"
        runtime.mkdir(mode=0o700)
        with _frozen_backup_source(
            path, max_archive_bytes=max_uncompressed_bytes
        ) as frozen:
            if frozen["sha256"] != source_archive_sha256:
                raise ValueError("backup_source_changed_after_verification")
            source_archive_path = frozen["source"]
            with zipfile.ZipFile(frozen["snapshot"], "r") as archive:
                for info in archive.infolist():
                    name = info.filename
                    if not _restorable_backup_member(name):
                        raise ValueError(
                            f"restore_member_not_allowed:{name}"
                        )
                    if name == "state/manifest.json":
                        continue
                    if name == "files/dispatcher-prompt.txt":
                        continue
                    if name == "state/orch.sqlite3":
                        target = runtime / "orch.sqlite3"
                    elif name.startswith("files/"):
                        relative = Path(*Path(name).parts[1:])
                        target = staging / relative
                    else:
                        raise ValueError(
                            f"restore_member_not_allowed:{name}"
                        )
                    _write_streamed_member(
                        archive, name, target,
                        max_bytes=max_uncompressed_bytes,
                    )

        for directory in [staging, *sorted(
            (item for item in staging.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
        )]:
            if directory.is_symlink():
                raise ValueError("restored_directory_symlink")
            os.chmod(directory, 0o700)

        restored = Orchestrator(staging)
        health = check_state(restored)
        if health["status"] == "BLOCKED":
            raise ValueError("restored_state_blocked")
        if health["active_runs"]:
            raise ValueError("restored_active_runs_present")
        if health["capabilities"]["missing_capabilities"]:
            raise ValueError("restored_capability_missing")

        receipt = {
            "schema_version": 1,
            "restored_at": utc_now(),
            "source_archive": str(source_archive_path),
            "source_archive_sha256": source_archive_sha256,
            "source_manifest": verified["manifest"],
            "source_schema_version": verified["schema_version"],
            "restored_schema_version": health["schema_version"],
            "compatibility_before_restore": verified["compatibility"],
            "destination": str(dest),
            "dispatcher_prompt_restored": False,
            "dispatcher_prompt_action": "regenerate_with_orch_dispatcher_render",
            "health_status": health["status"],
            "active_runs": len(health["active_runs"]),
            "writer_locks": len(health["writer_locks"]),
            "pending_publications": len(health["pending_publications"]),
        }
        receipt_path = staging / "restore-receipt.json"
        atomic_write_json(receipt_path, receipt, mode=0o600)

        os.replace(staging, dest)
        published = True
        try:
            fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

        final_orch = Orchestrator(dest)
        final_health = check_state(final_orch)
        return {
            "status": "RESTORED",
            "destination": str(dest),
            "archive": str(source_archive_path),
            "archive_sha256": receipt["source_archive_sha256"],
            "source_schema_version": verified["schema_version"],
            "restored_schema_version": final_health["schema_version"],
            "health": final_health,
            "receipt": str(dest / "restore-receipt.json"),
            "dispatcher_prompt_restored": False,
        }
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


_STANDALONE_HOME_ALLOWED = {
    ".runtime",
    "config.json",
    "rdc-bootstrap.json",
    "dispatcher-prompt.txt",
    "restore-receipt.json",
    "replacement-receipt.json",
    "projects",
    "plans",
    "backups",
    "logs",
    "snapshots",
    "reviews",
    "claims",
}


def _standalone_home_inventory(path: Path) -> Dict[str, Any]:
    root = path.expanduser().resolve()
    if path.is_symlink() or not root.is_dir():
        return {"status": "BLOCKED", "reason": "home_not_safe_directory"}
    unknown: List[str] = []
    unsafe: List[str] = []
    for item in sorted(root.iterdir(), key=lambda candidate: candidate.name):
        if item.name not in _STANDALONE_HOME_ALLOWED:
            unknown.append(item.name)
        if item.is_symlink():
            unsafe.append(item.name)
    db = root / ".runtime" / "orch.sqlite3"
    if not db.is_file() or db.is_symlink():
        unsafe.append(".runtime/orch.sqlite3")
    return {
        "status": "SAFE" if not unknown and not unsafe else "BLOCKED",
        "root": str(root),
        "unknown_entries": unknown,
        "unsafe_entries": sorted(set(unsafe)),
    }


def _replacement_journal_path(destination: Path) -> Path:
    dest = destination.expanduser().resolve()
    return dest.parent / f".{dest.name}.replacement-journal.json"


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _replacement_rename(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _replacement_write(path: Path, data: Dict[str, Any]) -> None:
    payload = dict(data)
    payload["updated_at"] = utc_now()
    atomic_write_json(path, payload, mode=0o600)


def _replacement_load(destination: Path) -> Tuple[Path, Dict[str, Any]]:
    dest = destination.expanduser().resolve()
    journal = _replacement_journal_path(dest)
    try:
        data, meta = read_bounded_json_object(
            journal,
            max_bytes=REPLACEMENT_JOURNAL_MAX_BYTES,
            unsafe_error="replacement_journal_unsafe",
            too_large_error="replacement_journal_too_large",
            invalid_error="replacement_journal_invalid_json",
        )
    except FileNotFoundError as exc:
        raise ValueError("replacement_journal_missing") from exc
    if meta["mode"] != 0o600:
        raise ValueError("replacement_journal_mode_unsafe")
    if data.get("destination") != str(dest):
        raise ValueError("replacement_journal_destination_mismatch")
    parent = dest.parent
    prepared = Path(data.get("prepared_home", "")).expanduser().resolve()
    rollback = Path(data.get("rollback_home", "")).expanduser().resolve()
    discard = Path(data.get("discard_home", "")).expanduser().resolve()
    failed = Path(data.get("failed_home", "")).expanduser().resolve()
    prefix_prepared = f".{dest.name}.replacement-"
    prefix_rollback = f".{dest.name}.rollback-"
    prefix_discard = f".{dest.name}.discard-"
    prefix_failed = f".{dest.name}.failed-"
    if (
        prepared.parent != parent
        or rollback.parent != parent
        or discard.parent != parent
        or failed.parent != parent
        or not prepared.name.startswith(prefix_prepared)
        or not rollback.name.startswith(prefix_rollback)
        or not discard.name.startswith(prefix_discard)
        or not failed.name.startswith(prefix_failed)
    ):
        raise ValueError("replacement_journal_path_unsafe")
    return journal, data


def _restored_home_matches(path: Path, archive_sha256: str) -> bool:
    receipt = path / "restore-receipt.json"
    try:
        data, meta = read_bounded_json_object(
            receipt,
            max_bytes=RESTORE_RECEIPT_MAX_BYTES,
            unsafe_error="restore_receipt_unsafe",
            too_large_error="restore_receipt_too_large",
            invalid_error="restore_receipt_invalid_json",
        )
    except (FileNotFoundError, ValueError):
        return False
    if meta["mode"] != 0o600:
        return False
    return data.get("source_archive_sha256") == archive_sha256


def replace_home_from_backup(path: Path, destination: Path) -> Dict[str, Any]:
    import uuid

    dest = destination.expanduser().resolve()
    journal_path = _replacement_journal_path(dest)
    if journal_path.exists() or journal_path.is_symlink():
        raise ValueError("replacement_reconciliation_required")
    inventory = _standalone_home_inventory(dest)
    if inventory["status"] != "SAFE":
        raise ValueError("replacement_home_not_standalone")

    live = Orchestrator(dest)
    health = check_state(live)
    if health["status"] != "READY":
        raise ValueError("replacement_live_state_not_ready")
    if live.reconcile()["status"] != "CLEAN":
        raise ValueError("replacement_live_reconcile_not_clean")
    if recovery_inspect(live)["status"] != "CLEAN":
        raise ValueError("replacement_live_recovery_not_clean")
    with live.connect() as conn:
        nonterminal = [
            dict(row) for row in conn.execute(
                "SELECT task_id,project_id,status,queue_seq FROM tasks "
                "WHERE status NOT IN ('DONE','CANCELLED') "
                "ORDER BY queue_seq,task_id"
            ).fetchall()
        ]
        global_pause = conn.execute(
            "SELECT value_json FROM settings WHERE key='paused'"
        ).fetchone()
        project_pauses = [
            row["key"] for row in conn.execute(
                "SELECT key FROM settings WHERE key GLOB 'project_paused:*' "
                "ORDER BY key"
            ).fetchall()
        ]
    if nonterminal:
        raise ValueError(
            "replacement_live_nonterminal_tasks:"
            + nonterminal[0]["task_id"]
        )
    if global_pause or project_pauses:
        raise ValueError("replacement_live_pause_present")

    verified = verify_backup_archive(path)
    if verified["status"] != "VERIFIED":
        errors = ",".join(verified.get("errors") or ["unknown"])
        raise ValueError("backup_not_verified:" + errors)

    operation_id = uuid.uuid4().hex
    parent = dest.parent
    old_stat = dest.stat()
    prepared = parent / f".{dest.name}.replacement-{operation_id}"
    rollback = parent / f".{dest.name}.rollback-{operation_id}"
    discard = parent / f".{dest.name}.discard-{operation_id}"
    failed = parent / f".{dest.name}.failed-{operation_id}"
    restored = restore_backup_archive(path, prepared)
    data = {
        "schema_version": 1,
        "operation_id": operation_id,
        "status": "PREPARED",
        "created_at": utc_now(),
        "destination": str(dest),
        "prepared_home": str(prepared),
        "rollback_home": str(rollback),
        "discard_home": str(discard),
        "failed_home": str(failed),
        "old_root_device": int(old_stat.st_dev),
        "old_root_inode": int(old_stat.st_ino),
        "archive": str(path.expanduser().resolve()),
        "archive_sha256": restored["archive_sha256"],
        "old_schema_version": health["schema_version"],
        "new_schema_version": restored["restored_schema_version"],
    }
    try:
        _replacement_write(journal_path, data)
    except Exception:
        if (
            prepared.is_dir()
            and not prepared.is_symlink()
            and _restored_home_matches(prepared, data["archive_sha256"])
            and _old_home_identity_matches(dest, data)
        ):
            shutil.rmtree(prepared)
            _fsync_directory(parent)
        raise
    try:
        _replacement_rename(dest, rollback)
        _fsync_directory(parent)
        data["status"] = "OLD_MOVED"
        _replacement_write(journal_path, data)

        _replacement_rename(prepared, dest)
        _fsync_directory(parent)
        data["status"] = "NEW_ACTIVE"
        _replacement_write(journal_path, data)
    except Exception:
        raise

    if not _restored_home_matches(dest, data["archive_sha256"]):
        raise ValueError("replacement_new_home_not_proven")
    new_health = check_state(Orchestrator(dest))
    if new_health["status"] == "BLOCKED":
        raise ValueError("replacement_new_home_blocked")
    return {
        "status": "REPLACED_ROLLBACK_AVAILABLE",
        "destination": str(dest),
        "rollback_home": str(rollback),
        "journal": str(journal_path),
        "operation_id": operation_id,
        "health": new_health,
        "finalize_available": True,
    }


def _old_home_identity_matches(path: Path, data: Dict[str, Any]) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        stat = path.stat()
        expected_dev = int(data["old_root_device"])
        expected_ino = int(data["old_root_inode"])
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return int(stat.st_dev) == expected_dev and int(stat.st_ino) == expected_ino


def _bind_replacement_discard(
    data: Dict[str, Any], path: Path, *, role: str,
) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    data["discard_source_role"] = role
    data["discard_root_device"] = int(info.st_dev)
    data["discard_root_inode"] = int(info.st_ino)
    return True


def _path_entry_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _replacement_discard_identity_matches(
    path: Path, data: Dict[str, Any], *, role: str,
) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        info = path.stat()
        expected_role = data["discard_source_role"]
        expected_dev = int(data["discard_root_device"])
        expected_ino = int(data["discard_root_inode"])
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return (
        expected_role == role
        and int(info.st_dev) == expected_dev
        and int(info.st_ino) == expected_ino
    )


def _delete_open_directory_contents(directory_fd: int) -> bool:
    try:
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
    except OSError:
        return False
    for entry in entries:
        name = entry.name
        try:
            _validate_child_name(name)
            info = entry.stat(follow_symlinks=False)
        except (OSError, ValueError):
            return False
        if statmod.S_ISDIR(info.st_mode):
            try:
                child_fd = _open_directory_at(directory_fd, name)
            except ValueError:
                return False
            try:
                child_info = os.fstat(child_fd)
                child_identity = (
                    int(child_info.st_dev), int(child_info.st_ino)
                )
                if not _delete_open_directory_contents(child_fd):
                    return False
                try:
                    current = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    return False
                if (
                    not statmod.S_ISDIR(current.st_mode)
                    or (int(current.st_dev), int(current.st_ino))
                    != child_identity
                ):
                    return False
            finally:
                os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError:
                return False
            continue
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            return False
    return True


def _delete_bound_replacement_discard(
    path: Path, data: Dict[str, Any], *, role: str,
) -> bool:
    try:
        parent_fd = _open_directory_path(path.parent)
    except ValueError:
        return False
    try:
        try:
            discard_fd = _open_directory_at(parent_fd, path.name)
        except ValueError:
            return False
        try:
            info = os.fstat(discard_fd)
            expected_role = data.get("discard_source_role")
            try:
                expected_dev = int(data["discard_root_device"])
                expected_ino = int(data["discard_root_inode"])
            except (KeyError, TypeError, ValueError):
                return False
            root_identity = (int(info.st_dev), int(info.st_ino))
            if (
                expected_role != role
                or root_identity != (expected_dev, expected_ino)
            ):
                return False
            if not _delete_open_directory_contents(discard_fd):
                return False
            try:
                current = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                return False
            if (
                not statmod.S_ISDIR(current.st_mode)
                or (int(current.st_dev), int(current.st_ino))
                != root_identity
            ):
                return False
        finally:
            os.close(discard_fd)
        try:
            os.rmdir(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError:
            return False
        return True
    finally:
        os.close(parent_fd)


def reconcile_home_replacement(
    destination: Path, *, resume: bool = False, finalize: bool = False,
    rollback: bool = False,
) -> Dict[str, Any]:
    if sum(bool(item) for item in (resume, finalize, rollback)) > 1:
        raise ValueError("replacement_action_conflict")
    dest = destination.expanduser().resolve()
    journal, data = _replacement_load(dest)
    prepared = Path(data["prepared_home"])
    rollback_home = Path(data["rollback_home"])
    discard = Path(data["discard_home"])
    failed = Path(data["failed_home"])
    parent = dest.parent
    status = data.get("status")
    archive_sha = data.get("archive_sha256")
    if not isinstance(archive_sha, str):
        raise ValueError("replacement_journal_invalid")

    dest_exists = dest.is_dir() and not dest.is_symlink()
    prepared_exists = prepared.is_dir() and not prepared.is_symlink()
    rollback_exists = rollback_home.is_dir() and not rollback_home.is_symlink()
    discard_exists = discard.is_dir() and not discard.is_symlink()
    failed_exists = failed.is_dir() and not failed.is_symlink()
    dest_is_new = dest_exists and _restored_home_matches(dest, archive_sha)

    if status == "PREPARED":
        if not dest_exists and rollback_exists and prepared_exists:
            status = "OLD_MOVED"
            data["status"] = status
            _replacement_write(journal, data)
        elif dest_is_new and rollback_exists and not prepared_exists:
            status = "NEW_ACTIVE"
            data["status"] = status
            _replacement_write(journal, data)
        elif not (dest_exists and prepared_exists and not rollback_exists):
            return {
                "status": "BLOCKED",
                "reason": "replacement_state_ambiguous",
                "journal_status": status,
            }

    if status == "PREPARED":
        if not resume:
            return {
                "status": "PREPARED",
                "resume_available": True,
                "destination": str(dest),
                "prepared_home": str(prepared),
                "journal": str(journal),
            }
        _replacement_rename(dest, rollback_home)
        _fsync_directory(parent)
        status = "OLD_MOVED"
        data["status"] = status
        _replacement_write(journal, data)
        dest_exists = False
        prepared_exists = True
        rollback_exists = True

    if status == "OLD_MOVED":
        if dest_is_new and rollback_exists and not prepared_exists:
            status = "NEW_ACTIVE"
            data["status"] = status
            _replacement_write(journal, data)
        elif not dest_exists and rollback_exists and prepared_exists:
            if not resume:
                return {
                    "status": "OLD_MOVED",
                    "resume_available": True,
                    "rollback_home": str(rollback_home),
                    "prepared_home": str(prepared),
                    "journal": str(journal),
                }
            _replacement_rename(prepared, dest)
            _fsync_directory(parent)
            status = "NEW_ACTIVE"
            data["status"] = status
            _replacement_write(journal, data)
            dest_exists = True
            prepared_exists = False
            dest_is_new = _restored_home_matches(dest, archive_sha)
        else:
            return {
                "status": "BLOCKED",
                "reason": "replacement_state_ambiguous",
                "journal_status": status,
            }

    if status == "NEW_ACTIVE":
        if (
            dest_is_new and not prepared.exists()
            and not rollback_home.exists() and discard_exists
        ):
            if not _replacement_discard_identity_matches(
                discard, data, role="rollback_old"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_identity_mismatch",
                    "journal_status": status,
                }
            status = "FINALIZE_PENDING_DELETE"
            data["status"] = status
            _replacement_write(journal, data)
        elif not (
            dest.is_dir() and not dest.is_symlink()
            and rollback_home.is_dir() and not rollback_home.is_symlink()
            and not prepared.exists()
            and not discard.exists()
            and _restored_home_matches(dest, archive_sha)
        ):
            return {
                "status": "BLOCKED",
                "reason": "replacement_new_active_not_proven",
                "journal_status": status,
            }
        elif rollback:
            rollback_inventory = _standalone_home_inventory(rollback_home)
            if (
                rollback_inventory["status"] != "SAFE"
                or not _old_home_identity_matches(rollback_home, data)
                or failed.exists()
                or discard.exists()
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_rollback_home_not_proven",
                }
            status = "ROLLBACK_PREPARED"
            data["status"] = status
            _replacement_write(journal, data)
        elif not finalize:
            return {
                "status": "REPLACED_ROLLBACK_AVAILABLE",
                "destination": str(dest),
                "rollback_home": str(rollback_home),
                "journal": str(journal),
                "finalize_available": True,
                "rollback_available": True,
            }
        else:
            rollback_inventory = _standalone_home_inventory(rollback_home)
            if (
                rollback_inventory["status"] != "SAFE"
                or not _old_home_identity_matches(rollback_home, data)
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_rollback_home_not_proven",
                }
            if not _bind_replacement_discard(
                data, rollback_home, role="rollback_old"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_binding_failed",
                }
            _replacement_write(journal, data)
            _replacement_rename(rollback_home, discard)
            _fsync_directory(parent)
            status = "FINALIZE_PENDING_DELETE"
            data["status"] = status
            _replacement_write(journal, data)

    if status == "ROLLBACK_PREPARED":
        dest_is_new = (
            dest.is_dir() and not dest.is_symlink()
            and _restored_home_matches(dest, archive_sha)
        )
        rollback_is_old = _old_home_identity_matches(rollback_home, data)
        failed_is_new = (
            failed.is_dir() and not failed.is_symlink()
            and _restored_home_matches(failed, archive_sha)
        )
        if not dest.exists() and rollback_is_old and failed_is_new:
            status = "ROLLBACK_NEW_MOVED"
            data["status"] = status
            _replacement_write(journal, data)
        elif (
            _old_home_identity_matches(dest, data)
            and not rollback_home.exists()
            and failed_is_new
        ):
            status = "ROLLED_BACK"
            data["status"] = status
            _replacement_write(journal, data)
        elif not (
            dest_is_new
            and rollback_is_old
            and not failed.exists()
            and not discard.exists()
            and not prepared.exists()
        ):
            return {
                "status": "BLOCKED",
                "reason": "replacement_rollback_state_ambiguous",
                "journal_status": status,
            }
        if status == "ROLLBACK_PREPARED":
            if not rollback:
                return {
                    "status": "ROLLBACK_PREPARED",
                    "rollback_resume_available": True,
                    "journal": str(journal),
                }
            _replacement_rename(dest, failed)
            _fsync_directory(parent)
            status = "ROLLBACK_NEW_MOVED"
            data["status"] = status
            _replacement_write(journal, data)

    if status == "ROLLBACK_NEW_MOVED":
        rollback_is_old = _old_home_identity_matches(rollback_home, data)
        failed_is_new = (
            failed.is_dir() and not failed.is_symlink()
            and _restored_home_matches(failed, archive_sha)
        )
        if (
            _old_home_identity_matches(dest, data)
            and not rollback_home.exists()
            and failed_is_new
        ):
            status = "ROLLED_BACK"
            data["status"] = status
            _replacement_write(journal, data)
        elif not (
            not dest.exists()
            and rollback_is_old
            and failed_is_new
            and not discard.exists()
        ):
            return {
                "status": "BLOCKED",
                "reason": "replacement_rollback_state_ambiguous",
                "journal_status": status,
            }
        if status == "ROLLBACK_NEW_MOVED":
            if not rollback:
                return {
                    "status": "ROLLBACK_NEW_MOVED",
                    "rollback_resume_available": True,
                    "rollback_home": str(rollback_home),
                    "failed_home": str(failed),
                    "journal": str(journal),
                }
            _replacement_rename(rollback_home, dest)
            _fsync_directory(parent)
            status = "ROLLED_BACK"
            data["status"] = status
            _replacement_write(journal, data)

    if status == "ROLLED_BACK":
        failed_is_new = (
            failed.is_dir() and not failed.is_symlink()
            and _restored_home_matches(failed, archive_sha)
        )
        if (
            _old_home_identity_matches(dest, data)
            and not rollback_home.exists()
            and not failed.exists()
            and discard.is_dir()
            and not discard.is_symlink()
        ):
            if not _replacement_discard_identity_matches(
                discard, data, role="failed_new"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_identity_mismatch",
                    "journal_status": status,
                }
            status = "ROLLBACK_FINALIZE_PENDING_DELETE"
            data["status"] = status
            _replacement_write(journal, data)
        elif not (
            _old_home_identity_matches(dest, data)
            and not rollback_home.exists()
            and failed_is_new
            and not discard.exists()
            and not prepared.exists()
        ):
            return {
                "status": "BLOCKED",
                "reason": "replacement_rolled_back_not_proven",
                "journal_status": status,
            }
        if status == "ROLLED_BACK":
            if not finalize:
                return {
                    "status": "ROLLED_BACK_FORWARD_COPY_AVAILABLE",
                    "destination": str(dest),
                    "failed_home": str(failed),
                    "journal": str(journal),
                    "finalize_available": True,
                }
            if not _bind_replacement_discard(
                data, failed, role="failed_new"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_binding_failed",
                }
            _replacement_write(journal, data)
            _replacement_rename(failed, discard)
            _fsync_directory(parent)
            status = "ROLLBACK_FINALIZE_PENDING_DELETE"
            data["status"] = status
            _replacement_write(journal, data)

    if status == "ROLLBACK_FINALIZE_PENDING_DELETE":
        if not (
            _old_home_identity_matches(dest, data)
            and not rollback_home.exists()
            and not failed.exists()
            and not prepared.exists()
        ):
            return {
                "status": "BLOCKED",
                "reason": "replacement_rollback_finalize_state_ambiguous",
                "journal_status": status,
            }
        if _path_entry_exists_no_follow(discard):
            if not _replacement_discard_identity_matches(
                discard, data, role="failed_new"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_identity_mismatch",
                }
            if not finalize:
                return {
                    "status": "ROLLBACK_FINALIZE_PENDING_DELETE",
                    "finalize_available": True,
                    "discard_home": str(discard),
                    "journal": str(journal),
                }
            if not _delete_bound_replacement_discard(
                discard, data, role="failed_new"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_delete_failed",
                }
            _fsync_directory(parent)
        elif not finalize:
            return {
                "status": "ROLLBACK_FINALIZE_PENDING_DELETE",
                "finalize_available": True,
                "discard_home": str(discard),
                "journal": str(journal),
            }
        receipt = {
            "schema_version": 1,
            "operation_id": data["operation_id"],
            "finalized_at": utc_now(),
            "archive_sha256": archive_sha,
            "outcome": "ROLLED_BACK",
            "failed_new_home_discarded": str(failed),
        }
        receipt_path = dest / "replacement-receipt.json"
        atomic_write_json(receipt_path, receipt, mode=0o600)
        data["status"] = "COMPLETE"
        data["outcome"] = "ROLLED_BACK"
        _replacement_write(journal, data)
        journal.unlink()
        _fsync_directory(parent)
        return {
            "status": "COMPLETE",
            "outcome": "ROLLED_BACK",
            "destination": str(dest),
            "receipt": str(receipt_path),
            "failed_new_home_removed": True,
        }

    if status == "FINALIZE_PENDING_DELETE":
        if not (
            dest.is_dir() and not dest.is_symlink()
            and not prepared.exists()
            and not rollback_home.exists()
            and _restored_home_matches(dest, archive_sha)
        ):
            return {
                "status": "BLOCKED",
                "reason": "replacement_finalize_state_ambiguous",
                "journal_status": status,
            }
        if _path_entry_exists_no_follow(discard):
            if not _replacement_discard_identity_matches(
                discard, data, role="rollback_old"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_identity_mismatch",
                }
            if not finalize:
                return {
                    "status": "FINALIZE_PENDING_DELETE",
                    "finalize_available": True,
                    "discard_home": str(discard),
                    "journal": str(journal),
                }
            if not _delete_bound_replacement_discard(
                discard, data, role="rollback_old"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "replacement_discard_delete_failed",
                }
            _fsync_directory(parent)
        elif not finalize:
            return {
                "status": "FINALIZE_PENDING_DELETE",
                "finalize_available": True,
                "discard_home": str(discard),
                "journal": str(journal),
            }
        receipt = {
            "schema_version": 1,
            "operation_id": data["operation_id"],
            "finalized_at": utc_now(),
            "archive_sha256": archive_sha,
            "rollback_discarded": str(rollback_home),
        }
        receipt_path = dest / "replacement-receipt.json"
        atomic_write_json(receipt_path, receipt, mode=0o600)
        data["status"] = "COMPLETE"
        _replacement_write(journal, data)
        journal.unlink()
        _fsync_directory(parent)
        return {
            "status": "COMPLETE",
            "destination": str(dest),
            "receipt": str(receipt_path),
            "rollback_removed": True,
        }

    if status == "COMPLETE":
        return {"status": "COMPLETE", "destination": str(dest)}

    return {
        "status": "BLOCKED",
        "reason": "replacement_journal_status_unknown",
        "journal_status": status,
    }


def _replacement_artifact_census(
    destination: Path, *, referenced: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    dest = destination.expanduser().resolve()
    parent = dest.parent
    refs = {
        str(item.expanduser().resolve())
        for item in (referenced or [])
    }
    prefixes = (
        f".{dest.name}.replacement-",
        f".{dest.name}.rollback-",
        f".{dest.name}.discard-",
        f".{dest.name}.failed-",
        f".{dest.name}.restore-",
        f"..{dest.name}.replacement-",
    )
    artifacts: List[Dict[str, Any]] = []
    if not parent.exists() or parent.is_symlink() or not parent.is_dir():
        return {
            "status": "BLOCKED",
            "reason": "replacement_parent_unsafe",
            "bytes": 0,
            "artifacts": [],
            "unmanaged": [],
        }
    for item in sorted(parent.iterdir(), key=lambda candidate: candidate.name):
        if not any(item.name.startswith(prefix) for prefix in prefixes):
            continue
        record: Dict[str, Any] = {
            "path": str(item),
            "name": item.name,
            "referenced": str(item.resolve()) in refs if not item.is_symlink() else False,
        }
        if item.is_symlink():
            record["status"] = "UNSAFE_SYMLINK"
            record["bytes"] = 0
        elif item.is_dir():
            size = _safe_tree_size(item)
            if size is None:
                record["status"] = "UNSAFE_TREE"
                record["bytes"] = 0
            else:
                record["status"] = "REFERENCED" if record["referenced"] else "UNMANAGED"
                record["bytes"] = size
        elif item.is_file():
            size = _regular_file_size(item)
            record["status"] = "REFERENCED" if record["referenced"] else "UNMANAGED"
            record["bytes"] = int(size or 0)
        else:
            record["status"] = "UNSAFE_NODE"
            record["bytes"] = 0
        artifacts.append(record)
    unmanaged = [
        item for item in artifacts
        if item["status"] != "REFERENCED"
    ]
    return {
        "status": "ATTENTION" if unmanaged else "READY",
        "bytes": sum(int(item.get("bytes", 0)) for item in artifacts),
        "artifacts": artifacts,
        "unmanaged": unmanaged,
    }


def inspect_home_replacement(destination: Path) -> Dict[str, Any]:
    dest = destination.expanduser().resolve()
    journal_path = _replacement_journal_path(dest)
    if journal_path.is_symlink():
        return {
            "status": "BLOCKED",
            "classification": "REPLACEMENT_JOURNAL_UNSAFE",
            "destination": str(dest),
            "journal": str(journal_path),
            "safe_next_steps": [],
        }
    if not journal_path.is_file():
        census = _replacement_artifact_census(dest)
        if census["status"] != "READY":
            return {
                "status": "ATTENTION" if census["status"] == "ATTENTION" else "BLOCKED",
                "classification": "ORPHAN_REPLACEMENT_ARTIFACTS",
                "destination": str(dest),
                "journal": str(journal_path),
                "artifact_census": census,
                "safe_next_steps": [],
                "operator_note": (
                    "No authoritative replacement journal exists. "
                    "Do not delete sibling artifacts automatically."
                ),
                "automatic_action": False,
            }
        return {
            "status": "CLEAN",
            "classification": "NO_REPLACEMENT",
            "destination": str(dest),
            "journal": str(journal_path),
            "artifact_census": census,
            "safe_next_steps": [],
        }
    try:
        journal, data = _replacement_load(dest)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return {
            "status": "BLOCKED",
            "classification": "REPLACEMENT_JOURNAL_INVALID",
            "destination": str(dest),
            "journal": str(journal_path),
            "reason": str(exc),
            "safe_next_steps": [],
        }

    prepared = Path(data["prepared_home"])
    rollback_home = Path(data["rollback_home"])
    discard = Path(data["discard_home"])
    failed = Path(data["failed_home"])
    census = _replacement_artifact_census(
        dest, referenced=[prepared, rollback_home, discard, failed]
    )
    archive_sha = data.get("archive_sha256")
    if not isinstance(archive_sha, str):
        return {
            "status": "BLOCKED",
            "classification": "REPLACEMENT_JOURNAL_INVALID",
            "destination": str(dest),
            "journal": str(journal),
            "reason": "archive_sha256_missing",
            "safe_next_steps": [],
        }

    observed = {
        "destination_exists": dest.is_dir() and not dest.is_symlink(),
        "destination_is_new": _restored_home_matches(dest, archive_sha),
        "destination_is_old": _old_home_identity_matches(dest, data),
        "prepared_exists": prepared.is_dir() and not prepared.is_symlink(),
        "prepared_is_new": _restored_home_matches(prepared, archive_sha),
        "rollback_exists": rollback_home.is_dir() and not rollback_home.is_symlink(),
        "rollback_is_old": _old_home_identity_matches(rollback_home, data),
        "failed_exists": failed.is_dir() and not failed.is_symlink(),
        "failed_is_new": _restored_home_matches(failed, archive_sha),
        "discard_exists": discard.is_dir() and not discard.is_symlink(),
    }
    status = data.get("status")
    classification = None
    actions: List[str] = []

    if status == "PREPARED":
        if (
            observed["destination_exists"]
            and not observed["destination_is_new"]
            and observed["prepared_is_new"]
            and not observed["rollback_exists"]
        ):
            classification = "REPLACEMENT_PREPARED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --resume"
            ]
        elif (
            not observed["destination_exists"]
            and observed["rollback_is_old"]
            and observed["prepared_is_new"]
        ):
            classification = "REPLACEMENT_GAP_OLD_MOVED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --resume"
            ]
        elif (
            observed["destination_is_new"]
            and observed["rollback_is_old"]
            and not observed["prepared_exists"]
        ):
            classification = "REPLACEMENT_GAP_NEW_ACTIVE"
            actions = [
                f"orch state replace-reconcile --destination {dest}",
                f"orch state replace-reconcile --destination {dest} --rollback",
                f"orch state replace-reconcile --destination {dest} --finalize",
            ]
    elif status == "OLD_MOVED":
        if (
            not observed["destination_exists"]
            and observed["rollback_is_old"]
            and observed["prepared_is_new"]
        ):
            classification = "REPLACEMENT_OLD_MOVED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --resume"
            ]
        elif (
            observed["destination_is_new"]
            and observed["rollback_is_old"]
            and not observed["prepared_exists"]
        ):
            classification = "REPLACEMENT_GAP_NEW_ACTIVE"
            actions = [
                f"orch state replace-reconcile --destination {dest}",
                f"orch state replace-reconcile --destination {dest} --rollback",
                f"orch state replace-reconcile --destination {dest} --finalize",
            ]
    elif status == "NEW_ACTIVE":
        if (
            observed["destination_is_new"]
            and observed["rollback_is_old"]
            and not observed["prepared_exists"]
            and not observed["discard_exists"]
            and not observed["failed_exists"]
        ):
            classification = "REPLACEMENT_ROLLBACK_AVAILABLE"
            actions = [
                f"orch state replace-reconcile --destination {dest} --rollback",
                f"orch state replace-reconcile --destination {dest} --finalize",
            ]
        elif (
            observed["destination_is_new"]
            and not observed["rollback_exists"]
            and observed["discard_exists"]
        ):
            classification = "REPLACEMENT_GAP_FINALIZE_DISCARD_MOVED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --finalize"
            ]
    elif status == "FINALIZE_PENDING_DELETE":
        if (
            observed["destination_is_new"]
            and not observed["rollback_exists"]
            and not observed["prepared_exists"]
        ):
            classification = "REPLACEMENT_FINALIZE_PENDING"
            actions = [
                f"orch state replace-reconcile --destination {dest} --finalize"
            ]
    elif status == "ROLLBACK_PREPARED":
        if (
            observed["destination_is_new"]
            and observed["rollback_is_old"]
            and not observed["failed_exists"]
        ):
            classification = "ROLLBACK_PREPARED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --rollback"
            ]
        elif (
            not observed["destination_exists"]
            and observed["rollback_is_old"]
            and observed["failed_is_new"]
        ):
            classification = "ROLLBACK_GAP_NEW_MOVED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --rollback"
            ]
        elif (
            observed["destination_is_old"]
            and not observed["rollback_exists"]
            and observed["failed_is_new"]
        ):
            classification = "ROLLBACK_GAP_OLD_RESTORED"
            actions = [
                f"orch state replace-reconcile --destination {dest}",
                f"orch state replace-reconcile --destination {dest} --finalize",
            ]
    elif status == "ROLLBACK_NEW_MOVED":
        if (
            not observed["destination_exists"]
            and observed["rollback_is_old"]
            and observed["failed_is_new"]
        ):
            classification = "ROLLBACK_NEW_MOVED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --rollback"
            ]
        elif (
            observed["destination_is_old"]
            and not observed["rollback_exists"]
            and observed["failed_is_new"]
        ):
            classification = "ROLLBACK_GAP_OLD_RESTORED"
            actions = [
                f"orch state replace-reconcile --destination {dest}",
                f"orch state replace-reconcile --destination {dest} --finalize",
            ]
    elif status == "ROLLED_BACK":
        if (
            observed["destination_is_old"]
            and not observed["rollback_exists"]
            and observed["failed_is_new"]
            and not observed["discard_exists"]
        ):
            classification = "ROLLED_BACK_FORWARD_COPY_AVAILABLE"
            actions = [
                f"orch state replace-reconcile --destination {dest} --finalize"
            ]
        elif (
            observed["destination_is_old"]
            and not observed["rollback_exists"]
            and not observed["failed_exists"]
            and observed["discard_exists"]
        ):
            classification = "ROLLBACK_GAP_FINALIZE_DISCARD_MOVED"
            actions = [
                f"orch state replace-reconcile --destination {dest} --finalize"
            ]
    elif status == "ROLLBACK_FINALIZE_PENDING_DELETE":
        if (
            observed["destination_is_old"]
            and not observed["rollback_exists"]
            and not observed["failed_exists"]
        ):
            classification = "ROLLBACK_FINALIZE_PENDING"
            actions = [
                f"orch state replace-reconcile --destination {dest} --finalize"
            ]

    if classification is None:
        return {
            "status": "BLOCKED",
            "classification": "REPLACEMENT_STATE_AMBIGUOUS",
            "destination": str(dest),
            "journal": str(journal),
            "journal_status": status,
            "operation_id": data.get("operation_id"),
            "observed": observed,
            "artifact_census": census,
            "safe_next_steps": [],
        }
    return {
        "status": "ATTENTION",
        "classification": classification,
        "destination": str(dest),
        "journal": str(journal),
        "journal_status": status,
        "operation_id": data.get("operation_id"),
        "observed": observed,
        "artifact_census": census,
        "safe_next_steps": actions,
        "automatic_action": False,
    }


def backup_state(orch: Orchestrator, output: Path | None = None) -> Dict[str, Any]:
    health = check_state(orch)
    if health["status"] == "BLOCKED":
        raise ValueError("state_integrity_blocked")
    if health["active_runs"]:
        raise ValueError("active_runs_present")
    backups = ensure_private_dir(orch.root / "backups")
    target = (
        Path(os.path.abspath(os.path.expanduser(str(output))))
        if output
        else backups
        / f"orch-state-{utc_now().replace(':','').replace('+0000','Z')}.zip"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("backup_output_unsafe")
    with tempfile.TemporaryDirectory(prefix="orch-backup-", dir=str(orch.runtime)) as tmp:
        db_copy = Path(tmp) / "orch.sqlite3"
        source = orch.connect()
        dest = sqlite3.connect(str(db_copy))
        try:
            source.backup(dest)
        finally:
            dest.close(); source.close()
        manifest = {
            "schema_version": STATE_SCHEMA_VERSION,
            "created_at": utc_now(),
            "source_root": str(orch.root),
            "database_sha256": sha256_file(db_copy),
            "excluded_secret_classes": [
                "claims", "capability_files", "provider_credentials",
            ],
            "file_evidence": {},
        }
        fd = -1
        temp_zip: Optional[Path] = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=target.name + ".tmp-",
                dir=str(target.parent),
            )
            temp_zip = Path(temp_name)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w+b", closefd=True) as handle:
                fd = -1
                with zipfile.ZipFile(
                    handle, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.write(db_copy, "state/orch.sqlite3")
                    evidence: Dict[str, Dict[str, Any]] = {}
                    for path in _backup_members(orch):
                        arcname, item = _stream_backup_member(
                            archive, orch, path
                        )
                        evidence[arcname] = item
                    manifest["file_evidence"] = evidence
                    archive.writestr(
                        "state/manifest.json",
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp_zip), str(target))
            temp_zip = None
            _fsync_directory(target.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_zip is not None:
                try:
                    temp_zip.unlink()
                except FileNotFoundError:
                    pass
    return {
        "status": "BACKED_UP",
        "path": str(target),
        "sha256": sha256_file(target),
        "bytes": target.stat().st_size,
        "manifest": manifest,
    }
