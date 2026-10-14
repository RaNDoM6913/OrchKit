"""Strict bounded JSON framing for the foreground, local read-only Bridge."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import stat
import time
from dataclasses import dataclass
from typing import Any

from .worker_operations import WorkerOperationRequest, WorkerRunBinding


LOCAL_SCHEMA_VERSION = 1
LOCAL_OPERATIONS = ("session.status", "file.read", "file.list")
_MAX_FRAME_BYTES = 65536
_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,200}", re.ASCII)
_ROUTING = re.compile(r"[A-Za-z0-9_-]{24,200}", re.ASCII)


@dataclass(frozen=True)
class LocalServiceLimits:
    max_frame_bytes: int = 65536
    min_response_bytes: int = 4096
    max_response_bytes: int = 65536
    io_timeout_sec: float = 5.0
    idle_timeout_sec: float = 30.0

    def __post_init__(self) -> None:
        if (any(type(value) is not int for value in
                (self.max_frame_bytes, self.min_response_bytes, self.max_response_bytes))
                or not 1 <= self.min_response_bytes <= self.max_response_bytes
                or not self.max_response_bytes <= self.max_frame_bytes <= _MAX_FRAME_BYTES
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value <= 0 for value in
                       (self.io_timeout_sec, self.idle_timeout_sec))
                or self.io_timeout_sec > 5 or self.idle_timeout_sec > 30):
            raise ValueError("local_bridge_invalid_limits")


def _unique_object(pairs: list) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("local_bridge_invalid_frame")
        value[key] = item
    return value


def decode_payload(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("local_bridge_invalid_frame") from None


def encode_payload(value: dict[str, Any]) -> bytes:
    try:
        if not isinstance(value, dict):
            raise ValueError()
        return json.dumps(value, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False).encode("ascii")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("local_bridge_invalid_frame") from None


def _receive_exact(sock: socket.socket, size: int, deadline: float,
                   *, clean_eof: bool = False) -> bytes:
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("local_bridge_io_timeout")
        sock.settimeout(remaining)
        try:
            piece = sock.recv(size - len(result))
        except socket.timeout:
            raise TimeoutError("local_bridge_io_timeout") from None
        if not piece:
            if not result and clean_eof:
                raise EOFError("local_bridge_connection_closed")
            raise ValueError("local_bridge_invalid_frame")
        result.extend(piece)
    return bytes(result)


def receive_frame(sock: socket.socket, limits: LocalServiceLimits) -> dict[str, Any]:
    deadline = time.monotonic() + limits.io_timeout_sec
    header = _receive_exact(sock, 4, deadline, clean_eof=True)
    size = struct.unpack("!I", header)[0]
    if not 1 <= size <= limits.max_frame_bytes:
        raise ValueError("local_bridge_invalid_frame")
    return decode_payload(_receive_exact(sock, size, deadline))


def send_frame(sock: socket.socket, value: dict[str, Any],
               limits: LocalServiceLimits, *, budget: int = None) -> None:
    payload = encode_payload(value)
    if len(payload) > min(limits.max_frame_bytes,
                          limits.max_response_bytes if budget is None else budget):
        raise ValueError("local_bridge_response_too_large")
    sock.settimeout(limits.io_timeout_sec)
    try:
        sock.sendall(struct.pack("!I", len(payload)) + payload)
    except socket.timeout:
        raise TimeoutError("local_bridge_io_timeout") from None


def _common(value: dict, keys: set) -> None:
    if (not isinstance(value, dict) or set(value) != keys
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != LOCAL_SCHEMA_VERSION
            or not isinstance(value.get("request_id"), str)
            or _IDENTIFIER.fullmatch(value["request_id"]) is None):
        raise ValueError("local_bridge_invalid_request")


def validate_hello(value: dict[str, Any]) -> dict[str, Any]:
    _common(value, {"schema_version", "request_id", "type", "transport_token"})
    if (value["type"] != "hello" or not isinstance(value["transport_token"], str)
            or _ROUTING.fullmatch(value["transport_token"]) is None):
        raise ValueError("local_bridge_invalid_request")
    return value


def validate_request(value: dict[str, Any], limits: LocalServiceLimits) -> dict[str, Any]:
    _common(value, {"schema_version", "request_id", "session_id", "operation",
                    "target", "max_response_bytes"})
    if (not isinstance(value["session_id"], str)
            or _ROUTING.fullmatch(value["session_id"]) is None
            or not isinstance(value["operation"], str)
            or value["operation"] not in LOCAL_OPERATIONS
            or type(value["max_response_bytes"]) is not int
            or not limits.min_response_bytes <= value["max_response_bytes"] <= limits.max_response_bytes):
        raise ValueError("local_bridge_invalid_request")
    if value["operation"] == "session.status":
        if value["target"] is not None:
            raise ValueError("local_bridge_invalid_request")
    else:
        # Reuse v1 syntactic target validation without accepting a wire binding.
        try:
            WorkerOperationRequest(
                WorkerRunBinding("local", "local", "local", "local", 1),
                value["request_id"], value["operation"], value["target"], 1,
            )
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("local_bridge_invalid_request") from None
    return value


def native_payload_budget(empty_envelope: dict[str, Any], budget: int) -> int:
    """Largest raw byte budget whose base64 fits the actual empty envelope."""
    available = budget - len(encode_payload(empty_envelope))
    return max(0, (available // 4) * 3)


def open_private_directory(path: Path) -> int:
    """Open an existing canonical operator directory without creating/repairing."""
    path = Path(path)
    if (not path.is_absolute() or str(path) != os.path.abspath(str(path))
            or os.path.realpath(str(path)) != str(path)):
        raise ValueError("local_bridge_endpoint_denied")
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        named = os.stat(str(path), follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise ValueError("local_bridge_endpoint_denied")
        return fd
    except BaseException:
        os.close(fd)
        raise
