"""Strict opt-in process wire schema; v1 read-only requests stay unchanged."""
from __future__ import annotations

import re
import hashlib
import hmac
from .bridge_local_protocol import LocalServiceLimits

_IDENTIFIER = re.compile(r'[A-Za-z0-9._-]{1,200}', re.ASCII)
_ROUTING = re.compile(r'[A-Za-z0-9_-]{24,200}', re.ASCII)
_OPERATION_ID = re.compile(r'[0-9a-f]{64}\.[0-9a-f]{64}', re.ASCII)
_CURSOR = re.compile(r'[0-9a-f]{1,5}\.[0-9a-f]{64}', re.ASCII)
PROCESS_OPERATIONS = frozenset(('process.start', 'process.poll', 'process.terminate'))


def validate_process_request(value: dict, limits: LocalServiceLimits) -> dict:
    if (not isinstance(value, dict) or set(value) != {
            'schema_version', 'request_id', 'session_id', 'operation_id', 'operation',
            'target', 'cursor', 'max_response_bytes'}
            or type(value['schema_version']) is not int or value['schema_version'] != 2
            or any(not isinstance(value[k], str) or _IDENTIFIER.fullmatch(value[k]) is None
                   for k in ('request_id', 'target'))
            or not isinstance(value['operation_id'], str)
            or _OPERATION_ID.fullmatch(value['operation_id']) is None
            or not isinstance(value['session_id'], str)
            or _ROUTING.fullmatch(value['session_id']) is None
            or not isinstance(value['operation'], str) or value['operation'] not in PROCESS_OPERATIONS
            or type(value['max_response_bytes']) is not int
            or not limits.min_response_bytes <= value['max_response_bytes'] <= limits.max_response_bytes
            or (value['operation'] != 'process.poll' and value['cursor'] is not None)
            or (value['operation'] == 'process.poll' and value['cursor'] is not None
                and (not isinstance(value['cursor'], str) or _CURSOR.fullmatch(value['cursor']) is None))):
        raise ValueError('local_bridge_invalid_request')
    return value


def sign_operation_id(token: str, session_id: str, nonce: str) -> str:
    message = (session_id + ':' + nonce).encode('ascii')
    digest = hmac.new(token.encode('ascii'), message, hashlib.sha256).hexdigest()
    return nonce + '.' + digest


def operation_id_matches(token: str, session_id: str, operation_id: str) -> bool:
    try:
        nonce, _ = operation_id.split('.', 1)
        return hmac.compare_digest(operation_id, sign_operation_id(token, session_id, nonce))
    except (ValueError, TypeError, UnicodeError):
        return False
