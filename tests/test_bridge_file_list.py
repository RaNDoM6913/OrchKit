"""Disposable live-authority tests for strictly bounded local file.list."""

import ast
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch import bridge_file_list as module
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_list import LocalBoundedFileLister
from orch.bridge_file_scope import LocalFileScope
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import (
    WORKER_MAX_OUTPUT_BYTES, WorkerOperationRequest, WorkerRunBinding,
    validate_worker_outcome,
)


class LocalBoundedFileListerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-bounded-list-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self._git("init", "-q", "-b", "main", str(self.root))
        self._git("-C", str(self.root), "config", "user.email",
                  "fixture@example.invalid")
        self._git("-C", str(self.root), "config", "user.name", "Fixture")
        (self.root / "docs" / "deep").mkdir(parents=True)
        (self.root / "empty").mkdir()
        (self.root / "fileonly").mkdir()
        (self.root / "hidden").mkdir()
        (self.root / "docs" / "zeta.txt").write_text("z", encoding="utf-8")
        (self.root / "docs" / "alpha.txt").write_text("a", encoding="utf-8")
        (self.root / "docs" / "deep" / "x.txt").write_text("x", encoding="utf-8")
        (self.root / "fileonly" / "one.txt").write_text("one", encoding="utf-8")
        (self.root / "hidden" / "secret.txt").write_text("secret", encoding="utf-8")
        (self.root / "non-directory").write_text("not a dir", encoding="utf-8")
        (self.root / "docs" / "external-link").symlink_to(
            self.base / "outside.txt"
        )
        (self.base / "outside.txt").write_text("outside secret", encoding="utf-8")
        (self.root / "alias").symlink_to(self.root / "docs")
        (self.root / "parent-alias").symlink_to(self.root / "docs")
        self._git("-C", str(self.root), "add", ".")
        self._git("-C", str(self.root), "commit", "-qm", "fixture")
        self.orch = Orchestrator(self.base / "orch-home")
        project = ProjectRegistry(self.orch.root).add(
            self.root, name="Disposable lister", profile="safe",
            review_mode="off", allow_commit=True, allow_push=False,
        )["project"]
        plan = build_single_task_plan(
            project, task_id="BOUNDED-LIST", goal="Test local list",
            allowed_paths=[
                "docs/", "docs/deep/", "empty/", "missing/",
                "non-directory/", "alias/", "parent-alias/deep/",
                "fileonly/one.txt",
            ],
            plan_revision="bounded-list-fixture",
        )
        # build_single_task_plan canonicalizes away trailing slashes. The
        # validated raw plan format preserves these explicit directory grants;
        # no direct SQLite or registry manipulation is used.
        plan["tasks"][0]["allowed_paths"] = [
            "docs/", "docs/deep/", "empty/", "missing/",
            "non-directory/", "alias/", "parent-alias/deep/",
            "fileonly/one.txt",
        ]
        plan_file = self.base / "plan.json"
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        claim = self.orch.claim("fixture-lister", project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED", claim)
        self.cap = Path(claim["capability_file"])
        self.binding = WorkerRunBinding(
            project_id=project["project_id"], writer_key=project["writer_key"],
            task_id=claim["task_id"], run_id=claim["run_id"],
            attempt=claim["attempt"],
        )
        self.authority = LocalRunSessionAuthority(self.orch)
        self.session = self.authority.open_session(self.binding, self.cap)

    def _git(self, *args):
        env = dict(os.environ)
        env.update({
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True, text=True, check=True, env=env, timeout=12,
        )

    def _scope(self):
        return LocalFileScope(
            self.authority, self.session, self.binding, self.cap,
        )

    def _request(self, target="docs", *, limit=WORKER_MAX_OUTPUT_BYTES,
                 binding=None, kind="file.list"):
        return WorkerOperationRequest(
            binding=binding or self.binding, operation_id="list-test",
            kind=kind, target=target, max_output_bytes=limit,
        )

    def test_allowed_directory_sorted_names_including_inert_symlink(self):
        with self._scope() as scope:
            req = self._request()
            result = LocalBoundedFileLister(scope).list(req)
        self.assertEqual(result.state, "SUCCEEDED", result)
        self.assertIs(validate_worker_outcome(req, result), result)
        self.assertEqual(
            json.loads(result.output),
            {"entries": ["alpha.txt", "deep", "external-link", "zeta.txt"]},
        )
        self.assertEqual(result.output, b'{"entries":["alpha.txt","deep","external-link","zeta.txt"]}')
        self.assertNotIn(b"outside secret", result.output)

    def test_empty_and_nested_directories(self):
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            for target, expected in (
                ("empty", b'{"entries":[]}'),
                ("docs/deep", b'{"entries":["x.txt"]}'),
            ):
                with self.subTest(target=target):
                    result = lister.list(self._request(target))
                    self.assertEqual(result.state, "SUCCEEDED", result)
                    self.assertEqual(result.output, expected)

    def test_json_limit_exact_and_one_byte_less(self):
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            enough = len(b'{"entries":[]}')
            success = lister.list(self._request("empty", limit=enough))
            denied = lister.list(self._request("empty", limit=enough-1))
        self.assertEqual(success.output, b'{"entries":[]}')
        self.assertEqual((denied.state, denied.output, denied.error_code),
                         ("REFUSED", b"", "file_list_too_large"))

    def test_oversize_directory_returns_no_partial_output(self):
        with self._scope() as scope:
            result = LocalBoundedFileLister(scope).list(self._request(limit=20))
        self.assertEqual((result.state, result.output, result.error_code),
                         ("REFUSED", b"", "file_list_too_large"))

    def test_many_entries_remain_bounded_at_full_limit(self):
        many = self.root / "many"
        many.mkdir()
        for index in range(1200):
            (many / (str(index).zfill(5) + "a" * 60)).touch()
        # This fixture uses the explicitly granted "docs/" directory.
        for source in many.iterdir():
            source.rename(self.root / "docs" / source.name)
        with self._scope() as scope:
            denied = LocalBoundedFileLister(scope).list(
                self._request(limit=WORKER_MAX_OUTPUT_BYTES)
            )
        self.assertEqual((denied.state, denied.output, denied.error_code),
                         ("REFUSED", b"", "file_list_too_large"))

    def test_no_inferred_grant_from_allowed_file_or_parent(self):
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            for target in ("fileonly", "hidden", "docs/deep/x.txt", "docs/other"):
                with self.subTest(target=target):
                    result = lister.list(self._request(target))
                    self.assertEqual((result.state, result.output),
                                     ("REFUSED", b""))

    def test_cross_run_and_invalid_target(self):
        foreign = dataclasses.replace(self.binding, run_id="foreign")
        with self._scope() as scope:
            result = LocalBoundedFileLister(scope).list(
                self._request(binding=foreign)
            )
        self.assertEqual((result.state, result.output), ("REFUSED", b""))
        for target in ("/etc", "../docs", "docs/../hidden", "docs//deep",
                       "docs\\deep", "docs/"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self._request(target)

    def test_symlink_parent_and_leaf_refused(self):
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            for target in ("alias", "parent-alias/deep"):
                with self.subTest(target=target):
                    result = lister.list(self._request(target))
                    self.assertEqual((result.state, result.output),
                                     ("REFUSED", b""))

    def test_missing_and_non_directory_refused(self):
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            for target in ("missing", "non-directory"):
                with self.subTest(target=target):
                    result = lister.list(self._request(target))
                    self.assertEqual((result.state, result.output),
                                     ("REFUSED", b""))
        self.assertFalse((self.root / "missing").exists())

    def test_capability_revoked_during_enumeration(self):
        orig = module._entry_name
        changed = [False]

        def revoke(entry):
            if not changed[0]:
                changed[0] = True
                self.cap.chmod(0o644)
            return orig(entry)

        with self._scope() as scope, mock.patch.object(
            module, "_entry_name", side_effect=revoke
        ):
            result = LocalBoundedFileLister(scope).list(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_branch_drift_after_enumeration_denied(self):
        orig = module._entry_name
        changed = [False]

        def drift(entry):
            if not changed[0]:
                changed[0] = True
                self._git("-C", str(self.root), "switch", "-q", "-c", "drift")
            return orig(entry)

        with self._scope() as scope, mock.patch.object(
            module, "_entry_name", side_effect=drift
        ):
            result = LocalBoundedFileLister(scope).list(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_leaf_directory_symlink_swap_after_scan_denied(self):
        orig = module._entry_name
        changed = [False]

        def swap(entry):
            if not changed[0]:
                changed[0] = True
                (self.root / "docs").rename(self.root / "old-docs")
                (self.root / "docs").symlink_to(self.root / "old-docs")
            return orig(entry)

        with self._scope() as scope, mock.patch.object(
            module, "_entry_name", side_effect=swap
        ):
            result = LocalBoundedFileLister(scope).list(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_directory_mutation_during_scan_denied(self):
        orig = module._entry_name
        changed = [False]

        def mutate(entry):
            if not changed[0]:
                changed[0] = True
                (self.root / "docs" / "new-file").write_bytes(b"new")
            return orig(entry)

        with self._scope() as scope, mock.patch.object(
            module, "_entry_name", side_effect=mutate
        ):
            result = LocalBoundedFileLister(scope).list(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_changed_inode_on_fresh_lookup_denied(self):
        original = module._entry_name
        changed = [False]

        def replace(entry):
            if not changed[0]:
                changed[0] = True
                (self.root / "docs").rename(self.root / "old-docs")
                (self.root / "docs").mkdir()
            return original(entry)

        with self._scope() as scope, mock.patch.object(
            module, "_entry_name", side_effect=replace
        ):
            result = LocalBoundedFileLister(scope).list(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_scanner_os_error_is_sanitized_and_handles_closed(self):
        with self._scope() as scope:
            with mock.patch.object(
                module, "_entry_name",
                side_effect=OSError("private internal filesystem details"),
            ):
                result = LocalBoundedFileLister(scope).list(self._request())
            self.assertEqual(scope._active_handles, 0)
        self.assertEqual((result.state, result.output, result.error_code),
                         ("FAILED", b"", "file_list_io_failed"))
        self.assertNotIn("private", result.error_code)

    def test_unsupported_scandir_fd_is_refused(self):
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            with mock.patch.object(module.os, "supports_fd", set()):
                result = lister.list(self._request())
        self.assertEqual((result.state, result.output, result.error_code),
                         ("REFUSED", b"", "file_list_unsupported_platform"))

    def test_close_refusal_and_handle_ownership(self):
        scope = self._scope()
        with scope.open_listing(self._request()) as fd:
            self.assertTrue(os.fstat(fd))
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_busy"):
                scope.close()
        with self.assertRaises(OSError):
            os.fstat(fd)
        scope.close()
        result = LocalBoundedFileLister(scope).list(self._request())
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_unrelated_kinds_cannot_enter_listing_scope(self):
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            with mock.patch.object(scope, "open_listing") as opened:
                for kind, target in (
                    ("file.read", "docs"), ("file.patch", "docs"),
                    ("process.start", "profile"),
                ):
                    with self.subTest(kind=kind):
                        result = lister.list(self._request(target, kind=kind))
                        self.assertEqual((result.state, result.output),
                                         ("REFUSED", b""))
                opened.assert_not_called()

    def test_invalid_inputs_and_no_exec_surface(self):
        with self.assertRaisesRegex(ValueError, "bridge_file_list_invalid_scope"):
            LocalBoundedFileLister(object())
        with self._scope() as scope:
            lister = LocalBoundedFileLister(scope)
            with self.assertRaisesRegex(ValueError, "bridge_file_list_invalid_request"):
                lister.list(object())
            self.assertFalse(hasattr(lister, "patch"))
            self.assertFalse(hasattr(lister, "start_process"))
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)}
        imported.update(alias.name for node in ast.walk(tree)
                        if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse({"subprocess", "git_transport", "dispatcher"} & imported)


if __name__ == "__main__":
    unittest.main()
