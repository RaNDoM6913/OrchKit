"""Bounded in-process file.read adapter over an active LocalFileScope.

No external transport, daemon, list/patch/process operation or OS sandbox.
The scope provides ledger/run/capability and no-follow descriptor authority;
this adapter reads only inside that scope and never returns partial contents.
"""

from __future__ import annotations

import os
import stat

from .bridge_file_scope import LocalFileScope
from .worker_operations import (
    WorkerOperationOutcome, WorkerOperationRequest, validate_worker_outcome,
)


_READ_CHUNK_BYTES = 8192


def _file_signature(info: os.stat_result) -> tuple[int, ...]:
    """Detect common changes, not malicious same-user in-place races."""
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _read_chunk(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _refused(request: WorkerOperationRequest, code: str) -> WorkerOperationOutcome:
    return WorkerOperationOutcome(
        operation_id=request.operation_id, state="REFUSED", error_code=code,
    )


class LocalBoundedFileReader:
    """Read at most the declared limit from a descriptor-owned live scope.

    Trusted local code constructs and owns the LocalFileScope. An in-memory
    reader or operation ID is not a remote permission, durable journal or
    cross-session credential. No raw OSError or capability data is returned.
    """

    def __init__(self, scope: LocalFileScope):
        if not isinstance(scope, LocalFileScope):
            raise ValueError("bridge_file_read_invalid_scope")
        self._scope = scope

    def read(self, request: WorkerOperationRequest) -> WorkerOperationOutcome:
        if not isinstance(request, WorkerOperationRequest):
            raise ValueError("bridge_file_read_invalid_request")
        if request.kind != "file.read":
            return _refused(request, "file_read_kind_unsupported")

        limit = request.max_output_bytes  # Validated <= 64 KiB by v1 request.
        try:
            with self._scope.open_readonly(request) as fd:
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
                        return _refused(request, "file_read_denied")
                    if before.st_size > limit:
                        return _refused(request, "file_read_too_large")

                    data = bytearray()
                    # Read through EOF, including one excess-byte sentinel.
                    # At no point request or retain more than limit + 1 bytes.
                    while True:
                        size = min(_READ_CHUNK_BYTES, limit + 1 - len(data))
                        chunk = _read_chunk(fd, size)
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > limit:
                            return _refused(request, "file_read_too_large")

                    after = os.fstat(fd)
                except OSError:
                    return WorkerOperationOutcome(
                        operation_id=request.operation_id, state="FAILED",
                        error_code="file_read_io_failed",
                    )

                if (_file_signature(before) != _file_signature(after)
                        or len(data) != before.st_size):
                    return _refused(request, "file_read_changed")

                # Revalidate live session/capability, allowlist and path by
                # opening a fresh no-follow descriptor immediately before
                # returning bytes. This does not fence later same-user races.
                with self._scope.open_readonly(request) as checked_fd:
                    if _file_signature(os.fstat(checked_fd)) != _file_signature(after):
                        return _refused(request, "file_read_changed")

                return validate_worker_outcome(
                    request, WorkerOperationOutcome(
                        operation_id=request.operation_id,
                        state="SUCCEEDED", output=bytes(data),
                    ),
                )
        except (OSError, ValueError):
            return _refused(request, "file_read_denied")
