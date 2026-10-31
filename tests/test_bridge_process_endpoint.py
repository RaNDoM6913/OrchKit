"""Real local endpoint coverage for explicitly admitted process profiles."""
import importlib
import importlib.util
import unittest

from orch.bridge_local_protocol import LocalServiceLimits, validate_request as validate_legacy


def _test_client(base):
    class Client(base):
        def call(self, operation, target, *, operation_id, **kwargs):
            if '.' not in operation_id:
                operation_id = self.new_operation_id()
            return super().call(operation, target, operation_id=operation_id, **kwargs)
    return Client


class ProcessProtocolTests(unittest.TestCase):
    def test_process_start_is_opt_in_and_legacy_wire_rejects_it(self):
        request = {'schema_version': 2, 'request_id': 'request-1',
                   'session_id': 's' * 32, 'operation_id': 'a' * 64 + '.' + 'b' * 64,
                   'operation': 'process.start', 'target': 'approved-profile',
                   'cursor': None, 'max_response_bytes': 4096}
        with self.assertRaises(ValueError):
            validate_legacy(request, LocalServiceLimits())
        self.assertIsNotNone(importlib.util.find_spec('orch.bridge_local_process_protocol'),
                             'the separate process protocol must accept an approved profile ID')
        protocol = importlib.import_module('orch.bridge_local_process_protocol')
        self.assertEqual(protocol.validate_process_request(request, LocalServiceLimits()), request)


