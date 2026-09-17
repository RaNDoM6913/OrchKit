import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from orch.core import Orchestrator
from orch.state import backup_state, check_state, prune_capabilities


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
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["quick_check"], ["ok"])
        self.assertEqual(result["foreign_key_violations"], [])

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
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
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
