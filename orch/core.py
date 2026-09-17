from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .review_policy import decide_review, normalize_review_policy

ACTIVE_RUN_STATES = {"RUNNING", "RESULT_SUBMITTED", "QUIESCING", "VERIFYING", "REVIEWING"}
READY_TASK_STATES = {"PLANNED", "READY", "NEEDS_FIX"}


def utc_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def safe_workspace_path(workspace: Path, relative: str, *, must_exist: bool = False) -> Path:
    if not relative or relative.startswith("/") or "\x00" in relative:
        raise ValueError("invalid_relative_path")
    raw = workspace / relative
    probe = raw if raw.exists() else raw.parent
    resolved_probe = probe.resolve()
    if not _inside(workspace.resolve(), resolved_probe):
        raise ValueError("path_escape")
    if raw.exists() and raw.is_symlink():
        raise ValueError("symlink_not_allowed")
    if must_exist and not raw.exists():
        raise ValueError("missing_path")
    return raw


def normalize_relative_path(relative: str) -> str:
    if not isinstance(relative, str) or not relative or "\x00" in relative or relative.startswith("/"):
        raise ValueError("invalid_relative_path")
    value = relative
    while value.startswith("./"):
        value = value[2:]
    parts = Path(value).parts
    if not value or any(part == ".." for part in parts):
        raise ValueError("path_escape")
    return Path(value).as_posix()


def path_allowed(relative: str, allowlist: Iterable[str]) -> bool:
    try:
        normalized = normalize_relative_path(relative)
    except ValueError:
        return False
    for entry in allowlist:
        directory = entry.endswith("/")
        try:
            item = normalize_relative_path(entry.rstrip("/"))
        except ValueError:
            continue
        if normalized == item:
            return True
        if directory and normalized.startswith(item + "/"):
            return True
    return False


def validate_task_definition(item: Dict[str, Any]) -> None:
    workspace = Path(item.get("workspace", ""))
    if not workspace.is_absolute():
        raise ValueError("workspace_must_be_absolute")
    allowed = item.get("allowed_paths", [])
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("allowed_paths_required")
    for relative in allowed:
        if not isinstance(relative, str) or not relative:
            raise ValueError("invalid_allowed_path")
        normalize_relative_path(relative.rstrip("/"))
    protected = item.get("protected_paths", {})
    if not isinstance(protected, dict):
        raise ValueError("invalid_protected_paths")
    for relative, digest in protected.items():
        normalize_relative_path(relative)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid_protected_hash")
    checks = item.get("checks", [])
    if not isinstance(checks, list):
        raise ValueError("invalid_checks")
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("invalid_check")
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
            raise ValueError("invalid_check_argv")
        cwd = check.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("invalid_check_cwd")
        if cwd != ".":
            normalize_relative_path(cwd)
        timeout = check.get("timeout_sec", 30)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120:
            raise ValueError("invalid_check_timeout")
    attempts = item.get("max_attempts", 2)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 20:
        raise ValueError("invalid_max_attempts")


def validate_dependency_graph(tasks: List[Dict[str, Any]]) -> None:
    graph = {item["id"]: list(item.get("dependencies", [])) for item in tasks}
    state: Dict[str, int] = {}
    def visit(task_id: str) -> None:
        current = state.get(task_id, 0)
        if current == 1:
            raise ValueError("dependency_cycle")
        if current == 2:
            return
        state[task_id] = 1
        for dep in graph[task_id]:
            visit(dep)
        state[task_id] = 2
    for task_id in graph:
        visit(task_id)