class ProcessEndpointIntegrationTests(unittest.TestCase):
    def setUp(self):
        import os
        import subprocess
        import tempfile
        from pathlib import Path
        from orch.core import Orchestrator
        from orch.plan import build_single_task_plan, write_plan
        from orch.project import ProjectRegistry
        from orch.worker_operations import WorkerRunBinding
        self.temp = tempfile.TemporaryDirectory(prefix='orch-b07-wire-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        (self.base / 'home').mkdir(mode=0o700)
        self.orch = Orchestrator(self.base / 'home')
        self.registry = ProjectRegistry(self.orch.root)
        self.admissions = []
        self.bindings = []
        for label in ('a', 'b', 'c', 'd'):
            root = self.base / ('repo-' + label)
            env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
            def git(*args):
                subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
                               env=env, check=True, capture_output=True, timeout=12)
            git('init', '-q', '-b', 'main', str(root))
            git('-C', str(root), 'config', 'user.email', 'fixture@example.invalid')
            git('-C', str(root), 'config', 'user.name', 'Fixture')
            (root / 'README.md').write_text('fixture\n')
            git('-C', str(root), 'add', 'README.md')
            git('-C', str(root), 'commit', '-qm', 'fixture')
            project = self.registry.add(root, name='B07 ' + label, profile='safe',
                                        review_mode='off', allow_commit=True, allow_push=False)['project']
            plan = build_single_task_plan(project, task_id='B07-' + label.upper(), goal='process endpoint',
                                          allowed_paths=['README.md'], plan_revision='b07-' + label)
            path = self.base / (label + '.json')
            write_plan(path, plan)
            self.orch.load_plan(path)
            claim = self.orch.claim('fixture-' + label, project_id=project['project_id'])
            self.assertEqual(claim['status'], 'CLAIMED')
            binding = WorkerRunBinding(project['project_id'], project['writer_key'],
                                       claim['task_id'], claim['run_id'], claim['attempt'])
            self.bindings.append(binding)
            self.admissions.append((binding, Path(claim['capability_file']), root))
        self.endpoint = self.base / 'endpoint'
        self.endpoint.mkdir(mode=0o700)
        self.server = None
        import orch.bridge_process as process_module
        from unittest import mock
        import orch.bridge_local_concurrent_service as concurrent_module
        import orch.bridge_local_process_service as service_module
        import orch.bridge_local_process_client as client_module
        import orch.bridge_process_store as store_module
        self.b07_runtime_observations = {'sources': {
            module.__name__: str(Path(module.__file__).resolve()) for module in
            (process_module, concurrent_module, service_module, client_module, store_module)},
            'endpoints': [], 'children': []}

        self.b07_service_children = []
        self.b07_server_threads = []
        self.b07_server_results = []
        self.foreign = self.base / 'foreign-sentinel'
        self.foreign.write_bytes(b'foreign resource must survive')
        self.native_before = self._native_state()
        with self.orch.connect() as conn:
            self.event_before = conn.execute('SELECT COALESCE(MAX(seq), 0) FROM events').fetchone()[0]
        original_spawn = process_module._spawn_process
        def observe_spawn(profile):
            child = original_spawn(profile)
            self.b07_service_children.append(child)
            return child
        patcher = mock.patch.object(process_module, '_spawn_process', observe_spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._capture_runtime)

    def _native_state(self):
        import hashlib
        import json
        with self.orch.connect() as conn:
            result = []
            for binding in self.bindings:
                run = tuple(conn.execute('SELECT * FROM runs WHERE run_id=?', (binding.run_id,)).fetchone())
                task = tuple(conn.execute('SELECT * FROM tasks WHERE task_id=?', (binding.task_id,)).fetchone())
                digest = lambda value: hashlib.sha256(json.dumps(value, separators=(',', ':'), default=str).encode()).hexdigest()
                result.append({'run_id': binding.run_id, 'task_id': binding.task_id,
                               'run_sha256': digest(run), 'task_sha256': digest(task)})
            return result

    @staticmethod
    def _endpoint_state(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        return (info.st_dev, info.st_ino, info.st_mode)

    def _record_endpoints(self, paths):
        self.b07_runtime_observations['endpoints'].append(
            {str(path): self._endpoint_state(path) for path in paths})

    def _capture_runtime(self):
        self.b07_runtime_observations['servers'] = [
            {'alive': thread.is_alive(), 'results': list(result)}
            for thread, result in zip(self.b07_server_threads, self.b07_server_results)]
        self.assertFalse(any(server['alive'] for server in self.b07_runtime_observations['servers']))
        self.b07_runtime_observations['children'] = [
            {'pid': child.pid, 'returncode': child.returncode,
             'stdout_closed': child.stdout.closed} for child in self.b07_service_children]
        endpoint_paths = (self.endpoint / 'bridge.sock', *(self.endpoint / (str(i) + '.json') for i in range(3)),
                          self.endpoint / 'd.sock', self.endpoint / 'd.json')
        self.b07_runtime_observations['remaining_endpoints'] = {
            str(path): self._endpoint_state(path) for path in endpoint_paths}
        self.b07_runtime_observations['server_threads'] = [thread.is_alive() for thread in self.b07_server_threads]
        self.b07_runtime_observations['server_results'] = list(self.b07_server_results)
        self.assertFalse(any(self.b07_runtime_observations['server_threads']))
        self.b07_runtime_observations['native_before'] = self.native_before
        self.b07_runtime_observations['native_after'] = self._native_state()
        with self.orch.connect() as conn:
            events = [tuple(row) for row in conn.execute(
                'SELECT seq,kind,task_id,run_id FROM events WHERE seq>? ORDER BY seq', (self.event_before,))]
        self.b07_runtime_observations['process_event_delta'] = events
        self.assertTrue(all(event[1] in ('PROCESS_DISPATCH_INTENT', 'PROCESS_EXIT_OBSERVED')
                            for event in events), events)

        self.b07_runtime_observations['foreign_unchanged'] = (
            self.foreign.read_bytes() == b'foreign resource must survive')
        self.assertEqual(self.native_before, self.b07_runtime_observations['native_after'])
        self.assertTrue(self.b07_runtime_observations['foreign_unchanged'])
        for child in self.b07_runtime_observations['children']:
            self.assertIsNotNone(child['returncode'])
            self.assertTrue(child['stdout_closed'])

    def _launch(self, code='print("endpoint-child", flush=True)', output_limit=1024, io_timeout=5):
        import sys
        import threading
        import time
        from pathlib import Path
        from orch.bridge_local_protocol import LocalServiceLimits
        from orch.bridge_process import OwnedProcessProfile
        from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
        self.assertIsNotNone(importlib.util.find_spec('orch.bridge_local_process_service'),
                             'the opt-in process service must serve admitted profiles')
        service = importlib.import_module('orch.bridge_local_process_service')
        admissions = tuple(service.LocalProcessAdmission(binding, capability,
            self.endpoint / (str(i) + '.json'), (OwnedProcessProfile(
                'approved', Path(sys.executable).resolve(), ('-c', code), root,
                {'LANG': 'C'}, 30, output_limit),) + ((OwnedProcessProfile(
                'only-a', Path(sys.executable).resolve(), ('-c', code), root,
                {'LANG': 'C'}, 30, output_limit),) if i == 0 else ()))
            for i, (binding, capability, root) in enumerate(self.admissions[:3]))
        self.result = []
        self.server = threading.Thread(target=lambda: self.result.append(
            service.serve_local_bridge_process_sessions(ReadOnlyAuthorityLedger(self.orch.root),
                admissions, self.endpoint / 'bridge.sock', LocalServiceLimits(io_timeout_sec=io_timeout, idle_timeout_sec=2))))
        self.b07_server_threads.append(self.server)
        self.b07_server_results.append(self.result)
        self.server.start()
        self.addCleanup(self._stop_server)
        deadline = time.monotonic() + 8
        while (not all((self.endpoint / (str(i) + '.json')).exists() for i in range(3))
               and self.server.is_alive() and time.monotonic() < deadline):
            time.sleep(.02)
        self.assertTrue(all((self.endpoint / (str(i) + '.json')).exists() for i in range(3)), self.result)
        self._record_endpoints([self.endpoint / 'bridge.sock', *(self.endpoint / (str(i) + '.json') for i in range(3))])

    def _stop_server(self):
        if self.server:
            self.server.join(timeout=8)
            self.assertFalse(self.server.is_alive())

    def test_real_start_poll_and_duplicate_after_reconnect(self):
        import base64
        import time
        self._launch()
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        first = LocalProcessClient(self.endpoint / '0.json')
        start = first.call('process.start', 'approved', operation_id='same-op')
        self.assertEqual(start['state'], 'SUCCEEDED', start)
        handle = start['result']['handle']
        first.close()
        second = LocalProcessClient(self.endpoint / '0.json')
        self.addCleanup(second.close)
        again = second.call('process.start', 'approved', operation_id=start['operation_id'])
        self.assertEqual(again['result']['handle'], handle)
        deadline = time.monotonic() + 5
        i = 0
        while True:
            i += 1
            observed = second.call('process.poll', handle, operation_id='poll-' + str(i))
            self.assertEqual(observed['state'], 'SUCCEEDED', observed)
            if observed['result']['process_state'] == 'EXITED':
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        self.assertIn(b'endpoint-child', base64.b64decode(observed['result']['output_base64']))
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 1)

    def test_cross_session_handle_cursor_and_conflicting_operation_are_denied(self):
        self._launch('import time; print("data", flush=True); time.sleep(20)')
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        a = LocalProcessClient(self.endpoint / '0.json')
        b = LocalProcessClient(self.endpoint / '1.json')
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        start = a.call('process.start', 'approved', operation_id='first')
        handle, cursor = start['result']['handle'], start['result']['next_cursor']
        stolen = b.call('process.start', 'approved', operation_id=start['operation_id'])
        self.assertEqual((stolen['state'], stolen['error_code']), ('REFUSED', 'process_operation_denied'))
        self.assertEqual(a.call('process.start', 'missing', operation_id=start['operation_id'])['error_code'],
                         'process_operation_conflict')
        self.assertEqual(a.call('process.start', 'only-a', operation_id='a-profile')['state'], 'SUCCEEDED')
        self.assertEqual(b.call('process.start', 'only-a', operation_id='new')['error_code'],
                         'process_profile_denied')
        self.assertEqual(b.call('process.poll', handle, operation_id='foreign-handle')['error_code'],
                         'process_handle_denied')
        foreign = b.call('process.start', 'approved', operation_id='b-start')['result']['handle']
        denied = b.call('process.poll', foreign, operation_id='foreign-cursor', cursor=cursor)
        self.assertEqual((denied['state'], denied['error_code']), ('REFUSED', 'process_cursor_denied'))
        self.assertEqual(b.call('process.poll', foreign, operation_id='still-live')['state'], 'SUCCEEDED')

    def test_process_schema_refuses_executable_binding_and_v1_calls(self):
        from orch.bridge_local_process_protocol import validate_process_request
        request = {'schema_version': 2, 'request_id': 'r', 'session_id': 's' * 32,
                   'operation_id': 'a' * 64 + '.' + 'b' * 64, 'operation': 'process.start', 'target': 'approved',
                   'cursor': None, 'max_response_bytes': 4096}
        for field, value in [('binding', {}), ('executable', '/bin/sh'), ('argv', []),
                             ('env', {}), ('pid', 1), ('capability_file', '/tmp/cap')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_process_request(dict(request, **{field: value}), LocalServiceLimits())
        for field, value in [('schema_version', 1), ('operation', 'process.shell'),
                             ('max_response_bytes', True), ('cursor', '0')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_process_request(dict(request, **{field: value}), LocalServiceLimits())
        for operation in ('process.start', 'process.poll', 'process.terminate'):
            legacy = {'schema_version': 1, 'request_id': 'r', 'session_id': 's' * 32,
                      'operation': operation, 'target': 'approved', 'max_response_bytes': 4096}
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                validate_legacy(legacy, LocalServiceLimits())

    def test_real_terminate_and_bounded_truncated_output(self):
        import base64
        import time
        self._launch('import time; print("z" * 50000, flush=True); time.sleep(20)')
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        client = LocalProcessClient(self.endpoint / '0.json')
        self.addCleanup(client.close)
        start = client.call('process.start', 'approved', operation_id='start')
        handle = start['result']['handle']
        deadline = time.monotonic() + 5
        while True:
            time.sleep(.05)
            item = client.call('process.poll', handle, operation_id='poll-' + str(time.monotonic_ns()))['result']
            if item['truncated']:
                break
            self.assertLess(time.monotonic(), deadline)
        self.assertEqual(item['retained_output_bytes'], 1024)
        self.assertEqual(len(base64.b64decode(item['output_base64'])), 1024)
        terminated = client.call('process.terminate', handle, operation_id='terminate')
        self.assertEqual(terminated['state'], 'SUCCEEDED')
        while True:
            time.sleep(.05)
            item = client.call('process.poll', handle, operation_id='poll-' + str(time.monotonic_ns()))['result']
            if item['process_state'] == 'EXITED':
                break
            self.assertLess(time.monotonic(), deadline)
        self.assertEqual(item['reason'], 'terminated')

    def test_two_per_run_capacity_and_other_routes_progress(self):
        self._launch('import time; time.sleep(20)')
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        clients = [LocalProcessClient(self.endpoint / (str(i) + '.json')) for i in range(3)]
        for client in clients:
            self.addCleanup(client.close)
        for i, client in enumerate(clients):
            for j in range(2):
                response = client.call('process.start', 'approved', operation_id='start-%d-%d' % (i, j))
                self.assertEqual(response['state'], 'SUCCEEDED', response)
            overflow = client.call('process.start', 'approved', operation_id='overflow-%d' % i)
            self.assertEqual(overflow['error_code'], 'process_dispatch_capacity')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 6)
        # A fourth run has no per-run reservations; rejection proves the shared global limit.
        import sys
        import threading
        import time
        from pathlib import Path
        from orch.bridge_local_process_service import LocalProcessAdmission, serve_local_bridge_process_sessions
        from orch.bridge_process import OwnedProcessProfile
        from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
        binding, capability, root = self.admissions[3]
        admission = LocalProcessAdmission(binding, capability, self.endpoint / 'd.json',
            (OwnedProcessProfile('approved', Path(sys.executable).resolve(),
             ('-c', 'import time; time.sleep(20)'), root, {'LANG': 'C'}, 30, 1024),))
        result = []
        server = threading.Thread(target=lambda: result.append(serve_local_bridge_process_sessions(
            ReadOnlyAuthorityLedger(self.orch.root), (admission,), self.endpoint / 'd.sock',
            LocalServiceLimits(idle_timeout_sec=2))))
        self.b07_server_threads.append(server)
        self.b07_server_results.append(result)
        server.start()
        try:
            deadline = time.monotonic() + 8
            while not (self.endpoint / 'd.json').exists() and server.is_alive() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.endpoint / 'd.json').exists(), result)
            self._record_endpoints([self.endpoint / 'd.sock', self.endpoint / 'd.json'])
            fourth = LocalProcessClient(self.endpoint / 'd.json')
            try:
                self.assertEqual(fourth.call('process.start', 'approved', operation_id='fourth')['error_code'],
                                 'process_dispatch_capacity')
            finally:
                fourth.close()
        finally:
            server.join(timeout=8)
            self.assertFalse(server.is_alive())

    def test_lost_response_reconnect_and_restart_never_respawns_or_adopts(self):
        import time
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        from orch.bridge_local_protocol import send_frame
        self._launch('import time; time.sleep(20)')
        first = LocalProcessClient(self.endpoint / '0.json')
        first._connect()
        request = {'schema_version': 2, 'request_id': 'lost-request', 'session_id': first._session,
                   'operation_id': first.new_operation_id(), 'operation': 'process.start', 'target': 'approved',
                   'cursor': None, 'max_response_bytes': 4096}
        send_frame(first._socket, request, first._limits)
        first.close()
        deadline = time.monotonic() + 5
        while True:
            with self.orch.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0]
            if count:
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        time.sleep(.1)
        second = LocalProcessClient(self.endpoint / '0.json')
        result = second.call('process.start', 'approved', operation_id=request['operation_id'])
        self.assertEqual(result['state'], 'SUCCEEDED')
        handle = result['result']['handle']
        second.close()
        self.server.join(timeout=8)
        self.assertFalse(self.server.is_alive())
        self._launch('import time; time.sleep(20)')
        third = LocalProcessClient(self.endpoint / '0.json')
        self.addCleanup(third.close)
        self.assertEqual(third.call('process.start', 'approved', operation_id=request['operation_id'])['error_code'],
                         'process_operation_denied')
        self.assertEqual(third.call('process.poll', handle, operation_id='old-handle')['error_code'],
                         'process_handle_denied')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 1)

    def _revocation_case(self, phase):
        from unittest import mock
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        from orch.bridge_local_process_service import _ProcessTransport
        from orch.bridge_local_broker import LocalRunContext
        capability = self.admissions[1][1]
        original_dispatch = _ProcessTransport.dispatch
        original_write = _ProcessTransport.write
        original_close = LocalRunContext.close
        triggered, cleanup_failed = [], []
        def selected(peer):
            return (peer.route is not None and peer.route.context.binding == self.bindings[1]
                    and peer.request is not None and peer.request.get('operation') == 'process.start')
        def dispatch(transport, peer, value):
            if (phase == 'dispatch' and peer.route is not None
                    and peer.route.context.binding == self.bindings[1]
                    and value.get('operation') == 'process.start' and not triggered):
                capability.chmod(0o640)
                triggered.append(True)
            return original_dispatch(transport, peer, value)
        def write(transport, peer):
            if phase == 'send' and selected(peer) and peer.success and not triggered:
                capability.chmod(0o640)
                triggered.append(True)
            return original_write(transport, peer)
        def close(context):
            if context.binding == self.bindings[1] and not cleanup_failed:
                cleanup_failed.append(True)
                raise OSError('fixture one-time owned cleanup failure')
            return original_close(context)
        with mock.patch.object(_ProcessTransport, 'dispatch', dispatch), \
             mock.patch.object(_ProcessTransport, 'write', write), \
             mock.patch.object(LocalRunContext, 'close', close):
            self._launch('import time; time.sleep(20)')
            a, b, c = [LocalProcessClient(self.endpoint / (str(i) + '.json')) for i in range(3)]
            try:
                denied = b.call('process.start', 'approved', operation_id='revoked')
                self.assertEqual(denied['error_code'], 'local_bridge_authority_denied')
                self.assertTrue(triggered)
                self.assertTrue(cleanup_failed)
                for label, client in (('a', a), ('c', c)):
                    response = client.call('process.start', 'approved', operation_id=label + '-alive')
                    self.assertEqual(response['state'], 'SUCCEEDED', response)
            finally:
                a.close(); b.close(); c.close()
                capability.chmod(0o600)
            self.server.join(timeout=8)
            self.assertFalse(self.server.is_alive())
    def test_revocation_before_dispatch_retires_only_selected_route(self):
        self._revocation_case('dispatch')

    def test_revocation_before_success_send_retires_only_selected_route(self):
        self._revocation_case('send')

    def test_revocation_after_partial_success_frame_closes_without_remaining_data(self):
        import time
        from unittest import mock
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        from orch.bridge_local_process_service import _ProcessTransport
        capability = self.admissions[1][1]
        original_write = _ProcessTransport.write
        triggered, selected_id = [], []
        def write(transport, peer):
            if (peer.route is not None and peer.route.context.binding == self.bindings[1]
                    and peer.request is not None and selected_id and peer.request.get('operation_id') == selected_id[0]
                    and peer.sent > 0 and not triggered):
                triggered.append(peer.sent)
                capability.chmod(0o640)
            return original_write(transport, peer)
        with mock.patch.object(_ProcessTransport, 'write', write):
            self._launch('import time; print("x" * 50000, flush=True); time.sleep(20)', output_limit=55000)
            a, b, c = [LocalProcessClient(self.endpoint / (str(i) + '.json')) for i in range(3)]
            try:
                handle = b.call('process.start', 'approved', operation_id='start')['result']['handle']
                deadline = time.monotonic() + 5
                i = 0
                while True:
                    i += 1
                    result = b.call('process.poll', handle, operation_id='ready-' + str(i))['result']
                    if result['retained_output_bytes'] >= 50000:
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                selected_id.append(b.new_operation_id())
                with self.assertRaisesRegex(ValueError, 'local_bridge_call_failed'):
                    b.call('process.poll', handle, operation_id=selected_id[0], max_response_bytes=65536)
                self.assertTrue(triggered)
                for name, client in (('a', a), ('c', c)):
                    self.assertEqual(client.call('process.start', 'approved', operation_id=name)['state'], 'SUCCEEDED')
            finally:
                a.close(); b.close(); c.close()
                capability.chmod(0o600)

    def test_unresolved_exits_charge_run_and_global_capacity_after_restart(self):
        import sys
        import threading
        import time
        from pathlib import Path
        from unittest import mock
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        from orch.bridge_local_process_service import LocalProcessAdmission, serve_local_bridge_process_sessions
        from orch.bridge_process import OwnedProcessProfile
        from orch.bridge_process_store import ProcessOperationStore
        from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
        LocalProcessClient = _test_client(BaseProcessClient)
        original = ProcessOperationStore.observe_exit
        def unconfirmed(store, intent, *args):
            if intent['request']['binding']['run_id'] == self.bindings[1].run_id:
                raise OSError('fixture unconfirmed terminal journal')
            return original(store, intent, *args)
        with mock.patch.object(ProcessOperationStore, 'observe_exit', unconfirmed):
            self._launch('import time; time.sleep(20)')
            clients = [LocalProcessClient(self.endpoint / (str(i) + '.json')) for i in range(3)]
            handles = []
            for i, client in enumerate(clients):
                group = []
                for j in range(2):
                    response = client.call('process.start', 'approved', operation_id='s-%d-%d' % (i, j))
                    self.assertEqual(response['state'], 'SUCCEEDED', response)
                    group.append(response['result']['handle'])
                handles.append(group)
            for i, handle in enumerate(handles[1]):
                self.assertEqual(clients[1].call('process.terminate', handle, operation_id='terminate-' + str(i))['state'], 'SUCCEEDED')
            deadline = time.monotonic() + 6
            for i, handle in enumerate(handles[1]):
                count = 0
                while True:
                    count += 1
                    response = clients[1].call('process.poll', handle, operation_id='poll-%d-%d' % (i, count))
                    if response['state'] == 'UNKNOWN':
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
            for client in clients:
                client.close()
            self.server.join(timeout=8)
            self.assertFalse(self.server.is_alive())
            self._launch('import time; time.sleep(20)')
            fresh = [LocalProcessClient(self.endpoint / (str(i) + '.json')) for i in range(3)]
            try:
                self.assertEqual(fresh[1].call('process.start', 'approved', operation_id='b-blocked')['error_code'],
                                 'process_dispatch_capacity')
                for index in (0, 2):
                    for j in range(2):
                        result = fresh[index].call('process.start', 'approved', operation_id='new-%d-%d' % (index, j))
                        self.assertEqual(result['state'], 'SUCCEEDED', result)
                binding, capability, root = self.admissions[3]
                admission = LocalProcessAdmission(binding, capability, self.endpoint / 'd.json',
                    (OwnedProcessProfile('approved', Path(sys.executable).resolve(),
                     ('-c', 'import time; time.sleep(20)'), root, {'LANG': 'C'}, 30, 1024),))
                status = []
                server = threading.Thread(target=lambda: status.append(serve_local_bridge_process_sessions(
                    ReadOnlyAuthorityLedger(self.orch.root), (admission,), self.endpoint / 'd.sock',
                    LocalServiceLimits(idle_timeout_sec=2))))
                self.b07_server_threads.append(server)
                self.b07_server_results.append(status)
                server.start()
                try:
                    deadline = time.monotonic() + 8
                    while not (self.endpoint / 'd.json').exists() and server.is_alive() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue((self.endpoint / 'd.json').exists(), status)
                    self._record_endpoints([self.endpoint / 'd.sock', self.endpoint / 'd.json'])
                    fourth = LocalProcessClient(self.endpoint / 'd.json')
                    try:
                        denied = fourth.call('process.start', 'approved', operation_id='global-blocked')
                        self.assertEqual(denied['error_code'], 'process_dispatch_capacity')
                    finally:
                        fourth.close()
                finally:
                    server.join(timeout=8)
                    self.assertFalse(server.is_alive())
            finally:
                for client in fresh:
                    client.close()
            self.server.join(timeout=8)
            self.assertFalse(self.server.is_alive())
            with self.orch.connect() as conn:
                intents = conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT' AND run_id=?",
                                       (self.bindings[1].run_id,)).fetchone()[0]
                exits = conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED' AND run_id=?",
                                     (self.bindings[1].run_id,)).fetchone()[0]
            self.assertEqual((intents, exits), (2, 0))

    def test_b_partial_request_timeout_preserves_b_owner_and_a_c_progress(self):
        import time
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        self._launch('import time; time.sleep(20)', io_timeout=.6)
        a, b, c = [LocalProcessClient(self.endpoint / (str(i) + '.json')) for i in range(3)]
        try:
            start = b.call('process.start', 'approved', operation_id='b-start')
            self.assertEqual(start['state'], 'SUCCEEDED')
            b.close()
            time.sleep(.1)
            b = LocalProcessClient(self.endpoint / '1.json')
            reconnected = b.call('process.start', 'approved', operation_id=start['operation_id'])
            self.assertEqual(reconnected['result']['handle'], start['result']['handle'])
            b._socket.sendall(b'\x00')
            for label, client in (('a', a), ('c', c)):
                response = client.call('process.start', 'approved', operation_id=label + '-start')
                self.assertEqual(response['state'], 'SUCCEEDED', response)
            time.sleep(.8)
        finally:
            a.close(); b.close(); c.close()
        replacement = LocalProcessClient(self.endpoint / '1.json')
        try:
            again = replacement.call('process.start', 'approved', operation_id=start['operation_id'])
            self.assertEqual(again['result']['handle'], start['result']['handle'])
        finally:
            replacement.close()

    def test_live_socket_rejects_v1_process_and_extra_authority_fields(self):
        from orch.bridge_local_process_client import LocalProcessClient
        from orch.bridge_local_protocol import send_frame, receive_frame
        self._launch()
        for variant in ('legacy', 'extra', 'unknown'):
            with self.subTest(variant=variant):
                client = LocalProcessClient(self.endpoint / '0.json')
                try:
                    op = client.new_operation_id()
                    request = {'schema_version': 2, 'request_id': 'r', 'session_id': client._session,
                               'operation_id': op, 'operation': 'process.start', 'target': 'approved',
                               'cursor': None, 'max_response_bytes': 4096}
                    if variant == 'legacy':
                        request = {'schema_version': 1, 'request_id': 'r', 'session_id': client._session,
                                   'operation': 'process.start', 'target': 'approved', 'max_response_bytes': 4096}
                    elif variant == 'extra':
                        request['executable'] = '/bin/sh'
                    else:
                        request['operation'] = 'process.exec'
                    send_frame(client._socket, request, client._limits)
                    response = receive_frame(client._socket, client._limits)
                    self.assertEqual((response['state'], response['error_code']),
                                     ('REFUSED', 'local_bridge_invalid_request'))
                finally:
                    client.close()
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 0)

    def test_cached_success_is_not_sent_after_authority_revocation(self):
        from unittest import mock
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        from orch.bridge_local_process_service import _ProcessTransport
        LocalProcessClient = _test_client(BaseProcessClient)
        original_write = _ProcessTransport.write
        selected = []
        capability = self.admissions[1][1]
        def write(transport, peer):
            if (selected and peer.request is not None and peer.request.get('operation_id') == selected[0]
                    and peer.route is not None and peer.route.context.binding == self.bindings[1]):
                capability.chmod(0o640)
                selected.clear()
            return original_write(transport, peer)
        with mock.patch.object(_ProcessTransport, 'write', write):
            self._launch('import time; time.sleep(20)')
            a, b, c = [LocalProcessClient(self.endpoint / (str(i) + '.json')) for i in range(3)]
            try:
                started = b.call('process.start', 'approved', operation_id='first')
                selected.append(started['operation_id'])
                denied = b.call('process.start', 'approved', operation_id=started['operation_id'])
                self.assertEqual(denied['error_code'], 'local_bridge_authority_denied')
                self.assertFalse(selected)
                for label, client in (('a', a), ('c', c)):
                    self.assertEqual(client.call('process.start', 'approved', operation_id=label)['state'], 'SUCCEEDED')
            finally:
                a.close(); b.close(); c.close()
                capability.chmod(0o600)

    def test_replaced_socket_and_credential_are_foreign_and_survive_cleanup(self):
        from orch.bridge_local_process_client import LocalProcessClient as BaseProcessClient
        LocalProcessClient = _test_client(BaseProcessClient)
        self._launch('import time; time.sleep(20)')
        client = LocalProcessClient(self.endpoint / '0.json')
        client._connect()
        socket_path, credential = self.endpoint / 'bridge.sock', self.endpoint / '1.json'
        socket_path.unlink(); credential.unlink()
        socket_path.write_bytes(b'foreign socket replacement')
        credential.write_bytes(b'foreign credential replacement')
        try:
            self.assertEqual(client.call('process.start', 'approved', operation_id='start')['state'], 'SUCCEEDED')
        finally:
            client.close()
        self.server.join(timeout=8)
        self.assertFalse(self.server.is_alive())
        self.assertEqual(socket_path.read_bytes(), b'foreign socket replacement')
        self.assertEqual(credential.read_bytes(), b'foreign credential replacement')

    def test_stop_after_spawn_reaps_owned_child(self):
        import sys
        import time
        from pathlib import Path
        from unittest import mock
        import orch.bridge_process as process_module
        from orch.bridge_local_broker import LocalRunBroker
        from orch.bridge_local_concurrent_service import _Peer, _Route
        from orch.bridge_local_process_protocol import sign_operation_id
        from orch.bridge_local_process_service import _ProcessTransport
        from orch.bridge_local_service import _DeferredStop, _ServiceStopped
        from orch.bridge_process import OwnedProcessProfile
        from orch.bridge_process_store import ProcessOperationStore
        from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger

        self._launch()
        binding, capability, root = self.admissions[0]
        ledger = ReadOnlyAuthorityLedger(self.orch.root)
        broker = LocalRunBroker(ledger, process_store=ProcessOperationStore.open_existing(ledger.root))
        stop = _DeferredStop()
        child = None
        try:
            profile = OwnedProcessProfile('approved', Path(sys.executable).resolve(),
                ('-c', 'import time; time.sleep(20)'), root, {'LANG': 'C'}, 30, 1024)
            context = broker.open_context(binding, capability, profiles=(profile,))
            token = 't' * 32
            route = _Route(token, context, 'unused')
            peer = _Peer(None, time.monotonic() + 30, route=route)
            transport = _ProcessTransport(None, None, (route,), 'i' * 32,
                LocalServiceLimits(), None, {}, stop)
            request = {'schema_version': 2, 'request_id': 'request-1',
                'session_id': context.session_id,
                'operation_id': sign_operation_id(token, context.session_id, 'a' * 64),
                'operation': 'process.start', 'target': 'approved', 'cursor': None,
                'max_response_bytes': 4096}
            original_spawn = process_module._spawn_process
            def spawn_then_stop(profile):
                nonlocal child
                child = original_spawn(profile)
                stop(None, None)
                return child
            with mock.patch.object(process_module, '_spawn_process', spawn_then_stop):
                with self.assertRaises(_ServiceStopped):
                    transport.dispatch(peer, request)
            broker.close()
            self.assertIsNotNone(child)
            self.assertIsNotNone(child.poll(), 'service cleanup left its spawned child alive')
            self.assertTrue(child.stdout.closed, 'service cleanup left its child stream open')
        finally:
            broker.close()
            if child is not None:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if child.stdout is not None and not child.stdout.closed:
                    child.stdout.close()
