"""Bounded real filesystem replacement, uncertain outcome and closure gates."""
import dataclasses
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_patch_intent import FilePatchReplacement
from orch.bridge_patch_preflight import FilePatchBaseline
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry, inspect_project
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding
try:
    from orch import bridge_file_patch as module
except ImportError:
    module = None


def _kill_worker(home, cap, repo, saved_request, phase):
    """Fresh process avoids inheriting macOS native-library fork state."""
    binding = WorkerRunBinding(**saved_request.pop('binding'))
    request = WorkerOperationRequest(binding=binding, **saved_request)
    orch = Orchestrator(home, initialize_directories=False)
    # Only unchanged external Git inspection is cached for the fault matrix.
    git_snapshot = inspect_project(repo)
    with mock.patch('orch.bridge_authorization.inspect_project', return_value=git_snapshot):
        authority = LocalRunSessionAuthority(orch)
        session = authority.open_session(binding, cap)
        patcher = module.LocalBoundedFilePatcher(authority, session, binding, cap)
        original_append = patcher._append
        original_copy = module.copy_file_metadata
        original_replace, original_fsync = os.replace, os.fsync
        def barrier():
            print('READY', flush=True)
            while True: signal.pause()
        def append(record, authorize):
            value = original_append(record, authorize)
            if record['phase'] == phase: barrier()
            return value
        def copy(metadata, fd):
            original_copy(metadata, fd)
            if phase == 'SCRATCH': barrier()
        def rename(*args, **kwargs):
            original_replace(*args, **kwargs)
            if phase == 'REPLACE': barrier()
        def fsync(fd):
            original_fsync(fd)
            info, parent = os.fstat(fd), repo.stat()
            if phase == 'PARENT_FSYNC' and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                barrier()
        with mock.patch.object(patcher, '_append', side_effect=append), \
             mock.patch.object(module, 'copy_file_metadata', side_effect=copy), \
             mock.patch.object(module.os, 'replace', side_effect=rename), \
             mock.patch.object(module.os, 'fsync', side_effect=fsync):
            print(json.dumps(patcher.apply(request, FilePatchReplacement(
                FilePatchBaseline(hashlib.sha256(b'original\n').hexdigest(), 9), b'replacement\n'))), flush=True)


class BoundedFilePatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='orch-bounded-patch-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / 'repo'
        self._git('init', '-q', '-b', 'main', str(self.repo))
        self._git('-C', str(self.repo), 'config', 'user.name', 'Fixture')
        self._git('-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'docs').mkdir()
        for p, data in [('README.md', b'original\n'), ('docs/nested.txt', b'nested\n'),
                        ('empty.txt', b''), ('big.txt', b'a' * 65536), ('outside.txt', b'private')]:
            (self.repo / p).write_bytes(data)
        (self.repo / 'README.md').chmod(0o751)
        self._git('-C', str(self.repo), 'add', '.')
        self._git('-C', str(self.repo), 'commit', '-qm', 'fixture')
        self.orch = Orchestrator(self.base / 'home')
        project = ProjectRegistry(self.orch.root).add(
            self.repo, name='Patch fixture', profile='safe', review_mode='off',
            allow_commit=True, allow_push=False,
        )['project']
        plan = build_single_task_plan(project, task_id='PATCH', goal='Bounded replacement',
            allowed_paths=['README.md', 'docs/nested.txt', 'empty.txt', 'big.txt',
                           'missing.txt', 'link.txt', 'fifo.txt', 'result.json'],
            plan_revision='patch-fixture')
        path = self.base / 'plan.json';write_plan(path, plan);self.orch.load_plan(path)
        self.claim = self.orch.claim('fixture', project_id=project['project_id'])
        self.cap = Path(self.claim['capability_file'])
        self.binding = WorkerRunBinding(project['project_id'], project['writer_key'],
            self.claim['task_id'], self.claim['run_id'], self.claim['attempt'])
        self.authority = LocalRunSessionAuthority(self.orch)
        # Fault matrices leave Git metadata unchanged. Snapshot only this slow
        # external read; ledger/capability and real filesystem checks stay live.
        baseline_git = inspect_project(self.repo)
        self.git_snapshot = mock.patch('orch.bridge_authorization.inspect_project',
                                       return_value=baseline_git)
        self.git_snapshot.start();self.addCleanup(self.git_snapshot.stop)
        self.session = self.authority.open_session(self.binding, self.cap)

    def _git(self, *args):
        env = dict(os.environ);env.update(GIT_CONFIG_GLOBAL='/dev/null',
            GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
            capture_output=True, text=True, check=True, env=env, timeout=12)

    def _patcher(self):
        self.assertIsNotNone(module, 'Bounded file.patch is not implemented')
        return module.LocalBoundedFilePatcher(self.authority, self.session, self.binding, self.cap)

    def _request(self, **updates):
        data = dict(binding=self.binding, operation_id='patch-1', kind='file.patch', target='README.md')
        data.update(updates);return WorkerOperationRequest(**data)

    def _replacement(self, original=b'original\n', result=b'replacement\n'):
        return FilePatchReplacement(FilePatchBaseline(hashlib.sha256(original).hexdigest(), len(original)), result)

    def test_applies_existing_file_preserves_metadata_and_terminal_is_historical(self):
        from orch.bridge_patch_metadata import _native_api, read_file_metadata
        fd = os.open(self.repo / 'README.md', os.O_RDONLY)
        try:
            _native_api().set_value(fd, b'com.apple.ResourceFork', b'private resource fork')
            metadata = read_file_metadata(fd)
        finally: os.close(fd)
        patcher = self._patcher();before = (self.repo / 'README.md').stat()
        result = patcher.apply(self._request(), self._replacement())
        self.assertEqual(result['status'], 'APPLIED');self.assertFalse(result['historical'])
        after = (self.repo / 'README.md').stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode),
                         (before.st_uid, before.st_gid, before.st_mode))
        self.assertNotEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'replacement\n')
        fd = os.open(self.repo / 'README.md', os.O_RDONLY)
        try: self.assertEqual(read_file_metadata(fd), metadata)
        finally: os.close(fd)
        (self.repo / 'README.md').write_bytes(b'later edit\n')
        replay = self._patcher().apply(self._request(), self._replacement())
        self.assertEqual(replay['status'], 'APPLIED');self.assertTrue(replay['historical'])
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'later edit\n')
        self.assertFalse(list(self.repo.rglob('.orch-patch-*.tmp')))

    def test_conflicts_and_revoked_authority_never_apply_again(self):
        patcher = self._patcher();patcher.apply(self._request(), self._replacement())
        conflict = patcher.apply(self._request(), self._replacement(result=b'different'))
        self.assertEqual(conflict['status'], 'CONFLICT')
        self.cap.chmod(0o644)
        self.assertEqual(patcher.apply(self._request(), self._replacement())['status'], 'REFUSED')
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'replacement\n')

    def _intent_paths(self, request):
        from orch.bridge_operation_journal import JOURNAL_DIRECTORY
        runtime = self.orch.root / '.runtime'
        journal = runtime / JOURNAL_DIRECTORY
        run = journal / hashlib.sha256(self.binding.run_id.encode()).hexdigest()
        record = run / (hashlib.sha256(request.operation_id.encode()).hexdigest() + '.json')
        return {'intent': record, 'intent-run': run, 'journal': journal,
                'runtime': runtime, 'home': self.orch.root}

    def _file_evidence(self, path):
        info = path.stat()
        identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                    info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                    getattr(info, 'st_flags', 0))
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try: metadata = module.read_file_metadata(fd)
        finally: os.close(fd)
        return path.read_bytes(), identity, metadata

    def _lose_initial_intent_acknowledgement(self, patcher, request):
        from orch.bridge_operation_journal import OperationIntentJournal
        paths = self._intent_paths(request);real_sync = os.fsync;failed = []
        def sync(fd):
            if paths['intent'].exists():
                info, run = os.fstat(fd), paths['intent-run'].stat()
                if (info.st_dev, info.st_ino) == (run.st_dev, run.st_ino):
                    failed.append('intent-run')
                    raise OSError('published intent run-directory acknowledgement failed')
            return real_sync(fd)
        with mock.patch.object(module.os, 'fsync', side_effect=sync):
            response = patcher.apply(request, self._replacement())
        self.assertEqual(response['status'], 'UNKNOWN', response)
        self.assertEqual(response['error_code'], 'journal_publication_unknown', response)
        self.assertFalse(response['historical'])
        self.assertEqual(failed, ['intent-run'])
        retained = OperationIntentJournal(self.orch.root).inspect(
            self.binding.run_id, request.operation_id)
        self.assertEqual(retained['status'], 'RECORDED', retained)
        self.assertEqual(retained['record']['request'], dataclasses.asdict(request))
        self.assertEqual(self._records(request)['status'], 'ABSENT')
        return paths

    def test_retained_intent_sync_failure_blocks_dispatch_until_exact_renewal(self):
        from orch import bridge_patch_execution as execution
        target = self.repo / 'README.md';request = self._request(operation_id='lost-intent-ack')
        patcher = self._patcher();target_before = self._file_evidence(target)
        index_before = (self.repo / '.git' / 'index').read_bytes()
        paths = self._lose_initial_intent_acknowledgement(patcher, request)
        evidence = self._file_evidence(paths['intent'])
        execution_root = self.orch.root / '.runtime' / execution.EXECUTION_DIRECTORY
        self.assertFalse(execution_root.exists())
        real_sync = os.fsync;failures = []
        def fail_run(fd):
            info, run = os.fstat(fd), paths['intent-run'].stat()
            if (info.st_dev, info.st_ino) == (run.st_dev, run.st_ino):
                failures.append('intent-run')
                raise OSError('retained intent acknowledgement still failing')
            return real_sync(fd)
        with mock.patch.object(module.os, 'fsync', side_effect=fail_run):
            for _ in range(2):
                response = self._patcher().apply(request, self._replacement())
                self.assertEqual(response['status'], 'UNKNOWN', response)
                self.assertFalse(response['historical'])
                self.assertFalse(response['retry_allowed'])
                self.assertEqual(self._records(request)['status'], 'ABSENT')
                self.assertFalse(execution_root.exists())
                self.assertFalse(list(self.repo.rglob('.orch-patch-*.tmp')))
                self.assertEqual(self._file_evidence(target), target_before)
                self.assertEqual(self._file_evidence(paths['intent']), evidence)
                self.assertEqual((self.repo / '.git' / 'index').read_bytes(), index_before)
        self.assertEqual(failures, ['intent-run', 'intent-run'])

        # Trace actual descriptor fsyncs at the real DISPATCH link, independently
        # of a helper return value or a readable journal record.
        identities = {(path.stat().st_dev, path.stat().st_ino): name
                      for name, path in paths.items()}
        synced, creation_barriers, dispatch_barriers, replacements = [], [], [], []
        real_link, real_replace, real_mkdir = os.link, os.replace, os.mkdir
        def sync(fd):
            info = os.fstat(fd);name = identities.get((info.st_dev, info.st_ino))
            result = real_sync(fd)
            if name is not None: synced.append(name)
            return result
        def link(source, destination, *args, **kwargs):
            if destination == 'dispatch.json':
                dispatch_barriers.append(tuple(synced))
                self.assertEqual(set(synced), set(paths), synced)
                self.assertEqual(self._file_evidence(paths['intent']), evidence)
                self.assertEqual(self._file_evidence(target), target_before)
            return real_link(source, destination, *args, **kwargs)
        def mkdir(path, *args, **kwargs):
            if path == execution.EXECUTION_DIRECTORY:
                creation_barriers.append(tuple(synced))
                self.assertEqual(set(synced), set(paths), synced)
                self.assertEqual(self._file_evidence(paths['intent']), evidence)
                self.assertEqual(self._file_evidence(target), target_before)
            return real_mkdir(path, *args, **kwargs)
        def replace(source, destination, *args, **kwargs):
            if isinstance(source, str) and source.startswith('.orch-patch-'):
                replacements.append((source, destination))
            return real_replace(source, destination, *args, **kwargs)
        with mock.patch.object(module.os, 'fsync', side_effect=sync), \
             mock.patch.object(module.os, 'link', side_effect=link), \
             mock.patch.object(module.os, 'mkdir', side_effect=mkdir), \
             mock.patch.object(module.os, 'replace', side_effect=replace):
            response = self._patcher().apply(request, self._replacement())
            self.assertEqual(response['status'], 'APPLIED', response)
            self.assertFalse(response['historical'])
            self.assertEqual(len(creation_barriers), 1)
            self.assertEqual(len(dispatch_barriers), 1)
            for name in paths:
                self.assertEqual(creation_barriers[0].count(name), 1, creation_barriers)
            self.assertEqual(len(replacements), 1)
            execution_evidence = {p.name: self._file_evidence(p) for p in
                (execution_root / hashlib.sha256(self.binding.run_id.encode()).hexdigest()
                 / hashlib.sha256(request.operation_id.encode()).hexdigest()).iterdir()}
            target.write_bytes(b'later workspace edit\n')
            for invoke in (lambda: self._patcher().apply(request, self._replacement()),
                           lambda: self._patcher().reconcile(request)):
                historical = invoke()
                self.assertEqual(historical['status'], 'APPLIED', historical)
                self.assertTrue(historical['historical'])
            self.assertEqual(len(replacements), 1)
        self.assertEqual(self._file_evidence(paths['intent']), evidence)
        operation = execution_root / hashlib.sha256(self.binding.run_id.encode()).hexdigest() / hashlib.sha256(request.operation_id.encode()).hexdigest()
        self.assertEqual({p.name: self._file_evidence(p) for p in operation.iterdir()}, execution_evidence)
        self.assertEqual(target.read_bytes(), b'later workspace edit\n')
        self.assertEqual((self.repo / '.git' / 'index').read_bytes(), index_before)
        self.assertFalse(list(self.repo.rglob('.orch-patch-*.tmp')))

    def test_retained_intent_renewal_requires_authority_before_and_after_sync(self):
        request = self._request(operation_id='intent-authority')
        paths = self._lose_initial_intent_acknowledgement(self._patcher(), request)
        target = self.repo / 'README.md';target_before = self._file_evidence(target)
        evidence = self._file_evidence(paths['intent'])
        self.cap.chmod(0o644)
        with mock.patch.object(module.os, 'fsync', side_effect=AssertionError('unauthorized renewal')):
            denied = self._patcher().apply(request, self._replacement())
        self.assertEqual(denied['status'], 'REFUSED', denied)
        self.assertEqual(denied['error_code'], 'patch_authority_denied', denied)
        self.assertFalse(denied['historical'])
        self.cap.chmod(0o600)
        real_sync = os.fsync;revocations = []
        def revoke(fd):
            result = real_sync(fd)
            info, record = os.fstat(fd), paths['intent'].stat()
            if (info.st_dev, info.st_ino) == (record.st_dev, record.st_ino):
                revocations.append('intent')
                self.cap.chmod(0o644)
            return result
        with mock.patch.object(module.os, 'fsync', side_effect=revoke):
            denied = self._patcher().apply(request, self._replacement())
        self.assertEqual(revocations, ['intent'])
        self.assertEqual(denied['status'], 'UNKNOWN', denied)
        self.assertEqual(denied['error_code'], 'patch_execution_unavailable', denied)
        self.assertFalse(denied['historical'])
        self.assertEqual(self._records(request)['status'], 'ABSENT')
        self.assertEqual(self._file_evidence(paths['intent']), evidence)
        self.assertEqual(self._file_evidence(target), target_before)
        self.assertFalse(list(self.repo.rglob('.orch-patch-*.tmp')))

    def test_busy_retained_intent_retry_does_not_acknowledge_or_dispatch(self):
        request = self._request(operation_id='intent-busy')
        paths = self._lose_initial_intent_acknowledgement(self._patcher(), request)
        target = self.repo / 'README.md';target_before = self._file_evidence(target)
        evidence = self._file_evidence(paths['intent'])
        with module.runtime_operation_lock(self.orch.root), \
             mock.patch.object(module.os, 'fsync', side_effect=AssertionError('busy renewal')):
            denied = self._patcher().apply(request, self._replacement())
        self.assertEqual(denied['status'], 'UNKNOWN', denied)
        self.assertEqual(denied['error_code'], 'patch_execution_busy', denied)
        self.assertFalse(denied['historical'])
        self.assertEqual(self._records(request)['status'], 'ABSENT')
        self.assertEqual(self._file_evidence(paths['intent']), evidence)
        self.assertEqual(self._file_evidence(target), target_before)
        self.assertFalse(list(self.repo.rglob('.orch-patch-*.tmp')))

    def test_retained_result_sync_failure_blocks_history_until_exact_renewal(self):
        from orch import bridge_patch_execution as execution
        for disposition in ('APPLIED', 'NOT_APPLIED'):
            with self.subTest(disposition=disposition):
                target = self.repo / 'README.md';target.write_bytes(b'original\n')
                request = self._request(operation_id='failed-result-' + disposition.lower())
                patcher = self._patcher()
                operation = (self.orch.root / '.runtime' / execution.EXECUTION_DIRECTORY
                             / hashlib.sha256(self.binding.run_id.encode()).hexdigest()
                             / hashlib.sha256(request.operation_id.encode()).hexdigest())
                real_sync = os.fsync;failures = []
                def sync(fd):
                    info = os.fstat(fd)
                    if (operation / 'result.json').exists():
                        owned = operation.stat()
                        if (info.st_dev, info.st_ino) == (owned.st_dev, owned.st_ino):
                            failures.append('result-directory')
                            raise OSError('final result acknowledgement failed')
                    return real_sync(fd)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(execution.os, 'fsync', side_effect=sync))
                    if disposition == 'NOT_APPLIED':
                        stack.enter_context(mock.patch.object(module, 'copy_file_metadata',
                                                side_effect=ValueError('prepublication failure')))
                    initial = patcher.apply(request, self._replacement())
                    self.assertEqual(initial['status'], 'UNKNOWN', initial)
                    found = self._records(request)
                    self.assertTrue(found['terminal'], found)
                    self.assertEqual(found['records']['result']['disposition'], disposition)
                    evidence = {p.name: (p.read_bytes(), p.stat().st_ino) for p in operation.iterdir()}
                    for invoke in (lambda: patcher.apply(request, self._replacement()),
                                   lambda: patcher.reconcile(request)):
                        response = invoke()
                        self.assertEqual(response['status'], 'UNKNOWN', response)
                        self.assertFalse(response['historical'])
                    self.assertEqual(len(failures), 3)
                target.write_bytes(b'later workspace edit\n')
                with mock.patch.object(module.LocalFileScope, 'open_patch_target',
                                       side_effect=AssertionError('historical target read')), \
                     mock.patch.object(patcher, '_read_target', side_effect=AssertionError('historical readback')), \
                     mock.patch.object(patcher._store, 'append', side_effect=AssertionError('historical append')), \
                     mock.patch.object(module.os, 'replace', side_effect=AssertionError('historical rename')):
                    for response in (patcher.apply(request, self._replacement()), patcher.reconcile(request)):
                        self.assertEqual(response['status'], disposition, response)
                        self.assertTrue(response['historical'])
                self.assertEqual({p.name: (p.read_bytes(), p.stat().st_ino) for p in operation.iterdir()}, evidence)
                self.assertEqual(target.read_bytes(), b'later workspace edit\n')

    def test_historical_reply_requires_live_authority_before_and_after_renewal(self):
        from orch import bridge_patch_execution as execution
        request = self._request()
        self.assertEqual(self._patcher().apply(request, self._replacement())['status'], 'APPLIED')
        target = self.repo / 'README.md';target.write_bytes(b'later edit\n')
        for boundary in ('before', 'after'):
            for method in ('apply', 'reconcile'):
                with self.subTest(boundary=boundary, method=method):
                    self.cap.chmod(0o600)
                    patcher = self._patcher();real_sync = os.fsync
                    calls = []
                    def sync(fd):
                        calls.append(fd)
                        real_sync(fd)
                        self.cap.chmod(0o644)
                    if boundary == 'before': self.cap.chmod(0o644)
                    with mock.patch.object(execution.os, 'fsync', side_effect=sync):
                        response = (patcher.apply(request, self._replacement()) if method == 'apply'
                                    else patcher.reconcile(request))
                    self.assertIn(response['status'], {'REFUSED', 'UNKNOWN'}, response)
                    self.assertFalse(response['historical'])
                    self.assertEqual(bool(calls), boundary == 'after')
                    self.assertEqual(target.read_bytes(), b'later edit\n')
        self.cap.chmod(0o600)

    def test_historical_full_request_conflict_never_acknowledges(self):
        request = self._request()
        patcher = self._patcher()
        self.assertEqual(patcher.apply(request, self._replacement())['status'], 'APPLIED')
        foreign = self._request(target='docs/nested.txt')
        with mock.patch.object(module.os, 'fsync', side_effect=AssertionError('foreign renewal')):
            self.assertEqual(patcher.apply(foreign, self._replacement())['status'], 'CONFLICT')
            self.assertEqual(patcher.reconcile(foreign)['status'], 'CONFLICT')
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'replacement\n')

    def test_unsafe_outside_missing_symlink_fifo_hardlink_and_mode_refuse(self):
        patcher = self._patcher();(self.repo / 'link.txt').symlink_to(self.repo / 'README.md')
        os.mkfifo(self.repo / 'fifo.txt')
        for target in ('outside.txt', 'missing.txt', 'link.txt', 'fifo.txt'):
            result = patcher.apply(self._request(target=target), self._replacement())
            self.assertEqual(result['status'], 'REFUSED')
        os.link(self.repo / 'README.md', self.repo / 'second-link')
        self.assertEqual(patcher.apply(self._request(), self._replacement())['status'], 'REFUSED')
        (self.repo / 'second-link').unlink();(self.repo / 'README.md').chmod(0o4751)
        self.assertEqual(patcher.apply(self._request(), self._replacement())['status'], 'REFUSED')
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'original\n')

    def test_uncached_real_git_branch_drift_blocks_apply(self):
        patcher = self._patcher();self.git_snapshot.stop()
        self._git('-C', str(self.repo), 'switch', '-q', '-c', 'foreign-branch')
        self.assertEqual(patcher.apply(self._request(), self._replacement())['status'], 'REFUSED')
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'original\n')

    def test_nested_empty_and_maximum_replacements_are_bounded(self):
        patcher = self._patcher()
        for index, (target, original, result) in enumerate([
            ('docs/nested.txt', b'nested\n', b''), ('empty.txt', b'', b'nonempty'),
            ('big.txt', b'a' * 65536, b'b' * 65536),
        ]):
            request = self._request(target=target, operation_id='bound-' + str(index))
            self.assertEqual(patcher.apply(request, self._replacement(original, result))['status'], 'APPLIED')
            self.assertEqual((self.repo / target).read_bytes(), result)
        self.assertFalse(list(self.repo.rglob('.orch-patch-*.tmp')))

    def _records(self, request):
        return module.PatchExecutionStore(self.orch.root).inspect(
            self.binding.run_id, request.operation_id)

    def _kill_at(self, request, phase):
        """Kill a fresh interpreter after a real lock-owning boundary."""
        code = (
            "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "from tests.test_bridge_file_patch import _kill_worker; "
            "_kill_worker(Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),"
            "json.loads(sys.stdin.read()),sys.argv[5])"
        )
        process = subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', code,
             str(Path(__file__).resolve().parents[1]), str(self.orch.root),
             str(self.cap), str(self.repo), phase],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        process.stdin.write(json.dumps(dataclasses.asdict(request)).encode('ascii'))
        process.stdin.close()
        try:
            ready, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(ready, 'child did not reach ' + phase)
            observed = process.stdout.readline()
        finally:
            process.kill()
            process.wait(timeout=10)
            errors = process.stderr.read().decode('utf-8', errors='replace')
            process.stdout.close();process.stderr.close()
        self.assertEqual(observed, b'READY\n', 'child failed before ' + phase + ': ' + errors)
        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_process_kill_boundaries_reconcile_without_replay(self):
        cases = [('DISPATCH', 'NOT_APPLIED'), ('SCRATCH', 'UNKNOWN'),
                 ('PUBLICATION', 'NOT_APPLIED'), ('REPLACE', 'APPLIED'),
                 ('PARENT_FSYNC', 'APPLIED'), ('RESULT', 'APPLIED')]
        for phase, expected in cases:
            with self.subTest(phase=phase):
                (self.repo / 'README.md').write_bytes(b'original\n')
                request = self._request(operation_id='kill-' + phase)
                self._kill_at(request, phase)
                found = self._records(request)
                self.assertEqual(found['status'], 'RECORDED', found)
                before = (self.repo / 'README.md').read_bytes()
                scratch = self.repo / found['records']['dispatch']['temp_name']
                scratch_bytes = scratch.read_bytes() if scratch.exists() else None
                with mock.patch.object(module.os, 'replace', side_effect=AssertionError('replay')), \
                     mock.patch.object(module.os, 'write', side_effect=AssertionError('new bytes')):
                    duplicate = self._patcher().apply(request, self._replacement())
                self.assertEqual(duplicate['status'], 'APPLIED' if phase == 'RESULT' else 'UNKNOWN')
                self.assertEqual((self.repo / 'README.md').read_bytes(), before)
                with mock.patch.object(module.os, 'replace', side_effect=AssertionError('replay')):
                    reconciled = self._patcher().reconcile(request)
                self.assertEqual(reconciled['status'], expected, reconciled)
                self.assertEqual((self.repo / 'README.md').read_bytes(), before)
                if expected == 'UNKNOWN':
                    self.assertEqual(scratch.read_bytes(), scratch_bytes)
                    self.assertFalse(self._records(request)['terminal'])
                else:
                    self.assertFalse(scratch.exists())
                    self.assertTrue(self._records(request)['terminal'])
                    closed = self._patcher().apply(request, self._replacement())
                    self.assertEqual(closed['status'], expected)
                    self.assertTrue(closed['historical'])

    def test_prepublication_faults_close_only_owned_scratch(self):
        for index, boundary in enumerate(('open', 'write', 'metadata', 'file_fsync')):
            with self.subTest(boundary=boundary):
                request = self._request(operation_id='fault-' + str(index))
                original_open, original_fsync = os.open, os.fsync
                def opened(path, flags, *args, **kwargs):
                    if isinstance(path, str) and path.startswith('.orch-patch-'):
                        raise OSError('exclusive create failed')
                    return original_open(path, flags, *args, **kwargs)
                def fsync(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_size == len(b'replacement\n'):
                        raise OSError('scratch fsync failed')
                    return original_fsync(fd)
                with contextlib.ExitStack() as stack:
                    if boundary == 'open':
                        opened_mock = stack.enter_context(mock.patch.object(module.os, 'open', side_effect=opened))
                        stack.enter_context(mock.patch.object(module.os, 'supports_dir_fd', os.supports_dir_fd | {opened_mock}))
                    elif boundary == 'write':
                        original_write = os.write
                        def written(fd, data):
                            if data == b'replacement\n': raise OSError('short write')
                            return original_write(fd, data)
                        stack.enter_context(mock.patch.object(module.os, 'write', side_effect=written))
                    elif boundary == 'metadata':
                        stack.enter_context(mock.patch.object(module, 'copy_file_metadata', side_effect=ValueError('metadata failed')))
                    else:
                        stack.enter_context(mock.patch.object(module.os, 'fsync', side_effect=fsync))
                    response = self._patcher().apply(request, self._replacement())
                self.assertEqual(response['status'], 'NOT_APPLIED', response)
                self.assertEqual((self.repo / 'README.md').read_bytes(), b'original\n')
                self.assertTrue(self._records(request)['terminal'])
        self.assertFalse(list(self.repo.glob('.orch-patch-*.tmp')))

    def test_uncertain_rename_fsync_ack_and_independent_readback(self):
        for index, boundary in enumerate(('rename_before', 'rename_after', 'parent_fsync', 'result_ack', 'readback', 'scratch_reappears')):
            with self.subTest(boundary=boundary):
                (self.repo / 'README.md').write_bytes(b'original\n')
                request = self._request(operation_id='uncertain-' + str(index))
                patcher = self._patcher()
                original_replace, original_fsync, original_append = os.replace, os.fsync, patcher._store.append
                original_read = patcher._read_target
                def rename(*args, **kwargs):
                    if boundary == 'rename_before': raise OSError('rename failed')
                    original_replace(*args, **kwargs)
                    raise OSError('rename response lost')
                def fsync(fd):
                    if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (self.repo.stat().st_dev, self.repo.stat().st_ino):
                        raise OSError('parent fsync failed')
                    return original_fsync(fd)
                def append(record, *, authorize):
                    response = original_append(record, authorize=authorize)
                    return {'status': 'UNKNOWN'} if record['phase'] == 'RESULT' else response
                def read(scope, selected):
                    observed = original_read(scope, selected)
                    name = self._records(selected)['records']['dispatch']['temp_name']
                    (self.repo / name).write_bytes(b'foreign after rename')
                    return observed
                with contextlib.ExitStack() as stack:
                    if boundary.startswith('rename'):
                        stack.enter_context(mock.patch.object(module.os, 'replace', side_effect=rename))
                    elif boundary == 'parent_fsync':
                        stack.enter_context(mock.patch.object(module.os, 'fsync', side_effect=fsync))
                    elif boundary == 'result_ack':
                        stack.enter_context(mock.patch.object(patcher._store, 'append', side_effect=append))
                    elif boundary == 'scratch_reappears':
                        stack.enter_context(mock.patch.object(patcher, '_read_target', side_effect=read))
                    else:
                        stack.enter_context(mock.patch.object(patcher, '_read_target', side_effect=ValueError('reopen unavailable')))
                    response = patcher.apply(request, self._replacement())
                self.assertEqual(response['status'], 'UNKNOWN', response)
                expected = ('UNKNOWN' if boundary == 'scratch_reappears' else
                            'NOT_APPLIED' if boundary == 'rename_before' else 'APPLIED')
                response = self._patcher().reconcile(request)
                self.assertEqual(response['status'], expected, response)
                if boundary == 'scratch_reappears':
                    self.assertFalse(self._records(request)['terminal'])
                    name = self._records(request)['records']['dispatch']['temp_name']
                    self.assertEqual((self.repo / name).read_bytes(), b'foreign after rename')
                self.assertEqual((self.repo / 'README.md').read_bytes(),
                                 b'original\n' if expected == 'NOT_APPLIED' else b'replacement\n')

    def test_equal_bytes_wrong_inode_missing_and_foreign_scratch_stay_unknown(self):
        for phase, drift in [('REPLACE', 'target_inode'), ('PUBLICATION', 'missing'),
                             ('PUBLICATION', 'foreign'), ('SCRATCH', 'present')]:
            with self.subTest(phase=phase, drift=drift):
                (self.repo / 'README.md').write_bytes(b'original\n')
                request = self._request(operation_id='drift-' + drift)
                self._kill_at(request, phase)
                records = self._records(request)['records']
                scratch = self.repo / records['dispatch']['temp_name']
                if drift == 'target_inode':
                    foreign = self.repo / 'foreign-target'
                    foreign.write_bytes(b'replacement\n');foreign.chmod(0o751)
                    os.replace(foreign, self.repo / 'README.md')
                elif drift == 'missing':
                    scratch.unlink()
                elif drift == 'foreign':
                    # Keep the exact old inode alive to exclude inode reuse.
                    held = os.open(scratch, os.O_RDONLY)
                    try:
                        scratch.unlink();scratch.write_bytes(b'replacement\n')
                        response = self._patcher().reconcile(request)
                        self.assertEqual(response['status'], 'UNKNOWN', response)
                        self.assertEqual(scratch.read_bytes(), b'replacement\n')
                    finally: os.close(held)
                    continue
                before = (self.repo / 'README.md').read_bytes()
                response = self._patcher().reconcile(request)
                self.assertEqual(response['status'], 'UNKNOWN', response)
                self.assertFalse(self._records(request)['terminal'])
                self.assertEqual((self.repo / 'README.md').read_bytes(), before)
                if drift == 'present': self.assertTrue(scratch.exists())

    def test_authority_and_path_drift_after_dispatch_preserve_evidence(self):
        for index, drift in enumerate(('capability', 'original', 'parent', 'staged_alias')):
            with self.subTest(drift=drift):
                target = 'docs/nested.txt'
                (self.repo / target).write_bytes(b'original\n')
                request = self._request(target=target, operation_id='authority-' + str(index))
                patcher = self._patcher();real_append = patcher._append
                def append(record, authorize):
                    response = real_append(record, authorize)
                    if record['phase'] == 'PUBLICATION':
                        if drift == 'capability': self.cap.chmod(0o644)
                        elif drift == 'original': (self.repo / target).write_bytes(b'foreign')
                        elif drift == 'parent':
                            (self.repo / 'docs').rename(self.repo / 'old-docs')
                            (self.repo / 'docs').mkdir();(self.repo / target).write_bytes(b'foreign')
                        else:
                            records = self._records(request)['records']
                            name = self.repo / 'docs' / records['dispatch']['temp_name']
                            name.rename(name.with_suffix('.held'))
                            name.write_bytes(b'replacement\n')
                    return response
                with mock.patch.object(patcher, '_append', side_effect=append):
                    response = patcher.apply(request, self._replacement())
                self.assertEqual(response['status'], 'UNKNOWN', response)
                self.assertFalse(self._records(request)['terminal'])
                if drift == 'capability':
                    self.assertEqual(patcher.reconcile(request)['status'], 'UNKNOWN')
                    self.cap.chmod(0o600)
                    self.assertEqual(patcher.reconcile(request)['status'], 'NOT_APPLIED')
                else:
                    self.assertEqual(patcher.reconcile(request)['status'], 'UNKNOWN')
                if drift == 'parent':
                    (self.repo / target).unlink();(self.repo / 'docs').rmdir()
                    (self.repo / 'old-docs').rename(self.repo / 'docs')

    def test_busy_apply_is_unknown_and_creates_no_intent(self):
        from orch.bridge_operation_journal import OperationIntentJournal
        with module.runtime_operation_lock(self.orch.root):
            response = self._patcher().apply(self._request(), self._replacement())
        self.assertEqual(response['status'], 'UNKNOWN')
        self.assertEqual(response['error_code'], 'patch_execution_busy')
        self.assertEqual(OperationIntentJournal(self.orch.root).inspect(
            self.binding.run_id, 'patch-1')['status'], 'ABSENT')

    def test_foreign_scratch_collision_never_cleans_or_infers_negative(self):
        patcher = self._patcher();real_append = patcher._append
        def append(record, authorize):
            response = real_append(record, authorize)
            if record['phase'] == 'DISPATCH':
                (self.repo / record['temp_name']).write_bytes(b'foreign scratch')
            return response
        with mock.patch.object(patcher, '_append', side_effect=append):
            response = patcher.apply(self._request(), self._replacement())
        self.assertEqual(response['status'], 'UNKNOWN')
        record = self._records(self._request())['records']['dispatch']
        scratch = self.repo / record['temp_name']
        self.assertEqual(scratch.read_bytes(), b'foreign scratch')
        self.assertEqual(patcher.reconcile(self._request())['status'], 'UNKNOWN')
        self.assertEqual(scratch.read_bytes(), b'foreign scratch')
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'original\n')

    def test_cli_inspection_is_read_only_and_reconcile_has_no_new_payload(self):
        from orch import cli
        def run(home, command):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(['--root', str(home), 'bridge', 'execution'] + command)
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())
        absent = self.base / 'absent-home'
        response = run(absent, ['inspect', '--run-id', self.binding.run_id, '--operation-id', 'patch-1'])
        self.assertEqual(response['status'], 'UNKNOWN');self.assertFalse(absent.exists())
        request = self._request();self._kill_at(request, 'PUBLICATION')
        arguments = ['--run-id', self.binding.run_id, '--operation-id', request.operation_id]
        with mock.patch.object(module.os, 'replace', side_effect=AssertionError('inspection rename')), \
             mock.patch.object(module.os, 'fsync', side_effect=AssertionError('inspection fsync')):
            response = run(self.orch.root, ['inspect'] + arguments)
        self.assertEqual(response['status'], 'RECORDED');self.assertFalse(response['terminal'])
        before = self._records(request)
        self.cap.chmod(0o644)
        response = run(self.orch.root, ['reconcile'] + arguments + ['--cap', str(self.cap)])
        self.assertEqual(response['status'], 'UNKNOWN');self.assertEqual(self._records(request), before)
        self.cap.chmod(0o600)
        with mock.patch.object(module.os, 'replace', side_effect=AssertionError('reconcile replay')):
            response = run(self.orch.root, ['reconcile'] + arguments + ['--cap', str(self.cap)])
        self.assertEqual(response['status'], 'NOT_APPLIED')
        self.assertEqual((self.repo / 'README.md').read_bytes(), b'original\n')
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
             self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(['bridge', 'execution', 'reconcile'] + arguments +
                ['--cap', str(self.cap), '--replacement-file', 'arbitrary'])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn('private resource fork', json.dumps(response))
