"""Self-contained real-ledger B03 retained process-owner acceptance."""

import base64
import copy
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from orch import bridge_local_broker as module
from orch import bridge_process as process_module
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_scope import LocalFileScope
from orch.bridge_process import LocalOwnedProcessOperations, OwnedProcessProfile
from orch.config import atomic_write_json
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import (
    WORKER_MAX_OUTPUT_BYTES, WorkerOperationOutcome, WorkerOperationRequest,
    WorkerRunBinding, validate_worker_outcome,
)


class RetainedBrokerProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-b03-broker-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.orch = Orchestrator(self.base / "home")
        self.registry = ProjectRegistry(self.orch.root)
        self.projects, self.claims, self.bindings = {}, {}, {}
        self.brokers, self.owners = [], []
        self.sequence = 0
        self.unresolved = set()
        self.addCleanup(self._cleanup_brokers)
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
        self._git("-C", str(root), "add", ".")
        self._git("-C", str(root), "commit", "-qm", "fixture")
        project = self.registry.add(
            root, name="Disposable B03 " + label, profile="safe", review_mode="off",
            allow_commit=True, allow_push=False,
        )["project"]
        self.projects[label] = project
        plan = build_single_task_plan(
            project, task_id="B03-" + label.upper(), goal="Test retained process owner",
            allowed_paths=["README.md", "docs/"], plan_revision="b03-" + label,
        )
        plan_path = self.base / (label + "-plan.json")
        write_plan(plan_path, plan)
        self.orch.load_plan(plan_path)
        claim = self.orch.claim("fixture-b03-" + label, project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED", claim)
        self.claims[label] = claim
        self.bindings[label] = WorkerRunBinding(
            project_id=project["project_id"], writer_key=project["writer_key"],
            task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"],
        )

    def _broker(self, **kwargs):
        broker = module.LocalRunBroker(self.orch, **kwargs)
        self.brokers.append(broker)
        return broker

    def _cleanup_brokers(self):
        for broker in reversed(self.brokers):
            try:
                broker.close()
            except ValueError as exc:
                if broker not in self.unresolved or str(exc) != "bridge_broker_cleanup_unresolved":
                    raise
        for owner in self.owners:
            owner.close()
            for child in owner._children.values():
                if child.thread is not None:
                    child.thread.join(timeout=2)
                    self.assertFalse(child.thread.is_alive())
                self.assertIsNotNone(child.process.poll())
                self.assertTrue(child.process.stdout.closed)

    def _cap(self, label):
        return Path(self.claims[label]["capability_file"])

    def _profile(self, label="a", *, name="python", code=None, args=None, env=None):
        return OwnedProcessProfile(
            name, Path(sys.executable).resolve(),
            args if args is not None else ("-c", code or "import time; time.sleep(600)"),
            Path(self.projects[label]["root"]),
            env if env is not None else {"LANG": "C", "PYTHONUNBUFFERED": "1"},
            600, WORKER_MAX_OUTPUT_BYTES,
        )

    def _open(self, label="a", *, broker=None, profiles=None):
        return (broker or self.broker).open_context(
            self.bindings[label], self._cap(label),
            profiles=(self._profile(label),) if profiles is None else profiles,
        )

    @contextmanager
    def _resources(self):
        owners, scopes = [], []

        def owner(*args, **kwargs):
            acquired = LocalOwnedProcessOperations(*args, **kwargs)
            owners.append(acquired)
            self.owners.append(acquired)
            return acquired

        def scope(*args, **kwargs):
            acquired = LocalFileScope(*args, **kwargs)
            scopes.append(acquired)
            return acquired

        with mock.patch.object(module, "LocalOwnedProcessOperations", side_effect=owner):
            with mock.patch.object(module, "LocalFileScope", side_effect=scope):
                yield owners, scopes

    def _request(self, label="a", *, kind="process.start", target="python", op=None,
                 limit=WORKER_MAX_OUTPUT_BYTES):
        self.sequence += 1
        return WorkerOperationRequest(
            self.bindings[label], op or "b03-op-" + str(self.sequence), kind, target, limit,
        )

    def _decode(self, request, outcome):
        self.assertEqual(outcome.state, "SUCCEEDED", outcome)
        self.assertIs(validate_worker_outcome(request, outcome), outcome)
        body = json.loads(outcome.output)
        self.assertEqual(body["schema_version"], 1)
        body["bytes"] = base64.b64decode(body["output_base64"], validate=True)
        return body

    def _start(self, context, label="a", **kwargs):
        request = self._request(label, **kwargs)
        outcome = context.process_start(request)
        return request, outcome, self._decode(request, outcome)

    def _poll(self, context, handle, label="a", *, cursor=0, limit=WORKER_MAX_OUTPUT_BYTES):
        request = self._request(label, kind="process.poll", target=handle, limit=limit)
        return self._decode(request, context.process_poll(request, cursor=cursor))

    def _wait(self, predicate):
        expires = time.monotonic() + 5
        while time.monotonic() < expires:
            if predicate():
                return
            time.sleep(.01)
        self.fail("retained direct child did not reach the expected observation")

    def _assert_live(self, context, label):
        read = self._request(label, kind="file.read", target="README.md")
        self.assertEqual(context.read(read).output, ("safe-" + label + "\n").encode("ascii"))
        self.assertEqual(context.status()["native"], {"run_state": "RUNNING", "task_state": "IN_PROGRESS"})

    def _assert_closed(self, context, label):
        for operation in (
            lambda: context.process_start(self._request(label)),
            lambda: context.process_poll(self._request(label, kind="process.poll", target="proc-unknown")),
            lambda: context.process_terminate(self._request(label, kind="process.terminate", target="proc-unknown")),
            context.status,
        ):
            with self.assertRaises(ValueError):
                operation()

    def _revoke(self, label):
        claim = self.claims[label]
        receipt = Path(claim["receipt_file"])
        atomic_write_json(receipt, {
            "run_id": claim["run_id"], "task_id": claim["task_id"],
            "changed_paths": ["README.md"], "summary": "disposable B03 authority revocation",
        }, mode=0o600)
        lease = self.orch.lease_from_capability(claim["run_id"], self._cap(label))
        self.assertEqual(self.orch.submit(claim["run_id"], lease, receipt)["status"], "RESULT_SUBMITTED")
        self.assertEqual(self.orch.quiesce(claim["run_id"], lease)["status"], "VERIFYING")

    def _assert_native_writers(self):
        with self.orch.connect() as conn:
            rows = conn.execute("SELECT state FROM runs ORDER BY run_id").fetchall()
        self.assertTrue(rows)
        self.assertEqual({row["state"] for row in rows}, {"RUNNING"})

    def test_b03_default_six_global_two_per_run_refuse_before_spawn(self):
        with self._resources() as (owners, _):
            contexts = {label: self._open(label) for label in ("a", "b", "c")}
            for label, context in contexts.items():
                for _ in range(2):
                    self._start(context, label)
                if label == "a":
                    with mock.patch.object(process_module, "_spawn_process") as spawn:
                        outcome = context.process_start(self._request(label))
                        self.assertEqual((outcome.state, outcome.error_code),
                                         ("REFUSED", "process_dispatch_capacity"))
                        spawn.assert_not_called()
                    with self.orch.connect() as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 2)
            self.assertEqual(len(owners), 3)
            self.assertEqual([len(owner._children) for owner in owners], [2, 2, 2])
            self._add_project("d")
            fourth = self._open("d", broker=self._broker())
            with mock.patch.object(process_module, "_spawn_process") as spawn:
                for label, context in (("a", contexts["a"]), ("d", fourth)):
                    outcome = context.process_start(self._request(label))
                    self.assertEqual((outcome.state, outcome.error_code),
                                     ("REFUSED", "process_dispatch_capacity"))
                spawn.assert_not_called()
            with self.orch.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 6)
            self._assert_native_writers()

    def test_b03_retains_single_owner_with_bounded_cursor_and_operation_identity(self):
        with self._resources() as (owners, _):
            context = self._open(profiles=(self._profile(code="print('x' * 3000)"),))
            start, outcome, started = self._start(context)
            owner = owners[0]
            self.assertEqual(context.process_start(start), outcome)
            child = owner._children[started["handle"]]
            self._wait(lambda: child.settled and child.recorded)
            collected, cursor = bytearray(), 0
            while True:
                request = self._request(kind="process.poll", target=started["handle"], limit=600)
                result = context.process_poll(request, cursor=cursor)
                body = self._decode(request, result)
                self.assertEqual(body["handle"], started["handle"])
                self.assertEqual(body["cursor"], cursor)
                collected.extend(body["bytes"])
                self.assertEqual(context.process_poll(request, cursor=cursor), result)
                conflict = context.process_poll(request, cursor=cursor + 1)
                self.assertEqual(conflict.error_code, "process_operation_conflict")
                cursor = body["next_cursor"]
                if not body["has_more"]:
                    break
            self.assertEqual(bytes(collected), b"x" * 3000 + b"\n")
            self.assertEqual(body["process_state"], "EXITED")
            self.assertEqual(len(owners), 1)
            self.assertIs(owners[0], owner)

    def test_b03_foreign_binding_and_handles_cannot_poll_or_terminate(self):
        with self._resources() as (owners, _):
            contexts = {label: self._open(label) for label in ("a", "b", "c")}
            _, _, started = self._start(contexts["a"])
            for kind, method in (("process.poll", "process_poll"), ("process.terminate", "process_terminate")):
                with self.assertRaisesRegex(ValueError, "worker_operation_scope_mismatch"):
                    getattr(contexts["a"], method)(self._request("b", kind=kind, target=started["handle"]))
                for label in ("b", "c"):
                    outcome = getattr(contexts[label], method)(self._request(label, kind=kind, target=started["handle"]))
                    self.assertEqual((outcome.state, outcome.error_code), ("REFUSED", "process_handle_denied"))
            self.assertIsNone(owners[0]._children[started["handle"]].process.poll())
            self.assertEqual(self._poll(contexts["a"], started["handle"])["process_state"], "RUNNING")

    def test_b03_profiles_are_admission_snapshot_and_arbitrary_targets_are_denied(self):
        args, env = ["-c", "print('frozen')"], {"LANG": "C", "PYTHONUNBUFFERED": "1"}
        profile = self._profile(args=args, env=env)
        profiles = [profile]
        with self._resources() as (owners, _):
            context = self._open(profiles=profiles)
            args[:] = ["-c", "raise RuntimeError('changed')"]
            env["LANG"] = "changed"
            profiles[:] = [self._profile(name="replacement")]
            with self.assertRaises(dataclasses.FrozenInstanceError):
                profile.args = ("changed",)
            with self.assertRaises(TypeError):
                profile.env["LANG"] = "changed"
            for target in ("replacement", "unknown", "sh", "12345"):
                outcome = context.process_start(self._request(target=target))
                self.assertEqual((outcome.state, outcome.error_code), ("REFUSED", "process_profile_denied"))
            _, _, started = self._start(context)
            child = owners[0]._children[started["handle"]]
            self._wait(lambda: child.settled and child.recorded)
            self.assertEqual(self._poll(context, started["handle"])["bytes"], b"frozen\n")

    def test_b03_empty_profiles_preserve_read_list_status_without_process_owner(self):
        with self._resources() as (owners, _):
            context = self.broker.open_context(self.bindings["a"], self._cap("a"))
            self._assert_live(context, "a")
            listed = context.list(self._request(kind="file.list", target="docs"))
            self.assertEqual(json.loads(listed.output), {"entries": ["a.txt"]})
            for kind, method in (("process.start", "process_start"), ("process.poll", "process_poll"), ("process.terminate", "process_terminate")):
                result = getattr(context, method)(self._request(kind=kind))
                self.assertEqual((result.state, result.error_code), ("REFUSED", "bridge_broker_process_unavailable"))
            self.assertEqual(owners, [])

    def test_b03_profile_admission_rejects_foreign_root_and_invalid_registry_without_slot_leak(self):
        broker = self._broker(max_worker_sessions=1)
        for profiles in ((self._profile("b"),), (object(),),
                         (self._profile(), self._profile())):
            with self.subTest(profiles=tuple(type(item).__name__ for item in profiles)):
                with self._resources() as (_, scopes):
                    with self.assertRaisesRegex(ValueError, "process_(profile_changed|invalid_profiles)"):
                        self._open(broker=broker, profiles=profiles)
                    self.assertEqual(len(scopes), 1)
                    with self.assertRaises(OSError):
                        os.fstat(scopes[0]._root_fd)
                    admitted = self._open("b", broker=broker)
                    self._assert_live(admitted, "b")
                    admitted.close()

    def test_b03_closing_b_reaps_only_b_and_preserves_native_writers(self):
        with self._resources() as (owners, scopes):
            contexts = {label: self._open(label) for label in ("a", "b", "c")}
            starts = {label: self._start(context, label)[2] for label, context in contexts.items()}
            b_fd = scopes[1]._root_fd
            contexts["b"].close()
            self._assert_closed(contexts["b"], "b")
            self.assertIsNotNone(owners[1]._children[starts["b"]["handle"]].process.poll())
            with self.assertRaises(OSError):
                os.fstat(b_fd)
            for index, label in ((0, "a"), (2, "c")):
                self.assertIsNone(owners[index]._children[starts[label]["handle"]].process.poll())
                self.assertEqual(self._poll(contexts[label], starts[label]["handle"], label)["process_state"], "RUNNING")
                self._assert_live(contexts[label], label)
            self._assert_native_writers()

    def test_b03_native_revocation_blocks_buffered_and_cached_operations_but_not_cleanup(self):
        with self._resources() as (owners, _):
            for label, method in (("a", "process_start"), ("b", "process_poll"), ("c", "process_terminate")):
                code = "print('buffered')" if label == "a" else "print('buffered'); import time; time.sleep(600)"
                context = self._open(label, profiles=(self._profile(label, code=code),))
                start, _, started = self._start(context, label)
                owner = owners[-1]
                child = owner._children[started["handle"]]
                self._wait(lambda: b"buffered" in child.output)
                if label == "a":
                    self._wait(lambda: child.settled and child.recorded)
                request = start if method == "process_start" else self._request(label, kind=method.replace("_", "."), target=started["handle"])
                adapter_method = method.removeprefix("process_")
                original = getattr(owner, adapter_method)

                def buffered(*args, _original=original, _label=label, **kwargs):
                    result = _original(*args, **kwargs)
                    if _label == "a":
                        self._revoke(_label)
                    else:
                        self.assertTrue(self.orch._revoke_capability(self.bindings[_label].run_id))
                    return result

                with mock.patch.object(owner, adapter_method, side_effect=buffered):
                    with self.assertRaises(ValueError):
                        getattr(context, method)(request)
                self._assert_closed(context, label)
                context.close()
                self.assertIsNotNone(child.process.poll())

    def test_b03_process_constructor_and_final_admission_failures_roll_back_scope_and_route(self):
        for failure in ("constructor", "final"):
            with self.subTest(failure=failure):
                broker = self._broker(max_worker_sessions=1)
                with self._resources() as (owners, scopes):
                    if failure == "constructor":
                        with mock.patch.object(module, "LocalOwnedProcessOperations", side_effect=ValueError("injected_owner_constructor")):
                            with self.assertRaisesRegex(ValueError, "injected_owner_constructor"):
                                self._open(broker=broker)
                    else:
                        original = LocalRunSessionAuthority.require_session
                        count = 0

                        def require(authority, *args):
                            nonlocal count
                            count += 1
                            original(authority, *args)
                            if count == 3:
                                raise ValueError("injected_final_admission")

                        with mock.patch.object(LocalRunSessionAuthority, "require_session", require):
                            with self.assertRaisesRegex(ValueError, "injected_final_admission"):
                                self._open(broker=broker)
                        self.assertEqual(len(owners), 1)
                        self.assertTrue(owners[0]._closed)
                    with self.assertRaises(OSError):
                        os.fstat(scopes[0]._root_fd)
                    self._assert_live(self._open("b", broker=broker), "b")

    def test_b03_process_cleanup_failure_retries_retained_owner_and_closes_scope_and_route(self):
        broker = self._broker(max_worker_sessions=1)
        with self._resources() as (owners, scopes):
            context = self._open(broker=broker)
            _, _, started = self._start(context)
            owner, scope = owners[0], scopes[0]
            original_close = owner.close
            with mock.patch.object(owner, "close", side_effect=RuntimeError("injected_cleanup")) as close:
                with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                    context.close()
                close.assert_called_once_with()
            self._assert_closed(context, "a")
            with self.assertRaises(OSError):
                os.fstat(scope._root_fd)
            b_context = self._open("b", broker=broker)
            self._assert_live(b_context, "b")
            with mock.patch.object(owner, "close", wraps=original_close) as close:
                with mock.patch.object(owner._store, "observe_exit", side_effect=OSError("terminal_commit_unconfirmed")):
                    context.close()
                close.assert_called_once_with()
            self.assertIsNotNone(owner._children[started["handle"]].process.poll())
            self.assertTrue(owner._children[started["handle"]].unknown)
            context.close()
            with self.orch.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)
            b_context.close()
            replacement = self._open(broker=broker)
            self._start(replacement)
            with mock.patch.object(process_module, "_spawn_process") as spawn:
                result = replacement.process_start(self._request())
                self.assertEqual((result.state, result.error_code), ("REFUSED", "process_dispatch_capacity"))
                spawn.assert_not_called()
            self._assert_native_writers()

    def test_b03_cleanup_attempts_process_scope_and_session_independently(self):
        with self._resources() as (owners, scopes):
            context = self._open()
            self._start(context)
            owner, scope = owners[0], scopes[0]
            original_scope_close = scope.close
            with mock.patch.object(owner, "close", side_effect=RuntimeError("process_cleanup")) as process_close:
                with mock.patch.object(scope, "close", side_effect=ValueError("bridge_file_scope_busy")) as scope_close:
                    with mock.patch.object(LocalRunSessionAuthority, "close_session", side_effect=RuntimeError("session_cleanup")) as session_close:
                        with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                            context.close()
                        process_close.assert_called_once_with()
                        scope_close.assert_called_once_with()
                        session_close.assert_called_once_with(context.session_id)
            self.unresolved.add(self.broker)
            with mock.patch.object(owner, "close", wraps=owner.close) as process_close:
                with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                    context.close()
                process_close.assert_called_once_with()
            # The existing B02 ambiguous-session flag deliberately does not
            # retry a scope descriptor. This fixture still owns its safe FD.
            original_scope_close()
            self._assert_closed(context, "a")

    def test_b03_unproven_exit_remains_charged_after_close_and_replacement(self):
        with self._resources() as (owners, _):
            contexts = {label: self._open(label) for label in ("a", "b", "c")}
            for label, context in contexts.items():
                for _ in range(2):
                    self._start(context, label)
            with mock.patch.object(owners[1]._store, "observe_exit", side_effect=OSError("terminal_commit_unconfirmed")):
                contexts["b"].close()
            self.assertTrue(all(child.unknown for child in owners[1]._children.values()))
            replacement = self._open("b")
            self._add_project("d")
            fourth = self._open("d", broker=self._broker())
            with mock.patch.object(process_module, "_spawn_process") as spawn:
                for label, context in (("b", replacement), ("d", fourth)):
                    outcome = context.process_start(self._request(label))
                    self.assertEqual(outcome.error_code, "process_dispatch_capacity")
                spawn.assert_not_called()
            with self.orch.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)
            for label in ("a", "c"):
                self._assert_live(contexts[label], label)

    def test_b03_reopen_and_new_broker_cannot_adopt_handles_or_replay_start(self):
        with self._resources():
            context = self._open()
            request, _, started = self._start(context)
            context.close()
            reopened = self._open()
            fresh = self._open(broker=self._broker())
            with mock.patch.object(process_module, "_spawn_process") as spawn:
                for replacement in (reopened, fresh):
                    result = replacement.process_start(request)
                    self.assertEqual((result.state, result.error_code), ("REFUSED", "process_operation_conflict"))
                    for kind, method in (("process.poll", "process_poll"), ("process.terminate", "process_terminate")):
                        result = getattr(replacement, method)(self._request(kind=kind, target=started["handle"]))
                        self.assertEqual(result.error_code, "process_handle_denied")
                spawn.assert_not_called()

    def test_b03_equal_hash_foreign_context_cannot_route_or_close_process_owner(self):
        class EqualContext(module.LocalRunContext):
            def __hash__(self):
                return hash(context)

            def __eq__(self, other):
                return other is context or self is other

        with self._resources() as (owners, _):
            context = self._open()
            _, _, started = self._start(context)
            foreign = object.__new__(EqualContext)
            for slot in module.LocalRunContext.__slots__:
                name = "_LocalRunContext" + slot
                setattr(foreign, name, getattr(context, name))
            for forged in (foreign, copy.copy(context)):
                self._assert_closed(forged, "a")
            with self.assertRaisesRegex(ValueError, "bridge_broker_context_unowned"):
                foreign.close()
            self.assertIsNone(owners[0]._children[started["handle"]].process.poll())
            self._assert_live(context, "a")

    def test_b03_broker_close_continues_after_b_cleanup_failure(self):
        with self._resources() as (owners, scopes):
            contexts = {label: self._open(label) for label in ("a", "b", "c")}
            for label, context in contexts.items():
                self._start(context, label)
            with mock.patch.object(owners[1], "close", side_effect=RuntimeError("b_cleanup_failed")):
                with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                    self.broker.close()
            for label, context in contexts.items():
                self._assert_closed(context, label)
            for index in (0, 2):
                self.assertTrue(all(child.process.poll() is not None for child in owners[index]._children.values()))
            for scope in scopes:
                with self.assertRaises(OSError):
                    os.fstat(scope._root_fd)
            self.broker.close()
            self._assert_native_writers()

    def test_b03_process_wrapper_validates_adapter_output_identity_and_bounds(self):
        with self._resources() as (owners, _):
            context = self._open()
            request = self._request()
            for outcome, error in (
                (WorkerOperationOutcome("foreign-op", "SUCCEEDED"), "worker_outcome_operation_mismatch"),
                (WorkerOperationOutcome(request.operation_id, "SUCCEEDED", b"x" * (request.max_output_bytes + 1)), "worker_outcome_too_large"),
            ):
                with mock.patch.object(owners[0], "start", return_value=outcome):
                    with self.assertRaisesRegex(ValueError, error):
                        context.process_start(request)
