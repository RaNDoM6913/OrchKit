"""Explicit local connections with a separate, privately read transport token."""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import socket
import stat
from typing import Any, Optional

from .bridge_local_protocol import (
    LocalServiceLimits, decode_payload, open_private_directory, receive_frame,
    send_frame, validate_request,
)


_ROUTING = re.compile(r"[A-Za-z0-9_-]{24,200}", re.ASCII)
_ERROR_CODES = frozenset({
    "local_bridge_auth_denied", "local_bridge_authority_denied", "local_bridge_session_denied",
    "local_bridge_invalid_request", "local_bridge_response_too_large",
    "local_bridge_operation_denied", "local_bridge_io_failed",
})


def _credential(path: Path) -> dict:
    parent_fd = fd = -1
    try:
        if not path.is_absolute() or str(path) != os.path.abspath(str(path)):
            raise ValueError()
        parent_fd = open_private_directory(path.parent)
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or not 1 <= before.st_size <= 4096):
            raise ValueError()
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=parent_fd)
        if os.fstat(fd) != before:
            raise ValueError()
        raw = bytearray()
        while len(raw) <= 4096:
            piece = os.read(fd, 4097 - len(raw))
            if not piece:
                break
            raw.extend(piece)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or len(raw) > 4096
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError()
        value = decode_payload(bytes(raw))
        if (set(value) != {"schema_version", "socket_path", "service_incarnation", "transport_token"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["socket_path"], str)
                or any(not isinstance(value[key], str) or _ROUTING.fullmatch(value[key]) is None
                       for key in ("service_incarnation", "transport_token"))):
            raise ValueError()
        endpoint = Path(value["socket_path"])
        if (not endpoint.is_absolute() or str(endpoint) != os.path.abspath(str(endpoint))
                or endpoint.parent != path.parent or endpoint == path):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError):
        raise ValueError("local_bridge_credential_denied") from None
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


class LocalBridgeClient:
    """One explicit connection; close/failure never retries or resumes it."""

    def __init__(self, credential_file: Path):
        self._credential = _credential(Path(credential_file))
        self._socket = None
        self._session = None
        self._closed = False
        self._limits = LocalServiceLimits()

    def _connect(self) -> None:
        endpoint = Path(self._credential["socket_path"])
        parent_fd = open_private_directory(endpoint.parent)
        sock = None
        try:
            before = os.stat(endpoint.name, dir_fd=parent_fd, follow_symlinks=False)
            if (not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600):
                raise ValueError()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._limits.io_timeout_sec)
            sock.connect(str(endpoint))
            after = os.stat(endpoint.name, dir_fd=parent_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_uid, before.st_mode) != (
                    after.st_dev, after.st_ino, after.st_uid, after.st_mode):
                raise ValueError()
            request_id = secrets.token_hex(16)
            send_frame(sock, {"schema_version": 1, "request_id": request_id,
                              "type": "hello", "transport_token": self._credential["transport_token"]},
                       self._limits)
            response = receive_frame(sock, self._limits)
            if (set(response) != {"schema_version", "request_id", "state", "session_id", "service_incarnation"}
                    or type(response["schema_version"]) is not int or response["schema_version"] != 1
                    or response["request_id"] != request_id or response["state"] != "SUCCEEDED"
                    or not isinstance(response["session_id"], str)
                    or _ROUTING.fullmatch(response["session_id"]) is None
                    or response["service_incarnation"] != self._credential["service_incarnation"]):
                raise ValueError()
            self._session = response["session_id"]
            self._socket = sock
            sock = None
        finally:
            if sock is not None:
                sock.close()
            os.close(parent_fd)

    def call(self, operation: str, target: Optional[str] = None,
             max_response_bytes: int = 4096) -> dict[str, Any]:
        if self._closed:
            raise ValueError("local_bridge_client_closed")
        request = {"schema_version": 1, "request_id": secrets.token_hex(16),
                   "session_id": self._session or ("x" * 32), "operation": operation,
                   "target": target, "max_response_bytes": max_response_bytes}
        validate_request(request, self._limits)
        try:
            if self._socket is None:
                self._connect()
            request["session_id"] = self._session
            send_frame(self._socket, request, self._limits)
            receive_limits = LocalServiceLimits(max_frame_bytes=max_response_bytes,
                                                max_response_bytes=max_response_bytes)
            response = receive_frame(self._socket, receive_limits)
            keys = {"schema_version", "request_id", "session_id", "service_incarnation",
                    "operation", "state", "output_base64", "error_code"}
            if operation == "session.status" and response.get("state") == "SUCCEEDED":
                keys.add("status")
            if (set(response) != keys or type(response["schema_version"]) is not int
                    or response["schema_version"] != 1 or response["request_id"] != request["request_id"]
                    or response["session_id"] != self._session
                    or response["service_incarnation"] != self._credential["service_incarnation"]
                    or response["operation"] != operation or not isinstance(response["output_base64"], str)
                    or response["state"] not in ("SUCCEEDED", "REFUSED")
                    or (response["state"] == "SUCCEEDED" and response["error_code"] is not None)
                    or (response["state"] == "REFUSED" and (response["output_base64"] != ""
                        or response["error_code"] not in _ERROR_CODES))):
                raise ValueError()
            return response
        except (OSError, ValueError, TypeError, EOFError, TimeoutError):
            self.close()
            raise ValueError("local_bridge_call_failed") from None

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._closed = True
