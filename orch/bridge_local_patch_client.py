"""Explicit v3 patch calls; failures never retry or infer a disposition."""
from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path

from .bridge_local_client import LocalBridgeClient
from .bridge_local_patch_protocol import validate_patch_request
from .bridge_local_protocol import LocalServiceLimits, encode_payload, receive_frame, send_frame

_HEX64 = re.compile(r'[0-9a-f]{64}', re.ASCII)
_CODE = re.compile(r'[A-Za-z0-9._-]{1,200}', re.ASCII)


class LocalPatchClient(LocalBridgeClient):
    def __init__(self, credential_file: Path):
        super().__init__(credential_file)

    def info(self):
        return self._call('patch.info')['scope_tag']

    def new_operation_id(self):
        return self.info() + '.' + secrets.token_hex(32)

    def apply(self, target, *, baseline_sha256, baseline_size, replacement, operation_id,
              max_response_bytes=4096):
        if not isinstance(replacement, bytes):
            raise ValueError('local_bridge_invalid_request')
        return self._call('file.patch', operation_id=operation_id, target=target,
            baseline={'sha256': baseline_sha256, 'size_bytes': baseline_size},
            replacement_base64=base64.b64encode(replacement).decode('ascii'),
            max_response_bytes=max_response_bytes)

    def reconcile(self, target, *, operation_id, max_response_bytes=4096):
        return self._call('file.patch.reconcile', operation_id=operation_id, target=target,
                          max_response_bytes=max_response_bytes)

    def _call(self, operation, *, max_response_bytes=4096, **fields):
        if self._closed:
            raise ValueError('local_bridge_client_closed')
        request = dict(schema_version=3, request_id=secrets.token_hex(16),
                       session_id=self._session or 'x' * 32, operation=operation,
                       max_response_bytes=max_response_bytes, **fields)
        validate_patch_request(request, self._limits)
        if len(encode_payload(request)) > self._limits.max_frame_bytes:
            raise ValueError('local_bridge_invalid_request')
        try:
            if self._socket is None: self._connect()
            request['session_id'] = self._session
            send_frame(self._socket, request, self._limits)
            response = receive_frame(self._socket, LocalServiceLimits(
                max_frame_bytes=max_response_bytes, max_response_bytes=max_response_bytes))
            if (set(response) != {'schema_version', 'request_id', 'session_id', 'service_incarnation',
                                  'operation', 'operation_id', 'state', 'result', 'error_code'}
                    or type(response['schema_version']) is not int or response['schema_version'] != 3
                    or any(response[k] != request.get(k) for k in ('request_id', 'session_id', 'operation', 'operation_id'))
                    or response['service_incarnation'] != self._credential['service_incarnation']):
                raise ValueError()
            if response['state'] == 'REFUSED':
                if response['result'] is not None or not isinstance(response['error_code'], str) or _CODE.fullmatch(response['error_code']) is None:
                    raise ValueError()
                if operation != 'patch.info' and response['error_code'] == 'local_bridge_authority_denied':
                    self.close()
                    return dict(status='UNKNOWN', operation_id=fields['operation_id'], historical=False,
                                retry_allowed=False, error_code='patch_authority_or_outcome_unknown')
                raise ValueError()
            result = response['result']
            if response['state'] != 'SUCCEEDED' or response['error_code'] is not None or not isinstance(result, dict):
                raise ValueError()
            if operation == 'patch.info':
                if set(result) != {'scope_tag'} or not isinstance(result['scope_tag'], str) or _HEX64.fullmatch(result['scope_tag']) is None:
                    raise ValueError()
            else:
                keys = {'status', 'operation_id', 'historical', 'retry_allowed'}
                if result.get('error_code') is not None: keys.add('error_code')
                if (set(result) != keys or result['operation_id'] != fields['operation_id']
                        or result['status'] not in ('APPLIED', 'NOT_APPLIED', 'REFUSED', 'CONFLICT', 'UNKNOWN')
                        or type(result['historical']) is not bool or result['retry_allowed'] is not False
                        or ('error_code' in result and (not isinstance(result['error_code'], str)
                            or _CODE.fullmatch(result['error_code']) is None))):
                    raise ValueError()
            return result
        except (OSError, ValueError, TypeError, EOFError, TimeoutError):
            self.close()
            raise ValueError('local_patch_call_outcome_unknown' if operation != 'patch.info'
                             else 'local_bridge_call_failed') from None
