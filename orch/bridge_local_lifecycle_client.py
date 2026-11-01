"""Explicit lifecycle client: no mutation replay on transport failure."""
from __future__ import annotations

import os
import re
from pathlib import Path
import secrets
import socket
import stat

from .bridge_local_client import _credential
from .bridge_local_lifecycle_protocol import identifier, routing, validate_request
from .bridge_local_protocol import LocalServiceLimits, open_private_directory, receive_frame, send_frame
from .bridge_worker_lifecycle import LifecycleResult

_ERRORS = frozenset(('lifecycle_invalid_request', 'lifecycle_auth_denied',
                     'lifecycle_authority_denied', 'lifecycle_session_denied',
                     'lifecycle_response_too_large', 'lifecycle_operation_denied'))


class LifecycleClient:
    def __init__(self, credential_file: Path):
        self._credential = _credential(Path(credential_file))
        self._limits = LocalServiceLimits()
        self._socket = None
        self._session = None
        self._closed = False

    def _connect(self):
        endpoint = Path(self._credential['socket_path'])
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
            send_frame(sock, dict(schema_version=4, request_id=request_id, type='hello',
                                  transport_token=self._credential['transport_token']), self._limits)
            response = receive_frame(sock, self._limits)
            if (set(response) != {'schema_version', 'request_id', 'state', 'session_id', 'service_incarnation'}
                    or type(response['schema_version']) is not int or response['schema_version'] != 4
                    or response['request_id'] != request_id or response['state'] != 'SUCCEEDED'
                    or not routing(response['session_id'])
                    or response['service_incarnation'] != self._credential['service_incarnation']):
                raise ValueError()
            self._session = response['session_id']
            self._socket, sock = sock, None
        finally:
            if sock is not None:
                sock.close()
            os.close(parent_fd)

    def _call(self, operation, max_response_bytes, **fields):
        if self._closed:
            raise ValueError('lifecycle_client_closed')
        request = dict(schema_version=4, request_id=secrets.token_hex(16),
                       session_id=self._session or 'x' * 32, operation=operation,
                       max_response_bytes=max_response_bytes, **fields)
        validate_request(request, self._limits)
        try:
            if self._socket is None:
                self._connect()
            request['session_id'] = self._session
            send_frame(self._socket, request, self._limits)
            receive_limits = LocalServiceLimits(max_frame_bytes=max_response_bytes,
                min_response_bytes=1, max_response_bytes=max_response_bytes)
            response = receive_frame(self._socket, receive_limits)
            if (set(response) != {'schema_version', 'request_id', 'session_id', 'service_incarnation',
                                 'operation', 'operation_id', 'state', 'result', 'error_code'}
                    or type(response['schema_version']) is not int or response['schema_version'] != 4
                    or response['request_id'] != request['request_id']
                    or response['session_id'] != self._session
                    or response['service_incarnation'] != self._credential['service_incarnation']
                    or response['operation'] != operation or response['operation_id'] != fields.get('operation_id')
                    or response['state'] not in ('SUCCEEDED', 'REFUSED')
                    or (response['state'] == 'REFUSED' and
                        (response['result'] is not None or response['error_code'] not in _ERRORS))
                    or (response['state'] == 'SUCCEEDED' and response['error_code'] is not None)):
                raise ValueError()
            if response['state'] == 'REFUSED':
                return response
            result = response['result']
            if operation == 'context':
                if type(result) is not dict:
                    raise ValueError()
            else:
                if (type(result) is not dict or set(result) != {'state', 'code', 'operation_id', 'run_id', 'attempt'}
                        or result['state'] not in ('SUCCEEDED', 'REFUSED', 'UNKNOWN')
                        or not identifier(result['code']) or result['operation_id'] != fields['operation_id']
                        or (result['run_id'] is not None and (not isinstance(result['run_id'], str)
                            or re.fullmatch(r'[A-Za-z0-9._-]{1,200}', result['run_id'], re.ASCII) is None))
                        or (result['attempt'] is not None and
                            (type(result['attempt']) is not int or not 1 <= result['attempt'] <= 2**31 - 1))):
                    raise ValueError()
            return response
        except (OSError, ValueError, TypeError, EOFError, TimeoutError):
            self.close()
            raise ValueError('lifecycle_call_unknown') from None

    def _mutation(self, operation, operation_id, max_response_bytes, **fields):
        try:
            response = self._call(operation, max_response_bytes, operation_id=operation_id, **fields)
        except ValueError as exc:
            if str(exc) == 'lifecycle_call_unknown':
                return LifecycleResult('UNKNOWN', 'lifecycle_call_unknown', operation_id, None, None)
            raise
        if response['state'] == 'REFUSED':
            return LifecycleResult('REFUSED', response['error_code'], operation_id, None, None)
        return LifecycleResult(**response['result'])

    def claim(self, operation_id: str, *, max_response_bytes: int = 4096) -> LifecycleResult:
        return self._mutation('claim', operation_id, max_response_bytes)

    def context(self, *, max_response_bytes: int = 65536) -> dict:
        response = self._call('context', max_response_bytes)
        if response['state'] != 'SUCCEEDED':
            raise ValueError(response['error_code'])
        return response['result']

    def checkpoint(self, operation_id: str, reason: str, process_state: str,
                   *, max_response_bytes: int = 4096) -> LifecycleResult:
        return self._mutation('checkpoint', operation_id, max_response_bytes,
                              reason=reason, process_state=process_state)

    def disposition(self, operation_id: str, *, max_response_bytes: int = 4096) -> LifecycleResult:
        return self._mutation('disposition', operation_id, max_response_bytes)

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._closed = True
