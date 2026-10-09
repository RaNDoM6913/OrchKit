import json
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

    def test_supported_timeouts_are_admitted_and_executed_unchanged(self):
        orch = Orchestrator(self.root / "home")
        for timeout in (1, 30, 180, 300, 301, 599, 600):
            with self.subTest(timeout=timeout):
                check = {
                    "id": "custom", "argv": ["python3", "-V"],
                    "timeout_sec": timeout,
                }
                validate_task_definition(self.task(check))
                bound = {**check, "executable_path": sys.executable}
                with mock.patch(
                    "orch.core.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as run:
                    result = orch._run_check(self.repo, "RUN-BOUND", bound)
                self.assertEqual(run.call_args.kwargs["timeout"], timeout)
                self.assertFalse(result["timed_out"])
                self.assertEqual(result["exit_code"], 0)

    def test_invalid_timeout_values_are_rejected(self):
        for timeout in (601, 1000, 0, -1, True, False, 600.0, "600", None):
            with self.subTest(timeout=timeout):
                check = {
                    "id": "custom", "argv": ["python3", "-V"],
                    "timeout_sec": timeout,
                }
                with self.assertRaisesRegex(ValueError, "invalid_check_timeout"):
                    validate_task_definition(self.task(check))

    def test_executor_retains_defensive_ceiling(self):
        # Admission rejects this value; the executor still has a hard bound.
        orch = Orchestrator(self.root / "home")
        check = {
            "id": "custom", "argv": ["python3", "-V"],
            "timeout_sec": 601, "executable_path": sys.executable,
        }
        with mock.patch(
            "orch.core.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            orch._run_check(self.repo, "RUN-CLAMP", check)
        self.assertEqual(run.call_args.kwargs["timeout"], 600)

    def test_600_second_check_survives_plan_reload_and_executes(self):
        # Claim requires a clean baseline, even for a local-only fixture.
        git = [
            "git", "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "user.name=Timeout Fixture",
            "-c", "user.email=fixture@example.invalid", "-C", str(self.repo),
        ]
        for args in (
            ["add", "--", "pyproject.toml"],
            ["commit", "-q", "--no-gpg-sign", "-m", "fixture baseline"],
        ):
            subprocess.run(
                git + args, check=True, capture_output=True, timeout=10,
            )
        check = {
            "id": "custom", "argv": [sys.executable, "-V"], "cwd": ".",
            "timeout_sec": 600, "output_tail_chars": 32,
            "timeout_action": "needs_fix",
        }
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({
            "schema_version": 1, "plan_revision": "timeout-600-v1",
            "tasks": [self.task(check)],
        }), encoding="utf-8")
        home = self.root / "home"
        self.assertEqual(Orchestrator(home).load_plan(plan)["status"], "OK")

        reopened = Orchestrator(home)
        claim = reopened.claim("timeout-fixture")
        self.assertEqual(claim["status"], "CLAIMED", claim.get("reason"))
        bound = claim["context"]["checks"][0]
        for key, value in check.items():
            self.assertEqual(bound[key], value)
        self.assertTrue(Path(bound["executable_path"]).is_absolute())
        self.assertEqual(len(bound["executable_sha256"]), 64)
        with mock.patch(
            "orch.core.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            result = reopened._run_check(self.repo, claim["run_id"], bound)
        self.assertEqual(run.call_args.kwargs["timeout"], 600)
        self.assertEqual(
            run.call_args.args[0], [bound["executable_path"], "-V"]
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])

    def test_600_second_timeout_preserves_failure_and_output_contract(self):
        orch = Orchestrator(self.root / "home")
        check = {
            "id": "custom", "argv": ["python3", "-V"],
            "timeout_sec": 600, "output_tail_chars": 4,
            "timeout_action": "needs_fix", "executable_path": sys.executable,
        }
        # Simulate the deadline; no slow or uncontrolled child is launched.
        with mock.patch(
            "orch.core.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                [sys.executable, "-V"], 600,
                output="prefix-tail", stderr="prefix-fail",
            ),
        ) as run:
            result = orch._run_check(self.repo, "RUN-TIMEOUT", check)
        self.assertEqual(run.call_args.kwargs["timeout"], 600)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["timeout_action"], "needs_fix")
        self.assertEqual(result["stdout"], "tail")
        self.assertEqual(result["stderr"], "fail")
        saved = json.loads(Path(result["log_path"]).read_text(encoding="utf-8"))
        self.assertTrue(saved["timed_out"])
        self.assertIsNone(saved["exit_code"])
        self.assertEqual(saved["output_tail_chars"], 4)

    def test_user_check_without_timeout_preserves_30_second_default(self):
        check = {"id": "custom", "argv": ["python3", "-V"]}
        validate_task_definition(self.task(check))
        orch = Orchestrator(self.root / "home")
        with mock.patch("orch.core.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            orch._run_check(self.repo, "RUN-2", {**check, "executable_path": sys.executable})
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
