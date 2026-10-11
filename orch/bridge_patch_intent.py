"""Local patch intent persistence and independent content observation only.

These trusted in-process inputs do not implement WorkerFileOperations.patch.
Stored metadata contains no replacement bytes, capability, or replay authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
from pathlib import Path

from .bridge_authorization import LocalRunSessionAuthority
from .bridge_file_read import LocalBoundedFileReader
from .bridge_file_scope import LocalFileScope
from .bridge_operation_journal import (
    OperationIntentJournal, build_intent_record, request_fingerprint,
)
from .bridge_patch_preflight import FilePatchBaseline, LocalFilePatchPreflight
from .worker_operations import (
    WORKER_MAX_OUTPUT_BYTES, WorkerOperationRequest, WorkerRunBinding,
)


@dataclass(frozen=True)
class FilePatchReplacement:
    """Bounded local companion; bytes are neither persisted nor displayed."""

    baseline: FilePatchBaseline
    replacement: bytes = field(repr=False)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (not isinstance(self.schema_version, int)
                or isinstance(self.schema_version, bool)
                or self.schema_version != 1):
            raise ValueError("patch_intent_invalid_schema")
        if not isinstance(self.baseline, FilePatchBaseline):
            raise ValueError("patch_intent_invalid_baseline")
        if (not isinstance(self.replacement, bytes)
                or len(self.replacement) > WORKER_MAX_OUTPUT_BYTES):
            raise ValueError("patch_intent_invalid_replacement")
        if (len(self.replacement) == self.baseline.size_bytes
                and hashlib.sha256(self.replacement).hexdigest() == self.baseline.sha256):
            raise ValueError("patch_intent_unchanged_replacement")

    def metadata(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "baseline": asdict(self.baseline),
            "sha256": hashlib.sha256(self.replacement).hexdigest(),
            "size_bytes": len(self.replacement),
        }


def _result(status: str, *, record: dict | None = None,
            error_code: str | None = None) -> dict:
    value = {
        "status": status,
        "workspace_mutation": "NOT_IMPLEMENTED",
        "retry_allowed": False,
    }
    if record is not None:
        value["record"] = record
    if error_code is not None:
        value["error_code"] = error_code
    return value


def _unavailable(code: str) -> dict:
    return {
        "status": "UNAVAILABLE", "observation": "UNAVAILABLE",
        "error_code": code, "workspace_mutation": "NOT_IMPLEMENTED",
        "retry_allowed": False,
    }


class LocalFilePatchIntentService:
    """Record an intent or observe its target through current run authority.

    A duplicate record is looked up before original-content preflight. Target
    drift therefore does not erase a recorded intent after a lost response.
    Every response remains separate from patch execution/completion and native
    verification. Sessions are volatile and this service is not thread-safe.
    """

    def __init__(
        self, authority: LocalRunSessionAuthority, session_id: str,
        binding: WorkerRunBinding, capability_file: Path,
    ):
        if (not isinstance(authority, LocalRunSessionAuthority)
                or not isinstance(session_id, str)
                or not isinstance(binding, WorkerRunBinding)
                or not isinstance(capability_file, Path)):
            raise ValueError("patch_intent_invalid_authority")
        self._authority = authority
        self._session = session_id
        self._binding = binding
        self._capability = capability_file
        # Like LocalFileScope, trusted local construction obtains the home
        # from the actual authority, never a request-supplied storage path.
        self._journal = OperationIntentJournal(authority._orch.root)

    def _authorize(self) -> None:
        self._authority.require_session(
            self._session, self._binding, self._capability,
        )

    def _request_denial(self, request: WorkerOperationRequest) -> str | None:
        if not isinstance(request, WorkerOperationRequest):
            raise ValueError("patch_intent_invalid_request")
        if request.kind != "file.patch":
            return "patch_intent_kind_unsupported"
        if request.binding != self._binding:
            return "patch_intent_scope_mismatch"
        return None

    @staticmethod
    def _read_request(request: WorkerOperationRequest) -> WorkerOperationRequest:
        # The original response limit stays in the stored fingerprint. This
        # local observation has its own bounded ceiling and returns no bytes.
        return replace(request, kind="file.read",
                       max_output_bytes=WORKER_MAX_OUTPUT_BYTES)

    def prepare(
        self, request: WorkerOperationRequest, replacement: FilePatchReplacement,
    ) -> dict:
        denial = self._request_denial(request)
        if not isinstance(replacement, FilePatchReplacement):
            raise ValueError("patch_intent_invalid_companion")
        if denial:
            return _result("REFUSED", error_code=denial)
        try:
            self._authorize()
        except (OSError, TypeError, ValueError):
            return _result("REFUSED", error_code="patch_intent_authority_denied")

        metadata = replacement.metadata()
        fingerprint = request_fingerprint(request, metadata)
        found = self._journal.inspect(request.binding.run_id, request.operation_id)
        if found["status"] == "UNKNOWN":
            return _result("UNKNOWN", error_code=found.get("error_code", "journal_unknown"))
        if found["status"] == "RECORDED":
            recorded = found["record"]
            if recorded["fingerprint"] != fingerprint:
                return _result("CONFLICT", error_code="patch_intent_id_conflict")
            return _result("EXISTING", record=recorded)

        read_request = self._read_request(request)
        try:
            with LocalFileScope(
                self._authority, self._session, self._binding, self._capability,
            ) as scope:
                observed = LocalFilePatchPreflight(scope).check(
                    read_request, replacement.baseline,
                )
                if observed.state != "SUCCEEDED":
                    return _result(
                        "UNKNOWN" if observed.state == "UNKNOWN" else "REFUSED",
                        error_code=observed.error_code or "patch_intent_original_unavailable",
                    )

                def authorize_publication() -> None:
                    self._authorize()
                    # Renew file scope at the final storage publication gate.
                    # This is still a point-in-time check, never a CAS token.
                    with scope.open_readonly(read_request):
                        pass

                result = self._journal.create(
                    build_intent_record(request, metadata),
                    authorize=authorize_publication,
                )
        except (OSError, TypeError, ValueError):
            return _result("REFUSED", error_code="patch_intent_authority_denied")
        return _result(result["status"], record=result.get("record"),
                       error_code=result.get("error_code"))

    def observe(self, request: WorkerOperationRequest) -> dict:
        denial = self._request_denial(request)
        if denial:
            return _unavailable(denial)
        try:
            self._authorize()
        except (OSError, TypeError, ValueError):
            return _unavailable("patch_intent_authority_denied")
        found = self._journal.inspect(request.binding.run_id, request.operation_id)
        if found["status"] != "RECORDED":
            return _unavailable(found.get("error_code", "patch_intent_record_unavailable"))
        record = found["record"]
        if record["request"] != asdict(request):
            return _unavailable("patch_intent_request_mismatch")
        read_request = self._read_request(request)
        try:
            with LocalFileScope(
                self._authority, self._session, self._binding, self._capability,
            ) as scope:
                reader = LocalBoundedFileReader(scope)
                first = reader.read(read_request)
                if first.state != "SUCCEEDED":
                    return _unavailable("patch_intent_content_unavailable")
                second = reader.read(read_request)
                if second.state != "SUCCEEDED" or first.output != second.output:
                    return _unavailable("patch_intent_observation_changed")
        except (OSError, TypeError, ValueError):
            return _unavailable("patch_intent_authority_denied")
        size = len(second.output)
        digest = hashlib.sha256(second.output).hexdigest()
        baseline = record["replacement"]["baseline"]
        replacement = record["replacement"]
        if size == baseline["size_bytes"] and digest == baseline["sha256"]:
            classification = "ORIGINAL_MATCH"
        elif size == replacement["size_bytes"] and digest == replacement["sha256"]:
            classification = "REPLACEMENT_MATCH"
        else:
            classification = "OTHER_CONTENT"
        return {
            "status": "OBSERVED", "observation": classification,
            "operation_id": request.operation_id, "fingerprint": record["fingerprint"],
            "content_sha256": digest, "content_size_bytes": size,
            "workspace_mutation": "NOT_IMPLEMENTED", "retry_allowed": False,
        }
