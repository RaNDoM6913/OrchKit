"""Pure version-1 RDC marker and route-evidence validation contracts.

These validators inspect in-memory objects only. File permissions, bounded
reads, binding decisions and record storage remain in the existing callers.
"""

from __future__ import annotations

import re
from typing import Any, Dict


ROUTE_OBSERVATION_VALUES = {"yes", "no", "unknown"}


def _bounded_marker_text(value: Any, field: str, *, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("rdc_" + field + "_required")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError("rdc_" + field + "_too_large")
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise ValueError("rdc_" + field + "_control_character")
    return value.strip()


def validate_rdc_marker(marker: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "schema_version", "device_id", "device_name", "recorded_at", "source",
    }
    unknown = sorted(set(marker) - allowed)
    if unknown:
        raise ValueError("rdc_marker_unknown_field:" + unknown[0])
    version = marker.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("rdc_marker_invalid_schema")
    _bounded_marker_text(marker.get("device_id"), "device_id", max_bytes=512)
    _bounded_marker_text(marker.get("device_name"), "device_name", max_bytes=512)
    _bounded_marker_text(marker.get("recorded_at"), "recorded_at", max_bytes=128)
    if marker.get("source") != "chatgpt_rdc_bootstrap":
        raise ValueError("rdc_marker_invalid_source")
    return marker


def validate_route_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "schema_version", "run_id", "task_id", "surface", "transport",
        "device_id", "device_name",
        "model", "reasoning", "usage", "observed_at", "source",
        "work_used", "codex_execution_used", "model_api_used",
        "external_provider_used",
    }
    unknown = sorted(set(evidence) - allowed)
    if unknown:
        raise ValueError("route_evidence_unknown_field:" + unknown[0])
    version = evidence.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("route_evidence_invalid_schema")
    for field in ("run_id", "task_id"):
        value = evidence.get(field)
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value) is None
        ):
            raise ValueError("route_evidence_invalid_" + field)
    if evidence.get("surface") != "ordinary_chat":
        raise ValueError("route_evidence_invalid_surface")
    if evidence.get("transport") != "rdc":
        raise ValueError("route_evidence_invalid_transport")
    for field, limit in (
        ("device_id", 512), ("device_name", 512), ("model", 256),
        ("reasoning", 128), ("usage", 256), ("observed_at", 128),
        ("source", 128),
    ):
        _bounded_marker_text(evidence.get(field), field, max_bytes=limit)
    for field in (
        "work_used", "codex_execution_used", "model_api_used",
        "external_provider_used",
    ):
        if evidence.get(field) not in ROUTE_OBSERVATION_VALUES:
            raise ValueError("route_evidence_invalid_observation:" + field)
    return evidence
