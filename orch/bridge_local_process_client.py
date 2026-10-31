"""Explicit process client; failure never retries or resumes a call."""
from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path
from typing import Any

from .bridge_local_client import LocalBridgeClient
from .bridge_local_process_protocol import sign_operation_id, validate_process_request
from .bridge_local_protocol import LocalServiceLimits, receive_frame, send_frame

_IDENTIFIER = re.compile(r'[A-Za-z0-9._-]{1,200}', re.ASCII)
_CURSOR = re.compile(r'[0-9a-f]{1,5}\.[0-9a-f]{64}', re.ASCII)


class LocalProcessClient(LocalBridgeClient):
    def __init__(self, credential_file: Path):
        super().__init__(credential_file)

    def new_operation_id(self) -> str:
        if self._closed:
            raise ValueError('local_bridge_client_closed')
        try:
            if self._socket is None:
                self._connect()
            return sign_operation_id(self._credential['transport_token'], self._session,
                                     secrets.token_hex(32))
        except (OSError, ValueError, TypeError, EOFError, TimeoutError):
            self.close()
            raise ValueError('local_bridge_call_failed') from None

    def call(self, operation: str, target: str, *, operation_id: str,
             cursor: str | None = None, max_response_bytes: int = 4096) -> dict[str, Any]:
        if self._closed:
            raise ValueError('local_bridge_client_closed')
        request = {'schema_version': 2, 'request_id': secrets.token_hex(16),
                   'session_id': self._session or ('x' * 32), 'operation_id': operation_id,
                   'operation': operation, 'target': target, 'cursor': cursor,
                   'max_response_bytes': max_response_bytes}
        validate_process_request(request, self._limits)
        try:
            if self._socket is None:
                self._connect()
            request['session_id'] = self._session
            send_frame(self._socket, request, self._limits)
            response = receive_frame(self._socket, LocalServiceLimits(
                max_frame_bytes=max_response_bytes, max_response_bytes=max_response_bytes))
            if (set(response) != {'schema_version', 'request_id', 'session_id', 'service_incarnation',
                                  'operation', 'operation_id', 'state', 'result', 'error_code'}
                    or type(response['schema_version']) is not int or response['schema_version'] != 2
                    or any(response[key] != request[key] for key in
                           ('request_id', 'session_id', 'operation', 'operation_id'))
                    or response['service_incarnation'] != self._credential['service_incarnation']
                    or response['state'] not in ('SUCCEEDED', 'REFUSED', 'UNKNOWN', 'FAILED')):
                raise ValueError()
            if response['state'] == 'SUCCEEDED':
                result = response['result']
                keys = {'schema_version', 'handle', 'process_state', 'exit_code', 'reason', 'cursor',
                        'next_cursor', 'output_base64', 'total_output_bytes', 'retained_output_bytes',
                        'truncated', 'has_more'}
                if (response['error_code'] is not None or not isinstance(result, dict)
                        or set(result) != keys or type(result['schema_version']) is not int
                        or result['schema_version'] != 1 or not isinstance(result['handle'], str)
                        or _IDENTIFIER.fullmatch(result['handle']) is None
                        or result['process_state'] not in ('RUNNING', 'EXITED')
                        or any(not isinstance(result[k], str) or _CURSOR.fullmatch(result[k]) is None
                               for k in ('cursor', 'next_cursor'))
                        or any(type(result[k]) is not int or not 0 <= result[k] for k in
                               ('total_output_bytes', 'retained_output_bytes'))
                        or any(type(result[k]) is not bool for k in ('truncated', 'has_more'))
                        or (result['exit_code'] is not None and type(result['exit_code']) is not int)
                        or (result['reason'] is not None and not isinstance(result['reason'], str))
                        or not isinstance(result['output_base64'], str)):
                    raise ValueError()
                base64.b64decode(result['output_base64'], validate=True)
            elif (response['result'] is not None or not isinstance(response['error_code'], str)
                  or _IDENTIFIER.fullmatch(response['error_code']) is None):
                raise ValueError()
            return response
        except (OSError, ValueError, TypeError, EOFError, TimeoutError):
            self.close()
            raise ValueError('local_bridge_call_failed') from None
