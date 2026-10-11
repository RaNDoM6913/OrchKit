"""Durable preparation is distinct from patch application and retry authority."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_patch_preflight import FilePatchBaseline
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding
try:
    from orch import bridge_patch_intent as intent
except ImportError:
    intent = None


class PatchIntentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='orch-intent-service-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'repo'
        self._git('init', '-q', '-b', 'main', str(self.root))
        self._git('-C', str(self.root), 'config', 'user.name', 'Fixture')
        self._git('-C', str(self.root), 'config', 'user.email', 'fixture@example.invalid')
        (self.root / 'docs').mkdir()
        for name, data in [('README.md', b'original\n'), ('empty.txt', b''),
                           ('docs/nested.txt', b'nested\n'), ('big.txt', b'a' * 65536),
                           ('outside.txt', b'private\n')]:
            (self.root / name).write_bytes(data)
        self._git('-C', str(self.root), 'add', '.')
        self._git('-C', str(self.root), 'commit', '-qm', 'fixture')
        self.orch = Orchestrator(self.base / 'home')
        project = ProjectRegistry(self.orch.root).add(
            self.root, name='Intent fixture', profile='safe', review_mode='off',
            allow_commit=True, allow_push=False,
        )['project']
        plan = build_single_task_plan(
            project, task_id='PATCH-INTENT', goal='Record intent, never apply',
            allowed_paths=['README.md', 'empty.txt', 'docs/nested.txt', 'big.txt',
                           'missing.txt', 'link.txt'], plan_revision='intent-fixture',
        )
        plan_file = self.base / 'plan.json'
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        claim = self.orch.claim('fixture', project_id=project['project_id'])
        self.assertEqual(claim['status'], 'CLAIMED')
        self.cap = Path(claim['capability_file'])
        self.binding = WorkerRunBinding(
            project_id=project['project_id'], writer_key=project['writer_key'],
            task_id=claim['task_id'], run_id=claim['run_id'], attempt=claim['attempt'],
        )
        self.authority = LocalRunSessionAuthority(self.orch)
        self.session = self.authority.open_session(self.binding, self.cap)

    def _git(self, *args):
        env = dict(os.environ)
        env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                   GIT_TERMINAL_PROMPT='0')
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
                              capture_output=True, text=True, check=True, env=env,
                              timeout=12)

    def _service(self):
        self.assertIsNotNone(intent, 'Durable patch-intent service is not implemented')
        return intent.LocalFilePatchIntentService(
            self.authority, self.session, self.binding, self.cap,
        )

    def _request(self, **changes):
        values = dict(binding=self.binding, operation_id='intent-1', kind='file.patch',
                      target='README.md')
        values.update(changes)
        return WorkerOperationRequest(**values)

    def _replacement(self, original=b'original\n', replacement=b'replacement\n'):
        self.assertIsNotNone(intent, 'Bounded replacement companion is not implemented')
        return intent.FilePatchReplacement(
            FilePatchBaseline(hashlib.sha256(original).hexdigest(), len(original)),
            replacement,
        )

    def test_new_intent_preserves_workspace_and_records_only_metadata(self):
        service = self._service()
        before = (self.root / 'README.md').stat()
        result = service.prepare(self._request(), self._replacement())
        self.assertEqual(result['status'], 'CREATED')
        self.assertFalse(result['retry_allowed'])
        self.assertEqual(result['workspace_mutation'], 'NOT_IMPLEMENTED')
        self.assertEqual((self.root / 'README.md').read_bytes(), b'original\n')
        after = (self.root / 'README.md').stat()
        self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns),
                         (after.st_ino, after.st_mode, after.st_mtime_ns))
        self.assertEqual(self._git('-C', str(self.root), 'status', '--porcelain').stdout, '')
        serialized = json.dumps(result)
        self.assertNotIn('original\\n', serialized)
        self.assertNotIn('replacement\\n', serialized)
        for path in (self.orch.runtime / 'operation_journal').rglob('*.json'):
            text = path.read_text()
            self.assertNotIn('original\\n', text)
            self.assertNotIn('replacement\\n', text)
            self.assertNotIn('lease_token', text)

    def test_duplicate_after_replacement_or_other_content_does_not_preflight_again(self):
        service = self._service()
        request, replacement = self._request(), self._replacement()
        self.assertEqual(service.prepare(request, replacement)['status'], 'CREATED')
        for data in (b'replacement\n', b'foreign\n'):
            (self.root / 'README.md').write_bytes(data)
            with mock.patch.object(intent.LocalFilePatchPreflight, 'check',
                                   side_effect=AssertionError('duplicate preflight')):
                result = self._service().prepare(request, replacement)
            self.assertEqual(result['status'], 'EXISTING')
            self.assertFalse(result['retry_allowed'])
            self.assertEqual((self.root / 'README.md').read_bytes(), data)

    def test_conflicting_target_baseline_result_limit_or_binding_does_not_replace_record(self):
        service = self._service()
        request = self._request()
        first = service.prepare(request, self._replacement())
        self.assertEqual(first['status'], 'CREATED')
        variants = [(self._request(target='docs/nested.txt'), self._replacement()),
                    (self._request(max_output_bytes=1), self._replacement()),
                    (request, self._replacement(replacement=b'different\n')),
                    (request, self._replacement(original=b'changed\n'))]
        for candidate, companion in variants:
            self.assertEqual(service.prepare(candidate, companion)['status'], 'CONFLICT')
        foreign = self._request(binding=dataclasses.replace(self.binding, run_id='foreign'))
        self.assertEqual(service.prepare(foreign, self._replacement())['status'], 'REFUSED')
        self.assertEqual(service.prepare(request, self._replacement())['record'], first['record'])

    def test_revoked_capability_blocks_even_identical_duplicate_and_observation(self):
        service = self._service()
        self.assertEqual(service.prepare(self._request(), self._replacement())['status'], 'CREATED')
        self.cap.chmod(0o644)
        self.assertEqual(service.prepare(self._request(), self._replacement())['status'], 'REFUSED')
        result = service.observe(self._request())
        self.assertEqual(result['observation'], 'UNAVAILABLE')
        self.assertNotIn('content_sha256', result)

    def test_final_authority_revocation_prevents_new_record(self):
        service = self._service()
        original = intent.LocalFilePatchPreflight.check
        def revoke(checker, request, baseline):
            result = original(checker, request, baseline)
            self.cap.chmod(0o644)
            return result
        with mock.patch.object(intent.LocalFilePatchPreflight, 'check', new=revoke):
            result = service.prepare(self._request(), self._replacement())
        self.assertEqual(result['status'], 'REFUSED')
        self.assertFalse(list((self.orch.runtime / 'operation_journal').rglob('*.json')))

    def test_new_outside_missing_symlink_and_wrong_original_refuse_without_record(self):
        service = self._service()
        (self.root / 'link.txt').symlink_to(self.root / 'README.md')
        for target in ('outside.txt', 'missing.txt', 'link.txt'):
            result = service.prepare(self._request(target=target), self._replacement())
            self.assertEqual(result['status'], 'REFUSED')
        result = service.prepare(self._request(), self._replacement(original=b'wrong'))
        self.assertEqual(result['status'], 'REFUSED')
        self.assertFalse(list((self.orch.runtime / 'operation_journal').rglob('*.json')))

    def test_other_operation_kinds_never_record_an_intent(self):
        service = self._service()
        for kind, target in [('file.read', 'README.md'), ('file.list', 'docs'),
                             ('process.start', 'profile')]:
            result = service.prepare(self._request(kind=kind, target=target), self._replacement())
            self.assertEqual(result['status'], 'REFUSED')
        self.assertFalse(list((self.orch.runtime / 'operation_journal').rglob('*.json')))

    def test_restart_needs_fresh_session_but_existing_intent_survives(self):
        request = self._request()
        first = self._service().prepare(request, self._replacement())
        self.authority.close_session(self.session)
        self.assertEqual(self._service().prepare(request, self._replacement())['status'], 'REFUSED')
        self.authority = LocalRunSessionAuthority(self.orch)
        self.session = self.authority.open_session(self.binding, self.cap)
        result = self._service().prepare(request, self._replacement())
        self.assertEqual(result['status'], 'EXISTING')
        self.assertEqual(result['record'], first['record'])
        self.assertFalse(result['retry_allowed'])

    def test_observation_distinguishes_original_replacement_other_and_missing(self):
        service = self._service()
        request = self._request()
        self.assertEqual(service.prepare(request, self._replacement())['status'], 'CREATED')
        for data, want in [(b'original\n', 'ORIGINAL_MATCH'),
                           (b'replacement\n', 'REPLACEMENT_MATCH'), (b'other\n', 'OTHER_CONTENT')]:
            (self.root / 'README.md').write_bytes(data)
            result = service.observe(request)
            self.assertEqual(result['observation'], want)
            self.assertEqual(result['content_sha256'], hashlib.sha256(data).hexdigest())
            self.assertFalse(result['retry_allowed'])
            self.assertNotIn(data.decode(), json.dumps(result))
        (self.root / 'README.md').unlink()
        result = service.observe(request)
        self.assertEqual(result['observation'], 'UNAVAILABLE')
        self.assertNotIn('content_sha256', result)

    def test_changed_between_two_observations_returns_no_content_description(self):
        service = self._service()
        request = self._request()
        service.prepare(request, self._replacement())
        original = intent.LocalBoundedFileReader.read
        count = [0]
        def change(reader, request):
            result = original(reader, request)
            count[0] += 1
            if count[0] == 1: (self.root / 'README.md').write_bytes(b'other\n')
            return result
        with mock.patch.object(intent.LocalBoundedFileReader, 'read', new=change):
            result = service.observe(request)
        self.assertEqual(result['observation'], 'UNAVAILABLE')
        self.assertNotIn('content_sha256', result)

    def test_empty_nested_and_maximum_content_use_bounded_internal_read_ceiling(self):
        service = self._service()
        for index, (target, original, replacement) in enumerate([
            ('empty.txt', b'', b'nonempty'), ('docs/nested.txt', b'nested\n', b''),
            ('big.txt', b'a' * 65536, b'b' * 65536),
        ]):
            request = self._request(target=target, operation_id='bound-' + str(index),
                                    max_output_bytes=1)
            self.assertEqual(service.prepare(request, self._replacement(original, replacement))['status'],
                             'CREATED')
            self.assertEqual(service.observe(request)['observation'], 'ORIGINAL_MATCH')
            (self.root / target).write_bytes(replacement)
            self.assertEqual(service.observe(request)['observation'], 'REPLACEMENT_MATCH')
            (self.root / target).write_bytes(b'c' * 65537)
            self.assertEqual(service.observe(request)['observation'], 'UNAVAILABLE')

    def test_observation_requires_exact_recorded_request_and_existing_record(self):
        service = self._service()
        self.assertEqual(service.observe(self._request())['observation'], 'UNAVAILABLE')
        service.prepare(self._request(), self._replacement())
        for request in [self._request(target='docs/nested.txt'), self._request(max_output_bytes=1)]:
            result = service.observe(request)
            self.assertEqual(result['observation'], 'UNAVAILABLE')
            self.assertNotIn('content_sha256', result)

    def test_companion_rejects_equal_content_invalid_types_versions_and_sizes(self):
        self.assertIsNotNone(intent, 'Bounded replacement companion is not implemented')
        baseline = FilePatchBaseline(hashlib.sha256(b'x').hexdigest(), 1)
        for data in ('text', bytearray(b'x'), True, b'z' * 65537, b'x'):
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ValueError):
                intent.FilePatchReplacement(baseline, data)
        for version in (True, 0, 2, '1'):
            with self.assertRaises(ValueError): intent.FilePatchReplacement(baseline, b'y', version)
        with self.assertRaises(ValueError): intent.FilePatchReplacement(None, b'y')
        companion = intent.FilePatchReplacement(baseline, b'private-payload')
        self.assertNotIn('private-payload', repr(companion))

    def test_cli_inspects_historical_record_without_capability_or_writes(self):
        first = self._service().prepare(self._request(), self._replacement())
        self.assertEqual(first['status'], 'CREATED')
        self.cap.chmod(0o644)
        before = {str(p): p.read_bytes() for p in
                  (self.orch.runtime / 'operation_journal').rglob('*.json')}
        code = "import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + "); from orch.cli import main; raise SystemExit(main())"
        process = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', code,
                                  '--root', str(self.orch.root), 'bridge', 'operation',
                                  'inspect', '--run-id', self.binding.run_id,
                                  '--operation-id', 'intent-1'], cwd=str(self.base),
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['status'], 'RECORDED')
        self.assertEqual(result['record'], first['record'])
        self.assertEqual({str(p): p.read_bytes() for p in
                          (self.orch.runtime / 'operation_journal').rglob('*.json')}, before)
        self.assertNotIn('lease_token', process.stdout)
        self.assertNotIn('replacement\\n', process.stdout)

    def test_cli_inspection_of_missing_home_creates_no_runtime_or_ledger(self):
        missing = self.base / 'absent-home'
        code = "import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + "); from orch.cli import main; raise SystemExit(main())"
        process = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', code,
                                  '--root', str(missing), 'bridge', 'operation', 'inspect',
                                  '--run-id', 'unknown', '--operation-id', 'missing'],
                                 cwd=str(self.base), capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        # A missing home has no valid pinned anchor from which to prove the
        # record absent. The query stays unknown and creates no authority.
        self.assertEqual(json.loads(process.stdout)['status'], 'UNKNOWN')
        self.assertFalse(missing.exists())
