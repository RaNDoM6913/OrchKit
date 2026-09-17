from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

from .codex_review import CODEX_BIN, subscription_preflight
from .config import ensure_home


def run_doctor(home: Path, *, check_codex: bool = True) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    setup = ensure_home(home)
    checks.append({"id": "python", "status": "PASS" if sys.version_info >= (3, 9) else "BLOCKED", "detail": platform.python_version()})
    git = shutil.which("git")
    checks.append({"id": "git", "status": "PASS" if git else "BLOCKED", "detail": git})
    writable = os.access(str(home), os.W_OK)
    checks.append({"id": "orch_home", "status": "PASS" if writable else "BLOCKED", "detail": str(home)})
    checks.append({"id": "codex_binary", "status": "PASS" if CODEX_BIN.is_file() else "OPTIONAL_MISSING", "detail": str(CODEX_BIN)})
    if check_codex and CODEX_BIN.is_file():
        preflight = subscription_preflight(home)
        checks.append({
            "id": "codex_subscription",
            "status": "PASS" if preflight.get("status") == "PASS" else "OPTIONAL_BLOCKED",
            "detail": preflight,
        })
    rdc_marker = home / "rdc-bootstrap.json"
    if rdc_marker.is_file():
        checks.append({"id": "rdc_chat_bridge", "status": "RECORDED", "detail": str(rdc_marker)})
    else:
        checks.append({"id": "rdc_chat_bridge", "status": "UNVERIFIED", "detail": "Requires a ChatGPT bootstrap run; local CLI cannot prove connector availability."})
    hard_block = any(item["status"] == "BLOCKED" for item in checks)
    attention = any(item["status"] in {"OPTIONAL_BLOCKED", "OPTIONAL_MISSING", "UNVERIFIED"} for item in checks)
    return {
        "status": "BLOCKED" if hard_block else "ATTENTION" if attention else "READY",
        "home": str(home),
        "config": setup["config"],
        "checks": checks,
    }
