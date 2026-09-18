import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile

from orch.core import Orchestrator
from orch.state import backup_state, check_state, migration_history, prune_capabilities


class StateMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orch"
        self.root.mkdir()
        self.orch = Orchestrator(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_state_has_schema_version_and_integrity(self):
        result = check_state(self.orch)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["quick_check"], ["ok"])
        self.assertEqual(result["foreign_key_violations"], [])
        self.assertEqual(result["migration_history"][-1]["version"], 3)
        self.assertIn(result["migration_history"][-1]["details"]["kind"], {
            "transactional_upgrade", "observed_existing_schema",
        })

    def test_orphan_capability_is_reported_and_pruned(self):
        claims = self.orch.runtime / "claims"
        claims.mkdir(exist_ok=True)
        stale = claims / "OLD-A1-deadbeef00.json"
        stale.write_text(json.dumps({"run_id": stale.stem, "lease_token": "dead"}) + "\n")
        self.assertEqual(check_state(self.orch)["status"], "ATTENTION")
        pruned = prune_capabilities(self.orch)
        self.assertEqual(pruned["count"], 1)
        self.assertFalse(stale.exists())
        self.assertEqual(check_state(self.orch)["status"], "READY")

    def test_backup_excludes_claims_and_contains_consistent_database(self):
        (self.root / "config.json").write_text('{"schema_version":1}\n')
        (self.orch.runtime / "logs" / "event.json").write_text('{"ok":true}\n')
        claims = self.orch.runtime / "claims"; claims.mkdir(exist_ok=True)
        (claims / "STALE.json").write_text('{"lease_token":"secret"}\n')
        result = backup_state(self.orch)
        archive = Path(result["path"])
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            self.assertIn("state/orch.sqlite3", names)
            self.assertIn("state/manifest.json", names)
            self.assertIn("files/config.json", names)
            self.assertIn("files/.runtime/logs/event.json", names)
            self.assertFalse(any("claims" in name for name in names))
            extracted = Path(self.tmp.name) / "backup.sqlite3"
            extracted.write_bytes(bundle.read("state/orch.sqlite3"))
        conn = sqlite3.connect(str(extracted))
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        finally:
            conn.close()

    def test_backup_refuses_active_writer(self):
        workspace = Path(self.tmp.name) / "ws"; workspace.mkdir()
        plan = {
            "schema_version": 1,
            "plan_revision": "active-v1",
            "tasks": [{
                "id": "ACTIVE-1", "goal": "fixture", "workspace": str(workspace),
                "dependencies": [], "allowed_paths": ["result.json"], "protected_paths": {},
                "checks": [], "required_review": False, "owner_acceptance": False,
                "publication": {"kind": "none"}, "max_attempts": 2,
            }],
        }
        path = self.root / "plan.json"; path.write_text(json.dumps(plan))
        self.orch.load_plan(path); self.orch.claim("worker")
        with self.assertRaisesRegex(ValueError, "active_runs_present"):
            backup_state(self.orch)
        with self.assertRaisesRegex(ValueError, "active_runs_present"):
            prune_capabilities(self.orch)

    def test_schema_v2_tasks_are_migrated_to_project_queue_v3(self):
        other = Path(self.tmp.name) / "legacy"
        runtime = other / ".runtime"
        runtime.mkdir(parents=True)
        workspace = Path(self.tmp.name) / "legacy-workspace"
        workspace.mkdir()
        db = sqlite3.connect(str(runtime / "orch.sqlite3"))
        try:
            db.execute(
                "CREATE TABLE tasks ("
                "task_id TEXT PRIMARY KEY, plan_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,"
                "status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            payload = {
                "id": "LEGACY-1", "project_id": "legacy-project", "goal": "legacy",
                "workspace": str(workspace), "dependencies": [], "allowed_paths": ["out.json"],
                "protected_paths": {}, "checks": [], "publication": {"kind": "none"},
            }
            db.execute(
                "INSERT INTO tasks(task_id,plan_revision,ordinal,status,payload_json,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                ("LEGACY-1", "legacy-v2", 0, "PLANNED", json.dumps(payload), "2026-01-01T00:00:00Z"),
            )
            db.execute("PRAGMA user_version=2")
            db.commit()
        finally:
            db.close()
        migrated = Orchestrator(other)
        result = check_state(migrated)
        self.assertEqual(result["schema_version"], 3)
        with migrated.connect() as conn:
            row = conn.execute(
                "SELECT project_id,writer_key,queue_seq FROM tasks WHERE task_id='LEGACY-1'"
            ).fetchone()
        self.assertEqual(row["project_id"], "legacy-project")
        self.assertTrue(row["writer_key"].startswith("workspace:"))
        self.assertEqual(row["queue_seq"], 1)
        history = migration_history(migrated)
        self.assertEqual(history["migrations"][-1]["from_version"], 2)
        self.assertEqual(history["migrations"][-1]["details"]["kind"], "transactional_upgrade")

    def test_failed_schema_upgrade_rolls_back_all_task_ddl(self):
        other = Path(self.tmp.name) / "broken-upgrade"
        runtime = other / ".runtime"
        runtime.mkdir(parents=True)
        workspace = Path(self.tmp.name) / "broken-workspace"
        workspace.mkdir()
        db_path = runtime / "orch.sqlite3"
        db = sqlite3.connect(str(db_path))
        try:
            db.execute(
                "CREATE TABLE tasks ("
                "task_id TEXT PRIMARY KEY, plan_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,"
                "status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            payload = {
                "id": "BROKEN-1", "goal": "broken", "workspace": str(workspace),
                "writer_key": "invalid writer key!", "dependencies": [],
                "allowed_paths": ["out.json"], "protected_paths": {}, "checks": [],
                "publication": {"kind": "none"},
            }
            db.execute(
                "INSERT INTO tasks(task_id,plan_revision,ordinal,status,payload_json,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                ("BROKEN-1", "legacy-v2", 0, "PLANNED", json.dumps(payload), "2026-01-01T00:00:00Z"),
            )
            db.execute("PRAGMA user_version=2")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "invalid_writer_key"):
            Orchestrator(other)
        db = sqlite3.connect(str(db_path))
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            self.assertNotIn("project_id", columns)
            self.assertNotIn("writer_key", columns)
            self.assertNotIn("queue_seq", columns)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 0)
        finally:
            db.close()

    def test_future_state_schema_is_rejected(self):
        other = Path(self.tmp.name) / "future"; runtime = other / ".runtime"
        runtime.mkdir(parents=True)
        db = sqlite3.connect(str(runtime / "orch.sqlite3"))
        try:
            db.execute("PRAGMA user_version=99")
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "unsupported_state_schema:99"):
            Orchestrator(other)


if __name__ == "__main__":
    unittest.main()
