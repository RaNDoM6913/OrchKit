"""Strict opt-in v3 patch messages and stable, non-authorizing scope tags."""
from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
import hashlib
import json
import re

from .bridge_local_protocol import LocalServiceLimits
from .bridge_patch_preflight import FilePatchBaseline
from .worker_operations import WorkerOperationRequest, WorkerRunBinding, WORKER_MAX_OUTPUT_BYTES

_ID = re.compile(r'[A-Za-z0-9._-]{1,200}', re.ASCII)
_ROUTE = re.compile(r'[A-Za-z0-9_-]{24,200}', re.ASCII)
_OPERATION = re.compile(r'[0-9a-f]{64}\.[0-9a-f]{64}', re.ASCII)


def binding_tag(binding: WorkerRunBinding) -> str:
    if not isinstance(binding, WorkerRunBinding):
        raise ValueError('local_patch_invalid_binding')
    encoded = json.dumps(asdict(binding), sort_keys=True, ensure_ascii=True,
                         separators=(',', ':')).encode('ascii')
    return hashlib.sha256(b'orchkit.patch.scope.v1\0' + encoded).hexdigest()


def validate_patch_request(value: dict, limits: LocalServiceLimits) -> dict:
    try:
        if not isinstance(value, dict): raise ValueError()
        op = value.get('operation')
        common = {'schema_version', 'request_id', 'session_id', 'operation', 'max_response_bytes'}
        extra = {'patch.info': set(), 'file.patch': {'operation_id', 'target', 'baseline', 'replacement_base64'},
                 'file.patch.reconcile': {'operation_id', 'target'}}
        if (not isinstance(op, str) or op not in extra or set(value) != common | extra[op]
            or type(value['schema_version']) is not int or value['schema_version'] != 3
            or not isinstance(value['request_id'], str) or _ID.fullmatch(value['request_id']) is None
            or not isinstance(value['session_id'], str) or _ROUTE.fullmatch(value['session_id']) is None
            or type(value['max_response_bytes']) is not int
            or not limits.min_response_bytes <= value['max_response_bytes'] <= limits.max_response_bytes):
            raise ValueError()
        if op != 'patch.info':
            if not isinstance(value['operation_id'], str) or _OPERATION.fullmatch(value['operation_id']) is None:
                raise ValueError()
            WorkerOperationRequest(WorkerRunBinding('local', 'local', 'local', 'local', 1),
                                   value['operation_id'], 'file.patch', value['target'])
        if op == 'file.patch':
            baseline = value['baseline']
            if not isinstance(baseline, dict) or set(baseline) != {'sha256', 'size_bytes'}:
                raise ValueError()
            FilePatchBaseline(baseline['sha256'], baseline['size_bytes'])
            encoded = value['replacement_base64']
            if not isinstance(encoded, str) or len(encoded) > ((WORKER_MAX_OUTPUT_BYTES + 2) // 3) * 4:
                raise ValueError()
            data = base64.b64decode(encoded, validate=True)
            if len(data) > WORKER_MAX_OUTPUT_BYTES or base64.b64encode(data).decode('ascii') != encoded:
                raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise ValueError('local_bridge_invalid_request') from None
