"""B10 native lifecycle: exact claims and durable non-replaying checkpoints."""
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from orch.core import Orchestrator, canonical_json
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry


class _FixtureOrchestrator(Orchestrator):
    @contextmanager
    def connect(self):
        # sqlite3's transaction context manager does not close the connection.
        with closing(super().connect()) as conn:
            with conn:
                yield conn


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='orch-b10-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / 'repo'
        env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        def git(*args):
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], env=env,
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
        git('init', '-q', '-b', 'main', str(self.repo))
        git('-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid')
        git('-C', str(self.repo), 'config', 'user.name', 'Fixture')
        (self.repo / 'README.md').write_text('fixture\n')
        git('-C', str(self.repo), 'add', 'README.md')
        git('-C', str(self.repo), 'commit', '-qm', 'root')
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.orch = _FixtureOrchestrator(self.home)
        self.project = ProjectRegistry(self.home).add(self.repo, name='B10 disposable', profile='safe',
                            review_mode='off', allow_commit=True, allow_push=False)['project']
        for suffix in ('A', 'B'):
            plan = build_single_task_plan(self.project, task_id='B10-' + suffix,
                    goal='Lifecycle fixture', allowed_paths=['result.txt'], plan_revision='b10-' + suffix)
            path = self.base / ('plan-' + suffix + '.json')
            write_plan(path, plan)
            self.orch.load_plan(path)

    def _spec(self, task='B10-B', worker='worker'):
        from orch.bridge_worker_lifecycle import LifecycleGrantSpec
        with self.orch.connect() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone()
        return LifecycleGrantSpec(row['project_id'], row['writer_key'], task, row['plan_revision'],
            hashlib.sha256(canonical_json(json.loads(row['payload_json'])).encode()).hexdigest(), 1, worker)

    def _durability_fault(self, fault, fail_on=1):
        """Wrap real writer connections while faulting one durability gate."""
        import sqlite3
        from contextlib import contextmanager
        from unittest import mock
        from orch.bridge_worker_lifecycle import _LifecycleConnection
        original = sqlite3.connect
        opened = []
        statements = []

        class RowResult:
            def __init__(self, row):
                self.row = row
            def fetchone(self):
                return self.row

        class Instrumented(_LifecycleConnection):
            def execute(conn, sql, *args):
                statements.append((conn, sql))
                selected = len(opened) == fail_on and conn is opened[-1]
                if selected and sql == 'PRAGMA synchronous=FULL' and fault == 'setter':
                    raise sqlite3.OperationalError('injected setter failure')
                if selected and sql == 'PRAGMA synchronous':
                    if fault == 'getter':
                        raise sqlite3.OperationalError('injected getter failure')
                    if fault in ('zero', 'normal', 'missing', 'malformed', 'float'):
                        return RowResult({'zero': (0,), 'normal': (1,),
                                          'missing': None, 'malformed': ('2',), 'float': (2.0,)}[fault])
                return super().execute(sql, *args)

        def connect(*args, **kwargs):
            if kwargs.get('factory') is _LifecycleConnection:
                kwargs['factory'] = Instrumented
                conn = original(*args, **kwargs)
                sqlite3.Connection.execute(conn, 'PRAGMA synchronous=NORMAL')
                opened.append(conn)
                return conn
            return original(*args, **kwargs)

        @contextmanager
        def inject():
            with mock.patch('orch.bridge_worker_lifecycle.sqlite3.connect', side_effect=connect):
                yield opened, statements
        return inject()

    def _durability_state(self):
        """SELECT-only ledger and operator-owned directory content snapshot."""
        import sqlite3
        conn = sqlite3.connect(self.orch.db_path.as_uri() + '?mode=ro', uri=True)
        try:
            tables = {name: sorted(tuple(row) for row in conn.execute('SELECT * FROM ' + name))
                      for name in ('tasks', 'runs', 'events', 'settings')}
        finally:
            conn.close()
        root = self.orch.runtime
        directories = [('.', root.stat().st_mode & 0o777, None)]
        for path in sorted(root.rglob('*')):
            # SQLite may create or remove its transient WAL files on read-only opens;
            # logical database content is compared by SELECT above.
            if path.parent == root and path.name in ('orch.sqlite3', 'orch.sqlite3-wal', 'orch.sqlite3-shm'):
                continue
            directories.append((str(path.relative_to(root)), path.stat().st_mode & 0o777,
                                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None))
        return tables, directories

    def _assert_failed_gate(self, opened, statements, expected_connections):
        import sqlite3
        self.assertEqual(len(opened), expected_connections)
        failed = opened[-1]
        self.assertFalse(any(sql.upper().startswith('BEGIN') for conn, sql in statements if conn is failed))
        with self.assertRaises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(failed, 'SELECT 1')

    def test_wal_writer_sets_full_on_each_fresh_connection_and_remains_usable(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        writer = authority._LifecycleAuthority__orch
        with self._durability_fault(None) as (opened, statements):
            for _ in range(2):
                with writer.connect() as conn:
                    self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
                    self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0], 2)
            grant = authority.create_grant(self._spec())
            route = authority.open_route(grant)
            self.assertEqual(route.claim('full-claim').state, 'SUCCEEDED')
            self.assertEqual(route.checkpoint('full-checkpoint', 'progress', 'active').state, 'SUCCEEDED')
            route.close()
        self.assertGreater(len(opened), 2)

    def test_durability_faults_close_before_begin_at_connection_boundary(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        writer = authority._LifecycleAuthority__orch
        before = self._durability_state()
        for fault in ('setter', 'getter', 'zero', 'normal', 'missing', 'malformed', 'float'):
            with self.subTest(fault=fault):
                with self._durability_fault(fault) as (opened, statements):
                    with self.assertRaisesRegex(ValueError, '^lifecycle_ledger_synchronous$'):
                        writer.connect()
                self._assert_failed_gate(opened, statements, 1)
                self.assertEqual(self._durability_state(), before)

    def test_durability_faults_fail_closed_for_grant_and_claim(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        spec = self._spec()
        for fault in ('setter', 'getter', 'zero', 'normal', 'missing', 'malformed', 'float'):
            with self.subTest(stage='grant', fault=fault):
                before = self._durability_state()
                with self._durability_fault(fault) as (opened, statements):
                    with self.assertRaisesRegex(ValueError, '^lifecycle_ledger_synchronous$'):
                        authority.create_grant(spec)
                self._assert_failed_gate(opened, statements, 1)
                self.assertEqual(self._durability_state(), before)
        grant = authority.create_grant(spec)
        route = authority.open_route(grant)
        for fault in ('setter', 'getter', 'zero', 'normal', 'missing', 'malformed', 'float'):
            with self.subTest(stage='claim', fault=fault):
                before = self._durability_state()
                # The first connection validates status; the second reserves the claim.
                with self._durability_fault(fault, fail_on=2) as (opened, statements):
                    self.assertEqual(route.claim('fault-claim').state, 'UNKNOWN')
                self._assert_failed_gate(opened, statements, 2)
                self.assertEqual(self._durability_state(), before)
                self.assertEqual(route.disposition('fault-claim').state, 'UNKNOWN')
        route.close()

    def test_durability_faults_fail_closed_for_checkpoint(self):
        authority, grant, route, claim = self._ready()
        for fault in ('setter', 'getter', 'zero', 'normal', 'missing', 'malformed', 'float'):
            with self.subTest(stage='checkpoint', fault=fault):
                before = self._durability_state()
                # The first connection validates status; the second reserves the checkpoint.
                with self._durability_fault(fault, fail_on=2) as (opened, statements):
                    self.assertEqual(route.checkpoint('fault-checkpoint', 'progress', 'active').state, 'UNKNOWN')
                self._assert_failed_gate(opened, statements, 2)
                self.assertEqual(self._durability_state(), before)
                self.assertEqual(route.disposition('fault-checkpoint').state, 'UNKNOWN')
        route.close()

    def test_lifecycle_module_exists_and_exact_claim_does_not_choose_queue_head(self):
        self.assertIsNotNone(importlib.util.find_spec('orch.bridge_worker_lifecycle'), 'native lifecycle module is missing')
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        result = route.claim('claim-1')
        self.assertEqual(result.state, 'SUCCEEDED')
        with self.orch.connect() as conn:
            runs = conn.execute('SELECT task_id,worker_id FROM runs').fetchall()
        self.assertEqual([(r['task_id'], r['worker_id']) for r in runs], [('B10-B', 'worker')])
        self.assertEqual(route.claim('claim-1'), result)
        self.assertEqual(route.claim('claim-2').state, 'REFUSED')
        route.close()

    def _ready(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        claim = route.claim('claim')
        self.assertEqual(claim.state, 'SUCCEEDED', claim)
        return authority, grant, route, claim

    def _events(self):
        with self.orch.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM events WHERE kind='WORKER_CHECKPOINT_RECORDED'").fetchone()[0]

    def test_checkpoint_dedupes_conflicts_and_retains_writer(self):
        authority, grant, route, claim = self._ready()
        first = route.checkpoint('checkpoint', '  progress  ', 'active')
        self.assertEqual(first.state, 'SUCCEEDED')
        self.assertEqual(route.checkpoint('checkpoint', 'progress', 'active'), first)
        self.assertEqual(route.checkpoint('checkpoint', 'other', 'active').state, 'REFUSED')
        self.assertEqual(route.checkpoint('claim', 'progress', 'active').state, 'REFUSED')
        self.assertEqual(route.disposition('checkpoint'), first)
        self.assertEqual(self._events(), 1)
        with self.orch.connect() as conn:
            row = conn.execute('SELECT r.state,t.status FROM runs r JOIN tasks t ON t.task_id=r.task_id WHERE r.run_id=?', (claim.run_id,)).fetchone()
        self.assertEqual(tuple(row), ('RUNNING', 'IN_PROGRESS'))
        self.assertTrue(self.orch._capability_path(claim.run_id).is_file())
        route.close()

    def test_capability_creation_failure_is_unknown_retains_writer_and_never_replays(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        with mock.patch('orch.bridge_worker_lifecycle._publish_capability', side_effect=OSError('private')):
            claim = route.claim('claim')
        self.assertEqual(claim.state, 'UNKNOWN')
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(route.claim('another').state, 'REFUSED')
        with self.orch.connect() as conn:
            rows = conn.execute('SELECT state,task_id FROM runs').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('RUNNING', 'B10-B')])
        route.close()
        resumed = authority.open_route(grant)
        resumed.status()
        self.assertEqual(resumed.disposition('claim').state, 'UNKNOWN')
        self.assertEqual(resumed.claim('claim').state, 'UNKNOWN')
        with self.assertRaises(ValueError):
            resumed.context(65536)
        resumed.close()

    def test_admission_failure_after_capability_stays_unknown(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        with mock.patch('orch.bridge_worker_lifecycle.LocalRunSessionAuthority.open_session', side_effect=ValueError('private')):
            self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            row = conn.execute('SELECT run_id,state FROM runs').fetchone()
        self.assertEqual(row['state'], 'RUNNING')
        self.assertTrue(self.orch._capability_path(row['run_id']).is_file())
        route.close()
        reopened = authority.open_route(grant)
        self.assertEqual(reopened.claim('claim').state, 'UNKNOWN')
        reopened.close()

    def test_reopen_rejects_record_and_native_attempt_different_from_grant(self):
        authority, grant, route, claim = self._ready()
        key = 'b10:v1:op:' + grant + ':claim'
        with self.orch.connect() as conn:
            raw = conn.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()[0]
            record = json.loads(raw)
            record['attempt'] = 2
            conn.execute('UPDATE settings SET value_json=? WHERE key=?', (canonical_json(record), key))
            conn.execute('UPDATE runs SET attempt=2 WHERE run_id=?', (claim.run_id,))
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        route.close()
        with self.assertRaises(ValueError):
            authority.open_route(grant)
        record['phase'] = 'PENDING'
        with self.orch.connect() as conn:
            conn.execute('UPDATE settings SET value_json=? WHERE key=?', (canonical_json(record), key))
        with self.assertRaises(ValueError):
            authority.open_route(grant)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT state FROM runs WHERE run_id=?', (claim.run_id,)).fetchone()[0], 'RUNNING')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)

    def test_attempt_identity_mismatches_fail_closed_across_status_and_disposition(self):
        from dataclasses import replace
        authority, grant, route, claim = self._ready()
        self.assertEqual(route.checkpoint('check', 'progress', 'active').state, 'SUCCEEDED')
        grant_key = 'b10:v1:grant:' + grant
        claim_key = 'b10:v1:op:' + grant + ':claim'
        check_key = 'b10:v1:op:' + grant + ':check'
        with self.orch.connect() as conn:
            originals = {key: conn.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()[0]
                         for key in (grant_key, claim_key, check_key)}
        binding = route._LifecycleRoute__binding
        def restore():
            with self.orch.connect() as conn:
                for key, value in originals.items():
                    conn.execute('UPDATE settings SET value_json=? WHERE key=?', (value, key))
                conn.execute('UPDATE runs SET attempt=1 WHERE run_id=?', (claim.run_id,))
            route._LifecycleRoute__binding = binding
        self.addCleanup(restore)
        for case in ('grant', 'claim', 'native', 'binding', 'checkpoint'):
            with self.subTest(case=case):
                restore()
                with self.orch.connect() as conn:
                    if case in ('grant', 'claim', 'checkpoint'):
                        key = {'grant': grant_key, 'claim': claim_key, 'checkpoint': check_key}[case]
                        record = json.loads(originals[key])
                        if case == 'grant':
                            record['spec']['attempt'] = 2
                        else:
                            record['attempt'] = 2
                        conn.execute('UPDATE settings SET value_json=? WHERE key=?', (canonical_json(record), key))
                    elif case == 'native':
                        conn.execute('UPDATE runs SET attempt=2 WHERE run_id=?', (claim.run_id,))
                    else:
                        route._LifecycleRoute__binding = replace(binding, attempt=2)
                if case != 'checkpoint':
                    with self.assertRaises(ValueError):
                        route.status()
                    self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
                    with self.assertRaises(ValueError):
                        authority.open_route(grant) if case != 'binding' else route.status()
                self.assertEqual(route.disposition('check').state, 'UNKNOWN')
        restore()
        record = json.loads(originals[claim_key])
        record['phase'] = 'PENDING'
        with self.orch.connect() as conn:
            conn.execute('UPDATE settings SET value_json=? WHERE key=?', (canonical_json(record), claim_key))
            conn.execute('UPDATE runs SET attempt=2 WHERE run_id=?', (claim.run_id,))
        with self.assertRaises(ValueError):
            authority.open_route(grant)
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        restore()
        self.assertEqual(route.disposition('check').state, 'SUCCEEDED')
        route.close()

    def test_reopen_ready_requires_live_capability(self):
        authority, grant, route, claim = self._ready()
        self.assertEqual(route.context(65536)['run_id'], claim.run_id)
        route.close()
        reopened = authority.open_route(grant)
        self.assertEqual(reopened.disposition('claim').state, 'SUCCEEDED')
        reopened.close()
        self.orch._capability_path(claim.run_id).unlink()
        with self.assertRaises(ValueError):
            authority.open_route(grant)

    def test_stale_spec_refuses_without_claiming_another_task(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        with self.orch.connect() as conn:
            conn.execute("UPDATE tasks SET plan_revision='changed' WHERE task_id='B10-B'")
        self.assertEqual(route.claim('claim').state, 'REFUSED')
        self.assertEqual(route.claim('claim').state, 'REFUSED')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM tasks WHERE task_id='B10-A'").fetchone()[0], 'PLANNED')
        route.close()

    def test_revoked_and_competing_grants_fail_closed(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        revoked = authority.create_grant(self._spec())
        authority.revoke_grant(revoked)
        with self.assertRaises(ValueError):
            authority.open_route(revoked)
        first = authority.create_grant(self._spec())
        second = authority.create_grant(self._spec())
        r1, r2 = authority.open_route(first), authority.open_route(second)
        self.assertEqual(r1.claim('first').state, 'SUCCEEDED')
        self.assertEqual(r2.claim('second').state, 'REFUSED')
        self.assertEqual(r2.disposition('first').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)
        r1.close(); r2.close()

    def test_checkpoint_concurrent_duplicate_has_one_event(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        authority, grant, route, claim = self._ready()
        route2 = authority.open_route(grant)
        barrier = threading.Barrier(2)
        def invoke(r):
            barrier.wait(timeout=5)
            return r.checkpoint('same', 'concurrent', 'unknown')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, (route, route2)))
        self.assertIn('SUCCEEDED', [x.state for x in results])
        self.assertTrue(all(x.state in {'SUCCEEDED', 'UNKNOWN'} for x in results))
        self.assertEqual(route.disposition('same').state, 'SUCCEEDED')
        self.assertEqual(self._events(), 1)
        route.close(); route2.close()

    def test_checkpoint_transaction_revalidates_task_and_rolls_back(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleRoute
        authority, grant, route, claim = self._ready()
        original = LifecycleRoute._LifecycleRoute__validate_native
        def invalidate(instance, conn, spec, binding, cap):
            conn.execute("UPDATE tasks SET status='BLOCKED' WHERE task_id=?", (spec.task_id,))
            return original(instance, conn, spec, binding, cap)
        with mock.patch.object(LifecycleRoute, '_LifecycleRoute__validate_native', invalidate):
            self.assertEqual(route.checkpoint('race', 'test', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 0)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM tasks WHERE task_id='B10-B'").fetchone()[0], 'IN_PROGRESS')
        self.assertEqual(route.disposition('race').state, 'UNKNOWN')
        route.close()

    def test_corrupt_operation_evidence_never_replays(self):
        authority, grant, route, claim = self._ready()
        self.assertEqual(route.checkpoint('done', 'test', 'active').state, 'SUCCEEDED')
        with self.orch.connect() as conn:
            conn.execute('UPDATE settings SET value_json=? WHERE key=?', ('{"version":99}', 'b10:v1:op:' + grant + ':done'))
        self.assertEqual(route.disposition('done').state, 'UNKNOWN')
        self.assertEqual(route.checkpoint('done', 'test', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 1)
        route.close()

    def test_invalid_checkpoint_and_budget_are_refused(self):
        authority, grant, route, claim = self._ready()
        for reason, state in [('', 'active'), ('x' * 501, 'active'), ('ok', 'inactive'), ('ok', None), ('ok', [])]:
            with self.subTest(reason=reason, state=state):
                self.assertEqual(route.checkpoint('invalid', reason, state).state, 'REFUSED')
        for budget in (0, 65537, True, '100'):
            with self.assertRaises(ValueError):
                route.context(budget)
        self.assertEqual(self._events(), 0)
        route.close()

    def test_checkpoint_limit_refuses_without_effect(self):
        authority, grant, route, claim = self._ready()
        for n in range(32):
            self.assertEqual(route.checkpoint('c' + str(n), 'test', 'active').state, 'SUCCEEDED')
        self.assertEqual(route.checkpoint('excess', 'test', 'active').state, 'REFUSED')
        self.assertEqual(self._events(), 32)
        route.close()

    def test_open_existing_authority_does_not_initialize_or_repair_schema(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        with self.orch.connect() as conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=4')
        LifecycleAuthority(self.home)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=4').fetchone()[0], 0)

    def test_failed_commit_to_ready_retains_pending_without_replay(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleAuthority, LifecycleRoute
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        with mock.patch.object(LifecycleRoute, '_LifecycleRoute__validate_native', side_effect=ValueError('simulated')):
            self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        route.close()
        retry = authority.open_route(grant)
        self.assertEqual(retry.claim('claim').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            rows = conn.execute('SELECT state FROM runs').fetchall()
        self.assertEqual([r[0] for r in rows], ['RUNNING'])
        retry.close()

    def test_live_authority_is_revalidated_after_context_and_commit(self):
        from unittest import mock
        authority, grant, route, claim = self._ready()
        original = Orchestrator.context
        def revoke_after_context(orch, run_id):
            result = original(orch, run_id)
            orch._capability_path(run_id).unlink()
            return result
        with mock.patch.object(Orchestrator, 'context', revoke_after_context):
            with self.assertRaises(ValueError):
                route.context(65536)
        self.assertEqual(route.checkpoint('x', 'reason', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 0)
        route.close()

    def test_inconsistent_claim_record_is_unknown_not_a_success(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        record = dict(version=1, grant_id=grant, operation_id='claim', kind='claim',
                      fingerprint=hashlib.sha256(canonical_json({'kind':'claim'}).encode()).hexdigest(),
                      phase='READY', run_id='forged', attempt=1, code='lifecycle_claimed')
        with self.orch.connect() as conn:
            conn.execute('INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)',
                         ('b10:v1:op:' + grant + ':claim', canonical_json(record), 'fixture'))
        self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        route.close()

    def test_checkpoint_lost_postcommit_authority_returns_unknown_without_replay(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleRoute
        authority, grant, route, claim = self._ready()
        original = LifecycleRoute.status
        calls = []
        def revoke_on_reply(instance):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('simulated lost authority')
            return original(instance)
        with mock.patch.object(LifecycleRoute, 'status', revoke_on_reply):
            self.assertEqual(route.checkpoint('cp', 'progress', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 1)
        self.assertEqual(route.disposition('cp').state, 'SUCCEEDED')
        self.assertEqual(route.checkpoint('cp', 'progress', 'active').state, 'SUCCEEDED')
        self.assertEqual(self._events(), 1)
        route.close()

    def test_global_pause_consumes_grant_without_run(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        with self.orch.connect() as conn:
            conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES('paused','{}','fixture')")
        result = route.claim('claim')
        self.assertEqual(result.state, 'REFUSED')
        with self.orch.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key='paused'")
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        self.assertEqual(route.claim('claim'), result)
        self.assertEqual(route.claim('new').state, 'REFUSED')
        route.close()

    def test_precommit_exception_is_not_reexecuted_on_same_id(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        original = Orchestrator._event
        def fail_claim(orch, conn, kind, **kwargs):
            if kind == 'RUN_CLAIMED':
                raise OSError('simulated precommit failure')
            return original(orch, conn, kind, **kwargs)
        with mock.patch.object(Orchestrator, '_event', fail_claim):
            self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(route.claim('different').state, 'REFUSED')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        route.close()

    def test_checkpoint_precommit_exception_is_not_reexecuted_on_same_id(self):
        from unittest import mock
        authority, grant, route, claim = self._ready()
        original = Orchestrator._event
        def fail_checkpoint(orch, conn, kind, **kwargs):
            if kind == 'WORKER_CHECKPOINT_RECORDED':
                raise OSError('simulated precommit failure')
            return original(orch, conn, kind, **kwargs)
        with mock.patch.object(Orchestrator, '_event', fail_checkpoint):
            self.assertEqual(route.checkpoint('cp', 'progress', 'active').state, 'UNKNOWN')
        self.assertEqual(route.disposition('cp').state, 'UNKNOWN')
        self.assertEqual(route.checkpoint('cp', 'progress', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 0)
        route.close()

    def test_concurrent_claims_one_writer_at_most_one_run(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grants = [authority.create_grant(self._spec()) for _ in range(2)]
        routes = [authority.open_route(g) for g in grants]
        barrier = threading.Barrier(2)
        def attempt(index):
            barrier.wait(timeout=5)
            return routes[index].claim('claim-' + str(index))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(result.state for result in results), ['REFUSED', 'SUCCEEDED'])
        with self.orch.connect() as conn:
            rows = conn.execute('SELECT task_id,state FROM runs').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('B10-B', 'RUNNING')])
        for route in routes:
            route.close()

    def test_existing_capability_is_not_overwritten_or_adopted(self):
        from types import SimpleNamespace
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        artifact = self.orch._capability_path('B10-B-A1-' + 'a' * 10)
        artifact.write_bytes(b'pre-existing artifact')
        artifact.chmod(0o600)
        with mock.patch('orch.bridge_worker_lifecycle.uuid.uuid4', return_value=SimpleNamespace(hex='a' * 32)):
            self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(artifact.read_bytes(), b'pre-existing artifact')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT state FROM runs').fetchone()[0], 'RUNNING')
        self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        route.close()

    def test_grant_capacity_refuses_without_deleting_records(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        with self.orch.connect() as conn:
            conn.executemany('INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)',
                [('b10:v1:grant:' + format(n, '032x'), '{}', 'fixture') for n in range(4096)])
        with self.assertRaises(ValueError):
            authority.create_grant(self._spec())
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'b10:v1:grant:%'").fetchone()[0], 4096)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_repeated_claim_revalidates_authority_before_success(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleRoute
        authority, grant, route, claim = self._ready()
        original = LifecycleRoute.status
        calls = []
        def fail_after_lookup(instance):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('authority changed')
            return original(instance)
        with mock.patch.object(LifecycleRoute, 'status', fail_after_lookup):
            self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        route.close()

    def test_missing_database_after_admission_is_not_recreated(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        db_path = self.orch.db_path
        moved = db_path.with_name('offline.sqlite3')
        db_path.rename(moved)
        self.assertEqual(route.claim('unavailable').state, 'UNKNOWN')
        self.assertFalse(db_path.exists())
        self.assertTrue(moved.exists())
        route.close()

    def test_unsafe_database_mode_is_not_repaired_by_lifecycle(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        db_path = self.orch.db_path
        db_path.chmod(0o644)
        self.assertEqual(route.claim('unavailable').state, 'UNKNOWN')
        self.assertEqual(db_path.stat().st_mode & 0o777, 0o644)
        db_path.chmod(0o600)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        route.close()

    def test_unavailable_database_after_reservation_never_replays(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        original = Orchestrator._event
        def lose_database(orch, conn, kind, **kwargs):
            if kind == 'RUN_CLAIMED':
                orch.db_path.chmod(0o644)
                raise OSError('storage became unsafe')
            return original(orch, conn, kind, **kwargs)
        with mock.patch.object(Orchestrator, '_event', lose_database):
            self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        self.assertEqual(self.orch.db_path.stat().st_mode & 0o777, 0o644)
        self.orch.db_path.chmod(0o600)
        self.assertEqual(route.disposition('claim').state, 'UNKNOWN')
        self.assertEqual(route.claim('claim').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        route.close()
        reopened = authority.open_route(grant)
        self.assertEqual(reopened.claim('claim').state, 'UNKNOWN')
        reopened.close()

    def test_non_wal_database_is_not_repaired(self):
        import sqlite3
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        conn = sqlite3.connect(str(self.orch.db_path), isolation_level=None)
        try:
            self.assertEqual(conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
            self.assertEqual(route.claim('claim').state, 'UNKNOWN')
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        finally:
            conn.close()
        route.close()

    def test_dependencies_max_attempts_and_dirty_workspace_block_exact_claim(self):
        from dataclasses import replace
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        with self.orch.connect() as conn:
            row = conn.execute("SELECT payload_json FROM tasks WHERE task_id='B10-B'").fetchone()
            payload = json.loads(row[0]); payload['dependencies'] = ['B10-A']
            conn.execute("UPDATE tasks SET payload_json=? WHERE task_id='B10-B'", (canonical_json(payload),))
        authority = LifecycleAuthority(self.home)
        route = authority.open_route(authority.create_grant(self._spec()))
        self.assertEqual(route.claim('dependencies').state, 'REFUSED')
        route.close()
        with self.orch.connect() as conn:
            payload['dependencies'] = []; payload['max_attempts'] = 1
            conn.execute("UPDATE tasks SET payload_json=? WHERE task_id='B10-B'", (canonical_json(payload),))
            conn.execute("INSERT INTO runs(run_id,task_id,attempt,worker_id,state,lease_token,started_at,heartbeat_at) VALUES('prior','B10-B',1,'w','ABORTED','token','t','t')")
        route = authority.open_route(authority.create_grant(replace(self._spec(), attempt=2)))
        self.assertEqual(route.claim('attempts').state, 'REFUSED')
        route.close()
        with self.orch.connect() as conn:
            payload['max_attempts'] = 2
            conn.execute("UPDATE tasks SET payload_json=? WHERE task_id='B10-B'", (canonical_json(payload),))
        (self.repo / 'unlisted-dirty.txt').write_text('dirty')
        route = authority.open_route(authority.create_grant(replace(self._spec(), attempt=2)))
        self.assertEqual(route.claim('workspace').state, 'REFUSED')
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM tasks WHERE task_id='B10-A'").fetchone()[0], 'PLANNED')
        route.close()

    def test_context_exact_native_limit_and_no_lease_in_result(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        head = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()
        with self.orch.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id='B10-B'").fetchone()
            payload = json.loads(row['payload_json'])
            sample = self.orch._build_context_pack(conn, row, payload,
                run_id='B10-B-A1-' + 'a' * 10, attempt=1, claim_git_head=head, run_persisted=False)
            needed = 32768 - len(canonical_json(sample).encode())
            self.assertGreater(needed, 0)
            payload['goal'] += 'x' * needed
            conn.execute("UPDATE tasks SET payload_json=? WHERE task_id='B10-B'", (canonical_json(payload),))
        authority = LifecycleAuthority(self.home)
        route = authority.open_route(authority.create_grant(self._spec()))
        claim = route.claim('claim')
        self.assertEqual(claim.state, 'SUCCEEDED')
        context = route.context(65536)
        self.assertEqual(len(canonical_json(context).encode()), 32768)
        lease = json.loads(self.orch._capability_path(claim.run_id).read_text())['lease_token']
        self.assertNotIn(lease, canonical_json(context))
        self.assertNotIn(lease, str(claim))
        with self.orch.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id='B10-B'").fetchone()
            payload = json.loads(row['payload_json']); payload['goal'] += 'x'
            with self.assertRaisesRegex(ValueError, 'context_pack_too_large'):
                self.orch._build_context_pack(conn, row, payload, run_id=claim.run_id,
                    attempt=1, claim_git_head=head, run_persisted=True)
        route.close()

    def test_claim_preserves_missing_unsafe_and_nonempty_hooks_directory(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        for case in ('missing', 'unsafe', 'nonempty'):
            with self.subTest(case=case):
                fixture = LifecycleTests('test_lifecycle_module_exists_and_exact_claim_does_not_choose_queue_head')
                fixture.setUp()
                try:
                    directory = fixture.home / '.runtime' / 'git-hooks-disabled'
                    authority = LifecycleAuthority(fixture.home)
                    route = authority.open_route(authority.create_grant(fixture._spec()))
                    self.addCleanup(route.close)
                    if case == 'missing':
                        directory.rename(directory.with_name('hooks-offline'))
                    elif case == 'unsafe':
                        directory.chmod(0o755)
                    else:
                        (directory / 'sentinel').write_bytes(b'keep exactly')
                    before = None if case == 'missing' else (directory.stat().st_ino, directory.stat().st_mode,
                             sorted((p.name, p.read_bytes()) for p in directory.iterdir()))
                    result = route.claim('claim-' + case)
                    after = None if not directory.exists() else (directory.stat().st_ino, directory.stat().st_mode,
                            sorted((p.name, p.read_bytes()) for p in directory.iterdir()))
                    self.assertEqual(after, before)
                    self.assertEqual(result.state, 'REFUSED')
                    with fixture.orch.connect() as conn:
                        self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
                    route.close()
                finally:
                    fixture.doCleanups()

    def test_lifecycle_does_not_create_or_repair_receipt_directory(self):
        from orch.bridge_worker_lifecycle import LifecycleAuthority
        authority = LifecycleAuthority(self.home)
        grant = authority.create_grant(self._spec())
        route = authority.open_route(grant)
        directory = self.home / '.runtime' / 'worker_receipts'
        directory.chmod(0o755)
        self.assertEqual(route.claim('unsafe').state, 'REFUSED')
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        directory.chmod(0o700)
        route.close()
        authority2 = LifecycleAuthority(self.home)
        route2 = authority2.open_route(authority2.create_grant(self._spec()))
        moved = directory.with_name('receipts-offline')
        directory.rename(moved)
        self.assertEqual(route2.claim('missing').state, 'REFUSED')
        self.assertFalse(directory.exists())
        self.assertTrue(moved.exists())
        route2.close()

    def test_checkpoint_commit_rechecks_latest_attempt_and_writer_exclusivity(self):
        from unittest import mock
        from orch.bridge_worker_lifecycle import LifecycleRoute
        authority, grant, route, claim = self._ready()
        original = LifecycleRoute._LifecycleRoute__validate_native
        for label in ('latest', 'writer'):
            calls = []
            def invalidate(instance, conn, spec, binding, cap):
                calls.append(1)
                if len(calls) == 2:
                    task, attempt, state = ('B10-B', 2, 'ABORTED') if label == 'latest' else ('B10-A', 1, 'RUNNING')
                    conn.execute('INSERT INTO runs(run_id,task_id,attempt,worker_id,state,lease_token,started_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?)',
                                 ('intruder', task, attempt, 'other', state, 'test', 't', 't'))
                return original(instance, conn, spec, binding, cap)
            with self.subTest(label=label), mock.patch.object(LifecycleRoute, '_LifecycleRoute__validate_native', invalidate):
                self.assertEqual(route.checkpoint(label, 'progress', 'active').state, 'UNKNOWN')
                self.assertEqual(len(calls), 2)
                self.assertEqual(route.disposition(label).state, 'UNKNOWN')
                self.assertEqual(self._events(), 0)
        route.close()

    def test_replaced_capability_cannot_authorize_checkpoint(self):
        authority, grant, route, claim = self._ready()
        cap = self.orch._capability_path(claim.run_id)
        original = cap.read_bytes()
        cap.write_text(canonical_json({'run_id': 'other-attempt', 'lease_token': 'invalid'}))
        self.assertEqual(route.checkpoint('cp', 'test', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 0)
        cap.write_bytes(original)
        route.close()

    def test_deleted_committed_checkpoint_never_replays_even_after_readmission(self):
        authority, grant, route, claim = self._ready()
        self.assertEqual(route.checkpoint('once', 'progress', 'active').state, 'SUCCEEDED')
        with self.orch.connect() as conn:
            conn.execute('DELETE FROM settings WHERE key=?', ('b10:v1:op:' + grant + ':once',))
        self.assertEqual(route.disposition('once').state, 'UNKNOWN')
        self.assertEqual(route.checkpoint('once', 'progress', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 1)
        route.close()
        reopened = authority.open_route(grant)
        self.assertEqual(reopened.disposition('once').state, 'UNKNOWN')
        self.assertEqual(reopened.checkpoint('once', 'progress', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 1)
        reopened.close()

    def test_deleted_pending_checkpoint_never_replays_after_readmission(self):
        from unittest import mock
        authority, grant, route, claim = self._ready()
        original = Orchestrator._event
        def fail(orch, conn, kind, **kwargs):
            if kind == 'WORKER_CHECKPOINT_RECORDED':
                raise OSError('failure before commit')
            return original(orch, conn, kind, **kwargs)
        with mock.patch.object(Orchestrator, '_event', fail):
            self.assertEqual(route.checkpoint('pending', 'progress', 'active').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            conn.execute('DELETE FROM settings WHERE key=?', ('b10:v1:op:' + grant + ':pending',))
        self.assertEqual(route.disposition('pending').state, 'UNKNOWN')
        self.assertEqual(route.checkpoint('pending', 'progress', 'active').state, 'UNKNOWN')
        route.close()
        reopened = authority.open_route(grant)
        self.assertEqual(reopened.checkpoint('pending', 'progress', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 0)
        reopened.close()

    def test_deleted_checkpoint_does_not_restore_quota(self):
        authority, grant, route, claim = self._ready()
        for n in range(32):
            self.assertEqual(route.checkpoint('quota-' + str(n), 'progress', 'active').state, 'SUCCEEDED')
        with self.orch.connect() as conn:
            conn.execute('DELETE FROM settings WHERE key=?', ('b10:v1:op:' + grant + ':quota-0',))
        self.assertEqual(route.checkpoint('overflow', 'progress', 'active').state, 'REFUSED')
        self.assertEqual(self._events(), 32)
        route.close()

    def test_checkpoint_record_without_anchor_is_unknown_and_not_replayed(self):
        authority, grant, route, claim = self._ready()
        self.assertEqual(route.checkpoint('orphan', 'progress', 'active').state, 'SUCCEEDED')
        key = 'b10:v1:grant:' + grant
        with self.orch.connect() as conn:
            raw = conn.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()[0]
            obj = json.loads(raw); obj['checkpoint_ids'] = []
            conn.execute('UPDATE settings SET value_json=? WHERE key=?', (canonical_json(obj), key))
        self.assertEqual(route.disposition('orphan').state, 'UNKNOWN')
        self.assertEqual(route.checkpoint('orphan', 'progress', 'active').state, 'UNKNOWN')
        self.assertEqual(route.checkpoint('orphan', 'changed', 'active').state, 'UNKNOWN')
        self.assertEqual(self._events(), 1)
        route.close()

    def test_malformed_checkpoint_anchors_fail_closed(self):
        authority, grant, route, claim = self._ready()
        self.assertEqual(route.checkpoint('recorded', 'progress', 'active').state, 'SUCCEEDED')
        key = 'b10:v1:grant:' + grant
        with self.orch.connect() as conn:
            raw = conn.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()[0]
        for anchors in ('not-a-list', ['bad'], ['a' * 64] * 2, [format(n, '064x') for n in range(33)]):
            with self.subTest(anchors=anchors):
                obj = json.loads(raw); obj['checkpoint_ids'] = anchors
                with self.orch.connect() as conn:
                    conn.execute('UPDATE settings SET value_json=? WHERE key=?', (canonical_json(obj), key))
                self.assertEqual(route.disposition('recorded').state, 'UNKNOWN')
                self.assertEqual(route.checkpoint('new', 'progress', 'active').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            conn.execute('UPDATE settings SET value_json=? WHERE key=?', (raw, key))
        self.assertEqual(self._events(), 1)
        route.close()

    def test_anchor_and_reservation_are_atomic_on_insert_failure(self):
        from unittest import mock
        from orch import bridge_worker_lifecycle as module
        authority, grant, route, claim = self._ready()
        original = module._store
        def fail_insert(conn, key, obj, **kwargs):
            if key == 'b10:v1:op:' + grant + ':new' and kwargs.get('insert'):
                raise OSError('simulated insert failure')
            return original(conn, key, obj, **kwargs)
        with mock.patch.object(module, '_store', fail_insert):
            self.assertEqual(route.checkpoint('new', 'progress', 'active').state, 'UNKNOWN')
        with self.orch.connect() as conn:
            grant_row = json.loads(conn.execute('SELECT value_json FROM settings WHERE key=?', ('b10:v1:grant:' + grant,)).fetchone()[0])
            op_row = conn.execute('SELECT 1 FROM settings WHERE key=?', ('b10:v1:op:' + grant + ':new',)).fetchone()
        self.assertEqual(grant_row['checkpoint_ids'], [])
        self.assertIsNone(op_row)
        self.assertEqual(self._events(), 0)
        route.close()

    def test_distinct_concurrent_checkpoint_anchors_are_preserved(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        authority, grant, route, claim = self._ready()
        route2 = authority.open_route(grant)
        barrier = threading.Barrier(2)
        def invoke(args):
            context, name = args
            barrier.wait(timeout=5)
            return context.checkpoint(name, 'concurrent', 'unknown')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, ((route, 'a'), (route2, 'b'))))
        self.assertEqual([result.state for result in results], ['SUCCEEDED', 'SUCCEEDED'])
        self.assertEqual(self._events(), 2)
        self.assertEqual(route.disposition('a').state, 'SUCCEEDED')
        self.assertEqual(route.disposition('b').state, 'SUCCEEDED')
        route.close(); route2.close()
