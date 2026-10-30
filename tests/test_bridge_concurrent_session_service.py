"""Real native-ledger and child-owned AF_UNIX acceptance for B06.

The fixture owns its setup writers and serving children explicitly. Only the
exact SQLite SHM sidecar and separately recorded endpoint resources are outside
the raw native/project equality comparison. No other test module is imported.
"""

import base64
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import orch
import orch.bridge_authorization
from orch.bridge_local_client import LocalBridgeClient
from orch.bridge_local_protocol import LocalServiceLimits, receive_frame, send_frame
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerRunBinding


def _path_state(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    result = dict(exists=True, dev=info.st_dev, ino=info.st_ino,
                  uid=info.st_uid, mode=info.st_mode)
    if stat.S_ISREG(info.st_mode):
        raw = path.read_bytes()
        result.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    if stat.S_ISLNK(info.st_mode):
        result["target"] = os.readlink(path)
    return result


# The child gets only paths/identities, never capability bytes. The package root
# is supplied from the actually loaded orch.__file__, including immutable replay.
_CHILD = r'''
import hashlib, json, os, signal, socket, sqlite3, stat, struct, sys, threading, time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, sys.argv[1])
import orch
import orch.bridge_authorization
from orch import bridge_local_concurrent_service as service
from orch import bridge_local_service as legacy_service
from orch import bridge_local_broker as broker_module
from orch import bridge_readonly_authority as authority_module
from orch import bridge_file_scope as scope_module
from orch.worker_operations import WorkerRunBinding

config = json.loads(Path(sys.argv[2]).read_text())
report = dict(connections=[], acquired={}, cleanup_attempts=[], close_attempts=[],
    context_calls=[], scopes=[], context_count=0, triggered=False, events=[],
    owner_thread=threading.get_ident(), recv_sizes=[], send_sizes=[], max_registered_peers=0,
    accept_turns=[], select_waits=[], nearest_waits=[], fixture_authority_mutations=[])
contexts, scope_objects = [], []
active_peer = None
result = 1

def state(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'exists': False}
    value = dict(exists=True, dev=info.st_dev, ino=info.st_ino,
                 uid=info.st_uid, mode=info.st_mode)
    if stat.S_ISREG(info.st_mode):
        raw = path.read_bytes()
        value.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    if stat.S_ISLNK(info.st_mode):
        value['target'] = os.readlink(path)
    return value

def event(kind, **values):
    value = dict(kind=kind, monotonic=time.monotonic(), **values)
    report['events'].append(value)
    with open(config['events'], 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n')

def boundary(name):
    if config.get('signal_phase') == name and not report['triggered']:
        report['triggered'] = True
        event('signal', phase=name)
        signal.raise_signal(config['signum'])

def selected(peer):
    return (peer is not None and peer.route is not None and
            peer.route.context.binding.run_id == config.get('selected_run'))

def operation(peer):
    return None if peer is None or peer.request is None else peer.request.get('operation')

def revoke(peer, phase):
    if (selected(peer) and operation(peer) == config.get('revoke_operation')
            and config.get('revoke_phase') == phase and not report.get('revoked')):
        report['revoked'] = True
        path = Path(config['selected_capability'])
        report['revoke_before'] = state(path)
        path.chmod(0o640)
        event('revoked', phase=phase, sent=peer.sent, buffered=len(peer.output))

original_sqlite_connect = sqlite3.connect
original_connection = authority_module._ReadOnlyConnection
pending_mode = None
class ObservedConnection(original_connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observation = dict(connection_id='%d:%d:%d' %
            (os.getpid(), id(self), len(report['connections'])), mode=pending_mode, closed=False)
        report['connections'].append(self.observation)
    def close(self):
        super().close()
        try:
            self.execute('SELECT MAX(attempt) FROM runs')
        except sqlite3.ProgrammingError:
            self.observation['closed'] = True
        else:
            raise AssertionError('native connection remained usable after close')
def connect(database, *args, **kwargs):
    global pending_mode
    pending_mode = parse_qs(urlsplit(str(database)).query).get('mode', [None])[0]
    return original_sqlite_connect(database, *args, **kwargs)
sqlite3.connect = connect
authority_module._ReadOnlyConnection = ObservedConnection

original_scope_init = scope_module.LocalFileScope.__init__
def scope_init(self, *args, **kwargs):
    original_scope_init(self, *args, **kwargs)
    scope_objects.append(self)
scope_module.LocalFileScope.__init__ = scope_init
original_broker_init = broker_module.LocalRunBroker.__init__
def broker_init(self, *args, **kwargs):
    original_broker_init(self, *args, **kwargs)
    boundary('broker')
broker_module.LocalRunBroker.__init__ = broker_init
original_open = broker_module.LocalRunBroker.open_context
def open_context(self, *args, **kwargs):
    report['context_count'] += 1
    number = report['context_count']
    if config.get('context_failure') == number and config.get('failure_when') == 'before':
        raise OSError('fixture context acquisition failure')
    context = original_open(self, *args, **kwargs)
    contexts.append(context)
    if config.get('context_failure') == number:
        raise OSError('fixture context transfer failure')
    if config.get('signal_context') == number:
        boundary('context')
    return context
broker_module.LocalRunBroker.open_context = open_context
for name in ('read', 'list', 'status', 'close'):
    original = getattr(broker_module.LocalRunContext, name)
    def observed(self, *args, _name=name, _original=original, **kwargs):
        report['context_calls'].append(dict(kind=_name, run_id=self.binding.run_id,
                                            thread=threading.get_ident(), monotonic=time.monotonic()))
        return _original(self, *args, **kwargs)
    setattr(broker_module.LocalRunContext, name, observed)
original_broker_close = broker_module.LocalRunBroker.close
def broker_close(self):
    report['close_attempts'].append('broker')
    original_broker_close(self)
    if config.get('resource_close_failure') == 'broker':
        raise OSError('fixture broker close reported failure')
broker_module.LocalRunBroker.close = broker_close

original_publish = service._publish_credential
original_os_open, original_os_fstat, original_os_close = os.open, os.fstat, os.close
credential_fd = None
def stop_credential(name):
    if config.get('signal_boundary') == name:
        boundary('credential')
def open_fd(name, flags, *args, **kwargs):
    global credential_fd
    fd = original_os_open(name, flags, *args, **kwargs)
    if name == config.get('signal_credential') and flags & os.O_EXCL:
        credential_fd = fd
        stop_credential('open')
    if str(name) == str(Path(config['socket']).parent) and flags & os.O_DIRECTORY:
        boundary('parent')
    return fd
def fstat_fd(fd):
    info = original_os_fstat(fd)
    if fd == credential_fd:
        stop_credential('identity')
    return info
def close_fd(fd):
    original_os_close(fd)
    if fd == credential_fd:
        stop_credential('close')
    if fd == report.get('parent_fd'):
        report['close_attempts'].append('parent')
        if config.get('resource_close_failure') == 'parent':
            raise OSError('fixture parent close reported failure')
original_private_directory = service.open_private_directory
def private_directory(path):
    fd = original_private_directory(path)
    report['parent_fd'] = fd
    boundary('parent')
    return fd
service.open_private_directory = private_directory

def trace_publish(frame, event_name, arg):
    if (frame.f_code is original_publish.__code__ and event_name == 'line'
            and frame.f_locals.get('name') == config.get('signal_credential')
            and frame.f_locals.get('succeeded') is True):
        stop_credential('success')
    return trace_publish

def publish(parent_fd, name, value, **kwargs):
    path = str(Path(config['socket']).parent / name)
    callback = kwargs.get('on_owned')
    def owned(identity):
        if callback is not None:
            callback(identity)
        report['acquired'][path] = state(path)
        if config.get('signal_credential') == name:
            stop_credential('owned')
    kwargs['on_owned'] = owned
    if config.get('credential_failure') == name and config.get('failure_when') == 'before':
        raise OSError('fixture credential acquisition failure')
    identity = original_publish(parent_fd, name, value, **kwargs)
    report['acquired'][path] = state(path)
    if config.get('credential_failure') == name:
        raise OSError('fixture credential publication failure')
    if config.get('signal_credential') == name:
        stop_credential('published')
    return identity
service._publish_credential = publish

original_socket = socket.socket
class ObservedSocket(original_socket):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if config.get('small_send_buffer'):
            self.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        if kwargs.get('fileno') is None and self.family == socket.AF_UNIX:
            boundary('listener_created')
    def bind(self, address):
        outcome = super().bind(address)
        report['acquired'][str(address)] = state(address)
        self._fixture_listener = True
        if config.get('signal_credential'):
            os.open, os.fstat, os.close = open_fd, fstat_fd, close_fd
        elif config.get('resource_close_failure') == 'parent':
            os.close = close_fd
        boundary('listener_bound')
        return outcome
    def accept(self):
        outcome = super().accept()
        report['accepted'] = report.get('accepted', 0) + 1
        event('accepted', total=report['accepted'])
        boundary('accept')
        return outcome
    def recv(self, size, *args):
        report['recv_sizes'].append(size)
        value = super().recv(size, *args)
        boundary('receive')
        return value
    def send(self, data, *args):
        report['send_sizes'].append(len(data))
        partial = (selected(active_peer) and operation(active_peer) == config.get('revoke_operation')
                   and config.get('revoke_phase') == 'partial' and not report.get('revoked'))
        if partial:
            data = data[:11]
        if (selected(active_peer) and config.get('send_failure')
                and operation(active_peer) == 'file.read' and not report.get('send_failed')):
            report['send_failed'] = True
            event('send_failure')
            raise BrokenPipeError('fixture send failure')
        try:
            value = super().send(data, *args)
        except BlockingIOError:
            event('would_block', selected=selected(active_peer), sent=active_peer.sent)
            raise
        if partial and value:
            report['partial_bytes'] = value
            revoke(active_peer, 'partial')
        boundary('send')
        return value
    def close(self):
        was_peer = not getattr(self, '_fixture_listener', False) and self.fileno() >= 0
        was_listener = getattr(self, '_fixture_listener', False)
        if was_listener:
            report['close_attempts'].append('listener')
        super().close()
        if was_peer:
            if config.get('peer_close_delay'):
                time.sleep(config['peer_close_delay'])
            report['close_attempts'].append('peer')
            if config.get('resource_close_failure') == 'peer' and not report.get('peer_close_failed'):
                report['peer_close_failed'] = True
                raise OSError('fixture peer close reported failure')
        if was_listener and config.get('resource_close_failure') == 'listener':
            raise OSError('fixture listener close reported failure')
socket.socket = ObservedSocket

original_selector = service.selectors.DefaultSelector
class ObservedSelector(original_selector):
    def __init__(self):
        super().__init__()
        self.stale = None
        self.injected = False
        self.stale_read_injected = False
        self.listener_fd = None
        self.flood_barrier_used = False
        boundary('selector')
    def register(self, fileobj, events, data=None):
        value = super().register(fileobj, events, data)
        if data is None:
            self.listener_fd = value.fd
        peers = [key for key in self.get_map().values() if key.data is not None]
        report['max_registered_peers'] = max(report['max_registered_peers'], len(peers))
        event('registered', fd=value.fd, peers=len(peers))
        return value
    def select(self, timeout=None):
        report['select_waits'].append(timeout)
        authenticated = [key.data for key in self.get_map().values()
            if key.data is not None and key.data.route is not None]
        if config.get('flood_barrier') and len(authenticated) == 2 and not self.flood_barrier_used:
            self.flood_barrier_used = True
            event('flood_barrier_ready')
            end = time.monotonic() + 2
            while not Path(config['flood_release']).exists():
                if time.monotonic() >= end:
                    raise OSError('fixture flood barrier release was not supplied')
                time.sleep(0.002)
        if current_transport is not None:
            nearest = min((peer.deadline for peer in current_transport.peers.values()),
                          default=current_transport.idle_deadline)
            report['nearest_waits'].append(dict(requested=timeout,
                available=max(0, nearest - time.monotonic())))
        if config.get('backpressure_probe') or config.get('stale_event'):
            timeout = min(timeout, 0.01)
        values = super().select(timeout)
        if config.get('flood_barrier') and len(authenticated) == 2 and any(key.data is None for key, mask in values):
            event('flood_listener_ready')
        for key in list(self.get_map().values()):
            peer = key.data
            if peer is None or not selected(peer):
                continue
            if (config.get('backpressure_probe') and peer.output
                    and operation(peer) == 'file.read'
                    and not any(item['kind'] == 'would_block' for item in report['events'])
                    and not any(event_key is key for event_key, _ in values)):
                # Real nonblocking send against a full kernel socket, with a
                # deliberately stale writable notification. No fake send result.
                values.append((key, service.selectors.EVENT_WRITE))
            if (config.get('stale_read') and peer.output and operation(peer) == 'file.read'
                    and not self.stale_read_injected
                    and any(item['kind'] == 'would_block' for item in report['events'])):
                values.append((key, service.selectors.EVENT_READ))
                self.stale_read_injected = True
                event('stale_read')
            if (config.get('stale_event') and self.stale is not None and not self.injected
                    and self.stale.data is not peer):
                values.append((self.stale, service.selectors.EVENT_READ | service.selectors.EVENT_WRITE))
                self.injected = True
                event('stale_event', old_fd=self.stale.fd, replacement_fd=key.fd)
        return values
    def close(self):
        report['close_attempts'].append('selector')
        super().close()
        if config.get('resource_close_failure') == 'selector':
            raise OSError('fixture selector close reported failure')
service.selectors.DefaultSelector = ObservedSelector

original_cleanup = service._cleanup_owned
def cleanup(parent_fd, name, identity):
    report['cleanup_attempts'].append(name)
    path = str(Path(config['socket']).parent / name)
    if identity is not None and state(path)['exists']:
        report['acquired'].setdefault(path, state(path))
    if config.get('cleanup_failure') == name and identity is not None:
        raise OSError('fixture owned-path unlink failure')
    return original_cleanup(parent_fd, name, identity)
service._cleanup_owned = cleanup

original_dispatch = service._dispatch_context
def dispatch(request, context, incarnation, limits):
    started = time.monotonic()
    event('native_start', run_id=context.binding.run_id, operation=request.get('operation'))
    try:
        response = original_dispatch(request, context, incarnation, limits)
    finally:
        event('native_end', run_id=context.binding.run_id, operation=request.get('operation'),
              duration=time.monotonic() - started)
    if (context.binding.run_id == config.get('selected_run')
            and request['operation'] == config.get('revoke_operation')
            and config.get('revoke_phase') == 'construction' and not report.get('revoked')):
        report['revoke_response_constructed'] = response['state'] == 'SUCCEEDED' and (
            'status' in response if request['operation'] == 'session.status' else bool(response['output_base64']))
        # The real native capability mode changes after the complete response.
        peer = next(route.peer for route in current_transport.routes if route.context is context)
        revoke(peer, 'construction')
    return response
service._dispatch_context = dispatch
current_transport = None
original_peer_init = service._Peer.__init__
def peer_init(self, *args, **kwargs):
    original_peer_init(self, *args, **kwargs)
    event('peer_allocated')
service._Peer.__init__ = peer_init
original_transport_init = service._Transport.__init__
def transport_init(self, *args, **kwargs):
    global current_transport
    original_transport_init(self, *args, **kwargs)
    current_transport = self
service._Transport.__init__ = transport_init
original_transport_dispatch = service._Transport.dispatch
def transport_dispatch(self, peer, value):
    if peer.route is not None:
        peer.request = value
        revoke(peer, 'before_dispatch')
    event('dispatch', operation=value.get('operation'), request_id=value.get('request_id'),
          fd=peer.sock.fileno())
    return original_transport_dispatch(self, peer, value)
service._Transport.dispatch = transport_dispatch
original_queue = service._Transport.queue
def queue(self, peer, value, **kwargs):
    original_queue(self, peer, value, **kwargs)
    if peer.success and self.current(peer):
        if selected(peer) and operation(peer):
            event('response_queued', operation=operation(peer), deadline=peer.deadline)
        revoke(peer, 'queued')
service._Transport.queue = queue
original_write = service._Transport.write
def write(self, peer):
    global active_peer
    active_peer = peer
    request_id = None if peer.request is None else peer.request.get('request_id')
    pending = bool(peer.output)
    try:
        return original_write(self, peer)
    finally:
        if pending and self.current(peer) and not peer.output:
            event('response_complete', request_id=request_id, fd=peer.sock.fileno(),
                  run_id=peer.route.context.binding.run_id, deadline=peer.deadline)
        active_peer = None
service._Transport.write = write
original_retire = service._Transport.retire
def retire(self, route):
    original_retire(self, route)
    if not report.get('retirement_reported'):
        report['retirement_reported'] = True
        if report.get('revoked'):
            path = Path(config['selected_capability'])
            before = state(path)
            path.chmod(0o600)
            report['fixture_authority_mutations'].append(dict(path=str(path),
                reason='fixture restores a deliberately revoked capability after permanent route retirement',
                before=before, after=state(path)))
        event('retired', run_id=route.context.binding.run_id, peers=len(self.peers))
service._Transport.retire = retire
original_close_peer = service._Transport.close_peer
def close_peer(self, peer):
    owned = self.current(peer)
    if owned:
        event('peer_closed', fd=peer.sock.fileno(), deadline=peer.deadline,
              run_id=None if peer.route is None else peer.route.context.binding.run_id,
              phase='send' if peer.output else 'receive',
              close_classification='deadline_elapsed' if time.monotonic() >= peer.deadline else 'other_or_unknown',
              output_pending=bool(peer.output), output_bytes=len(peer.output), sent=peer.sent,
              retired=None if peer.route is None else peer.route.retired)
    if owned and selected(peer):
        self.selector.stale = self.selector.get_key(peer.sock)
        event('selected_unregistered', fd=peer.sock.fileno(), deadline=peer.deadline)
    return original_close_peer(self, peer)
service._Transport.close_peer = close_peer
original_accept = service._Transport.accept
def accept(self):
    before = report.get('accepted', 0)
    try:
        return original_accept(self)
    finally:
        report['accept_turns'].append(report.get('accepted', 0) - before)
service._Transport.accept = accept

def previous_handler(_signum, _frame):
    pass
for signum in (signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, previous_handler)
try:
    admissions = [service.LocalBridgeAdmission(WorkerRunBinding(**item['binding']),
        Path(item['capability_file']), Path(item['credential_file'])) for item in config['admissions']]
    kind = config.get('container', 'tuple')
    admissions = tuple(admissions) if kind == 'tuple' else (
        admissions if kind == 'list' else (item for item in admissions))
    if config.get('signal_boundary') == 'success':
        sys.settrace(trace_publish)
    result = service.serve_local_bridge_concurrent_sessions(
        authority_module.ReadOnlyAuthorityLedger(Path(config['home'])), admissions,
        Path(config['socket']), service.LocalServiceLimits(**config['limits']))
except BaseException as error:
    report['error'] = type(error).__name__ + ': ' + str(error)
finally:
    sys.settrace(None)
    report['signals_restored'] = all(signal.getsignal(signum) is previous_handler
        for signum in (signal.SIGINT, signal.SIGTERM))
    report['scopes'] = [dict(root_fd=scope._root_fd) for scope in scope_objects]
    report['orch_imports'] = {}
    for name, module in sorted(sys.modules.items()):
        if name == 'orch' or name.startswith('orch.'):
            source = getattr(module, '__file__', None)
            if source:
                path = Path(source).resolve()
                report['orch_imports'][name] = dict(source=str(path),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    print(json.dumps(report, sort_keys=True), flush=True)
raise SystemExit(result)
'''


class _StatusPump:
    """Own one original client socket and surface every background failure."""
    def __init__(self, test, label, peer, phased=False):
        self.test, self.label = test, label
        self.phased = phased
        self.release = threading.Event()
        self.completed = threading.Event()
        self.sock, self.hello = peer
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name='b06-pump-' + label, daemon=True)

    def _run(self):
        try:
            number = 0
            while not self.stop_requested.is_set():
                number += 1
                reply = self.test._call(self.sock, self.hello,
                    request_id='pump-%s-%d' % (self.label, number))
                if (reply.get('state') != 'SUCCEEDED'
                        or reply.get('status', {}).get('binding') != asdict(self.test.bindings[self.label])):
                    raise AssertionError('pump %s received invalid status response' % self.label)
                self.ready.set()
                if self.phased:
                    if number == 1:
                        while not self.stop_requested.is_set() and not self.release.wait(0.05):
                            pass
                    else:
                        self.completed.set()
                        self.stop_requested.wait()
                else:
                    self.stop_requested.wait(0.05)
        except BaseException as error:
            self.error = error
            self.ready.set()
            self.completed.set()

    def start(self):
        self.thread.start()

    def check(self):
        if self.error is not None:
            raise self.error

    def stop(self):
        self.stop_requested.set()
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            shutdown_error = None
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError as error:
                shutdown_error = '%s: %s' % (type(error).__name__, error)
            self.thread.join(timeout=2)
            raise AssertionError('pump %s required forced shutdown; alive=%s; shutdown_error=%s; pump_error=%s' %
                (self.label, self.thread.is_alive(), shutdown_error, self.error))
        self.check()


class ConcurrentSessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.b05_service_children = []
        self.b05_runtime_observations = []
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-b06-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.subprocess_env = dict(os.environ)
        if sys.platform == "darwin":
            # Resolve the developer-tool launcher once. Every authority check
            # still invokes the real Git executable with its original arguments.
            native_git = Path(subprocess.check_output(
                ["/usr/bin/xcrun", "--find", "git"], text=True,
                stderr=subprocess.PIPE, timeout=12).strip())
            self.assertTrue(native_git.is_absolute() and native_git.is_file()
                            and os.access(native_git, os.X_OK))
            git_bin = self.base / "native-git-bin"
            git_bin.mkdir(mode=0o700)
            (git_bin / "git").symlink_to(native_git)
            # A Git-only directory keeps the selected Python interpreter.
            self.subprocess_env["PATH"] = str(git_bin) + os.pathsep + os.environ.get("PATH", os.defpath)
        (self.base / "home").mkdir(mode=0o700)
        self.native_connections = []
        self.addCleanup(self._close_native_setup)
        original_connect = Orchestrator.connect

        def setup_connect(owner):
            connection = original_connect(owner)
            self.native_connections.append(connection)
            return connection

        self.repos, self.bindings, self.caps = {}, {}, {}
        with mock.patch.object(Orchestrator, "connect", setup_connect):
            self.orchestrator = Orchestrator(self.base / "home")
            registry = ProjectRegistry(self.orchestrator.root)
            for label in ("a", "b", "c", "d"):
                repo = self.base / ("repo-" + label)
                if label == "d" and self._testMethodName != "test_b06_invalid_static_admissions_fail_without_publishing_resources":
                    repo.mkdir(mode=0o700)
                    (repo / "secret.txt").write_bytes(b"foreign sentinel outside every admitted native project")
                    self.repos[label] = repo
                    continue
                self._git("init", "-q", "-b", "main", str(repo))
                self._git("-C", str(repo), "config", "user.email", "fixture@example.invalid")
                self._git("-C", str(repo), "config", "user.name", "Fixture")
                (repo / "binary.bin").write_bytes(label.encode("ascii") + b"\x00\xff\n")
                (repo / "large.bin").write_bytes((label.encode("ascii") + bytes(range(256))) * 170)
                (repo / "secret.txt").write_bytes(("private-" + label).encode("ascii"))
                (repo / "entries").mkdir()
                (repo / "entries" / (label + ".txt")).write_bytes(b"unread child content")
                self._git("-C", str(repo), "add", ".")
                self._git("-C", str(repo), "commit", "-qm", "fixture")
                project = registry.add(repo, name="B06 fixture " + label, profile="safe",
                    review_mode="off", allow_commit=True, allow_push=False)["project"]
                plan = build_single_task_plan(project, task_id="B06-" + label.upper(),
                    goal="Disposable multi-session endpoint", allowed_paths=["binary.bin", "large.bin", "entries/"],
                    plan_revision="b06-" + label)
                plan_path = self.base / (label + "-plan.json")
                write_plan(plan_path, plan)
                self.orchestrator.load_plan(plan_path)
                claim = self.orchestrator.claim("fixture-" + label, project_id=project["project_id"])
                self.assertEqual(claim["status"], "CLAIMED", claim)
                self.repos[label] = repo
                self.caps[label] = Path(claim["capability_file"])
                self.bindings[label] = WorkerRunBinding(project["project_id"], project["writer_key"],
                    claim["task_id"], claim["run_id"], claim["attempt"])
        # These are deliberately retained fixture setup writers. They perform no
        # reads/writes during service observation, and close after raw snapshots.
        self.home = self.orchestrator.root
        self.db = self.orchestrator.db_path
        self.ipc = self.base / "ipc"
        self.ipc.mkdir(mode=0o700)
        self.endpoint = self.ipc / "s"
        self.credentials = {label: self.ipc / (label + ".json") for label in self.bindings}
        self.limits = LocalServiceLimits(io_timeout_sec=5.0, idle_timeout_sec=0.4)
        self.active = []
        self.addCleanup(self._finish_remaining)

    def _git(self, *args):
        env = dict(self.subprocess_env, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                   GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
            check=True, capture_output=True, env=env, timeout=12)

    def _close_native_setup(self):
        for connection in self.native_connections:
            connection.close()

    def _snapshot(self):
        result = {}
        shm = str(self.db) + "-shm"
        for root in (self.home, *self.repos.values()):
            for path in (root, *sorted(root.rglob("*"))):
                if str(path) != shm:
                    result[str(path)] = _path_state(path)
        result[str(self.db) + "-wal"] = _path_state(str(self.db) + "-wal")
        return result

    def _launch(self, labels=("a", "b", "c"), **changes):
        admissions = [dict(binding=asdict(self.bindings[label]),
            capability_file=str(self.caps[label]), credential_file=str(self.credentials[label]))
            for label in labels]
        config = dict(home=str(self.home), socket=str(self.endpoint), admissions=admissions,
                      limits=asdict(self.limits), selected_run=self.bindings["b"].run_id,
                      selected_capability=str(self.caps["b"]),
                      events=str(self.base / ("events-%d.jsonl" % len(self.b05_service_children))),
                      flood_release=str(self.base / ("flood-release-%d" % len(self.b05_service_children))))
        config.update(changes)
        if config.get("signal_context"):
            config["signal_phase"] = "context"
        if config.get("signal_credential"):
            config["signal_phase"] = "credential"
        resources = list(dict.fromkeys(os.path.abspath(str(self.base / path))
            for path in [config["socket"], *[
                admission["credential_file"] for admission in config["admissions"]]]))
        launch = dict(config=config, before=self._snapshot(),
            shm_before=_path_state(str(self.db) + "-shm"),
            resources_before={path: _path_state(path) for path in resources}, finished=False)
        config_file = self.base / ("child-%d.json" % len(self.b05_service_children))
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        package_root = Path(orch.__file__).resolve().parent.parent
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", _CHILD,
            str(package_root), str(config_file)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=str(self.base), env=self.subprocess_env)
        self.b05_service_children.append(child)
        launch["child"] = child
        self.active.append(launch)
        return launch

    def _ready(self, launch):
        child = launch["child"]
        expected = [Path(item["credential_file"]) for item in launch["config"]["admissions"]]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and child.poll() is None:
            try:
                if expected and all(set(json.loads(path.read_text())) == {
                        "schema_version", "socket_path", "service_incarnation", "transport_token"}
                        for path in expected):
                    return
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.01)
        report = self._finish(launch)
        self.fail("multi-session endpoint did not publish its independent credentials: " +
                  str(report.get("error", child.returncode)))

    def _finish_remaining(self):
        for launch in self.active:
            if not launch["finished"]:
                self._finish(launch)

    def _finish(self, launch, *, replacements=None, cleanup_failure=None):
        if launch["finished"]:
            return launch["report"]
        if cleanup_failure is None and launch["config"].get("cleanup_failure"):
            cleanup_failure = str(self.ipc / launch["config"]["cleanup_failure"])
        child = launch["child"]
        try:
            stdout, stderr = child.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate(timeout=3)
            self.fail("fixture-owned serving child exceeded bounded lifetime: " + stderr)
        finally:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        report = json.loads(stdout)
        launch.update(finished=True, report=report)
        self.assertEqual(stderr, "", stderr)
        resources = []
        for path, before in launch["resources_before"].items():
            after = _path_state(path)
            acquired = report["acquired"].get(path)
            record = dict(path=path, before=before, after=after)
            if replacements and path in replacements:
                record.update(disposition="replaced", acquired=acquired,
                              replacement=replacements[path])
            elif cleanup_failure == path:
                record.update(disposition="cleanup_unresolved", acquired=acquired)
                current = Path(path).lstat()
                self.assertEqual((current.st_dev, current.st_ino, current.st_uid,
                                  stat.S_IFMT(current.st_mode)),
                    (acquired["dev"], acquired["ino"], acquired["uid"], stat.S_IFMT(acquired["mode"])))
                Path(path).unlink()
                record["fixture_after"] = _path_state(path)
            elif before["exists"]:
                record["disposition"] = "foreign"
            elif acquired is not None:
                record.update(disposition="owned", acquired=acquired)
            else:
                record["disposition"] = "absent"
            resources.append(record)
        connection_records = report["connections"]
        observation = dict(subject=launch["config"].get("subject", "B06 actual endpoint lifecycle"),
            actual_socket=launch.get("connected", False), native_before=launch["before"],
            native_after=self._snapshot(), ledger_db_paths=[str(self.db)],
            protected_paths=dict(capability_files=[str(path) for path in self.caps.values()],
                authority_files=[str(self.db)],
                project_sentinels=[str(repo / "secret.txt") for repo in self.repos.values()]),
            sqlite_shm_observations=[dict(path=str(self.db) + "-shm",
                before=launch["shm_before"], after=_path_state(str(self.db) + "-shm"))],
            connection_modes=[item["mode"] for item in connection_records],
            connections_closed=connection_records, endpoint_resources=resources,
            service_children=[dict(pid=child.pid, returncode=child.returncode,
                streams_closed={name: None if getattr(child, name) is None else getattr(child, name).closed
                    for name in ("stdin", "stdout", "stderr")}, orch_imports=report["orch_imports"])])
        if not connection_records:
            observation["startup_rejected_before_read_connection"] = True
        if cleanup_failure:
            observation["fixture_controlled_cleanup"] = True
        observation["fixture_authority_mutations"] = report.get("fixture_authority_mutations", [])
        observation["transport_observations"] = {key: report.get(key) for key in (
            "events", "context_calls", "owner_thread", "recv_sizes", "send_sizes",
            "max_registered_peers", "accept_turns", "select_waits", "nearest_waits", "partial_bytes")}
        self.b05_runtime_observations.append(observation)
        self.assertNotIn("error", report, "fixture/launcher failed before the service returned")
        self.assertEqual(observation["native_before"], observation["native_after"],
                         "endpoint changed raw protected native/project state")
        self.assertTrue(all(item["mode"] == "ro" and item["closed"]
                            for item in connection_records), connection_records)
        for resource in resources:
            if resource["disposition"] == "owned":
                self.assertEqual(resource["after"], {"exists": False}, resource["path"])
            if resource["disposition"] == "foreign":
                self.assertEqual(resource["after"], resource["before"], resource["path"])
        self.assertEqual({item["thread"] for item in report["context_calls"]},
                         {report["owner_thread"]} if report["context_calls"] else set())
        self.assertTrue(all(0 <= value <= 0.25 for value in report["select_waits"]))
        self.assertLessEqual(report["max_registered_peers"], 3)
        self.assertTrue(all(0 < value <= 16384 for value in report["recv_sizes"]))
        self.assertTrue(all(0 < value <= 16384 for value in report["send_sizes"]))
        self.assertTrue(all(value <= 3 for value in report["accept_turns"]))
        self.assertTrue(report["signals_restored"], "service did not restore original signal handlers")
        self.assertTrue(all(item["root_fd"] == -1 for item in report["scopes"]), report["scopes"])
        return report

    def _connect(self, launch, label="a", **hello_changes):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.settimeout(5)
        sock.connect(str(self.endpoint))
        launch["connected"] = True
        credential = json.loads(self.credentials[label].read_text())
        hello = dict(schema_version=1, request_id="hello-b06", type="hello",
                     transport_token=credential["transport_token"])
        hello.update(hello_changes)
        send_frame(sock, hello, self.limits)
        return sock, receive_frame(sock, LocalServiceLimits())

    def _call(self, sock, hello, operation="session.status", target=None, **changes):
        request = dict(schema_version=1, request_id="call-b06", session_id=hello["session_id"],
                       operation=operation, target=target, max_response_bytes=4096)
        request.update(changes)
        send_frame(sock, request, self.limits)
        return receive_frame(sock, LocalServiceLimits())

    def test_b06_single_and_two_admission_new_entrypoints_serve_their_native_runs(self):
        for labels in (("a",), ("a", "b")):
            with self.subTest(admissions=len(labels)):
                launch = self._launch(labels)
                self._ready(launch)
                for label in labels:
                    sock, hello = self._connect(launch, label)
                    self.assertEqual(self._call(sock, hello)["status"]["binding"],
                                     asdict(self.bindings[label]))
                    sock.close()
                self._finish(launch)
                self.assertEqual(launch["child"].returncode, 0)

    def test_b06_complete_b_credential_at_a_path_can_only_select_b(self):
        launch = self._launch()
        self._ready(launch)
        a_path = self.credentials["a"]
        held = os.open(a_path, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, held)
        a_path.unlink()
        a_path.write_bytes(self.credentials["b"].read_bytes())
        a_path.chmod(0o600)
        replacement = _path_state(a_path)
        with closing(LocalBridgeClient(a_path)) as client:
            status = client.call("session.status")
            self.assertEqual(status["status"]["binding"], asdict(self.bindings["b"]))
            self.assertEqual(base64.b64decode(client.call("file.read", "binary.bin")["output_base64"]),
                             b"b\x00\xff\n")
        launch["connected"] = True
        self._finish(launch, replacements={str(a_path): replacement})
        self.assertEqual(_path_state(a_path), replacement)
        self.assertEqual(launch["child"].returncode, 0)

    def test_b06_unknown_and_previous_incarnation_tokens_and_sessions_are_denied(self):
        first = self._launch()
        self._ready(first)
        old = json.loads(self.credentials["a"].read_text())
        sock, hello = self._connect(first)
        old_session = hello["session_id"]
        sock.close()
        self._finish(first)
        second = self._launch()
        self._ready(second)
        fresh = json.loads(self.credentials["a"].read_text())
        self.assertNotEqual(old["service_incarnation"], fresh["service_incarnation"])
        self.assertNotEqual(old["transport_token"], fresh["transport_token"])
        for token in ("unknown" * 5, old["transport_token"]):
            sock, refused = self._connect(second, transport_token=token)
            self.assertEqual((refused["state"], refused["error_code"]),
                             ("REFUSED", "local_bridge_auth_denied"))
            sock.close()
        sock, hello = self._connect(second)
        self.assertNotEqual(hello["session_id"], old_session)
        self.assertEqual(self._call(sock, hello, session_id=old_session)["error_code"],
                         "local_bridge_session_denied")
        sock.close()
        self._finish(second)
        self.assertEqual(second["child"].returncode, 0)

    def test_b06_invalid_static_admissions_fail_without_publishing_resources(self):
        for labels, container in (((), "tuple"), (("a", "b", "c", "d"), "tuple"),
                                  (("a", "a"), "tuple"), (("a", "b"), "list"),
                                  (("a", "b"), "generator")):
            with self.subTest(labels=labels, container=container):
                launch = self._launch(labels, container=container)
                report = self._finish(launch)
                self.assertNotEqual(launch["child"].returncode, 0)
                self.assertEqual(report["acquired"], {})
        for defect in ("duplicate_credential", "socket_collision", "foreign_capability"):
            with self.subTest(defect=defect):
                admissions = [dict(binding=asdict(self.bindings[label]),
                    capability_file=str(self.caps[label]), credential_file=str(self.credentials[label]))
                    for label in ("a", "b", "c")]
                if defect == "duplicate_credential":
                    admissions[1]["credential_file"] = admissions[0]["credential_file"]
                elif defect == "socket_collision":
                    admissions[1]["credential_file"] = str(self.endpoint)
                else:
                    admissions[0]["capability_file"] = str(self.caps["b"])
                launch = self._launch(admissions=admissions)
                report = self._finish(launch)
                self.assertNotEqual(launch["child"].returncode, 0)
                self.assertEqual(report["acquired"], {})

    def test_b06_noncanonical_and_nonprivate_endpoint_parents_are_refused_unchanged(self):
        other = self.base / "other-ipc"
        other.mkdir(mode=0o700)
        alias = self.base / "alias-ipc"
        alias.symlink_to(self.ipc, target_is_directory=True)
        variants = (str(alias / "s"), str(self.ipc / ".." / "ipc" / "s"), "relative-s")
        for endpoint in variants:
            with self.subTest(endpoint=endpoint):
                launch = self._launch(socket=endpoint)
                report = self._finish(launch)
                self.assertNotEqual(launch["child"].returncode, 0)
                self.assertEqual(report["acquired"], {})
        for private in (False, True):
            with self.subTest(private=private):
                admissions = [dict(binding=asdict(self.bindings["a"]),
                    capability_file=str(self.caps["a"]),
                    credential_file=str((other if private else self.ipc) / "a.json"))]
                if not private:
                    self.ipc.chmod(0o755)
                try:
                    launch = self._launch(("a",), admissions=admissions)
                    report = self._finish(launch)
                    self.assertNotEqual(launch["child"].returncode, 0)
                    self.assertEqual(report["acquired"], {})
                finally:
                    self.ipc.chmod(0o700)

    def test_b06_later_context_acquisition_failures_close_all_acquired_scopes(self):
        for number in (2, 3):
            for when in ("before", "after"):
                with self.subTest(context=number, when=when):
                    launch = self._launch(context_failure=number, failure_when=when)
                    report = self._finish(launch)
                    self.assertEqual(report["context_count"], number)
                    self.assertNotEqual(launch["child"].returncode, 0)
                    self.assertEqual(len(report["scopes"]), number - (when == "before"))
                    self.assertEqual(report["acquired"], {})

    def test_b06_later_credential_failures_clean_every_previously_owned_path(self):
        for label in ("b", "c"):
            for when in ("before", "after"):
                with self.subTest(credential=label, when=when):
                    launch = self._launch(credential_failure=label + ".json", failure_when=when)
                    report = self._finish(launch)
                    self.assertNotEqual(launch["child"].returncode, 0)
                    self.assertIn(str(self.endpoint), report["acquired"])
                    self.assertIn(str(self.credentials["a"]), report["acquired"])
                    self.assertEqual(len(report["scopes"]), 3)

    def test_b06_sigint_and_sigterm_at_later_context_transfer_unwind_owned_scopes(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for number in (2, 3):
                with self.subTest(signum=signum, context=number):
                    launch = self._launch(signal_context=number, signum=int(signum))
                    report = self._finish(launch)
                    self.assertTrue(report["triggered"])
                    self.assertEqual(len(report["scopes"]), number)
                    self.assertEqual(launch["child"].returncode, 0)

    def test_b06_sigint_and_sigterm_at_later_credential_ownership_boundaries(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for boundary in ("open", "identity", "owned", "success", "close", "published"):
                with self.subTest(signum=signum, boundary=boundary):
                    launch = self._launch(signal_credential="b.json", signal_boundary=boundary,
                                          signum=int(signum))
                    report = self._finish(launch)
                    self.assertTrue(report["triggered"], boundary)
                    self.assertEqual(launch["child"].returncode, 0)
                    self.assertIn(str(self.credentials["a"]), report["acquired"])

    def test_b06_preexisting_socket_and_credential_objects_survive_startup_refusal(self):
        for path in (self.endpoint, self.credentials["b"]):
            for kind in ("regular", "symlink", "socket"):
                with self.subTest(path=path.name, kind=kind):
                    foreign_socket = None
                    if kind == "regular":
                        path.write_bytes(b"foreign untouched endpoint object")
                        path.chmod(0o600)
                    elif kind == "symlink":
                        path.symlink_to(self.repos["d"] / "secret.txt")
                    else:
                        foreign_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        foreign_socket.bind(str(path))
                    try:
                        before = _path_state(path)
                        launch = self._launch()
                        report = self._finish(launch)
                        self.assertNotEqual(launch["child"].returncode, 0)
                        self.assertEqual(_path_state(path), before)
                        self.assertEqual(report["acquired"], {})
                    finally:
                        if foreign_socket is not None:
                            foreign_socket.close()
                        path.unlink()

    def test_b06_replaced_owned_socket_and_later_credential_survive_exactly(self):
        launch = self._launch()
        self._ready(launch)
        sock, hello = self._connect(launch)
        self.assertEqual(self._call(sock, hello)["state"], "SUCCEEDED")
        sock.close()
        replacements = {}
        # Hold a credential descriptor so unlink/create cannot reuse its inode.
        fd = os.open(self.credentials["c"], os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, fd)
        for path in (self.endpoint, self.credentials["c"]):
            if path == self.endpoint:
                path.unlink()
                path.symlink_to(self.repos["d"] / "secret.txt")
            else:
                path.unlink()
                path.write_bytes(b"replacement must survive service cleanup")
                path.chmod(0o600)
            replacements[str(path)] = _path_state(path)
        self._finish(launch, replacements=replacements)
        for path, replacement in replacements.items():
            self.assertEqual(_path_state(path), replacement)
        self.assertEqual(launch["child"].returncode, 0)

    def test_b06_cleanup_failure_reports_failure_but_attempts_all_other_owned_paths(self):
        for name in ("s", "b.json"):
            with self.subTest(resource=name):
                launch = self._launch(cleanup_failure=name)
                self._ready(launch)
                sock, hello = self._connect(launch)
                self.assertEqual(self._call(sock, hello)["state"], "SUCCEEDED")
                sock.close()
                path = str(self.ipc / name)
                report = self._finish(launch, cleanup_failure=path)
                self.assertNotEqual(launch["child"].returncode, 0)
                self.assertEqual(set(report["cleanup_attempts"]), {"s", "a.json", "b.json", "c.json"})


    def _raw(self, launch):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.settimeout(5)
        sock.connect(str(self.endpoint))
        launch["connected"] = True
        return sock

    def _hello(self, label="a", **changes):
        value = dict(schema_version=1, request_id="hello-b06", type="hello",
            transport_token=json.loads(self.credentials[label].read_text())["transport_token"])
        value.update(changes)
        return value

    def _request(self, hello, operation="session.status", target=None, **changes):
        value = dict(schema_version=1, request_id="request-b06", session_id=hello["session_id"],
                     operation=operation, target=target, max_response_bytes=4096)
        value.update(changes)
        return value

    def _encoded(self, value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
        return struct.pack("!I", len(raw)) + raw

    def _events(self, launch):
        path = Path(launch["config"]["events"])
        if not path.exists():
            return []
        values = []
        for line in path.read_text().splitlines():
            try:
                values.append(json.loads(line))
            except ValueError:
                pass  # A serving child may still be appending the last line.
        return values

    def _wait_event(self, launch, kind, timeout=4.5, pumps=()):
        # B has no send deadline until its response is queued. Observe that
        # separately, then observe the real nonblocking send within its budget.
        def wait_for(predicate, seconds, phase):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                for pump in pumps:
                    pump.check()
                events = self._events(launch)
                found = [item for item in events if predicate(item)]
                if found:
                    return found[-1]
                if launch['child'].poll() is not None:
                    self.fail('serving child exited while observing ' + phase)
                if kind == 'would_block' and any(
                        item['kind'] == 'selected_unregistered' for item in events):
                    self.fail('selected peer closed while observing ' + phase)
                time.sleep(0.005)
            self.fail('serving child did not observe ' + phase)

        if kind == 'would_block':
            # Each observer is bounded by one unmodified 5s transport budget
            # plus the existing 1s observer margin, not a new transport deadline.
            margin = self.limits.io_timeout_sec + 1
            queued = wait_for(lambda item: item['kind'] == 'response_queued'
                              and item.get('operation') == 'file.read', margin, 'B response queue')
            observed = wait_for(lambda item: item['kind'] == 'would_block'
                                and item.get('selected'), margin, kind)
            self.assertLess(queued['monotonic'], observed['monotonic'])
            self.assertLess(observed['monotonic'], queued['deadline'])
            return observed
        return wait_for(lambda item: item['kind'] == kind, timeout, kind)

    def _eof(self, sock):
        sock.settimeout(6)  # Observer margin above the unchanged maximum service deadline.
        try:
            data = sock.recv(65536)
        except ConnectionResetError:
            data = b""
        self.assertEqual(data, b"", "connection remained live or appended unexpected bytes")

    def _ac_progress(self, launch, peers=None, close=True):
        for label in ("a", "c"):
            sock, hello = self._connect(launch, label) if peers is None else peers[label]
            reply = self._call(sock, hello, request_id="ac-" + label)
            self.assertEqual(reply["state"], "SUCCEEDED")
            self.assertEqual(reply["status"]["binding"], asdict(self.bindings[label]))
            if close:
                sock.close()

    def test_b06_three_simultaneous_native_contexts_return_exact_data_on_owner_thread(self):
        launch = self._launch()
        self._ready(launch)
        peers = {label: self._connect(launch, label) for label in ("a", "b", "c")}
        self.assertEqual(len({hello["session_id"] for _, hello in peers.values()}), 3)
        self.assertEqual(len({hello["service_incarnation"] for _, hello in peers.values()}), 1)
        # Native calls are synchronous. Keep all three authenticated routes
        # active while alternating native file work and status work; two slow
        # native file calls must not consume another peer's entire idle budget.
        work = (("a", "file.read", "binary.bin"), ("b", "session.status", None),
                ("c", "file.read", "binary.bin"), ("a", "session.status", None),
                ("b", "file.read", "binary.bin"), ("c", "session.status", None),
                ("a", "file.list", "entries"), ("b", "session.status", None),
                ("c", "file.list", "entries"), ("a", "session.status", None),
                ("b", "file.list", "entries"), ("c", "session.status", None))
        seen = {label: set() for label in peers}
        for label, operation, target in work:
            sock, hello = peers[label]
            reply = self._call(sock, hello, operation, target)
            self.assertEqual(reply["state"], "SUCCEEDED")
            self.assertEqual(reply["operation"], operation)
            seen[label].add(operation)
            if operation == "session.status":
                self.assertEqual(reply["status"]["binding"], asdict(self.bindings[label]))
                self.assertEqual(reply["status"]["native"],
                                 {"run_state": "RUNNING", "task_state": "IN_PROGRESS"})
            elif operation == "file.read":
                self.assertEqual(base64.b64decode(reply["output_base64"]),
                                 label.encode() + b"\x00\xff\n")
            else:
                self.assertEqual(json.loads(base64.b64decode(reply["output_base64"])),
                                 {"entries": [label + ".txt"]})
        for operations in seen.values():
            self.assertEqual(operations, {"session.status", "file.read", "file.list"})
        for sock, _ in peers.values():
            sock.close()
        report = self._finish(launch)
        self.assertEqual(report["context_count"], 3)
        self.assertEqual(launch["child"].returncode, 0)

    def _slow(self, phase, trickle=False):
        self.limits = LocalServiceLimits(io_timeout_sec=5, idle_timeout_sec=0.4)
        launch = self._launch()
        self._ready(launch)
        peers = {label: self._connect(launch, label) for label in ("a", "c")}
        if phase == "hello":
            stalled = self._raw(launch)
            raw = self._encoded(self._hello("b"))
            started = time.monotonic()
        else:
            stalled, hello = self._connect(launch, "b")
            raw = self._encoded(self._request(hello, "file.read", "binary.bin"))
            started = time.monotonic()
        stalled.sendall(raw[:1] if phase != "body" else raw[:8])
        self._ac_progress(launch, peers)
        self.assertLess(time.monotonic() - started, 4.7, "A/C finished after B receive deadline")
        if trickle:
            # Real progress continues past the original deadline. It cannot
            # renew that deadline, and the declared frame remains incomplete.
            sent = 8 if phase == "body" else 1
            while time.monotonic() - started < 5.15:
                try:
                    stalled.sendall(raw[sent:sent + 1])
                except (BrokenPipeError, ConnectionResetError):
                    break
                sent += 1
                time.sleep(0.13)
        stalled.settimeout(5.5)
        self._eof(stalled)
        self.assertLess(time.monotonic() - started, 5.8)
        stalled.close()
        fresh, hello = self._connect(launch, "b")
        self.assertEqual(self._call(fresh, hello)["state"], "SUCCEEDED")
        fresh.close()
        self._finish(launch)
        self.assertEqual(launch["child"].returncode, 0)

    def test_b06_slow_hello_does_not_block_a_or_c(self):
        self._slow("hello")

    def test_b06_slow_request_header_does_not_block_a_or_c(self):
        self._slow("header")

    def test_b06_slow_request_body_does_not_block_a_or_c(self):
        self._slow("body")

    def test_b06_trickle_cannot_extend_absolute_hello_deadline(self):
        self._slow("hello", trickle=True)
        launch = self._launch(("a",), peer_close_delay=0.12,
            limits=asdict(LocalServiceLimits(io_timeout_sec=0.6, idle_timeout_sec=0.4)))
        self._ready(launch)
        first = self._raw(launch)
        first.sendall(b"\x00")
        time.sleep(0.05)
        second = self._raw(launch)
        second.sendall(b"\x00")
        self._eof(first)
        self._eof(second)
        first.close()
        second.close()
        report = self._finish(launch)
        self.assertTrue(report["nearest_waits"])
        for item in report["nearest_waits"]:
            self.assertLessEqual(item["requested"], min(0.25, item["available"]) + 0.003,
                "selector wait outlived the nearest absolute deadline after real peer cleanup")

    def test_b06_trickle_cannot_extend_absolute_request_deadline(self):
        self._slow("body", trickle=True)

    def _backpressure_would_block_leaves_a_c_progress_and_send_deadline(self):
        launch = self._launch(small_send_buffer=True, backpressure_probe=True)
        self._ready(launch)
        peers = {label: self._connect(launch, label) for label in ("a", "c")}
        stalled, hello = self._connect(launch, "b")
        pumps = [_StatusPump(self, label, peers[label], phased=True) for label in ('a', 'c')]
        caught = None
        try:
            # Finish initial requests sequentially, preserving each socket and
            # its real receive deadline before introducing B.
            for pump in pumps:
                pump.start()
                self.assertTrue(pump.ready.wait(5), 'pump did not complete initial status: ' + pump.label)
                pump.check()
            def completed_event(request_id):
                # A client can receive the last bytes before the child has
                # appended its completion event. Wait for that real event.
                end = time.monotonic() + 1
                while time.monotonic() < end:
                    for active in pumps:
                        active.check()
                    for item in self._events(launch):
                        if item['kind'] == 'response_complete' and item.get('request_id') == request_id:
                            return item
                    if launch['child'].poll() is not None:
                        break
                    time.sleep(0.005)
                self.fail('child did not report response completion: ' + request_id)

            initial = {('pump-%s-1' % pump.label): completed_event('pump-%s-1' % pump.label)
                       for pump in pumps}
            send_frame(stalled, self._request(hello, "file.read", "large.bin", max_response_bytes=65536), self.limits)
            observed = self._wait_event(launch, 'would_block', pumps=pumps)
            self.assertGreater(observed['sent'], 0)
            queued = next(item for item in self._events(launch)
                          if item['kind'] == 'response_queued' and item.get('operation') == 'file.read')
            deadline = queued['deadline']
            # Earliest original receive budget first. Each pump issues exactly
            # one new request and waits for its actual response before the next.
            for pump in sorted(pumps, key=lambda p: initial['pump-%s-1' % p.label]['deadline']):
                pump.release.set()
                end = time.monotonic() + self.limits.io_timeout_sec + 1
                while not pump.completed.wait(0.01):
                    for active in pumps:
                        active.check()
                    if launch['child'].poll() is not None or time.monotonic() >= end:
                        self.fail('pump did not complete after would_block: ' + pump.label)
                pump.check()
                request_id = 'pump-%s-2' % pump.label
                completed = completed_event(request_id)
                dispatched = next(item for item in self._events(launch)
                                  if item['kind'] == 'dispatch' and item.get('request_id') == request_id)
                self.assertLess(observed['monotonic'], dispatched['monotonic'])
                self.assertLessEqual(dispatched['monotonic'], completed['monotonic'])
                self.assertLess(completed['monotonic'], deadline)
                self.assertEqual(dispatched['fd'], initial['pump-%s-1' % pump.label]['fd'])
                self.assertEqual(completed['fd'], initial['pump-%s-1' % pump.label]['fd'])
        except BaseException as error:
            caught = error
        finally:
            # Stop both, even if the first one failed; preserve all failures.
            for pump in pumps:
                pump.stop_requested.set()
            errors = []
            for pump in pumps:
                try:
                    pump.stop()
                except BaseException as error:
                    errors.append('%s: %s: %s' % (pump.label, type(error).__name__, error))
            if caught is not None or errors:
                events = [item for item in self._events(launch) if item['kind'] in
                    ('dispatch', 'native_start', 'native_end', 'response_queued',
                     'response_complete', 'would_block', 'peer_closed', 'retired')][-30:]
                raise AssertionError('backpressure failure; pump errors=%s; recent events=%s' %
                    (errors, events)) from caught
        for sock, _ in peers.values():
            sock.close()
        closed = self._wait_event(launch, 'selected_unregistered', timeout=6)
        self.assertGreaterEqual(closed['monotonic'], deadline,
            'B closed before its original absolute send deadline')
        stalled.settimeout(1)
        pieces = bytearray()
        while True:
            data = stalled.recv(65536)
            if not data:
                break
            pieces.extend(data)
        self.assertGreater(len(pieces), 4)
        declared = struct.unpack("!I", pieces[:4])[0]
        self.assertLess(len(pieces) - 4, declared)
        stalled.close()
        fresh, new_hello = self._connect(launch, "b")
        self.assertEqual(new_hello["session_id"], hello["session_id"])
        fresh.close()
        report = self._finish(launch)
        self.assertEqual(report["context_count"], 3)
        self.assertEqual(launch["child"].returncode, 0)
        return report

    def test_b06_real_backpressure_would_block_leaves_a_c_progress_and_send_deadline(self):
        self._backpressure_would_block_leaves_a_c_progress_and_send_deadline()

    def test_b06_backpressure_with_slow_real_authority(self):
        # Exercise slow genuine authority during both post-backpressure requests.
        # One delayed check per request bounds injected self-load; every other
        # dispatch and send check still performs its real native work.
        marker = "original_broker_close = broker_module.LocalRunBroker.close"
        dispatch_marker = "def transport_dispatch(self, peer, value):\n"
        dispatch_injection = """    global fixture_slow_request
    fixture_slow_request = value.get('request_id')
"""
        injected = """fixture_slow_request = None
fixture_slowed = set()
actual_status = broker_module.LocalRunContext.status
def slow_status(self, *args, **kwargs):
    request_id = fixture_slow_request
    if request_id in ('pump-a-2', 'pump-c-2') and request_id not in fixture_slowed:
        fixture_slowed.add(request_id)
        event('slow_authority_injected', request_id=request_id, seconds=0.22)
        time.sleep(0.22)
    return actual_status(self, *args, **kwargs)
broker_module.LocalRunContext.status = slow_status
"""
        self.assertEqual(_CHILD.count(marker), 1)
        self.assertEqual(_CHILD.count(dispatch_marker), 1)
        child = _CHILD.replace(marker, injected + marker)
        child = child.replace(dispatch_marker, dispatch_marker + dispatch_injection)
        with mock.patch.dict(globals(), {'_CHILD': child}):
            report = self._backpressure_would_block_leaves_a_c_progress_and_send_deadline()
        slowed = [event['request_id'] for event in report['events']
                  if event['kind'] == 'slow_authority_injected']
        self.assertCountEqual(slowed, ['pump-a-2', 'pump-c-2'])

    def test_b06_backpressure_prequeue_delay_does_not_consume_send_observer(self):
        # The child performs the real B dispatch after a deliberately slow
        # prequeue phase. The queue and send deadlines remain unchanged.
        dispatch_marker = "def transport_dispatch(self, peer, value):\n"
        queue_marker = "def queue(self, peer, value, **kwargs):\n    original_queue(self, peer, value, **kwargs)"
        dispatch_injection = """    global fixture_b_dispatch_start
    if selected(peer) and value.get('operation') == 'file.read':
        fixture_b_dispatch_start = time.monotonic()
"""
        queue_injection = """    if selected(peer) and operation(peer) == 'file.read':
        time.sleep(max(0, fixture_b_dispatch_start + 4.7 - time.monotonic()))
"""
        self.assertEqual(_CHILD.count(dispatch_marker), 1)
        self.assertEqual(_CHILD.count(queue_marker), 1)
        child = _CHILD.replace(dispatch_marker, dispatch_marker + dispatch_injection)
        child = child.replace(queue_marker, "def queue(self, peer, value, **kwargs):\n" +
                              queue_injection + "    original_queue(self, peer, value, **kwargs)")
        with mock.patch.dict(globals(), {'_CHILD': child}):
            launch = self._launch(small_send_buffer=True, backpressure_probe=True)
        self._ready(launch)
        stalled, hello = self._connect(launch, 'b')
        send_frame(stalled, self._request(hello, 'file.read', 'large.bin', max_response_bytes=65536), self.limits)
        observed = self._wait_event(launch, 'would_block')
        queued = next(item for item in self._events(launch) if item['kind'] == 'response_queued')
        self.assertTrue(observed['selected'])
        self.assertGreater(observed['sent'], 0)
        dispatched = next(item for item in self._events(launch)
                          if item['kind'] == 'dispatch' and item.get('operation') == 'file.read')
        self.assertGreater(queued['monotonic'] - dispatched['monotonic'], 4.5)
        self.assertLess(queued['monotonic'], observed['monotonic'])
        self.assertLess(observed['monotonic'], queued['deadline'])
        closed = self._wait_event(launch, 'selected_unregistered', timeout=6)
        self.assertGreaterEqual(closed['monotonic'], queued['deadline'])
        stalled.close()
        report = self._finish(launch)
        self.assertEqual(report['context_count'], 3)
        self.assertEqual(launch['child'].returncode, 0)

    def test_b06_status_pump_reports_original_peer_expiry_and_joins(self):
        launch = self._launch()
        self._ready(launch)
        peer = self._connect(launch, 'a')
        closed = self._wait_event(launch, 'peer_closed', timeout=6)
        self.assertEqual(closed['run_id'], self.bindings['a'].run_id)
        self.assertEqual(closed['phase'], 'receive')
        self.assertGreaterEqual(closed['monotonic'], closed['deadline'])
        pump = _StatusPump(self, 'a', peer)
        pump.start()
        try:
            self.assertTrue(pump.ready.wait(3), 'expired peer pump did not report failure')
        finally:
            with self.assertRaises((OSError, EOFError)):
                pump.stop()
        self.assertFalse(pump.thread.is_alive())
        peer[0].close()
        report = self._finish(launch)
        self.assertEqual(report['context_count'], 3)
        self.assertEqual(launch['child'].returncode, 0)

    def test_b06_current_frame_only_does_not_dispatch_pipeline_during_pending_output(self):
        launch = self._launch(small_send_buffer=True, backpressure_probe=True, stale_read=True)
        self._ready(launch)
        sock, hello = self._connect(launch, "b")
        first = self._request(hello, "file.read", "large.bin", request_id="first", max_response_bytes=65536)
        second = self._request(hello, request_id="second")
        sock.sendall(self._encoded(first) + self._encoded(second))
        self._wait_event(launch, "would_block")
        self.assertFalse(any(item.get("request_id") == "second" for item in self._events(launch)))
        reply = receive_frame(sock, LocalServiceLimits())
        self.assertEqual(base64.b64decode(reply["output_base64"]), (b"b" + bytes(range(256))) * 170)
        self.assertEqual(receive_frame(sock, LocalServiceLimits())["request_id"], "second")
        self._wait_event(launch, "stale_read")
        sock.close()
        report = self._finish(launch)
        events = report["events"]
        first_complete = next(i for i, item in enumerate(events)
                              if item["kind"] == "response_complete" and item.get("request_id") == "first")
        second_dispatch = next(i for i, item in enumerate(events)
                               if item["kind"] == "dispatch" and item.get("request_id") == "second")
        self.assertLess(first_complete, second_dispatch)

    def test_b06_unauthenticated_capacity_overflow_and_connect_flood_remain_bounded(self):
        launch = self._launch(flood_barrier=True)
        self._ready(launch)
        stalled = [self._raw(launch) for _ in range(3)]
        self._wait_event(launch, "registered")
        # Force the existing three accepted peers to be present before overflow.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any(
                item.get("peers") == 3 for item in self._events(launch)):
            time.sleep(0.005)
        overflow = self._raw(launch)
        try:
            overflow.sendall(self._encoded(self._hello("a")))
        except BrokenPipeError:
            pass
        self._eof(overflow)
        overflow.close()
        self.assertFalse(any(item["kind"] == "dispatch" for item in self._events(launch)))
        self.assertEqual(sum(item["kind"] == "peer_allocated" for item in self._events(launch)), 3,
                         "overflow allocated state despite unauthenticated capacity")
        for sock in stalled:
            sock.close()
        time.sleep(0.03)
        a, ah = self._connect(launch, "a")
        c, ch = self._connect(launch, "c")
        # Queue authenticated work before a real burst, without fabricating
        # selector notifications. The child records its actual accept batches.
        self._wait_event(launch, "flood_barrier_ready")
        send_frame(a, self._request(ah, request_id="flood-a"), self.limits)
        send_frame(c, self._request(ch, request_id="flood-c"), self.limits)
        flood = []
        try:
            flood.extend(self._raw(launch) for _ in range(3))
        finally:
            Path(launch["config"]["flood_release"]).write_text("release actual queued sockets")
        # Observe the first real accept batch before replenishing the kernel
        # backlog while A/C's already queued native requests execute.
        end = time.monotonic() + 2
        while not any(event.get("total", 0) >= 9 for event in self._events(launch)
                      if event["kind"] == "accepted"):
            self.assertLess(time.monotonic(), end, "initial real burst was not accepted")
            time.sleep(0.002)
        attempts = [dict(connected=True, error=None) for _ in range(3)]
        for _ in range(9):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            client.settimeout(5)
            flood.append(client)
            try:
                client.connect(str(self.endpoint))
            except ConnectionRefusedError:
                # A full OS listen backlog may reject an overflow before the
                # bounded user-space accept loop acquires a descriptor.
                attempts.append(dict(connected=False, error="ConnectionRefusedError"))
            else:
                attempts.append(dict(connected=True, error=None))
        self.assertEqual(len(attempts), 12)
        self.assertTrue(any(item["connected"] for item in attempts[3:]))
        for label, client in (("a", a), ("c", c)):
            reply = receive_frame(client, LocalServiceLimits())
            self.assertEqual(reply["state"], "SUCCEEDED")
            self.assertEqual(reply["status"]["binding"], asdict(self.bindings[label]))
        for client in flood:
            client.close()
        a.close(); c.close()
        report = self._finish(launch)
        accepted = [event for event in report["events"] if event["kind"] == "accepted"]
        self.b05_runtime_observations[-1]["fixture_flood_attempts"] = attempts
        self.assertGreaterEqual(accepted[-1]["total"], 6 + sum(item["connected"] for item in attempts))
        burst_started = next(event["monotonic"] for event in report["events"]
            if event["kind"] == "flood_listener_ready")
        burst_completed = accepted[-1]["monotonic"]
        for request_id in ("flood-a", "flood-c"):
            completed = next(event["monotonic"] for event in report["events"]
                if event["kind"] == "response_complete" and event.get("request_id") == request_id)
            self.assertGreater(completed, burst_started)
            self.assertLess(completed, burst_completed)
        self.assertEqual(report["max_registered_peers"], 3)
        self.assertTrue(all(value <= 3 for value in report["accept_turns"]))
        self.assertEqual(report["context_count"], 3)

    def test_b06_duplicate_newcomer_preserves_incumbent_and_explicit_fresh_client(self):
        launch = self._launch()
        self._ready(launch)
        incumbent = LocalBridgeClient(self.credentials["b"])
        self.addCleanup(incumbent.close)
        first = incumbent.call("session.status")
        newcomer = self._raw(launch)
        newcomer.sendall(self._encoded(self._hello("b")))
        self._eof(newcomer)
        newcomer.close()
        self.assertEqual(base64.b64decode(incumbent.call("file.read", "binary.bin")["output_base64"]),
                         b"b\x00\xff\n")
        incumbent.close()
        with self.assertRaisesRegex(ValueError, "local_bridge_client_closed"):
            incumbent.call("session.status")
        self._wait_event(launch, "selected_unregistered")
        with closing(LocalBridgeClient(self.credentials["b"])) as fresh:
            second = fresh.call("session.status")
        self.assertEqual((first["session_id"], first["service_incarnation"]),
                         (second["session_id"], second["service_incarnation"]))
        report = self._finish(launch)
        self.assertEqual(report["context_count"], 3)

    def test_b06_eof_malformed_and_receive_timeout_allow_only_explicit_reconnect(self):
        for failure in ("eof", "malformed", "timeout"):
            with self.subTest(failure=failure):
                launch = self._launch(limits=asdict(LocalServiceLimits(io_timeout_sec=1, idle_timeout_sec=0.4)))
                self._ready(launch)
                sock, hello = self._connect(launch, "b")
                if failure == "malformed":
                    sock.sendall(struct.pack("!I", 1) + b"[")
                    reply = receive_frame(sock, LocalServiceLimits())
                    self.assertEqual(reply["error_code"], "local_bridge_invalid_request")
                    self._eof(sock)
                elif failure == "timeout":
                    sock.sendall(b"\x00")
                    self._eof(sock)
                sock.close()
                self._wait_event(launch, "selected_unregistered")
                fresh, next_hello = self._connect(launch, "b")
                self.assertEqual(next_hello["session_id"], hello["session_id"])
                self.assertEqual(self._call(fresh, next_hello)["state"], "SUCCEEDED")
                fresh.close()
                report = self._finish(launch)
                self.assertEqual(report["context_count"], 3)
                self.assertEqual(launch["child"].returncode, 0)

    def test_b06_send_failure_allows_fresh_hello_without_replay_or_new_admission(self):
        launch = self._launch(send_failure=True)
        self._ready(launch)
        sock, hello = self._connect(launch, "b")
        send_frame(sock, self._request(hello, "file.read", "binary.bin"), self.limits)
        self._eof(sock)
        self._wait_event(launch, "send_failure")
        sock.close()
        fresh, next_hello = self._connect(launch, "b")
        self.assertEqual(next_hello["session_id"], hello["session_id"])
        self.assertEqual(self._call(fresh, next_hello)["state"], "SUCCEEDED")
        fresh.close()
        report = self._finish(launch)
        self.assertEqual(sum(item["kind"] == "read" for item in report["context_calls"]), 1)
        self.assertEqual(report["context_count"], 3)
        self.assertEqual(launch["child"].returncode, 0)

    def test_b06_partial_output_disconnect_and_stale_descriptor_event_preserve_replacement(self):
        launch = self._launch(small_send_buffer=True, backpressure_probe=True, stale_event=True)
        self._ready(launch)
        sock, hello = self._connect(launch, "b")
        send_frame(sock, self._request(hello, "file.read", "large.bin", max_response_bytes=65536), self.limits)
        self._wait_event(launch, "would_block")
        prefix = sock.recv(9)
        self.assertEqual(len(prefix), 9)
        sock.close()
        self._wait_event(launch, "selected_unregistered")
        fresh, next_hello = self._connect(launch, "b")
        self.assertEqual(next_hello["session_id"], hello["session_id"])
        stale = self._wait_event(launch, "stale_event")
        self.assertEqual(stale["old_fd"], stale["replacement_fd"], "OS did not recycle controlled peer descriptor")
        self.assertEqual(self._call(fresh, next_hello, "file.read", "binary.bin")["state"], "SUCCEEDED")
        fresh.close()
        self._ac_progress(launch)
        report = self._finish(launch)
        self.assertEqual(sum(item["kind"] == "read" and item["run_id"] == self.bindings["b"].run_id
                             for item in report["context_calls"]), 2)
        self.assertEqual(report["context_count"], 3)

    def test_b06_six_cross_session_pairs_never_select_foreign_context(self):
        launch = self._launch()
        self._ready(launch)
        sessions = {}
        for label in ("a", "b", "c"):
            sock, hello = self._connect(launch, label)
            sessions[label] = hello["session_id"]
            sock.close()
        for selected in ("a", "b", "c"):
            for foreign in ("a", "b", "c"):
                if selected == foreign:
                    continue
                for operation, target in (("session.status", None), ("file.read", "binary.bin"), ("file.list", "entries")):
                    with self.subTest(selected=selected, foreign=foreign, operation=operation):
                        sock, hello = self._connect(launch, selected)
                        reply = self._call(sock, hello, operation, target, session_id=sessions[foreign])
                        self.assertEqual((reply["state"], reply["error_code"], reply["output_base64"]),
                                         ("REFUSED", "local_bridge_session_denied", ""))
                        self.assertEqual(reply["session_id"], sessions[selected])
                        self.assertNotIn("status", reply)
                        sock.close()
        report = self._finish(launch)
        self.assertFalse(any(item["kind"] in ("read", "list") for item in report["context_calls"]))

    def test_b06_wire_identity_schema_process_mutation_and_budget_denials(self):
        launch = self._launch()
        self._ready(launch)
        for field in ("binding", "capability_file", "credential_file", "service_id", "session_id"):
            sock, reply = self._connect(launch, **{field: "injected"})
            self.assertEqual(reply["error_code"], "local_bridge_invalid_request")
            sock.close()
        for field in ("binding", "capability_file", "credential_file", "service_id", "transport_token"):
            sock, hello = self._connect(launch)
            reply = self._call(sock, hello, **{field: "injected"})
            self.assertEqual(reply["error_code"], "local_bridge_invalid_request")
            sock.close()
        invalid = [dict(operation=operation, target="binary.bin") for operation in
                   ("file.patch", "process.start", "process.poll", "process.terminate")]
        invalid.extend([dict(schema_version=True), dict(request_id="bad request"), dict(target="../secret.txt", operation="file.read"),
                        dict(session_id="x"), dict(max_response_bytes=True), dict(max_response_bytes=65537)])
        for changes in invalid:
            sock, hello = self._connect(launch)
            reply = self._call(sock, hello, **changes)
            self.assertEqual((reply["state"], reply["error_code"], reply["output_base64"]),
                             ("REFUSED", "local_bridge_invalid_request", ""))
            sock.close()
        sock, hello = self._connect(launch)
        self.assertEqual(self._call(sock, hello, "file.read", "large.bin")["error_code"],
                         "local_bridge_response_too_large")
        self.assertEqual(self._call(sock, hello, "file.read", "secret.txt")["error_code"],
                         "local_bridge_operation_denied")
        sock.close()
        for budget in (0, 1, 16):
            sock, hello = self._connect(launch)
            send_frame(sock, self._request(hello, max_response_bytes=budget), self.limits)
            self._eof(sock)
            sock.close()
        for raw in (struct.pack("!I", 0), struct.pack("!I", 65537),
                    self._encoded({"schema_version": 2}),
                    struct.pack("!I", 5) + b"{x:x}",
                    struct.pack("!I", 13) + b'{"x":1,"x":2}'):
            sock = self._raw(launch)
            sock.sendall(raw)
            self.assertEqual(receive_frame(sock, LocalServiceLimits())["error_code"],
                             "local_bridge_invalid_request")
            sock.close()
        self._finish(launch)

    def _revocation(self, operation, target, phase, replace_credential=False):
        launch = self._launch(revoke_operation=operation, revoke_phase=phase)
        self._ready(launch)
        old = json.loads(self.credentials["b"].read_text())
        b, hello = self._connect(launch, "b")
        replacements = None
        if replace_credential:
            path = self.credentials["b"]
            held = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            self.addCleanup(os.close, held)
            path.unlink()
            path.write_bytes(b"foreign replacement of retired credential")
            path.chmod(0o600)
            replacements = {str(path): _path_state(path)}
        send_frame(b, self._request(hello, operation, target, max_response_bytes=65536), self.limits)
        if phase == "partial":
            raw = bytearray()
            while True:
                data = b.recv(65536)
                if not data:
                    break
                raw.extend(data)
            self.assertEqual(len(raw), 11, "partial success was appended/replayed")
            self.assertGreater(struct.unpack("!I", raw[:4])[0], len(raw) - 4)
        else:
            reply = receive_frame(b, LocalServiceLimits())
            self.assertEqual((reply["state"], reply["error_code"], reply["output_base64"]),
                             ("REFUSED", "local_bridge_authority_denied", ""))
            self.assertNotIn("status", reply)
        self._wait_event(launch, "retired")
        b.close()
        if replace_credential:
            self.assertEqual(_path_state(self.credentials["b"]), replacements[str(self.credentials["b"])])
        else:
            self.assertFalse(self.credentials["b"].exists())
        self.assertEqual(stat.S_IMODE(self.caps["b"].stat().st_mode), 0o600,
                         "fixture did not restore native authority for nonresurrection check")
        # A fresh hello using the old token remains refused after restoration.
        stale = self._raw(launch)
        stale.sendall(self._encoded(dict(schema_version=1, request_id="stale", type="hello",
                                        transport_token=old["transport_token"])))
        self.assertEqual(receive_frame(stale, LocalServiceLimits())["error_code"],
                         "local_bridge_auth_denied")
        stale.close()
        self._ac_progress(launch)
        report = self._finish(launch, replacements=replacements)
        self.assertTrue(report["revoked"])
        if phase == "construction":
            self.assertTrue(report["revoke_response_constructed"])
        self.assertNotEqual(launch["child"].returncode, 0)
        return report

    def test_b06_native_revoke_after_complete_status_construction_retires_only_b(self):
        self._revocation("session.status", None, "construction")

    def test_b06_native_revoke_after_complete_read_construction_retires_only_b(self):
        self._revocation("file.read", "binary.bin", "construction")

    def test_b06_native_revoke_after_complete_list_construction_retires_only_b(self):
        self._revocation("file.list", "entries", "construction")

    def test_b06_native_revoke_before_dispatch_discards_b_and_preserves_a_c(self):
        report = self._revocation("file.read", "binary.bin", "before_dispatch")
        self.assertFalse(any(item["kind"] == "read" and item["run_id"] == self.bindings["b"].run_id
                             for item in report["context_calls"]))

    def test_b06_native_revoke_queued_success_is_refused_before_first_send(self):
        report = self._revocation("file.read", "binary.bin", "queued")
        queued = next(item for item in report["events"] if item["kind"] == "revoked")
        self.assertEqual(queued["sent"], 0)
        self.assertGreater(queued["buffered"], 0)

    def test_b06_native_revoke_after_controlled_partial_send_closes_without_refusal(self):
        self._revocation("file.read", "large.bin", "partial")

    def test_b06_sigint_sigterm_at_startup_receive_send_and_accept_unwind_independently(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for phase in ("parent", "broker", "selector", "listener_created", "listener_bound", "accept", "receive", "send"):
                with self.subTest(signum=signum, phase=phase):
                    launch = self._launch(("a",), signal_phase=phase, signum=int(signum))
                    if phase in ("accept", "receive", "send"):
                        self._ready(launch)
                        sock = self._raw(launch)
                        if phase != "accept":
                            sock.sendall(self._encoded(self._hello("a")))
                        try:
                            while sock.recv(4096):
                                pass
                        except ConnectionResetError:
                            pass
                        sock.close()
                    report = self._finish(launch)
                    self.assertTrue(report["triggered"], phase)
                    self.assertEqual(launch["child"].returncode, 0)

    def test_b06_retirement_failure_remains_sticky_through_sigint_and_sigterm(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            launch = self._launch(revoke_operation="file.read", revoke_phase="queued")
            self._ready(launch)
            b, hello = self._connect(launch, "b")
            self._call(b, hello, "file.read", "binary.bin")
            self._wait_event(launch, "retired")
            b.close()
            launch["child"].send_signal(signum)
            self._finish(launch)
            self.assertNotEqual(launch["child"].returncode, 0)

    def test_b06_cleanup_failures_do_not_suppress_other_resource_closes(self):
        for resource in ("peer", "selector", "broker", "listener", "parent"):
            launch = self._launch(resource_close_failure=resource)
            self._ready(launch)
            sock, hello = self._connect(launch)
            self.assertEqual(self._call(sock, hello)["state"], "SUCCEEDED")
            sock.close()
            report = self._finish(launch)
            self.assertNotEqual(launch["child"].returncode, 0, resource)
            self.assertTrue({"selector", "broker", "listener"}.issubset(report["close_attempts"]))
            self.assertEqual(set(report["cleanup_attempts"]), {"s", "a.json", "b.json", "c.json"})


    def test_b06_all_routes_retired_end_promptly_without_idle_wait(self):
        launch = self._launch(("b",), revoke_operation="file.read", revoke_phase="queued",
                              limits=asdict(LocalServiceLimits(io_timeout_sec=5, idle_timeout_sec=30)))
        self._ready(launch)
        held_unauthenticated = [self._raw(launch) for _ in range(2)]
        b, hello = self._connect(launch, "b")
        reply = self._call(b, hello, "file.read", "binary.bin")
        self.assertEqual(reply["error_code"], "local_bridge_authority_denied")
        started = time.monotonic()
        retired = self._wait_event(launch, "retired")
        self.assertEqual(retired["peers"], 3)
        b.close()
        self._finish(launch)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertNotEqual(launch["child"].returncode, 0)

    def test_b06_paused_native_project_keeps_existing_run_read_authority(self):
        original = Orchestrator.connect
        def setup_connection(owner):
            conn = original(owner)
            self.native_connections.append(conn)
            return conn
        with mock.patch.object(Orchestrator, "connect", setup_connection):
            result = self.orchestrator.pause_project(self.bindings["b"].project_id,
                "fixture pauses new claims while the already-running B attempt remains authorized")
        self.assertEqual(result["status"], "PROJECT_PAUSED")
        launch = self._launch()
        self._ready(launch)
        b, hello = self._connect(launch, "b")
        self.assertEqual(base64.b64decode(self._call(b, hello, "file.read", "binary.bin")["output_base64"]),
                         b"b\x00\xff\n")
        b.close()
        self._finish(launch)
        self.assertEqual(launch["child"].returncode, 0)


    def test_b06_selected_retirement_preserves_replaced_b_credential(self):
        self._revocation("file.read", "binary.bin", "queued", replace_credential=True)

    def test_b06_cleanup_failure_preserves_unrelated_foreign_replacements(self):
        launch = self._launch(cleanup_failure="b.json")
        self._ready(launch)
        sock, hello = self._connect(launch)
        self.assertEqual(self._call(sock, hello)["state"], "SUCCEEDED")
        sock.close()
        held = os.open(self.credentials["c"], os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, held)
        replacements = {}
        self.endpoint.unlink()
        self.endpoint.symlink_to(self.repos["d"] / "secret.txt")
        self.credentials["c"].unlink()
        self.credentials["c"].write_bytes(b"foreign credential survives another resource's unlink failure")
        self.credentials["c"].chmod(0o600)
        for path in (self.endpoint, self.credentials["c"]):
            replacements[str(path)] = _path_state(path)
        report = self._finish(launch, replacements=replacements,
                              cleanup_failure=str(self.credentials["b"]))
        self.assertNotEqual(launch["child"].returncode, 0)
        self.assertEqual(set(report["cleanup_attempts"]), {"s", "a.json", "b.json", "c.json"})
        for path, expected in replacements.items():
            self.assertEqual(_path_state(path), expected)


if __name__ == "__main__":
    unittest.main()
