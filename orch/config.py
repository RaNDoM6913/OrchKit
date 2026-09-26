from __future__ import annotations

import json
import os
import stat as statmod
import tempfile
from pathlib import Path
from typing import Any, Dict

SCHEMA_VERSION = 1
DEFAULT_PROFILE = "safe"
HOME_CONFIG_MAX_BYTES = 64 * 1024




def ensure_private_dir(path: Path) -> Path:
    target = path.expanduser()
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("private_directory_unsafe")
    else:
        target.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(target, 0o700)
    return target


def ensure_private_file(path: Path) -> Path:
    target = path.expanduser()
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise ValueError("private_file_unsafe")
        os.chmod(target, 0o600)
    return target


def regular_file_read_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def read_bounded_regular_file(
    path: Path,
    *,
    max_bytes: int,
    unsafe_error: str,
    too_large_error: str,
    repair_mode: int | None = None,
):
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    flags = regular_file_read_flags()
    if not hasattr(os, "O_NOFOLLOW") and target.is_symlink():
        raise ValueError(unsafe_error)
    try:
        fd = os.open(str(target), flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(unsafe_error) from exc
    try:
        info = os.fstat(fd)
        if not statmod.S_ISREG(info.st_mode):
            raise ValueError(unsafe_error)
        if info.st_size > max_bytes:
            raise ValueError(too_large_error)
        if repair_mode is not None:
            os.fchmod(fd, repair_mode)
            mode = repair_mode
        else:
            mode = statmod.S_IMODE(info.st_mode)
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > max_bytes:
        raise ValueError(too_large_error)
    return raw, {"path": str(target), "bytes": len(raw), "mode": mode}


def read_bounded_json_object(
    path: Path,
    *,
    max_bytes: int,
    unsafe_error: str,
    too_large_error: str,
    invalid_error: str,
    repair_mode: int | None = None,
):
    raw, meta = read_bounded_regular_file(
        path,
        max_bytes=max_bytes,
        unsafe_error=unsafe_error,
        too_large_error=too_large_error,
        repair_mode=repair_mode,
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(invalid_error) from exc
    if not isinstance(data, dict):
        raise ValueError(invalid_error)
    return data, meta


def default_home() -> Path:
    value = os.environ.get("ORCH_HOME")
    return Path(value).expanduser().resolve() if value else (Path.home() / ".orch").resolve()


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    unsafe_error: str = "atomic_target_unsafe",
) -> None:
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(unsafe_error)
    fd, temp_name = tempfile.mkstemp(
        prefix=target.name + ".tmp-",
        dir=str(target.parent),
    )
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(target))
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int = 0o600,
    unsafe_error: str = "atomic_text_target_unsafe",
) -> None:
    atomic_write_bytes(
        path,
        text.encode("utf-8"),
        mode=mode,
        unsafe_error=unsafe_error,
    )


def atomic_write_json(path: Path, value: Dict[str, Any], *, mode: int = 0o600) -> None:
    data = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(
        path,
        data,
        mode=mode,
        unsafe_error="atomic_json_target_unsafe",
    )


def ensure_home(home: Path) -> Dict[str, Any]:
    home = home.expanduser().resolve()
    for relative in ("projects", "logs", "snapshots", "reviews", "claims"):
        ensure_private_dir(home / relative)
    config_path = home / "config.json"
    try:
        config, _ = read_bounded_json_object(
            config_path,
            max_bytes=HOME_CONFIG_MAX_BYTES,
            unsafe_error="home_config_unsafe",
            too_large_error="home_config_too_large",
            invalid_error="home_config_invalid_json",
            repair_mode=0o600,
        )
    except FileNotFoundError:
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
