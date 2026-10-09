"""Version-aware storage seam for observational route evidence.

Only the historical v1 codec is registered. Evidence is never execution
authority: caller-owned device binding and ledger checks stay outside here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

from .config import ensure_private_dir, read_bounded_json_object
from .transport_contracts import validate_route_evidence


ROUTE_EVIDENCE_MAX_BYTES = 64 * 1024


class RouteEvidenceCodec:
    """Dispatch known evidence versions without implicitly upgrading records."""

    _validators = {1: validate_route_evidence}

    @classmethod
    def validate(cls, evidence: Dict[str, Any]) -> Dict[str, Any]:
        version = evidence.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool):
            validator = cls._validators.get(version)
            if validator is not None:
                return validator(evidence)
        # Keep historical v1 error precedence (unknown fields before schema)
        # while rejecting every unregistered version and noninteger value.
        return validate_route_evidence(evidence)

    @classmethod
    def encode(cls, evidence: Dict[str, Any]) -> bytes:
        cls.validate(evidence)
        return (
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")


def _create_private_bytes_once(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise ValueError("route_evidence_unsafe")
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise ValueError("route_evidence_already_recorded") from exc
    except OSError as exc:
        raise ValueError("route_evidence_unsafe") from exc
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("short route evidence write")
            offset += written
        os.fsync(fd)
    except OSError as exc:
        raise ValueError("route_evidence_write_failed") from exc
    finally:
        os.close(fd)


class RouteEvidenceStore:
    """Versioned v1 file I/O only; no ledger, marker or trust decisions."""

    def __init__(self, home: Path):
        self.home = home

    def write_once(self, evidence: Dict[str, Any]) -> Path:
        # Fail schema validation before creating an evidence directory.
        data = RouteEvidenceCodec.encode(evidence)
        directory = ensure_private_dir(self.home.resolve() / "route-evidence")
        path = directory / f"{evidence['run_id']}.json"
        _create_private_bytes_once(path, data)
        return path

    def read(self, run_id: str) -> Dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,200}", run_id) is None:
            raise ValueError("route_evidence_invalid_run_id")
        path = self.home.resolve() / "route-evidence" / f"{run_id}.json"
        try:
            evidence, meta = read_bounded_json_object(
                path,
                max_bytes=ROUTE_EVIDENCE_MAX_BYTES,
                unsafe_error="route_evidence_unsafe",
                too_large_error="route_evidence_too_large",
                invalid_error="route_evidence_invalid_json",
                repair_mode=0o600,
            )
        except FileNotFoundError:
            return {"status": "UNVERIFIED", "path": str(path)}
        RouteEvidenceCodec.validate(evidence)
        if evidence["run_id"] != run_id:
            raise ValueError("route_evidence_run_binding_mismatch")
        return {
            "status": "RECORDED",
            "path": str(path),
            "route_evidence": evidence,
            "bytes": meta["bytes"],
            "mode": oct(meta["mode"]),
        }
