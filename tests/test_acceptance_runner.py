"""Fail-closed checks for the disposable multi-project acceptance runner."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import run_multi_project_acceptance as runner


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_multi_project_acceptance.py"


class AcceptanceRunnerTests(unittest.TestCase):
    def _invoke(self, *options):
        with tempfile.TemporaryDirectory(prefix="orch-p1-a3-test-") as temp:
            base = Path(temp)
            report = base / "acceptance.json"
            env = dict(os.environ)
            env.update({
                "TMPDIR": str(base),
                "ORCH_HOME": str(base / "untouched-parent-home"),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT),
                 "--json-report", str(report), *options],
                cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=40,
            )
            self.assertTrue(report.is_file(), result.stderr)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse((base / "untouched-parent-home").exists())
            self.assertEqual(list(base.glob("orch-p1-a3-run-*")), [])
            self.assertNotIn("/Users/", json.dumps(data))
            self.assertEqual(data["exit_code"], result.returncode)
            self.assertEqual(data["cleanup_status"], "COMPLETE")
            self.assertEqual(data["schema_version"], 1)
            return result, data

    def test_success_report_is_complete_and_scoped(self):
        run, data = self._invoke(
            "--case", "test_retried_attempt_rejects_stale_capability_and_receipt"
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(data["status"], "PASS")
        self.assertEqual(
            (data["tests_run"], data["passed"], data["failed"],
             data["errors"], data["skipped"]),
            (1, 1, 0, 0, 0),
        )
        self.assertEqual(len(data["test_results"]), 1)
        self.assertEqual(data["test_results"][0]["status"], "PASS")
        self.assertRegex(data["source_commit"], r"^[0-9a-f]{40}$")

    def test_controlled_missing_case_reports_error_and_nonzero_exit(self):
        run, data = self._invoke("--case", "test_nonexistent_acceptance_case")
        self.assertEqual(run.returncode, 1)
        self.assertEqual(data["status"], "ERROR")
        self.assertEqual(data["tests_run"], 1)
        self.assertEqual((data["passed"], data["failed"],
                          data["errors"], data["skipped"]), (0, 0, 1, 0))
        self.assertEqual(len(data["test_results"]), 1)
        self.assertEqual(data["test_results"][0]["status"], "ERROR")
        self.assertEqual(
            data["test_results"][0]["diagnostic"]["error_type"], "AttributeError"
        )

    def test_deadline_does_not_invent_completed_tests(self):
        run, data = self._invoke(
            "--case", "test_three_scoped_claims_race_and_survive_reopen",
            "--timeout-seconds", "0.001",
        )
        self.assertEqual(run.returncode, 124, run.stderr)
        self.assertEqual(data["status"], "INCOMPLETE")
        self.assertEqual(data["reason"], "deadline_exceeded")
        self.assertIsNone(data["tests_run"])
        self.assertEqual(data["test_results"], [])

    def test_existing_report_is_never_replaced(self):
        with tempfile.TemporaryDirectory(prefix="orch-p1-a3-existing-") as temp:
            path = Path(temp) / "report.json"
            path.write_text("keep this file\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT),
                 "--json-report", str(path), "--case", "test_dummy"],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep this file\n")

    def test_validated_pass_requires_consistent_totals_and_exit(self):
        good = {
            "status": "PASS", "expected_tests": 1, "tests_run": 1,
            "passed": 1, "failed": 0, "errors": 0, "skipped": 0,
            "test_results": [{"test": "test_example", "status": "PASS"}],
        }
        self.assertEqual(runner._validate_worker(good, 0), ("PASS", None))
        for invalid, exit_code in (
            (dict(good, passed=0), 0),
            (dict(good, tests_run=0), 0),
            (dict(good, test_results=[]), 0),
            (good, 1),
            (None, 0),
        ):
            self.assertNotEqual(runner._validate_worker(invalid, exit_code)[0],
                                "PASS")

    def test_failure_skip_and_diagnostics_are_not_promoted_to_pass(self):
        example = {"test": "example.case", "status": "FAIL"}
        data = {
            "status": "FAIL", "expected_tests": 1, "tests_run": 1,
            "passed": 0, "failed": 1, "errors": 0, "skipped": 0,
            "test_results": [example],
        }
        self.assertEqual(runner._validate_worker(data, 1), ("FAIL", None))
        example["status"] = "SKIPPED"
        data.update(status="INCOMPLETE", failed=0, skipped=1)
        self.assertEqual(runner._validate_worker(data, 1)[0], "INCOMPLETE")
        try:
            raise RuntimeError("private-token-value")
        except RuntimeError:
            diagnostic = runner._diagnostic(sys.exc_info())
        self.assertEqual(diagnostic["error_type"], "RuntimeError")
        self.assertNotIn("private-token-value", json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()
