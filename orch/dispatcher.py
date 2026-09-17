from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Dict

from .config import atomic_write_json
from .core import utc_now


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


def render_dispatcher(home: Path, *, output: Path | None = None) -> Dict[str, Any]:
    template = resources.files("orch").joinpath("templates/dispatcher_prompt.txt").read_text(encoding="utf-8")
    rendered = template.replace("{{ORCH_COMMAND}}", _orch_command()).replace("{{ORCH_HOME}}", shlex.quote(str(home.resolve())))
    target = output or (home.resolve() / "dispatcher-prompt.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return {"status": "RENDERED", "path": str(target), "bytes": len(rendered.encode("utf-8")), "rdc": read_rdc(home)}


def bootstrap_prompt(home: Path) -> str:
    command = _orch_command()
    root = shlex.quote(str(home.resolve()))
    return (
        "Use Remote Desktop Commander only. Discover my connected computer, then on that same device run "
        f"`{command} --root {root} rdc record --device-id <actual-device-id> --device-name <actual-device-name>`. "
        "Use the exact identity returned by RDC discovery; do not invent it. Do not open or modify any project. "
        "Then report only whether ORCH recorded the RDC marker successfully."
    )
