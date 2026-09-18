from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict, List
import zipfile

from .core import ACTIVE_RUN_STATES, STATE_SCHEMA_VERSION, Orchestrator, utc_now


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
        pending_publications = [dict(row) for row in conn.execute(
            "SELECT run_id,status,commit_id,remote_commit,error,updated_at FROM publications "
            "WHERE status NOT IN ('COMPLETE','ABANDONED') ORDER BY updated_at"
        )]
    quick_values = [row[0] for row in quick]
    caps = capability_health(orch)
    blocked = quick_values != ["ok"] or bool(foreign)
    attention = bool(active) or caps["status"] != "READY" or bool(pending_publications)
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
        "pending_publications": pending_publications,
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


def _backup_members(orch: Orchestrator) -> List[Path]:
    members: List[Path] = []
    for relative in ("config.json", "rdc-bootstrap.json", "dispatcher-prompt.txt"):
        path = orch.root / relative
        if path.is_file() and not path.is_symlink():
            members.append(path)
    for base in (orch.root / "projects", orch.runtime / "logs", orch.runtime / "worker_receipts"):
        if base.is_dir():
            members.extend(path for path in base.rglob("*") if path.is_file() and not path.is_symlink())
    review_root = orch.runtime / "review_exports"
    if review_root.is_dir():
        members.extend(path for path in review_root.rglob("*") if path.is_file() and not path.is_symlink())
    return sorted(set(members))


def backup_state(orch: Orchestrator, output: Path | None = None) -> Dict[str, Any]:
    health = check_state(orch)
    if health["status"] == "BLOCKED":
        raise ValueError("state_integrity_blocked")
    if health["active_runs"]:
        raise ValueError("active_runs_present")
    backups = orch.root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
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
