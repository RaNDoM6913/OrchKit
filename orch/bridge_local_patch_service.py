"""Opt-in existing-file patching over the bounded owner-thread socket transport."""
from __future__ import annotations

import base64
import hmac
from pathlib import Path
import time

from .bridge_local_broker import LocalRunContext
from .bridge_local_concurrent_service import _Transport, _serve_concurrent
from .bridge_local_patch_protocol import binding_tag, validate_patch_request
from .bridge_local_protocol import LocalServiceLimits, encode_payload
from .bridge_local_service import LocalBridgeAdmission
from .bridge_patch_intent import FilePatchReplacement
from .bridge_patch_preflight import FilePatchBaseline
from .bridge_readonly_authority import ReadOnlyAuthorityLedger
from .worker_operations import WorkerOperationRequest, WORKER_MAX_OUTPUT_BYTES


class _PatchTransport(_Transport):
    def refuse(self, peer, code, *, request=None, budget=None, preserve_deadline=False):
        if peer.route is None:
            return super().refuse(peer, code, request=request, budget=budget,
                                  preserve_deadline=preserve_deadline)
        response = dict(schema_version=3, request_id='invalid', session_id=peer.route.context.session_id,
                        service_incarnation=self.incarnation, operation=None, operation_id=None,
                        state='REFUSED', result=None, error_code=code)
        if request is not None and isinstance(request.get('request_id'), str):
            response.update(request_id=request['request_id'], operation=request.get('operation'),
                            operation_id=request.get('operation_id'))
        self.queue(peer, response, budget=budget, close_after=True, preserve_deadline=preserve_deadline)

    def dispatch(self, peer, value):
        if peer.route is None:
            return super().dispatch(peer, value)
        if time.monotonic() >= peer.deadline:
            self.close_peer(peer); return
        try:
            request = validate_patch_request(value, self.limits)
        except (ValueError, TypeError):
            budget = value.get('max_response_bytes')
            if type(budget) is not int or not 0 <= budget <= self.limits.max_response_bytes:
                budget = None
            self.refuse(peer, 'local_bridge_invalid_request', budget=budget); return
        peer.request, peer.budget = request, request['max_response_bytes']
        route = peer.route
        if request['session_id'] != route.context.session_id:
            self.refuse(peer, 'local_bridge_session_denied', request=request, budget=peer.budget); return
        try:
            route.context.status()
            tag = binding_tag(route.context.binding)
            operation_id = request.get('operation_id')
            if operation_id is not None and not hmac.compare_digest(operation_id[:64], tag):
                result = dict(status='REFUSED', operation_id=operation_id, historical=False,
                              retry_allowed=False, error_code='patch_scope_denied')
            elif request['operation'] == 'patch.info':
                result = {'scope_tag': tag}
            else:
                native = WorkerOperationRequest(route.context.binding, operation_id, 'file.patch',
                                                request['target'], WORKER_MAX_OUTPUT_BYTES)
                if request['operation'] == 'file.patch':
                    baseline = FilePatchBaseline(**request['baseline'])
                    try:
                        replacement = FilePatchReplacement(baseline, base64.b64decode(request['replacement_base64'], validate=True))
                    except ValueError:
                        result = dict(status='REFUSED', operation_id=operation_id, historical=False,
                                      retry_allowed=False, error_code='patch_invalid_replacement')
                    else:
                        with self.stop.defer():
                            result = route.context.patch_apply(native, replacement)
                else:
                    with self.stop.defer():
                        result = route.context.patch_reconcile(native)
            response = dict(schema_version=3, request_id=request['request_id'],
                session_id=route.context.session_id, service_incarnation=self.incarnation,
                operation=request['operation'], operation_id=operation_id, state='SUCCEEDED',
                result=result, error_code=None)
            if len(encode_payload(response)) > min(peer.budget, self.limits.max_frame_bytes):
                self.close_peer(peer); return
            route.context.status()
        except Exception:
            self.authority_denied(peer); return
        self.queue(peer, response, budget=peer.budget)


def serve_local_bridge_patch_sessions(ledger, admissions: tuple[LocalBridgeAdmission, ...],
                                      socket_path: Path, limits: LocalServiceLimits) -> int:
    """Start an explicit patch-only service using trusted native admissions."""
    try:
        if (type(ledger) is not ReadOnlyAuthorityLedger
                or type(admissions) is not tuple or not admissions
                or any(type(item) is not LocalBridgeAdmission for item in admissions)):
            raise ValueError('local_bridge_endpoint_denied')
        return _serve_concurrent(ledger, admissions, socket_path, limits,
                                 transport_class=_PatchTransport, patch_enabled=True)
    except Exception:
        return 1
