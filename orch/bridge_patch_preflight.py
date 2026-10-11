"""Local original-content observation for a future file.patch contract.

This helper accepts file.read metadata only, performs no write, and returns no
contents or permission to apply a patch. Existing live scope/reader authority
is reused unchanged. No patch payload, durable journal or endpoint is added.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from .bridge_file_read import LocalBoundedFileReader
from .bridge_file_scope import LocalFileScope
from .worker_operations import (
    WORKER_MAX_OUTPUT_BYTES, WorkerOperationOutcome, WorkerOperationRequest,
    validate_worker_outcome,
)


@dataclass(frozen=True)
class FilePatchBaseline:
    """Trusted local companion input; not a v1 request field or credential."""

    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if (not isinstance(self.sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.sha256, flags=re.ASCII) is None):
            raise ValueError("patch_preflight_invalid_digest")
        if (not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool)
                or not 0 <= self.size_bytes <= WORKER_MAX_OUTPUT_BYTES):
            raise ValueError("patch_preflight_invalid_size")


def _refused(request: WorkerOperationRequest, code: str) -> WorkerOperationOutcome:
    return WorkerOperationOutcome(
        operation_id=request.operation_id, state="REFUSED", error_code=code,
    )


class LocalFilePatchPreflight:
    """Observe an existing original twice through the same live file reader.

    Each read retains at most 64 KiB; both observations must match. Success
    carries no bytes and is point-in-time only, never a write authorization.
    Repeated IDs recompute, without durable idempotency or replay semantics.
    Scope ownership, non-thread-safety and same-user limitations are unchanged.
    """

    def __init__(self, scope: LocalFileScope):
        if not isinstance(scope, LocalFileScope):
            raise ValueError("patch_preflight_invalid_scope")
        self._reader = LocalBoundedFileReader(scope)

    def check(
        self, request: WorkerOperationRequest, baseline: FilePatchBaseline,
    ) -> WorkerOperationOutcome:
        if not isinstance(request, WorkerOperationRequest):
            raise ValueError("patch_preflight_invalid_request")
        if not isinstance(baseline, FilePatchBaseline):
            raise ValueError("patch_preflight_invalid_baseline")
        if request.kind != "file.read":
            return _refused(request, "patch_preflight_kind_unsupported")

        observed = self._reader.read(request)
        if observed.state != "SUCCEEDED":
            return observed  # Reader errors already contain no partial bytes.
        if (len(observed.output) != baseline.size_bytes
                or hashlib.sha256(observed.output).hexdigest() != baseline.sha256):
            return _refused(request, "patch_preflight_base_mismatch")

        # Reobserve after comparison, including renewed session/capability,
        # root/path and file identity checks. Never cache a checked path or ID.
        checked = self._reader.read(request)
        if checked.state != "SUCCEEDED":
            return checked
        if checked.output != observed.output:
            return _refused(request, "patch_preflight_base_changed")
        return validate_worker_outcome(
            request, WorkerOperationOutcome(
                operation_id=request.operation_id, state="SUCCEEDED",
            ),
        )
