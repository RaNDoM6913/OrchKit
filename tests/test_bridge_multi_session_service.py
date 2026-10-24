"""Real native-ledger and child-owned AF_UNIX acceptance for B05.

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
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import orch
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
import hashlib, json, os, signal, sqlite3, stat, sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, sys.argv[1])
import orch
from orch import bridge_local_service as service
from orch import bridge_local_broker as broker_module
from orch import bridge_readonly_authority as authority_module
from orch import bridge_file_scope as scope_module
from orch.worker_operations import WorkerRunBinding

config = json.loads(Path(sys.argv[2]).read_text())
report = dict(connections=[], acquired={}, cleanup_attempts=[], context_calls=[],
              scopes=[], context_count=0, triggered=False)
contexts, scope_objects, held_fds = [], [], []
result = 1

def state(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    value = dict(exists=True, dev=info.st_dev, ino=info.st_ino,
                 uid=info.st_uid, mode=info.st_mode)
    if stat.S_ISREG(info.st_mode):
        raw = path.read_bytes()
        value.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    if stat.S_ISLNK(info.st_mode):
        value['target'] = os.readlink(path)
    return value

original_sqlite_connect = sqlite3.connect
original_connection = authority_module._ReadOnlyConnection
pending_mode = None

class ObservedConnection(original_connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observation = dict(connection_id='%d:%d:%d' %
            (os.getpid(), id(self), len(report['connections'])),
            mode=pending_mode, closed=False)
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

original_open = broker_module.LocalRunBroker.open_context
def open_context(self, *args, **kwargs):
    report['context_count'] += 1
    number = report['context_count']
    if config.get('context_failure') == number and config.get('failure_when') == 'before':
        raise OSError('fixture later-context acquisition failure')
    context = original_open(self, *args, **kwargs)
    contexts.append(context)
    if config.get('context_failure') == number:
        raise OSError('fixture later-context transfer failure')
    if config.get('signal_context') == number:
        report['triggered'] = True
        signal.raise_signal(config['signum'])
    return context
broker_module.LocalRunBroker.open_context = open_context

for name in ('read', 'list', 'status'):
    original = getattr(broker_module.LocalRunContext, name)
    def observed(self, *args, _name=name, _original=original, **kwargs):
        report['context_calls'].append(dict(kind=_name, run_id=self.binding.run_id))
        return _original(self, *args, **kwargs)
    setattr(broker_module.LocalRunContext, name, observed)

original_publish = service._publish_credential
original_os_open, original_os_fstat, original_os_close = os.open, os.fstat, os.close
credential_fd = None
def stop_credential(boundary):
    if config.get('signal_boundary') == boundary and not report['triggered']:
        report['triggered'] = True
        signal.raise_signal(config['signum'])

def open_fd(name, flags, *args, **kwargs):
    global credential_fd
    fd = original_os_open(name, flags, *args, **kwargs)
    if name == config.get('signal_credential') and flags & os.O_EXCL:
        credential_fd = fd
        stop_credential('open')
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

def trace_publish(frame, event, arg):
    if (frame.f_code is original_publish.__code__ and event == 'line'
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
        raise OSError('fixture later-credential acquisition failure')
    identity = original_publish(parent_fd, name, value, **kwargs)
    report['acquired'][path] = state(path)
    if config.get('credential_failure') == name:
        raise OSError('fixture later-credential publication failure')
    if config.get('signal_credential') == name:
        stop_credential('published')
    return identity
service._publish_credential = publish

original_socket = service.socket.socket
class ObservedSocket(original_socket):
    def bind(self, address):
        outcome = super().bind(address)
        report['acquired'][str(address)] = state(address)
        # File-scope admission checks platform primitive identities. Install
        # credential-only boundary observers after all real scopes admitted.
        if config.get('signal_credential'):
            service.os.open, service.os.fstat, service.os.close = open_fd, fstat_fd, close_fd
        return outcome
service.socket.socket = ObservedSocket

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

def previous_handler(_signum, _frame):
    pass
for signum in (signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, previous_handler)

def profile(frame, event, value):
    if (event == 'return' and frame.f_code.co_name == '_dispatch_context'
            and frame.f_globals is service.__dict__ and isinstance(value, dict)
            and value.get('state') == 'SUCCEEDED'
            and value.get('operation') == config.get('revoke_operation')
            and not report['triggered']):
        context = frame.f_locals['context']
        report['revoke_response_constructed'] = (
            ('status' in value) if config['revoke_operation'] == 'session.status'
            else bool(value.get('output_base64')))
        context.close()
        report['triggered'] = True
        sys.setprofile(None)

try:
    admissions = [service.LocalBridgeAdmission(
        WorkerRunBinding(**item['binding']), Path(item['capability_file']),
        Path(item['credential_file'])) for item in config['admissions']]
    kind = config.get('container', 'tuple')
    admissions = tuple(admissions) if kind == 'tuple' else (
        admissions if kind == 'list' else (item for item in admissions))
    if config.get('revoke_operation'):
        sys.setprofile(profile)
    if config.get('signal_boundary') == 'success':
        sys.settrace(trace_publish)
    result = service.serve_local_bridge_sessions(
        authority_module.ReadOnlyAuthorityLedger(Path(config['home'])),
        admissions, Path(config['socket']), service.LocalServiceLimits(**config['limits']))
except BaseException as error:
    report['error'] = type(error).__name__ + ': ' + str(error)
finally:
    sys.setprofile(None)
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


class MultiSessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.b05_service_children = []
        self.b05_runtime_observations = []
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-b05-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
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
                self._git("init", "-q", "-b", "main", str(repo))
                self._git("-C", str(repo), "config", "user.email", "fixture@example.invalid")
                self._git("-C", str(repo), "config", "user.name", "Fixture")
                (repo / "binary.bin").write_bytes(label.encode("ascii") + b"\x00\xff\n")
                (repo / "secret.txt").write_bytes(("private-" + label).encode("ascii"))
                (repo / "entries").mkdir()
                (repo / "entries" / (label + ".txt")).write_bytes(b"unread child content")
                self._git("-C", str(repo), "add", ".")
                self._git("-C", str(repo), "commit", "-qm", "fixture")
                project = registry.add(repo, name="B05 fixture " + label, profile="safe",
                    review_mode="off", allow_commit=True, allow_push=False)["project"]
                plan = build_single_task_plan(project, task_id="B05-" + label.upper(),
                    goal="Disposable multi-session endpoint", allowed_paths=["binary.bin", "entries/"],
                    plan_revision="b05-" + label)
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
        self.limits = LocalServiceLimits(io_timeout_sec=0.5, idle_timeout_sec=1.0)
        self.active = []
        self.addCleanup(self._finish_remaining)

    def _git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
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
                      limits=asdict(self.limits))
        config.update(changes)
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
            stderr=subprocess.PIPE, text=True, cwd=str(self.base))
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
        observation = dict(subject=launch["config"].get("subject", "B05 actual endpoint lifecycle"),
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
        self.assertTrue(report["signals_restored"], "service did not restore original signal handlers")
        self.assertTrue(all(item["root_fd"] == -1 for item in report["scopes"]), report["scopes"])
        return report

    def _connect(self, launch, label="a", **hello_changes):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.settimeout(2)
        sock.connect(str(self.endpoint))
        launch["connected"] = True
        credential = json.loads(self.credentials[label].read_text())
        hello = dict(schema_version=1, request_id="hello-b05", type="hello",
                     transport_token=credential["transport_token"])
        hello.update(hello_changes)
        send_frame(sock, hello, self.limits)
        return sock, receive_frame(sock, LocalServiceLimits())

    def _call(self, sock, hello, operation="session.status", target=None, **changes):
        request = dict(schema_version=1, request_id="call-b05", session_id=hello["session_id"],
                       operation=operation, target=target, max_response_bytes=4096)
        request.update(changes)
        send_frame(sock, request, self.limits)
        return receive_frame(sock, LocalServiceLimits())

    def test_b05_three_native_contexts_route_exact_sequential_client_results(self):
        launch = self._launch()
        self._ready(launch)
        credentials = [json.loads(self.credentials[label].read_text()) for label in ("a", "b", "c")]
        self.assertEqual(len({item["service_incarnation"] for item in credentials}), 1)
        self.assertEqual(len({item["transport_token"] for item in credentials}), 3)
        sessions = set()
        for label in ("a", "b", "c"):
            with closing(LocalBridgeClient(self.credentials[label])) as client:
                reply = client.call("session.status")
                sessions.add(reply["session_id"])
                self.assertEqual(reply["status"]["binding"], asdict(self.bindings[label]))
                self.assertEqual(reply["status"]["native"],
                                 {"run_state": "RUNNING", "task_state": "IN_PROGRESS"})
                self.assertEqual(base64.b64decode(client.call("file.read", "binary.bin")["output_base64"]),
                                 label.encode("ascii") + b"\x00\xff\n")
                self.assertEqual(json.loads(base64.b64decode(
                    client.call("file.list", "entries")["output_base64"])), {"entries": [label + ".txt"]})
            launch["connected"] = True
        self.assertEqual(len(sessions), 3)
        self._finish(launch)
        self.assertEqual(launch["child"].returncode, 0)

    def test_b05_single_and_two_admission_new_entrypoints_serve_their_native_runs(self):
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

    def test_b05_every_cross_session_pair_is_refused_before_adapter_dispatch(self):
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
                for operation, target in (("session.status", None), ("file.read", "binary.bin"),
                                          ("file.list", "entries")):
                    with self.subTest(selected=selected, foreign=foreign, operation=operation):
                        sock, hello = self._connect(launch, selected)
                        reply = self._call(sock, hello, operation, target, session_id=sessions[foreign])
                        self.assertEqual((reply["state"], reply["error_code"], reply["output_base64"]),
                                         ("REFUSED", "local_bridge_session_denied", ""))
                        self.assertNotIn("status", reply)
                        self.assertEqual(reply["session_id"], sessions[selected])
                        sock.close()
        report = self._finish(launch)
        # Twenty-one actual authenticated hellos require authority status; the
        # eighteen forged session requests must add no dispatched context call.
        self.assertEqual(len(report["context_calls"]), 21, report["context_calls"])
        self.assertEqual({item["kind"] for item in report["context_calls"]}, {"status"})
        self.assertEqual(launch["child"].returncode, 0)

    def test_b05_complete_b_credential_at_a_path_can_only_select_b(self):
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

    def test_b05_unknown_and_previous_incarnation_tokens_and_sessions_are_denied(self):
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

    def test_b05_wire_v1_rejects_identity_injection_and_all_mutation_process_operations(self):
        launch = self._launch()
        self._ready(launch)
        for field in ("binding", "capability_file", "credential_file", "service_id", "session_id"):
            with self.subTest(hello_field=field):
                sock, reply = self._connect(launch, **{field: "injected"})
                self.assertEqual(reply["error_code"], "local_bridge_invalid_request")
                sock.close()
        for field in ("binding", "capability_file", "credential_file", "service_id", "transport_token"):
            with self.subTest(request_field=field):
                sock, hello = self._connect(launch)
                reply = self._call(sock, hello, **{field: "injected"})
                self.assertEqual((reply["state"], reply["error_code"], reply["output_base64"]),
                                 ("REFUSED", "local_bridge_invalid_request", ""))
                sock.close()
        for operation in ("file.patch", "process.start", "process.poll", "process.terminate"):
            with self.subTest(operation=operation):
                sock, hello = self._connect(launch)
                reply = self._call(sock, hello, operation, "binary.bin")
                self.assertEqual((reply["state"], reply["error_code"], reply["output_base64"]),
                                 ("REFUSED", "local_bridge_invalid_request", ""))
                sock.close()
        report = self._finish(launch)
        self.assertEqual(len(report["context_calls"]), 9, report["context_calls"])
        self.assertEqual({item["kind"] for item in report["context_calls"]}, {"status"})
        self.assertEqual(launch["child"].returncode, 0)

    def test_b05_invalid_static_admissions_fail_without_publishing_resources(self):
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

    def test_b05_noncanonical_and_nonprivate_endpoint_parents_are_refused_unchanged(self):
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

    def _final_revocation(self, operation, target):
        launch = self._launch(revoke_operation=operation)
        self._ready(launch)
        sock, hello = self._connect(launch, "b")
        reply = self._call(sock, hello, operation, target)
        self.assertEqual((reply["state"], reply["error_code"], reply["output_base64"]),
                         ("REFUSED", "local_bridge_authority_denied", ""))
        self.assertNotIn("status", reply)
        sock.close()
        report = self._finish(launch)
        self.assertTrue(report["triggered"], "completed response return boundary was not reached")
        self.assertTrue(report["revoke_response_constructed"], "revocation preceded complete response")
        self.assertNotEqual(launch["child"].returncode, 0)

    def test_b05_final_send_rechecks_read_after_complete_response_construction(self):
        self._final_revocation("file.read", "binary.bin")

    def test_b05_final_send_rechecks_list_after_complete_response_construction(self):
        self._final_revocation("file.list", "entries")

    def test_b05_final_send_rechecks_status_after_complete_response_construction(self):
        self._final_revocation("session.status", None)

    def test_b05_later_context_acquisition_failures_close_all_acquired_scopes(self):
        for number in (2, 3):
            for when in ("before", "after"):
                with self.subTest(context=number, when=when):
                    launch = self._launch(context_failure=number, failure_when=when)
                    report = self._finish(launch)
                    self.assertEqual(report["context_count"], number)
                    self.assertNotEqual(launch["child"].returncode, 0)
                    self.assertEqual(len(report["scopes"]), number - (when == "before"))
                    self.assertEqual(report["acquired"], {})

    def test_b05_later_credential_failures_clean_every_previously_owned_path(self):
        for label in ("b", "c"):
            for when in ("before", "after"):
                with self.subTest(credential=label, when=when):
                    launch = self._launch(credential_failure=label + ".json", failure_when=when)
                    report = self._finish(launch)
                    self.assertNotEqual(launch["child"].returncode, 0)
                    self.assertIn(str(self.endpoint), report["acquired"])
                    self.assertIn(str(self.credentials["a"]), report["acquired"])
                    self.assertEqual(len(report["scopes"]), 3)

    def test_b05_sigint_and_sigterm_at_later_context_transfer_unwind_owned_scopes(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for number in (2, 3):
                with self.subTest(signum=signum, context=number):
                    launch = self._launch(signal_context=number, signum=int(signum))
                    report = self._finish(launch)
                    self.assertTrue(report["triggered"])
                    self.assertEqual(len(report["scopes"]), number)
                    self.assertEqual(launch["child"].returncode, 0)

    def test_b05_sigint_and_sigterm_at_later_credential_ownership_boundaries(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for boundary in ("open", "identity", "owned", "success", "close", "published"):
                with self.subTest(signum=signum, boundary=boundary):
                    launch = self._launch(signal_credential="b.json", signal_boundary=boundary,
                                          signum=int(signum))
                    report = self._finish(launch)
                    self.assertTrue(report["triggered"], boundary)
                    self.assertEqual(launch["child"].returncode, 0)
                    self.assertIn(str(self.credentials["a"]), report["acquired"])

    def test_b05_preexisting_socket_and_credential_objects_survive_startup_refusal(self):
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

    def test_b05_replaced_owned_socket_and_later_credential_survive_exactly(self):
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

    def test_b05_cleanup_failure_reports_failure_but_attempts_all_other_owned_paths(self):
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


if __name__ == "__main__":
    unittest.main()
