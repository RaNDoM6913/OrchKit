"""Explicit process-only protocol over the retained concurrent local transport."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import time

from .bridge_local_concurrent_service import _Transport, _serve_concurrent
from .bridge_local_protocol import LocalServiceLimits, encode_payload
from .bridge_local_process_protocol import operation_id_matches, validate_process_request
from .bridge_local_service import LocalBridgeAdmission
from .bridge_process import OwnedProcessProfile
from .bridge_process_store import ProcessOperationStore
from .worker_operations import WorkerOperationRequest


@dataclass(frozen=True)
class LocalProcessAdmission(LocalBridgeAdmission):
    profiles: tuple[OwnedProcessProfile, ...]

    def __post_init__(self):
        if (type(self.profiles) is not tuple or not 1 <= len(self.profiles) <= 64
                or any(not isinstance(profile, OwnedProcessProfile) for profile in self.profiles)
                or len({profile.profile_id for profile in self.profiles}) != len(self.profiles)):
            raise ValueError('process_invalid_profiles')


def _cursor(route, handle, offset):
    number = format(offset, 'x')
    message = (route.context.session_id + ':' + handle + ':' + number).encode('ascii')
    digest = hmac.new(route.token.encode('ascii'), message, hashlib.sha256).hexdigest()
    return number + '.' + digest


def _offset(route, handle, cursor):
    if cursor is None:
        return 0
    try:
        number, _ = cursor.split('.', 1)
        offset = int(number, 16)
        if offset > 65536 or not hmac.compare_digest(cursor, _cursor(route, handle, offset)):
            raise ValueError()
        return offset
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('process_cursor_denied') from None


class _ProcessTransport(_Transport):
    def refuse(self, peer, code, *, request=None, budget=None, preserve_deadline=False):
        if peer.route is None:
            return super().refuse(peer, code, request=request, budget=budget,
                                  preserve_deadline=preserve_deadline)
        response = {'schema_version': 2, 'request_id': 'invalid',
                    'session_id': peer.route.context.session_id,
                    'service_incarnation': self.incarnation, 'operation': None,
                    'operation_id': None, 'state': 'REFUSED', 'result': None,
                    'error_code': code}
        if request is not None and 'operation' in request:
            response.update(request_id=request['request_id'], operation=request['operation'],
                            operation_id=request['operation_id'])
        self.queue(peer, response, budget=budget, close_after=True,
                   preserve_deadline=preserve_deadline)

    def dispatch(self, peer, value):
        if peer.route is None:
            return super().dispatch(peer, value)
        if time.monotonic() >= peer.deadline:
            self.close_peer(peer)
            return
        try:
            request = validate_process_request(value, self.limits)
        except (ValueError, TypeError):
            budget = value.get('max_response_bytes')
            if type(budget) is not int or not 0 <= budget <= self.limits.max_response_bytes:
                budget = None
            self.refuse(peer, 'local_bridge_invalid_request', budget=budget)
            return
        peer.request, peer.budget = request, request['max_response_bytes']
        route = peer.route
        if request['session_id'] != route.context.session_id:
            self.refuse(peer, 'local_bridge_session_denied', request=request, budget=peer.budget)
            return
        try:
            route.context.status()
            try:
                if not operation_id_matches(route.token, route.context.session_id, request['operation_id']):
                    raise ValueError('process_operation_denied')
                offset = _offset(route, request['target'], request['cursor'])
            except ValueError as exc:
                response = self._result(request, route, 'REFUSED', None, str(exc))
            else:
                native = WorkerOperationRequest(route.context.binding, request['operation_id'],
                    request['operation'], request['target'], min(65536, peer.budget - 1024))
                if request['operation'] == 'process.start':
                    # Record ownership before a pending stop can unwind the service.
                    with self.stop.defer():
                        outcome = route.context.process_start(native)
                elif request['operation'] == 'process.poll':
                    outcome = route.context.process_poll(native, cursor=offset)
                else:
                    outcome = route.context.process_terminate(native)
                result = None
                if outcome.state == 'SUCCEEDED':
                    result = json.loads(outcome.output)
                    handle = result['handle']
                    result['cursor'] = _cursor(route, handle, result['cursor'])
                    result['next_cursor'] = _cursor(route, handle, result['next_cursor'])
                response = self._result(request, route, outcome.state, result, outcome.error_code)
            if len(encode_payload(response)) > min(peer.budget, self.limits.max_frame_bytes):
                self.close_peer(peer)
                return
            route.context.status()
        except Exception:
            self.authority_denied(peer)
            return
        self.queue(peer, response, budget=peer.budget)

    def _result(self, request, route, state, result, error_code):
        return {'schema_version': 2, 'request_id': request['request_id'],
                'session_id': route.context.session_id, 'service_incarnation': self.incarnation,
                'operation': request['operation'], 'operation_id': request['operation_id'],
                'state': state, 'result': result, 'error_code': error_code}


def serve_local_bridge_process_sessions(ledger, admissions: tuple[LocalProcessAdmission, ...],
                                       socket_path: Path, limits: LocalServiceLimits) -> int:
    """Serve frozen operator profiles; a request supplies no execution authority."""
    try:
        if (type(admissions) is not tuple or not admissions
                or any(type(admission) is not LocalProcessAdmission for admission in admissions)):
            raise ValueError('local_bridge_endpoint_denied')
        store = ProcessOperationStore.open_existing(ledger.root)
        return _serve_concurrent(ledger, admissions, socket_path, limits, process_store=store,
            profiles_by_binding={admission.binding: admission.profiles for admission in admissions},
            transport_class=_ProcessTransport)
    except Exception:
        return 1
