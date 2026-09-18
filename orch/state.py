from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import zipfile

from .config import ensure_private_dir
from .core import (ACTIVE_RUN_STATES, STATE_SCHEMA_VERSION, WRITER_LOCK_RUN_STATES,
                   Orchestrator, utc_now)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()




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
    claims.mkdir(parents=True, exist_ok=True)
    with orch.connect() as conn:
        rows = conn.execute("SELECT run_id,state FROM runs").fetchall()
    states = {row["run_id"]: row["state"] for row in rows}
    expected = {rid for rid, state in states.items() if state in {"RUNNING", "RESULT_SUBMITTED"}}
    present = {path.stem for path in claims.glob("*.json") if path.is_file() and not path.is_symlink()}
    orphan = sorted(rid for rid in present if states.get(rid) not in {"RUNNING", "RESULT_SUBMITTED"})
    missing = sorted(expected - present)
    return {
        "status": "ATTENTION" if orphan or missing else "READY",
        "orphan_capabilities": orphan,
        "missing_capabilities": missing,
        "expected_live_capabilities": sorted(expected),
    }


def permission_health(orch: Orchestrator) -> Dict[str, Any]:
    targets = [
        (orch.runtime, 0o700, True),
        (orch.runtime / "logs", 0o700, True),
        (orch.runtime / "worker_receipts", 0o700, True),
        (orch.runtime / "claims", 0o700, True),
        (orch.runtime / "review_exports", 0o700, True),
        (orch.runtime / "git-hooks-disabled", 0o700, True),
        (orch.db_path, 0o600, True),
        (orch.db_path.with_name(orch.db_path.name + "-wal"), 0o600, False),
        (orch.db_path.with_name(orch.db_path.name + "-shm"), 0o600, False),
        (orch.root / "projects", 0o700, False),
        (orch.root / "plans", 0o700, False),
        (orch.root / "backups", 0o700, False),
    ]
    rows: List[Dict[str, Any]] = []
    blocked = False
    for path, expected, required in targets:
        if not path.exists():
            if required:
                blocked = True
                rows.append({
                    "path": str(path), "status": "MISSING",
                    "expected_mode": oct(expected), "actual_mode": None,
                })
            continue
        if path.is_symlink():
            blocked = True
            rows.append({
                "path": str(path), "status": "UNSAFE_SYMLINK",
                "expected_mode": oct(expected), "actual_mode": None,
            })
            continue
        actual = path.stat().st_mode & 0o777
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
    blocked = quick_values != ["ok"] or bool(foreign) or permissions["status"] != "READY"
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
            capability.exists() and capability.is_file() and not capability.is_symlink()
        )
        heartbeat_ts = _parse_utc(row["heartbeat_at"])
        heartbeat_age = (
            max(0, int(now - heartbeat_ts)) if heartbeat_ts is not None else None
        )
        commands: List[str] = []
        classification = "TERMINAL"

        if unresolved_publication:
            classification = "PUBLICATION_RECONCILIATION_REQUIRED"
            commands = [
                f"orch publish-reconcile --run-id {row['run_id']}",
                "Use --resume only when reconciliation reports resume_available=true.",
            ]
        elif row["state"] == "RUNNING":
            if capability_present:
                classification = "WORKER_MAY_STILL_BE_ACTIVE"
                commands = [
                    "Verify the external ChatGPT/RDC worker state; heartbeat age is informational only.",
                    f"orch abort --run-id {row['run_id']} --reason <reason> --retry",
                ]
            else:
                classification = "CAPABILITY_MISSING"
                commands = [
                    "Do not recreate a lease/capability file.",
                    f"orch abort --run-id {row['run_id']} --reason <reason> --retry",
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
            "publication": publication,
            "safe_next_steps": commands,
        }
        items.append(item)

    caps = capability_health(orch)
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
    claims = orch.runtime / "claims"
    pruned: List[str] = []
    for run_id in health["capabilities"]["orphan_capabilities"]:
        path = claims / f"{run_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"invalid_capability_artifact:{run_id}")
        path.unlink()
        pruned.append(run_id)
    return {"status": "PRUNED", "count": len(pruned), "run_ids": pruned}



