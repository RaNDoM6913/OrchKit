"""Deterministic disposable P1-C2 file scope and descriptor lifetime tests."""

import ast
import dataclasses
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch import bridge_file_scope as module
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_scope import LocalFileScope
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding


class LocalFileScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-file-scope-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.orch = Orchestrator(self.base / "orch-home")
        registry = ProjectRegistry(self.orch.root)
        self.projects, self.claims, self.bindings = {}, {}, {}
        for label in ("a", "b"):
            root = self.base / ("repo-" + label)
            self._git("init", "-q", "-b", "main", str(root))
            self._git("-C", str(root), "config", "user.email", "fixture@example.invalid")
            self._git("-C", str(root), "config", "user.name", "Fixture")
            (root / "docs").mkdir()
            (root / "docs" / "inside.txt").write_text("in-scope\n")
            (root / "README.md").write_text("safe-" + label + "\n")
            (root / "secret.txt").write_text("out-of-scope\n")
            self._git("-C", str(root), "add", ".")
            self._git("-C", str(root), "commit", "-qm", "fixture")
            project = registry.add(
                root, name="Disposable " + label, profile="safe",
                review_mode="off", allow_commit=True, allow_push=False,
            )["project"]
            self.projects[label] = project
            plan = build_single_task_plan(
                project, task_id="FILE-SCOPE-" + label.upper(),
                goal="Test local file scope",
                allowed_paths=[
                    "README.md", "docs/inside.txt", "missing.txt",
                    "missing-parent/child.txt", "link.txt", "swap.txt",
                ],
                plan_revision="file-scope-" + label,
            )
            plan_path = self.base / (label + "-plan.json")
            write_plan(plan_path, plan)
            self.orch.load_plan(plan_path)
            claim = self.orch.claim("fixture-" + label, project_id=project["project_id"])
            self.assertEqual(claim["status"], "CLAIMED", claim)
            self.claims[label] = claim
            self.bindings[label] = WorkerRunBinding(
                project_id=project["project_id"],
                writer_key=project["writer_key"],
                task_id=claim["task_id"],
                run_id=claim["run_id"],
                attempt=claim["attempt"],
            )
        self.authority = LocalRunSessionAuthority(self.orch)
        self.sessions = {
            label: self.authority.open_session(
                self.bindings[label], Path(self.claims[label]["capability_file"]),
            ) for label in ("a", "b")
        }

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

    def _root(self, label="a"):
        return Path(self.projects[label]["root"])

    def _cap(self, label="a"):
        return Path(self.claims[label]["capability_file"])

    def _scope(self, label="a"):
        return LocalFileScope(
            self.authority, self.sessions[label], self.bindings[label],
            self._cap(label),
        )

    def _request(self, target="README.md", label="a", kind="file.read"):
        return WorkerOperationRequest(
            binding=self.bindings[label], operation_id="scope-test",
            kind=kind, target=target,
        )

    def test_existing_regular_file_and_nested_file_are_descriptor_anchored(self):
        with self._scope() as scope:
            for target, expected in (("README.md", b"safe-a\n"),
                                     ("docs/inside.txt", b"in-scope\n")):
                with scope.open_readonly(self._request(target)) as fd:
                    self.assertEqual(os.read(fd, 100), expected)
                    self.assertFalse(os.get_inheritable(fd))
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertEqual(scope._root_fd >= 0, True)
        with self.assertRaisesRegex(ValueError, "bridge_file_scope_closed"):
            with scope.open_readonly(self._request()):
                pass

    def test_distinct_project_and_cross_run_denial(self):
        with self._scope("a") as a, self._scope("b") as b:
            with a.open_readonly(self._request(label="a")) as fd:
                self.assertEqual(os.read(fd, 100), b"safe-a\n")
            with b.open_readonly(self._request(label="b")) as fd:
                self.assertEqual(os.read(fd, 100), b"safe-b\n")
            with self.assertRaisesRegex(ValueError, "worker_operation_scope_mismatch"):
                with a.open_readonly(self._request(label="b")):
                    pass

    def test_forbidden_existing_file_rejected(self):
        with self._scope() as scope:
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_path_denied"):
                with scope.open_readonly(self._request("secret.txt")):
                    pass

    def test_absolute_and_traversal_paths_invalid_before_scope(self):
        for target in ("/etc/passwd", "../README.md", "docs/../README.md",
                       "docs//inside.txt", "./README.md", "docs/./inside.txt",
                       "docs\\inside.txt"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "worker_operation_invalid_target"):
                    self._request(target)

    def test_missing_leaf_and_missing_parent_denied_without_creation(self):
        with self._scope() as scope:
            for target in ("missing.txt", "missing-parent/child.txt"):
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ValueError, "bridge_file_scope_path_denied"):
                        with scope.open_readonly(self._request(target)):
                            pass
                    self.assertFalse((self._root() / target).exists())

    def test_symlink_leaf_and_symlink_ancestor_denied(self):
        root = self._root()
        outside = self.base / "outside.txt"
        outside.write_text("external\n")
        (root / "link.txt").symlink_to(outside)
        (root / "docs").rename(root / "docs-real")
        (root / "docs").symlink_to(self.base, target_is_directory=True)
        with self._scope() as scope:
            for target in ("link.txt", "docs/inside.txt"):
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ValueError, "bridge_file_scope_"):
                        with scope.open_readonly(self._request(target)):
                            pass

    def test_nonregular_fifo_denied_without_opening(self):
        path = self._root() / "swap.txt"
        os.mkfifo(path)
        with self._scope() as scope:
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_unsafe_type"):
                with scope.open_readonly(self._request("swap.txt")):
                    pass

    def test_unsupported_list_patch_and_process_kinds(self):
        with self._scope() as scope:
            for kind, target in (("file.list", "docs"),
                                 ("file.patch", "README.md"),
                                 ("process.start", "profile")):
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(ValueError, "bridge_file_scope_kind_unsupported"):
                        with scope.open_readonly(self._request(target, kind=kind)):
                            pass

    def test_leaf_swapped_to_symlink_between_precheck_and_open(self):
        root = self._root()
        outside = self.base / "outside.txt"
        outside.write_text("secret\n")
        original = module._entry
        changed = [False]

        def swap(parent, name, *, directory):
            info = original(parent, name, directory=directory)
            if name == "README.md" and not changed[0]:
                changed[0] = True
                (root / "README.md").rename(root / "README-old.md")
                (root / "README.md").symlink_to(outside)
            return info

        with self._scope() as scope, mock.patch.object(module, "_entry", side_effect=swap):
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_path_denied"):
                with scope.open_readonly(self._request()):
                    pass
        self.assertTrue(changed[0])

    def test_directory_swapped_after_fd_open_is_denied(self):
        root = self._root()
        original = module._entry
        changed = [False]

        def swap(parent, name, *, directory):
            info = original(parent, name, directory=directory)
            if name == "inside.txt" and not changed[0]:
                changed[0] = True
                (root / "docs").rename(root / "docs-old")
                (root / "docs").symlink_to(self.base, target_is_directory=True)
            return info

        with self._scope() as scope, mock.patch.object(module, "_entry", side_effect=swap):
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_"):
                with scope.open_readonly(self._request("docs/inside.txt")):
                    pass
        self.assertTrue(changed[0])

    def test_leaf_swapped_after_open_before_identity_recheck(self):
        root = self._root()
        outside = self.base / "outside.txt"
        outside.write_text("other\n")
        original = module._entry
        count = [0]

        def swap(parent, name, *, directory):
            if name == "README.md":
                count[0] += 1
                if count[0] == 2:
                    (root / "README.md").rename(root / "README-original.md")
                    (root / "README.md").symlink_to(outside)
            return original(parent, name, directory=directory)

        with self._scope() as scope, mock.patch.object(module, "_entry", side_effect=swap):
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_"):
                with scope.open_readonly(self._request()):
                    pass
        self.assertEqual(count[0], 2)

    def test_root_in_place_inode_substitution_refused(self):
        with self._scope() as scope:
            self._root().rename(self.base / "previous-root")
            self._root().mkdir()
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_root_changed"):
                scope._check_root_identity()
            with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
                with scope.open_readonly(self._request()):
                    pass

    def test_unsupported_primitives_fail_closed(self):
        with mock.patch.object(module.os, "supports_dir_fd", frozenset()):
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_unsupported"):
                self._scope()
        with self._scope() as scope:
            with mock.patch.object(module.os, "supports_follow_symlinks", frozenset()):
                with self.assertRaisesRegex(ValueError, "bridge_file_scope_unsupported"):
                    with scope.open_readonly(self._request()):
                        pass

    def test_revoke_or_drift_after_scope_creation(self):
        with self._scope() as scope:
            self._cap().chmod(0o644)
            with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
                with scope.open_readonly(self._request()):
                    pass
        self._cap().chmod(0o600)
        with self._scope() as scope:
            self._git("-C", str(self._root()), "switch", "-q", "-c", "other")
            with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
                with scope.open_readonly(self._request()):
                    pass

    def test_close_busy_and_exception_closes_borrowed_file(self):
        with self._scope() as scope:
            with self.assertRaisesRegex(RuntimeError, "caller"):
                with scope.open_readonly(self._request()) as fd:
                    with self.assertRaisesRegex(ValueError, "bridge_file_scope_busy"):
                        scope.close()
                    raise RuntimeError("caller")
            with self.assertRaises(OSError):
                os.fstat(fd)
        scope.close()

    def test_no_execution_or_file_adapter_imports(self):
        source = Path(module.__file__)
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)}
        imported.update(alias.name for node in ast.walk(tree)
                        if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse({"subprocess", "git_transport", "dispatcher"} & imported)
        self.assertFalse(hasattr(module.LocalFileScope, "read"))
        self.assertFalse(hasattr(module.LocalFileScope, "patch"))
        self.assertFalse(hasattr(module.LocalFileScope, "start_process"))

    def test_patch_recheck_uses_uncached_live_git_and_capability_authority(self):
        with self._scope() as scope:
            with scope.open_patch_target(self._request(kind="file.patch")) as target:
                self._cap().chmod(0o644)
                with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
                    target.recheck()
                self._cap().chmod(0o600)
                self._git("-C", str(self._root()), "switch", "-q", "-c", "patch-drift")
                with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
                    target.recheck()


class PatchScopeDescriptorTests(unittest.TestCase):
    """Fast descriptor fault matrix; production authority is never cached."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-patch-scope-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "file").write_bytes(b"original")
        self.binding = WorkerRunBinding("fixture", "writer", "task", "run", 1)
        self.request = WorkerOperationRequest(self.binding, "operation", "file.patch", "docs/file")
        self.allowed = ["docs/file", "missing", "missing-parent/file", "link", "fifo"]
        self.authority = object.__new__(LocalRunSessionAuthority)
        self.live = mock.patch.object(LocalFileScope, "_live_scope",
                                      side_effect=lambda: (self.root, list(self.allowed)))
        self.live_mock = self.live.start()
        self.addCleanup(self.live.stop)

    def _scope(self):
        return LocalFileScope(self.authority, "session", self.binding, self.root / "unused-cap")

    def test_patch_handle_owns_fds_and_anchors_and_rechecks_fresh_scope(self):
        with self._scope() as scope:
            root_fd = scope._root_fd
            with scope.open_patch_target(self.request) as target:
                parent_fd, fd = target.parent_fd, target.fd
                self.assertEqual(target.leaf, "file")
                self.assertEqual(os.read(fd, 100), b"original")
                self.assertEqual(target.anchors["root"], [os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino])
                self.assertEqual(target.anchors["parent"], [os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino])
                self.assertEqual(target.anchors["parents"][0], target.anchors["root"])
                self.assertEqual(target.anchors["parents"][-1], target.anchors["parent"])
                calls = self.live_mock.call_count
                target.recheck()
                self.assertGreater(self.live_mock.call_count, calls)
                self.allowed = []
                with self.assertRaisesRegex(ValueError, "bridge_file_scope_"):
                    target.recheck()
            for closed in (parent_fd, fd):
                with self.assertRaises(OSError):
                    os.fstat(closed)
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_closed"):
                target.recheck()

    def test_named_original_substitution_refuses_then_parent_only_gate_accepts(self):
        with self._scope() as scope:
            with scope.open_patch_target(self.request) as target:
                path = self.root / "docs" / "file"
                path.rename(self.root / "docs" / "old")
                path.write_bytes(b"replacement")
                with self.assertRaisesRegex(ValueError, "bridge_file_scope_path_changed"):
                    target.recheck()
                target.recheck(original_required=False)

    def test_same_inode_metadata_or_content_drift_refuses_original_gate(self):
        for change in (lambda path: path.write_bytes(b"drift"), lambda path: path.chmod(0o600)):
            with self.subTest(change=change):
                path = self.root / "docs" / "file"
                path.write_bytes(b"original")
                path.chmod(0o644)
                with self._scope() as scope:
                    with scope.open_patch_target(self.request) as target:
                        change(path)
                        with self.assertRaisesRegex(ValueError, "bridge_file_scope_path_changed"):
                            target.recheck()

    def test_replaced_ancestor_and_root_refuse_even_parent_only_recheck(self):
        with self._scope() as scope:
            with scope.open_patch_target(self.request) as target:
                (self.root / "docs").rename(self.root / "old-docs")
                (self.root / "docs").mkdir()
                with self.assertRaisesRegex(ValueError, "bridge_file_scope_path_changed"):
                    target.recheck(original_required=False)

    def test_replaced_root_refuses_parent_only_recheck(self):
        previous = self.root.with_name(self.root.name + "-previous")
        try:
            with self._scope() as scope:
                with scope.open_patch_target(self.request) as target:
                    self.root.rename(previous)
                    self.root.mkdir()
                    with self.assertRaisesRegex(ValueError, "bridge_file_scope_root_changed"):
                        target.recheck(original_required=False)
        finally:
            if previous.exists():
                self.root.rmdir()
                previous.rename(self.root)

    def test_parent_acl_including_inheritance_refuses(self):
        parent = os.stat(self.root / "docs")

        def acl(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                raise ValueError("patch_metadata_acl_unsupported")

        with self._scope() as scope, mock.patch.object(module, "require_no_acl", side_effect=acl):
            with self.assertRaisesRegex(ValueError, "patch_metadata_acl_unsupported"):
                with scope.open_patch_target(self.request):
                    self.fail("parent ACL was accepted")
        with self._scope() as scope:
            with scope.open_patch_target(self.request) as target:
                with mock.patch.object(module, "require_no_acl", side_effect=acl):
                    with self.assertRaisesRegex(ValueError, "patch_metadata_acl_unsupported"):
                        target.recheck(original_required=False)

    def test_special_modes_hardlinks_and_oversize_original_refuse(self):
        path = self.root / "docs" / "file"
        for change in (lambda: path.chmod(0o4644),
                       lambda: os.link(path, self.root / "hardlink"),
                       lambda: path.write_bytes(b"x" * 65537)):
            with self.subTest(change=change):
                with self._scope() as scope:
                    change()
                    with self.assertRaisesRegex(ValueError, "patch_metadata_target_unsafe"):
                        with scope.open_patch_target(self.request):
                            pass
                path.chmod(0o644)
                if (self.root / "hardlink").exists():
                    (self.root / "hardlink").unlink()
                path.write_bytes(b"original")

    def test_missing_symlink_and_fifo_never_create_or_open_write(self):
        (self.root / "link").symlink_to(self.root / "docs" / "file")
        os.mkfifo(self.root / "fifo")
        with self._scope() as scope:
            for name in ("missing", "missing-parent/file", "link", "fifo"):
                request = dataclasses.replace(self.request, target=name)
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "bridge_file_scope_"):
                        with scope.open_patch_target(request):
                            pass
        self.assertFalse((self.root / "missing").exists())
        self.assertFalse((self.root / "missing-parent").exists())

    def test_caller_exception_closes_handles_and_scope_close_is_busy(self):
        with self._scope() as scope:
            with self.assertRaisesRegex(RuntimeError, "caller"):
                with scope.open_patch_target(self.request) as target:
                    parent_fd, fd = target.parent_fd, target.fd
                    with self.assertRaisesRegex(ValueError, "bridge_file_scope_busy"):
                        scope.close()
                    raise RuntimeError("caller")
            for closed in (parent_fd, fd):
                with self.assertRaises(OSError):
                    os.fstat(closed)

    def test_failed_metadata_gate_closes_all_new_descriptors(self):
        opened = []
        original_open, original_dup = os.open, os.dup

        def record_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def record_dup(*args, **kwargs):
            fd = original_dup(*args, **kwargs)
            opened.append(fd)
            return fd

        with self._scope() as scope:
            with mock.patch.object(module.os, "open", side_effect=record_open), \
                 mock.patch.object(module.os, "dup", side_effect=record_dup), \
                 mock.patch.object(module, "read_file_metadata", side_effect=ValueError("patch_metadata_unreadable")):
                # A wrapped os.open must still advertise the existing primitive.
                supports = set(os.supports_dir_fd) | {module.os.open}
                with mock.patch.object(module.os, "supports_dir_fd", supports):
                    with self.assertRaisesRegex(ValueError, "patch_metadata_unreadable"):
                        with scope.open_patch_target(self.request):
                            pass
            self.assertTrue(opened)
            for fd in set(opened):
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_patch_rejects_wrong_kind_and_unsupported_descriptor_rename(self):
        with self._scope() as scope:
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_kind_unsupported"):
                with scope.open_patch_target(dataclasses.replace(self.request, kind="file.read")):
                    pass
            supports = set(os.supports_dir_fd) - {os.rename}
            with mock.patch.object(module.os, "supports_dir_fd", supports):
                with self.assertRaisesRegex(ValueError, "bridge_file_scope_unsupported"):
                    with scope.open_patch_target(self.request):
                        pass

    def test_native_descriptor_relative_replace_operates_on_anchored_parent(self):
        with self._scope() as scope:
            with scope.open_patch_target(self.request) as target:
                scratch = os.open("owned-temp", os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                  0o600, dir_fd=target.parent_fd)
                os.write(scratch, b"replacement")
                staged = os.fstat(scratch)
                os.close(scratch)
                os.replace("owned-temp", target.leaf, src_dir_fd=target.parent_fd,
                           dst_dir_fd=target.parent_fd)
                target.recheck(original_required=False)
                actual = os.stat(target.leaf, dir_fd=target.parent_fd, follow_symlinks=False)
                self.assertEqual((actual.st_dev, actual.st_ino), (staged.st_dev, staged.st_ino))


if __name__ == "__main__":
    unittest.main()