class Orchestrator:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.runtime = self.root / ".runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db_path = self.runtime / "orch.sqlite3"
        self.logs = self.runtime / "logs"
        self.logs.mkdir(exist_ok=True)
        (self.runtime / "worker_receipts").mkdir(exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _capability_path(self, run_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9._-]+", run_id) is None:
            raise ValueError("invalid_run_id")
        return self.runtime / "claims" / f"{run_id}.json"

    def _revoke_capability(self, run_id: str) -> bool:
        path = self._capability_path(run_id)
        if not path.exists():
            return False
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid_capability_artifact")
        path.unlink()
        return True

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS plans (
          plan_revision TEXT PRIMARY KEY, source_digest TEXT NOT NULL, loaded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
          task_id TEXT PRIMARY KEY, plan_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,
          status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          worker_id TEXT NOT NULL, state TEXT NOT NULL, lease_token TEXT NOT NULL,
          started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, submitted_at TEXT,
          snapshot_id TEXT, receipt_json TEXT, verify_status TEXT, review_status TEXT,
          feedback_json TEXT, completed_at TEXT, error TEXT,
          FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, manifest_json TEXT NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
          run_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, approved_at TEXT NOT NULL, note TEXT,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
          task_id TEXT, run_id TEXT, payload_json TEXT NOT NULL
        );
        """
        with self.connect() as conn:
            conn.executescript(schema)

    def _event(self, conn: sqlite3.Connection, kind: str, *, task_id: str = None,
               run_id: str = None, payload: Dict[str, Any] = None) -> None:
        conn.execute("INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES(?,?,?,?,?)",
                     (utc_now(), kind, task_id, run_id, canonical_json(payload or {})))

    def load_plan(self, plan_path: Path) -> Dict[str, Any]:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not data.get("plan_revision"):
            raise ValueError("invalid_plan")
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("empty_plan")
        ids = [item.get("id") for item in tasks]
        if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
            raise ValueError("invalid_task_ids")
        known = set(ids)
        for item in tasks:
            deps = item.get("dependencies", [])
            if not isinstance(deps, list) or any(dep not in known or dep == item["id"] for dep in deps):
                raise ValueError("invalid_dependency")
            validate_task_definition(item)
        validate_dependency_graph(tasks)
        digest = sha256_file(plan_path)
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT source_digest FROM plans WHERE plan_revision=?",
                                    (data["plan_revision"],)).fetchone()
            if existing and existing["source_digest"] != digest:
                conn.execute("ROLLBACK")
                raise ValueError("plan_revision_digest_conflict")
            for item in tasks:
                task_existing = conn.execute("SELECT plan_revision FROM tasks WHERE task_id=?", (item["id"],)).fetchone()
                if task_existing and task_existing["plan_revision"] != data["plan_revision"]:
                    conn.execute("ROLLBACK")
                    raise ValueError(f"task_id_conflict:{item['id']}")
            conn.execute("INSERT OR IGNORE INTO plans(plan_revision,source_digest,loaded_at) VALUES(?,?,?)",
                         (data["plan_revision"], digest, now))
            for ordinal, item in enumerate(tasks):
                payload = dict(item)
                payload.setdefault("max_attempts", 2)
                payload.setdefault("checks", [])
                payload.setdefault("protected_paths", {})
                payload.setdefault("required_review", False)
                payload["review"] = normalize_review_policy(payload)
                payload.setdefault("owner_acceptance", False)
                payload.setdefault("publication", {"kind": "none"})
                conn.execute("INSERT OR IGNORE INTO tasks(task_id,plan_revision,ordinal,status,payload_json,updated_at) VALUES(?,?,?,?,?,?)",
                             (item["id"], data["plan_revision"], ordinal, "PLANNED", canonical_json(payload), now))
            self._event(conn, "PLAN_LOADED", payload={"revision": data["plan_revision"], "digest": digest})
            conn.execute("COMMIT")
        return {"status": "OK", "plan_revision": data["plan_revision"], "digest": digest, "task_count": len(tasks)}

    def _task_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        return json.loads(row["payload_json"])

    def _dependencies_done(self, conn: sqlite3.Connection, payload: Dict[str, Any]) -> bool:
        for dep in payload.get("dependencies", []):
            row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (dep,)).fetchone()
            if not row or row["status"] != "DONE":
                return False
        return True

    def claim(self, worker_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            paused = conn.execute("SELECT value_json FROM settings WHERE key='paused'").fetchone()
            if paused:
                conn.execute("COMMIT")
                return {"status": "PAUSED", "details": json.loads(paused["value_json"])}
            active = conn.execute("SELECT run_id,task_id,state,heartbeat_at FROM runs WHERE state IN (?,?,?,?,?) ORDER BY started_at LIMIT 1",
                                  tuple(ACTIVE_RUN_STATES)).fetchone()
            if active:
                conn.execute("COMMIT")
                return {"status": "BUSY", "active": dict(active)}
            selected = None
            for row in conn.execute("SELECT * FROM tasks ORDER BY ordinal,task_id").fetchall():
                if row["status"] not in READY_TASK_STATES:
                    continue
                payload = self._task_payload(row)
                if not self._dependencies_done(conn, payload):
                    continue
                attempts = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (row["task_id"],)).fetchone()["n"]
                if attempts >= int(payload.get("max_attempts", 2)):
                    conn.execute("UPDATE tasks SET status='BLOCKED',updated_at=? WHERE task_id=?", (now, row["task_id"]))
                    self._event(conn, "TASK_BLOCKED_MAX_ATTEMPTS", task_id=row["task_id"], payload={"attempts": attempts})
                    continue
                selected = (row, payload, attempts + 1)
                break
            if not selected:
                conn.execute("COMMIT")
                return {"status": "NO_WORK"}
            row, payload, attempt = selected
            run_id = f"{row['task_id']}-A{attempt}-{uuid.uuid4().hex[:10]}"
            lease = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO runs(run_id,task_id,attempt,worker_id,state,lease_token,started_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?)",
                         (run_id, row["task_id"], attempt, worker_id, "RUNNING", lease, now, now))
            conn.execute("UPDATE tasks SET status='IN_PROGRESS',updated_at=? WHERE task_id=?", (now, row["task_id"]))
            self._event(conn, "RUN_CLAIMED", task_id=row["task_id"], run_id=run_id,
                        payload={"attempt": attempt, "worker_id": worker_id})
            conn.execute("COMMIT")
        claims = self.runtime / "claims"
        claims.mkdir(exist_ok=True)
        cap = self._capability_path(run_id)
        cap.write_text(json.dumps({"run_id": run_id, "lease_token": lease}, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(cap, 0o600)
        return {"status": "CLAIMED", "run_id": run_id, "task_id": row["task_id"],
                "attempt": attempt, "capability_file": str(cap), "context": self.context(run_id)}

    def lease_from_capability(self, run_id: str, capability_file: Path) -> str:
        path = capability_file.resolve()
        claims = (self.runtime / "claims").resolve()
        if not _inside(claims, path) or not path.is_file() or path.is_symlink():
            raise ValueError("invalid_capability_file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("run_id") != run_id or not isinstance(data.get("lease_token"), str):
            raise ValueError("capability_identity_mismatch")
        return data["lease_token"]

    def context(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise ValueError("unknown_run")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            previous = conn.execute("SELECT feedback_json,snapshot_id,attempt FROM runs WHERE task_id=? AND run_id<>? AND feedback_json IS NOT NULL ORDER BY attempt DESC LIMIT 1",
                                    (task["task_id"], run_id)).fetchone()
            feedback = json.loads(previous["feedback_json"]) if previous and previous["feedback_json"] else None
            pack = {"schema_version": 1, "variant": "B_NEW_CHAT_PER_ATTEMPT", "run_id": run_id,
                    "task_id": task["task_id"], "attempt": run["attempt"], "plan_revision": task["plan_revision"],
                    "goal": payload.get("goal"), "non_goals": payload.get("non_goals", []),
                    "workspace": payload["workspace"], "allowed_paths": payload.get("allowed_paths", []),
                    "protected_paths": payload.get("protected_paths", {}), "checks": payload.get("checks", []),
                    "review": normalize_review_policy(payload), "owner_acceptance": bool(payload.get("owner_acceptance")),
                    "publication": payload.get("publication", {"kind": "none"}), "feedback": feedback,
                    "previous_snapshot_id": previous["snapshot_id"] if previous else None}
            if len(canonical_json(pack).encode("utf-8")) > 32768:
                raise ValueError("context_pack_too_large")
            return pack

    def heartbeat(self, run_id: str, lease_token: str) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT state,lease_token FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or not secrets.compare_digest(row["lease_token"], lease_token):
                raise ValueError("invalid_lease")
            if row["state"] != "RUNNING":
                raise ValueError("run_not_running")
            now = utc_now()
            conn.execute("UPDATE runs SET heartbeat_at=? WHERE run_id=?", (now, run_id))
            return {"status": "OK", "heartbeat_at": now}

    def submit(self, run_id: str, lease_token: str, receipt_path: Path) -> Dict[str, Any]:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or not secrets.compare_digest(run["lease_token"], lease_token):
                conn.execute("ROLLBACK")
                raise ValueError("invalid_lease")
            if run["state"] != "RUNNING":
                conn.execute("ROLLBACK")
                raise ValueError("run_not_running")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            if receipt.get("run_id") != run_id or receipt.get("task_id") != run["task_id"]:
                conn.execute("ROLLBACK")
                raise ValueError("receipt_identity_mismatch")
            changed = receipt.get("changed_paths")
            if not isinstance(changed, list) or not changed:
                conn.execute("ROLLBACK")
                raise ValueError("receipt_changed_paths_required")
            for relative in changed:
                if not isinstance(relative, str) or not path_allowed(relative, payload.get("allowed_paths", [])):
                    conn.execute("ROLLBACK")
                    raise ValueError(f"path_not_allowed:{relative}")
            now = utc_now()
            conn.execute("UPDATE runs SET state='RESULT_SUBMITTED',receipt_json=?,submitted_at=?,heartbeat_at=? WHERE run_id=?",
                         (canonical_json(receipt), now, now, run_id))
            self._event(conn, "RESULT_SUBMITTED", task_id=run["task_id"], run_id=run_id,
                        payload={"changed_paths": changed})
            conn.execute("COMMIT")
        return {"status": "RESULT_SUBMITTED", "run_id": run_id}

    def quiesce(self, run_id: str, lease_token: str) -> Dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT task_id,state,lease_token FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or not secrets.compare_digest(row["lease_token"], lease_token):
                conn.execute("ROLLBACK")
                raise ValueError("invalid_lease")
            if row["state"] != "RESULT_SUBMITTED":
                conn.execute("ROLLBACK")
                raise ValueError("result_not_submitted")
            conn.execute("UPDATE runs SET state='VERIFYING',heartbeat_at=? WHERE run_id=?", (utc_now(), run_id))
            self._event(conn, "COOPERATIVE_QUIESCENCE_CONFIRMED", task_id=row["task_id"], run_id=run_id,
                        payload={"boundary": "helper_lease_only", "direct_rdc_shell_residual_risk": True})
            conn.execute("COMMIT")
        revoked = self._revoke_capability(run_id)
        return {"status": "VERIFYING", "residual_risk": "cooperative_direct_rdc_not_os_fenced",
                "capability_revoked": revoked}

    def _snapshot(self, payload: Dict[str, Any], changed_paths: List[str]) -> Tuple[str, Dict[str, Any]]:
        workspace = Path(payload["workspace"]).resolve()
        files: Dict[str, Any] = {}
        for relative in sorted(set(changed_paths)):
            path = safe_workspace_path(workspace, relative, must_exist=False)
            if not path.exists():
                files[relative] = {"deleted": True, "sha256": None, "bytes": 0, "mode": None}
                continue
            if not path.is_file():
                raise ValueError(f"not_regular_file:{relative}")
            files[relative] = {"deleted": False, "sha256": sha256_file(path), "bytes": path.stat().st_size,
                               "mode": oct(path.stat().st_mode & 0o777)}
        protected_results: Dict[str, Any] = {}
        for relative, expected_hash in sorted(payload.get("protected_paths", {}).items()):
            path = safe_workspace_path(workspace, relative, must_exist=True)
            actual = sha256_file(path) if path.is_file() else None
            protected_results[relative] = {"expected": expected_hash, "actual": actual,
                                           "matches": actual == expected_hash}
            if actual != expected_hash:
                raise ValueError(f"protected_path_changed:{relative}")
        manifest = {"schema_version": 1, "workspace": str(workspace), "files": files,
                    "protected": protected_results, "created_at": utc_now()}
        snapshot_id = "sha256:" + sha256_bytes(canonical_json(manifest).encode("utf-8"))
        return snapshot_id, manifest

    def _run_check(self, workspace: Path, run_id: str, check: Dict[str, Any]) -> Dict[str, Any]:
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("invalid_check_argv")
        cwd_rel = check.get("cwd", ".")
        cwd = workspace if cwd_rel == "." else safe_workspace_path(workspace, cwd_rel, must_exist=True)
        if not cwd.is_dir():
            raise ValueError("invalid_check_cwd")
        timeout = min(max(int(check.get("timeout_sec", 30)), 1), 120)
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True,
                                  timeout=timeout, check=False)
            result = {"id": check.get("id", "unnamed"), "argv": argv, "exit_code": proc.returncode,
                      "duration_ms": int((time.monotonic() - started) * 1000),
                      "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-8000:], "timed_out": False}
        except subprocess.TimeoutExpired as exc:
            result = {"id": check.get("id", "unnamed"), "argv": argv, "exit_code": None,
                      "duration_ms": int((time.monotonic() - started) * 1000),
                      "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
                      "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
                      "timed_out": True}
        log_path = self.logs / f"{run_id}-{str(check.get('id','check')).replace('/','_')}.json"
        log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["log_path"] = str(log_path)
        return result

    def verify(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] != "VERIFYING":
                raise ValueError("run_not_verifying")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            receipt = json.loads(run["receipt_json"])
        workspace = Path(payload["workspace"]).resolve()
        try:
            snapshot_id, manifest = self._snapshot(payload, receipt["changed_paths"])
        except ValueError as exc:
            reason = str(exc)
            safety_prefixes = (
                "protected_path_changed:", "path_escape", "symlink_not_allowed",
                "missing_path", "not_regular_file:",
            )
            if reason.startswith(safety_prefixes):
                now = utc_now()
                feedback = {"kind": "verification_safety_block", "error": reason}
                with self.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute("SELECT task_id,state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if not current or current["state"] != "VERIFYING":
                        conn.execute("ROLLBACK")
                        raise ValueError("run_not_verifying")
                    conn.execute("UPDATE runs SET state='BLOCKED',verify_status='BLOCKED',feedback_json=?,completed_at=?,error=? WHERE run_id=?",
                                 (canonical_json(feedback), now, reason[:1000], run_id))
                    conn.execute("UPDATE tasks SET status='BLOCKED',updated_at=? WHERE task_id=?", (now, current["task_id"]))
                    self._event(conn, "VERIFY_SAFETY_BLOCK", task_id=current["task_id"], run_id=run_id, payload=feedback)
                    conn.execute("COMMIT")
                self._revoke_capability(run_id)
                return {"status": "BLOCKED", "reason": reason, "feedback": feedback}
            raise
        checks = [self._run_check(workspace, run_id, item) for item in payload.get("checks", [])]
        passed = all(item["exit_code"] == 0 and not item["timed_out"] for item in checks)
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO snapshots(snapshot_id,run_id,manifest_json,created_at) VALUES(?,?,?,?)",
                         (snapshot_id, run_id, canonical_json(manifest), now))
            if not passed:
                feedback = {"kind": "verification_failure", "checks": checks, "snapshot_id": snapshot_id}
                conn.execute("UPDATE runs SET state='NEEDS_FIX',snapshot_id=?,verify_status='FAIL',feedback_json=? WHERE run_id=?",
                             (snapshot_id, canonical_json(feedback), run_id))
                conn.execute("UPDATE tasks SET status='NEEDS_FIX',updated_at=? WHERE task_id=?", (now, run["task_id"]))
                self._event(conn, "VERIFY_FAIL", task_id=run["task_id"], run_id=run_id,
                            payload={"snapshot_id": snapshot_id, "checks": checks})
                conn.execute("COMMIT")
                return {"status": "NEEDS_FIX", "snapshot_id": snapshot_id, "checks": checks}
            decision = decide_review(payload, manifest, attempt=int(run["attempt"]))
            if decision["required"]:
                next_state, task_state = "REVIEWING", "WAITING_REVIEW"
            else:
                next_state, task_state = "VERIFIED", "READY_TO_PUBLISH"
            conn.execute("UPDATE runs SET state=?,snapshot_id=?,verify_status='PASS' WHERE run_id=?",
                         (next_state, snapshot_id, run_id))
            conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?", (task_state, now, run["task_id"]))
            self._event(conn, "VERIFY_PASS", task_id=run["task_id"], run_id=run_id,
                        payload={"snapshot_id": snapshot_id,
                                 "checks": [{"id": c["id"], "exit_code": c["exit_code"]} for c in checks],
                                 "review_decision": decision})
            conn.execute("COMMIT")
        return {"status": next_state, "snapshot_id": snapshot_id, "checks": checks, "review_decision": decision}

    def review_decision(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or not run["snapshot_id"]:
                raise ValueError("snapshot_missing")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            snap = conn.execute("SELECT manifest_json FROM snapshots WHERE snapshot_id=?", (run["snapshot_id"],)).fetchone()
            if not task or not snap:
                raise ValueError("review_basis_missing")
            payload = self._task_payload(task)
            manifest = json.loads(snap["manifest_json"])
            return decide_review(payload, manifest, attempt=int(run["attempt"]))

    def import_review(self, run_id: str, report_path: Path) -> Dict[str, Any]:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        verdict = report.get("verdict")
        if verdict not in {"PASS", "NEEDS_FIX", "BLOCKED"}:
            raise ValueError("invalid_review_verdict")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] != "REVIEWING":
                conn.execute("ROLLBACK")
                raise ValueError("run_not_reviewing")
            if report.get("run_id") != run_id or report.get("snapshot_id") != run["snapshot_id"]:
                conn.execute("ROLLBACK")
                raise ValueError("stale_review")
            now = utc_now()
            if verdict == "PASS":
                conn.execute("UPDATE runs SET state='VERIFIED',review_status='PASS' WHERE run_id=?", (run_id,))
                conn.execute("UPDATE tasks SET status='READY_TO_PUBLISH',updated_at=? WHERE task_id=?", (now, run["task_id"]))
            else:
                feedback = {"kind": "review", "verdict": verdict,
                            "findings": report.get("findings", []), "snapshot_id": run["snapshot_id"]}
                conn.execute("UPDATE runs SET state='NEEDS_FIX',review_status=?,feedback_json=? WHERE run_id=?",
                             (verdict, canonical_json(feedback), run_id))
                conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?",
                             ("NEEDS_FIX" if verdict == "NEEDS_FIX" else "BLOCKED", now, run["task_id"]))
            self._event(conn, "REVIEW_IMPORTED", task_id=run["task_id"], run_id=run_id,
                        payload={"verdict": verdict, "snapshot_id": run["snapshot_id"]})
            conn.execute("COMMIT")
        return {"status": verdict, "run_id": run_id}

    def _assert_snapshot_current(self, conn: sqlite3.Connection, run: sqlite3.Row,
                                 payload: Dict[str, Any]) -> Dict[str, Any]:
        snap = conn.execute("SELECT manifest_json FROM snapshots WHERE snapshot_id=?", (run["snapshot_id"],)).fetchone()
        if not snap:
            raise ValueError("snapshot_missing")
        manifest = json.loads(snap["manifest_json"])
        workspace = Path(payload["workspace"]).resolve()
        for relative, recorded in manifest.get("files", {}).items():
            path = safe_workspace_path(workspace, relative, must_exist=False)
            if recorded.get("deleted"):
                if path.exists():
                    raise ValueError(f"snapshot_stale:{relative}")
                continue
            if not path.is_file() or sha256_file(path) != recorded["sha256"]:
                raise ValueError(f"snapshot_stale:{relative}")
        for relative, recorded in manifest.get("protected", {}).items():
            path = safe_workspace_path(workspace, relative, must_exist=True)
            if sha256_file(path) != recorded["actual"]:
                raise ValueError(f"protected_stale:{relative}")
        return manifest

    def complete(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] != "VERIFIED":
                conn.execute("ROLLBACK")
                raise ValueError("run_not_verified")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            self._assert_snapshot_current(conn, run, payload)
            if payload.get("owner_acceptance"):
                approval = conn.execute("SELECT snapshot_id FROM approvals WHERE run_id=?", (run_id,)).fetchone()
                if not approval or approval["snapshot_id"] != run["snapshot_id"]:
                    conn.execute("UPDATE tasks SET status='WAITING_OWNER',updated_at=? WHERE task_id=?", (utc_now(), run["task_id"]))
                    conn.execute("COMMIT")
                    return {"status": "WAITING_OWNER", "run_id": run_id, "snapshot_id": run["snapshot_id"]}
            if payload.get("publication", {}).get("kind", "none") != "none":
                conn.execute("ROLLBACK")
                return {"status": "PUBLICATION_REQUIRED", "run_id": run_id}
            now = utc_now()
            conn.execute("UPDATE runs SET state='COMPLETE',completed_at=? WHERE run_id=?", (now, run_id))
            conn.execute("UPDATE tasks SET status='DONE',updated_at=? WHERE task_id=?", (now, run["task_id"]))
            self._event(conn, "RUN_COMPLETE", task_id=run["task_id"], run_id=run_id)
            conn.execute("COMMIT")
            return {"status": "COMPLETE", "run_id": run_id, "task_id": run["task_id"]}

    def approve(self, run_id: str, note: str = "owner approved") -> Dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] != "VERIFIED" or not run["snapshot_id"]:
                conn.execute("ROLLBACK")
                raise ValueError("run_not_approvable")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            if not payload.get("owner_acceptance"):
                conn.execute("ROLLBACK")
                raise ValueError("owner_approval_not_required")
            self._assert_snapshot_current(conn, run, payload)
            now = utc_now()
            conn.execute("INSERT OR REPLACE INTO approvals(run_id,snapshot_id,approved_at,note) VALUES(?,?,?,?)",
                         (run_id, run["snapshot_id"], now, note[:500]))
            conn.execute("UPDATE tasks SET status='READY_TO_PUBLISH',updated_at=? WHERE task_id=?", (now, run["task_id"]))
            self._event(conn, "OWNER_APPROVED", task_id=run["task_id"], run_id=run_id, payload={"snapshot_id": run["snapshot_id"]})
            conn.execute("COMMIT")
            return {"status": "APPROVED", "run_id": run_id, "snapshot_id": run["snapshot_id"]}

    def publish(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] != "VERIFIED":
                raise ValueError("run_not_verified")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            manifest = self._assert_snapshot_current(conn, run, payload)
            if payload.get("owner_acceptance"):
                approval = conn.execute("SELECT snapshot_id FROM approvals WHERE run_id=?", (run_id,)).fetchone()
                if not approval or approval["snapshot_id"] != run["snapshot_id"]:
                    raise ValueError("owner_approval_missing_or_stale")
        pub = payload.get("publication", {})
        if pub.get("kind") not in {"git", "git_local"}:
            raise ValueError("git_publication_not_configured")
        workspace = Path(payload["workspace"]).resolve()
        changed = sorted(manifest.get("files", {}).keys())
        if not changed:
            raise ValueError("nothing_to_publish")
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        env.update({"GIT_PAGER": "cat", "PAGER": "cat"})
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", str(workspace), *args], env=env,
                                  capture_output=True, text=True, timeout=30, check=False)
        def git_bytes(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", str(workspace), *args], env=env,
                                  capture_output=True, text=False, timeout=30, check=False)
        branch = git("branch", "--show-current")
        if branch.returncode or branch.stdout.strip() != pub.get("branch", "main"):
            raise ValueError("publication_branch_mismatch")
        expected_base = pub.get("expected_base")
        head = git("rev-parse", "HEAD")
        if expected_base and head.stdout.strip() != expected_base:
            raise ValueError("publication_base_changed")
        existing_staged = git_bytes("diff", "--cached", "--name-only", "-z")
        if existing_staged.returncode or existing_staged.stdout:
            raise ValueError("preexisting_staging_not_empty")
        add = git("add", "--", *changed)
        if add.returncode:
            raise RuntimeError("git_add_failed:" + add.stderr[-1000:])
        staged_result = git_bytes("diff", "--cached", "--name-only", "-z")
        staged = sorted(os.fsdecode(item) for item in staged_result.stdout.split(b"\0") if item) if staged_result.returncode == 0 else []
        if staged != changed:
            raise ValueError("staged_scope_mismatch")
        for relative, recorded in manifest["files"].items():
            staged_blob = git_bytes("show", f":{relative}")
            if recorded.get("deleted"):
                if staged_blob.returncode == 0:
                    raise ValueError(f"staged_deletion_mismatch:{relative};staging_requires_reconciliation")
                continue
            if staged_blob.returncode or sha256_bytes(staged_blob.stdout) != recorded["sha256"]:
                raise ValueError(f"staged_bytes_mismatch:{relative};staging_requires_reconciliation")
        message = pub.get("commit_message") or f"orch: complete {run['task_id']}"
        commit = git("commit", "-m", message, "--", *changed)
        if commit.returncode:
            raise RuntimeError("git_commit_failed:" + commit.stderr[-1000:])
        commit_id = git("rev-parse", "HEAD").stdout.strip()
        remote_commit = None
        remote = pub.get("remote", "origin")
        ref = pub.get("ref", "main")
        if pub.get("kind") == "git":
            push = git("push", remote, f"HEAD:{ref}")
            if push.returncode:
                raise RuntimeError("git_push_failed:" + push.stderr[-1000:])
            remote_ref = git("ls-remote", remote, f"refs/heads/{ref}")
            remote_commit = remote_ref.stdout.split()[0] if remote_ref.returncode == 0 and remote_ref.stdout.strip() else None
            if remote_commit != commit_id:
                raise ValueError("remote_verification_failed")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE runs SET state='COMPLETE',completed_at=? WHERE run_id=?", (now, run_id))
            conn.execute("UPDATE tasks SET status='DONE',updated_at=? WHERE task_id=?", (now, run["task_id"]))
            self._event(conn, "PUBLISHED", task_id=run["task_id"], run_id=run_id,
                        payload={"commit": commit_id, "kind": pub.get("kind"),
                                 "remote": remote if pub.get("kind") == "git" else None,
                                 "ref": ref if pub.get("kind") == "git" else None})
            conn.execute("COMMIT")
        return {"status": "COMPLETE", "commit": commit_id, "remote_commit": remote_commit}

    def status(self) -> Dict[str, Any]:
        with self.connect() as conn:
            tasks = [dict(row) for row in conn.execute("SELECT task_id,plan_revision,ordinal,status,updated_at FROM tasks ORDER BY ordinal,task_id")]
            runs = [dict(row) for row in conn.execute("SELECT run_id,task_id,attempt,worker_id,state,started_at,heartbeat_at,snapshot_id,verify_status,review_status,completed_at,error FROM runs ORDER BY started_at")]
            events = [dict(row) for row in conn.execute("SELECT seq,ts,kind,task_id,run_id,payload_json FROM events ORDER BY seq DESC LIMIT 30")]
            paused = conn.execute("SELECT value_json FROM settings WHERE key='paused'").fetchone()
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return {"schema_version": 1, "variant": "B_NEW_CHAT_PER_ATTEMPT", "paused": json.loads(paused["value_json"]) if paused else None, "tasks": tasks, "runs": runs, "recent_events": events}

    def pause(self, reason: str) -> Dict[str, Any]:
        details={"reason": reason[:500], "paused_at": utc_now()}
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES('paused',?,?)", (canonical_json(details), utc_now()))
            self._event(conn, "PAUSED", payload=details)
        return {"status":"PAUSED", "details":details}

    def resume(self) -> Dict[str, Any]:
        with self.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key='paused'")
            self._event(conn, "RESUMED")
        return {"status":"RESUMED"}

    def abort(self, run_id: str, reason: str, retry: bool=False) -> Dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run=conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] not in ACTIVE_RUN_STATES:
                conn.execute("ROLLBACK"); raise ValueError("run_not_active")
            now=utc_now(); state="NEEDS_FIX" if retry else "BLOCKED"
            conn.execute("UPDATE runs SET state='ABORTED',error=?,completed_at=? WHERE run_id=?", (reason[:1000],now,run_id))
            conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?", (state,now,run["task_id"]))
            self._event(conn, "RUN_ABORTED", task_id=run["task_id"], run_id=run_id, payload={"reason":reason[:500],"retry":retry})
            conn.execute("COMMIT")
        self._revoke_capability(run_id)
        return {"status":"ABORTED", "run_id":run_id, "task_status":state}

    def next_work(self) -> Dict[str, Any]:
        with self.connect() as conn:
            paused = conn.execute("SELECT value_json FROM settings WHERE key='paused'").fetchone()
            if paused:
                return {"status": "PAUSED", "details": json.loads(paused["value_json"])}
            active = conn.execute("SELECT run_id,task_id,state FROM runs WHERE state IN (?,?,?,?,?) ORDER BY started_at LIMIT 1", tuple(ACTIVE_RUN_STATES)).fetchone()
            if active:
                return {"status": "BUSY", "active": dict(active)}
            for row in conn.execute("SELECT * FROM tasks ORDER BY ordinal,task_id").fetchall():
                if row["status"] not in READY_TASK_STATES:
                    continue
                payload = self._task_payload(row)
                if not self._dependencies_done(conn, payload):
                    continue
                attempts = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (row["task_id"],)).fetchone()["n"]
                if attempts < int(payload.get("max_attempts", 2)):
                    return {"status": "READY", "task_id": row["task_id"], "next_attempt": attempts + 1}
            return {"status": "NO_WORK"}

    def reconcile(self) -> Dict[str, Any]:
        with self.connect() as conn:
            active = [dict(row) for row in conn.execute("SELECT run_id,task_id,state,heartbeat_at FROM runs WHERE state IN (?,?,?,?,?) ORDER BY started_at",
                                                       tuple(ACTIVE_RUN_STATES))]
        return {"status": "ATTENTION" if active else "CLEAN", "active_runs": active,
                "rule": "No automatic lease expiry; inspect active runs before any new writer."}