def _parse_utc(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    import datetime
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _regular_file_size(path: Path) -> Optional[int]:
    if not path.is_file() or path.is_symlink():
        return None
    return path.stat().st_size


def _safe_tree_size(path: Path) -> Optional[int]:
    if not path.is_dir() or path.is_symlink():
        return None
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            return None
        if item.is_file():
            total += item.stat().st_size
    return total


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
            size = _regular_file_size(log)
            if size is None:
                continue
            artifacts.append({"kind": "log", "path": str(log), "bytes": size})
        receipt = orch.runtime / "worker_receipts" / f"{run_id}.json"
        receipt_size = _regular_file_size(receipt)
        if receipt_size is not None:
            artifacts.append({"kind": "worker_receipt", "path": str(receipt), "bytes": receipt_size})
        export = orch.runtime / "review_exports" / run_id
        export_size = _safe_tree_size(export)
        if export_size is not None:
            artifacts.append({"kind": "review_export", "path": str(export), "bytes": export_size})
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
            if log.exists():
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
            if item.name.startswith("orch-state-") and item.suffix == ".zip" and item.is_file() and not item.is_symlink():
                backups.append({
                    "path": str(item),
                    "bytes": item.stat().st_size,
                    "mtime": item.stat().st_mtime,
                })
            elif item.exists():
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
    for item in terminal:
        age_due = item["reference_ts"] <= cutoff
        budget_due = total > max_evidence_bytes and item["run_id"] not in newest_keep
        if not age_due and not budget_due:
            continue
        removed = 0
        paths = []
        for artifact in item["artifacts"]:
            path = Path(artifact["path"])
            if artifact["kind"] == "review_export":
                if _safe_tree_size(path) is None:
                    continue
                removed += artifact["bytes"]
                paths.append(str(path))
                shutil.rmtree(path)
            else:
                if _regular_file_size(path) is None:
                    continue
                removed += artifact["bytes"]
                paths.append(str(path))
                path.unlink()
        if removed:
            total -= removed
            deleted_runs.append({"run_id": item["run_id"], "bytes": removed, "paths": paths})
    backups = sorted(inventory["backups"], key=lambda item: (item["mtime"], item["path"]))
    backup_total = sum(item["bytes"] for item in backups)
    deleted_backups: List[Dict[str, Any]] = []
    while len(backups) > keep_backups or backup_total > max_backup_bytes:
        if len(backups) <= 1:
            break
        item = backups.pop(0)
        path = Path(item["path"])
        if _regular_file_size(path) is None:
            continue
        path.unlink()
        backup_total -= item["bytes"]
        deleted_backups.append({"path": str(path), "bytes": item["bytes"]})
    after = retention_status(
        orch,
        max_evidence_bytes=max_evidence_bytes,
        max_backup_bytes=max_backup_bytes,
        keep_backups=keep_backups,
    )
    after["status"] = "PRUNED" if deleted_runs or deleted_backups else after["status"]
    after["pruned"] = {
        "runs": deleted_runs,
        "run_count": len(deleted_runs),
        "evidence_bytes": sum(item["bytes"] for item in deleted_runs),
        "backups": deleted_backups,
        "backup_count": len(deleted_backups),
        "backup_bytes": sum(item["bytes"] for item in deleted_backups),
    }
    after["bounded"] = (
        after["evidence"]["bytes"] + after["evidence"]["unmanaged_bytes"] <= max_evidence_bytes
        and after["backups"]["bytes"] <= max_backup_bytes
        and after["backups"]["count"] <= keep_backups
        and not after["evidence"]["unmanaged"]
        and not after["backups"]["unmanaged"]
    )
    return after


def _backup_members(orch: Orchestrator) -> List[Path]:
    members: List[Path] = []
    for relative in ("config.json", "rdc-bootstrap.json", "dispatcher-prompt.txt"):
        path = orch.root / relative
        if path.is_file() and not path.is_symlink():
            members.append(path)
    for base in (
        orch.root / "projects", orch.root / "plans",
        orch.runtime / "logs", orch.runtime / "worker_receipts",
    ):
        if base.is_dir():
            members.extend(path for path in base.rglob("*") if path.is_file() and not path.is_symlink())
    review_root = orch.runtime / "review_exports"
    if review_root.is_dir():
        members.extend(path for path in review_root.rglob("*") if path.is_file() and not path.is_symlink())
    return sorted(set(members))


def verify_backup_archive(
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
                ):
                    unsafe.append(name)
                    continue
                if not (
                    name in {"state/orch.sqlite3", "state/manifest.json"}
                    or name.startswith("files/")
                ):
                    unsafe.append(name)
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
            required_exclusions = {"claims", "capability_files", "provider_credentials"}
            if not isinstance(excluded, list) or not required_exclusions.issubset(set(excluded)):
                base["errors"].append("backup_secret_exclusion_contract_missing")
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


def backup_state(orch: Orchestrator, output: Path | None = None) -> Dict[str, Any]:
    health = check_state(orch)
    if health["status"] == "BLOCKED":
        raise ValueError("state_integrity_blocked")
    if health["active_runs"]:
        raise ValueError("active_runs_present")
    backups = ensure_private_dir(orch.root / "backups")
    target = output.expanduser().resolve() if output else backups / f"orch-state-{utc_now().replace(':','').replace('+0000','Z')}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
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
            "database_sha256": _sha256(db_copy),
            "excluded_secret_classes": ["claims", "capability_files", "provider_credentials"],
        }
        temp_zip = target.with_name(target.name + f".tmp-{os.getpid()}")
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_copy, "state/orch.sqlite3")
            archive.writestr("state/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            for path in _backup_members(orch):
                archive.write(path, "files/" + path.relative_to(orch.root).as_posix())
        os.chmod(temp_zip, 0o600)
        os.replace(temp_zip, target)
    return {
        "status": "BACKED_UP",
        "path": str(target),
        "sha256": _sha256(target),
        "bytes": target.stat().st_size,
        "manifest": manifest,
    }
