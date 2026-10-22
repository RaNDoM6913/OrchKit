"""Disposable native authority and real Unix-socket lifecycle fixtures."""

import base64
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from orch import bridge_local_service as service
from orch.bridge_local_client import LocalBridgeClient
from orch.bridge_local_protocol import LocalServiceLimits, receive_frame, send_frame
from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerRunBinding


class LocalServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-ipc-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / "repo"
        self._git("init", "-q", "-b", "main", str(self.repo))
        self._git("-C", str(self.repo), "config", "user.email", "fixture@example.invalid")
        self._git("-C", str(self.repo), "config", "user.name", "Fixture")
        (self.repo / "binary.bin").write_bytes(b"\x00\xff\x80\n")
        (self.repo / "oversize.bin").write_bytes(b"x" * 4096)
        (self.repo / "private.txt").write_bytes(b"private-fixture-content")
        (self.repo / "dir").mkdir()
        (self.repo / "dir" / "a.txt").write_bytes(b"never-listed-content")
        self._git("-C", str(self.repo), "add", ".")
        self._git("-C", str(self.repo), "commit", "-qm", "fixture")
        (self.base / "home").mkdir(mode=0o700)
        self.orch = Orchestrator(self.base / "home")
        project = ProjectRegistry(self.orch.root).add(
            self.repo, name="Disposable IPC", profile="safe", review_mode="off",
            allow_commit=True, allow_push=False,
        )["project"]
        plan = build_single_task_plan(
            project, task_id="IPC-TEST", goal="Bounded local IPC fixture",
            allowed_paths=["binary.bin", "oversize.bin", "dir/", "link.bin"],
            plan_revision="ipc-fixture",
        )
        plan_file = self.base / "plan.json"
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        claim = self.orch.claim("fixture-ipc", project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED")
        self.cap = Path(claim["capability_file"])
        self.binding = WorkerRunBinding(
            project["project_id"], project["writer_key"], claim["task_id"],
            claim["run_id"], claim["attempt"],
        )
        self.ledger = ReadOnlyAuthorityLedger(self.orch.root)
        self.directory = self.base / "ipc"
        self.directory.mkdir(mode=0o700)
        self.endpoint = self.directory / "s"
        self.credential = self.directory / "c.json"
        self.limits = LocalServiceLimits(io_timeout_sec=0.2, idle_timeout_sec=0.25)
        self.threads = []
        self.addCleanup(self._join_all)

    def _git(self, *args):
        env = dict(os.environ)
        env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              capture_output=True, text=True, check=True, env=env, timeout=12)

    def _join_all(self):
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "foreground service did not exit")

    def _start(self, **kwargs):
        ready = threading.Event()
        results = []
        completed = kwargs.get("completed")
        original = service._publish_credential

        def publish(*args, **kw):
            identity = original(*args, **kw)
            ready.set()
            return identity

        def run():
            try:
                results.append(service.serve_local_bridge(
                    self.ledger, self.binding, self.cap, self.endpoint,
                    self.credential, kwargs.get("limits", self.limits),
                ))
            finally:
                ready.set()
                if completed is not None:
                    completed.set()

        with mock.patch.object(service, "_publish_credential", side_effect=publish):
            thread = threading.Thread(target=run)
            self.threads.append(thread)
            thread.start()
            self.assertTrue(ready.wait(3), "service readiness was not signalled")
        self.assertTrue(self.credential.is_file(), results)
        return thread, results

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(str(self.endpoint))
        self.addCleanup(sock.close)
        return sock

    def _hello(self, sock, **changes):
        credential = json.loads(self.credential.read_text())
        hello = {"schema_version": 1, "request_id": "hello-test", "type": "hello",
                 "transport_token": credential["transport_token"]}
        hello.update(changes)
        send_frame(sock, hello, self.limits)
        return receive_frame(sock, LocalServiceLimits())

    def _call(self, sock, hello, **changes):
        request = {"schema_version": 1, "request_id": "call-test", "session_id": hello["session_id"],
                   "operation": "session.status", "target": None, "max_response_bytes": 4096}
        request.update(changes)
        send_frame(sock, request, self.limits)
        return receive_frame(sock, LocalServiceLimits())

    def _native_state(self):
        with self.orch.connect() as conn:
            return tuple(conn.execute("SELECT state,lease_token FROM runs WHERE run_id=?", (self.binding.run_id,)).fetchone())

    def test_separate_client_calls_read_binary_list_names_and_keep_writer(self):
        before = self._native_state()
        thread, results = self._start()
        sessions = []
        for operation, target in (("session.status", None), ("file.read", "binary.bin"), ("file.list", "dir")):
            client = LocalBridgeClient(self.credential)
            try:
                reply = client.call(operation, target)
            finally:
                client.close()
            self.assertEqual(reply["state"], "SUCCEEDED", reply)
            self.assertLessEqual(len(json.dumps(reply, sort_keys=True, separators=(",", ":")).encode("ascii")), 4096)
            sessions.append(reply["session_id"])
            if operation == "file.read":
                self.assertEqual(base64.b64decode(reply["output_base64"]), b"\x00\xff\x80\n")
            if operation == "file.list":
                self.assertEqual(json.loads(base64.b64decode(reply["output_base64"])), {"entries": ["a.txt"]})
            if operation == "session.status":
                self.assertEqual(reply["status"]["native"], {"run_state": "RUNNING", "task_state": "IN_PROGRESS"})
                self.assertEqual(reply["status"]["limits"]["io_timeout_sec"], 0.2)
                for secret in (str(self.cap), str(self.repo), before[1], "private-fixture-content"):
                    self.assertNotIn(secret, json.dumps(reply))
        self.assertEqual(len(set(sessions)), 1)
        thread.join(3)
        self.assertEqual(results, [0])
        self.assertEqual(self._native_state(), before)
        self.assertFalse(self.endpoint.exists())
        self.assertFalse(self.credential.exists())

    def test_endpoint_and_credential_are_private(self):
        self._start()
        self.assertTrue(stat.S_ISSOCK(self.endpoint.lstat().st_mode))
        self.assertEqual(self.endpoint.lstat().st_mode & 0o777, 0o600)
        self.assertEqual(self.credential.stat().st_mode & 0o777, 0o600)

    def test_wrong_token_foreign_session_and_mutation_are_fixed_refusals(self):
        self._start()
        sock = self._connect()
        reply = self._hello(sock, transport_token="wrong" * 8)
        self.assertEqual((reply["state"], reply["error_code"]), ("REFUSED", "local_bridge_auth_denied"))
        sock.close()
        sock = self._connect()
        hello = self._hello(sock)
        reply = self._call(sock, hello, session_id="foreign" * 5)
        self.assertEqual(reply["state"], "REFUSED")
        sock.close()
        sock = self._connect()
        hello = self._hello(sock)
        reply = self._call(sock, hello, operation="process.start", target="anything")
        self.assertEqual((reply["state"], reply["error_code"]), ("REFUSED", "local_bridge_invalid_request"))

    def test_complete_response_budget_refuses_data_fitting_only_raw_budget(self):
        self._start()
        client = LocalBridgeClient(self.credential)
        self.addCleanup(client.close)
        reply = client.call("file.read", "oversize.bin")
        self.assertEqual((reply["state"], reply["output_base64"], reply["error_code"]),
                         ("REFUSED", "", "local_bridge_response_too_large"))

    def test_budget_too_small_even_for_refusal_closes_without_response(self):
        self._start()
        for budget in (1, 0):
            sock = self._connect()
            hello = self._hello(sock)
            send_frame(sock, {"schema_version": 1, "request_id": "tiny", "session_id": hello["session_id"],
                             "operation": "file.read", "target": "binary.bin", "max_response_bytes": budget}, self.limits)
            self.assertEqual(sock.recv(1), b"")
            sock.close()

    def test_binary_and_listing_fit_exact_native_budget_then_one_more_byte_is_refused(self):
        self._start()
        sock = self._connect()
        hello = self._hello(sock)
        envelope = {"schema_version": 1, "request_id": "call-test", "session_id": hello["session_id"],
                    "service_incarnation": hello["service_incarnation"], "operation": "file.read",
                    "state": "SUCCEEDED", "output_base64": "", "error_code": None}
        overhead = len(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("ascii"))
        raw_limit = ((4096 - overhead) // 4) * 3
        (self.repo / "binary.bin").write_bytes(b"b" * raw_limit)
        reply = self._call(sock, hello, operation="file.read", target="binary.bin")
        self.assertEqual(len(base64.b64decode(reply["output_base64"])), raw_limit)
        self.assertLessEqual(len(json.dumps(reply, sort_keys=True, separators=(",", ":")).encode("ascii")), 4096)
        (self.repo / "binary.bin").write_bytes(b"b" * (raw_limit + 1))
        self.assertEqual(self._call(sock, hello, operation="file.read", target="binary.bin")["error_code"],
                         "local_bridge_response_too_large")
        # list's operation name has the same width as read's. Fill the native
        # JSON byte bound using actual immediate names, without child reads.
        (self.repo / "dir" / "a.txt").unlink()
        names = []
        while True:
            used = len(json.dumps({"entries": names}, sort_keys=True, separators=(",", ":")).encode("ascii"))
            remaining = raw_limit - used - (3 if names else 2)
            if remaining <= 255:
                name = "z" * remaining
                names.append(name)
                (self.repo / "dir" / name).write_bytes(b"unread-child")
                break
            name = "n%03d_" % len(names) + "x" * 200
            names.append(name)
            (self.repo / "dir" / name).write_bytes(b"unread-child")
        reply = self._call(sock, hello, operation="file.list", target="dir")
        self.assertEqual(json.loads(base64.b64decode(reply["output_base64"])), {"entries": sorted(names)})
        self.assertEqual(len(base64.b64decode(reply["output_base64"])), raw_limit)
        (self.repo / "dir" / "a").write_bytes(b"unread-child")
        reply = self._call(sock, hello, operation="file.list", target="dir")
        self.assertEqual((reply["state"], reply["output_base64"], reply["error_code"]),
                         ("REFUSED", "", "local_bridge_response_too_large"))

    def _native_rows(self):
        with self.orch.connect() as conn:
            return {table: [tuple(row) for row in conn.execute(
                "SELECT * FROM " + table + " ORDER BY rowid")]
                for table in ("runs", "tasks")}

    def _wait_for_active_read(self, active, entered, completed, results):
        # Preparation is distinct from the competitor's transport timeout.
        deadline = time.monotonic() + self.limits.io_timeout_sec
        while not entered.is_set():
            if select.select([active], [], [], 0)[0]:
                try:
                    reply = receive_frame(active, self.limits)
                except (EOFError, OSError, TimeoutError, ValueError) as exc:
                    self.fail("active read ended before readiness: " + type(exc).__name__)
                self.fail("active read ended before readiness: %s/%s" %
                          (reply.get("state"), reply.get("error_code")))
            if completed.is_set():
                self.fail("service exited before active read: %r" % results)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("active read readiness deadline expired while service was preparing")
            entered.wait(min(remaining, 0.05))

    def _exercise_competing_read(self, *, before_request=None, expected_result=0):
        from orch import bridge_file_read
        entered, release = threading.Event(), threading.Event()
        stopped, completed = threading.Event(), threading.Event()
        original_read, socket_type = bridge_file_read._read_chunk, socket.socket
        before = self._native_rows()
        thread, results, active, competitor = None, [], None, None
        thread_count = len(self.threads)

        class StoppableListener(socket_type):
            # Stop only at the fixture's accept boundary, after active I/O ends.
            def accept(listener):
                if stopped.is_set():
                    raise service._ServiceStopped()
                connection, address = super().accept()
                if stopped.is_set():
                    connection.close()
                    raise service._ServiceStopped()
                return connection, address

        def gated_read(fd, size):
            entered.set()
            release.wait()  # The owner releases this in every exit path below.
            return original_read(fd, size)

        # Serialism does not require the separate short-deadline frame fixture.
        with mock.patch.object(self, "limits", LocalServiceLimits()), \
                mock.patch.object(service.socket, "socket", StoppableListener), \
                mock.patch.object(bridge_file_read, "_read_chunk", gated_read):
            try:
                thread, results = self._start(completed=completed)
                active = self._connect()
                hello = self._hello(active)
                if before_request is not None:
                    before_request()
                send_frame(active, {"schema_version": 1, "request_id": "active", "session_id": hello["session_id"],
                                   "operation": "file.read", "target": "binary.bin", "max_response_bytes": 4096}, self.limits)
                self._wait_for_active_read(active, entered, completed, results)
                competitor = self._connect()
                credential = json.loads(self.credential.read_text())
                send_frame(competitor, {"schema_version": 1, "request_id": "competing", "type": "hello",
                                       "transport_token": credential["transport_token"]}, self.limits)
                competitor.settimeout(0.05)
                with self.assertRaises(socket.timeout):
                    competitor.recv(1)
                competitor.close()
                release.set()
                reply = receive_frame(active, self.limits)
                self.assertEqual((reply["state"], reply["error_code"]), ("SUCCEEDED", None))
                self.assertEqual(base64.b64decode(reply["output_base64"]), b"\x00\xff\x80\n")
            finally:
                release.set()
                for client in (competitor, active):
                    if client is not None:
                        client.close()
                stopped.set()
                if thread is None and len(self.threads) > thread_count:
                    thread = self.threads[-1]
                if thread is not None:
                    if thread.is_alive():
                        # Wake an idle accept; the fixture boundary owns closure.
                        try:
                            with socket_type(socket.AF_UNIX, socket.SOCK_STREAM) as wakeup:
                                wakeup.settimeout(self.limits.io_timeout_sec)
                                wakeup.connect(str(self.endpoint))
                        except OSError:
                            pass  # The listener may already have closed.
                    thread.join(self.limits.io_timeout_sec)
                    self.assertFalse(thread.is_alive(), "foreground service did not exit")
                if results:
                    self.assertEqual(results, [expected_result])
                self.assertFalse(self.endpoint.exists())
                self.assertFalse(self.credential.exists())
                self.assertEqual(self._native_rows(), before)

    def test_competing_connection_times_out_while_active_read_remains_serial(self):
        self._exercise_competing_read()

    def test_competing_read_readiness_tolerates_delayed_authority(self):
        armed, delayed, checked = threading.Event(), threading.Event(), threading.Event()
        original = service.LocalRunSessionAuthority.require_session

        def delayed_authority(authority, *args, **kwargs):
            pause = armed.is_set() and not delayed.is_set()
            if pause:
                delayed.set()
                time.sleep(1.1)  # Fault injection beyond the former one-second wait.
            outcome = original(authority, *args, **kwargs)
            if pause:
                checked.set()
            return outcome

        with mock.patch.object(service.LocalRunSessionAuthority, "require_session", delayed_authority):
            self._exercise_competing_read(before_request=armed.set)
        self.assertTrue(checked.is_set(), "the delayed authentic authority check did not finish")

    def test_competing_read_refusal_fails_readiness_and_releases_fixture(self):
        with mock.patch.object(service, "_dispatch", side_effect=RuntimeError("fixture dispatch failure")):
            with self.assertRaisesRegex(self.failureException,
                                        "active read ended before readiness: REFUSED/local_bridge_authority_denied"):
                self._exercise_competing_read(expected_result=1)

    def test_competing_read_competitor_setup_failure_releases_active_read(self):
        original, connections = self._connect, 0

        def connect():
            nonlocal connections
            connections += 1
            if connections == 2:
                raise OSError("fixture competitor setup failed")
            return original()

        with mock.patch.object(self, "_connect", side_effect=connect):
            with self.assertRaisesRegex(OSError, "fixture competitor setup failed"):
                self._exercise_competing_read()

    def test_branch_and_head_drift_are_live_wire_refusals(self):
        before = self._native_state()
        thread, results = self._start()
        sock = self._connect()
        hello = self._hello(sock)
        self._git("-C", str(self.repo), "switch", "-q", "-c", "drift")
        reply = self._call(sock, hello)
        self.assertEqual(reply["error_code"], "local_bridge_authority_denied")
        thread.join(3)
        self.assertEqual(results, [1])
        self._git("-C", str(self.repo), "switch", "-q", "main")
        thread, results = self._start()
        sock = self._connect()
        hello = self._hello(sock)
        (self.repo / "revision.txt").write_bytes(b"revision")
        self._git("-C", str(self.repo), "add", "revision.txt")
        self._git("-C", str(self.repo), "commit", "-qm", "drift")
        self.assertEqual(self._call(sock, hello)["error_code"], "local_bridge_authority_denied")
        thread.join(3)
        self.assertEqual(results, [1])
        self.assertEqual(self._native_state(), before)

    def test_unrelated_project_rows_and_files_remain_unchanged(self):
        other = self.base / "other"
        self._git("init", "-q", "-b", "main", str(other))
        self._git("-C", str(other), "config", "user.email", "fixture@example.invalid")
        self._git("-C", str(other), "config", "user.name", "Fixture")
        (other / "result.txt").write_bytes(b"unrelated")
        self._git("-C", str(other), "add", ".")
        self._git("-C", str(other), "commit", "-qm", "fixture")
        project = ProjectRegistry(self.orch.root).add(other, name="Unrelated IPC fixture", profile="safe",
                   review_mode="off", allow_commit=True, allow_push=False)["project"]
        plan = build_single_task_plan(project, task_id="IPC-UNRELATED", goal="Preserve unrelated fixture",
                                      allowed_paths=["result.txt"], plan_revision="ipc-unrelated")
        plan_file = self.base / "other-plan.json"
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        self.orch.claim("fixture-other", project_id=project["project_id"])
        with self.orch.connect() as conn:
            runs = [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY run_id")]
            tasks = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY task_id")]
        self._start()
        client = LocalBridgeClient(self.credential)
        self.assertEqual(client.call("file.read", "binary.bin")["state"], "SUCCEEDED")
        client.close()
        self._join_all()
        with self.orch.connect() as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY run_id")], runs)
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY task_id")], tasks)
        self.assertEqual((other / "result.txt").read_bytes(), b"unrelated")

    def _stop_child_during(self, stage):
        before = self._native_state()
        program = r'''
import json, sys, threading
from pathlib import Path
from orch import bridge_local_service as service, bridge_file_read
from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
from orch.worker_operations import WorkerRunBinding
stage = sys.argv[1]
if stage == "startup":
    write = service.os.write
    def blocked_write(fd, data):
        written = write(fd, data)
        print("WRITE_READY", flush=True)
        threading.Event().wait(10)
        return written
    service.os.write = blocked_write
publish = service._publish_credential
def ready(*args, **kwargs):
    result = publish(*args, **kwargs)
    print("SERVICE_READY", flush=True)
    return result
read = bridge_file_read._read_chunk
def blocked(fd, size):
    data = read(fd, size)
    print("READ_READY", flush=True)
    threading.Event().wait(10)
    return data
service._publish_credential = ready
bridge_file_read._read_chunk = blocked
root, binding, cap, endpoint, credential = sys.argv[2:]
raise SystemExit(service.serve_local_bridge(ReadOnlyAuthorityLedger(Path(root)),
    WorkerRunBinding(**json.loads(binding)), Path(cap), Path(endpoint), Path(credential),
    service.LocalServiceLimits()))
'''
        import sys
        from dataclasses import asdict
        child = subprocess.Popen([sys.executable, "-c", program, stage, str(self.orch.root), json.dumps(asdict(self.binding)),
                                  str(self.cap), str(self.endpoint), str(self.credential)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0], "child service readiness missing")
            self.assertEqual(child.stdout.readline().strip(), "WRITE_READY" if stage == "startup" else "SERVICE_READY")
            sock = None
            if stage == "read":
                sock = self._connect()
                hello = self._hello(sock)
                send_frame(sock, {"schema_version": 1, "request_id": "read-stop", "session_id": hello["session_id"],
                                 "operation": "file.read", "target": "binary.bin", "max_response_bytes": 4096}, self.limits)
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "child active read readiness missing")
                self.assertEqual(child.stdout.readline().strip(), "READ_READY")
            child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual((child.returncode, stdout, stderr), (0, "", ""))
            if sock is not None:
                self.assertEqual(sock.recv(1), b"")
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
            child.stdout.close()
            child.stderr.close()
        self.assertFalse(self.endpoint.exists())
        self.assertFalse(self.credential.exists())
        self.assertEqual(self._native_state(), before)

    def test_sigterm_during_active_read_unwinds_and_preserves_native_writer(self):
        self._stop_child_during("read")

    def test_sigterm_during_credential_creation_cleans_owned_files(self):
        self._stop_child_during("startup")

    def _stop_child_at_startup_boundary(self, boundary, signum, *, replace=False, delivery="raise_signal"):
        # Each subTest must start fresh even when a preceding boundary leaked
        # a path; the assertions below still observe that leak before removal.
        for path in (self.endpoint, self.credential):
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".owned").unlink(missing_ok=True)
        before = self._native_state()
        program = r'''
import errno, json, signal, sys, threading
from pathlib import Path
from orch import bridge_local_service as service
from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
from orch.worker_operations import WorkerRunBinding
boundary, signum, replace, delivery, root, binding, cap, endpoint, credential = sys.argv[1:]
signum, replace = int(signum), replace == "1"
endpoint, credential = Path(endpoint), Path(credential)
triggered, credential_fd = False, None
replacements = {}
send_signal, signal_sent = threading.Event(), threading.Event()
original_open, original_fstat, original_close = service.os.open, service.os.fstat, service.os.close
original_identity, original_publish = service._identity, service._publish_credential
original_socket = service.socket.socket

def stop_at_boundary():
    global triggered
    if triggered:
        return
    triggered = True
    if replace:
        for path in (endpoint, credential):
            if path.exists():
                path.rename(path.with_name(path.name + ".owned"))
                path.write_bytes(b"replacement")
                path.chmod(0o600)
                info = path.lstat()
                replacements[path] = (info.st_dev, info.st_ino)
    if delivery == "thread":
        send_signal.set()
        assert signal_sent.wait(1), "signal sender did not finish"
    else:
        signal.raise_signal(signum)

def open_at_boundary(name, flags, *args, **kwargs):
    global credential_fd
    fd = original_open(name, flags, *args, **kwargs)
    if name == credential.name and flags & service.os.O_EXCL:
        credential_fd = fd
        if boundary == "credential_open":
            stop_at_boundary()
    return fd

def fstat_at_boundary(fd):
    info = original_fstat(fd)
    if boundary == "credential_identity" and fd == credential_fd:
        stop_at_boundary()
    return info

def close_at_boundary(fd):
    original_close(fd)
    if boundary == "credential_close" and fd == credential_fd:
        stop_at_boundary()

def identity_at_boundary(info):
    identity = original_identity(info)
    if boundary == "socket_identity" and service.stat.S_ISSOCK(info.st_mode):
        stop_at_boundary()
    return identity

def publish_at_boundary(*args, **kwargs):
    identity = original_publish(*args, **kwargs)
    if boundary == "credential_publish":
        stop_at_boundary()
    return identity

class BoundarySocket(original_socket):
    def bind(self, address):
        result = super().bind(address)
        # Scope primitive checks have completed; only credential acquisition
        # now needs this wrapper around the real O_EXCL open.
        service.os.open = open_at_boundary
        if boundary == "socket_bind":
            stop_at_boundary()
        return result

    def accept(self):
        raise AssertionError("listener continued after startup stop")

def trace_success(frame, event, arg):
    if (boundary == "credential_success" and frame.f_code is original_publish.__code__
            and event == "line" and frame.f_locals.get("succeeded") is True):
        stop_at_boundary()
    return trace_success

service.socket.socket = BoundarySocket
service.os.fstat, service.os.close = fstat_at_boundary, close_at_boundary
service._identity, service._publish_credential = identity_at_boundary, publish_at_boundary
sys.settrace(trace_success)
def previous_handler(_signum, _frame):
    pass
for stop_signal in (signal.SIGINT, signal.SIGTERM):
    signal.signal(stop_signal, previous_handler)
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
sender = None
if delivery == "thread":
    def signal_from_existing_thread():
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
        if send_signal.wait(5):
            service.os.kill(service.os.getpid(), signum)
            signal_sent.set()
    sender = threading.Thread(target=signal_from_existing_thread, daemon=True)
    sender.start()
result = service.serve_local_bridge(ReadOnlyAuthorityLedger(Path(root)),
    WorkerRunBinding(**json.loads(binding)), Path(cap), endpoint, credential,
    service.LocalServiceLimits(io_timeout_sec=0.2, idle_timeout_sec=0.25))
sys.settrace(None)
assert triggered, "startup boundary was not reached"
assert all(signal.getsignal(stop_signal) is previous_handler
           for stop_signal in (signal.SIGINT, signal.SIGTERM)), "signal handlers were not restored"
assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == previous_mask, "signal mask was not restored"
if sender is not None:
    sender.join(1)
    assert not sender.is_alive(), "signal sender remained active"
for path, identity in replacements.items():
    info = path.lstat()
    assert (info.st_dev, info.st_ino) == identity, "replacement identity changed"
    assert path.read_bytes() == b"replacement", "replacement contents changed"
if credential_fd is not None:
    try:
        original_fstat(credential_fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF, "unexpected credential fd state"
    else:
        raise AssertionError("credential fd remained open after startup stop")
raise SystemExit(result)
'''
        import sys
        from dataclasses import asdict
        child = subprocess.Popen([
            sys.executable, "-c", program, boundary, str(int(signum)), "1" if replace else "0", delivery,
            str(self.orch.root), json.dumps(asdict(self.binding)), str(self.cap),
            str(self.endpoint), str(self.credential),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual((child.returncode, stdout, stderr), (0, "", ""))
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
            child.stdout.close()
            child.stderr.close()
        for path in (self.endpoint, self.credential):
            moved = path.with_name(path.name + ".owned")
            if replace and moved.exists():
                info = path.lstat()
                self.assertEqual((path.read_bytes(), info.st_mode & 0o777), (b"replacement", 0o600))
                identity = (info.st_dev, info.st_ino)
                self.assertNotEqual(identity, (moved.lstat().st_dev, moved.lstat().st_ino))
                path.unlink()
                moved.unlink()
            else:
                self.assertFalse(path.exists(), "owned startup path survived stop")
        self.assertTrue(self._native_state() == before, "native writer changed during startup stop")

    def test_startup_stop_after_socket_bind_cleans_owned_files(self):
        for boundary in ("socket_bind", "socket_identity"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                for delivery in ("raise_signal", "thread"):
                    with self.subTest(boundary=boundary, signal=signum, delivery=delivery):
                        self._stop_child_at_startup_boundary(boundary, signum, delivery=delivery)

    def test_startup_stop_after_credential_publish_cleans_owned_files(self):
        for boundary in ("credential_success", "credential_close", "credential_publish"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                for delivery in ("raise_signal", "thread"):
                    with self.subTest(boundary=boundary, signal=signum, delivery=delivery):
                        self._stop_child_at_startup_boundary(boundary, signum, delivery=delivery)

    def test_startup_stop_after_credential_open_cleans_owned_files(self):
        for boundary in ("credential_open", "credential_identity"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                for delivery in ("raise_signal", "thread"):
                    with self.subTest(boundary=boundary, signal=signum, delivery=delivery):
                        self._stop_child_at_startup_boundary(boundary, signum, delivery=delivery)

    def test_startup_stop_preserves_replaced_files(self):
        for boundary in ("socket_bind", "socket_identity", "credential_open", "credential_identity",
                         "credential_success", "credential_close", "credential_publish"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                for delivery in ("raise_signal", "thread"):
                    with self.subTest(boundary=boundary, signal=signum, delivery=delivery):
                        self._stop_child_at_startup_boundary(boundary, signum, replace=True, delivery=delivery)

    def test_live_scope_denies_ungranted_and_symlink_targets(self):
        (self.repo / "link.bin").symlink_to(self.repo / "private.txt")
        self._start()
        client = LocalBridgeClient(self.credential)
        self.addCleanup(client.close)
        for target in ("private.txt", "link.bin"):
            reply = client.call("file.read", target)
            self.assertEqual((reply["state"], reply["output_base64"]), ("REFUSED", ""))
            self.assertNotIn("private-fixture-content", json.dumps(reply))

    def test_stalled_or_truncated_connection_does_not_end_listener(self):
        self._start()
        stalled = self._connect()
        self.assertEqual(stalled.recv(1), b"")
        truncated = self._connect()
        truncated.sendall(struct.pack("!I", 40) + b"{")
        truncated.shutdown(socket.SHUT_WR)
        self.assertEqual(receive_frame(truncated, self.limits)["state"], "REFUSED")
        client = LocalBridgeClient(self.credential)
        self.addCleanup(client.close)
        self.assertEqual(client.call("session.status")["state"], "SUCCEEDED")

    def test_duplicate_unknown_keys_and_oversize_header_are_refused(self):
        self._start()
        for payload in (b'{"schema_version":1,"schema_version":1}', b'{"root":"/tmp"}'):
            sock = self._connect()
            sock.sendall(struct.pack("!I", len(payload)) + payload)
            self.assertEqual(receive_frame(sock, self.limits)["state"], "REFUSED")
            sock.close()
        sock = self._connect()
        sock.sendall(struct.pack("!I", 65537))
        self.assertEqual(receive_frame(sock, self.limits)["state"], "REFUSED")

    def test_authority_revocation_ends_service_without_writer_lifecycle(self):
        before = self._native_state()
        thread, results = self._start()
        sock = self._connect()
        hello = self._hello(sock)
        self.cap.chmod(0o644)
        reply = self._call(sock, hello)
        self.assertEqual((reply["state"], reply["error_code"]), ("REFUSED", "local_bridge_authority_denied"))
        thread.join(3)
        self.assertEqual(results, [1])
        self.assertEqual(self._native_state(), before)

    def test_final_authority_check_prevents_buffered_content_release(self):
        original = service.LocalBoundedFileReader.read

        def revoke_after_read(reader, request):
            outcome = original(reader, request)
            self.cap.chmod(0o644)
            return outcome

        with mock.patch.object(service.LocalBoundedFileReader, "read", revoke_after_read):
            self._start()
            client = LocalBridgeClient(self.credential)
            self.addCleanup(client.close)
            reply = client.call("file.read", "binary.bin")
        self.assertEqual((reply["state"], reply["output_base64"]), ("REFUSED", ""))

    def test_existing_and_symlink_paths_are_preserved_without_unlink(self):
        for path in (self.endpoint, self.credential):
            with self.subTest(path=path.name):
                path.write_bytes(b"foreign")
                identity = path.stat().st_ino
                self.assertEqual(service.serve_local_bridge(self.ledger, self.binding, self.cap,
                                 self.endpoint, self.credential, self.limits), 1)
                self.assertEqual((path.read_bytes(), path.stat().st_ino), (b"foreign", identity))
                path.unlink()
        external = self.base / "external"
        external.write_bytes(b"external")
        self.endpoint.symlink_to(external)
        self.assertEqual(service.serve_local_bridge(self.ledger, self.binding, self.cap,
                         self.endpoint, self.credential, self.limits), 1)
        self.assertTrue(self.endpoint.is_symlink())
        self.assertEqual(external.read_bytes(), b"external")

    def test_replaced_owned_paths_survive_cleanup(self):
        thread, results = self._start()
        for path in (self.endpoint, self.credential):
            path.rename(path.with_name(path.name + "-owned"))
            path.write_bytes(b"replacement")
        thread.join(3)
        self.assertEqual(results, [0])
        self.assertEqual(self.endpoint.read_bytes(), b"replacement")
        self.assertEqual(self.credential.read_bytes(), b"replacement")

    def test_explicit_restart_changes_session_incarnation_and_token(self):
        first, results = self._start()
        old = json.loads(self.credential.read_text())
        client = LocalBridgeClient(self.credential)
        old_reply = client.call("session.status")
        client.close()
        first.join(3)
        self.assertEqual(results, [0])
        self._start()
        fresh = json.loads(self.credential.read_text())
        self.assertNotEqual(old["transport_token"], fresh["transport_token"])
        self.assertNotEqual(old["service_incarnation"], fresh["service_incarnation"])
        stale = self._connect()
        self.assertEqual(self._hello(stale, transport_token=old["transport_token"])["error_code"],
                         "local_bridge_auth_denied")
        stale.close()
        sock = self._connect()
        hello = self._hello(sock)
        self.assertNotEqual(old_reply["session_id"], hello["session_id"])
        reply = self._call(sock, hello, session_id=old_reply["session_id"])
        self.assertEqual(reply["state"], "REFUSED")

    def test_client_rejects_unsafe_credential_without_mode_repair(self):
        self._start()
        self.credential.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "local_bridge_credential_denied"):
            LocalBridgeClient(self.credential).call("session.status")
        self.assertEqual(self.credential.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
