"""Disposable real-ledger B02 broker composition and cleanup acceptance."""

import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from unittest import mock

from orch import bridge_file_scope as scope_module
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_scope import LocalFileScope
from orch.config import atomic_write_json
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding


if importlib.util.find_spec("orch.bridge_local_broker") is not None:
    from orch import bridge_local_broker as module
else:
    module = None


class LocalRunBrokerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, "B02 trusted local broker is not implemented")
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-b02-broker-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.orch = Orchestrator(self.base / "home")
        self.registry = ProjectRegistry(self.orch.root)
        self.projects, self.claims, self.bindings = {}, {}, {}
        self.unresolved = set()
        for label in ("a", "b", "c"):
            self._add_project(label)
        self.broker = self._broker()

    def _git(self, *args):
        env = dict(os.environ)
        env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True, text=True, check=True, env=env, timeout=12,
        )

    def _add_project(self, label):
        root = self.base / ("repo-" + label)
        self._git("init", "-q", "-b", "main", str(root))
        self._git("-C", str(root), "config", "user.email", "fixture@example.invalid")
        self._git("-C", str(root), "config", "user.name", "Fixture")
        (root / "docs").mkdir()
        (root / "README.md").write_bytes(("safe-" + label + "\n").encode("ascii"))
        (root / "docs" / (label + ".txt")).write_bytes(b"entry\n")
        (root / "secret.txt").write_bytes(b"private\n")
        self._git("-C", str(root), "add", ".")
        self._git("-C", str(root), "commit", "-qm", "fixture")
        project = self.registry.add(
            root, name="Disposable broker " + label, profile="safe",
            review_mode="off", allow_commit=True, allow_push=False,
        )["project"]
        self.projects[label] = project
        plan = build_single_task_plan(
            project, task_id="B02-" + label.upper(), goal="Test local broker",
            allowed_paths=["README.md", "docs/"], plan_revision="b02-" + label,
        )
        plan_path = self.base / (label + "-plan.json")
        write_plan(plan_path, plan)
        self.orch.load_plan(plan_path)
        claim = self.orch.claim("fixture-" + label, project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED", claim)
        self.claims[label] = claim
        self.bindings[label] = WorkerRunBinding(
            project_id=project["project_id"], writer_key=project["writer_key"],
            task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"],
        )

    def _broker(self, **kwargs):
        broker = module.LocalRunBroker(self.orch, **kwargs)

        def cleanup():
            try:
                broker.close()
            except ValueError as exc:
                if broker not in self.unresolved or str(exc) != "bridge_broker_cleanup_unresolved":
                    raise

        self.addCleanup(cleanup)
        return broker

    def _cap(self, label):
        return Path(self.claims[label]["capability_file"])

    def _open(self, label="a", broker=None):
        return (broker or self.broker).open_context(self.bindings[label], self._cap(label))

    def _request(self, label="a", *, kind="file.read", target="README.md", limit=65536):
        return WorkerOperationRequest(
            self.bindings[label], "b02-operation", kind, target, limit,
        )

    def _assert_context(self, context, label):
        self.assertEqual(context.read(self._request(label)).output,
                         ("safe-" + label + "\n").encode("ascii"))
        listed = context.list(self._request(label, kind="file.list", target="docs"))
        self.assertEqual(listed.state, "SUCCEEDED")
        self.assertEqual(json.loads(listed.output), {"entries": [label + ".txt"]})
        self.assertEqual(context.status(), {
            "binding": dataclasses.asdict(self.bindings[label]),
            "native": {"run_state": "RUNNING", "task_state": "IN_PROGRESS"},
        })

    def _assert_denied(self, context, label):
        for operation in (
            lambda: context.read(self._request(label)),
            lambda: context.list(self._request(label, kind="file.list", target="docs")),
            context.status,
        ):
            with self.assertRaises(ValueError):
                operation()

    def _snapshot(self):
        with self.orch.connect() as conn:
            ledger = hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()
        caps = {}
        for label in self.claims:
            cap = self._cap(label)
            info = cap.stat()
            caps[label] = (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                           info.st_mtime_ns, hashlib.sha256(cap.read_bytes()).hexdigest())
        return ledger, caps

    def _revoke(self, label):
        claim = self.claims[label]
        receipt = Path(claim["receipt_file"])
        atomic_write_json(receipt, {
            "run_id": claim["run_id"], "task_id": claim["task_id"],
            "changed_paths": ["README.md"], "summary": "disposable broker revocation",
        }, mode=0o600)
        lease = self.orch.lease_from_capability(claim["run_id"], self._cap(label))
        self.assertEqual(self.orch.submit(claim["run_id"], lease, receipt)["status"],
                         "RESULT_SUBMITTED")
        self.assertEqual(self.orch.quiesce(claim["run_id"], lease)["status"], "VERIFYING")

    @contextmanager
    def _scopes(self):
        scopes = []

        def construct(*args, **kwargs):
            scope = LocalFileScope(*args, **kwargs)
            scopes.append(scope)
            return scope

        with mock.patch.object(module, "LocalFileScope", side_effect=construct):
            yield scopes

    def test_b02_three_contexts_capacity_close_isolation_and_native_preservation(self):
        self._add_project("d")
        before = self._snapshot()
        contexts = {label: self._open(label) for label in ("a", "b", "c")}
        self.assertEqual(len({item.session_id for item in contexts.values()}), 3)
        with self.assertRaisesRegex(ValueError, "bridge_session_capacity_exhausted"):
            self._open("d")
        with self.assertRaisesRegex(ValueError, "bridge_run_already_bound_locally"):
            self._open("b")
        for label, context in contexts.items():
            self._assert_context(context, label)
        contexts["b"].close()
        contexts["b"].close()
        self._assert_denied(contexts["b"], "b")
        replacement = self._open("d")
        self.assertNotIn(replacement.session_id, {x.session_id for x in contexts.values()})
        self._assert_context(replacement, "d")
        for label in ("a", "c"):
            self._assert_context(contexts[label], label)
        with self.assertRaisesRegex(ValueError, "bridge_session_capacity_exhausted"):
            self._open("b")
        self.assertEqual(self._snapshot(), before)

    def test_b02_wrong_capability_and_forged_bindings_do_not_consume_capacity(self):
        broker = self._broker(max_worker_sessions=1)
        before = self._snapshot()
        forged = dataclasses.replace(self.bindings["a"], writer_key=self.bindings["b"].writer_key)
        for binding, cap in ((self.bindings["a"], self._cap("b")),
                             (forged, self._cap("a"))):
            with self.assertRaises(ValueError):
                broker.open_context(binding, cap)
        self._assert_context(self._open("a", broker), "a")
        self.assertEqual(self._snapshot(), before)

    def test_b02_sealed_context_rejects_cross_binding_and_copied_identity(self):
        a, b = self._open("a"), self._open("b")
        for name, value in (("session_id", b.session_id), ("binding", b.binding)):
            with self.assertRaises(AttributeError):
                setattr(a, name, value)
        for request in (self._request("b"),
                        self._request("b", kind="file.list", target="docs")):
            with self.assertRaisesRegex(ValueError, "worker_operation_scope_mismatch"):
                (a.read if request.kind == "file.read" else a.list)(request)
        copied = copy.copy(a)
        self._assert_denied(copied, "a")
        self._assert_context(a, "a")
        self._assert_context(b, "b")

    def test_b02_new_broker_requires_fresh_admission_without_invalidating_live_owner(self):
        old = self._open("a")
        fresh = self._broker(max_worker_sessions=1)
        before = self._snapshot()
        for foreign in (old, old.session_id):
            with self.assertRaises(ValueError):
                fresh.open_context(foreign, self._cap("a"))
        replacement = self._open("a", fresh)
        self.assertNotEqual(replacement.session_id, old.session_id)
        self._assert_context(old, "a")
        self._assert_context(replacement, "a")
        old.close()
        self._assert_denied(old, "a")
        self._assert_context(replacement, "a")
        self.assertEqual(self._snapshot(), before)

    def test_b02_equal_foreign_context_cannot_select_or_close_an_owned_context(self):
        context = self._open()

        class EqualForeignContext(module.LocalRunContext):
            def __hash__(self):
                return hash(context)

            def __eq__(self, other):
                return other is context

        forged = object.__new__(EqualForeignContext)
        object.__setattr__(forged, "_LocalRunContext__broker", self.broker)
        object.__setattr__(forged, "_LocalRunContext__session_id", context.session_id)
        object.__setattr__(forged, "_LocalRunContext__binding", context.binding)
        self._assert_denied(forged, "a")
        with self.assertRaises(ValueError):
            forged.close()
        self._assert_context(context, "a")

    def test_b02_scope_adapter_and_final_authority_failures_roll_back_owned_resources(self):
        for failure in ("scope", "reader", "lister", "final"):
            with self.subTest(failure=failure):
                broker = self._broker(max_worker_sessions=1)
                captured = []
                real_require = LocalRunSessionAuthority.require_session
                calls = 0

                def require(authority, *args):
                    nonlocal calls
                    calls += 1
                    real_require(authority, *args)
                    if calls == 2:
                        raise ValueError("injected_final_authority_failure")

                def scope(*args):
                    if failure == "scope":
                        raise ValueError("injected_scope_failure")
                    acquired = LocalFileScope(*args)
                    captured.append(acquired._root_fd)
                    return acquired

                with mock.patch.object(module, "LocalFileScope", side_effect=scope):
                    with mock.patch.object(module, "LocalBoundedFileReader",
                                           side_effect=ValueError("injected_reader_failure")) if failure == "reader" else nullcontext():
                        with mock.patch.object(module, "LocalBoundedFileLister",
                                               side_effect=ValueError("injected_lister_failure")) if failure == "lister" else nullcontext():
                            with mock.patch.object(LocalRunSessionAuthority, "require_session", require) if failure == "final" else nullcontext():
                                with self.assertRaises(ValueError):
                                    self._open("a", broker)
                for fd in captured:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                self._assert_context(self._open("b", broker), "b")
                broker.close()

    def test_b02_constructor_busy_cleanup_is_retained_and_slot_is_released(self):
        broker = self._broker(max_worker_sessions=1)
        held = []
        with self._scopes() as scopes:
            def fail_reader(scope):
                handle = scope.open_readonly(self._request())
                handle.__enter__()
                held.append(handle)
                raise ValueError("injected_adapter_failure")

            with mock.patch.object(module, "LocalBoundedFileReader", side_effect=fail_reader):
                with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_failed"):
                    self._open("a", broker)
            self.addCleanup(held[0].__exit__, None, None, None)
            root_fd = scopes[0]._root_fd
            os.fstat(root_fd)
            self._assert_context(self._open("b", broker), "b")
            held[0].__exit__(None, None, None)
            broker.close()
            with self.assertRaises(OSError):
                os.fstat(root_fd)

    def test_b02_busy_context_close_invalidates_only_b_and_allows_explicit_safe_retry(self):
        self._add_project("d")
        before = self._snapshot()
        with self._scopes() as scopes:
            contexts = {label: self._open(label) for label in ("a", "b", "c")}
        b_scope, b_fd = scopes[1], scopes[1]._root_fd
        with b_scope.open_readonly(self._request("b")):
            with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_failed"):
                contexts["b"].close()
            self._assert_denied(contexts["b"], "b")
            self._assert_context(self._open("d"), "d")
            for label in ("a", "c"):
                self._assert_context(contexts[label], label)
        contexts["b"].close()
        with self.assertRaises(OSError):
            os.fstat(b_fd)
        self.assertEqual(self._snapshot(), before)

    def test_b02_broker_close_attempts_a_and_c_cleanup_after_busy_b(self):
        before = self._snapshot()
        with self._scopes() as scopes:
            contexts = {label: self._open(label) for label in ("a", "b", "c")}
        fds = [scope._root_fd for scope in scopes]
        with scopes[1].open_readonly(self._request("b")):
            with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_failed"):
                self.broker.close()
            for fd in (fds[0], fds[2]):
                with self.assertRaises(OSError):
                    os.fstat(fd)
            os.fstat(fds[1])
            for label, context in contexts.items():
                self._assert_denied(context, label)
            with self.assertRaisesRegex(ValueError, "bridge_broker_closed"):
                self._open("a")
        self.broker.close()
        self.broker.close()
        with self.assertRaises(OSError):
            os.fstat(fds[1])
        self.assertEqual(self._snapshot(), before)

    def _ambiguous_close(self, broker, operation, scopes):
        scope = scopes[0]
        target_fd = scope._root_fd
        real_close = os.close
        recycled = []

        def close(fd):
            if fd != target_fd or recycled:
                return real_close(fd)
            real_close(fd)
            opened = os.open(self.base / "recycled.txt", os.O_CREAT | os.O_RDWR, 0o600)
            if opened != target_fd:
                os.dup2(opened, target_fd)
                real_close(opened)
            recycled.append(target_fd)
            raise OSError("injected_ambiguous_close")

        with mock.patch.object(scope_module.os, "close", side_effect=close):
            with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                operation()
        self.unresolved.add(broker)
        self.addCleanup(real_close, recycled[0])
        os.fstat(recycled[0])
        with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
            broker.close()
        os.fstat(recycled[0])
        return recycled[0]

    def test_b02_ambiguous_context_close_never_retries_a_reused_descriptor(self):
        broker = self._broker(max_worker_sessions=1)
        with self._scopes() as scopes:
            a = self._open("a", broker)
        self._ambiguous_close(broker, a.close, scopes)
        self._assert_denied(a, "a")
        # A different broker can still admit under unchanged native authority.
        self._assert_context(self._open("b"), "b")

    def test_b02_ambiguous_constructor_cleanup_releases_capacity_without_retry(self):
        broker = self._broker(max_worker_sessions=1)
        real_scope = LocalFileScope
        scopes = []
        real_close = os.close
        recycled = []

        def construct(*args):
            scope = real_scope(*args)
            scopes.append(scope)
            return scope

        def close(fd):
            if scopes and fd == scopes[0]._root_fd and not recycled:
                real_close(fd)
                opened = os.open(self.base / "constructor-recycled.txt", os.O_CREAT | os.O_RDWR, 0o600)
                if opened != fd:
                    os.dup2(opened, fd)
                    real_close(opened)
                recycled.append(fd)
                raise OSError("injected_ambiguous_close")
            return real_close(fd)

        with mock.patch.object(module, "LocalFileScope", side_effect=construct):
            with mock.patch.object(module, "LocalBoundedFileReader", side_effect=ValueError("injected")):
                with mock.patch.object(scope_module.os, "close", side_effect=close):
                    with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                        self._open("a", broker)
        self.unresolved.add(broker)
        self.addCleanup(real_close, recycled[0])
        self._assert_context(self._open("b", broker), "b")
        with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
            broker.close()
        os.fstat(recycled[0])

    def test_b02_native_pause_keeps_live_b_then_legitimate_revoke_is_isolated(self):
        contexts = {label: self._open(label) for label in ("a", "b", "c")}
        project_b = self.projects["b"]["project_id"]
        self.assertEqual(self.orch.pause_project(project_b, "disposable hold")["status"],
                         "PROJECT_PAUSED")
        self._assert_context(contexts["b"], "b")
        self._revoke("b")
        self._assert_denied(contexts["b"], "b")
        with self.assertRaises(ValueError):
            self._open("b")
        self.assertEqual(self.orch.claim("new-b", project_id=project_b)["status"], "PROJECT_PAUSED")
        for label in ("a", "c"):
            self._assert_context(contexts[label], label)

    def _buffered_revocation(self, kind):
        contexts = {label: self._open(label) for label in ("a", "b", "c")}
        adapter = module.LocalBoundedFileReader if kind == "file.read" else module.LocalBoundedFileLister
        method = "read" if kind == "file.read" else "list"
        original = getattr(adapter, method)

        def revoke_after_buffer(instance, request):
            outcome = original(instance, request)
            self.assertEqual(outcome.state, "SUCCEEDED")
            self.assertTrue(outcome.output)
            self._revoke("b")
            return outcome

        request = self._request("b", kind=kind, target="README.md" if method == "read" else "docs")
        with mock.patch.object(adapter, method, revoke_after_buffer):
            with self.assertRaises(ValueError):
                getattr(contexts["b"], method)(request)
        self.assertFalse(self._cap("b").exists(), "native revocation must complete")
        for label in ("a", "c"):
            self._assert_context(contexts[label], label)

    def test_b02_read_revalidates_native_authority_after_buffering(self):
        self._buffered_revocation("file.read")

    def test_b02_list_revalidates_native_authority_after_buffering(self):
        self._buffered_revocation("file.list")

    def test_b02_status_revalidates_native_authority_after_query(self):
        context = self._open("b")
        original = LocalRunSessionAuthority.require_session
        calls = 0

        def revoke_before_final(authority, *args):
            nonlocal calls
            calls += 1
            if calls == 2:
                self._revoke("b")
            return original(authority, *args)

        with mock.patch.object(LocalRunSessionAuthority, "require_session", revoke_before_final):
            with self.assertRaises(ValueError):
                context.status()
        self.assertFalse(self._cap("b").exists(), "native revocation must complete")

    def test_b02_existing_path_and_output_bounds_are_composed(self):
        context = self._open()
        for request in (self._request(target="secret.txt"), self._request(limit=2)):
            outcome = context.read(request)
            self.assertEqual(outcome.state, "REFUSED")
            self.assertEqual(outcome.output, b"")
        outcome = context.list(self._request(kind="file.list", target="docs", limit=2))
        self.assertEqual(outcome.state, "REFUSED")
        self.assertEqual(outcome.output, b"")
        self._assert_context(context, "a")

    def test_b02_contexts_stay_on_the_creating_thread(self):
        context = self._open()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(context.status)
            with self.assertRaisesRegex(ValueError, "bridge_broker_owner_mismatch"):
                future.result(timeout=12)
        self._assert_context(context, "a")


if __name__ == "__main__":
    unittest.main()
