"""Fake-only acceptance for the future worker-operation contract boundary."""

import ast
import dataclasses
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orch.worker_operations import (
    FILE_OPERATION_KINDS,
    MUTATING_OPERATION_KINDS,
    PROCESS_OPERATION_KINDS,
    WORKER_MAX_OUTPUT_BYTES,
    WorkerFileOperations,
    WorkerOperationOutcome,
    WorkerOperationRequest,
    WorkerProcessOperations,
    WorkerRunBinding,
    WorkerTransport,
    require_matching_worker_run,
    requires_independent_readback,
    validate_worker_outcome,
)


def scope(**changes):
    values = dict(
        project_id="orchkit-development-0d58cf03",
        writer_key="git:6879013cd0f64dcc151446999654bba4",
        task_id="P1-B4",
        run_id="P1-B4-A1-fake",
        attempt=1,
    )
    values.update(changes)
    return WorkerRunBinding(**values)


def request(kind="file.read", target="docs/example.md", **changes):
    values = dict(
        binding=scope(),
        operation_id="OP-A1-001",
        kind=kind,
        target=target,
        max_output_bytes=128,
    )
    values.update(changes)
    return WorkerOperationRequest(**values)


def outcome(operation_id="OP-A1-001", state="SUCCEEDED", **changes):
    values = dict(operation_id=operation_id, state=state)
    if state != "SUCCEEDED":
        values["error_code"] = "external_status"
    values.update(changes)
    return WorkerOperationOutcome(**values)


class FakeFiles:
    """In-memory only: no local workspace access or authority is granted."""

    def __init__(self, expected):
        self.expected = expected
        self.calls = []

    def _invoke(self, req, expected_kind):
        require_matching_worker_run(self.expected, req)
        if req.kind != expected_kind:
            raise ValueError("fake_wrong_kind")
        self.calls.append(req.operation_id)
        if req.target == "pending.txt":
            return outcome(req.operation_id, "UNKNOWN", error_code="lost_response")
        if req.target == "denied.txt":
            return outcome(req.operation_id, "REFUSED", error_code="scope_denied")
        if req.target == "failed.txt":
            return outcome(req.operation_id, "FAILED", error_code="io_failed")
        return outcome(req.operation_id, output=b"fixture")

    def read(self, req):
        return self._invoke(req, "file.read")

    def list(self, req):
        return self._invoke(req, "file.list")

    def patch(self, req):
        return self._invoke(req, "file.patch")


class FakeProcesses:
    """No PIDs, child processes, shell calls or termination side effects."""

    def __init__(self, expected):
        self.expected = expected
        self.calls = []

    def _invoke(self, req, expected_kind):
        require_matching_worker_run(self.expected, req)
        if req.kind != expected_kind:
            raise ValueError("fake_wrong_kind")
        self.calls.append(req.operation_id)
        if req.target == "missing-state":
            return outcome(req.operation_id, "UNKNOWN",
                           error_code="process_state_unknown")
        if req.target == "foreign-handle":
            return outcome(req.operation_id, "REFUSED",
                           error_code="process_not_owned")
        return outcome(req.operation_id, output=b"fake-ack")

    def start(self, req):
        return self._invoke(req, "process.start")

    def poll(self, req):
        return self._invoke(req, "process.poll")

    def terminate(self, req):
        return self._invoke(req, "process.terminate")


class FakeWorkerTransport:
    def __init__(self, expected):
        self.files = FakeFiles(expected)
        self.processes = FakeProcesses(expected)


