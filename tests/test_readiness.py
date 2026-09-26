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
from orch.dispatcher import RDC_MARKER_MAX_BYTES, record_rdc, render_dispatcher
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

    def test_schema_versions_fail_closed_for_selected_and_foreign_projects(self):
        registry = ProjectRegistry(self.home)
        project_a = self.config["project_id"]
        repo_b = self.base / "repo-b"
        subprocess.run(["git", "clone", str(self.repo), str(repo_b)],
                       check=True, capture_output=True)
        registered = registry.add(repo_b, profile="standard", review_mode="off")
        project_b = registered["project"]["project_id"]
        path = Path(registered["config_path"])
        original = path.read_bytes()
        self.assertEqual(audit_project(self.home, project_a)["status"], "READY")
        self.assertEqual(audit_project(self.home, project_b)["status"], "READY")
        for version in (2, True, "1", None, [], {}):
            with self.subTest(version=version):
                config = json.loads(original)
                config["schema_version"] = version
                path.write_text(json.dumps(config) + "\n", encoding="utf-8")
                self.assertEqual(audit_project(self.home, project_b)["status"], "BLOCKED")
                foreign = audit_project(self.home, project_a)
                self.assertEqual(foreign["status"], "BLOCKED")
                self.assertIn(project_b, str(self._check(foreign, "registry")["detail"]))
        config = json.loads(original)
        del config["schema_version"]
        path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        self.assertEqual(audit_project(self.home, project_b)["status"], "BLOCKED")
        self.assertEqual(audit_project(self.home, project_a)["status"], "BLOCKED")
        self.assertEqual(path.read_text(encoding="utf-8"), json.dumps(config) + "\n")

    def test_selected_registry_identity_mismatch_preserves_error(self):
        project_id = self.config["project_id"]
        path = self.home / "projects" / f"{project_id}.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["project_id"] = "wrong-project"
        path.write_text(json.dumps(config) + "\n", encoding="utf-8")

        result = audit_project(self.home, project_id)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            self._check(result, "registry")["detail"],
            "project_config_identity_mismatch",
        )

    def test_foreign_registry_identity_mismatch_blocks_selected_project(self):
        project_id = self.config["project_id"]
        path = self.home / "projects" / "other-project.json"
        config = dict(self.config)
        config["project_id"] = "wrong-project"
        config["root"] = str(self.base / "other-repo")
        path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

        result = audit_project(self.home, project_id)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            self._check(result, "registry")["detail"],
            "project_registry_invalid:other-project",
        )

    def test_foreign_relative_root_blocks_audit_but_selected_keeps_reason(self):
        project_id = self.config["project_id"]
        other = self.base / "other-repo"
        subprocess.run(["git", "clone", str(self.repo), str(other)],
                       check=True, capture_output=True)
        registered = ProjectRegistry(self.home).add(
            other, profile="standard", review_mode="off"
        )
        other_id = registered["project"]["project_id"]
        path = Path(registered["config_path"])
        config = json.loads(path.read_text(encoding="utf-8"))
        config["root"] = "relative-repo-b"
        path.write_text(json.dumps(config) + "\n", encoding="utf-8")

        foreign = audit_project(self.home, project_id)
        self.assertEqual(foreign["status"], "BLOCKED")
        self.assertEqual(self._check(foreign, "registry")["detail"],
                         f"project_registry_invalid:{other_id}")
        selected = audit_project(self.home, other_id)
        self.assertEqual(selected["status"], "BLOCKED")
        self.assertEqual(self._check(selected, "registry")["detail"],
                         "project_root_not_absolute")

    def test_foreign_project_mode_blocks_audit_without_repair(self):
        project_a = self.config["project_id"]
        repo_b = self.base / "repo-b"
        subprocess.run(["git", "clone", str(self.repo), str(repo_b)],
                       check=True, capture_output=True)
        registered = ProjectRegistry(self.home).add(
            repo_b, profile="standard", review_mode="off"
        )
        project_b = registered["project"]["project_id"]
        config_b = Path(registered["config_path"])
        self.assertEqual(audit_project(self.home, project_a)["status"], "READY")
        self.assertEqual(audit_project(self.home, project_b)["status"], "READY")

        os.chmod(config_b, 0o644)
        foreign = audit_project(self.home, project_a)
        selected = audit_project(self.home, project_b)

        self.assertEqual(foreign["status"], "BLOCKED")
        self.assertEqual(self._check(foreign, "registry")["detail"],
                         f"project_registry_permission:{project_b}")
        self.assertEqual(selected["status"], "BLOCKED")
        permission = self._check(selected, "project_config_permission")
        self.assertEqual(permission["detail"]["path"], str(config_b))
        self.assertEqual(permission["detail"]["actual_mode"], "0o644")
        self.assertEqual(config_b.stat().st_mode & 0o777, 0o644)

    def test_audit_blocks_regular_file_substituted_for_logs_directory(self):
        logs = self.home / ".runtime" / "logs"
        logs.rmdir()
        logs.write_text("wrong object type\n", encoding="utf-8")
        os.chmod(logs, 0o700)

        result = audit_project(self.home, self.config["project_id"])

        self.assertEqual(result["status"], "BLOCKED")
        finding = next(
            item for item in result["checks"]
            if item["id"] == "state_permission"
            and item["detail"]["path"] == str(logs.resolve())
        )
        self.assertEqual(finding["detail"]["reason"], "not_directory")

    def test_audit_labels_dangling_logs_symlink_as_unsafe(self):
        logs = self.home / ".runtime" / "logs"
        logs.rmdir()
        missing = self.base / "missing-logs"
        logs.symlink_to(missing, target_is_directory=True)

        result = audit_project(self.home, self.config["project_id"])

        self.assertEqual(result["status"], "BLOCKED")
        finding = next(
            item for item in result["checks"]
            if item["id"] == "state_permission"
            and item["detail"]["path"] == str(self.orch.runtime / "logs")
        )
        self.assertEqual(finding["detail"]["reason"], "unsafe_symlink")
        self.assertFalse(missing.exists())

    def test_audit_blocks_loose_sqlite_sidecar_permissions(self):
        conn = self.orch.connect()
        wal = self.orch.db_path.with_name(self.orch.db_path.name + "-wal")
        shm = self.orch.db_path.with_name(self.orch.db_path.name + "-shm")
        try:
            self.assertTrue(wal.is_file())
            self.assertTrue(shm.is_file())
            os.chmod(wal, 0o644)
            os.chmod(shm, 0o644)

            result = audit_project(self.home, self.config["project_id"])

            self.assertEqual(result["status"], "BLOCKED")
            permissions = {
                item["detail"]["path"]: item["detail"]
                for item in result["checks"]
                if item["id"] == "state_permission"
            }
            for path in (wal, shm):
                self.assertEqual(
                    permissions[str(path)]["reason"], "mode_mismatch"
                )
                self.assertEqual(
                    permissions[str(path)]["actual_mode"], "0o644"
                )
        finally:
            conn.close()

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

    def test_audit_reads_ledger_in_home_with_uri_query_character(self):
        special_home = self.base / "audit-ledger?owner"
        Orchestrator(special_home)
        config = ProjectRegistry(special_home).add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        record_rdc(
            special_home, device_id="fixture-device", device_name="Fixture Mac"
        )
        stray_database = self.base / "audit-ledger"
        self.assertFalse(stray_database.exists())

        result = audit_project(special_home, config["project_id"])

        self.assertFalse(stray_database.exists())
        self.assertEqual(result["status"], "READY")
        self.assertEqual(self._check(result, "state_ledger")["status"], "PASS")

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

    def test_foreign_root_digest_blocks_registry_and_other_project(self):
        registry = ProjectRegistry(self.home)
        repo_b = self.base / "repo-b"
        repo_c = self.base / "repo-c"
        repo_d = self.base / "repo-d"
        subprocess.run(["git", "clone", str(self.repo), str(repo_b)],
                       check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.repo), str(repo_c)],
                       check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.repo), str(repo_d)],
                       check=True, capture_output=True)
        registered = registry.add(repo_b, profile="standard", review_mode="off")
        project_b = registered["project"]["project_id"]
        self.assertEqual(audit_project(self.home, project_b)["status"], "READY")
        path = Path(registered["config_path"])
        config = json.loads(path.read_text(encoding="utf-8"))
        config["root"] = str(repo_c.resolve())
        path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

        with self.assertRaises(ValueError):
            registry.get(project_b)
        rows = {item["project_id"]: item for item in registry.list()}
        self.assertEqual(rows[project_b]["status"], "UNSAFE")
        with self.assertRaises(ValueError):
            registry.add(repo_d, profile="standard", review_mode="off")
        self.assertEqual(len(list(registry.projects_dir.glob("*.json"))), 2)
        selected = audit_project(self.home, project_b)
        self.assertEqual(self._check(selected, "project_root_identity")["status"], "BLOCKED")
        other = audit_project(self.home, self.config["project_id"])
        self.assertEqual(other["status"], "BLOCKED")
        self.assertIn(project_b, str(self._check(other, "registry")["detail"]))

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

    def test_oversized_rdc_marker_blocks_readiness(self):
        rdc_path = self.home / "rdc-bootstrap.json"
        rdc_path.write_bytes(b"{" + b"x" * RDC_MARKER_MAX_BYTES)
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        rdc = self._check(result, "rdc_binding")
        self.assertEqual(rdc["status"], "BLOCKED")
        self.assertEqual(rdc["detail"], "rdc_marker_too_large")

    def test_oversized_dispatcher_blocks_readiness(self):
        dispatchers = self.home / "dispatchers"
        dispatchers.mkdir(mode=0o700)
        path = dispatchers / f"{self.config['project_id']}.txt"
        path.write_bytes(b"x" * (256 * 1024 + 1))
        os.chmod(path, 0o600)
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        dispatcher = self._check(result, "project_dispatcher")
        self.assertEqual(dispatcher["status"], "BLOCKED")
        self.assertEqual(
            dispatcher["detail"]["reason"], "project_dispatcher_too_large"
        )

    def test_oversized_project_config_blocks_readiness(self):
        config_path = (
            self.home / "projects" / f"{self.config['project_id']}.json"
        )
        config_path.write_bytes(b"{" + b"x" * (2 * 1024 * 1024))
        result = audit_project(self.home, self.config["project_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(
            self._check(result, "registry")["detail"],
            f"project_registry_unreadable:{self.config['project_id']}",
        )

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
