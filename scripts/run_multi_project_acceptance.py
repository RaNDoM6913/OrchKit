#!/usr/bin/env python3
"""Run the disposable multi-project acceptance suite with bounded JSON evidence."""

import argparse
from datetime import datetime, timezone
import importlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUITE_FILE = "test_multi_project_acceptance.py"
CASE_NAME = re.compile(r"test_[A-Za-z0-9_]+\Z")
STATUSES = {"PASS", "FAIL", "ERROR", "SKIPPED"}


def _diagnostic(error):
    """Return an exception category and relative source line, never exception text."""
    kind = error[0].__name__
    location = None
    for frame in reversed(traceback.extract_tb(error[2])):
        if Path(frame.filename).name == SUITE_FILE:
            location = "tests/{}:{}".format(SUITE_FILE, frame.lineno)
            break
    return {"error_type": kind, "location": location}


class AcceptanceResult(unittest.TextTestResult):
    """Collect outcomes through unittest callbacks rather than parsing its output."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries = {}

    def startTest(self, test):
        super().startTest(test)
        self.entries[id(test)] = {"test": test.id(), "status": "INCOMPLETE"}

    def _record(self, test, status, error=None):
        entry = self.entries.setdefault(id(test), {"test": test.id()})
        entry["status"] = status
        if error is not None:
            entry["diagnostic"] = _diagnostic(error)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "FAIL", err)

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "ERROR", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "SKIPPED")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "SKIPPED")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "FAIL")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            status = "FAIL" if issubclass(err[0], AssertionError) else "ERROR"
            self._record(test, status, err)


def _worker(case, destination):
    """Run only the checked-in suite in the isolated runner subprocess."""
    if os.environ.get("ORCHKIT_ACCEPTANCE_WORKER") != "1":
        return 2
    try:
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "tests"))
        loader = unittest.TestLoader()
        if case:
            module = importlib.import_module("test_multi_project_acceptance")
            suite = loader.loadTestsFromName(
                "MultiProjectAcceptanceTests.{}".format(case), module=module
            )
        else:
            suite = loader.discover(str(ROOT / "tests"), pattern=SUITE_FILE)
        expected = suite.countTestCases()
        runner = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0, buffer=True,
            resultclass=AcceptanceResult,
        )
        result = runner.run(suite)
        entries = list(result.entries.values())
        counts = {key: sum(row["status"] == key for row in entries)
                  for key in ("PASS", "FAIL", "ERROR", "SKIPPED")}
        complete = (
            result.testsRun == expected == len(entries)
            and all(row["status"] in STATUSES for row in entries)
            and sum(counts.values()) == result.testsRun
            and not loader.errors
        )
        if counts["ERROR"]:
            status = "ERROR"
        elif not complete or counts["SKIPPED"]:
            status = "INCOMPLETE"
        elif counts["FAIL"]:
            status = "FAIL"
        elif result.testsRun > 0 and result.wasSuccessful():
            status = "PASS"
        else:
            status = "ERROR"
        payload = {
            "status": status, "expected_tests": expected,
            "tests_run": result.testsRun, "passed": counts["PASS"],
            "failed": counts["FAIL"], "errors": counts["ERROR"],
            "skipped": counts["SKIPPED"], "test_results": entries,
        }
    except Exception as exc:
        payload = {
            "status": "ERROR", "expected_tests": None,
            "tests_run": None, "passed": None, "failed": None,
            "errors": None, "skipped": None, "test_results": [],
            "reason": "worker_exception", "error_type": type(exc).__name__,
        }
    destination.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return 0 if payload["status"] == "PASS" else 1


def _source_commit():
    try:
        run = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        commit = run.stdout.strip()
        return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_owned_group(pgid, action):
    try:
        os.killpg(pgid, action)
    except (ProcessLookupError, PermissionError):
        # No permission or no process is not proof that a group finished.
        pass


def _stop_owned_group(process):
    """Signal only the new session/process group created by this invocation."""
    pgid = process.pid
    if process.poll() is None:
        try:
            if os.getpgid(pgid) != pgid:
                return False
        except ProcessLookupError:
            pass
    if _group_exists(pgid):
        _signal_owned_group(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.monotonic() + 2
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_exists(pgid):
        _signal_owned_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.monotonic() + 2
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _group_exists(pgid)


def _validate_worker(data, returncode):
    """Fail closed on absent or contradictory worker accounting."""
    if not isinstance(data, dict):
        return "ERROR", "missing_or_invalid_worker_report"
    status = data.get("status")
    values = [data.get(key) for key in
              ("expected_tests", "tests_run", "passed", "failed", "errors", "skipped")]
    if status == "ERROR" and data.get("reason") == "worker_exception":
        return "ERROR", "worker_exception"
    if any(type(value) is not int or value < 0 for value in values):
        return "ERROR", "invalid_worker_counts"
    expected, total, passed, failed, errors, skipped = values
    entries = data.get("test_results")
    if (not isinstance(entries, list) or expected < 1 or total != expected
            or len(entries) != total or sum(values[2:]) != total):
        return "ERROR", "incomplete_worker_accounting"
    if (any(not isinstance(item, dict) or not isinstance(item.get("test"), str)
            or item.get("status") not in STATUSES for item in entries)
            or len({item["test"] for item in entries}) != total):
        return "ERROR", "invalid_test_results"
    for label, count in (("PASS", passed), ("FAIL", failed),
                         ("ERROR", errors), ("SKIPPED", skipped)):
        if sum(item["status"] == label for item in entries) != count:
            return "ERROR", "mismatched_test_counts"
    if status == "PASS" and passed == total and returncode == 0:
        return "PASS", None
    if status == "FAIL" and failed > 0 and errors == skipped == 0 and returncode == 1:
        return "FAIL", None
    if status == "ERROR" and errors > 0 and returncode == 1:
        return "ERROR", None
    if status == "INCOMPLETE" and skipped > 0 and returncode == 1:
        return "INCOMPLETE", "skipped_or_incomplete_tests"
    return "ERROR", "inconsistent_worker_result"


def _write_report(destination, payload):
    """Create the requested report exclusively, never overwrite existing files."""
    destination = Path(destination)
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _run(args):
    started = datetime.now(timezone.utc).isoformat()
    begun = time.monotonic()
    commit = _source_commit()
    worker_data = None
    reason = None
    status = "INCOMPLETE"
    exit_code = 2
    cleanup = "INCOMPLETE"
    temporary = Path(tempfile.mkdtemp(prefix="orch-p1-a3-run-"))
    child = None
    try:
        env = dict(os.environ)
        env.update({
            "ORCH_HOME": str(temporary / "isolated-orch-home"),
            "TMPDIR": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "ORCHKIT_ACCEPTANCE_WORKER": "1",
        })
        for name in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        output = temporary / "worker-results.json"
        command = [
            sys.executable, "-B", str(Path(__file__).resolve()),
            "--internal-worker", "--worker-output", str(output),
        ]
        if args.case:
            command.extend(["--case", args.case])
        child = subprocess.Popen(
            command, cwd=str(ROOT), env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            child.wait(timeout=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            reason = "deadline_exceeded"
            exit_code = 124
            _stop_owned_group(child)
        else:
            if output.is_file():
                try:
                    worker_data = json.loads(output.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    reason = "invalid_worker_report"
            status, validation_reason = _validate_worker(worker_data, child.returncode)
            reason = reason or validation_reason
            if validation_reason and validation_reason not in (
                    "skipped_or_incomplete_tests", "worker_exception"):
                worker_data = None  # Do not publish malformed child evidence.
            exit_code = 0 if status == "PASS" else 1
    except (OSError, subprocess.SubprocessError):
        status, reason, exit_code = "ERROR", "runner_process_error", 2
    finally:
        if child is not None and child.poll() is None:
            _stop_owned_group(child)
        if child is None or not _group_exists(child.pid):
            try:
                shutil.rmtree(temporary)
                if not temporary.exists():
                    cleanup = "COMPLETE"
            except OSError:
                pass
        if cleanup != "COMPLETE":
            status, reason, exit_code = "INCOMPLETE", "cleanup_unconfirmed", 3
        if commit is None:
            status, reason, exit_code = "ERROR", "source_commit_unavailable", 2

    payload = {
        "schema_version": 1,
        "source_commit": commit,
        "python_version": platform.python_version(),
        "platform": "{}-{}".format(platform.system(), platform.machine()),
        "started_at": started,
        "duration_seconds": round(time.monotonic() - begun, 3),
        "status": status,
        "tests_run": worker_data.get("tests_run") if isinstance(worker_data, dict) else None,
        "passed": worker_data.get("passed") if isinstance(worker_data, dict) else None,
        "failed": worker_data.get("failed") if isinstance(worker_data, dict) else None,
        "errors": worker_data.get("errors") if isinstance(worker_data, dict) else None,
        "skipped": worker_data.get("skipped") if isinstance(worker_data, dict) else None,
        "test_results": worker_data.get("test_results", []) if isinstance(worker_data, dict) else [],
        "exit_code": exit_code, "cleanup_status": cleanup,
    }
    if reason:
        payload["reason"] = reason
    if args.json_report:
        try:
            _write_report(args.json_report, payload)
        except OSError:
            print("ERROR: could not create JSON report (existing files are never overwritten)",
                  file=sys.stderr)
            return 2
    print(json.dumps({key: payload[key] for key in (
        "status", "tests_run", "passed", "failed", "errors", "skipped",
        "exit_code", "cleanup_status",
    )}, sort_keys=True))
    if reason:
        print("Reason: {}".format(reason), file=sys.stderr)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-report", type=Path, help="New JSON report path (never overwritten)")
    parser.add_argument("--timeout-seconds", type=float, default=120,
                        help="Overall suite deadline in seconds (default: 120)")
    parser.add_argument("--case", help="One test_* method (for focused diagnosis)")
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case and not CASE_NAME.fullmatch(args.case):
        parser.error("--case must be a test_* method name")
    if not 0 < args.timeout_seconds <= 900:
        parser.error("--timeout-seconds must be >0 and <=900")
    if args.internal_worker:
        if args.worker_output is None:
            return 2
        return _worker(args.case, args.worker_output)
    if args.worker_output is not None:
        parser.error("--worker-output is internal")
    if os.name != "posix":
        parser.error("bounded process-group cleanup requires POSIX")
    if args.json_report and args.json_report.exists():
        parser.error("JSON report already exists; choose a fresh path")
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
