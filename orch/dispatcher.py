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
