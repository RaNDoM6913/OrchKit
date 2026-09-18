import json
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile

from orch.core import Orchestrator
from orch.state import (backup_state, check_state, migration_history, prune_capabilities,
                        prune_retention, retention_status)


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

    def test_runtime_permissions_are_private_and_existing_modes_are_repaired(self):
        private_dirs = [
            self.orch.runtime,
            self.orch.runtime / "logs",
            self.orch.runtime / "worker_receipts",
            self.orch.runtime / "claims",
            self.orch.runtime / "review_exports",
            self.orch.runtime / "git-hooks-disabled",
        ]
        for path in private_dirs:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.orch.db_path.stat().st_mode & 0o777, 0o600)

        os.chmod(self.orch.runtime, 0o755)
        os.chmod(self.orch.db_path, 0o644)
        for sidecar in (
            self.orch.db_path.with_name(self.orch.db_path.name + "-wal"),
            self.orch.db_path.with_name(self.orch.db_path.name + "-shm"),
        ):
            if sidecar.exists():
                os.chmod(sidecar, 0o644)

        repaired = Orchestrator(self.root)
        self.assertEqual(repaired.runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual(repaired.db_path.stat().st_mode & 0o777, 0o600)
        for sidecar in (
            repaired.db_path.with_name(repaired.db_path.name + "-wal"),
            repaired.db_path.with_name(repaired.db_path.name + "-shm"),
        ):
            if sidecar.exists():
                self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        health = check_state(repaired)
        self.assertEqual(health["permissions"]["status"], "READY")
        self.assertNotEqual(health["status"], "BLOCKED")

    def test_runtime_symlink_is_rejected(self):
        other = Path(self.tmp.name) / "symlink-root"
        other.mkdir()
        external = Path(self.tmp.name) / "external-runtime"
        external.mkdir()
        (other / ".runtime").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "private_directory_unsafe"):
            Orchestrator(other)

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
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
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

    def test_retention_prunes_terminal_evidence_but_preserves_active_run(self):
        ws1 = Path(self.tmp.name) / "retention-ws1"
        ws2 = Path(self.tmp.name) / "retention-ws2"
        ws1.mkdir(); ws2.mkdir()
        plan = {
            "schema_version": 1,
            "plan_revision": "retention-v1",
            "tasks": [
                {
                    "id": "OLD-1", "goal": "old", "workspace": str(ws1),
                    "dependencies": [], "allowed_paths": ["out.json"], "protected_paths": {},
                    "checks": [], "publication": {"kind": "none"}, "max_attempts": 2,
                },
                {
                    "id": "ACTIVE-2", "goal": "active", "workspace": str(ws2),
                    "dependencies": [], "allowed_paths": ["out.json"], "protected_paths": {},
                    "checks": [], "publication": {"kind": "none"}, "max_attempts": 2,
                },
            ],
        }
        path = self.root / "retention-plan.json"
        path.write_text(json.dumps(plan))
        self.orch.load_plan(path)
        old = self.orch.claim("worker-old")
        self.orch.abort(old["run_id"], "terminal fixture", retry=False)
        active = self.orch.claim("worker-active")
        logs = self.orch.runtime / "logs"
        receipts = self.orch.runtime / "worker_receipts"
        exports = self.orch.runtime / "review_exports"
        (logs / f"{old['run_id']}-scope.json").write_text('{"old":true}\n')
        (receipts / f"{old['run_id']}.json").write_text('{"old":true}\n')
        old_export = exports / old["run_id"]; old_export.mkdir(parents=True)
        (old_export / "review.json").write_text('{"old":true}\n')
        (logs / f"{active['run_id']}-scope.json").write_text('{"active":true}\n')
        (receipts / f"{active['run_id']}.json").write_text('{"active":true}\n')
        active_export = exports / active["run_id"]; active_export.mkdir(parents=True)
        (active_export / "review.json").write_text('{"active":true}\n')

        result = prune_retention(
            self.orch, older_than_days=0, max_evidence_bytes=1024 * 1024,
            keep_recent_runs=20, keep_backups=5, max_backup_bytes=1024 * 1024,
        )
        self.assertEqual(result["pruned"]["run_count"], 1)
        self.assertEqual(result["pruned"]["runs"][0]["run_id"], old["run_id"])
        self.assertFalse((logs / f"{old['run_id']}-scope.json").exists())
        self.assertFalse((receipts / f"{old['run_id']}.json").exists())
        self.assertFalse(old_export.exists())
        self.assertTrue((logs / f"{active['run_id']}-scope.json").exists())
        self.assertTrue((receipts / f"{active['run_id']}.json").exists())
        self.assertTrue(active_export.exists())
        status = retention_status(self.orch)
        self.assertIn(active["run_id"], status["evidence"]["protected_runs"])

    def test_retention_reports_unknown_artifacts_without_deleting_them(self):
        rogue = self.orch.runtime / "logs" / "manual-note.json"
        rogue.write_text('{"owner":"keep"}\n')
        result = prune_retention(
            self.orch, older_than_days=0, max_evidence_bytes=0,
            keep_recent_runs=0, keep_backups=1, max_backup_bytes=0,
        )
        self.assertTrue(rogue.exists())
        self.assertFalse(result["bounded"])
        self.assertEqual(result["evidence"]["unmanaged"][0]["reason"], "unknown_run")

    def test_retention_prunes_only_managed_backup_files(self):
        backups = self.root / "backups"
        backups.mkdir()
        managed = []
        for index in range(3):
            item = backups / f"orch-state-2026010{index + 1}.zip"
            item.write_bytes(bytes([index + 1]) * 10)
            os.utime(item, (100 + index, 100 + index))
            managed.append(item)
        owner = backups / "owner-copy.zip"
        owner.write_bytes(b"keep")
        result = prune_retention(
            self.orch, older_than_days=30, max_evidence_bytes=1024,
            keep_recent_runs=20, keep_backups=2, max_backup_bytes=1024,
        )
        self.assertFalse(managed[0].exists())
        self.assertTrue(managed[1].exists())
        self.assertTrue(managed[2].exists())
        self.assertTrue(owner.exists())
        self.assertEqual(result["pruned"]["backup_count"], 1)
        self.assertEqual(Path(result["backups"]["unmanaged"][0]["path"]).resolve(), owner.resolve())

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
