"""Opt-in private socket facade for admitted lifecycle routes."""
from __future__ import annotations

from dataclasses import asdict
import os
import re
from pathlib import Path
import secrets
import selectors
import signal
import socket
import stat
import threading
import time

from .bridge_local_concurrent_service import _MAX_PEERS, _Route, _Transport
from .bridge_local_protocol import LocalServiceLimits, encode_payload, open_private_directory
from .bridge_local_lifecycle_protocol import identifier, validate_hello, validate_request
from .bridge_local_service import _DeferredStop, _ServiceStopped, _cleanup_owned, _identity, _publish_credential
from .bridge_worker_lifecycle import LifecycleAdmission, LifecycleAuthority, LifecycleResult


def _response(request, session, incarnation, *, state='SUCCEEDED', result=None, code=None):
    return dict(schema_version=4, request_id=request.get('request_id'), session_id=session,
                service_incarnation=incarnation, operation=request.get('operation'),
                operation_id=request.get('operation_id'), state=state, result=result, error_code=code)


class _LifecycleTransport(_Transport):
    def refuse(self, peer, code, *, request=None, budget=None, preserve_deadline=False):
        route = peer.route
        if route is None or (request or {}).get('type') == 'hello':
            response = dict(schema_version=4, request_id=(request or {}).get('request_id'),
                            state='REFUSED', session_id=None, service_incarnation=None)
        else:
            response = _response(request or {}, route.context.session_id, self.incarnation,
                                 state='REFUSED', code=code)
        self.queue(peer, response, budget=budget, close_after=True, preserve_deadline=preserve_deadline)

    def authority_denied(self, peer):
        if peer.route is not None:
            self.retire(peer.route)
        if peer.sent or getattr(peer, 'dispatched_mutation', False):
            self.close_peer(peer)
        else:
            self.refuse(peer, 'lifecycle_authority_denied', request=peer.request,
                        budget=peer.budget, preserve_deadline=bool(peer.output))

    def dispatch(self, peer, value):
        peer.dispatched_mutation = False
        if time.monotonic() >= peer.deadline:
            self.close_peer(peer)
            return
        if peer.route is None:
            try:
                hello = validate_hello(value)
            except (ValueError, TypeError):
                self.refuse(peer, 'lifecycle_invalid_request')
                return
            route = next((r for r in self.routes if not r.retired
                          and secrets.compare_digest(hello['transport_token'], r.token)), None)
            if route is None:
                self.refuse(peer, 'lifecycle_auth_denied', request=hello)
                return
            if route.peer is not None:
                self.close_peer(peer)
                return
            peer.route, route.peer, peer.request = route, peer, hello
            try:
                route.context.status()
                response = dict(schema_version=4, request_id=hello['request_id'], state='SUCCEEDED',
                                session_id=route.context.session_id, service_incarnation=self.incarnation)
                encode_payload(response)
                route.context.status()
            except Exception:
                self.authority_denied(peer)
                return
            self.queue(peer, response)
            return
        try:
            request = validate_request(value, self.limits)
        except (ValueError, TypeError, UnicodeError):
            budget = value.get('max_response_bytes') if isinstance(value, dict) else None
            if type(budget) is not int or not 0 <= budget <= self.limits.max_response_bytes:
                budget = None
            self.refuse(peer, 'lifecycle_invalid_request', budget=budget)
            return
        peer.request, peer.budget = request, request['max_response_bytes']
        route = peer.route.context
        if request['session_id'] != route.session_id:
            self.refuse(peer, 'lifecycle_session_denied', request=request, budget=peer.budget)
            return
        try:
            route.status()
            operation = request['operation']
            if operation == 'context':
                try:
                    result = route.context(peer.budget)
                except ValueError as exc:
                    result = None
                    code = ('lifecycle_response_too_large' if str(exc) == 'lifecycle_context_too_large'
                            else 'lifecycle_operation_denied')
                    response = _response(request, route.session_id, self.incarnation,
                                         state='REFUSED', code=code)
                else:
                    response = _response(request, route.session_id, self.incarnation, result=result)
            else:
                peer.dispatched_mutation = operation in ('claim', 'checkpoint')
                if operation == 'claim':
                    result = route.claim(request['operation_id'])
                elif operation == 'checkpoint':
                    result = route.checkpoint(request['operation_id'], request['reason'], request['process_state'])
                else:
                    result = route.disposition(request['operation_id'])
                if (not isinstance(result, LifecycleResult)
                        or result.state not in ('SUCCEEDED', 'REFUSED', 'UNKNOWN')
                        or not identifier(result.code) or result.operation_id != request['operation_id']
                        or (result.run_id is not None and (not isinstance(result.run_id, str)
                            or re.fullmatch(r'[A-Za-z0-9._-]{1,200}', result.run_id, re.ASCII) is None))
                        or (result.attempt is not None and (type(result.attempt) is not int
                            or not 1 <= result.attempt <= 2**31 - 1))):
                    raise ValueError('lifecycle_invalid_result')
                response = _response(request, route.session_id, self.incarnation, result=asdict(result))
            if len(encode_payload(response)) > min(peer.budget, self.limits.max_response_bytes, self.limits.max_frame_bytes):
                if peer.dispatched_mutation:
                    self.close_peer(peer)
                    return
                response = _response(request, route.session_id, self.incarnation,
                                     state='REFUSED', code='lifecycle_response_too_large')
            if len(encode_payload(response)) > min(peer.budget, self.limits.max_response_bytes, self.limits.max_frame_bytes):
                self.close_peer(peer)
                return
            route.status()
        except Exception:
            self.authority_denied(peer)
            return
        self.queue(peer, response, budget=peer.budget)


