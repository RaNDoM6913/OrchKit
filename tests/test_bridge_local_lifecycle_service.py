"""Real private sockets for the opt-in lifecycle facade."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from orch.core import Orchestrator, canonical_json
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.bridge_local_protocol import LocalServiceLimits, encode_payload, receive_frame, send_frame
from orch.bridge_local_lifecycle_client import LifecycleClient
from orch.bridge_worker_lifecycle import LifecycleAdmission, LifecycleAuthority, LifecycleGrantSpec


def child_main():
    from orch.bridge_local_lifecycle_service import serve_local_lifecycle
    config = json.load(sys.stdin)
    modules = ('tests.test_bridge_local_lifecycle_service', 'orch', 'orch.core',
               'orch.bridge_worker_lifecycle', 'orch.bridge_local_lifecycle_protocol',
               'orch.bridge_local_lifecycle_service', 'orch.bridge_local_lifecycle_client',
               'orch.bridge_local_concurrent_service', 'orch.bridge_local_protocol',
               'orch.bridge_local_service', 'orch.bridge_authorization')
    origins = {}
    for name in modules:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        origins[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    Path(config['receipt_dir'], 'startup-%d.json' % os.getpid()).write_text(
        json.dumps({'pid': os.getpid(), 'modules': origins}, sort_keys=True))
    if config.get('fault') == 'PENDING':
        import orch.bridge_worker_lifecycle as lifecycle
        def fail_capability(*args):
            raise OSError('injected')
        lifecycle._publish_capability = fail_capability
    if config.get('fault') in ('DENY_AFTER_CHECKPOINT', 'PARTIAL_CONTEXT'):
        import orch.bridge_local_concurrent_service as concurrent
        from orch.bridge_worker_lifecycle import LifecycleRoute
        from orch.bridge_local_lifecycle_service import _LifecycleTransport
        old_status, old_queue, old_write = LifecycleRoute.status, _LifecycleTransport.queue, _LifecycleTransport.write
        def status(self):
            if Path(config['barrier']).exists():
                raise ValueError('injected_denial')
            return old_status(self)
        def queue(self, peer, value, **kwargs):
            old_queue(self, peer, value, **kwargs)
            if config['fault'] == 'PARTIAL_CONTEXT' and value.get('operation') == 'context':
                concurrent._IO_CHUNK = 1
            if config['fault'] == 'DENY_AFTER_CHECKPOINT' and value.get('operation') == 'checkpoint':
                Path(config['barrier']).write_text('denied')
        def write(self, peer):
            if config['fault'] == 'PARTIAL_CONTEXT' and peer.request and peer.request.get('operation') == 'context' and peer.sent:
                Path(config['barrier']).write_text('denied')
            old_write(self, peer)
        LifecycleRoute.status = status
        _LifecycleTransport.queue, _LifecycleTransport.write = queue, write
    if config.get('fault') == 'DROP_CHECKPOINT':
        from orch.bridge_local_lifecycle_service import _LifecycleTransport
        original_queue = _LifecycleTransport.queue
        def drop_once(self, peer, value, **kwargs):
            if value.get('operation') == 'checkpoint' and not Path(config['barrier']).exists():
                Path(config['barrier']).write_text('dropped')
                self.close_peer(peer)
                return
            return original_queue(self, peer, value, **kwargs)
        _LifecycleTransport.queue = drop_once
    admissions = tuple(LifecycleAdmission(item['grant_id'], Path(item['credential_file']))
                       for item in config['admissions'])
    raise SystemExit(serve_local_lifecycle(LifecycleAuthority(Path(config['home'])), admissions,
                      Path(config['socket']), LocalServiceLimits(idle_timeout_sec=30,
                          min_response_bytes=config.get('min_response_bytes', 4096))))


class LifecycleSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='orch-b10-wire-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.children, self.all_children, self.startup_receipts = [], [], {}
        self.addCleanup(self._stop_children)
        self.repo = self.base / 'repo'
        self._git('init', '-q', '-b', 'main', str(self.repo))
        self._git('-C', str(self.repo), 'config', 'user.name', 'Fixture')
        self._git('-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'file.txt').write_text('fixture\n')
        self._git('-C', str(self.repo), 'add', '.')
        self._git('-C', str(self.repo), 'commit', '-qm', 'fixture')
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.orch = Orchestrator(self.home)
        self.receipt_dir = self.base / 'fixture-observations'
        self.receipt_dir.mkdir(mode=0o700)
        self.project = ProjectRegistry(self.home).add(self.repo, name='B10 wire', profile='safe',
                                  review_mode='off', allow_commit=True, allow_push=False)['project']
        for task in ('FIRST', 'SECOND'):
            plan = build_single_task_plan(self.project, task_id=task, goal='Socket fixture ' + 'é' * 1500,
                                          allowed_paths=['file.txt'], plan_revision='v-' + task)
            path = self.base / (task + '.json')
            write_plan(path, plan)
            self.orch.load_plan(path)
        self.authority = LifecycleAuthority(self.home)
        self.grant = self.authority.create_grant(self._spec('SECOND'))
        self.ipc = self.base / 'ipc'
        self.ipc.mkdir(mode=0o700)
        self.endpoint, self.credential = self.ipc / 's', self.ipc / 'c'
        self.limits = LocalServiceLimits()

    def _spec(self, task):
        with self.orch.connect() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone()
        return LifecycleGrantSpec(row['project_id'], row['writer_key'], task, row['plan_revision'],
             hashlib.sha256(canonical_json(json.loads(row['payload_json'])).encode()).hexdigest(), 1, 'worker')

    def _git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=12)

    def _start(self, extra=(), fault=None, min_response_bytes=4096):
        config = {'home': str(self.home), 'socket': str(self.endpoint), 'receipt_dir': str(self.receipt_dir),
                  'admissions': [{'grant_id': self.grant, 'credential_file': str(self.credential)}, *extra],
                  'fault': fault, 'barrier': str(self.base / 'barrier'),
                  'min_response_bytes': min_response_bytes}
        import orch
        source_root = str(Path(orch.__file__).resolve().parent.parent)
        test_root = str(Path(__file__).resolve().parent.parent)
        bootstrap = ('import os, sys; sys.path[:] = [p for p in sys.path if p and os.path.abspath(p) != os.getcwd()]; '
                     'sys.path[:0] = %r; from tests.test_bridge_local_lifecycle_service import child_main; child_main()'
                     % [source_root, test_root])
        child = subprocess.Popen([sys.executable, '-c', bootstrap], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.all_children.append(child)
        child.stdin.write(json.dumps(config))
        child.stdin.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            receipt = self.receipt_dir / ('startup-%d.json' % child.pid)
            if self.endpoint.exists() and self.credential.exists() and receipt.exists():
                self.startup_receipts[child.pid] = json.loads(receipt.read_text())
                self.assertEqual(self.startup_receipts[child.pid]['pid'], child.pid)
                return child
            if child.poll() is not None:
                self.fail('service exited: ' + child.stderr.read())
            time.sleep(.02)
        self.fail('service did not publish credentials')

    def _stop(self, child, expected=0, remaining=(), autonomous=False):
        forced = False
        if child.poll() is None:
            if not autonomous:
                child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forced = True
                child.kill()
                child.wait(timeout=5)
        output = child.stdout.read()
        error = child.stderr.read()
        child.stdout.close()
        child.stderr.close()
        self.children.remove(child)
        self.assertFalse(forced, 'service required forced termination')
        self.assertEqual(child.returncode, expected, (output, error))
        self.assertFalse(self.endpoint.exists())
        self.assertEqual({p.name for p in self.ipc.iterdir()}, set(remaining))
        self.assertEqual(output, '')
        self.assertEqual(error, '')

    def _stop_children(self):
        for child in list(self.children):
            self._stop(child)

    def _socket(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(str(self.endpoint))
        self.addCleanup(sock.close)
        return sock

    def _hello(self, sock, **overrides):
        credential = json.loads(self.credential.read_text())
        hello = dict(schema_version=4, request_id='hello', type='hello',
                     transport_token=credential['transport_token'])
        hello.update(overrides)
        send_frame(sock, hello, self.limits)
        return receive_frame(sock, self.limits)

    def _request(self, sock, hello, **overrides):
        request = dict(schema_version=4, request_id='request', session_id=hello['session_id'],
                       operation='disposition', operation_id='missing', max_response_bytes=4096)
        request.update(overrides)
        send_frame(sock, request, self.limits)
        return receive_frame(sock, self.limits)

    def test_oversized_claim_response_is_unknown_then_disposition_proves_one_effect(self):
        child = self._start(min_response_bytes=1)
        client = LifecycleClient(self.credential)
        client._limits = LocalServiceLimits(min_response_bytes=1)
        self.addCleanup(client.close)
        self.assertEqual(client.claim('bounded', max_response_bytes=300).state, 'UNKNOWN')
        recovered = LifecycleClient(self.credential)
        self.addCleanup(recovered.close)
        disposition = recovered.disposition('bounded')
        self.assertEqual(disposition.state, 'SUCCEEDED')
        self.assertEqual(recovered.claim('bounded'), disposition)
        with self.orch.connect() as conn:
            runs = conn.execute('SELECT run_id,task_id,attempt,state FROM runs').fetchall()
            events = conn.execute("SELECT COUNT(*) FROM events WHERE kind='RUN_CLAIMED'").fetchone()[0]
            task = conn.execute("SELECT status FROM tasks WHERE task_id='SECOND'").fetchone()[0]
        self.assertEqual([tuple(r) for r in runs], [(disposition.run_id, 'SECOND', 1, 'RUNNING')])
        self.assertEqual(events, 1)
        self.assertEqual(task, 'IN_PROGRESS')
        self.assertTrue(self.orch._capability_path(disposition.run_id).is_file())
        self._stop(child)

    def test_oversized_checkpoint_response_is_unknown_then_disposition_proves_one_effect(self):
        child = self._start(min_response_bytes=1)
        client = LifecycleClient(self.credential)
        client._limits = LocalServiceLimits(min_response_bytes=1)
        self.addCleanup(client.close)
        claim = client.claim('claim')
        self.assertEqual(claim.state, 'SUCCEEDED')
        self.assertEqual(client.checkpoint('bounded', 'working', 'active', max_response_bytes=300).state, 'UNKNOWN')
        recovered = LifecycleClient(self.credential)
        self.addCleanup(recovered.close)
        disposition = recovered.disposition('bounded')
        self.assertEqual(disposition.state, 'SUCCEEDED')
        self.assertEqual(recovered.checkpoint('bounded', 'working', 'active'), disposition)
        with self.orch.connect() as conn:
            events = conn.execute("SELECT COUNT(*) FROM events WHERE kind='WORKER_CHECKPOINT_RECORDED'").fetchone()[0]
            runs = conn.execute('SELECT run_id,state FROM runs').fetchall()
        self.assertEqual(events, 1)
        self.assertEqual([tuple(r) for r in runs], [(claim.run_id, 'RUNNING')])
        self._stop(child)

    def test_claim_context_checkpoint_and_disposition_use_exact_task_and_keep_writer(self):
        child = self._start()
        client = LifecycleClient(self.credential)
        self.addCleanup(client.close)
        claim = client.claim('claim-1')
        self.assertEqual(claim.state, 'SUCCEEDED')
        context = client.context()
        self.assertIsInstance(context, dict)
        checkpoint = client.checkpoint('check-1', 'still working', 'active')
        self.assertEqual(checkpoint.state, 'SUCCEEDED')
        self.assertEqual(client.checkpoint('check-1', 'still working', 'active'), checkpoint)
        self.assertEqual(client.disposition('check-1'), checkpoint)
        self.assertEqual(client.disposition('missing').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            run = conn.execute('SELECT run_id,task_id,worker_id,attempt,state,lease_token FROM runs').fetchone()
            task = conn.execute('SELECT status FROM tasks WHERE task_id=?', ('SECOND',)).fetchone()
            events = conn.execute("SELECT count(*) FROM events WHERE kind='WORKER_CHECKPOINT_RECORDED'").fetchone()[0]
            first = conn.execute('SELECT status FROM tasks WHERE task_id=?', ('FIRST',)).fetchone()
        self.assertEqual((run['task_id'], run['worker_id'], run['attempt'], run['state']), ('SECOND', 'worker', 1, 'RUNNING'))
        self.assertEqual(task['status'], 'IN_PROGRESS')
        self.assertNotEqual(first['status'], 'IN_PROGRESS')
        self.assertEqual(events, 1)
        published = repr((claim, context, checkpoint))
        self.assertNotIn(run['lease_token'], published)
        self.assertNotIn(str(self.orch._capability_path(run['run_id'])), published)
        self.assertNotIn(json.loads(self.credential.read_text())['transport_token'], published)
        client.close()
        self._stop(child)

    def test_wrong_token_session_and_extra_target_do_not_affect_admitted_route(self):
        self._start()
        sock = self._socket()
        self.assertEqual(self._hello(sock, transport_token='x' * 32)['state'], 'REFUSED')
        sock.close()
        sock = self._socket()
        hello = self._hello(sock)
        self.assertEqual(self._request(sock, hello, session_id='x' * 32)['state'], 'REFUSED')
        sock.close()
        sock = self._socket()
        hello = self._hello(sock)
        self.assertEqual(self._request(sock, hello, target='FIRST')['state'], 'REFUSED')
        sock.close()
        client = LifecycleClient(self.credential)
        self.addCleanup(client.close)
        self.assertEqual(client.claim('own-claim').state, 'SUCCEEDED')

    def test_invalid_frames_and_versions_are_rejected_without_retiring_route(self):
        self._start()
        for payload in (b'{"schema_version":4,"schema_version":4}', b'\xff', b'[]'):
            with self.subTest(payload=payload):
                sock = self._socket()
                sock.sendall(struct.pack('!I', len(payload)) + payload)
                self.assertEqual(receive_frame(sock, self.limits)['state'], 'REFUSED')
                sock.close()
        sock = self._socket()
        self.assertEqual(self._hello(sock, schema_version=3)['state'], 'REFUSED')
        sock.close()
        client = LifecycleClient(self.credential)
        self.addCleanup(client.close)
        self.assertEqual(client.claim('valid').state, 'SUCCEEDED')

    def test_context_budget_refuses_complete_encoded_envelope_without_truncation(self):
        self._start()
        client = LifecycleClient(self.credential)
        self.addCleanup(client.close)
        self.assertEqual(client.claim('claim').state, 'SUCCEEDED')
        with self.assertRaises(ValueError):
            client.context(max_response_bytes=1)
        self.assertIsInstance(client.context(), dict)

    def test_exact_encoded_budget_counts_unicode_and_never_returns_partial_pack(self):
        self._start()
        client = LifecycleClient(self.credential)
        self.assertEqual(client.claim('claim').state, 'SUCCEEDED')
        client.close()
        sock = self._socket()
        hello = self._hello(sock)
        request = dict(schema_version=4, request_id='budget', session_id=hello['session_id'],
                       operation='context', max_response_bytes=65536)
        send_frame(sock, request, self.limits)
        full = receive_frame(sock, self.limits)
        self.assertEqual(full['state'], 'SUCCEEDED')
        encoded_size = len(encode_payload(full))
        self.assertGreater(encoded_size, 4096)
        request['max_response_bytes'] = encoded_size
        send_frame(sock, request, self.limits)
        exact = receive_frame(sock, self.limits)
        self.assertEqual(exact, full)
        request['max_response_bytes'] = encoded_size - 1
        send_frame(sock, request, self.limits)
        denied = receive_frame(sock, self.limits)
        self.assertEqual((denied['state'], denied['result'], denied['error_code']),
                         ('REFUSED', None, 'lifecycle_response_too_large'))

    def test_lost_checkpoint_reply_disposition_reconnects_without_replay(self):
        self._start(fault='DROP_CHECKPOINT')
        client = LifecycleClient(self.credential)
        self.assertEqual(client.claim('claim').state, 'SUCCEEDED')
        self.assertEqual(client.checkpoint('checkpoint', 'working', 'unknown').state, 'UNKNOWN')
        client.close()
        second = LifecycleClient(self.credential)
        self.addCleanup(second.close)
        result = second.disposition('checkpoint')
        self.assertEqual(result.state, 'SUCCEEDED')
        with self.orch.connect() as conn:
            count = conn.execute("SELECT count(*) FROM events WHERE kind='WORKER_CHECKPOINT_RECORDED'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(second.checkpoint('checkpoint', 'changed', 'unknown').state, 'REFUSED')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE kind='WORKER_CHECKPOINT_RECORDED'").fetchone()[0], 1)

    def test_pending_claim_restart_allows_disposition_but_no_automatic_replay(self):
        child = self._start(fault='PENDING')
        client = LifecycleClient(self.credential)
        self.assertEqual(client.claim('claim').state, 'UNKNOWN')
        self.assertEqual(client.disposition('claim').state, 'UNKNOWN')
        self.assertNotEqual(client.checkpoint('checkpoint', 'working', 'active').state, 'SUCCEEDED')
        client.close()
        self._stop(child)
        self._start()
        client = LifecycleClient(self.credential)
        self.addCleanup(client.close)
        self.assertEqual(client.disposition('claim').state, 'UNKNOWN')
        self.assertNotEqual(client.checkpoint('checkpoint', 'working', 'active').state, 'SUCCEEDED')
        with self.assertRaises(ValueError):
            client.context()
        with self.orch.connect() as conn:
            runs = conn.execute('SELECT task_id,state FROM runs').fetchall()
            self.assertEqual([(row['task_id'], row['state']) for row in runs], [('SECOND', 'RUNNING')])
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE kind='WORKER_CHECKPOINT_RECORDED'").fetchone()[0], 0)

    def test_reconnect_and_restart_need_fresh_credential_and_restore_only_admitted_grant(self):
        child = self._start()
        first = LifecycleClient(self.credential)
        result = first.claim('claim')
        self.assertEqual(result.state, 'SUCCEEDED')
        first.close()
        second = LifecycleClient(self.credential)
        self.assertEqual(second.disposition('claim'), result)
        second.close()
        stale = LifecycleClient(self.credential)
        self._stop(child)
        self._start()
        self.assertEqual(stale.disposition('claim').state, 'UNKNOWN')
        fresh = LifecycleClient(self.credential)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.disposition('claim'), result)
        self.assertEqual(fresh.claim('other').state, 'REFUSED')

    def test_schema_bounds_and_cross_grant_disposition_are_isolated(self):
        other_grant = self.authority.create_grant(self._spec('FIRST'))
        other_credential = self.ipc / 'other'
        self._start(extra=({'grant_id': other_grant, 'credential_file': str(other_credential)},))
        first = LifecycleClient(self.credential)
        second = LifecycleClient(other_credential)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertEqual(first.claim('claim').state, 'SUCCEEDED')
        self.assertEqual(second.disposition('claim').state, 'UNKNOWN')
        for operation_id, reason, state in (('', 'ok', 'active'), ('id', '', 'active'),
                                             ('id', 'x' * 501, 'active'), ('id', 'ok', 'idle')):
            with self.subTest(operation_id=operation_id, state=state), self.assertRaises(ValueError):
                first.checkpoint(operation_id, reason, state)
        with self.assertRaises(ValueError):
            first.claim('x' * 129)
        self.assertEqual(first.checkpoint('valid', 'ok', 'unknown').state, 'SUCCEEDED')

    def test_post_checkpoint_authority_loss_is_unknown_then_disposition_proves_commit(self):
        child = self._start(fault='DENY_AFTER_CHECKPOINT')
        client = LifecycleClient(self.credential)
        self.assertEqual(client.claim('claim').state, 'SUCCEEDED')
        self.assertEqual(client.checkpoint('checkpoint', 'working', 'active').state, 'UNKNOWN')
        client.close()
        self._stop(child, expected=1, autonomous=True)
        (self.base / 'barrier').unlink()
        self._start()
        client = LifecycleClient(self.credential)
        self.addCleanup(client.close)
        self.assertEqual(client.disposition('checkpoint').state, 'SUCCEEDED')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE kind='WORKER_CHECKPOINT_RECORDED'").fetchone()[0], 1)

    def test_partial_context_write_after_authority_loss_never_delivers_success(self):
        child = self._start(fault='PARTIAL_CONTEXT')
        client = LifecycleClient(self.credential)
        self.assertEqual(client.claim('claim').state, 'SUCCEEDED')
        with self.assertRaisesRegex(ValueError, 'lifecycle_call_unknown'):
            client.context()
        client.close()
        self._stop(child, expected=1, autonomous=True)
        self.assertTrue((self.base / 'barrier').exists())

    def test_revoked_capability_denies_next_request_without_success(self):
        child = self._start()
        client = LifecycleClient(self.credential)
        result = client.claim('claim')
        self.assertEqual(result.state, 'SUCCEEDED')
        self.orch._capability_path(result.run_id).unlink()
        denied = client.disposition('claim')
        self.assertNotEqual(denied.state, 'SUCCEEDED')
        client.close()
        self._stop(child, expected=1, autonomous=True)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT state FROM runs WHERE run_id=?', (result.run_id,)).fetchone()['state'], 'RUNNING')

    def test_oversized_frame_and_invalid_operation_inputs_close_without_effects(self):
        self._start()
        for size in (0, 65537, 2**32 - 1):
            with self.subTest(size=size):
                sock = self._socket()
                sock.sendall(struct.pack('!I', size))
                self.assertEqual(receive_frame(sock, self.limits)['state'], 'REFUSED')
                sock.close()
        for changes in (
            {'operation': 'submit'},
            {'schema_version': True},
            {'operation_id': 'x' * 129},
            {'operation': 'checkpoint', 'reason': 'x' * 501, 'process_state': 'active'},
            {'operation': 'checkpoint', 'reason': 'working', 'process_state': 'stopped'},
            {'max_response_bytes': 65537},
        ):
            with self.subTest(changes=changes):
                sock = self._socket()
                hello = self._hello(sock)
                self.assertEqual(self._request(sock, hello, **changes)['state'], 'REFUSED')
                sock.close()
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        client = LifecycleClient(self.credential)
        self.addCleanup(client.close)
        self.assertEqual(client.claim('valid').state, 'SUCCEEDED')

    def test_cleanup_preserves_a_credential_replacement_it_does_not_own(self):
        child = self._start()
        original = self.ipc / 'original'
        self.credential.rename(original)
        self.credential.write_text('foreign')
        self.credential.chmod(0o600)
        self._stop(child, remaining=('c', 'original'))
        self.assertEqual(self.credential.read_text(), 'foreign')
        self.credential.unlink()
        original.unlink()

    def test_hello_authority_denial_uses_hello_envelope(self):
        child = self._start()
        self.authority.revoke_grant(self.grant)
        sock = self._socket()
        response = self._hello(sock)
        self.assertEqual(set(response), {'schema_version', 'request_id', 'state', 'session_id', 'service_incarnation'})
        self.assertEqual(response['state'], 'REFUSED')
        self.assertIsNone(response['session_id'])
        sock.close()
        self._stop(child, expected=1, autonomous=True)
