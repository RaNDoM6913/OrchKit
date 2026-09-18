from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import ensure_private_dir, ensure_private_file
from .git_transport import inspect_transport_url, run_sandboxed_transport
from .review_policy import decide_review, normalize_review_policy

ACTIVE_RUN_STATES = {"RUNNING", "RESULT_SUBMITTED", "QUIESCING", "VERIFYING", "REVIEWING"}
WRITER_LOCK_RUN_STATES = ACTIVE_RUN_STATES | {"VERIFIED"}
READY_TASK_STATES = {"PLANNED", "READY", "NEEDS_FIX"}
STATE_SCHEMA_VERSION = 4


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


def derive_writer_key(workspace: Path) -> str:
    resolved = workspace.expanduser().resolve()
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
    prefix = [
        "git", "--no-pager",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "core.hooksPath=/dev/null",
        "-C", str(resolved),
    ]
    inside = subprocess.run(
        prefix + ["rev-parse", "--is-inside-work-tree"],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    if inside.returncode == 0 and inside.stdout.strip() == "true":
        common = subprocess.run(
            prefix + ["rev-parse", "--git-common-dir"],
            env=env, capture_output=True, text=True, timeout=10, check=False,
        )
        if common.returncode == 0 and common.stdout.strip():
            raw = Path(common.stdout.strip())
            common_dir = raw.resolve() if raw.is_absolute() else (resolved / raw).resolve()
            return "git:" + sha256_bytes(str(common_dir).encode("utf-8"))[:32]
    return "workspace:" + sha256_bytes(str(resolved).encode("utf-8"))[:32]


def task_writer_key(item: Dict[str, Any]) -> str:
    workspace = Path(item.get("workspace", "")).expanduser().resolve()
    derived = derive_writer_key(workspace)
    explicit = item.get("writer_key")
    if explicit is not None:
        if (
            not isinstance(explicit, str)
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", explicit) is None
        ):
            raise ValueError("invalid_writer_key")
        if explicit != derived:
            raise ValueError("writer_key_mismatch")
    return derived


def _check_cwd(workspace: Path, check: Dict[str, Any]) -> Path:
    cwd_rel = check.get("cwd", ".")
    cwd = (
        workspace.resolve()
        if cwd_rel == "."
        else safe_workspace_path(workspace, cwd_rel, must_exist=True).resolve()
    )
    if not cwd.is_dir():
        raise ValueError("invalid_check_cwd")
    return cwd


def _resolve_check_executable(
    workspace: Path, check: Dict[str, Any]
) -> Path:
    argv = check["argv"]
    argv0 = argv[0]
    cwd = _check_cwd(workspace, check)
    raw = Path(argv0)
    if raw.is_absolute():
        candidate = raw
    elif "/" in argv0:
        candidate = (cwd / raw).resolve()
        if not _inside(workspace.resolve(), candidate):
            raise ValueError("check_executable_path_escape")
    else:
        found = shutil.which(argv0, path=os.environ.get("PATH"))
        if not found:
            raise ValueError(f"check_executable_not_found:{argv0}")
        candidate = Path(found)
    resolved = candidate.expanduser().resolve()
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or not os.access(str(resolved), os.X_OK)
    ):
        raise ValueError(f"check_executable_unsafe:{argv0}")
    return resolved


def _authority_record(
    workspace: Path, relative: str, *, expected: str
) -> Dict[str, Any]:
    normalized = normalize_relative_path(relative)
    path = safe_workspace_path(workspace, normalized, must_exist=False)
    if path.is_symlink():
        raise ValueError(f"check_authority_symlink_not_allowed:{normalized}")
    if expected == "file":
        if not path.is_file():
            raise ValueError(f"check_authority_file_missing:{normalized}")
        return {
            "path": normalized,
            "expected": "file",
            "sha256": sha256_file(path),
        }
    if expected != "absent":
        raise ValueError("invalid_check_authority_expectation")
    if path.exists():
        raise ValueError(f"check_authority_expected_absent:{normalized}")
    return {"path": normalized, "expected": "absent", "sha256": None}