def serve_local_lifecycle(authority: LifecycleAuthority, admissions: tuple[LifecycleAdmission, ...],
                          socket_path: Path, limits: LocalServiceLimits) -> int:
    """Serve explicitly admitted grants, leaving native authority in-process."""
    parent_fd = -1
    parent_identity = socket_identity = None
    listener = selector = transport = None
    credentials, contexts, handlers = {}, [], {}
    stop = _DeferredStop()
    result = 0
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                with stop.defer():
                    handlers[sig] = signal.signal(sig, stop)
        if (not isinstance(authority, LifecycleAuthority) or not isinstance(limits, LocalServiceLimits)
                or type(admissions) is not tuple or not 1 <= len(admissions) <= 3
                or any(not isinstance(a, LifecycleAdmission) for a in admissions)
                or len({a.grant_id for a in admissions}) != len(admissions)):
            raise ValueError('lifecycle_endpoint_denied')
        socket_path = Path(socket_path)
        credential_files = tuple(Path(a.credential_file) for a in admissions)
        paths = (socket_path,) + credential_files
        if (len(set(paths)) != len(paths) or any(p.parent != socket_path.parent or not p.is_absolute()
                or str(p) != os.path.abspath(str(p)) for p in paths)):
            raise ValueError('lifecycle_endpoint_denied')
        with stop.defer():
            parent_fd = open_private_directory(socket_path.parent)
            parent_identity = _identity(os.fstat(parent_fd))
        for path in paths:
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('lifecycle_endpoint_denied')
        for admission in admissions:
            with stop.defer():
                contexts.append(authority.open_route(admission.grant_id))
        incarnation = secrets.token_urlsafe(24)
        tokens = tuple(secrets.token_urlsafe(32) for _ in contexts)
        if len(set(tokens)) != len(tokens):
            raise ValueError('lifecycle_auth_denied')
        routes = tuple(_Route(token, context, credential.name)
                       for token, context, credential in zip(tokens, contexts, credential_files))
        with stop.defer():
            selector = selectors.DefaultSelector()
        with stop.defer():
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            info = os.stat(socket_path.name, dir_fd=parent_fd, follow_symlinks=False)
            socket_identity = _identity(info)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('lifecycle_endpoint_denied')
        os.chmod(str(socket_path), 0o600, follow_symlinks=False)
        if _identity(os.stat(socket_path.name, dir_fd=parent_fd, follow_symlinks=False)) != socket_identity:
            raise ValueError('lifecycle_endpoint_denied')
        listener.listen(_MAX_PEERS)
        listener.setblocking(False)
        for credential, token in zip(credential_files, tokens):
            def owned(identity, name=credential.name):
                credentials[name] = identity
            _publish_credential(parent_fd, credential.name, dict(schema_version=1, socket_path=str(socket_path),
                service_incarnation=incarnation, transport_token=token), stop=stop, on_owned=owned)
        transport = _LifecycleTransport(selector, listener, routes, incarnation, limits,
                                        parent_fd, credentials, stop)
        transport.run()
    except _ServiceStopped:
        pass
    except Exception:
        result = 1
    finally:
        for sig in handlers:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except BaseException:
                result = 1
        if transport is not None:
            for peer in tuple(transport.peers.values()):
                transport.close_peer(peer)
            if transport.failed:
                result = 1
        for resource in (selector, *contexts, listener):
            if resource is not None:
                try:
                    resource.close()
                except BaseException:
                    result = 1
        if parent_fd >= 0:
            try:
                parent_owned = _identity(os.fstat(parent_fd)) == parent_identity
            except OSError:
                parent_owned = False
            if not parent_owned:
                result = 1
            else:
                for name, identity in [(socket_path.name, socket_identity), *tuple(credentials.items())]:
                    try:
                        _cleanup_owned(parent_fd, name, identity)
                    except BaseException:
                        result = 1
                try:
                    os.close(parent_fd)
                except BaseException:
                    result = 1
        for sig, handler in handlers.items():
            try:
                signal.signal(sig, handler)
            except BaseException:
                result = 1
    return result
