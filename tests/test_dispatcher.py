import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from orch.cli import main as cli_main
from orch.core import Orchestrator
from orch.dispatcher import (read_route_evidence, record_rdc,
                             record_route_evidence, render_dispatcher)
from orch.project import ProjectRegistry


class DispatcherRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"

    def register(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        return ProjectRegistry(self.home).add(
            repo, profile="safe", review_mode="off"
        )["project"]["project_id"]

    def test_route_cli_rejects_unknown_run_before_writing_evidence(self):
        Orchestrator(self.home)
        record_rdc(
            self.home, device_id="device-1", device_name="Mac.test"
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli_main([
                "--root", str(self.home), "route", "record",
                "--run-id", "UNKNOWN-RUN", "--model", "UNKNOWN",
                "--reasoning", "UNKNOWN", "--usage", "UNKNOWN",
                "--source", "operator_observed", "--work-used", "unknown",
                "--codex-execution-used", "unknown", "--model-api-used", "unknown",
                "--external-provider-used", "unknown",
            ])
        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"], "unknown_run")
        self.assertFalse((self.home / "route-evidence").exists())

    def test_route_evidence_requires_rdc_and_does_not_claim_acceptance(self):
        with self.assertRaisesRegex(
            ValueError, "route_evidence_requires_rdc_marker"
        ):
            record_route_evidence(
                self.home,
                run_id="RUN-A1",
                task_id="TASK-A",
                model="UNKNOWN",
                reasoning="UNKNOWN",
                usage="UNKNOWN",
                source="operator_observed",
                work_used="unknown",
                codex_execution_used="unknown",
                model_api_used="unknown",
                external_provider_used="unknown",
            )
        self.assertFalse(
            (self.home / "route-evidence" / "RUN-A1.json").exists()
        )

        record_rdc(
            self.home, device_id="device-1", device_name="Mac.test"
        )
        recorded = record_route_evidence(
            self.home,
            run_id="RUN-A1",
            task_id="TASK-A",
            model="GPT-5.6 Sol",
            reasoning="UNKNOWN",
            usage="UNKNOWN",
            source="operator_observed",
            work_used="no",
            codex_execution_used="no",
            model_api_used="no",
            external_provider_used="no",
        )
        self.assertEqual(recorded["status"], "RECORDED")
        self.assertEqual(recorded["acceptance"], "NOT_EVALUATED")
        self.assertEqual(recorded["route_evidence"]["run_id"], "RUN-A1")
        self.assertEqual(recorded["route_evidence"]["task_id"], "TASK-A")
        self.assertEqual(
            recorded["route_evidence"]["surface"], "ordinary_chat"
        )
        readback = read_route_evidence(self.home, run_id="RUN-A1")
        self.assertEqual(readback["status"], "RECORDED")
        self.assertTrue(readback["rdc_binding_matches"])
        self.assertEqual(readback["acceptance"], "NOT_EVALUATED")
        self.assertEqual(
            stat.S_IMODE(
                (self.home / "route-evidence" / "RUN-A1.json").stat().st_mode
            ),
            0o600,
        )

    def test_route_evidence_fails_closed_on_device_rebinding(self):
        record_rdc(
            self.home, device_id="device-1", device_name="Mac.one"
        )
        record_route_evidence(
            self.home,
            run_id="RUN-B1",
            task_id="TASK-B",
            model="UNKNOWN",
            reasoning="UNKNOWN",
            usage="UNKNOWN",
            source="operator_observed",
            work_used="unknown",
            codex_execution_used="unknown",
            model_api_used="unknown",
            external_provider_used="unknown",
        )
        record_rdc(
            self.home, device_id="device-2", device_name="Mac.two"
        )

        readback = read_route_evidence(self.home, run_id="RUN-B1")
        self.assertEqual(readback["status"], "STALE_DEVICE_BINDING")
        self.assertFalse(readback["rdc_binding_matches"])
        self.assertEqual(readback["acceptance"], "NOT_EVALUATED")

    def test_missing_home_unknown_project_does_not_create_registry(self):
        with self.assertRaisesRegex(ValueError, "unknown_project"):
            render_dispatcher(self.home, project_id="missing")
        self.assertFalse(self.home.exists())

    def test_registry_removed_at_open_boundary_is_not_recreated(self):
        self.register()
        original_open = ProjectRegistry.open_readonly

        def remove_at_open(home):
            shutil.rmtree(self.home)
            return original_open(home)

        with mock.patch.object(
            ProjectRegistry, "open_readonly", side_effect=remove_at_open, create=True
        ):
            with self.assertRaisesRegex(ValueError, "unknown_project"):
                render_dispatcher(self.home, project_id="missing")
        self.assertFalse(self.home.exists())

    def test_dangling_config_symlink_is_rejected(self):
        self.home.mkdir()
        projects = self.home / "projects"
        projects.mkdir(mode=0o700)
        (projects / "missing.json").symlink_to(self.root / "absent.json")
        with self.assertRaisesRegex(ValueError, "project_config_unsafe"):
            render_dispatcher(self.home, project_id="missing")
        self.assertTrue((projects / "missing.json").is_symlink())

    def test_projects_symlink_is_rejected_without_touching_external_config(self):
        self.home.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        config = outside / "missing.json"
        config.write_text('{"project_id":"missing"}', encoding="utf-8")
        (self.home / "projects").symlink_to(outside, target_is_directory=True)
        before = config.read_bytes()
        with self.assertRaisesRegex(ValueError, "private_directory_unsafe"):
            render_dispatcher(self.home, project_id="missing")
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue((self.home / "projects").is_symlink())

    def test_loose_registry_mode_is_rejected_without_chmod(self):
        project_id = self.register()
        projects = self.home / "projects"
        config = projects / (project_id + ".json")
        before = config.read_bytes()
        for mode in (0o755, 0o777):
            with self.subTest(mode=oct(mode)):
                os.chmod(projects, mode)
                with self.assertRaisesRegex(ValueError, "private_directory_unsafe"):
                    render_dispatcher(self.home, project_id=project_id)
                self.assertEqual(stat.S_IMODE(projects.stat().st_mode), mode)
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse((self.home / "dispatchers").exists())

    def test_registered_project_renders(self):
        project_id = self.register()
        result = render_dispatcher(self.home, project_id=project_id)
        self.assertEqual(result["status"], "RENDERED")
        self.assertEqual(result["project_id"], project_id)
        self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
