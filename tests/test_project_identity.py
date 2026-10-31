"""Fresh local authority identity checks over disposable native repositories."""
import hashlib
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_bridge_authorization as native
from orch import project


class ProjectIdentityTests(unittest.TestCase):
    setUp = native.LocalRunSessionAuthorityTests.setUp
    _add_project = native.LocalRunSessionAuthorityTests._add_project
    _git = native.LocalRunSessionAuthorityTests._git
    _cap = native.LocalRunSessionAuthorityTests._cap
    _open = native.LocalRunSessionAuthorityTests._open
    _require = native.LocalRunSessionAuthorityTests._require

    def _denial(self, session_id, reason):
        with self.assertRaisesRegex(ValueError, 'bridge_project_binding_denied') as caught:
            self._require(session_id)
        self.assertEqual(str(caught.exception.__cause__), reason)

    def test_identity_matches_full_inspection_clean_and_dirty(self):
        root = Path(self.projects['a']['root'])
        for dirty in (False, True):
            if dirty:
                (root / 'untracked.txt').write_text('dirty\n')
            full = project.inspect_project(root)
            self.assertEqual(project.inspect_project_identity(root),
                {key: full[key] for key in ('root', 'writer_key', 'branch', 'head')})

    def test_branch_drift_denies_existing_session(self):
        sid = self._open(); self._require(sid)
        self._git('-C', self.projects['a']['root'], 'switch', '-q', '-c', 'other')
        self._denial(sid, 'git_branch_mismatch')

    def test_head_drift_denies_existing_session(self):
        sid = self._open(); self._require(sid)
        root = Path(self.projects['a']['root'])
        (root / 'another.txt').write_text('drift\n')
        self._git('-C', str(root), 'add', 'another.txt')
        self._git('-C', str(root), 'commit', '-qm', 'drift')
        self._denial(sid, 'git_head_mismatch')

    def test_writer_identity_drift_denies_existing_session(self):
        sid = self._open(); self._require(sid)
        root = Path(self.projects['a']['root']); dot = root / '.git'; held = root / '.git-retained'
        other = Path(self.projects['b']['root']) / '.git'; dot.rename(held)
        try:
            dot.symlink_to(other, target_is_directory=True)
            self._denial(sid, 'git_writer_mismatch')
        finally:
            if dot.is_symlink():
                dot.unlink()
            held.rename(dot)
        self._require(sid)

    def test_root_replacement_denies_existing_session(self):
        sid = self._open(); self._require(sid)
        root = Path(self.projects['a']['root']); held = root.with_name('held-original')
        root.rename(held); root.mkdir()
        try:
            self._denial(sid, 'not_a_git_repository')
        finally:
            root.rmdir(); held.rename(root)
        self._require(sid)

    def test_revoked_capability_denies_existing_session(self):
        sid = self._open(); self._require(sid); cap = self._cap('a'); cap.chmod(0o640)
        try:
            with self.assertRaisesRegex(ValueError, 'bridge_capability_denied'):
                self._require(sid)
        finally:
            cap.chmod(0o600)
        self._require(sid)

    def test_common_directory_failure_preserves_existing_fallback(self):
        root = self.base / 'unborn'
        self._git('init', '-q', '-b', 'main', str(root))
        original = project._git_inspection_metadata
        def failure(repo, *args):
            if args == ('rev-parse', '--git-common-dir'):
                return subprocess.CompletedProcess(['git'], 1, '', 'injected failure')
            return original(repo, *args)
        with mock.patch.object(project, '_git_inspection_metadata', side_effect=failure):
            full = project.inspect_project(root)
            identity = project.inspect_project_identity(root)
        expected = 'git:' + hashlib.sha256(str((root / '.git').resolve()).encode()).hexdigest()[:32]
        self.assertEqual(identity, {key: full[key] for key in ('root', 'writer_key', 'branch', 'head')})
        self.assertEqual(identity['writer_key'], expected)
        self.assertIsNone(identity['head'])
