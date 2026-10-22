"""One foreground, authenticated Unix socket for existing read-only adapters.

The service owns volatile routing and transport objects only. Native writer
completion and lifecycle remain independent of connection and service exits.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import threading
from typing import Callable

from .bridge_authorization import LocalRunSessionAuthority
from .bridge_file_list import LocalBoundedFileLister
from .bridge_file_read import LocalBoundedFileReader
from .bridge_file_scope import LocalFileScope
from .bridge_local_protocol import (
    LOCAL_OPERATIONS, LocalServiceLimits, encode_payload, native_payload_budget,
    open_private_directory, receive_frame, send_frame, validate_hello,
    validate_request,
)
from .bridge_readonly_authority import ReadOnlyAuthorityLedger
from .worker_operations import WorkerOperationRequest, WorkerRunBinding


class _ServiceStopped(BaseException):
    pass


class _DeferredStop:
    """Defer the handler exception only until resource ownership is recorded."""

    def __init__(self):
        self._depth = 0
        self._requested = False

    def __call__(self, _signum: int, _frame) -> None:
        if self._depth:
            self._requested = True
        else:
            raise _ServiceStopped()

    @contextmanager
    def defer(self):
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and self._requested:
                self._requested = False
                raise _ServiceStopped()


def _identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IFMT(info.st_mode)


def _cleanup_owned(parent_fd: int, name: str, identity: tuple) -> None:
    if identity is None:
        return
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(current) == identity:
        os.unlink(name, dir_fd=parent_fd)


def _publish_credential(parent_fd: int, name: str, value: dict, *,
                        stop: _DeferredStop = None,
                        on_owned: Callable[[tuple], None] = None) -> tuple:
    stop = stop if stop is not None else _DeferredStop()
    fd = -1
    identity = None
    succeeded = False
    try:
        with stop.defer():
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent_fd)
            identity = _identity(os.fstat(fd))
            if on_owned is not None:
                on_owned(identity)
        os.fchmod(fd, 0o600)
        payload = encode_payload(value)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("local_bridge_credential_denied")
            offset += written
        os.fsync(fd)
        succeeded = True
        return identity
    finally:
        with stop.defer():
            if fd >= 0:
                os.close(fd)
            if not succeeded:
                _cleanup_owned(parent_fd, name, identity)


def _refusal(code: str, *, request: dict = None, session_id: str = None,
             incarnation: str = None) -> dict:
    value = {"schema_version": 1, "request_id": "invalid", "state": "REFUSED",
             "output_base64": "", "error_code": code}
    if request is not None:
        value["request_id"] = request["request_id"]
        if "operation" in request:
            value["operation"] = request["operation"]
    if session_id is not None:
        value["session_id"] = session_id
        value["service_incarnation"] = incarnation
    return value


def _safe_send(sock: socket.socket, value: dict, limits: LocalServiceLimits,
               budget: int = None) -> None:
    try:
        send_frame(sock, value, limits, budget=budget)
    except (OSError, ValueError, TimeoutError):
        # A refused/failed send ends the connection; never retry the call.
        pass


def _response(request: dict, session_id: str, incarnation: str) -> dict:
    return {"schema_version": 1, "request_id": request["request_id"],
            "session_id": session_id, "service_incarnation": incarnation,
            "operation": request["operation"], "state": "SUCCEEDED",
            "output_base64": "", "error_code": None}


def _status(ledger: ReadOnlyAuthorityLedger, binding: WorkerRunBinding,
            incarnation: str, limits: LocalServiceLimits) -> dict:
    with ledger.connect() as conn:
        row = conn.execute(
            "SELECT r.state AS run_state, t.status AS task_state "
            "FROM runs r JOIN tasks t ON t.task_id=r.task_id WHERE r.run_id=?",
            (binding.run_id,),
        ).fetchone()
    if row is None or row["run_state"] != "RUNNING" or row["task_state"] != "IN_PROGRESS":
        raise ValueError("local_bridge_authority_denied")
    return {"binding": asdict(binding), "native": {"run_state": row["run_state"],
            "task_state": row["task_state"]}, "service_state": "SERVING",
            "service_incarnation": incarnation, "supported_operations": list(LOCAL_OPERATIONS),
            "limits": asdict(limits)}


def _dispatch(request: dict, ledger: ReadOnlyAuthorityLedger,
              binding: WorkerRunBinding, session_id: str, incarnation: str,
              limits: LocalServiceLimits, reader: LocalBoundedFileReader,
              lister: LocalBoundedFileLister) -> dict:
    response = _response(request, session_id, incarnation)
    budget = request["max_response_bytes"]
    if request["operation"] == "session.status":
        response["status"] = _status(ledger, binding, incarnation, limits)
    else:
        raw_budget = native_payload_budget(response, budget)
        if raw_budget < 1:
            return _refusal("local_bridge_response_too_large", request=request,
                            session_id=session_id, incarnation=incarnation)
        operation = WorkerOperationRequest(
            binding, request["request_id"], request["operation"], request["target"], raw_budget,
        )
        outcome = reader.read(operation) if operation.kind == "file.read" else lister.list(operation)
        if outcome.state != "SUCCEEDED":
            code = ("local_bridge_response_too_large" if outcome.error_code in
                    ("file_read_too_large", "file_list_too_large") else
                    "local_bridge_io_failed" if outcome.state == "FAILED" else
                    "local_bridge_operation_denied")
            return _refusal(code, request=request, session_id=session_id, incarnation=incarnation)
        response["output_base64"] = base64.b64encode(outcome.output).decode("ascii")
    if len(encode_payload(response)) > budget:
        return _refusal("local_bridge_response_too_large", request=request,
                        session_id=session_id, incarnation=incarnation)
    return response


def _serve_connection(sock: socket.socket, ledger: ReadOnlyAuthorityLedger,
                      binding: WorkerRunBinding, capability_file: Path,
                      authority: LocalRunSessionAuthority, session_id: str,
                      incarnation: str, token: str, limits: LocalServiceLimits,
                      reader: LocalBoundedFileReader, lister: LocalBoundedFileLister) -> bool:
    hello = None
    try:
        hello = validate_hello(receive_frame(sock, limits))
    except (EOFError, OSError, TimeoutError):
        return True
    except (ValueError, TypeError):
        _safe_send(sock, _refusal("local_bridge_invalid_request"), limits)
        return True
    if not secrets.compare_digest(hello["transport_token"], token):
        _safe_send(sock, _refusal("local_bridge_auth_denied", request=hello), limits)
        return True
    try:
        authority.require_session(session_id, binding, capability_file)
    except Exception:
        _safe_send(sock, _refusal("local_bridge_authority_denied", request=hello), limits)
        return False
    try:
        send_frame(sock, {"schema_version": 1, "request_id": hello["request_id"],
                          "state": "SUCCEEDED", "session_id": session_id,
                          "service_incarnation": incarnation}, limits)
    except (OSError, ValueError, TimeoutError):
        return True

    while True:
        request = None
        try:
            request = receive_frame(sock, limits)
            validate_request(request, limits)
        except (EOFError, OSError, TimeoutError):
            return True
        except (ValueError, TypeError):
            # Preserve a syntactically usable requested budget even when the
            # request itself is refused; tiny budgets may require EOF only.
            refused_budget = None
            if (request is not None and type(request.get("max_response_bytes")) is int
                    and 0 <= request["max_response_bytes"] <= limits.max_response_bytes):
                refused_budget = request["max_response_bytes"]
            _safe_send(sock, _refusal("local_bridge_invalid_request", session_id=session_id,
                                     incarnation=incarnation), limits, refused_budget)
            return True
        budget = request["max_response_bytes"]
        if request["session_id"] != session_id:
            _safe_send(sock, _refusal("local_bridge_session_denied", request=request,
                                     session_id=session_id, incarnation=incarnation), limits, budget)
            return True
        try:
            authority.require_session(session_id, binding, capability_file)
            response = _dispatch(request, ledger, binding, session_id, incarnation, limits, reader, lister)
            # Buffered content cannot leave on an authority check made before I/O.
            authority.require_session(session_id, binding, capability_file)
        except Exception:
            _safe_send(sock, _refusal("local_bridge_authority_denied", request=request,
                                     session_id=session_id, incarnation=incarnation), limits, budget)
            return False
        try:
            send_frame(sock, response, limits, budget=budget)
        except (OSError, ValueError, TimeoutError):
            return True


def serve_local_bridge(ledger: ReadOnlyAuthorityLedger, binding: WorkerRunBinding,
                       capability_file: Path, socket_path: Path, credential_file: Path,
                       limits: LocalServiceLimits) -> int:
    """Serve serially until stop/idle/failure; never advance the native writer."""
    parent_fd = -1
    listener = None
    scope = None
    session_id = None
    socket_identity = credential_identity = None
    stop = _DeferredStop()
    old_handlers = {}
    result = 1
    socket_path, credential_file = Path(socket_path), Path(credential_file)
    authority = LocalRunSessionAuthority(ledger)
    try:
        # Handle stop before acquiring scope/socket/credential ownership, so
        # a stop during setup unwinds through the same cleanup as a live call.
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signum] = signal.signal(signum, stop)
        if not isinstance(limits, LocalServiceLimits) or socket_path.parent != credential_file.parent or socket_path == credential_file:
            raise ValueError("local_bridge_endpoint_denied")
        parent_fd = open_private_directory(socket_path.parent)
        for path in (socket_path, credential_file):
            if not path.is_absolute() or str(path) != os.path.abspath(str(path)):
                raise ValueError("local_bridge_endpoint_denied")
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("local_bridge_endpoint_denied")
        session_id = authority.open_session(binding, capability_file)
        scope = LocalFileScope(authority, session_id, binding, capability_file)
        reader, lister = LocalBoundedFileReader(scope), LocalBoundedFileLister(scope)
        incarnation, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        with stop.defer():
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            info = os.stat(socket_path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("local_bridge_endpoint_denied")
            socket_identity = _identity(info)
        os.chmod(str(socket_path), 0o600, follow_symlinks=False)
        if _identity(os.stat(socket_path.name, dir_fd=parent_fd, follow_symlinks=False)) != socket_identity:
            raise ValueError("local_bridge_endpoint_denied")
        listener.listen(1)
        listener.settimeout(limits.idle_timeout_sec)

        def credential_owned(identity: tuple) -> None:
            nonlocal credential_identity
            credential_identity = identity

        credential_identity = _publish_credential(parent_fd, credential_file.name, {
            "schema_version": 1, "socket_path": str(socket_path),
            "service_incarnation": incarnation, "transport_token": token,
        }, stop=stop, on_owned=credential_owned)
        result = 0
        while True:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                break
            with connection:
                if not _serve_connection(connection, ledger, binding, capability_file,
                                         authority, session_id, incarnation, token, limits, reader, lister):
                    result = 1
                    break
    except _ServiceStopped:
        result = 0
    except Exception:
        result = 1
    finally:
        for signum in old_handlers:
            signal.signal(signum, signal.SIG_IGN)
        # Scope contexts unwind before the pinned root and volatile authority.
        try:
            if scope is not None:
                scope.close()
        except Exception:
            result = 1
        try:
            if session_id is not None:
                authority.close_session(session_id)
        except Exception:
            result = 1
        if listener is not None:
            listener.close()
        try:
            if parent_fd >= 0:
                _cleanup_owned(parent_fd, socket_path.name, socket_identity)
                _cleanup_owned(parent_fd, credential_file.name, credential_identity)
        except OSError:
            result = 1
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
        for signum, old_handler in old_handlers.items():
            signal.signal(signum, old_handler)
    return result
