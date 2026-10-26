"""Bounded, owner-thread transport for one to three sealed local read routes.

Only transport is interleaved. Native filesystem/authority calls remain
synchronous; disconnects never complete or release a native writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import selectors
import signal
import socket
import stat
import struct
import threading
import time

from .bridge_local_broker import LocalRunBroker, LocalRunContext
from .bridge_local_protocol import (
    LocalServiceLimits, decode_payload, encode_payload, open_private_directory,
    validate_hello, validate_request,
)
from .bridge_local_service import (
    LocalBridgeAdmission, _DeferredStop, _ServiceStopped, _cleanup_owned,
    _dispatch_context, _identity, _publish_credential, _refusal,
)
from .bridge_readonly_authority import ReadOnlyAuthorityLedger
from .worker_operations import WorkerRunBinding


_IO_CHUNK = 16 * 1024
_MAX_PEERS = 3
_ACCEPT_BUDGET = 3


@dataclass(eq=False)
class _Route:
    token: str
    context: LocalRunContext
    credential_name: str
    retired: bool = False
    peer: _Peer | None = None


@dataclass(eq=False)
class _Peer:
    sock: socket.socket
    deadline: float
    route: _Route | None = None
    header: bytearray = field(default_factory=bytearray)
    body: bytearray = field(default_factory=bytearray)
    size: int | None = None
    output: bytes = b""
    sent: int = 0
    success: bool = False
    close_after: bool = False
    request: dict | None = None
    budget: int | None = None


class _Transport:
    def __init__(self, selector, listener, routes, incarnation, limits,
                 parent_fd, credential_identities, stop):
        self.selector = selector
        self.listener = listener
        self.routes = routes
        self.incarnation = incarnation
        self.limits = limits
        self.parent_fd = parent_fd
        self.credential_identities = credential_identities
        self.stop = stop
        self.peers = {}
        self.failed = False
        self.idle_deadline = time.monotonic() + limits.idle_timeout_sec

    def current(self, peer):
        return self.peers.get(peer.sock) is peer

    def close_peer(self, peer):
        with self.stop.defer():
            if not self.current(peer):
                return
            # Remove the exact object first: delayed events cannot affect a peer
            # which later owns the same descriptor or context reservation.
            del self.peers[peer.sock]
            if peer.route is not None and peer.route.peer is peer:
                peer.route.peer = None
            try:
                key = self.selector.get_key(peer.sock)
                if key.data is peer:
                    self.selector.unregister(peer.sock)
            except KeyError:
                pass
            except BaseException:
                self.failed = True
            try:
                peer.sock.close()
            except BaseException:
                self.failed = True
            if not self.peers:
                self.idle_deadline = time.monotonic() + self.limits.idle_timeout_sec

    def retire(self, route):
        with self.stop.defer():
            if route.retired:
                return
            # Retirement is permanent, including if native authority is restored.
            route.retired = True
            self.failed = True
            try:
                route.context.close()
            except BaseException:
                pass
            identity = self.credential_identities.get(route.credential_name)
            try:
                _cleanup_owned(self.parent_fd, route.credential_name, identity)
            except BaseException:
                pass  # The final independent cleanup retains this owned identity.
            else:
                self.credential_identities.pop(route.credential_name, None)

    def queue(self, peer, value, *, budget=None, close_after=False,
              preserve_deadline=False):
        try:
            payload = encode_payload(value)
            if len(payload) > min(self.limits.max_frame_bytes,
                                  self.limits.max_response_bytes if budget is None else budget):
                raise ValueError("local_bridge_response_too_large")
            peer.output = struct.pack("!I", len(payload)) + payload
            peer.sent = 0
            peer.success = value.get("state") == "SUCCEEDED"
            peer.close_after = close_after
            peer.budget = budget
            if not preserve_deadline:
                peer.deadline = time.monotonic() + self.limits.io_timeout_sec
            self.selector.modify(peer.sock, selectors.EVENT_WRITE, peer)
        except (ValueError, TypeError, OSError, KeyError):
            self.close_peer(peer)

    def refuse(self, peer, code, *, request=None, budget=None,
               preserve_deadline=False):
        route = peer.route
        self.queue(peer, _refusal(code, request=request,
            session_id=None if route is None else route.context.session_id,
            incarnation=None if route is None else self.incarnation), budget=budget,
            close_after=True, preserve_deadline=preserve_deadline)

    def authority_denied(self, peer):
        self.retire(peer.route)
        if peer.sent:
            # A partial success frame cannot be followed by a refusal frame.
            self.close_peer(peer)
        else:
            self.refuse(peer, "local_bridge_authority_denied", request=peer.request,
                        budget=peer.budget, preserve_deadline=bool(peer.output))

    def dispatch(self, peer, value):
        if time.monotonic() >= peer.deadline:
            self.close_peer(peer)
            return
        if peer.route is None:
            try:
                hello = validate_hello(value)
            except (ValueError, TypeError):
                self.refuse(peer, "local_bridge_invalid_request")
                return
            route = next((route for route in self.routes if not route.retired
                          and secrets.compare_digest(hello["transport_token"], route.token)), None)
            if route is None:
                self.refuse(peer, "local_bridge_auth_denied", request=hello)
                return
            if route.peer is not None:
                self.close_peer(peer)
                return
            # Reserve before hello success is buffered, using the sealed object.
            peer.route = route
            route.peer = peer
            peer.request = hello
            try:
                route.context.status()
                response = dict(schema_version=1, request_id=hello["request_id"],
                    state="SUCCEEDED", session_id=route.context.session_id,
                    service_incarnation=self.incarnation)
                route.context.status()
            except Exception:
                self.authority_denied(peer)
                return
            self.queue(peer, response)
            return
        request = value
        try:
            validate_request(request, self.limits)
        except (ValueError, TypeError):
            budget = request.get("max_response_bytes")
            if type(budget) is not int or not 0 <= budget <= self.limits.max_response_bytes:
                budget = None
            self.refuse(peer, "local_bridge_invalid_request", budget=budget)
            return
        peer.request = request
        peer.budget = request["max_response_bytes"]
        route = peer.route
        if request["session_id"] != route.context.session_id:
            self.refuse(peer, "local_bridge_session_denied", request=request, budget=peer.budget)
            return
        try:
            route.context.status()
            response = _dispatch_context(request, route.context, self.incarnation, self.limits)
            # Complete construction AND encoding/budget validation precede the
            # renewed authority check, including refused adapter responses.
            payload = encode_payload(response)
            if len(payload) > min(peer.budget, self.limits.max_response_bytes,
                                  self.limits.max_frame_bytes):
                self.close_peer(peer)
                return
            route.context.status()
        except Exception:
            self.authority_denied(peer)
            return
        self.queue(peer, response, budget=peer.budget)

    def read(self, peer):
        try:
            # Never consume the next frame or allocate from an unchecked size.
            remaining = 4 - len(peer.header) if peer.size is None else peer.size - len(peer.body)
            piece = peer.sock.recv(min(remaining, _IO_CHUNK))
            if not piece:
                self.close_peer(peer)
                return
            if peer.size is None:
                peer.header.extend(piece)
                if len(peer.header) == 4:
                    peer.size = struct.unpack("!I", peer.header)[0]
                    if not 1 <= peer.size <= self.limits.max_frame_bytes:
                        self.refuse(peer, "local_bridge_invalid_request")
            else:
                peer.body.extend(piece)
                if len(peer.body) == peer.size:
                    try:
                        value = decode_payload(bytes(peer.body))
                    except (ValueError, TypeError):
                        self.refuse(peer, "local_bridge_invalid_request")
                    else:
                        self.dispatch(peer, value)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self.close_peer(peer)

    def write(self, peer):
        if peer.success:
            try:
                peer.route.context.status()
            except Exception:
                self.authority_denied(peer)
                return
        # The synchronous native check may itself exhaust the original timeout.
        if not self.current(peer) or time.monotonic() >= peer.deadline:
            self.close_peer(peer)
            return
        try:
            sent = peer.sock.send(peer.output[peer.sent:peer.sent + _IO_CHUNK])
            if sent <= 0:
                self.close_peer(peer)
                return
            peer.sent += sent
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.close_peer(peer)
            return
        if peer.sent == len(peer.output):
            if peer.close_after:
                self.close_peer(peer)
                return
            peer.output = b""
            peer.sent = 0
            peer.header.clear()
            peer.body.clear()
            peer.size = None
            peer.request = None
            peer.budget = None
            peer.deadline = time.monotonic() + self.limits.io_timeout_sec
            self.selector.modify(peer.sock, selectors.EVENT_READ, peer)

    def accept(self):
        for _ in range(_ACCEPT_BUDGET):
            connection = None
            peer = None
            try:
                with self.stop.defer():
                    connection, _ = self.listener.accept()
                    if len(self.peers) >= _MAX_PEERS:
                        # Own the raw accepted socket only long enough to
                        # close it. Overflow never acquires a peer or buffers.
                        try:
                            connection.close()
                        except BaseException:
                            self.failed = True
                        finally:
                            connection = None
                    else:
                        peer = _Peer(connection, time.monotonic() + self.limits.io_timeout_sec)
                        self.peers[connection] = peer
                if peer is None:
                    continue
                connection.setblocking(False)
                self.selector.register(connection, selectors.EVENT_READ, peer)
            except (BlockingIOError, InterruptedError):
                return
            except BaseException:
                if peer is not None:
                    self.close_peer(peer)
                elif connection is not None:
                    try:
                        connection.close()
                    except BaseException:
                        self.failed = True
                raise

    def run(self):
        self.selector.register(self.listener, selectors.EVENT_READ, None)
        while True:
            now = time.monotonic()
            for peer in tuple(self.peers.values()):
                if now >= peer.deadline:
                    self.close_peer(peer)
            if not any(not route.retired for route in self.routes):
                # No future hello can succeed. Do not let unauthenticated
                # peers prolong retirement until their receive deadlines.
                for peer in tuple(self.peers.values()):
                    if peer.route is None:
                        self.close_peer(peer)
                if not self.peers:
                    return
            if not self.peers and now >= self.idle_deadline:
                return
            deadline = min((peer.deadline for peer in self.peers.values()),
                           default=self.idle_deadline)
            events = self.selector.select(max(0, min(0.25, deadline - time.monotonic())))
            for key, mask in events:
                peer = key.data
                if peer is None:
                    self.accept()
                elif self.current(peer):
                    if time.monotonic() >= peer.deadline:
                        self.close_peer(peer)
                    elif peer.output:
                        if mask & selectors.EVENT_WRITE:
                            self.write(peer)
                    elif mask & selectors.EVENT_READ:
                        self.read(peer)
                        # Release one bounded output step before another peer's
                        # synchronous native dispatch. Never consume another
                        # request while this response remains pending.
                        if self.current(peer) and peer.output:
                            self.write(peer)


def serve_local_bridge_concurrent_sessions(ledger: ReadOnlyAuthorityLedger,
        admissions: tuple[LocalBridgeAdmission, ...], socket_path: Path,
        limits: LocalServiceLimits) -> int:
    """Interleave bounded local transport without acquiring authority on reconnect."""
    parent_fd = -1
    parent_identity = None
    broker = listener = selector = transport = None
    socket_identity = None
    credential_identities = {}
    old_handlers = {}
    stop = _DeferredStop()
    result = 0
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                with stop.defer():
                    old_handlers[signum] = signal.signal(signum, stop)
        if (not isinstance(limits, LocalServiceLimits) or type(admissions) is not tuple
                or not 1 <= len(admissions) <= 3
                or any(not isinstance(item, LocalBridgeAdmission)
                       or not isinstance(item.binding, WorkerRunBinding) for item in admissions)
                or len({item.binding for item in admissions}) != len(admissions)):
            raise ValueError("local_bridge_endpoint_denied")
        socket_path = Path(socket_path)
        credential_files = tuple(Path(item.credential_file) for item in admissions)
        paths = (socket_path,) + credential_files
        if (len(set(paths)) != len(paths)
                or any(path.parent != socket_path.parent or not path.is_absolute()
                       or str(path) != os.path.abspath(str(path)) for path in paths)):
            raise ValueError("local_bridge_endpoint_denied")
        with stop.defer():
            parent_fd = open_private_directory(socket_path.parent)
            parent_identity = _identity(os.fstat(parent_fd))
        for path in paths:
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("local_bridge_endpoint_denied")
        with stop.defer():
            broker = LocalRunBroker(ledger, max_worker_sessions=3)
        contexts = []
        for admission in admissions:
            with stop.defer():
                context = broker.open_context(admission.binding, Path(admission.capability_file),
                                              profiles=())
                contexts.append(context)
        incarnation = secrets.token_urlsafe(24)
        tokens = tuple(secrets.token_urlsafe(32) for _ in contexts)
        if len(set(tokens)) != len(tokens):
            raise ValueError("local_bridge_auth_denied")
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
                raise ValueError("local_bridge_endpoint_denied")
        os.chmod(str(socket_path), 0o600, follow_symlinks=False)
        if _identity(os.stat(socket_path.name, dir_fd=parent_fd, follow_symlinks=False)) != socket_identity:
            raise ValueError("local_bridge_endpoint_denied")
        listener.listen(_MAX_PEERS)
        listener.setblocking(False)
        for credential_file, token in zip(credential_files, tokens):
            def credential_owned(identity, name=credential_file.name):
                credential_identities[name] = identity
            _publish_credential(parent_fd, credential_file.name, dict(
                schema_version=1, socket_path=str(socket_path), service_incarnation=incarnation,
                transport_token=token), stop=stop, on_owned=credential_owned)
        transport = _Transport(selector, listener, routes, incarnation, limits,
                               parent_fd, credential_identities, stop)
        transport.run()
    except _ServiceStopped:
        pass
    except Exception:
        result = 1
    finally:
        for signum in old_handlers:
            try:
                signal.signal(signum, signal.SIG_IGN)
            except BaseException:
                result = 1
        if transport is not None:
            for peer in tuple(transport.peers.values()):
                transport.close_peer(peer)
            if transport.failed:
                result = 1
        # A failed close must never suppress an unrelated cleanup attempt.
        for resource in (selector, broker, listener):
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
                # A recycled numeric descriptor grants no cleanup authority.
                result = 1
            for name, identity in ([(socket_path.name, socket_identity),
                                    *tuple(credential_identities.items())] if parent_owned else []):
                try:
                    _cleanup_owned(parent_fd, name, identity)
                except BaseException:
                    result = 1
            if parent_owned:
                try:
                    os.close(parent_fd)
                except BaseException:
                    result = 1
        for signum, handler in old_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException:
                result = 1
    return result
