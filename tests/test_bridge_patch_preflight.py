"""Original-content preflight through disposable live-authority fixtures."""

import dataclasses
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch import bridge_file_read
from orch import bridge_patch_preflight as module
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_scope import LocalFileScope
from orch.bridge_patch_preflight import FilePatchBaseline, LocalFilePatchPreflight
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import (
    WorkerOperationOutcome, WorkerOperationRequest, WorkerRunBinding,
    validate_worker_outcome,
)


class FilePatchBaselineTests(unittest.TestCase):
    def test_baseline_requires_canonical_sha256_and_bounded_integer_size(self):
        for digest in (None, True, 4, b"a" * 64, "a" * 63, "g" * 64,
                       "A" * 64, "a" * 64 + "\n"):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                FilePatchBaseline(digest, 0)
        for size in (None, True, False, "0", 0.0, -1, 65537):
            with self.subTest(size=size), self.assertRaises(ValueError):
                FilePatchBaseline("a" * 64, size)
        self.assertEqual(FilePatchBaseline("a" * 64, 0).size_bytes, 0)
        self.assertEqual(FilePatchBaseline("a" * 64, 65536).size_bytes, 65536)


class LocalFilePatchPreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="orch-patch-preflight-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "repo"
        self._git("init", "-q", "-b", "main", str(self.root))
        self._git("-C", str(self.root), "config", "user.email", "fixture@example.invalid")
        self._git("-C", str(self.root), "config", "user.name", "Fixture")
        (self.root / "docs").mkdir()
        for target, data in (("README.md", b"safe\n"), ("empty.txt", b""),
                             ("docs/inside.txt", b"nested\n"),
                             ("big.txt", b"x" * 65536), ("secret.txt", b"private\n")):
            (self.root / target).write_bytes(data)
        self._git("-C", str(self.root), "add", ".")
        self._git("-C", str(self.root), "commit", "-qm", "fixture")
        self.orch = Orchestrator(self.base / "home")
        project = ProjectRegistry(self.orch.root).add(
            self.root, name="Disposable preflight", profile="safe",
            review_mode="off", allow_commit=True, allow_push=False,
        )["project"]
        plan = build_single_task_plan(
            project, task_id="PATCH-PREFLIGHT", goal="Check original content only",
            allowed_paths=["README.md", "empty.txt", "docs/inside.txt", "big.txt",
                           "missing.txt", "link.txt", "docs-link/inside.txt", "fifo.txt"],
            plan_revision="patch-preflight-fixture",
        )
        plan_file = self.base / "plan.json"
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        claim = self.orch.claim("fixture", project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED", claim)
        self.cap = Path(claim["capability_file"])
        self.binding = WorkerRunBinding(
            project_id=project["project_id"], writer_key=project["writer_key"],
            task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"],
        )
        self.authority = LocalRunSessionAuthority(self.orch)
        self.session = self.authority.open_session(self.binding, self.cap)

    def _git(self, *args):
        env = dict(os.environ)
        env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                   GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              capture_output=True, text=True, check=True,
                              env=env, timeout=12)

    def _scope(self):
        return LocalFileScope(self.authority, self.session, self.binding, self.cap)

    def _request(self, target="README.md", **changes):
        values = dict(binding=self.binding, operation_id="preflight-1",
                      kind="file.read", target=target)
        values.update(changes)
        return WorkerOperationRequest(**values)

    def _baseline(self, data=b"safe\n"):
        return FilePatchBaseline(hashlib.sha256(data).hexdigest(), len(data))

    def _assert_empty(self, request, result, state):
        self.assertEqual((result.state, result.output), (state, b""))
        self.assertIs(validate_worker_outcome(request, result), result)

    def test_matching_nested_empty_and_boundary_originals_without_mutation(self):
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            for target, data, limit in (("README.md", b"safe\n", 5),
                                        ("empty.txt", b"", 1),
                                        ("docs/inside.txt", b"nested\n", 7),
                                        ("big.txt", b"x" * 65536, 65536)):
                before = (self.root / target).stat()
                request = self._request(target, max_output_bytes=limit)
                result = checker.check(request, self._baseline(data))
                self._assert_empty(request, result, "SUCCEEDED")
                self.assertEqual((self.root / target).read_bytes(), data)
                after = (self.root / target).stat()
                self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                                 (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertEqual(self._git("-C", str(self.root), "status", "--porcelain").stdout, "")

    def test_size_and_digest_mismatches_refuse_without_original_content(self):
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            for baseline in (self._baseline(b"other"),
                             FilePatchBaseline(hashlib.sha256(b"safe\n").hexdigest(), 4)):
                request = self._request()
                self._assert_empty(request, checker.check(request, baseline), "REFUSED")

    def test_two_full_reads_remain_bounded_and_return_no_source_bytes(self):
        original = bridge_file_read._read_chunk
        transferred = []

        def observe(fd, size):
            self.assertLessEqual(size, 8192)
            data = original(fd, size)
            transferred.append(len(data))
            return data

        with self._scope() as scope, mock.patch.object(bridge_file_read, "_read_chunk",
                                                     side_effect=observe):
            request = self._request("big.txt")
            result = LocalFilePatchPreflight(scope).check(request, self._baseline(b"x" * 65536))
            self._assert_empty(request, result, "SUCCEEDED")
        self.assertEqual(sum(transferred), 2 * 65536)

    def test_unresolved_observation_is_not_success_or_a_retry(self):
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            request = self._request()
            unknown = WorkerOperationOutcome(operation_id=request.operation_id,
                                             state="UNKNOWN", error_code="readback_required")
            with mock.patch.object(checker._reader, "read", return_value=unknown) as read:
                self._assert_empty(request, checker.check(request, self._baseline()), "UNKNOWN")
                self.assertEqual(read.call_count, 1)

    def test_oversize_and_request_byte_limit_are_enforced(self):
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            request = self._request(max_output_bytes=4)
            self._assert_empty(request, checker.check(request, self._baseline()), "REFUSED")
            (self.root / "big.txt").write_bytes(b"x" * 65537)
            request = self._request("big.txt")
            self._assert_empty(request, checker.check(request, self._baseline(b"x" * 65536)),
                               "REFUSED")

    def test_cross_run_and_out_of_allowlist_do_not_authorize_read(self):
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            for request in (self._request(binding=dataclasses.replace(self.binding, run_id="foreign")),
                            self._request("secret.txt")):
                self._assert_empty(request, checker.check(request, self._baseline()), "REFUSED")

    def test_traversal_is_rejected_by_unchanged_v1_contract(self):
        for target in ("/etc/passwd", "../README.md", "docs/../README.md", "./README.md"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self._request(target)

    def test_symlink_missing_and_nonregular_targets_fail_closed(self):
        (self.root / "link.txt").symlink_to(self.root / "README.md")
        (self.root / "docs-link").symlink_to(self.root / "docs")
        os.mkfifo(self.root / "fifo.txt")
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            for target in ("link.txt", "docs-link/inside.txt", "missing.txt", "fifo.txt"):
                request = self._request(target)
                self._assert_empty(request, checker.check(request, self._baseline()), "REFUSED")
        self.assertFalse((self.root / "missing.txt").exists())

    def test_actual_patch_list_and_process_requests_remain_unsupported(self):
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            with mock.patch.object(scope, "open_readonly", side_effect=AssertionError("unexpected I/O")):
                for kind, target in (("file.patch", "README.md"), ("file.list", "docs"),
                                     ("process.start", "profile")):
                    request = self._request(target, kind=kind)
                    self._assert_empty(request, checker.check(request, self._baseline()), "REFUSED")

    def test_repeated_operation_id_rechecks_content_and_live_capability(self):
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            request = self._request()
            self._assert_empty(request, checker.check(request, self._baseline()), "SUCCEEDED")
            (self.root / "README.md").write_bytes(b"evil\n")
            self._assert_empty(request, checker.check(request, self._baseline()), "REFUSED")
            self._assert_empty(request, checker.check(request, self._baseline(b"evil\n")), "SUCCEEDED")
            self.cap.chmod(0o644)
            self._assert_empty(request, checker.check(request, self._baseline(b"evil\n")), "REFUSED")

    def test_revocation_between_comparison_and_readback_refuses(self):
        original = module.hashlib.sha256

        def revoke(data):
            result = original(data)
            if data == b"safe\n":
                self.cap.chmod(0o644)
            return result

        baseline = self._baseline()
        with self._scope() as scope, mock.patch.object(module.hashlib, "sha256", side_effect=revoke):
            request = self._request()
            self._assert_empty(request, LocalFilePatchPreflight(scope).check(request, baseline), "REFUSED")

    def test_content_drift_between_comparison_and_readback_refuses(self):
        original = module.hashlib.sha256

        def alter(data):
            result = original(data)
            if data == b"safe\n":
                (self.root / "README.md").write_bytes(b"evil\n")
            return result

        baseline = self._baseline()
        with self._scope() as scope, mock.patch.object(module.hashlib, "sha256", side_effect=alter):
            request = self._request()
            self._assert_empty(request, LocalFilePatchPreflight(scope).check(request, baseline), "REFUSED")

    def test_branch_drift_before_call_refuses(self):
        with self._scope() as scope:
            self._git("-C", str(self.root), "switch", "-q", "-c", "drift")
            request = self._request()
            self._assert_empty(request, LocalFilePatchPreflight(scope).check(request, self._baseline()),
                               "REFUSED")

    def test_root_substitution_refuses(self):
        with self._scope() as scope:
            self.root.rename(self.base / "original-repo")
            self.root.mkdir()
            (self.root / "README.md").write_bytes(b"safe\n")
            request = self._request()
            self._assert_empty(request, LocalFilePatchPreflight(scope).check(request, self._baseline()),
                               "REFUSED")

    def test_leaf_swap_during_read_refuses_without_external_bytes(self):
        original = bridge_file_read._read_chunk
        outside = self.base / "outside.txt"
        outside.write_bytes(b"external-private-data")
        changed = []

        def swap(fd, size):
            data = original(fd, size)
            if data and not changed:
                changed.append(True)
                (self.root / "README.md").unlink()
                (self.root / "README.md").symlink_to(outside)
            return data

        with self._scope() as scope, mock.patch.object(bridge_file_read, "_read_chunk", side_effect=swap):
            request = self._request()
            self._assert_empty(request, LocalFilePatchPreflight(scope).check(request, self._baseline()),
                               "REFUSED")

    def test_io_error_is_sanitized_and_descriptors_are_closed(self):
        seen = []

        def fail(fd, size):
            seen.append(fd)
            raise OSError("private internal detail")

        with self._scope() as scope, mock.patch.object(bridge_file_read, "_read_chunk", side_effect=fail):
            request = self._request()
            result = LocalFilePatchPreflight(scope).check(request, self._baseline())
            self._assert_empty(request, result, "FAILED")
            self.assertNotIn("private", result.error_code)
        self.assertTrue(seen)
        for fd in seen:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_closed_scope_and_unknown_session_refuse(self):
        scope = self._scope()
        scope.close()
        request = self._request()
        self._assert_empty(request, LocalFilePatchPreflight(scope).check(request, self._baseline()),
                           "REFUSED")
        with self._scope() as active:
            self.authority.close_session(self.session)
            self._assert_empty(request, LocalFilePatchPreflight(active).check(request, self._baseline()),
                               "REFUSED")

    def test_invalid_inputs_fail_before_any_file_access(self):
        with self.assertRaises(ValueError):
            LocalFilePatchPreflight(object())
        with self._scope() as scope:
            checker = LocalFilePatchPreflight(scope)
            with mock.patch.object(scope, "open_readonly", side_effect=AssertionError("unexpected I/O")):
                for request, baseline in ((object(), self._baseline()), (self._request(), object())):
                    with self.assertRaises(ValueError):
                        checker.check(request, baseline)


if __name__ == "__main__":
    unittest.main()
