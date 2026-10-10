"""Bounded local file.list over an explicitly granted, descriptor-owned directory.

A trusted in-process caller supplies a live LocalFileScope. This module does
not open arbitrary paths, follow children, read child contents, implement
mutations, provide an external endpoint or fence hostile same-user processes.
"""

from __future__ import annotations

import json
import os
import stat

from .bridge_file_scope import LocalFileScope
from .worker_operations import (
    WorkerOperationOutcome, WorkerOperationRequest, validate_worker_outcome,
)


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _entry_name(entry: os.DirEntry) -> str:
    # Names only: never follow, stat, or open children.
    return entry.name


def _refused(request: WorkerOperationRequest, code: str) -> WorkerOperationOutcome:
    return WorkerOperationOutcome(
        operation_id=request.operation_id, state="REFUSED", error_code=code,
    )


class LocalBoundedFileLister:
    """Enumerate a single explicitly allowed directory, without partial output.

    The requested directory must appear as an exact trailing-slash entry in
    the task's live allowed_paths; grants for only its children or its parent
    do not suffice. Results are compact ASCII-escaped JSON bytes containing
    sorted immediate child names, including symlinks as inert names only.
    """

    def __init__(self, scope: LocalFileScope):
        if not isinstance(scope, LocalFileScope):
            raise ValueError("bridge_file_list_invalid_scope")
        self._scope = scope

    def list(self, request: WorkerOperationRequest) -> WorkerOperationOutcome:
        if not isinstance(request, WorkerOperationRequest):
            raise ValueError("bridge_file_list_invalid_request")
        if request.kind != "file.list":
            return _refused(request, "file_list_kind_unsupported")
        if os.scandir not in os.supports_fd:
            return _refused(request, "file_list_unsupported_platform")

        limit = request.max_output_bytes
        try:
            with self._scope.open_listing(request) as fd:
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISDIR(before.st_mode):
                        return _refused(request, "file_list_denied")
                    names = []
                    # The response is {"entries":[...]} (compact ASCII JSON).
                    # Track final size before retaining any additional name.
                    used = len(b'{"entries":[]}')
                    if used > limit:
                        return _refused(request, "file_list_too_large")
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            name = _entry_name(entry)
                            item_bytes = len(json.dumps(name, ensure_ascii=True).encode("ascii"))
                            addition = item_bytes + (1 if names else 0)
                            if used + addition > limit:
                                return _refused(request, "file_list_too_large")
                            names.append(name)
                            used += addition
                    after = os.fstat(fd)
                except OSError:
                    return WorkerOperationOutcome(
                        operation_id=request.operation_id, state="FAILED",
                        error_code="file_list_io_failed",
                    )
                if _signature(before) != _signature(after):
                    return _refused(request, "file_list_changed")

                # Reacquire through every no-follow component, rechecking the
                # live claim/session/capability/allowlist before output leaves.
                with self._scope.open_listing(request) as checked_fd:
                    if _signature(os.fstat(checked_fd)) != _signature(after):
                        return _refused(request, "file_list_changed")

                output = json.dumps(
                    {"entries": sorted(names)}, sort_keys=True,
                    ensure_ascii=True, separators=(",", ":"),
                ).encode("ascii")
                if len(output) != used:
                    return _refused(request, "file_list_changed")
                return validate_worker_outcome(
                    request, WorkerOperationOutcome(
                        operation_id=request.operation_id,
                        state="SUCCEEDED", output=output,
                    ),
                )
        except (OSError, ValueError):
            return _refused(request, "file_list_denied")