class WorkerOperationContractTests(unittest.TestCase):
    def test_in_memory_protocols_describe_six_distinct_operations(self):
        self.assertEqual(
            FILE_OPERATION_KINDS,
            {"file.read", "file.list", "file.patch"},
        )
        self.assertEqual(
            PROCESS_OPERATION_KINDS,
            {"process.start", "process.poll", "process.terminate"},
        )
        self.assertEqual(
            MUTATING_OPERATION_KINDS,
            {"file.patch", "process.start", "process.terminate"},
        )
        transport = FakeWorkerTransport(scope())
        self.assertIsInstance(transport.files, FakeFiles)
        self.assertIsInstance(transport.processes, FakeProcesses)
        self.assertTrue(hasattr(WorkerTransport, "__annotations__"))
        self.assertTrue(callable(getattr(WorkerFileOperations, "read")))
        self.assertTrue(callable(getattr(WorkerProcessOperations, "start")))

    def test_successful_fake_file_and_process_observations_are_bounded(self):
        adapter = FakeWorkerTransport(scope())
        fixtures = (
            (adapter.files.read, "file.read", "docs/example.md"),
            (adapter.files.list, "file.list", "docs"),
            (adapter.files.patch, "file.patch", "docs/example.md"),
            (adapter.processes.start, "process.start", "registered-profile"),
            (adapter.processes.poll, "process.poll", "owned-handle-1"),
            (adapter.processes.terminate, "process.terminate", "owned-handle-1"),
        )
        for index, (operation, kind, target) in enumerate(fixtures):
            with self.subTest(kind=kind):
                req = request(kind, target, operation_id="OP-A1-%03d" % index)
                result = validate_worker_outcome(req, operation(req))
                self.assertEqual(result.state, "SUCCEEDED")
                self.assertFalse(requires_independent_readback(req, result))
                self.assertTrue(len(result.output) <= req.max_output_bytes)
        self.assertEqual(len(adapter.files.calls), 3)
        self.assertEqual(len(adapter.processes.calls), 3)

    def test_unknown_result_never_becomes_success_or_auto_retry(self):
        adapter = FakeWorkerTransport(scope())
        for operation, req in (
            (adapter.files.patch, request("file.patch", "pending.txt")),
            (adapter.processes.poll,
             request("process.poll", "missing-state", operation_id="poll-01")),
            (adapter.processes.terminate,
             request("process.terminate", "missing-state",
                     operation_id="terminate-01")),
        ):
            with self.subTest(kind=req.kind):
                result = validate_worker_outcome(req, operation(req))
                self.assertEqual(result.state, "UNKNOWN")
                self.assertTrue(requires_independent_readback(req, result))
                self.assertEqual(result.output, b"")
        self.assertEqual(adapter.files.calls, ["OP-A1-001"])
        self.assertEqual(adapter.processes.calls, ["poll-01", "terminate-01"])

    def test_explicit_refusal_and_failure_never_become_success(self):
        adapter = FakeWorkerTransport(scope())
        for target, expected in (
            ("denied.txt", "REFUSED"),
            ("failed.txt", "FAILED"),
        ):
            req = request(target=target)
            result = validate_worker_outcome(req, adapter.files.read(req))
            self.assertEqual(result.state, expected)
            self.assertIsNotNone(result.error_code)
            self.assertFalse(requires_independent_readback(req, result))
        result = adapter.processes.poll(request(
            "process.poll", "foreign-handle"
        ))
        self.assertEqual(result.state, "REFUSED")
        self.assertEqual(result.error_code, "process_not_owned")

    def test_identity_matching_rejects_each_cross_scope_dimension(self):
        adapter = FakeWorkerTransport(scope())
        changes = (
            {"project_id": "another-project"},
            {"writer_key": "git:other"},
            {"task_id": "P1-B3"},
            {"run_id": "P1-B4-A2-foreign"},
            {"attempt": 2},
        )
        for change in changes:
            with self.subTest(change=change):
                req = request(binding=scope(**change))
                with self.assertRaisesRegex(
                    ValueError, "^worker_operation_scope_mismatch$"
                ):
                    adapter.files.read(req)
        self.assertEqual(adapter.files.calls, [])

    def test_run_identity_and_attempt_are_strictly_validated(self):
        for change, reason in (
            ({"project_id": "UPPER"}, "project_id"),
            ({"project_id": ""}, "project_id"),
            ({"writer_key": "git:\nno"}, "writer_key"),
            ({"task_id": "bad/task"}, "task_id"),
            ({"run_id": ""}, "run_id"),
            ({"run_id": "r" * 201}, "run_id"),
            ({"attempt": False}, "attempt"),
            ({"attempt": 0}, "attempt"),
            ({"attempt": 1000001}, "attempt"),
            ({"attempt": "1"}, "attempt"),
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(
                    ValueError, "^worker_operation_invalid_" + reason + "$"
                ):
                    scope(**change)

    def test_unknown_request_schema_and_kind_are_refused(self):
        for change, reason in (
            ({"schema_version": 2}, "invalid_schema"),
            ({"schema_version": True}, "invalid_schema"),
            ({"schema_version": "1"}, "invalid_schema"),
            ({"binding": "not-bound"}, "invalid_binding"),
            ({"operation_id": "bad/id"}, "invalid_operation_id"),
            ({"operation_id": ""}, "invalid_operation_id"),
            ({"kind": "git.push"}, "invalid_kind"),
            ({"kind": "check.run"}, "invalid_kind"),
            ({"kind": None}, "invalid_kind"),
            ({"max_output_bytes": 0}, "invalid_output_limit"),
            ({"max_output_bytes": True}, "invalid_output_limit"),
            ({"max_output_bytes": WORKER_MAX_OUTPUT_BYTES + 1},
             "invalid_output_limit"),
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(
                    ValueError, "^worker_operation_" + reason + "$"
                ):
                    request(**change)

    def test_unsafe_file_target_syntax_fails_closed(self):
        for target in (
            "", "../outside", "/etc/passwd", "a/../b", "./file",
            "folder//file", "folder/.", "folder/..", "C:/Windows/file",
            "folder\\file", "folder\nfile", "folder\x00file", "x" * 1025,
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(
                    ValueError, "^worker_operation_invalid_target$"
                ):
                    request(target=target)
        self.assertEqual(
            request("file.list", "dir name/reports").target,
            "dir name/reports",
        )

    def test_process_command_text_is_not_a_valid_profile_or_handle(self):
        for kind in PROCESS_OPERATION_KINDS:
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(
                    ValueError, "^worker_operation_invalid_target$"
                ):
                    request(kind, "python -c 'print(1)'")
                with self.assertRaisesRegex(
                    ValueError, "^worker_operation_invalid_target$"
                ):
                    request(kind, "../handle")

    def test_outcome_schema_status_output_and_error_are_strict(self):
        for changes, error in (
            ({"schema_version": 2}, "worker_outcome_invalid_schema"),
            ({"schema_version": True}, "worker_outcome_invalid_schema"),
            ({"state": "MAYBE"}, "worker_outcome_invalid_state"),
            ({"state": "succeeded"}, "worker_outcome_invalid_state"),
            ({"output": "not bytes"}, "worker_outcome_invalid_output"),
            ({"error_code": "reported_failure"}, "worker_outcome_invalid_error"),
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "^" + error + "$"):
                    outcome(**changes)
        with self.assertRaisesRegex(
            ValueError, "^worker_outcome_invalid_output$"
        ):
            outcome(state="UNKNOWN", output=b"partial")
        for bad_code in (None, "", "unknown code", "x" * 129):
            with self.subTest(error_code=bad_code):
                with self.assertRaisesRegex(
                    ValueError, "^worker_outcome_invalid_error$"
                ):
                    WorkerOperationOutcome("op", "FAILED", error_code=bad_code)

    def test_results_are_run_operation_correlated_and_bounded(self):
        req = request(max_output_bytes=4)
        for bad in (outcome(operation_id="another"), "not-an-outcome"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(
                    ValueError, "^worker_outcome_operation_mismatch$"
                ):
                    validate_worker_outcome(req, bad)
        with self.assertRaisesRegex(ValueError, "^worker_outcome_too_large$"):
            validate_worker_outcome(req, outcome(output=b"12345"))
        self.assertEqual(
            validate_worker_outcome(req, outcome(output=b"1234")).output,
            b"1234",
        )

    def test_contract_cannot_call_verifier_publisher_shell_or_filesystem(self):
        # There is intentionally no production WorkerTransport implementation.
        from orch import worker_operations
        tree = ast.parse(
            Path(worker_operations.__file__).read_text(encoding="utf-8")
        )
        imports = {
            name.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for name in node.names
        }
        imports |= {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        self.assertFalse(
            imports & {"subprocess", "os", "pathlib", "core",
                       "git_transport", "dispatcher", "state"}
        )
        fake = FakeWorkerTransport(scope())
        with (mock.patch("subprocess.run",
                         side_effect=AssertionError("no verifier subprocess")),
              mock.patch("os.open",
                         side_effect=AssertionError("no filesystem open"))):
            req = request("process.start", "registered-profile")
            result = fake.processes.start(req)
            self.assertEqual(
                validate_worker_outcome(req, result).state, "SUCCEEDED"
            )


if __name__ == "__main__":
    unittest.main()
