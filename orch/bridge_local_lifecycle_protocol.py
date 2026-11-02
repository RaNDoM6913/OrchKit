"""Strict request validation for the opt-in lifecycle protocol."""
from __future__ import annotations

import re
from .bridge_local_protocol import LocalServiceLimits

SCHEMA_VERSION = 4
OPERATIONS = frozenset(('claim', 'context', 'checkpoint', 'disposition'))
_IDENTIFIER = re.compile(r'[A-Za-z0-9._-]{1,128}', re.ASCII)
_ROUTING = re.compile(r'[A-Za-z0-9_-]{24,200}', re.ASCII)


def identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def routing(value):
    return isinstance(value, str) and _ROUTING.fullmatch(value) is not None


def _base(value, keys):
    if (type(value) is not dict or set(value) != keys
            or type(value.get('schema_version')) is not int
            or value['schema_version'] != SCHEMA_VERSION
            or not identifier(value.get('request_id'))):
        raise ValueError('lifecycle_invalid_request')


def validate_hello(value):
    _base(value, {'schema_version', 'request_id', 'type', 'transport_token'})
    if value['type'] != 'hello' or not routing(value['transport_token']):
        raise ValueError('lifecycle_invalid_request')
    return value


def validate_request(value, limits: LocalServiceLimits):
    if type(value) is not dict:
        raise ValueError('lifecycle_invalid_request')
    operation = value.get('operation')
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError('lifecycle_invalid_request')
    keys = {'schema_version', 'request_id', 'session_id', 'operation', 'max_response_bytes'}
    if operation in ('claim', 'disposition', 'checkpoint'):
        keys.add('operation_id')
    if operation == 'checkpoint':
        keys.update(('reason', 'process_state'))
    _base(value, keys)
    if (not routing(value['session_id'])
            or type(value['max_response_bytes']) is not int
            or not limits.min_response_bytes <= value['max_response_bytes'] <= limits.max_response_bytes
            or ('operation_id' in value and not identifier(value['operation_id']))):
        raise ValueError('lifecycle_invalid_request')
    if operation == 'checkpoint':
        reason = value['reason']
        if (not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500
                or len(reason.strip().encode('utf-8')) > 2000
                or value['process_state'] not in ('active', 'unknown')):
            raise ValueError('lifecycle_invalid_request')
    return value
