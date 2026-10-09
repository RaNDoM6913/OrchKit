"""Pure in-memory worker-operation contracts for future Bridge adapters.

These structures perform *syntactic* validation and same-run comparison only.
They do not authenticate a session, verify a capability, grant file/process
access, or execute RDC, registered checks, subprocesses or Git publication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol


WORKER_OPERATION_SCHEMA_VERSION = 1
WORKER_MAX_OUTPUT_BYTES = 64 * 1024
FILE_OPERATION_KINDS = frozenset({"file.read", "file.list", "file.patch"})
PROCESS_OPERATION_KINDS = frozenset({
    "process.start", "process.poll", "process.terminate",
})
WORKER_OPERATION_KINDS = FILE_OPERATION_KINDS | PROCESS_OPERATION_KINDS
MUTATING_OPERATION_KINDS = frozenset({
    "file.patch", "process.start", "process.terminate",
})
WORKER_OUTCOME_STATES = frozenset({
    "SUCCEEDED", "REFUSED", "FAILED", "UNKNOWN",
})


def _identifier(value: object, field: str, *, max_length: int = 200,
                pattern: str = r"[A-Za-z0-9._-]+") -> None:
    if (not isinstance(value, str) or len(value) > max_length
            or re.fullmatch(pattern, value, flags=re.ASCII) is None):
        raise ValueError("worker_operation_invalid_" + field)


def _file_target(target: object) -> None:
    """Reject obvious traversal lexically; this is NOT filesystem fencing."""
    if not isinstance(target, str) or not target:
        raise ValueError("worker_operation_invalid_target")
    if len(target.encode("utf-8")) > 1024:
        raise ValueError("worker_operation_invalid_target")
    if (target.startswith("/") or "\\" in target
            or ":" in target.split("/")[0]
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
            or any(part in ("", ".", "..") for part in target.split("/"))):
        raise ValueError("worker_operation_invalid_target")


@dataclass(frozen=True)
class WorkerRunBinding:
    """Caller-supplied run identity; never proof of ledger or device authority."""

    project_id: str
    writer_key: str
    task_id: str
    run_id: str
    attempt: int

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id", max_length=160,
                    pattern=r"[a-z0-9._-]+")
        _identifier(self.writer_key, "writer_key",
                    pattern=r"[A-Za-z0-9:._-]+")
        _identifier(self.task_id, "task_id")
        _identifier(self.run_id, "run_id")
        if (not isinstance(self.attempt, int) or isinstance(self.attempt, bool)
                or not 1 <= self.attempt <= 1000000):
            raise ValueError("worker_operation_invalid_attempt")


@dataclass(frozen=True)
class WorkerOperationRequest:
    """Versioned metadata; no command argv, patch bytes or raw credentials."""

    binding: WorkerRunBinding
    operation_id: str
    kind: str
    target: str
    max_output_bytes: int = WORKER_MAX_OUTPUT_BYTES
    schema_version: int = WORKER_OPERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (not isinstance(self.schema_version, int)
                or isinstance(self.schema_version, bool)
                or self.schema_version != WORKER_OPERATION_SCHEMA_VERSION):
            raise ValueError("worker_operation_invalid_schema")
        if not isinstance(self.binding, WorkerRunBinding):
            raise ValueError("worker_operation_invalid_binding")
        _identifier(self.operation_id, "operation_id")
        if not isinstance(self.kind, str) or self.kind not in WORKER_OPERATION_KINDS:
            raise ValueError("worker_operation_invalid_kind")
        if self.kind in FILE_OPERATION_KINDS:
            _file_target(self.target)
        else:
            # process.start targets a future allowlisted *profile ID* (not
            # shell text); poll/terminate targets an opaque owned handle.
            _identifier(self.target, "target")
        if (not isinstance(self.max_output_bytes, int)
                or isinstance(self.max_output_bytes, bool)
                or not 1 <= self.max_output_bytes <= WORKER_MAX_OUTPUT_BYTES):
            raise ValueError("worker_operation_invalid_output_limit")


@dataclass(frozen=True)
class WorkerOperationOutcome:
    """An observed result; UNKNOWN remains an unresolved external outcome."""

    operation_id: str
    state: str
    output: bytes = b""
    error_code: Optional[str] = None
    schema_version: int = WORKER_OPERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (not isinstance(self.schema_version, int)
                or isinstance(self.schema_version, bool)
                or self.schema_version != WORKER_OPERATION_SCHEMA_VERSION):
            raise ValueError("worker_outcome_invalid_schema")
        _identifier(self.operation_id, "operation_id")
        if not isinstance(self.state, str) or self.state not in WORKER_OUTCOME_STATES:
            raise ValueError("worker_outcome_invalid_state")
        if not isinstance(self.output, bytes):
            raise ValueError("worker_outcome_invalid_output")
        if self.state == "SUCCEEDED":
            if self.error_code is not None:
                raise ValueError("worker_outcome_invalid_error")
        else:
            if self.output:
                raise ValueError("worker_outcome_invalid_output")
            if (not isinstance(self.error_code, str)
                    or len(self.error_code) > 128
                    or re.fullmatch(r"[A-Za-z0-9._-]+", self.error_code,
                                    flags=re.ASCII) is None):
                raise ValueError("worker_outcome_invalid_error")


def require_matching_worker_run(
    expected: WorkerRunBinding, request: WorkerOperationRequest
) -> None:
    """Compare against a *separately obtained* ledger-bound expected identity.

    Passing forged 'expected' data is not authorization. P1-C must perform
    independent ledger, capability, device, path and process ownership checks.
    """
    if (not isinstance(expected, WorkerRunBinding)
            or not isinstance(request, WorkerOperationRequest)
            or expected != request.binding):
        raise ValueError("worker_operation_scope_mismatch")


def validate_worker_outcome(
    request: WorkerOperationRequest, outcome: WorkerOperationOutcome
) -> WorkerOperationOutcome:
    if not isinstance(request, WorkerOperationRequest):
        raise ValueError("worker_operation_invalid_request")
    if (not isinstance(outcome, WorkerOperationOutcome)
            or outcome.operation_id != request.operation_id):
        raise ValueError("worker_outcome_operation_mismatch")
    if len(outcome.output) > request.max_output_bytes:
        raise ValueError("worker_outcome_too_large")
    return outcome


def requires_independent_readback(
    request: WorkerOperationRequest, outcome: WorkerOperationOutcome
) -> bool:
    """Unknown results require readback; this never triggers a retry."""
    return validate_worker_outcome(request, outcome).state == "UNKNOWN"


class WorkerFileOperations(Protocol):
    """Future run-scoped adapter operations; not implemented in production."""

    def read(self, request: WorkerOperationRequest) -> WorkerOperationOutcome: ...

    def list(self, request: WorkerOperationRequest) -> WorkerOperationOutcome: ...

    def patch(self, request: WorkerOperationRequest) -> WorkerOperationOutcome: ...


class WorkerProcessOperations(Protocol):
    """Future owned-process adapter operations; no raw shell in this module."""

    def start(self, request: WorkerOperationRequest) -> WorkerOperationOutcome: ...

    def poll(self, request: WorkerOperationRequest) -> WorkerOperationOutcome: ...

    def terminate(self, request: WorkerOperationRequest) -> WorkerOperationOutcome: ...


class WorkerTransport(Protocol):
    """Structural composition only, deliberately not an executor."""

    files: WorkerFileOperations
    processes: WorkerProcessOperations