def bind_check_authority(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    workspace = Path(item["workspace"]).resolve()
    bound: List[Dict[str, Any]] = []
    for raw_check in item.get("checks", []):
        check = dict(raw_check)
        check.pop("executable_path", None)
        check.pop("executable_sha256", None)
        check.pop("authority_files", None)
        executable = _resolve_check_executable(workspace, check)
        check["executable_path"] = str(executable)
        check["executable_sha256"] = sha256_file(executable)

        cwd = _check_cwd(workspace, check)
        required = set(check.get("authority_paths", []))
        absent = set(check.get("authority_absent_paths", []))

        for arg in check["argv"][1:]:
            if not arg or arg.startswith("-") or Path(arg).is_absolute():
                continue
            raw_candidate = cwd / arg
            if raw_candidate.is_symlink():
                raise ValueError(
                    f"check_authority_symlink_not_allowed:{arg}"
                )
            try:
                candidate = raw_candidate.resolve()
            except OSError:
                continue
            if _inside(workspace, candidate) and candidate.is_file():
                required.add(candidate.relative_to(workspace).as_posix())

        command_name = Path(check["argv"][0]).name
        if (
            command_name in {"npm", "yarn", "pnpm"}
            and "run" in check["argv"][1:3]
        ):
            required.add("package.json")
            for relative in (
                ".npmrc", ".yarnrc", ".yarnrc.yml",
                ".pnpmfile.cjs", "pnpm-workspace.yaml",
            ):
                path = workspace / relative
                if path.exists() or path.is_symlink():
                    required.add(relative)
                else:
                    absent.add(relative)

        overlap = required.intersection(absent)
        if overlap:
            raise ValueError(
                "check_authority_conflicting_expectation:" + sorted(overlap)[0]
            )
        authority = [
            _authority_record(workspace, relative, expected="file")
            for relative in sorted(required)
        ]
        authority.extend(
            _authority_record(workspace, relative, expected="absent")
            for relative in sorted(absent)
        )
        check["authority_files"] = authority
        bound.append(check)
    return bound


def prepare_task_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(item)
    payload.setdefault("max_attempts", 2)
    payload.setdefault("checks", [])
    payload.setdefault("protected_paths", {})
    payload.setdefault("required_review", False)
    payload["review"] = normalize_review_policy(payload)
    payload.setdefault("owner_acceptance", False)
    payload.setdefault("publication", {"kind": "none"})
    payload["writer_key"] = task_writer_key(payload)
    payload["checks"] = bind_check_authority(payload)
    return payload


def validate_task_definition(item: Dict[str, Any]) -> None:
    workspace = Path(item.get("workspace", ""))
    if not workspace.is_absolute():
        raise ValueError("workspace_must_be_absolute")
    project_id = item.get("project_id")
    if project_id is not None and (
        not isinstance(project_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None
    ):
        raise ValueError("invalid_project_id")
    task_writer_key(item)
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
        for field in ("authority_paths", "authority_absent_paths"):
            values = check.get(field, [])
            if not isinstance(values, list):
                raise ValueError(f"invalid_{field}")
            for relative in values:
                if not isinstance(relative, str) or not relative:
                    raise ValueError(f"invalid_{field}")
                normalize_relative_path(relative)
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
            if dep in graph:
                visit(dep)
        state[task_id] = 2
    for task_id in graph:
        visit(task_id)


class Orchestrator:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.runtime = ensure_private_dir(self.root / ".runtime")
        self.db_path = self.runtime / "orch.sqlite3"
        self.logs = ensure_private_dir(self.runtime / "logs")
        ensure_private_dir(self.runtime / "worker_receipts")
        ensure_private_dir(self.runtime / "claims")
        ensure_private_dir(self.runtime / "review_exports")
        ensure_private_dir(self.runtime / "git-hooks-disabled")
        for optional_state_dir in ("projects", "plans", "backups"):
            candidate = self.root / optional_state_dir
            if candidate.exists():
                ensure_private_dir(candidate)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        ensure_private_file(self.db_path)
        for sidecar in (
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            ensure_private_file(sidecar)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_private_file(self.db_path)
        for sidecar in (
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            ensure_private_file(sidecar)
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
          project_id TEXT, writer_key TEXT NOT NULL, queue_seq INTEGER NOT NULL,
          status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          worker_id TEXT NOT NULL, state TEXT NOT NULL, lease_token TEXT NOT NULL,
          started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, claim_git_head TEXT,
          submitted_at TEXT, snapshot_id TEXT, receipt_json TEXT,
          verify_status TEXT, review_status TEXT,
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
        CREATE TABLE IF NOT EXISTS publications (
          run_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
          expected_base TEXT, staged_paths_json TEXT, commit_id TEXT, remote_commit TEXT,
          error TEXT, updated_at TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY, from_version INTEGER NOT NULL,
          applied_at TEXT NOT NULL, details_json TEXT NOT NULL
        );
        """
        with self.connect() as conn:
            conn.executescript(schema)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise ValueError(f"unsupported_state_schema:{version}")
            if version < STATE_SCHEMA_VERSION:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrated_tasks = 0
                    if version < 3:
                        columns = {
                            row["name"]
                            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
                        }
                        if "project_id" not in columns:
                            conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT")
                        if "writer_key" not in columns:
                            conn.execute("ALTER TABLE tasks ADD COLUMN writer_key TEXT")
                        if "queue_seq" not in columns:
                            conn.execute("ALTER TABLE tasks ADD COLUMN queue_seq INTEGER")
                        rows = conn.execute(
                            "SELECT rowid,task_id,payload_json,project_id,writer_key,queue_seq "
                            "FROM tasks ORDER BY rowid"
                        ).fetchall()
                        next_seq = 0
                        for row in rows:
                            payload = json.loads(row["payload_json"])
                            next_seq = max(next_seq + 1, int(row["queue_seq"] or 0))
                            conn.execute(
                                "UPDATE tasks SET project_id=?,writer_key=?,queue_seq=? "
                                "WHERE task_id=?",
                                (
                                    row["project_id"]
                                    if row["project_id"] is not None
                                    else payload.get("project_id"),
                                    row["writer_key"] or task_writer_key(payload),
                                    next_seq,
                                    row["task_id"],
                                ),
                            )
                        migrated_tasks = len(rows)
                    run_columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
                    }
                    if "claim_git_head" not in run_columns:
                        conn.execute(
                            "ALTER TABLE runs ADD COLUMN claim_git_head TEXT"
                        )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tasks_queue "
                        "ON tasks(queue_seq,task_id)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_runs_state_task "
                        "ON runs(state,task_id)"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_migrations"
                        "(version,from_version,applied_at,details_json) "
                        "VALUES(?,?,?,?)",
                        (
                            STATE_SCHEMA_VERSION, version, utc_now(),
                            canonical_json({
                                "kind": "transactional_upgrade",
                                "tasks_migrated": migrated_tasks,
                                "claim_git_head_added": True,
                                "target_version": STATE_SCHEMA_VERSION,
                            }),
                        ),
                    )
                    conn.execute(f"PRAGMA user_version={STATE_SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            else:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_queue "
                    "ON tasks(queue_seq,task_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_runs_state_task "
                    "ON runs(state,task_id)"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations"
                    "(version,from_version,applied_at,details_json) "
                    "VALUES(?,?,?,?)",
                    (
                        STATE_SCHEMA_VERSION, STATE_SCHEMA_VERSION, utc_now(),
                        canonical_json({
                            "kind": "observed_existing_schema",
                            "target_version": STATE_SCHEMA_VERSION,
                        }),
                    ),
                )

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
            if not isinstance(deps, list) or any(
                not isinstance(dep, str) or not dep or dep == item["id"] for dep in deps
            ):
                raise ValueError("invalid_dependency")
            validate_task_definition(item)
        validate_dependency_graph(tasks)
        prepared_payloads = {
            item["id"]: prepare_task_payload(item) for item in tasks
        }
        digest = sha256_file(plan_path)
        now = utc_now()
        queued_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT source_digest FROM plans WHERE plan_revision=?",
                                    (data["plan_revision"],)).fetchone()
            if existing and existing["source_digest"] != digest:
                conn.execute("ROLLBACK")
                raise ValueError("plan_revision_digest_conflict")
            durable_task_ids = {
                row["task_id"] for row in conn.execute("SELECT task_id FROM tasks").fetchall()
            }
            for item in tasks:
                missing = [
                    dep for dep in item.get("dependencies", [])
                    if dep not in known and dep not in durable_task_ids
                ]
                if missing:
                    conn.execute("ROLLBACK")
                    raise ValueError("invalid_dependency:" + missing[0])
            existing_task_ids = set()
            for item in tasks:
                task_existing = conn.execute(
                    "SELECT plan_revision FROM tasks WHERE task_id=?", (item["id"],)
                ).fetchone()
                if task_existing and task_existing["plan_revision"] != data["plan_revision"]:
                    conn.execute("ROLLBACK")
                    raise ValueError(f"task_id_conflict:{item['id']}")
                if task_existing:
                    existing_task_ids.add(item["id"])
            conn.execute("INSERT OR IGNORE INTO plans(plan_revision,source_digest,loaded_at) VALUES(?,?,?)",
                         (data["plan_revision"], digest, now))
            next_queue = conn.execute("SELECT COALESCE(MAX(queue_seq),0) FROM tasks").fetchone()[0]
            for ordinal, item in enumerate(tasks):
                if item["id"] in existing_task_ids:
                    continue
                payload = prepared_payloads[item["id"]]
                next_queue += 1
                conn.execute(
                    "INSERT INTO tasks(task_id,plan_revision,ordinal,project_id,writer_key,queue_seq,status,payload_json,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        item["id"], data["plan_revision"], ordinal, payload.get("project_id"),
                        payload["writer_key"], next_queue, "PLANNED", canonical_json(payload), now,
                    ),
                )
                queued_count += 1
            self._event(
                conn, "PLAN_LOADED",
                payload={"revision": data["plan_revision"], "digest": digest, "queued_count": queued_count},
            )
            conn.execute("COMMIT")
        return {
            "status": "OK", "plan_revision": data["plan_revision"], "digest": digest,
            "task_count": len(tasks), "queued_count": queued_count,
        }

    def _task_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        return json.loads(row["payload_json"])

    def _dependencies_done(self, conn: sqlite3.Connection, payload: Dict[str, Any]) -> bool:
        for dep in payload.get("dependencies", []):
            row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (dep,)).fetchone()
            if not row or row["status"] != "DONE":
                return False
        return True

    def _active_writer_locks(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        placeholders = ",".join("?" for _ in WRITER_LOCK_RUN_STATES)
        rows = conn.execute(
            "SELECT r.run_id,r.task_id,r.state,r.heartbeat_at,t.project_id,t.writer_key "
            "FROM runs r JOIN tasks t ON t.task_id=r.task_id "
            f"WHERE r.state IN ({placeholders}) ORDER BY r.started_at",
            tuple(sorted(WRITER_LOCK_RUN_STATES)),
        ).fetchall()
        return {row["writer_key"]: dict(row) for row in rows}

    def _project_pauses(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        rows = conn.execute(
            "SELECT key,value_json FROM settings WHERE key GLOB 'project_paused:*' ORDER BY key"
        ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        prefix = "project_paused:"
        for row in rows:
            project_id = row["key"][len(prefix):]
            if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
                continue
            result[project_id] = json.loads(row["value_json"])
        return result

    def _claim_git_state(
        self, payload: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[str]]:
        publication = payload.get("publication") or {}
        if publication.get("kind") not in {"git", "git_local"}:
            return None, None
        workspace = Path(payload["workspace"]).resolve()
        git_binary = Path("/usr/bin/git")
        if not git_binary.is_file() or not os.access(str(git_binary), os.X_OK):
            found = shutil.which("git")
            if not found:
                raise ValueError("claim_git_binary_missing")
            git_binary = Path(found).resolve()
        env = {
            key: os.environ[key]
            for key in ("HOME", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        })
        prefix = [
            str(git_binary), "--no-pager",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "core.hooksPath=/dev/null",
            "-C", str(workspace),
        ]
        head = subprocess.run(
            prefix + ["rev-parse", "HEAD"],
            env=env, capture_output=True, text=True, timeout=15, check=False,
        )
        if head.returncode or not head.stdout.strip():
            raise ValueError("claim_git_head_unavailable")
        claim_head = head.stdout.strip()
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", claim_head) is None:
            raise ValueError("claim_git_head_invalid")
        branch = subprocess.run(
            prefix + ["branch", "--show-current"],
            env=env, capture_output=True, text=True, timeout=15, check=False,
        )
        branch_name = branch.stdout.strip() if branch.returncode == 0 else ""
        expected_branch = publication.get("branch")
        if expected_branch and branch_name != expected_branch:
            raise ValueError("claim_branch_mismatch")
        expected_base = publication.get("expected_base")
        if expected_base and claim_head != expected_base:
            raise ValueError("claim_expected_base_changed")
        return claim_head, branch_name or None

    def _claim_workspace_guard(
        self, payload: Dict[str, Any], claim_git_head: Optional[str]
    ) -> Dict[str, Any]:
        workspace = Path(payload["workspace"]).resolve()
        protected_results: List[Dict[str, Any]] = []
        for relative, expected_hash in sorted(
            payload.get("protected_paths", {}).items()
        ):
            try:
                path = safe_workspace_path(
                    workspace, relative, must_exist=False
                )
            except ValueError as exc:
                return {
                    "status": "BLOCKED",
                    "reason": f"claim_protected_path_unsafe:{relative}:{exc}",
                    "protected": protected_results,
                }
            actual_hash = (
                sha256_file(path)
                if path.is_file() and not path.is_symlink()
                else None
            )
            item = {
                "path": relative,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "matches": actual_hash == expected_hash,
            }
            protected_results.append(item)
            if actual_hash != expected_hash:
                return {
                    "status": "BLOCKED",
                    "reason": f"claim_protected_path_changed:{relative}",
                    "protected": protected_results,
                }

        scope = self._workspace_scope_evidence(payload, [])
        if scope.get("status") == "BLOCKED":
            observed = scope.get("observed_paths") or []
            first = observed[0] if observed else "unknown"
            return {
                "status": "BLOCKED",
                "reason": f"claim_workspace_dirty:{first}",
                "scope": scope,
                "protected": protected_results,
            }
        if (
            scope.get("status") == "PASS"
            and claim_git_head is not None
            and scope.get("git_head") != claim_git_head
        ):
            return {
                "status": "BLOCKED",
                "reason": "claim_git_head_changed_during_preflight",
                "scope": scope,
                "protected": protected_results,
            }

        authority = self._check_authority_evidence(
            workspace, payload.get("checks", [])
        )
        if authority.get("status") == "BLOCKED":
            return {
                "status": "BLOCKED",
                "reason": authority.get("reason") or "claim_check_authority_changed",
                "scope": scope,
                "protected": protected_results,
                "check_authority": authority,
            }
        return {
            "status": "PASS",
            "scope": scope,
            "protected": protected_results,
            "check_authority": authority,
        }

    def claim(self, worker_id: str, project_id: Optional[str] = None) -> Dict[str, Any]:
        if project_id is not None and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            paused = conn.execute("SELECT value_json FROM settings WHERE key='paused'").fetchone()
            if paused:
                conn.execute("COMMIT")
                return {"status": "PAUSED", "details": json.loads(paused["value_json"])}
            locks = self._active_writer_locks(conn)
            project_pauses = self._project_pauses(conn)
            if project_id is not None and project_id in project_pauses:
                conn.execute("COMMIT")
                return {
                    "status": "PROJECT_PAUSED",
                    "project_id": project_id,
                    "details": project_pauses[project_id],
                }
            blocked_locks: List[Dict[str, Any]] = []
            paused_projects: Dict[str, Dict[str, Any]] = {}
            selected = None
            for row in conn.execute("SELECT * FROM tasks ORDER BY queue_seq,task_id").fetchall():
                if row["status"] not in READY_TASK_STATES:
                    continue
                if project_id is not None and row["project_id"] != project_id:
                    continue
                if row["project_id"] in project_pauses:
                    paused_projects[row["project_id"]] = project_pauses[row["project_id"]]
                    continue
                payload = self._task_payload(row)
                if not self._dependencies_done(conn, payload):
                    continue
                attempts = conn.execute(
                    "SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (row["task_id"],)
                ).fetchone()["n"]
                if attempts >= int(payload.get("max_attempts", 2)):
                    conn.execute(
                        "UPDATE tasks SET status='BLOCKED',updated_at=? WHERE task_id=?",
                        (now, row["task_id"]),
                    )
                    self._event(
                        conn, "TASK_BLOCKED_MAX_ATTEMPTS", task_id=row["task_id"],
                        payload={"attempts": attempts},
                    )
                    continue
                lock = locks.get(row["writer_key"])
                if lock:
                    blocked_locks.append(lock)
                    continue
                selected = (row, payload, attempts + 1)
                break
            if not selected:
                conn.execute("COMMIT")
                if blocked_locks:
                    unique = {item["run_id"]: item for item in blocked_locks}
                    active = list(unique.values())
                    return {"status": "BUSY", "active": active[0], "locks": active}
                if paused_projects:
                    return {
                        "status": "PROJECTS_PAUSED",
                        "project_id": project_id,
                        "paused_projects": paused_projects,
                    }
                return {"status": "NO_WORK", "project_id": project_id}
            row, payload, attempt = selected
            claim_guard = None
            try:
                claim_git_head, claim_branch = self._claim_git_state(payload)
                claim_guard = self._claim_workspace_guard(
                    payload, claim_git_head
                )
                if claim_guard["status"] != "PASS":
                    raise ValueError(claim_guard["reason"])
            except ValueError as exc:
                reason = str(exc)
                details = claim_guard
                conn.execute(
                    "UPDATE tasks SET status='BLOCKED',updated_at=? WHERE task_id=?",
                    (now, row["task_id"]),
                )
                self._event(
                    conn, "TASK_BLOCKED_AT_CLAIM", task_id=row["task_id"],
                    payload={
                        "reason": reason,
                        "project_id": row["project_id"],
                    },
                )
                conn.execute("COMMIT")
                result = {
                    "status": "BLOCKED",
                    "task_id": row["task_id"],
                    "project_id": row["project_id"],
                    "reason": reason,
                }
                if isinstance(details, dict) and details.get("status") == "BLOCKED":
                    result["details"] = details
                return result
            run_id = f"{row['task_id']}-A{attempt}-{uuid.uuid4().hex[:10]}"
            lease = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO runs("
                "run_id,task_id,attempt,worker_id,state,lease_token,"
                "started_at,heartbeat_at,claim_git_head"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id, row["task_id"], attempt, worker_id, "RUNNING",
                    lease, now, now, claim_git_head,
                ),
            )
            conn.execute(
                "UPDATE tasks SET status='IN_PROGRESS',updated_at=? WHERE task_id=?",
                (now, row["task_id"]),
            )
            self._event(
                conn, "RUN_CLAIMED", task_id=row["task_id"], run_id=run_id,
                payload={
                    "attempt": attempt, "worker_id": worker_id,
                    "project_id": row["project_id"],
                    "writer_key": row["writer_key"],
                    "queue_seq": row["queue_seq"],
                    "claim_git_head": claim_git_head,
                    "claim_branch": claim_branch,
                },
            )
            conn.execute("COMMIT")
        claims = ensure_private_dir(self.runtime / "claims")
        cap = self._capability_path(run_id)
        cap.write_text(
            json.dumps({"run_id": run_id, "lease_token": lease}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(cap, 0o600)
        return {
            "status": "CLAIMED", "run_id": run_id, "task_id": row["task_id"],
            "project_id": row["project_id"], "attempt": attempt,
            "capability_file": str(cap), "context": self.context(run_id),
        }

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
                    "project_id": task["project_id"], "writer_key": task["writer_key"],
                    "queue_seq": task["queue_seq"],
                    "claim_git_head": run["claim_git_head"],
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

    def _block_verification(self, run_id: str, reason: str, *, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = utc_now()
        feedback = {"kind": "verification_safety_block", "error": reason}
        if evidence is not None:
            feedback["evidence"] = evidence
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

    def _disabled_git_hooks_path(self) -> Path:
        hooks = ensure_private_dir(self.runtime / "git-hooks-disabled")
        contents = list(hooks.iterdir())
        if contents:
            raise ValueError("git_hooks_guard_not_empty")
        return hooks

    def _git_filter_attributes(self, workspace: Path, paths: List[str]) -> Dict[str, str]:
        if not paths:
            return {}
        env = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
        prefix = [
            "git", "--no-pager",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "diff.external=",
            "-c", f"core.hooksPath={self._disabled_git_hooks_path()}",
            "-C", str(workspace),
        ]
        result: Dict[str, str] = {}
        normalized = sorted(set(normalize_relative_path(path) for path in paths))
        for offset in range(0, len(normalized), 200):
            batch = normalized[offset:offset + 200]
            probe = subprocess.run(
                prefix + ["check-attr", "-z", "filter", "--", *batch],
                env=env, capture_output=True, text=False, timeout=15, check=False,
            )
            if probe.returncode != 0:
                raise RuntimeError("git_filter_attribute_probe_failed")
            fields = probe.stdout.split(b"\0")
            if fields and fields[-1] == b"":
                fields.pop()
            if len(fields) % 3:
                raise RuntimeError("git_filter_attribute_probe_malformed")
            for index in range(0, len(fields), 3):
                relative = normalize_relative_path(os.fsdecode(fields[index]))
                value = os.fsdecode(fields[index + 2])
                result[relative] = value
        return result

    def _git_filter_overrides(self, workspace: Path) -> Tuple[List[str], List[str]]:
        env = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
        prefix = [
            "git", "--no-pager",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "diff.external=",
            "-c", f"core.hooksPath={self._disabled_git_hooks_path()}",
            "-C", str(workspace),
        ]
        listed = subprocess.run(
            prefix + ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            env=env, capture_output=True, text=False, timeout=20, check=False,
        )
        if listed.returncode != 0:
            return [], []
        paths = [
            normalize_relative_path(os.fsdecode(item))
            for item in listed.stdout.split(b"\0") if item
        ]
        attributes = self._git_filter_attributes(workspace, paths)
        drivers = sorted({
            value for value in attributes.values()
            if value not in {"unspecified", "unset"}
        })
        cat_binary = "/bin/cat" if Path("/bin/cat").is_file() else "/usr/bin/cat"
        if not Path(cat_binary).is_file():
            raise ValueError("trusted_cat_binary_missing")
        overrides: List[str] = []
        for driver in drivers:
            if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", driver) is None:
                raise ValueError("unsafe_git_filter_driver")
            overrides.extend([
                "-c", f"filter.{driver}.process=",
                "-c", f"filter.{driver}.clean={cat_binary}",
                "-c", f"filter.{driver}.smudge={cat_binary}",
                "-c", f"filter.{driver}.required=false",
            ])
        return overrides, drivers

    def _workspace_scope_evidence(self, payload: Dict[str, Any], declared_paths: List[str]) -> Dict[str, Any]:
        workspace = Path(payload["workspace"]).resolve()
        env = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
        base_prefix = [
            "git", "--no-pager",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "diff.external=",
            "-c", f"core.hooksPath={self._disabled_git_hooks_path()}",
            "-C", str(workspace),
        ]
        probe = subprocess.run(
            base_prefix + ["rev-parse", "--is-inside-work-tree"],
            env=env, capture_output=True, text=True, timeout=10, check=False,
        )
        declared = sorted(set(normalize_relative_path(path) for path in declared_paths))
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return {
                "status": "NON_GIT_UNAVAILABLE", "declared_paths": declared, "git_head": None,
                "limitation": "independent_scope_census_requires_git_workspace",
            }
        filter_overrides, neutralized_drivers = self._git_filter_overrides(workspace)
        prefix = base_prefix[:-2] + filter_overrides + base_prefix[-2:]

        def paths(*args: str) -> List[str]:
            result = subprocess.run(
                prefix + list(args), env=env, capture_output=True, text=False,
                timeout=15, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("git_scope_probe_failed:" + " ".join(args))
            return [
                normalize_relative_path(os.fsdecode(item))
                for item in result.stdout.split(b"\0") if item
            ]

        tracked = paths("diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z")
        staged = paths("diff", "--no-ext-diff", "--no-textconv", "--cached", "--name-only", "-z")
        untracked = paths("ls-files", "--others", "--exclude-standard", "-z")
        observed_all = sorted(set(tracked + staged + untracked))
        protected = set(
            normalize_relative_path(path) for path in payload.get("protected_paths", {})
        )
        observed = sorted(path for path in observed_all if path not in protected)
        outside_allowlist = sorted(
            path for path in observed
            if not path_allowed(path, payload.get("allowed_paths", []))
        )
        omitted_from_receipt = sorted(set(observed) - set(declared))
        declared_but_unobserved = sorted(set(declared) - set(observed))
        head_probe = subprocess.run(
            prefix + ["rev-parse", "HEAD"], env=env, capture_output=True, text=True,
            timeout=10, check=False,
        )
        git_head = head_probe.stdout.strip() if head_probe.returncode == 0 else None
        return {
            "status": (
                "PASS"
                if not outside_allowlist and not omitted_from_receipt and not declared_but_unobserved
                else "BLOCKED"
            ),
            "declared_paths": declared,
            "git_head": git_head,
            "observed_paths": observed,
            "protected_preexisting_paths": sorted(protected.intersection(observed_all)),
            "outside_allowlist": outside_allowlist,
            "omitted_from_receipt": omitted_from_receipt,
            "declared_but_unobserved": declared_but_unobserved,
            "ignored_files_observed": False,
            "neutralized_filter_drivers": neutralized_drivers,
        }

    def _snapshot(self, payload: Dict[str, Any], changed_paths: List[str],
                  git_head: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
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
                    "protected": protected_results, "git_head": git_head, "created_at": utc_now()}
        snapshot_id = "sha256:" + sha256_bytes(canonical_json(manifest).encode("utf-8"))
        return snapshot_id, manifest

    def _check_authority_evidence(
        self, workspace: Path, checks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        items: List[Dict[str, Any]] = []
        first_error: Optional[str] = None
        for check in checks:
            check_id = str(check.get("id", "unnamed"))
            errors: List[str] = []
            executable_value = check.get("executable_path")
            expected_executable_hash = check.get("executable_sha256")
            actual_executable_hash = None
            executable_path = (
                Path(executable_value)
                if isinstance(executable_value, str)
                else None
            )
            if (
                executable_path is None
                or not executable_path.is_absolute()
                or executable_path.is_symlink()
                or not executable_path.is_file()
            ):
                errors.append("executable_missing_or_unsafe")
            elif (
                not isinstance(expected_executable_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_executable_hash) is None
            ):
                errors.append("executable_hash_missing")
            else:
                actual_executable_hash = sha256_file(executable_path)
                if actual_executable_hash != expected_executable_hash:
                    errors.append("executable_hash_changed")

            authority_items: List[Dict[str, Any]] = []
            for record in check.get("authority_files", []):
                relative = record.get("path")
                expected = record.get("expected")
                expected_hash = record.get("sha256")
                state = "PASS"
                actual_hash = None
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
                            actual_hash = sha256_file(path)
                            if actual_hash != expected_hash:
                                state = "HASH_CHANGED"
                    elif expected == "absent":
                        if path.exists():
                            state = "UNEXPECTED_PRESENT"
                    else:
                        state = "INVALID_EXPECTATION"
                authority_items.append({
                    "path": relative,
                    "expected": expected,
                    "expected_sha256": expected_hash,
                    "actual_sha256": actual_hash,
                    "status": state,
                })
                if state != "PASS":
                    errors.append(f"authority_file:{relative}:{state}")

            if errors and first_error is None:
                first_error = f"check_authority_changed:{check_id}:{errors[0]}"
            items.append({
                "id": check_id,
                "status": "BLOCKED" if errors else "PASS",
                "executable_path": (
                    str(executable_path) if executable_path is not None else None
                ),
                "expected_executable_sha256": expected_executable_hash,
                "actual_executable_sha256": actual_executable_hash,
                "authority_files": authority_items,
                "errors": errors,
            })
        return {
            "status": "BLOCKED" if first_error else "PASS",
            "reason": first_error,
            "checks": items,
        }

    def _run_check(self, workspace: Path, run_id: str, check: Dict[str, Any]) -> Dict[str, Any]:
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("invalid_check_argv")
        cwd_rel = check.get("cwd", ".")
        cwd = workspace if cwd_rel == "." else safe_workspace_path(workspace, cwd_rel, must_exist=True)
        if not cwd.is_dir():
            raise ValueError("invalid_check_cwd")
        timeout = min(max(int(check.get("timeout_sec", 30)), 1), 120)
        executable = check.get("executable_path")
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            raise ValueError("check_executable_not_bound")
        executed_argv = [executable, *argv[1:]]
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        started = time.monotonic()
        try:
            proc = subprocess.run(executed_argv, cwd=str(cwd), env=env, capture_output=True, text=True,
                                  timeout=timeout, check=False)
            result = {"id": check.get("id", "unnamed"), "argv": argv,
                      "executed_argv": executed_argv, "exit_code": proc.returncode,
                      "duration_ms": int((time.monotonic() - started) * 1000),
                      "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-8000:], "timed_out": False}
        except subprocess.TimeoutExpired as exc:
            result = {"id": check.get("id", "unnamed"), "argv": argv,
                      "executed_argv": executed_argv, "exit_code": None,
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
        scope_evidence = self._workspace_scope_evidence(payload, receipt["changed_paths"])
        scope_log = self.logs / f"{run_id}-scope.json"
        scope_log.write_text(json.dumps(scope_evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        scope_evidence["log_path"] = str(scope_log)
        if scope_evidence["status"] == "BLOCKED":
            if scope_evidence["outside_allowlist"]:
                reason = "workspace_scope_violation:" + scope_evidence["outside_allowlist"][0]
            else:
                reason = "receipt_scope_mismatch"
            return self._block_verification(run_id, reason, evidence=scope_evidence)
        publication = payload.get("publication", {})
        if publication.get("kind") in {"git", "git_local"}:
            observed_base = scope_evidence.get("git_head")
            if not observed_base:
                return self._block_verification(
                    run_id, "workspace_git_unavailable", evidence=scope_evidence
                )
            claim_base = run["claim_git_head"]
            expected_base = claim_base or publication.get("expected_base")
            if expected_base and observed_base != expected_base:
                scope_evidence["expected_base"] = expected_base
                scope_evidence["claim_git_head"] = claim_base
                return self._block_verification(
                    run_id, "workspace_base_changed", evidence=scope_evidence
                )
        authority_evidence = self._check_authority_evidence(
            workspace, payload.get("checks", [])
        )
        authority_log = self.logs / f"{run_id}-check-authority.json"
        authority_log.write_text(
            json.dumps(authority_evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        authority_evidence["log_path"] = str(authority_log)
        if authority_evidence["status"] == "BLOCKED":
            scope_evidence["check_authority"] = authority_evidence
            return self._block_verification(
                run_id,
                authority_evidence["reason"],
                evidence=scope_evidence,
            )
        try:
            snapshot_id, manifest = self._snapshot(
                payload, receipt["changed_paths"], git_head=scope_evidence.get("git_head")
            )
        except ValueError as exc:
            reason = str(exc)
            safety_prefixes = (
                "protected_path_changed:", "path_escape", "symlink_not_allowed",
                "missing_path", "not_regular_file:",
            )
            if reason.startswith(safety_prefixes):
                return self._block_verification(run_id, reason, evidence=scope_evidence)
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
                            payload={"snapshot_id": snapshot_id, "checks": checks, "scope_evidence": scope_evidence})
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
                                 "review_decision": decision, "scope_evidence": scope_evidence})
            conn.execute("COMMIT")
        return {"status": next_state, "snapshot_id": snapshot_id, "checks": checks,
                "review_decision": decision, "scope_evidence": scope_evidence,
                "check_authority": authority_evidence}

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

    def _assert_manifest_current(self, payload: Dict[str, Any],
                                 manifest: Dict[str, Any]) -> None:
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

    def _assert_snapshot_current(self, conn: sqlite3.Connection, run: sqlite3.Row,
                                 payload: Dict[str, Any]) -> Dict[str, Any]:
        snap = conn.execute("SELECT manifest_json FROM snapshots WHERE snapshot_id=?", (run["snapshot_id"],)).fetchone()
        if not snap:
            raise ValueError("snapshot_missing")
        manifest = json.loads(snap["manifest_json"])
        self._assert_manifest_current(payload, manifest)
        return manifest

    def _publication_workspace_guard(
        self, run_id: str, payload: Dict[str, Any], manifest: Dict[str, Any],
        expected_base: str, *, phase: str,
    ) -> Dict[str, Any]:
        safe_phase = re.sub(r"[^A-Za-z0-9._-]+", "-", phase).strip("-") or "check"
        log_path = self.logs / f"{run_id}-publication-scope-{safe_phase}.json"
        try:
            self._assert_manifest_current(payload, manifest)
        except ValueError as exc:
            evidence = {
                "status": "BLOCKED",
                "phase": phase,
                "expected_base": expected_base,
                "reason": str(exc),
            }
            log_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            ensure_private_file(log_path)
            raise ValueError(f"publication_workspace_snapshot_changed:{exc}") from exc

        changed = sorted(manifest.get("files", {}))
        evidence = self._workspace_scope_evidence(payload, changed)
        evidence.update({
            "phase": phase,
            "expected_base": expected_base,
            "manifest_git_head": manifest.get("git_head"),
        })
        reason = None
        if evidence.get("status") == "NON_GIT_UNAVAILABLE":
            reason = "git_unavailable"
        elif evidence.get("status") != "PASS":
            if evidence.get("outside_allowlist"):
                reason = "outside_allowlist:" + evidence["outside_allowlist"][0]
            elif evidence.get("omitted_from_receipt"):
                reason = "unexpected_path:" + evidence["omitted_from_receipt"][0]
            elif evidence.get("declared_but_unobserved"):
                reason = "snapshot_path_not_observed:" + evidence["declared_but_unobserved"][0]
            else:
                reason = "scope_mismatch"
        elif evidence.get("git_head") != expected_base:
            reason = "base_changed:" + str(evidence.get("git_head") or "unknown")

        evidence["publication_guard_status"] = "BLOCKED" if reason else "PASS"
        if reason:
            evidence["reason"] = reason
        log_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ensure_private_file(log_path)
        evidence["log_path"] = str(log_path)
        if reason:
            raise ValueError("publication_workspace_scope_changed:" + reason)
        return evidence

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

    def _publication_update(self, run_id: str, *, status: str, operation_id: Optional[str] = None,
                            kind: Optional[str] = None, expected_base: Optional[str] = None,
                            staged_paths: Optional[List[str]] = None, commit_id: Optional[str] = None,
                            remote_commit: Optional[str] = None, error: Optional[str] = None) -> None:
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM publications WHERE run_id=?", (run_id,)).fetchone()
            now = utc_now()
            if existing:
                conn.execute(
                    "UPDATE publications SET status=?,staged_paths_json=COALESCE(?,staged_paths_json),"
                    "commit_id=COALESCE(?,commit_id),remote_commit=COALESCE(?,remote_commit),error=?,updated_at=? WHERE run_id=?",
                    (status, canonical_json(staged_paths) if staged_paths is not None else None,
                     commit_id, remote_commit, error, now, run_id),
                )
            else:
                if not operation_id or not kind:
                    raise ValueError("publication_intent_metadata_required")
                conn.execute(
                    "INSERT INTO publications(run_id,operation_id,kind,status,expected_base,staged_paths_json,commit_id,remote_commit,error,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (run_id, operation_id, kind, status, expected_base,
                     canonical_json(staged_paths) if staged_paths is not None else None,
                     commit_id, remote_commit, error, now),
                )

    def _publication_git(self, workspace: Path, *, binary: bool = False):
        env = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env.update({"GIT_PAGER": "cat", "PAGER": "cat"})
        filter_overrides, _drivers = self._git_filter_overrides(workspace)
        prefix = [
            "git", "--no-pager",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "diff.external=",
            "-c", f"core.hooksPath={self._disabled_git_hooks_path()}",
            *filter_overrides,
            "-C", str(workspace),
        ]

        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                prefix + list(args), env=env, capture_output=True,
                text=not binary, timeout=30, check=False,
            )
        return run

    def _publication_transport_git(
        self, workspace: Path, remote_url: str, *args: str, binary: bool = False
    ) -> subprocess.CompletedProcess:
        policy = inspect_transport_url(remote_url, workspace)
        if policy.get("status") != "READY":
            raise ValueError("git_transport_blocked:" + str(policy.get("reason")))
        return run_sandboxed_transport(
            workspace, self.runtime, policy["canonical_url"], args, binary=binary
        )

    def _assert_publication_paths_unfiltered(self, workspace: Path, paths: List[str]) -> None:
        attributes = self._git_filter_attributes(workspace, paths)
        filtered = sorted(
            (relative, value)
            for relative, value in attributes.items()
            if value not in {"unspecified", "unset"}
        )
        if filtered:
            relative, driver = filtered[0]
            raise ValueError(f"publication_filtered_path_not_supported:{relative}:{driver}")

    def _staged_matches_snapshot(self, workspace: Path, manifest: Dict[str, Any]) -> bool:
        gitb = self._publication_git(workspace, binary=True)
        changed = sorted(manifest.get("files", {}))
        staged_result = gitb("diff", "--cached", "--name-only", "-z")
        staged = sorted(os.fsdecode(item) for item in staged_result.stdout.split(b"\0") if item) if staged_result.returncode == 0 else []
        if staged != changed:
            return False
        for relative, recorded in manifest.get("files", {}).items():
            blob = gitb("show", f":{relative}")
            if recorded.get("deleted"):
                if blob.returncode == 0:
                    return False
            elif blob.returncode or sha256_bytes(blob.stdout) != recorded["sha256"]:
                return False
        return True

    def _commit_matches_snapshot(self, workspace: Path, commit_id: str, expected_base: Optional[str],
                                 manifest: Dict[str, Any]) -> bool:
        gitt = self._publication_git(workspace, binary=False)
        gitb = self._publication_git(workspace, binary=True)
        changed = sorted(manifest.get("files", {}))
        paths = gitb("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit_id)
        actual_paths = sorted(os.fsdecode(item) for item in paths.stdout.split(b"\0") if item) if paths.returncode == 0 else []
        if actual_paths != changed:
            return False
        if expected_base:
            parent = gitt("rev-parse", f"{commit_id}^")
            if parent.returncode or parent.stdout.strip() != expected_base:
                return False
        for relative, recorded in manifest.get("files", {}).items():
            blob = gitb("show", f"{commit_id}:{relative}")
            if recorded.get("deleted"):
                if blob.returncode == 0:
                    return False
            elif blob.returncode or sha256_bytes(blob.stdout) != recorded["sha256"]:
                return False
        return True

    def _prepare_snapshot_commit(self, workspace: Path, *, expected_base: str,
                                 manifest: Dict[str, Any], message: str) -> str:
        gitt = self._publication_git(workspace, binary=False)
        tree = gitt("write-tree")
        if tree.returncode or not tree.stdout.strip():
            raise RuntimeError("git_write_tree_failed:" + tree.stderr[-1000:])
        commit = gitt("commit-tree", tree.stdout.strip(), "-p", expected_base, "-m", message)
        if commit.returncode or not commit.stdout.strip():
            raise RuntimeError("git_commit_tree_failed:" + commit.stderr[-1000:])
        commit_id = commit.stdout.strip()
        if not self._commit_matches_snapshot(workspace, commit_id, expected_base, manifest):
            raise ValueError("prepared_snapshot_mismatch")
        return commit_id

    def _cas_update_branch(self, workspace: Path, *, branch: str,
                           commit_id: str, expected_base: str) -> None:
        gitt = self._publication_git(workspace, binary=False)
        symbolic = gitt("symbolic-ref", "-q", "HEAD")
        expected_ref = f"refs/heads/{branch}"
        if symbolic.returncode or symbolic.stdout.strip() != expected_ref:
            raise ValueError("publication_branch_ref_mismatch")
        update = gitt("update-ref", expected_ref, commit_id, expected_base)
        if update.returncode:
            head = gitt("rev-parse", "HEAD")
            actual = head.stdout.strip() if head.returncode == 0 else None
            raise ValueError(
                "publication_base_changed_during_commit:"
                + (actual or "unknown")
            )

    def _finalize_publication(self, run_id: str, task_id: str, *, commit_id: str,
                              remote_commit: Optional[str], kind: str, remote: Optional[str], ref: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE publications SET status='COMPLETE',commit_id=?,remote_commit=?,error=NULL,updated_at=? WHERE run_id=?",
                         (commit_id, remote_commit, now, run_id))
            conn.execute("UPDATE runs SET state='COMPLETE',completed_at=? WHERE run_id=?", (now, run_id))
            conn.execute("UPDATE tasks SET status='DONE',updated_at=? WHERE task_id=?", (now, task_id))
            self._event(conn, "PUBLISHED", task_id=task_id, run_id=run_id,
                        payload={"commit": commit_id, "kind": kind, "remote": remote, "ref": ref,
                                 "remote_commit": remote_commit})
            conn.execute("COMMIT")
        return {"status": "COMPLETE", "commit": commit_id, "remote_commit": remote_commit}

    def publish(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] != "VERIFIED":
                raise ValueError("run_not_verified")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            manifest = self._assert_snapshot_current(conn, run, payload)
            existing = conn.execute("SELECT status FROM publications WHERE run_id=?", (run_id,)).fetchone()
            if existing and existing["status"] not in {"ABANDONED"}:
                if existing["status"] == "COMPLETE":
                    row = conn.execute("SELECT commit_id,remote_commit FROM publications WHERE run_id=?", (run_id,)).fetchone()
                    return {"status": "COMPLETE", "commit": row["commit_id"], "remote_commit": row["remote_commit"]}
                raise ValueError(f"publication_reconciliation_required:{existing['status']}")
            if existing and existing["status"] == "ABANDONED":
                conn.execute("DELETE FROM publications WHERE run_id=?", (run_id,))
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
        self._assert_publication_paths_unfiltered(workspace, changed)
        gitt = self._publication_git(workspace, binary=False)
        gitb = self._publication_git(workspace, binary=True)
        branch = gitt("branch", "--show-current")
        if branch.returncode or branch.stdout.strip() != pub.get("branch", "main"):
            raise ValueError("publication_branch_mismatch")
        expected_base = pub.get("expected_base") or manifest.get("git_head")
        if not expected_base:
            raise ValueError("publication_expected_base_missing")
        head = gitt("rev-parse", "HEAD")
        if head.returncode or head.stdout.strip() != expected_base:
            raise ValueError("publication_base_changed")
        existing_staged = gitb("diff", "--cached", "--name-only", "-z")
        if existing_staged.returncode or existing_staged.stdout:
            raise ValueError("preexisting_staging_not_empty")
        self._publication_workspace_guard(
            run_id, payload, manifest, expected_base, phase="preflight"
        )
        operation_id = uuid.uuid4().hex
        self._publication_update(run_id, status="INTENT", operation_id=operation_id, kind=pub["kind"], expected_base=expected_base)
        phase = "INTENT"
        try:
            add = gitt("add", "--", *changed)
            if add.returncode:
                raise RuntimeError("git_add_failed:" + add.stderr[-1000:])
            if not self._staged_matches_snapshot(workspace, manifest):
                raise ValueError("staged_snapshot_mismatch;staging_requires_reconciliation")
            phase = "STAGED"
            self._publication_update(run_id, status=phase, staged_paths=changed)
            message = pub.get("commit_message") or f"orch: complete {run['task_id']}"
            commit_id = self._prepare_snapshot_commit(
                workspace, expected_base=expected_base, manifest=manifest, message=message
            )
            phase = "PREPARED"
            self._publication_update(run_id, status=phase, commit_id=commit_id)
            self._publication_workspace_guard(
                run_id, payload, manifest, expected_base, phase="pre-ref-update"
            )
            self._cas_update_branch(
                workspace, branch=branch.stdout.strip(),
                commit_id=commit_id, expected_base=expected_base,
            )
            phase = "COMMITTED"
            self._publication_update(run_id, status=phase, commit_id=commit_id)
            remote_commit = None
            remote = pub.get("remote", "origin")
            remote_url = pub.get("remote_url")
            ref = pub.get("ref", "main")
            if pub.get("kind") == "git":
                if not isinstance(remote_url, str) or not remote_url:
                    raise ValueError("publication_remote_url_missing")
                push = self._publication_transport_git(
                    workspace, remote_url, "push", remote_url,
                    f"{commit_id}:refs/heads/{ref}",
                )
                if push.returncode:
                    raise RuntimeError("git_push_failed:" + push.stderr[-1000:])
                phase = "PUSHED"
                self._publication_update(run_id, status=phase, commit_id=commit_id)
                remote_ref = self._publication_transport_git(
                    workspace, remote_url, "ls-remote", remote_url, f"refs/heads/{ref}"
                )
                remote_commit = remote_ref.stdout.split()[0] if remote_ref.returncode == 0 and remote_ref.stdout.strip() else None
                if remote_commit != commit_id:
                    raise ValueError("remote_verification_failed")
                phase = "REMOTE_VERIFIED"
                self._publication_update(run_id, status=phase, commit_id=commit_id, remote_commit=remote_commit)
            return self._finalize_publication(run_id, run["task_id"], commit_id=commit_id,
                                              remote_commit=remote_commit, kind=pub["kind"],
                                              remote=remote if pub.get("kind") == "git" else None,
                                              ref=ref if pub.get("kind") == "git" else None)
        except Exception as exc:
            self._publication_update(run_id, status=phase, error=f"{type(exc).__name__}:{str(exc)[:900]}")
            raise

    def reconcile_publication(self, run_id: str, *, resume: bool = False) -> Dict[str, Any]:
        with self.connect() as conn:
            journal = conn.execute("SELECT * FROM publications WHERE run_id=?", (run_id,)).fetchone()
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not journal:
                return {"status": "NO_PUBLICATION", "run_id": run_id}
            if not run:
                raise ValueError("unknown_run")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            payload = self._task_payload(task)
            snap = conn.execute("SELECT manifest_json FROM snapshots WHERE snapshot_id=?", (run["snapshot_id"],)).fetchone()
            if not snap:
                raise ValueError("snapshot_missing")
            manifest = json.loads(snap["manifest_json"])
            if journal["status"] == "COMPLETE":
                return {"status": "COMPLETE", "commit": journal["commit_id"], "remote_commit": journal["remote_commit"]}
        pub = payload.get("publication", {})
        workspace = Path(payload["workspace"]).resolve()
        gitt = self._publication_git(workspace, binary=False)
        gitb = self._publication_git(workspace, binary=True)
        expected_base = journal["expected_base"] or pub.get("expected_base") or manifest.get("git_head")
        if not expected_base:
            return {"status": "BLOCKED", "run_id": run_id, "reason": "publication_expected_base_missing"}
        head_result = gitt("rev-parse", "HEAD")
        head = head_result.stdout.strip() if head_result.returncode == 0 else None
        staged = self._staged_matches_snapshot(workspace, manifest)
        status = journal["status"]
        commit_id = journal["commit_id"]
        if commit_id and not self._commit_matches_snapshot(workspace, commit_id, expected_base, manifest):
            return {"status": "BLOCKED", "run_id": run_id, "reason": "commit_not_proven"}
        if not commit_id and head and head != expected_base and self._commit_matches_snapshot(
            workspace, head, expected_base, manifest
        ):
            commit_id = head
            status = "COMMITTED"
            self._publication_update(run_id, status=status, commit_id=commit_id, error=None)
        if head == expected_base and not commit_id:
            if not staged:
                staged_raw = gitb("diff", "--cached", "--name-only", "-z")
                if staged_raw.returncode == 0 and not staged_raw.stdout and status == "INTENT":
                    self._publication_update(run_id, status="ABANDONED", error=None)
                    return {"status": "SAFE_TO_RETRY", "run_id": run_id, "reason": "no_git_side_effect_observed"}
                return {"status": "BLOCKED", "run_id": run_id, "reason": "unexpected_staging_state"}
            try:
                self._publication_workspace_guard(
                    run_id, payload, manifest, expected_base, phase="reconcile-staged"
                )
            except ValueError as exc:
                return {"status": "BLOCKED", "run_id": run_id, "reason": str(exc)}
            if not resume:
                return {"status": "STAGED_PENDING_COMMIT", "run_id": run_id, "resume_available": True}
            message = pub.get("commit_message") or f"orch: complete {run['task_id']}"
            try:
                commit_id = self._prepare_snapshot_commit(
                    workspace, expected_base=expected_base, manifest=manifest, message=message
                )
            except Exception as exc:
                self._publication_update(
                    run_id, status="STAGED",
                    error=f"{type(exc).__name__}:{str(exc)[:900]}"
                )
                return {"status": "BLOCKED", "run_id": run_id, "reason": "commit_prepare_failed"}
            status = "PREPARED"
            self._publication_update(run_id, status=status, commit_id=commit_id, error=None)
        if commit_id and head == expected_base:
            if not staged:
                return {"status": "BLOCKED", "run_id": run_id, "reason": "unexpected_staging_state"}
            try:
                self._publication_workspace_guard(
                    run_id, payload, manifest, expected_base, phase="reconcile-pre-ref-update"
                )
            except ValueError as exc:
                return {"status": "BLOCKED", "run_id": run_id, "reason": str(exc)}
            if not resume:
                return {
                    "status": "PREPARED_PENDING_REF_UPDATE", "run_id": run_id,
                    "commit": commit_id, "resume_available": True,
                }
            try:
                self._cas_update_branch(
                    workspace, branch=pub.get("branch", "main"),
                    commit_id=commit_id, expected_base=expected_base,
                )
            except ValueError:
                return {
                    "status": "BLOCKED", "run_id": run_id,
                    "reason": "publication_base_changed_during_commit",
                }
            head = commit_id
            status = "COMMITTED"
            self._publication_update(run_id, status=status, commit_id=commit_id, error=None)
        if commit_id and head == commit_id:
            if status != "COMMITTED":
                status = "COMMITTED"
                self._publication_update(run_id, status=status, commit_id=commit_id, error=None)
        elif commit_id and head != commit_id:
            return {
                "status": "BLOCKED", "run_id": run_id,
                "reason": "publication_base_changed", "head": head,
            }
        if not commit_id or not self._commit_matches_snapshot(workspace, commit_id, expected_base, manifest):
            return {"status": "BLOCKED", "run_id": run_id, "reason": "commit_not_proven"}
        if pub.get("kind") == "git_local":
            return self._finalize_publication(run_id, run["task_id"], commit_id=commit_id,
                                              remote_commit=None, kind="git_local", remote=None, ref=None)
        remote = pub.get("remote", "origin")
        remote_url = pub.get("remote_url")
        ref = pub.get("ref", "main")
        if not isinstance(remote_url, str) or not remote_url:
            return {"status": "BLOCKED", "run_id": run_id, "reason": "publication_remote_url_missing"}
        remote_ref = self._publication_transport_git(
            workspace, remote_url, "ls-remote", remote_url, f"refs/heads/{ref}"
        )
        remote_commit = remote_ref.stdout.split()[0] if remote_ref.returncode == 0 and remote_ref.stdout.strip() else None
        if remote_commit == commit_id:
            self._publication_update(run_id, status="REMOTE_VERIFIED", commit_id=commit_id, remote_commit=remote_commit, error=None)
            return self._finalize_publication(run_id, run["task_id"], commit_id=commit_id,
                                              remote_commit=remote_commit, kind="git", remote=remote, ref=ref)
        if not resume:
            return {"status": "COMMIT_PROVEN_REMOTE_PENDING", "run_id": run_id,
                    "commit": commit_id, "remote_commit": remote_commit, "resume_available": True}
        if remote_commit not in {None, expected_base}:
            return {"status": "BLOCKED", "run_id": run_id, "reason": "remote_advanced", "remote_commit": remote_commit}
        push = self._publication_transport_git(
            workspace, remote_url, "push", remote_url, f"{commit_id}:refs/heads/{ref}"
        )
        if push.returncode:
            self._publication_update(run_id, status="COMMITTED", commit_id=commit_id, error="git_push_failed:" + push.stderr[-900:])
            return {"status": "BLOCKED", "run_id": run_id, "reason": "git_push_failed"}
        self._publication_update(run_id, status="PUSHED", commit_id=commit_id, error=None)
        remote_ref = self._publication_transport_git(
            workspace, remote_url, "ls-remote", remote_url, f"refs/heads/{ref}"
        )
        remote_commit = remote_ref.stdout.split()[0] if remote_ref.returncode == 0 and remote_ref.stdout.strip() else None
        if remote_commit != commit_id:
            return {"status": "BLOCKED", "run_id": run_id, "reason": "remote_verification_failed", "remote_commit": remote_commit}
        self._publication_update(run_id, status="REMOTE_VERIFIED", commit_id=commit_id, remote_commit=remote_commit, error=None)
        return self._finalize_publication(run_id, run["task_id"], commit_id=commit_id,
                                          remote_commit=remote_commit, kind="git", remote=remote, ref=ref)

    def queue_view(self, project_id: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        if project_id is not None and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("invalid_queue_limit")
        with self.connect() as conn:
            paused = conn.execute("SELECT value_json FROM settings WHERE key='paused'").fetchone()
            project_pauses = self._project_pauses(conn)
            locks = self._active_writer_locks(conn)
            active_by_task = {item["task_id"]: item for item in locks.values()}
            rows = conn.execute("SELECT * FROM tasks ORDER BY queue_seq,task_id").fetchall()
            task_statuses = {row["task_id"]: row["status"] for row in rows}
            attempt_rows = conn.execute(
                "SELECT task_id,COUNT(*) AS n FROM runs GROUP BY task_id"
            ).fetchall()
            attempts_by_task = {row["task_id"]: row["n"] for row in attempt_rows}
        items: List[Dict[str, Any]] = []
        task_status_counts: Dict[str, int] = {}
        queue_state_counts: Dict[str, int] = {}
        for row in rows:
            if project_id is not None and row["project_id"] != project_id:
                continue
            payload = self._task_payload(row)
            attempts = int(attempts_by_task.get(row["task_id"], 0))
            max_attempts = int(payload.get("max_attempts", 2))
            waiting_dependencies = [
                dep for dep in payload.get("dependencies", [])
                if task_statuses.get(dep) != "DONE"
            ]
            lock = locks.get(row["writer_key"])
            if row["task_id"] in active_by_task:
                queue_state = "ACTIVE"
            elif row["status"] in READY_TASK_STATES:
                if row["project_id"] in project_pauses:
                    queue_state = "PAUSED_PROJECT"
                elif attempts >= max_attempts:
                    queue_state = "EXHAUSTED"
                elif waiting_dependencies:
                    queue_state = "WAITING_DEPENDENCY"
                elif lock:
                    queue_state = "WAITING_WRITER"
                else:
                    queue_state = "READY"
            else:
                queue_state = row["status"]
            task_status_counts[row["status"]] = task_status_counts.get(row["status"], 0) + 1
            queue_state_counts[queue_state] = queue_state_counts.get(queue_state, 0) + 1
            item = {
                "task_id": row["task_id"],
                "project_id": row["project_id"],
                "queue_seq": row["queue_seq"],
                "task_status": row["status"],
                "queue_state": queue_state,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "writer_key": row["writer_key"],
                "waiting_dependencies": waiting_dependencies,
            }
            if lock and queue_state == "WAITING_WRITER":
                item["writer_lock"] = {
                    "run_id": lock["run_id"],
                    "task_id": lock["task_id"],
                    "state": lock["state"],
                }
            items.append(item)
        total = len(items)
        visible_project_pauses = (
            {project_id: project_pauses[project_id]}
            if project_id is not None and project_id in project_pauses
            else project_pauses if project_id is None else {}
        )
        return {
            "status": (
                "PAUSED" if paused
                else "PROJECT_PAUSED" if project_id is not None and project_id in project_pauses
                else "OK"
            ),
            "project_id": project_id,
            "paused": json.loads(paused["value_json"]) if paused else None,
            "project_pauses": visible_project_pauses,
            "summary": {
                "total": total,
                "task_status": dict(sorted(task_status_counts.items())),
                "queue_state": dict(sorted(queue_state_counts.items())),
                "active_writer_count": len(locks),
                "paused_project_count": len(visible_project_pauses),
            },
            "active_writers": list(locks.values()),
            "tasks": items[:limit],
            "shown": min(total, limit),
            "truncated": total > limit,
        }

    def project_unresolved_tasks(self, project_id: str) -> Dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        terminal = {"DONE", "CANCELLED"}
        with self.connect() as conn:
            rows = [
                dict(row) for row in conn.execute(
                    "SELECT task_id,status,queue_seq FROM tasks "
                    "WHERE project_id=? ORDER BY queue_seq,task_id",
                    (project_id,),
                ).fetchall()
            ]
        unresolved = [item for item in rows if item["status"] not in terminal]
        return {
            "project_id": project_id,
            "has_unresolved": bool(unresolved),
            "unresolved_tasks": unresolved,
            "historical_task_count": len(rows),
        }

    def project_removal_guard(self, project_id: str) -> Dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        terminal = {"DONE", "CANCELLED"}
        with self.connect() as conn:
            tasks = [
                dict(row) for row in conn.execute(
                    "SELECT task_id,status,queue_seq,updated_at FROM tasks "
                    "WHERE project_id=? ORDER BY queue_seq,task_id",
                    (project_id,),
                ).fetchall()
            ]
            blocking_tasks = [
                item for item in tasks if item["status"] not in terminal
            ]
            writer_locks = [
                item for item in self._active_writer_locks(conn).values()
                if item.get("project_id") == project_id
            ]
            pending_publications = [
                dict(row) for row in conn.execute(
                    "SELECT p.run_id,p.status,p.commit_id,p.remote_commit,p.error,p.updated_at "
                    "FROM publications p JOIN runs r ON r.run_id=p.run_id "
                    "JOIN tasks t ON t.task_id=r.task_id "
                    "WHERE t.project_id=? AND p.status NOT IN ('COMPLETE','ABANDONED') "
                    "ORDER BY p.updated_at",
                    (project_id,),
                ).fetchall()
            ]
            pause = conn.execute(
                "SELECT value_json FROM settings WHERE key=?",
                (f"project_paused:{project_id}",),
            ).fetchone()
        blocked = bool(blocking_tasks or writer_locks or pending_publications)
        return {
            "status": "BLOCKED" if blocked else "SAFE",
            "project_id": project_id,
            "reason": "durable_project_work_present" if blocked else None,
            "blocking_tasks": blocking_tasks,
            "writer_locks": writer_locks,
            "pending_publications": pending_publications,
            "historical_task_count": len(tasks),
            "project_pause": json.loads(pause["value_json"]) if pause else None,
            "terminal_task_states": sorted(terminal),
        }

    def record_project_removed(self, project_id: str) -> Dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            key = f"project_paused:{project_id}"
            pause = conn.execute(
                "SELECT value_json FROM settings WHERE key=?", (key,)
            ).fetchone()
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
            self._event(
                conn, "PROJECT_DEREGISTERED",
                payload={
                    "project_id": project_id,
                    "cleared_pause": bool(pause),
                },
            )
            conn.execute("COMMIT")
        return {
            "status": "RECORDED",
            "project_id": project_id,
            "cleared_pause": bool(pause),
        }

    def pause_project(self, project_id: str, reason: str) -> Dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        if not reason.strip():
            raise ValueError("pause_reason_required")
        details = {
            "project_id": project_id,
            "reason": reason.strip()[:500],
            "paused_at": utc_now(),
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE project_id=? LIMIT 1", (project_id,)
            ).fetchone()
            if not exists:
                conn.execute("ROLLBACK")
                raise ValueError("unknown_project_queue")
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                (
                    f"project_paused:{project_id}",
                    canonical_json(details),
                    details["paused_at"],
                ),
            )
            self._event(
                conn, "PROJECT_PAUSED",
                payload={
                    "project_id": project_id,
                    "reason": details["reason"],
                },
            )
            conn.execute("COMMIT")
        return {"status": "PROJECT_PAUSED", "project_id": project_id, "details": details}

    def resume_project(self, project_id: str) -> Dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        key = f"project_paused:{project_id}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT value_json FROM settings WHERE key=?", (key,)
            ).fetchone()
            if not existing:
                conn.execute("COMMIT")
                return {
                    "status": "PROJECT_RESUMED",
                    "project_id": project_id,
                    "already_resumed": True,
                }
            previous = json.loads(existing["value_json"])
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
            self._event(
                conn, "PROJECT_RESUMED",
                payload={"project_id": project_id, "previous_pause": previous},
            )
            conn.execute("COMMIT")
        return {
            "status": "PROJECT_RESUMED",
            "project_id": project_id,
            "already_resumed": False,
        }

    def cancel_task(self, task_id: str, reason: str) -> Dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]+", task_id) is None:
            raise ValueError("invalid_task_id")
        if not reason.strip():
            raise ValueError("cancel_reason_required")
        cancellable = READY_TASK_STATES | {"BLOCKED"}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                conn.execute("ROLLBACK")
                raise ValueError("unknown_task")
            if task["status"] == "CANCELLED":
                conn.execute("COMMIT")
                return {"status": "CANCELLED", "task_id": task_id, "already_cancelled": True}
            active = conn.execute(
                "SELECT run_id,state FROM runs WHERE task_id=? AND state IN (?,?,?,?,?) "
                "ORDER BY started_at DESC LIMIT 1",
                (task_id, *tuple(ACTIVE_RUN_STATES)),
            ).fetchone()
            if active:
                conn.execute("ROLLBACK")
                raise ValueError(f"task_active:{active['run_id']}")
            if task["status"] not in cancellable:
                conn.execute("ROLLBACK")
                raise ValueError(f"task_not_cancellable:{task['status']}")
            now = utc_now()
            conn.execute(
                "UPDATE tasks SET status='CANCELLED',updated_at=? WHERE task_id=?",
                (now, task_id),
            )
            self._event(
                conn, "TASK_CANCELLED", task_id=task_id,
                payload={"reason": reason.strip()[:500], "previous_status": task["status"]},
            )
            conn.execute("COMMIT")
        return {
            "status": "CANCELLED", "task_id": task_id,
            "previous_status": task["status"], "reason": reason.strip()[:500],
        }

    def status(self) -> Dict[str, Any]:
        with self.connect() as conn:
            tasks = [
                dict(row) for row in conn.execute(
                    "SELECT task_id,plan_revision,ordinal,project_id,writer_key,queue_seq,status,updated_at "
                    "FROM tasks ORDER BY queue_seq,task_id"
                )
            ]
            runs = [
                dict(row) for row in conn.execute(
                    "SELECT r.run_id,r.task_id,r.attempt,r.worker_id,r.state,r.started_at,r.heartbeat_at,"
                    "r.claim_git_head,r.snapshot_id,r.verify_status,r.review_status,"
                    "r.completed_at,r.error,"
                    "t.project_id,t.writer_key "
                    "FROM runs r JOIN tasks t ON t.task_id=r.task_id ORDER BY r.started_at"
                )
            ]
            events = [
                dict(row) for row in conn.execute(
                    "SELECT seq,ts,kind,task_id,run_id,payload_json FROM events ORDER BY seq DESC LIMIT 30"
                )
            ]
            paused = conn.execute("SELECT value_json FROM settings WHERE key='paused'").fetchone()
            project_pauses = self._project_pauses(conn)
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return {
            "schema_version": 1, "variant": "B_NEW_CHAT_PER_ATTEMPT",
            "paused": json.loads(paused["value_json"]) if paused else None,
            "project_pauses": project_pauses,
            "tasks": tasks, "runs": runs, "recent_events": events,
        }

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

    def next_work(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        if project_id is not None and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", project_id) is None:
            raise ValueError("invalid_project_id")
        with self.connect() as conn:
            paused = conn.execute("SELECT value_json FROM settings WHERE key='paused'").fetchone()
            if paused:
                return {"status": "PAUSED", "details": json.loads(paused["value_json"])}
            locks = self._active_writer_locks(conn)
            project_pauses = self._project_pauses(conn)
            if project_id is not None and project_id in project_pauses:
                return {
                    "status": "PROJECT_PAUSED",
                    "project_id": project_id,
                    "details": project_pauses[project_id],
                }
            blocked_locks: List[Dict[str, Any]] = []
            paused_projects: Dict[str, Dict[str, Any]] = {}
            for row in conn.execute("SELECT * FROM tasks ORDER BY queue_seq,task_id").fetchall():
                if row["status"] not in READY_TASK_STATES:
                    continue
                if project_id is not None and row["project_id"] != project_id:
                    continue
                if row["project_id"] in project_pauses:
                    paused_projects[row["project_id"]] = project_pauses[row["project_id"]]
                    continue
                payload = self._task_payload(row)
                if not self._dependencies_done(conn, payload):
                    continue
                attempts = conn.execute(
                    "SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (row["task_id"],)
                ).fetchone()["n"]
                if attempts >= int(payload.get("max_attempts", 2)):
                    continue
                lock = locks.get(row["writer_key"])
                if lock:
                    blocked_locks.append(lock)
                    continue
                return {
                    "status": "READY", "task_id": row["task_id"], "project_id": row["project_id"],
                    "writer_key": row["writer_key"], "queue_seq": row["queue_seq"],
                    "next_attempt": attempts + 1,
                }
            if blocked_locks:
                unique = {item["run_id"]: item for item in blocked_locks}
                active = list(unique.values())
                return {"status": "BUSY", "active": active[0], "locks": active}
            if paused_projects:
                return {
                    "status": "PROJECTS_PAUSED",
                    "project_id": project_id,
                    "paused_projects": paused_projects,
                }
            return {"status": "NO_WORK", "project_id": project_id}

    def reconcile(self) -> Dict[str, Any]:
        with self.connect() as conn:
            active = [dict(row) for row in conn.execute(
                "SELECT run_id,task_id,state,heartbeat_at FROM runs "
                "WHERE state IN (?,?,?,?,?) ORDER BY started_at",
                tuple(ACTIVE_RUN_STATES),
            )]
            writer_locks = list(self._active_writer_locks(conn).values())
            pending_publications = [dict(row) for row in conn.execute(
                "SELECT run_id,status,commit_id,remote_commit,error,updated_at FROM publications "
                "WHERE status NOT IN ('COMPLETE','ABANDONED') ORDER BY updated_at"
            )]
        return {
            "status": "ATTENTION" if writer_locks or pending_publications else "CLEAN",
            "active_runs": active,
            "writer_locks": writer_locks,
            "pending_publications": pending_publications,
            "rule": "No automatic lease expiry or blind publication retry; reconcile observed state before a new writer.",
        }
