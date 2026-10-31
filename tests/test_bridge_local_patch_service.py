"""Real socket tests for the opt-in existing-file patch transport."""
import base64
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict

from orch.bridge_patch_execution import PatchExecutionStore, runtime_operation_lock, require_execution_resolved
from orch.bridge_operation_journal import OperationIntentJournal
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerRunBinding
from orch.bridge_local_protocol import LocalServiceLimits, encode_payload, receive_frame, send_frame


def child_main():
    from orch.bridge_local_patch_service import serve_local_bridge_patch_sessions
    from orch.bridge_local_service import LocalBridgeAdmission
    from orch.bridge_local_protocol import LocalServiceLimits
    from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
    config = json.load(sys.stdin)
    modules = ('tests.test_bridge_local_patch_service', 'orch', 'orch.bridge_local_patch_protocol',
               'orch.bridge_local_patch_service', 'orch.bridge_local_patch_client',
               'orch.bridge_local_broker', 'orch.bridge_authorization',
               'orch.bridge_readonly_authority', 'orch.bridge_file_patch',
               'orch.bridge_patch_intent', 'orch.bridge_patch_execution',
               'orch.bridge_operation_journal')
    origins = {}
    for name in modules:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        origins[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    receipt = {'pid': os.getpid(), 'modules': origins}
    Path(config['receipt_dir'], 'startup-%d.json' % os.getpid()).write_text(json.dumps(receipt, sort_keys=True))
    admissions = tuple(LocalBridgeAdmission(WorkerRunBinding(**item['binding']),
        Path(item['capability_file']), Path(item['credential_file'])) for item in config['admissions'])
    if config.get('fault') in ('DISPATCH', 'REPLACE', 'HOLD_DISPATCH'):
        from orch.bridge_file_patch import LocalBoundedFilePatcher
        import orch.bridge_file_patch as patch_module
        def wait_at_boundary():
            Path(config['barrier']).write_text('reached')
            if config['fault'] == 'HOLD_DISPATCH':
                while not Path(config['release']).exists(): time.sleep(.01)
            else:
                while True: signal.pause()
        old_append = LocalBoundedFilePatcher._append
        old_replace = patch_module.os.replace
        def append(self, record, authorize):
            result = old_append(self, record, authorize)
            if record['phase'] == config['fault'] or (record['phase'] == 'DISPATCH' and config['fault'] == 'HOLD_DISPATCH'): wait_at_boundary()
            return result
        def replace(*args, **kwargs):
            old_replace(*args, **kwargs)
            if config['fault'] == 'REPLACE': wait_at_boundary()
        LocalBoundedFilePatcher._append = append
        patch_module.os.replace = replace
    if config.get('fault') == 'HOLD_DISPATCH':
        from orch.bridge_local_patch_service import _PatchTransport
        old_close_peer = _PatchTransport.close_peer
        def close_peer(self, peer):
            old_close_peer(self, peer)
            if Path(config['barrier']).exists() and Path(config['release']).exists():
                Path(config['closed']).write_text('closed')
        _PatchTransport.close_peer = close_peer
    if config.get('fault') == 'QUEUE':
        from orch.bridge_local_patch_service import _PatchTransport
        old_queue = _PatchTransport.queue
        def queue(self, peer, value, **kwargs):
            if value.get('operation') == 'file.patch' and value.get('state') == 'SUCCEEDED':
                Path(config['barrier']).write_text('reached')
                while not Path(config['release']).exists(): time.sleep(.01)
            return old_queue(self, peer, value, **kwargs)
        _PatchTransport.queue = queue
    result = serve_local_bridge_patch_sessions(ReadOnlyAuthorityLedger(Path(config['home'])),
        admissions, Path(config['socket']), LocalServiceLimits(idle_timeout_sec=30))
    raise SystemExit(result)


class LocalPatchSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='orch-wire-patch-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.children = []
        self.all_children = []
        self.startup_receipts = {}
        self.client_observations = []
        self.addCleanup(self._stop_children)
        self.repo = self.base / 'repo'
        self._git('init', '-q', '-b', 'main', str(self.repo))
        self._git('-C', str(self.repo), 'config', 'user.name', 'Fixture')
        self._git('-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'file.txt').write_bytes(b'original\n')
        (self.repo / 'sentinel.txt').write_bytes(b'protected\n')
        self._git('-C', str(self.repo), 'add', '.')
        self._git('-C', str(self.repo), 'commit', '-qm', 'fixture')
        (self.base / 'home').mkdir(mode=0o700)
        self.orch = Orchestrator(self.base / 'home')
        self.receipt_dir = self.orch.root / 'fixture-observations'
        self.receipt_dir.mkdir(mode=0o700)
        project = ProjectRegistry(self.orch.root).add(self.repo, name='Wire fixture',
            profile='safe', review_mode='off', allow_commit=True, allow_push=False)['project']
        plan = build_single_task_plan(project, task_id='WIRE', goal='Test socket',
            allowed_paths=['file.txt', 'missing.txt'], plan_revision='wire-fixture')
        plan_path = self.base / 'plan.json'
        write_plan(plan_path, plan)
        self.orch.load_plan(plan_path)
        claim = self.orch.claim('fixture', project_id=project['project_id'])
        self.cap = Path(claim['capability_file'])
        self.binding = WorkerRunBinding(project['project_id'], project['writer_key'],
            claim['task_id'], claim['run_id'], claim['attempt'])
        self.ipc = self.base / 'ipc'; self.ipc.mkdir(mode=0o700)
        self.endpoint = self.ipc / 's'
        self.credential = self.ipc / 'a.json'

    def _git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=12)

    def _start(self, *, fault=None, extra=()):
        config = {'home': str(self.orch.root), 'socket': str(self.endpoint), 'admissions': [
            {'binding': asdict(self.binding), 'capability_file': str(self.cap),
             'credential_file': str(self.credential)}, *extra], 'fault': fault,
             'barrier': str(self.base / 'barrier'), 'release': str(self.base / 'release'),
             'closed': str(self.base / 'closed'), 'receipt_dir': str(self.receipt_dir)}
        import orch
        source_root = str(Path(orch.__file__).resolve().parent.parent)
        published_test_root = str(Path(__file__).resolve().parent.parent)
        bootstrap = ('import os, sys; '
                     'sys.path[:] = [p for p in sys.path if p and os.path.abspath(p) != os.getcwd()]; '
                     'sys.path[:0] = %r; '
                     'from tests.test_bridge_local_patch_service import child_main; child_main()'
                     % [source_root, published_test_root])
        child = subprocess.Popen([sys.executable, '-c', bootstrap],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.all_children.append(child)
        child.stdin.write(json.dumps(config)); child.stdin.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            receipt_path = self.receipt_dir / ('startup-%d.json' % child.pid)
            if self.endpoint.exists() and self.credential.exists() and receipt_path.exists():
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt['pid'], child.pid)
                self.startup_receipts[child.pid] = receipt
                return child
            if child.poll() is not None:
                self.fail('service exited: ' + child.stderr.read())
            time.sleep(.02)
        self.fail('service did not publish socket and credential')

    def _stop(self, child, *, expected=0):
        forced = False
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forced = True
                child.kill(); child.wait(timeout=5)
        error = child.stderr.read() if child.returncode != expected else ''
        child.stdout.close(); child.stderr.close()
        self.children.remove(child)
        if forced:
            self.fail('service required kill during cleanup')
        if child.returncode != expected:
            self.fail('service exited %s: %s' % (child.returncode, error))

    def _kill_child(self, child):
        child.kill(); child.wait(timeout=5)
        child.stdout.close(); child.stderr.close(); self.children.remove(child)
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def _stop_children(self):
        failures = []
        for child in list(self.children):
            try: self._stop(child)
            except Exception as exc: failures.append(str(exc))
        if failures: self.fail('; '.join(failures))

    def test_existing_file_is_replaced_once_and_replay_is_historical(self):
        # Removing socket patch dispatch would leave the original file unchanged.
        self.assertIsNotNone(importlib.util.find_spec('orch.bridge_local_patch_service'),
                             'opt-in patch endpoint is missing')
        from orch.bridge_local_patch_client import LocalPatchClient
        from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
        ledger = ReadOnlyAuthorityLedger(self.orch.root)
        with ledger.connect() as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE runs SET state='ABORTED'")
        db = self.orch.db_path
        tracked = (db, db.with_name(db.name + '-wal'))
        before = {str(p): p.read_bytes() if p.exists() else None for p in tracked}
        child = self._start()
        client = LocalPatchClient(self.credential)
        self.addCleanup(client.close)
        op = client.new_operation_id()
        result = client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                              baseline_size=9, replacement=b'replacement\n', operation_id=op)
        self.assertEqual(result['status'], 'APPLIED')
        self.assertFalse(result['historical'])
        self.assertEqual((self.repo / 'file.txt').read_bytes(), b'replacement\n')
        replay = client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                              baseline_size=9, replacement=b'replacement\n', operation_id=op)
        self.assertEqual(replay['status'], 'APPLIED')
        self.assertTrue(replay['historical'])
        found = PatchExecutionStore(self.orch.root).inspect(self.binding.run_id, op)
        self.assertTrue(found['terminal'])
        self.assertEqual(set(found['records']), {'dispatch', 'publication', 'result'})
        self.assertEqual(OperationIntentJournal(self.orch.root).inspect(self.binding.run_id, op)['status'], 'RECORDED')
        self.assertEqual((self.repo / 'sentinel.txt').read_bytes(), b'protected\n')
        self.assertEqual(before, {str(p): p.read_bytes() if p.exists() else None for p in tracked})
        self.assertEqual((self.repo / 'sentinel.txt').read_bytes(), b'protected\n')
        intent = OperationIntentJournal(self.orch.root).inspect(self.binding.run_id, op)
        execution = PatchExecutionStore(self.orch.root).inspect(self.binding.run_id, op)
        self.assertEqual(intent['status'], 'RECORDED')
        self.assertEqual(set(execution['records']), {'dispatch', 'publication', 'result'})
        self.assertTrue(execution['terminal'])
        client.close()
        self._stop(child)

    def _wire(self):
        credential = json.loads(self.credential.read_text())
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.settimeout(5); peer.connect(str(self.endpoint))
        send_frame(peer, dict(schema_version=1, request_id='hello', type='hello',
                              transport_token=credential['transport_token']), LocalServiceLimits())
        hello = receive_frame(peer, LocalServiceLimits())
        self.assertEqual(hello['state'], 'SUCCEEDED')
        return peer, hello

    def _patch_request(self, session, op, **changes):
        request = dict(schema_version=3, request_id='r', session_id=session, operation='file.patch',
            operation_id=op, target='file.txt', max_response_bytes=4096,
            baseline={'sha256': hashlib.sha256(b'original\n').hexdigest(), 'size_bytes': 9},
            replacement_base64=base64.b64encode(b'replacement\n').decode())
        request.update(changes)
        return request

    def test_foreign_path_stale_baseline_conflicts_and_missing_leaf_are_refused(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        self._start()
        client = LocalPatchClient(self.credential); self.addCleanup(client.close)
        digest = hashlib.sha256(b'original\n').hexdigest()
        for target, sha, op in [('sentinel.txt', digest, client.new_operation_id()),
                                ('missing.txt', digest, client.new_operation_id()),
                                ('file.txt', '0' * 64, client.new_operation_id())]:
            result = client.apply(target, baseline_sha256=sha, baseline_size=9,
                                  replacement=b'new', operation_id=op)
            self.assertEqual(result['status'], 'REFUSED', result)
        foreign = '0' * 64 + '.' + '1' * 64
        result = client.apply('file.txt', baseline_sha256=digest, baseline_size=9,
                              replacement=b'new', operation_id=foreign)
        self.assertEqual(result['status'], 'REFUSED')
        self.assertEqual((self.repo / 'file.txt').read_bytes(), b'original\n')
        self.assertEqual((self.repo / 'sentinel.txt').read_bytes(), b'protected\n')
        self.assertFalse((self.repo / 'missing.txt').exists())
        op = client.new_operation_id()
        try:
            applied = client.apply('file.txt', baseline_sha256=digest, baseline_size=9,
                                   replacement=b'new', operation_id=op)
        except ValueError as exc:
            if str(exc) != 'local_patch_call_outcome_unknown': raise
            self.client_observations.append({'operation': 'file.patch', 'outcome': 'UNKNOWN'})
            deadline = time.monotonic() + 20
            store = PatchExecutionStore(self.orch.root)
            while time.monotonic() < deadline:
                record = store.inspect(self.binding.run_id, op)
                if record.get('terminal'): break
                time.sleep(.02)
            self.assertTrue(record.get('terminal'), record)
            client = LocalPatchClient(self.credential); self.addCleanup(client.close)
            applied = client.reconcile('file.txt', operation_id=op)
            self.assertTrue(applied['historical'])
            self.client_observations.append({'operation': 'file.patch.reconcile', 'outcome': applied['status']})
        self.assertEqual(applied['status'], 'APPLIED')
        self.assertEqual(client.apply('file.txt', baseline_sha256=digest, baseline_size=9,
                         replacement=b'other', operation_id=op)['status'], 'CONFLICT')

    def test_disconnect_retry_and_restart_never_reapply_or_change_identity(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        child = self._start()
        client = LocalPatchClient(self.credential); op = client.new_operation_id(); client.close()
        peer, hello = self._wire()
        send_frame(peer, self._patch_request(hello['session_id'], op), LocalServiceLimits())
        peer.close()  # A reply may be lost; the service still owns dispatch.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (self.repo / 'file.txt').read_bytes() != b'replacement\n':
            time.sleep(.02)
        self.assertEqual((self.repo / 'file.txt').read_bytes(), b'replacement\n')
        inode = (self.repo / 'file.txt').stat().st_ino
        self._stop(child)
        self._start()
        client = LocalPatchClient(self.credential); self.addCleanup(client.close)
        result = client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                              baseline_size=9, replacement=b'replacement\n', operation_id=op,
                              max_response_bytes=8192)
        self.assertEqual((result['status'], result['historical']), ('APPLIED', True))
        self.assertEqual((self.repo / 'file.txt').stat().st_ino, inode)
        self.assertEqual(client.reconcile('file.txt', operation_id=op)['status'], 'APPLIED')

    def test_busy_lock_is_unknown_without_dispatch_then_explicit_attempt_works(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        self._start(); client = LocalPatchClient(self.credential); self.addCleanup(client.close)
        op = client.new_operation_id()
        with runtime_operation_lock(self.orch.root):
            result = client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                                  baseline_size=9, replacement=b'replacement\n', operation_id=op)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual((self.repo / 'file.txt').read_bytes(), b'original\n')
        self.assertEqual(PatchExecutionStore(self.orch.root).inspect(self.binding.run_id, op)['status'], 'ABSENT')
        result = client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                              baseline_size=9, replacement=b'replacement\n', operation_id=op)
        self.assertEqual(result['status'], 'APPLIED')

    def test_crash_boundaries_reconcile_without_reapplying(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        for phase, disposition in [('DISPATCH', 'NOT_APPLIED'), ('REPLACE', 'APPLIED')]:
            with self.subTest(phase=phase):
                # Distinct op IDs and reset bytes are fixture actions, never recovery.
                (self.repo / 'file.txt').write_bytes(b'original\n')
                barrier = self.base / 'barrier'
                if barrier.exists(): barrier.unlink()
                child = self._start(fault=phase)
                client = LocalPatchClient(self.credential); op = client.new_operation_id(); client.close()
                peer, hello = self._wire()
                send_frame(peer, self._patch_request(hello['session_id'], op), LocalServiceLimits())
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not barrier.exists(): time.sleep(.02)
                self.assertTrue(barrier.exists(), phase)
                self._kill_child(child); peer.close()
                # SIGKILL intentionally leaves owned endpoint objects in the disposable fixture.
                self.endpoint.unlink(); self.credential.unlink()
                found = PatchExecutionStore(self.orch.root).inspect(self.binding.run_id, op)
                self.assertEqual(found['status'], 'RECORDED'); self.assertFalse(found['terminal'])
                with self.assertRaisesRegex(ValueError, 'patch_execution_unresolved'):
                    self.orch.abort(self.binding.run_id, 'fixture unresolved')
                inode = (self.repo / 'file.txt').stat().st_ino
                restarted = self._start(); recovery = LocalPatchClient(self.credential)
                result = recovery.reconcile('file.txt', operation_id=op)
                self.assertEqual(result['status'], disposition, result)
                self.assertEqual((self.repo / 'file.txt').stat().st_ino, inode)
                recovery.close(); self._stop(restarted)

    def test_strict_schema_and_frame_budget_refuse_before_mutation(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        self._start(); client = LocalPatchClient(self.credential); op = client.new_operation_id(); client.close()
        for changes in ({'operation': 'file.delete'}, {'operation': 'file.create'},
                        {'replacement_base64': '!!!!'}, {'unexpected': True}, {'schema_version': True}):
            with self.subTest(changes=changes):
                peer, hello = self._wire()
                send_frame(peer, self._patch_request(hello['session_id'], op, **changes), LocalServiceLimits())
                result = receive_frame(peer, LocalServiceLimits())
                self.assertEqual(result['state'], 'REFUSED'); peer.close()
        client = LocalPatchClient(self.credential); self.addCleanup(client.close)
        with self.assertRaisesRegex(ValueError, 'local_bridge_invalid_request'):
            client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                         baseline_size=9, replacement=b'x' * 65536, operation_id=op)
        self.assertEqual((self.repo / 'file.txt').read_bytes(), b'original\n')

    def _second_admission(self):
        repo = self.base / 'repo-b'
        self._git('init', '-q', '-b', 'main', str(repo))
        self._git('-C', str(repo), 'config', 'user.name', 'Fixture')
        self._git('-C', str(repo), 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'file.txt').write_bytes(b'original\n')
        self._git('-C', str(repo), 'add', '.')
        self._git('-C', str(repo), 'commit', '-qm', 'fixture')
        project = ProjectRegistry(self.orch.root).add(repo, name='Second', profile='safe',
            review_mode='off', allow_commit=True, allow_push=False)['project']
        plan = build_single_task_plan(project, task_id='PATCH-B', goal='Second route',
            allowed_paths=['file.txt'], plan_revision='wire-b')
        path = self.base / 'plan-b.json'; write_plan(path, plan); self.orch.load_plan(path)
        claim = self.orch.claim('fixture-b', project_id=project['project_id'])
        binding = WorkerRunBinding(project['project_id'], project['writer_key'], claim['task_id'],
                                   claim['run_id'], claim['attempt'])
        credential = self.ipc / 'b.json'
        return repo, credential, dict(binding=asdict(binding), capability_file=claim['capability_file'],
                                      credential_file=str(credential))

    def test_foreign_scope_session_and_old_token_are_denied(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        repo, credential, second = self._second_admission()
        child = self._start(extra=(second,))
        a = LocalPatchClient(self.credential); b = LocalPatchClient(credential)
        foreign = a.new_operation_id(); op_b = b.new_operation_id()
        denied = b.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                         baseline_size=9, replacement=b'bad', operation_id=foreign)
        self.assertEqual(denied['status'], 'REFUSED'); self.assertEqual((repo / 'file.txt').read_bytes(), b'original\n')
        b.close(); a.close()
        peer, hello = self._wire()
        request = self._patch_request(hello['session_id'], foreign, session_id='z' * 32)
        send_frame(peer, request, LocalServiceLimits())
        self.assertEqual(receive_frame(peer, LocalServiceLimits())['error_code'], 'local_bridge_session_denied')
        peer.close()
        old = json.loads(self.credential.read_text())['transport_token']
        self._stop(child); self._start(extra=(second,))
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); peer.settimeout(5); peer.connect(str(self.endpoint))
        send_frame(peer, dict(schema_version=1, request_id='old', type='hello', transport_token=old), LocalServiceLimits())
        self.assertEqual(receive_frame(peer, LocalServiceLimits())['error_code'], 'local_bridge_auth_denied')
        peer.close()
        b = LocalPatchClient(credential); self.addCleanup(b.close)
        self.assertEqual(b.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                         baseline_size=9, replacement=b'good', operation_id=op_b)['status'], 'APPLIED')

    def test_duplicate_peer_cannot_evict_live_route(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        self._start()
        client = LocalPatchClient(self.credential); self.addCleanup(client.close); op = client.new_operation_id()
        duplicate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); duplicate.settimeout(5)
        duplicate.connect(str(self.endpoint)); token = json.loads(self.credential.read_text())['transport_token']
        send_frame(duplicate, dict(schema_version=1, request_id='dup', type='hello', transport_token=token), LocalServiceLimits())
        with self.assertRaises((EOFError, OSError, ConnectionError)):
            receive_frame(duplicate, LocalServiceLimits())
        duplicate.close()
        result = client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                              baseline_size=9, replacement=b'replacement\n', operation_id=op)
        self.assertEqual(result['status'], 'APPLIED')

    def test_ambiguous_foreign_content_remains_unknown_and_blocks_closure(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        child = self._start(fault='REPLACE')
        client = LocalPatchClient(self.credential); op = client.new_operation_id(); client.close()
        peer, hello = self._wire(); send_frame(peer, self._patch_request(hello['session_id'], op), LocalServiceLimits())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (self.base / 'barrier').exists(): time.sleep(.02)
        self.assertTrue((self.base / 'barrier').exists())
        self._kill_child(child); peer.close()
        self.endpoint.unlink(); self.credential.unlink()
        (self.repo / 'file.txt').write_bytes(b'foreign\n')
        self._start(); recovery = LocalPatchClient(self.credential); self.addCleanup(recovery.close)
        self.assertEqual(recovery.reconcile('file.txt', operation_id=op)['status'], 'UNKNOWN')
        self.assertEqual((self.repo / 'file.txt').read_bytes(), b'foreign\n')
        with self.assertRaisesRegex(ValueError, 'patch_execution_unresolved'):
            self.orch.abort(self.binding.run_id, 'fixture unresolved')

    def test_revocation_before_queued_response_does_not_affect_other_route(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        repo, credential, second = self._second_admission()
        child = self._start(fault='QUEUE', extra=(second,))
        client = LocalPatchClient(self.credential); op = client.new_operation_id(); client.close()
        peer, hello = self._wire(); send_frame(peer, self._patch_request(hello['session_id'], op), LocalServiceLimits())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (self.base / 'barrier').exists(): time.sleep(.02)
        self.assertTrue((self.base / 'barrier').exists())
        self.cap.unlink(); (self.base / 'release').write_text('release')
        response = receive_frame(peer, LocalServiceLimits())
        self.assertEqual(response['state'], 'REFUSED')
        self.assertEqual(response['error_code'], 'local_bridge_authority_denied'); peer.close()
        b = LocalPatchClient(credential); op_b = b.new_operation_id()
        result = b.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                         baseline_size=9, replacement=b'other route', operation_id=op_b)
        self.assertEqual(result['status'], 'APPLIED'); b.close()
        self.assertEqual((repo / 'file.txt').read_bytes(), b'other route')
        self._stop(child, expected=1)

    def test_concurrent_services_same_operation_dispatch_at_most_once(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        first = self._start(fault='HOLD_DISPATCH')
        original_endpoint, original_credential = self.endpoint, self.credential
        client = LocalPatchClient(original_credential); op = client.new_operation_id(); client.close()
        peer, hello = self._wire()
        send_frame(peer, self._patch_request(hello['session_id'], op), LocalServiceLimits())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (self.base / 'barrier').exists(): time.sleep(.02)
        self.assertTrue((self.base / 'barrier').exists())
        self.endpoint = self.ipc / 'second'; self.credential = self.ipc / 'second.json'
        second = self._start(); contender = LocalPatchClient(self.credential)
        result = contender.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
            baseline_size=9, replacement=b'replacement\n', operation_id=op)
        self.assertEqual(result['status'], 'UNKNOWN')
        (self.base / 'release').write_text('release')
        response = receive_frame(peer, LocalServiceLimits()); peer.close()
        self.assertEqual(response['result']['status'], 'APPLIED')
        inode = (self.repo / 'file.txt').stat().st_ino
        replay = contender.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
            baseline_size=9, replacement=b'replacement\n', operation_id=op)
        self.assertEqual((replay['status'], replay['historical']), ('APPLIED', True))
        self.assertEqual((self.repo / 'file.txt').stat().st_ino, inode)
        contender.close(); self._stop(second); self._stop(first)

    def test_service_refuses_write_capable_native_ledger(self):
        from orch.bridge_local_patch_service import serve_local_bridge_patch_sessions
        from orch.bridge_local_service import LocalBridgeAdmission
        admission = LocalBridgeAdmission(self.binding, self.cap, self.credential)
        result = serve_local_bridge_patch_sessions(self.orch, (admission,), self.endpoint,
                                                   LocalServiceLimits(idle_timeout_sec=.01))
        self.assertEqual(result, 1)
        self.assertFalse(self.endpoint.exists())
        self.assertFalse(self.credential.exists())

    def test_timeout_is_unknown_then_explicit_reconcile_never_reapplies(self):
        from orch.bridge_local_patch_client import LocalPatchClient
        self._start(fault='HOLD_DISPATCH')
        client = LocalPatchClient(self.credential); op = client.new_operation_id()
        with self.assertRaisesRegex(ValueError, 'local_patch_call_outcome_unknown'):
            client.apply('file.txt', baseline_sha256=hashlib.sha256(b'original\n').hexdigest(),
                         baseline_size=9, replacement=b'new', operation_id=op)
        self.assertTrue((self.base / 'barrier').exists())
        self.client_observations.append({'operation': 'file.patch', 'outcome': 'UNKNOWN'})
        (self.base / 'release').write_text('release')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (self.base / 'closed').exists(): time.sleep(.02)
        self.assertTrue((self.base / 'closed').exists())
        record = PatchExecutionStore(self.orch.root).inspect(self.binding.run_id, op)
        self.assertTrue(record['terminal'])
        inode = (self.repo / 'file.txt').stat().st_ino
        recovery = LocalPatchClient(self.credential); self.addCleanup(recovery.close)
        result = recovery.reconcile('file.txt', operation_id=op)
        self.assertEqual((result['status'], result['historical']), ('APPLIED', True))
        self.assertEqual((self.repo / 'file.txt').stat().st_ino, inode)
        self.assertEqual((self.repo / 'file.txt').read_bytes(), b'new')
        self.client_observations.append({'operation': 'file.patch.reconcile', 'outcome': 'APPLIED'})
