"""Disposable, live-authority tests for bounded local file.read content."""

import ast
import dataclasses
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch import bridge_file_read as module
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_read import LocalBoundedFileReader
from orch.bridge_file_scope import LocalFileScope
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import (
    WORKER_MAX_OUTPUT_BYTES, WorkerOperationRequest, WorkerRunBinding,
    validate_worker_outcome,
)


class LocalBoundedFileReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-bounded-read-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self._git("init", "-q", "-b", "main", str(self.root))
        self._git("-C", str(self.root), "config", "user.email",
                  "fixture@example.invalid")
        self._git("-C", str(self.root), "config", "user.name", "Fixture")
        (self.root / "docs").mkdir()
        (self.root / "README.md").write_bytes(b"safe\n")
        (self.root / "docs" / "inside.txt").write_bytes(b"nested\n")
        (self.root / "empty.txt").write_bytes(b"")
        (self.root / "big.txt").write_bytes(b"x" * (WORKER_MAX_OUTPUT_BYTES + 10))
        (self.root / "secret.txt").write_bytes(b"not-allowed\n")
        self._git("-C", str(self.root), "add", ".")
        self._git("-C", str(self.root), "commit", "-qm", "fixture")
        self.orch = Orchestrator(self.base / "orch-home")
        project = ProjectRegistry(self.orch.root).add(
            self.root, name="Disposable reader", profile="safe",
            review_mode="off", allow_commit=True, allow_push=False,
        )["project"]
        plan = build_single_task_plan(
            project, task_id="BOUNDED-READ", goal="Test bounded read adapter",
            allowed_paths=[
                "README.md", "docs/inside.txt", "empty.txt", "big.txt",
                "missing.txt", "link.txt", "docs-link/inside.txt",
                "nonregular.txt",
            ], plan_revision="bounded-read-fixture",
        )
        plan_file = self.base / "plan.json"
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        claim = self.orch.claim("fixture-reader", project_id=project["project_id"])
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

    def _request(self, target="README.md", *, limit=WORKER_MAX_OUTPUT_BYTES,
                 kind="file.read", binding=None):
        return WorkerOperationRequest(
            binding=binding or self.binding, operation_id="read-test",
            kind=kind, target=target, max_output_bytes=limit,
        )

    def test_existing_nested_and_empty_read(self):
        with self._scope() as scope:
            reader = LocalBoundedFileReader(scope)
            for target, data in (("README.md", b"safe\n"),
                                 ("docs/inside.txt", b"nested\n"),
                                 ("empty.txt", b"")):
                with self.subTest(target=target):
                    request = self._request(target)
                    result = reader.read(request)
                    self.assertEqual(result.state, "SUCCEEDED", result)
                    self.assertEqual(result.output, data)
                    self.assertIs(validate_worker_outcome(request, result), result)

    def test_exact_limit_and_one_byte_short_are_distinct(self):
        with self._scope() as scope:
            reader = LocalBoundedFileReader(scope)
            self.assertEqual(reader.read(self._request(limit=5)).output, b"safe\n")
            denied = reader.read(self._request(limit=4))
            self.assertEqual((denied.state, denied.output, denied.error_code),
                             ("REFUSED", b"", "file_read_too_large"))

    def test_full_64k_limit_uses_bounded_chunks(self):
        (self.root / "big.txt").write_bytes(b"x" * WORKER_MAX_OUTPUT_BYTES)
        sizes = []
        original = module._read_chunk

        def track(fd, size):
            sizes.append(size)
            return original(fd, size)

        with self._scope() as scope, mock.patch.object(module, "_read_chunk", side_effect=track):
            result = LocalBoundedFileReader(scope).read(self._request("big.txt"))
        self.assertEqual((result.state, len(result.output)),
                         ("SUCCEEDED", WORKER_MAX_OUTPUT_BYTES))
        self.assertGreater(len(sizes), 2)
        self.assertLessEqual(max(sizes), module._READ_CHUNK_BYTES)

    def test_oversize_precheck_never_calls_read_and_returns_no_partial_data(self):
        with self._scope() as scope:
            with mock.patch.object(module, "_read_chunk", wraps=module._read_chunk) as spy:
                result = LocalBoundedFileReader(scope).read(self._request("big.txt"))
            self.assertEqual(result.state, "REFUSED")
            self.assertEqual(result.error_code, "file_read_too_large")
            self.assertEqual(result.output, b"")
            spy.assert_not_called()

    def test_content_grows_between_stat_and_read(self):
        original = module._read_chunk
        changed = [False]

        def grow(fd, size):
            if not changed[0]:
                changed[0] = True
                with (self.root / "README.md").open("ab") as output:
                    output.write(b"growth")
            return original(fd, size)

        with self._scope() as scope, mock.patch.object(module, "_read_chunk", side_effect=grow):
            result = LocalBoundedFileReader(scope).read(self._request(limit=5))
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output),
                         ("REFUSED", b""))

    def test_inplace_content_change_is_detected_by_metadata(self):
        original = module._read_chunk
        changed = [False]

        def alter(fd, size):
            if not changed[0]:
                changed[0] = True
                target = self.root / "README.md"
                before = target.stat()
                target.write_bytes(b"evil\n")
                os.utime(target, ns=(before.st_atime_ns,
                                      before.st_mtime_ns + 2_000_000_000))
            return original(fd, size)

        with self._scope() as scope, mock.patch.object(module, "_read_chunk", side_effect=alter):
            result = LocalBoundedFileReader(scope).read(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output, result.error_code),
                         ("REFUSED", b"", "file_read_changed"))

    def test_capability_revoked_after_bytes_read_is_denied(self):
        original = module._read_chunk
        changed = [False]

        def revoke(fd, size):
            result = original(fd, size)
            if result and not changed[0]:
                changed[0] = True
                self.cap.chmod(0o644)
            return result

        with self._scope() as scope, mock.patch.object(module, "_read_chunk", side_effect=revoke):
            result = LocalBoundedFileReader(scope).read(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_branch_drift_after_bytes_read_is_denied(self):
        original = module._read_chunk
        changed = [False]

        def drift(fd, size):
            result = original(fd, size)
            if result and not changed[0]:
                changed[0] = True
                self._git("-C", str(self.root), "switch", "-q", "-c", "drift")
            return result

        with self._scope() as scope, mock.patch.object(module, "_read_chunk", side_effect=drift):
            result = LocalBoundedFileReader(scope).read(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_leaf_swap_after_bytes_read_denied_without_external_bytes(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"external-secret")
        original = module._read_chunk
        changed = [False]

        def swap(fd, size):
            result = original(fd, size)
            if result and not changed[0]:
                changed[0] = True
                (self.root / "README.md").rename(self.root / "old-readme.txt")
                (self.root / "README.md").symlink_to(outside)
            return result

        with self._scope() as scope, mock.patch.object(module, "_read_chunk", side_effect=swap):
            result = LocalBoundedFileReader(scope).read(self._request())
        self.assertTrue(changed[0])
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_cross_run_and_out_of_allowlist_denied(self):
        foreign = dataclasses.replace(self.binding, run_id="foreign-run")
        with self._scope() as scope:
            reader = LocalBoundedFileReader(scope)
            for request in (self._request(binding=foreign),
                            self._request("secret.txt")):
                with self.subTest(request=request.target):
                    result = reader.read(request)
                    self.assertEqual((result.state, result.output),
                                     ("REFUSED", b""))

    def test_traversal_and_absolute_target_rejected_by_v1_contract(self):
        for target in ("/etc/passwd", "../README.md", "docs/../inside.txt",
                       "./README.md", "docs//inside.txt"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self._request(target)

    def test_symlink_leaf_and_parent_denied(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"external")
        (self.root / "link.txt").symlink_to(outside)
        (self.root / "docs-link").symlink_to(self.root / "docs")
        with self._scope() as scope:
            reader = LocalBoundedFileReader(scope)
            for target in ("link.txt", "docs-link/inside.txt"):
                result = reader.read(self._request(target))
                self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_missing_or_nonregular_refused_without_creation_or_blocking(self):
        os.mkfifo(self.root / "nonregular.txt")
        with self._scope() as scope:
            reader = LocalBoundedFileReader(scope)
            for target in ("missing.txt", "nonregular.txt"):
                result = reader.read(self._request(target))
                self.assertEqual((result.state, result.output), ("REFUSED", b""))
        self.assertFalse((self.root / "missing.txt").exists())

    def test_unsupported_kinds_cannot_touch_file_scope(self):
        with self._scope() as scope:
            reader = LocalBoundedFileReader(scope)
            with mock.patch.object(scope, "open_readonly") as opened:
                for kind, target in (("file.list", "docs"),
                                     ("file.patch", "README.md"),
                                     ("process.start", "profile")):
                    result = reader.read(self._request(target, kind=kind))
                    self.assertEqual((result.state, result.output),
                                     ("REFUSED", b""))
                opened.assert_not_called()

    def test_read_os_error_is_sanitized_and_fd_closed(self):
        seen = []

        def break_read(fd, size):
            seen.append(fd)
            raise OSError("internal private operating system detail")

        with self._scope() as scope:
            with mock.patch.object(module, "_read_chunk", side_effect=break_read):
                result = LocalBoundedFileReader(scope).read(self._request())
        self.assertEqual((result.state, result.output, result.error_code),
                         ("FAILED", b"", "file_read_io_failed"))
        self.assertTrue(seen)
        with self.assertRaises(OSError):
            os.fstat(seen[0])

    def test_closed_scope_refused(self):
        scope = self._scope()
        scope.close()
        result = LocalBoundedFileReader(scope).read(self._request())
        self.assertEqual((result.state, result.output), ("REFUSED", b""))

    def test_invalid_inputs_and_no_mutating_process_surface(self):
        with self.assertRaisesRegex(ValueError, "bridge_file_read_invalid_scope"):
            LocalBoundedFileReader(object())
        with self._scope() as scope:
            reader = LocalBoundedFileReader(scope)
            with self.assertRaisesRegex(ValueError, "bridge_file_read_invalid_request"):
                reader.read(object())
            self.assertFalse(hasattr(reader, "list"))
            self.assertFalse(hasattr(reader, "patch"))
            self.assertFalse(hasattr(reader, "start_process"))
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)}
        imported.update(alias.name for node in ast.walk(tree)
                        if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse({"subprocess", "git_transport", "dispatcher"} & imported)


if __name__ == "__main__":
    unittest.main()
