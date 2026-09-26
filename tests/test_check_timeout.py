import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orch.core import Orchestrator, validate_task_definition
from orch.project import inspect_project


class CheckTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def task(self, check):
        return {
            "id": "CHECK-1", "goal": "Check timeout",
            "workspace": str(self.repo), "allowed_paths": ["result.txt"],
            "checks": [check],
        }
    def test_detected_python_check_is_admitted_and_executed_with_180_seconds(self):
        checks = inspect_project(self.repo)["detected"]["suggested_checks"]
        check = next(c for c in checks if c["id"] == "python-tests")
        self.assertEqual(check["timeout_sec"], 180)
        validate_task_definition(self.task(check))
        orch = Orchestrator(self.root / "home")
        bound = {**check, "executable_path": sys.executable}
        with mock.patch("orch.core.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = orch._run_check(self.repo, "RUN-1", bound)
        self.assertFalse(result["timed_out"])
        self.assertEqual(run.call_args.kwargs["timeout"], 180)

    def test_cap_is_admitted_but_excess_rejected(self):
        check = {"id": "custom", "argv": ["python3", "-V"], "timeout_sec": 300}
        validate_task_definition(self.task(check))
        check["timeout_sec"] = 301
        with self.assertRaisesRegex(ValueError, "invalid_check_timeout"):
            validate_task_definition(self.task(check))

    def test_user_check_without_timeout_preserves_30_second_default(self):
        check = {"id": "custom", "argv": ["python3", "-V"]}
        validate_task_definition(self.task(check))
        orch = Orchestrator(self.root / "home")
        with mock.patch("orch.core.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            orch._run_check(self.repo, "RUN-2", {**check, "executable_path": sys.executable})
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
