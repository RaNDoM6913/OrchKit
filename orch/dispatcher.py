from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Dict

from .config import (atomic_write_json, atomic_write_text, ensure_private_dir,
                     read_bounded_json_object)
from .core import utc_now
from .project import ProjectRegistry


RDC_MARKER_MAX_BYTES = 64 * 1024
ROUTE_EVIDENCE_MAX_BYTES = 64 * 1024

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


def record_rdc(home: Path, *, device_id: str, device_name: str) -> Dict[str, Any]:
    device_id = _bounded_marker_text(device_id, "device_id", max_bytes=512)
    device_name = _bounded_marker_text(device_name, "device_name", max_bytes=512)
    marker = {
        "schema_version": 1,
        "device_id": device_id,
        "device_name": device_name,
        "recorded_at": utc_now(),
        "source": "chatgpt_rdc_bootstrap",
    }
    path = home.resolve() / "rdc-bootstrap.json"
    atomic_write_json(path, marker)
    return {"status": "RECORDED", "path": str(path), "rdc": marker}


def read_rdc(home: Path) -> Dict[str, Any]:
    path = home.resolve() / "rdc-bootstrap.json"
    try:
        marker, meta = read_bounded_json_object(
            path,
            max_bytes=RDC_MARKER_MAX_BYTES,
            unsafe_error="rdc_marker_unsafe",
            too_large_error="rdc_marker_too_large",
            invalid_error="rdc_marker_invalid_json",
            repair_mode=0o600,
        )
    except FileNotFoundError:
        return {"status": "UNVERIFIED", "path": str(path)}
    validate_rdc_marker(marker)
    return {
        "status": "RECORDED", "path": str(path), "rdc": marker,
        "bytes": meta["bytes"], "mode": oct(meta["mode"]),
    }


def validate_route_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "schema_version", "surface", "transport", "device_id", "device_name",
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


def record_route_evidence(
    home: Path, *, model: str, reasoning: str, usage: str, source: str,
    work_used: str, codex_execution_used: str, model_api_used: str,
    external_provider_used: str,
) -> Dict[str, Any]:
    rdc = read_rdc(home)
    if rdc.get("status") != "RECORDED":
        raise ValueError("route_evidence_requires_rdc_marker")
    marker = rdc["rdc"]
    evidence = {
        "schema_version": 1,
        "surface": "ordinary_chat",
        "transport": "rdc",
        "device_id": marker["device_id"],
        "device_name": marker["device_name"],
        "model": _bounded_marker_text(model, "model", max_bytes=256),
        "reasoning": _bounded_marker_text(
            reasoning, "reasoning", max_bytes=128
        ),
        "usage": _bounded_marker_text(usage, "usage", max_bytes=256),
        "observed_at": utc_now(),
        "source": _bounded_marker_text(source, "source", max_bytes=128),
        "work_used": work_used,
        "codex_execution_used": codex_execution_used,
        "model_api_used": model_api_used,
        "external_provider_used": external_provider_used,
    }
    validate_route_evidence(evidence)
    path = home.resolve() / "worker-route-evidence.json"
    atomic_write_json(path, evidence)
    return {
        "status": "RECORDED",
        "path": str(path),
        "route_evidence": evidence,
        "acceptance": "NOT_EVALUATED",
    }


def read_route_evidence(home: Path) -> Dict[str, Any]:
    path = home.resolve() / "worker-route-evidence.json"
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
    validate_route_evidence(evidence)
    rdc = read_rdc(home)
    binding_matches = (
        rdc.get("status") == "RECORDED"
        and evidence["device_id"] == rdc["rdc"]["device_id"]
        and evidence["device_name"] == rdc["rdc"]["device_name"]
    )
    return {
        "status": "RECORDED" if binding_matches else "STALE_DEVICE_BINDING",
        "path": str(path),
        "route_evidence": evidence,
        "rdc_binding_matches": binding_matches,
        "bytes": meta["bytes"],
        "mode": oct(meta["mode"]),
        "acceptance": "NOT_EVALUATED",
    }


def _orch_command() -> str:
    explicit = os.environ.get("ORCH_EXECUTABLE")
    if explicit:
        return shlex.quote(str(Path(explicit).expanduser().resolve()))
    installed = shutil.which("orch")
    if installed:
        return shlex.quote(installed)
    return f"{shlex.quote(sys.executable)} -m orch"


def render_dispatcher(
    home: Path, *, output: Path | None = None, project_id: str | None = None
) -> Dict[str, Any]:
    resolved_home = home.resolve()
    template = resources.files("orch").joinpath(
        "templates/dispatcher_prompt.txt"
    ).read_text(encoding="utf-8")
    if project_id is not None:
        if re.fullmatch(r"[a-z0-9._-]+", project_id) is None:
            raise ValueError("invalid_project_id")
        ProjectRegistry.open_readonly(resolved_home).get(project_id)
        worker_id = f"scheduled-variant-b-{project_id}"
        project_arg = " --project " + shlex.quote(project_id)
        scope_boundary = (
            f"- This dispatcher is permanently scoped to project_id={project_id}. "
            "Never claim or dispatch work from another project."
        )
        default_target = resolved_home / "dispatchers" / f"{project_id}.txt"
        scope = "project"
    else:
        worker_id = "scheduled-variant-b"
        project_arg = ""
        scope_boundary = (
            "- This is the global dispatcher and may claim the oldest runnable "
            "task whose writer key is free."
        )
        default_target = resolved_home / "dispatcher-prompt.txt"
        scope = "global"
    rendered = (
        template
        .replace("{{ORCH_COMMAND}}", _orch_command())
        .replace("{{ORCH_HOME}}", shlex.quote(str(resolved_home)))
        .replace("{{WORKER_ID}}", shlex.quote(worker_id))
        .replace("{{PROJECT_ARG}}", project_arg)
        .replace("{{PROJECT_SCOPE_BOUNDARY}}", scope_boundary)
    )
    if output is not None:
        target = Path(
            os.path.abspath(os.path.expanduser(str(output)))
        )
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        ensure_private_dir(default_target.parent)
        target = default_target
    atomic_write_text(
        target,
        rendered,
        mode=0o600,
        unsafe_error="dispatcher_target_unsafe",
    )
    return {
        "status": "RENDERED", "path": str(target),
        "bytes": len(rendered.encode("utf-8")), "rdc": read_rdc(home),
        "scope": scope, "project_id": project_id, "worker_id": worker_id,
    }


def bootstrap_prompt(home: Path) -> str:
    command = _orch_command()
    root = shlex.quote(str(home.resolve()))
    return (
        "Use Remote Desktop Commander only. Discover my connected computer, then on that same device run "
        f"`{command} --root {root} rdc record --device-id <actual-device-id> --device-name <actual-device-name>`. "
        "Use the exact identity returned by RDC discovery; do not invent it. Do not open or modify any project. "
        "Then report only whether ORCH recorded the RDC marker successfully."
    )
