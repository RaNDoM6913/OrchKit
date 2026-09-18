import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orch.cli import main as cli_main
from orch.core import Orchestrator
from orch.dispatcher import record_rdc, render_dispatcher
from orch.plan import build_single_task_plan
from orch.project import ProjectRegistry
from orch.readiness import audit_project


class ProjectReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.repo = self.base / "repo"
        self.remote = self.base / "remote.git"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(self.repo)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config",
             "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config",
             "user.name", "Readiness Fixture"],
            check=True,
        )
        (self.repo / "README.md").write_text("readiness fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "README.md"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "initial"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", str(self.remote)],
            check=True,
        )
        self.orch = Orchestrator(self.home)
        self.config = ProjectRegistry(self.home).add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        record_rdc(
            self.home, device_id="fixture-device", device_name="Fixture Mac"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _check(self, result, finding_id):
        return next(item for item in result["checks"] if item["id"] == finding_id)

    def test_clean_registered_project_is_ready_without_optional_dispatcher(self):
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["read_only"])
        self.assertEqual(
            self._check(result, "project_dispatcher")["status"],
            "OPTIONAL_MISSING",
        )
        self.assertEqual(
            self._check(result, "workspace_baseline")["status"], "PASS"
        )
        self.assertEqual(self._check(result, "state_ledger")["status"], "PASS")

    def test_missing_ledger_blocks_without_creating_state(self):
        other_home = self.base / "no-ledger-home"
        registry = ProjectRegistry(other_home)
        config = registry.add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        record_rdc(
            other_home, device_id="fixture-device", device_name="Fixture Mac"
        )
        db = other_home / ".runtime" / "orch.sqlite3"
        self.assertFalse(db.exists())
        result = audit_project(other_home, config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("state_ledger", " ".join(result["blockers"]))
        self.assertFalse(db.exists())
        self.assertFalse((other_home / ".runtime").exists())

    def test_cli_audit_missing_ledger_does_not_initialize_runtime(self):
        other_home = self.base / "cli-no-ledger"
        config = ProjectRegistry(other_home).add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        record_rdc(
            other_home, device_id="fixture-device", device_name="Fixture Mac"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main([
                "--root", str(other_home),
                "project", "audit", config["project_id"],
            ])
        result = json.loads(output.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse((other_home / ".runtime").exists())

    def test_unprotected_workspace_change_blocks_readiness(self):
        (self.repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        baseline = self._check(result, "workspace_baseline")
        self.assertEqual(baseline["status"], "BLOCKED")
        self.assertEqual(
            baseline["detail"]["unprotected_changes"], ["foreign.txt"]
        )

    def test_writer_identity_tamper_blocks_readiness(self):
        config_path = self.home / "projects" / f"{self.config['project_id']}.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["writer_key"] = "git:" + ("0" * 32)
        config_path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        os.chmod(config_path, 0o600)
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            self._check(result, "writer_identity")["status"], "BLOCKED"
        )

    def test_project_root_identity_tamper_blocks_readiness(self):
        other_repo = self.base / "other-repo"
        subprocess.run(
            ["git", "clone", str(self.repo), str(other_repo)],
            check=True, capture_output=True,
        )
        config_path = self.home / "projects" / f"{self.config['project_id']}.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["root"] = str(other_repo.resolve())
        config_path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        os.chmod(config_path, 0o600)
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            self._check(result, "project_root_identity")["status"], "BLOCKED"
        )

    def test_registered_remote_drift_blocks_readiness(self):
        remote_two = self.base / "remote-two.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote_two)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "set-url",
             "origin", str(remote_two)],
            check=True,
        )
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        transport = self._check(result, "publication_transport")
        self.assertEqual(transport["status"], "BLOCKED")
        self.assertIn("remote_url_changed", transport["detail"]["push_blockers"])

    def test_queued_check_support_drift_blocks_readiness_on_clean_worktree(self):
        support = self.repo / "audit_check.py"
        support.write_text("raise SystemExit(0)\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "audit_check.py"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "add audit check"],
            check=True, capture_output=True,
        )
        task = build_single_task_plan(
            self.config,
            task_id="AUDIT-CHECK-SUPPORT",
            goal="bound check support",
            allowed_paths=["result.json"],
        )
        task["tasks"][0]["checks"] = [{
            "id": "support",
            "argv": [sys.executable, "audit_check.py"],
            "cwd": ".",
            "timeout_sec": 5,
        }]
        plan_path = self.home / "audit-check-support.json"
        plan_path.write_text(json.dumps(task), encoding="utf-8")
        self.orch.load_plan(plan_path)

        support.write_text("raise SystemExit(7)\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "audit_check.py"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "mutate audit check"],
            check=True, capture_output=True,
        )
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual(status, "")

        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        authority = self._check(result, "check_execution_authority")
        self.assertEqual(authority["status"], "BLOCKED")
        errors = authority["detail"]["tasks"][0]["errors"]
        self.assertIn(
            "support:authority_file:audit_check.py:HASH_CHANGED", errors
        )

    def test_queued_check_executable_drift_blocks_readiness(self):
        runner = self.repo / "audit-runner.sh"
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "audit-runner.sh"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "add audit runner"],
            check=True, capture_output=True,
        )
        task = build_single_task_plan(
            self.config,
            task_id="AUDIT-CHECK-EXEC",
            goal="bound executable",
            allowed_paths=["result.json"],
        )
        task["tasks"][0]["checks"] = [{
            "id": "runner",
            "argv": ["./audit-runner.sh"],
            "cwd": ".",
            "timeout_sec": 5,
        }]
        plan_path = self.home / "audit-check-exec.json"
        plan_path.write_text(json.dumps(task), encoding="utf-8")
        self.orch.load_plan(plan_path)

        runner.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        runner.chmod(0o755)
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "audit-runner.sh"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "mutate audit runner"],
            check=True, capture_output=True,
        )
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        authority = self._check(result, "check_execution_authority")
        self.assertEqual(authority["status"], "BLOCKED")
        errors = authority["detail"]["tasks"][0]["errors"]
        self.assertIn("runner:executable_hash_changed", errors)

    def test_active_writer_is_attention_not_integrity_block(self):
        plan = build_single_task_plan(
            self.config,
            task_id="AUDIT-ACTIVE",
            goal="readiness active writer fixture",
            allowed_paths=["result.json"],
        )
        plan_path = self.home / "audit-active-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.orch.load_plan(plan_path)
        claim = self.orch.claim("readiness-worker")
        self.assertEqual(claim["status"], "CLAIMED")

        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "ATTENTION")
        ledger = self._check(result, "state_ledger")
        self.assertEqual(ledger["status"], "ATTENTION")
        self.assertEqual(
            ledger["detail"]["writer_locks"][0]["run_id"], claim["run_id"]
        )

    def test_project_pause_is_attention(self):
        plan = build_single_task_plan(
            self.config,
            task_id="AUDIT-PAUSED",
            goal="paused project fixture",
            allowed_paths=["paused.json"],
        )
        plan_path = self.home / "audit-paused.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.orch.load_plan(plan_path)
        self.orch.pause_project(self.config["project_id"], "maintenance")
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "ATTENTION")
        ledger = self._check(result, "state_ledger")
        self.assertEqual(ledger["status"], "ATTENTION")
        self.assertEqual(
            ledger["detail"]["project_pause"]["reason"], "maintenance"
        )

    def test_ledger_writer_identity_drift_is_blocked(self):
        plan = build_single_task_plan(
            self.config,
            task_id="AUDIT-LEDGER-WRITER",
            goal="ledger writer binding fixture",
            allowed_paths=["writer.json"],
        )
        plan_path = self.home / "audit-ledger-writer.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.orch.load_plan(plan_path)
        with self.orch.connect() as conn:
            conn.execute(
                "UPDATE tasks SET writer_key=? WHERE task_id=?",
                ("git:" + ("f" * 32), "AUDIT-LEDGER-WRITER"),
            )
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            self._check(result, "ledger_writer_identity")["status"], "BLOCKED"
        )

    def test_audit_does_not_modify_durable_authority_files(self):
        config_path = self.home / "projects" / f"{self.config['project_id']}.json"
        db_path = self.home / ".runtime" / "orch.sqlite3"
        rdc_path = self.home / "rdc-bootstrap.json"
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (config_path, db_path, rdc_path)
        }
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "READY")
        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (config_path, db_path, rdc_path)
        }
        self.assertEqual(before, after)

    def test_required_scoped_dispatcher_moves_attention_to_ready(self):
        before = audit_project(
            self.home, self.config["project_id"], require_dispatcher=True
        )
        self.assertEqual(before["status"], "ATTENTION")
        self.assertEqual(
            self._check(before, "project_dispatcher")["status"], "ATTENTION"
        )

        rendered = render_dispatcher(
            self.home, project_id=self.config["project_id"]
        )
        self.assertEqual(rendered["scope"], "project")
        after = audit_project(
            self.home, self.config["project_id"], require_dispatcher=True
        )
        self.assertEqual(after["status"], "READY")
        self.assertEqual(
            self._check(after, "project_dispatcher")["status"], "PASS"
        )

    def test_stale_dispatcher_scope_is_blocked(self):
        dispatchers = self.home / "dispatchers"
        dispatchers.mkdir(mode=0o700)
        stale = dispatchers / f"{self.config['project_id']}.txt"
        stale.write_text("global dispatcher\n", encoding="utf-8")
        os.chmod(stale, 0o600)
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            self._check(result, "project_dispatcher")["status"], "BLOCKED"
        )


if __name__ == "__main__":
    unittest.main()
