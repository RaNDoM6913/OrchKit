from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

SCHEMA_VERSION = 1
DEFAULT_PROFILE = "safe"


def default_home() -> Path:
    value = os.environ.get("ORCH_HOME")
    return Path(value).expanduser().resolve() if value else (Path.home() / ".orch").resolve()


def atomic_write_json(path: Path, value: Dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)


def ensure_home(home: Path) -> Dict[str, Any]:
    home = home.expanduser().resolve()
    for relative in ("projects", "logs", "snapshots", "reviews", "claims"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    config_path = home / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = {
            "schema_version": SCHEMA_VERSION,
            "variant": "B_NEW_CHAT_PER_ATTEMPT",
            "default_profile": DEFAULT_PROFILE,
            "billing": {
                "model_api_budget": 0,
                "allow_paid_model_api": False,
                "allow_external_ai": False,
                "allow_extra_paid_credits": False,
                "on_unknown_billing": "pause_before_model_run",
            },
            "review": {
                "default_mode": "risk_based",
                "default_reviewer": "codex",
                "codex_optional": True,
            },
        }
        atomic_write_json(config_path, config)
    return {"home": str(home), "config": str(config_path), "settings": config}


def configure_home(home: Path, *, profile: str = DEFAULT_PROFILE) -> Dict[str, Any]:
    state = ensure_home(home)
    config_path = Path(state["config"])
    config = state["settings"]
    config["default_profile"] = profile
    atomic_write_json(config_path, config)
    return {"home": str(home.resolve()), "config": str(config_path), "settings": config}
