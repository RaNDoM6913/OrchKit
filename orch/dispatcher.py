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

from .config import atomic_write_json
from .core import utc_now
from .project import ProjectRegistry


def record_rdc(home: Path, *, device_id: str, device_name: str) -> Dict[str, Any]:
    if not device_id.strip() or not device_name.strip():
        raise ValueError("rdc_identity_required")
    marker = {
        "schema_version": 1,
        "device_id": device_id.strip(),
        "device_name": device_name.strip(),
        "recorded_at": utc_now(),
        "source": "chatgpt_rdc_bootstrap",
    }
    path = home.resolve() / "rdc-bootstrap.json"
    atomic_write_json(path, marker)
    return {"status": "RECORDED", "path": str(path), "rdc": marker}


def read_rdc(home: Path) -> Dict[str, Any]:
    path = home.resolve() / "rdc-bootstrap.json"
    if not path.is_file():
        return {"status": "UNVERIFIED", "path": str(path)}
    return {"status": "RECORDED", "path": str(path), "rdc": json.loads(path.read_text(encoding="utf-8"))}


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
        ProjectRegistry(resolved_home).get(project_id)
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
    target = output.resolve() if output else default_target
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    target.write_text(rendered, encoding="utf-8")
    os.chmod(target, 0o600)
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
