"""Real disposable children exercise the trusted local owned-process boundary."""

import base64
import dataclasses
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

try:
    from orch import bridge_process as module
except ImportError:
    module = None

from orch import bridge_authorization as authorization_module
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import (
    WORKER_MAX_OUTPUT_BYTES, WorkerOperationRequest, WorkerRunBinding,
    validate_worker_outcome,
)


class LocalOwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-owned-process-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "repo"
        self._git("init", "-q", "-b", "main", str(self.root))
        self._git("-C", str(self.root), "config", "user.email",
                  "fixture@example.invalid")
        self._git("-C", str(self.root), "config", "user.name", "Fixture")
        (self.root / "README.md").write_bytes(b"safe\n")
        self._git("-C", str(self.root), "add", ".")
        self._git("-C", str(self.root), "commit", "-qm", "fixture")
        self.orch = Orchestrator(self.base / "orch-home")
        project = ProjectRegistry(self.orch.root).add(
            self.root, name="Disposable process", profile="safe",
            review_mode="off", allow_commit=True, allow_push=False,
        )["project"]
        plan = build_single_task_plan(
            project, task_id="OWNED-PROCESS", goal="Test owned child adapter",
            allowed_paths=["README.md"], plan_revision="owned-process-fixture",
        )
        plan_file = self.base / "plan.json"
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        claim = self.orch.claim("fixture-process", project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED", claim)
        self.cap = Path(claim["capability_file"])
        self.binding = WorkerRunBinding(
            project_id=project["project_id"], writer_key=project["writer_key"],
            task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"],
        )
        self.authority = LocalRunSessionAuthority(self.orch)
        self.session = self.authority.open_session(self.binding, self.cap)
        self.sequence = 0

    def _git(self, *args):
        env = dict(os.environ)
        env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              capture_output=True, text=True, check=True,
                              env=env, timeout=12)

    def _profile(self, code="print('child-output')", *, name="python",
                 deadline=2.0, limit=WORKER_MAX_OUTPUT_BYTES, env=None,
                 executable=None, cwd=None, args=None):
        self.assertIsNotNone(module, "owned process adapter is not implemented")
        return module.OwnedProcessProfile(
            name, executable or Path(sys.executable).resolve(),
            args if args is not None else ("-c", code), cwd or self.root,
            env or {"LANG": "C", "PYTHONUNBUFFERED": "1"}, deadline, limit,
        )

    def _executor(self, *profiles, authority=None, session=None, **limits):
        self.assertIsNotNone(module, "owned process adapter is not implemented")
        executor = module.LocalOwnedProcessOperations(
            authority or self.authority, session or self.session,
            self.binding, self.cap, profiles or (self._profile(),), **limits,
        )
        self.addCleanup(executor.close)
        return executor

    def _request(self, kind, target, *, op=None, limit=WORKER_MAX_OUTPUT_BYTES,
                 binding=None):
        self.sequence += 1
        return WorkerOperationRequest(binding or self.binding,
                                      op or "op-" + str(self.sequence),
                                      kind, target, limit)

    def _b01_executor(self, label, *, code="import time; time.sleep(600)", **limits):
        root = self.base / ("repo-" + label)
        self._git("clone", "--quiet", "--no-hardlinks", str(self.root), str(root))
        project = ProjectRegistry(self.orch.root).add(
            root, name="Disposable B01 " + label, profile="safe", review_mode="off",
            allow_commit=True, allow_push=False,
        )["project"]
        plan = build_single_task_plan(project, task_id="B01-" + label,
                                      goal="Test one-home process capacity", allowed_paths=["README.md"],
                                      plan_revision="b01-" + label)
        plan_file = self.base / (label + "-plan.json")
        write_plan(plan_file, plan)
        self.orch.load_plan(plan_file)
        claim = self.orch.claim("b01-" + label, project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED", claim)
        binding = WorkerRunBinding(project_id=project["project_id"], writer_key=project["writer_key"],
                                   task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"])
        cap = Path(claim["capability_file"])
        authority = LocalRunSessionAuthority(self.orch)
        session = authority.open_session(binding, cap)
        profile = module.OwnedProcessProfile("python", Path(sys.executable).resolve(), ("-c", code),
                                             root, {"LANG": "C", "PYTHONUNBUFFERED": "1"}, 600,
                                             WORKER_MAX_OUTPUT_BYTES)
        executor = module.LocalOwnedProcessOperations(authority, session, binding, cap, (profile,), **limits)
        self.addCleanup(executor.close)
        return executor, binding

    def test_b01_three_real_runs_admit_two_children_and_refuse_before_spawn(self):
        first = self._executor(self._profile("import time; time.sleep(600)", deadline=600))
        executors = [(first, self.binding), self._b01_executor("second"), self._b01_executor("third")]
        fourth, fourth_binding = self._b01_executor("fourth")
        with mock.patch.object(module, "_spawn_process", wraps=module._spawn_process) as spawn:
            for executor, binding in executors:
                for _ in range(2):
                    self._start(executor, binding=binding)
                if executor is first:
                    request = self._request("process.start", "python")
                    outcome = executor.start(request)
                    self.assertEqual((outcome.state, outcome.error_code), ("REFUSED", "process_dispatch_capacity"))
                    self.assertEqual(spawn.call_count, 2)
            self.assertEqual(spawn.call_count, 6)
            request = self._request("process.start", "python", binding=fourth_binding)
            outcome = fourth.start(request)
            self.assertEqual((outcome.state, outcome.error_code), ("REFUSED", "process_dispatch_capacity"))
            self.assertEqual(spawn.call_count, 6)
        self.assertEqual(sum(len(executor._children) for executor, _ in executors), 6)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 6)

    def test_b01_invalid_adapter_limits_refuse_without_spawn_or_intent(self):
        with mock.patch.object(module, "_spawn_process") as spawn:
            for option in ("max_processes_total", "max_processes_per_run"):
                for invalid in (True, 0, -1, 1.5, "2", None):
                    with self.subTest(option=option, invalid=invalid), self.assertRaisesRegex(ValueError, "^process_invalid_limits$"):
                        self._executor(**{option: invalid})
            spawn.assert_not_called()
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 0)

    def test_b01_confirmed_real_child_exit_releases_global_capacity(self):
        first = self._executor(self._profile("pass"), max_processes_total=1)
        second, binding = self._b01_executor("exit-reuse", max_processes_total=1)
        _, _, started = self._start(first)
        self.assertEqual(self._exit(first, started["handle"])["exit_code"], 0)
        first._children[started["handle"]].thread.join(timeout=3)
        self.assertFalse(first._children[started["handle"]].thread.is_alive())
        self._start(second, binding=binding)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 1)

    def test_b01_spawn_uncertainty_survives_route_close_owner_replacement_and_restart(self):
        first = self._executor(max_processes_total=1)
        request = self._request("process.start", "python")
        with mock.patch.object(module, "_spawn_process", side_effect=OSError("ambiguous spawn")) as spawn:
            self.assertEqual(first.start(request).state, "UNKNOWN")
            self.assertEqual(spawn.call_count, 1)
        self.authority.close_session(self.session)
        first.close()
        replacement_session = self.authority.open_session(self.binding, self.cap)
        replacement = self._executor(session=replacement_session, max_processes_total=1)
        other, other_binding = self._b01_executor("retained", max_processes_total=1)
        with mock.patch.object(module, "_spawn_process") as spawn:
            for executor, binding in ((replacement, self.binding), (other, other_binding)):
                denied = self._request("process.start", "python", binding=binding)
                outcome = executor.start(denied)
                self.assertEqual((outcome.state, outcome.error_code), ("REFUSED", "process_dispatch_capacity"))
            spawn.assert_not_called()
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)

    def _decode(self, request, outcome):
        self.assertEqual(outcome.state, "SUCCEEDED", outcome)
        self.assertIs(validate_worker_outcome(request, outcome), outcome)
        body = json.loads(outcome.output)
        self.assertEqual(body["schema_version"], 1)
        self.assertLessEqual(len(outcome.output), request.max_output_bytes)
        body["bytes"] = base64.b64decode(body["output_base64"], validate=True)
        return body

    def _start(self, executor, target="python", **kwargs):
        request = self._request("process.start", target, **kwargs)
        outcome = executor.start(request)
        return request, outcome, self._decode(request, outcome)

    def _exit(self, executor, handle, *, cursor=0, limit=WORKER_MAX_OUTPUT_BYTES):
        expires = time.monotonic() + 4
        while time.monotonic() < expires:
            request = self._request("process.poll", handle, limit=limit)
            body = self._decode(request, executor.poll(request, cursor=cursor))
            if body["process_state"] == "EXITED":
                return body
            time.sleep(0.025)
        self.fail("owned direct child did not reach a proven exit")

    def _outside_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
        self.addCleanup(cleanup)
        return child

    def _exhaust_running_polls(self, executor, handle):
        # This fixture never changes Git identity during exhaustion. Observe
        # it once; every ledger/session/capability/registry/path check remains
        # real and fresh. Terminate and final readback run outside this scope.
        current = authorization_module.inspect_project(self.root)
        with mock.patch.object(authorization_module, "inspect_project", return_value=current):
            for _ in range(64):
                request = self._request("process.poll", handle)
                outcome = executor.poll(request)
                self.assertLessEqual(len(executor._outcomes), 64)
                if outcome.state == "REFUSED":
                    self.assertEqual(outcome.error_code, "process_operation_capacity")
                    return request
                self.assertEqual(self._decode(request, outcome)["process_state"], "RUNNING")
        self.fail("ordinary polls did not reach bounded admission")

    def _reserved_exit(self, executor, handle):
        expires = time.monotonic() + 4
        while time.monotonic() < expires:
            request = self._request("process.poll", handle)
            outcome = executor.poll(request)
            if outcome.state == "SUCCEEDED":
                body = self._decode(request, outcome)
                if body["process_state"] == "EXITED":
                    return request, outcome, body
            else:
                self.assertEqual((outcome.state, outcome.error_code),
                                 ("REFUSED", "process_operation_capacity"))
            self.assertLessEqual(len(executor._outcomes), 64)
            time.sleep(.025)
        self.fail("reserved terminal readback did not reach a proven exit")

    def test_exhausted_live_child_retains_terminate_and_terminal_readback(self):
        executor = self._executor(self._profile(
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready'); time.sleep(600)",
            deadline=600))
        start, first, started = self._start(executor)
        handle = started["handle"]
        pending = self._exhaust_running_polls(executor, handle)
        self.assertIsNone(executor._children[handle].process.poll())
        cached_count = len(executor._outcomes)
        tiny_outcomes = []
        for kind in ("process.poll", "process.terminate"):
            tiny = self._request(kind, handle, limit=1)
            outcome = (executor.poll(tiny) if kind == "process.poll"
                       else executor.terminate(tiny))
            self.assertEqual(outcome.state, "REFUSED")
            tiny_outcomes.append(outcome)
        invalid = self._request("process.poll", handle)
        invalid_outcome = executor.poll(invalid, cursor=100000)
        self.assertEqual(invalid_outcome.state, "REFUSED")
        self.assertEqual(len(executor._outcomes), cached_count)
        self.assertIsNone(executor._children[handle].stop_reason)
        terminate = self._request("process.terminate", handle)
        stopped = executor.terminate(terminate)
        self._decode(terminate, stopped)
        for outcome in tiny_outcomes:
            self.assertEqual(outcome.error_code, "process_envelope_too_small")
        self.assertEqual(invalid_outcome.error_code, "process_cursor_denied")
        final_request, final_outcome, final = self._reserved_exit(executor, handle)
        self.assertEqual((final["reason"], final["exit_code"]), ("terminated", -9))
        self.assertNotEqual(final_request.operation_id, pending.operation_id)
        self.assertLessEqual(len(executor._outcomes), 64)
        self.assertEqual(executor.start(start), first)
        self.assertEqual(executor.terminate(terminate), stopped)
        self.assertEqual(executor.poll(final_request), final_outcome)
        self.assertEqual(executor.poll(final_request, cursor=1).error_code,
                         "process_operation_conflict")
        conflict = dataclasses.replace(terminate, kind="process.poll")
        self.assertEqual(executor.poll(conflict).error_code, "process_operation_conflict")

    def test_exhaustion_preserves_each_of_two_owned_children(self):
        executor = self._executor(self._profile("import time; time.sleep(600)", deadline=600))
        handles = [self._start(executor)[2]["handle"] for _ in range(2)]
        self._exhaust_running_polls(executor, handles[0])
        with self.orch.connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0]
        denied = self._request("process.start", "python")
        self.assertEqual(executor.start(denied).error_code, "process_operation_capacity")
        for handle in handles:
            self.assertIsNone(executor._children[handle].process.poll())
            request = self._request("process.terminate", handle)
            self._decode(request, executor.terminate(request))
        for handle in handles:
            self.assertEqual(self._reserved_exit(executor, handle)[2]["reason"], "terminated")
        for _ in range(8):
            request = self._request("process.terminate", handles[0])
            self.assertEqual(executor.terminate(request).error_code, "process_operation_capacity")
        self.assertLessEqual(len(executor._outcomes), 64)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], before)

    def test_natural_exit_after_exhaustion_keeps_terminal_and_terminate_slots(self):
        release = self.root / "release-child"
        code = "import os,time\nwhile not os.path.exists(" + repr(str(release)) + "): time.sleep(.01)\nprint('natural-exit')"
        executor = self._executor(self._profile(code, deadline=600))
        start, first, started = self._start(executor)
        handle = started["handle"]
        pending = self._exhaust_running_polls(executor, handle)
        self.assertIsNone(executor._children[handle].process.poll())
        release.write_text("release", encoding="ascii")
        final_request, final_outcome, final = self._reserved_exit(executor, handle)
        self.assertEqual((final["reason"], final["exit_code"], final["bytes"]),
                         ("exited", 0, b"natural-exit\n"))
        self.assertNotEqual(pending.operation_id, final_request.operation_id)
        terminate = self._request("process.terminate", handle)
        stopped = executor.terminate(terminate)
        self.assertEqual(self._decode(terminate, stopped)["process_state"], "EXITED")
        self.assertEqual(executor.start(start), first)
        self.assertEqual(executor.poll(final_request), final_outcome)
        self.assertEqual(executor.terminate(terminate), stopped)
        self.assertLessEqual(len(executor._outcomes), 64)

    def test_deadline_exit_after_exhaustion_keeps_terminal_readback(self):
        executor = self._executor(self._profile("import time; time.sleep(600)", deadline=5))
        _, _, started = self._start(executor)
        handle = started["handle"]
        self._exhaust_running_polls(executor, handle)
        self.assertIsNone(executor._children[handle].process.poll())
        executor._children[handle].thread.join(timeout=7)
        final = self._reserved_exit(executor, handle)[2]
        self.assertEqual(final["reason"], "deadline")
        self.assertIsInstance(final["exit_code"], int)
        terminate = self._request("process.terminate", handle)
        self.assertEqual(self._decode(terminate, executor.terminate(terminate))["process_state"], "EXITED")
        self.assertLessEqual(len(executor._outcomes), 64)

    def test_unknown_exit_after_exhaustion_cannot_spend_safety_slots(self):
        release = self.root / "release-unknown-child"
        code = "import os,time\nwhile not os.path.exists(" + repr(str(release)) + "): time.sleep(.01)"
        executor = self._executor(self._profile(code, deadline=600))
        start, first, started = self._start(executor)
        handle = started["handle"]
        self._exhaust_running_polls(executor, handle)
        cached_count = len(executor._outcomes)
        with mock.patch.object(executor._store, "observe_exit", side_effect=OSError("unconfirmed")):
            release.write_text("release", encoding="ascii")
            executor._children[handle].thread.join(timeout=3)
            for kind in ("process.poll", "process.terminate"):
                request = self._request(kind, handle)
                for _ in range(2):
                    outcome = (executor.poll(request) if kind == "process.poll"
                               else executor.terminate(request))
                    self.assertEqual((outcome.state, outcome.error_code),
                                     ("REFUSED", "process_operation_capacity"))
        self.assertEqual(len(executor._outcomes), cached_count)
        self.assertIsNone(executor._children[handle].stop_reason)
        self.assertEqual(executor.start(start), first)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)

    def test_sequential_lifecycles_cannot_dispatch_without_safety_capacity(self):
        executor = self._executor(self._profile("pass"))
        first_start = first_outcome = None
        completed = 0
        for _ in range(22):
            request = self._request("process.start", "python")
            outcome = executor.start(request)
            if outcome.state == "REFUSED":
                self.assertEqual(outcome.error_code, "process_operation_capacity")
                break
            body = self._decode(request, outcome)
            first_start, first_outcome = first_start or request, first_outcome or outcome
            child = executor._children[body["handle"]]
            child.thread.join(timeout=3)
            terminate = self._request("process.terminate", body["handle"])
            self._decode(terminate, executor.terminate(terminate))
            final = self._request("process.poll", body["handle"])
            self.assertEqual(self._decode(final, executor.poll(final))["process_state"], "EXITED")
            completed += 1
            self.assertLessEqual(len(executor._outcomes), 64)
        else:
            self.fail("start dispatched without room for the admitted lifecycle")
        self.assertEqual(completed, 21)
        self.assertEqual(executor.start(first_start), first_outcome)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], completed)

    def test_exit_code_is_proof_separate_from_successful_api(self):
        executor = self._executor(self._profile("print('child-output'); raise SystemExit(7)"))
        request, outcome, started = self._start(executor)
        self.assertTrue(started["handle"].startswith("proc-"))
        self.assertNotIn("pid", started)
        final = self._exit(executor, started["handle"])
        self.assertEqual((final["exit_code"], final["reason"], final["bytes"]),
                         (7, "exited", b"child-output\n"))
        self.assertEqual(executor.start(request), outcome)

    def test_frozen_argv_environment_cwd_and_devnull_stdin(self):
        args = ["-c", "import os,sys; print(sys.argv[1]); print(os.getenv('UNRELATED_SECRET','absent')); print(os.getenv('LANG')); print(os.getcwd()); print(repr(sys.stdin.read()))", "frozen"]
        env = {"LANG": "C"}
        profile = self._profile(args=args, env=env)
        args[-1] = "changed"
        env["LANG"] = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.args = ()
        with self.assertRaises(TypeError):
            profile.env["LANG"] = "changed"
        with mock.patch.dict(os.environ, {"UNRELATED_SECRET": "private-not-inherited"}):
            executor = self._executor(profile)
            _, _, started = self._start(executor)
            final = self._exit(executor, started["handle"])
        self.assertEqual(final["bytes"],
                         ("frozen\nabsent\nC\n" + str(self.root) + "\n''\n").encode())

    def test_noisy_merged_stream_is_drained_and_retention_is_bounded(self):
        executor = self._executor(self._profile("import os; os.write(1,b'o'*200000); os.write(2,b'e'*200000)"))
        _, _, started = self._start(executor)
        final = self._exit(executor, started["handle"])
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(final["total_output_bytes"], 400000)
        self.assertEqual(final["retained_output_bytes"], 65536)
        self.assertTrue(final["truncated"])
        retained = bytearray(final["bytes"])
        cursor = final["next_cursor"]
        while cursor < 65536:
            request = self._request("process.poll", started["handle"], limit=1024)
            body = self._decode(request, executor.poll(request, cursor=cursor))
            self.assertEqual(body["cursor"], cursor)
            self.assertGreater(body["next_cursor"], cursor)
            retained.extend(body["bytes"])
            cursor = body["next_cursor"]
        self.assertEqual(bytes(retained), b"o" * 65536)

    def test_watchdog_enforces_deadline_without_worker_poll(self):
        executor = self._executor(self._profile("import time; print('ready'); time.sleep(10)", deadline=.1))
        _, _, started = self._start(executor)
        time.sleep(.45)
        request = self._request("process.poll", started["handle"])
        final = self._decode(request, executor.poll(request))
        self.assertEqual(final["process_state"], "EXITED")
        self.assertEqual(final["reason"], "deadline")
        self.assertIsInstance(final["exit_code"], int)

    def test_terminate_reaps_owned_child_and_leaves_unrelated_process_alive(self):
        unrelated = self._outside_child()
        executor = self._executor(self._profile("import time; time.sleep(10)"))
        _, _, started = self._start(executor)
        request = self._request("process.terminate", started["handle"])
        self._decode(request, executor.terminate(request))
        final = self._exit(executor, started["handle"])
        self.assertEqual(final["reason"], "terminated")
        self.assertIsNone(unrelated.poll())

    def test_tiny_start_or_terminate_envelope_refuses_before_effect(self):
        executor = self._executor(self._profile("import time; time.sleep(10)"))
        tiny = self._request("process.start", "python", limit=1)
        self.assertEqual(executor.start(tiny).state, "REFUSED")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 0)
        _, _, started = self._start(executor)
        tiny = self._request("process.terminate", started["handle"], limit=1)
        self.assertEqual(executor.terminate(tiny).state, "REFUSED")
        time.sleep(.05)
        poll = self._request("process.poll", started["handle"])
        self.assertEqual(self._decode(poll, executor.poll(poll))["process_state"], "RUNNING")

    def test_duplicate_poll_is_cached_and_cursor_or_request_conflicts_refuse(self):
        executor = self._executor(self._profile("print('abcdefgh')"))
        _, _, started = self._start(executor)
        final = self._exit(executor, started["handle"])
        request = self._request("process.poll", started["handle"], op="stable-poll")
        first = executor.poll(request, cursor=0)
        self.assertEqual(executor.poll(request, cursor=0), first)
        self.assertEqual(executor.poll(request, cursor=1).state, "REFUSED")
        altered = dataclasses.replace(request, max_output_bytes=1000)
        self.assertEqual(executor.poll(altered).state, "REFUSED")
        fresh = self._request("process.poll", started["handle"])
        body = self._decode(fresh, executor.poll(fresh, cursor=3))
        self.assertEqual(body["bytes"], b"defgh\n")
        self.assertEqual(final["retained_output_bytes"], 9)

    def test_invalid_cursors_raw_shell_profile_and_raw_pid_are_refused(self):
        unrelated = self._outside_child()
        executor = self._executor()
        shell = self._request("process.start", "bash")
        self.assertEqual(executor.start(shell).state, "REFUSED")
        pid = self._request("process.terminate", str(unrelated.pid))
        self.assertEqual(executor.terminate(pid).state, "REFUSED")
        _, _, started = self._start(executor)
        self._exit(executor, started["handle"])
        for cursor in (-1, True, 1.5, 100000):
            request = self._request("process.poll", started["handle"])
            self.assertEqual(executor.poll(request, cursor=cursor).state, "REFUSED")
        self.assertIsNone(unrelated.poll())

    def test_profile_validation_rejects_unsafe_env_limits_and_cwd(self):
        for deadline in (.049, 601, True, float("nan"), float("inf")):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                self._profile(deadline=deadline)
        for env in ({"LD_PRELOAD": "/tmp/evil"}, {"PRIVATE_TOKEN": "secret"}):
            with self.subTest(env=tuple(env)), self.assertRaises(ValueError):
                self._profile(env=env)
        other = self.base / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            self._executor(self._profile(cwd=other))

    def test_executable_replacement_denied_before_dispatch(self):
        copied = self.root / "copied-python"
        shutil.copyfile(Path(sys.executable).resolve(), copied)
        copied.chmod(0o700)
        profile = self._profile(executable=copied)
        executor = self._executor(profile)
        with copied.open("ab") as output:
            output.write(b"changed")
        request = self._request("process.start", "python")
        self.assertEqual(executor.start(request).state, "REFUSED")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 0)

    def test_cross_session_and_binding_never_observe_or_signal_child(self):
        executor = self._executor(self._profile("import time; time.sleep(10)"))
        _, _, started = self._start(executor)
        other_authority = LocalRunSessionAuthority(self.orch)
        other_session = other_authority.open_session(self.binding, self.cap)
        foreign = self._executor(authority=other_authority, session=other_session)
        request = self._request("process.poll", started["handle"])
        self.assertEqual(foreign.poll(request).state, "REFUSED")
        request = self._request("process.terminate", started["handle"])
        self.assertEqual(foreign.terminate(request).state, "REFUSED")
        wrong = dataclasses.replace(self.binding, attempt=self.binding.attempt + 1)
        request = self._request("process.poll", started["handle"], binding=wrong)
        self.assertEqual(executor.poll(request).state, "REFUSED")
        request = self._request("process.poll", started["handle"])
        self.assertEqual(self._decode(request, executor.poll(request))["process_state"], "RUNNING")

    def test_revoked_authority_refuses_cached_bytes_and_cleans_held_child(self):
        unrelated = self._outside_child()
        executor = self._executor(self._profile("import time; time.sleep(10)"))
        request, _, started = self._start(executor)
        self.cap.chmod(0o644)
        self.assertEqual(executor.start(request).state, "REFUSED")
        time.sleep(.3)
        self.cap.chmod(0o600)
        final = self._exit(executor, started["handle"])
        self.assertEqual(final["reason"], "owner_cleanup")
        self.assertIsNone(unrelated.poll())

    def test_restart_never_replays_start_or_reconstructs_signal_target(self):
        unrelated = self._outside_child()
        executor = self._executor(self._profile("import time; time.sleep(10)"))
        request, _, started = self._start(executor)
        restarted = self._executor(self._profile("import time; time.sleep(10)"))
        self.assertEqual(restarted.start(request).state, "UNKNOWN")
        request = self._request("process.poll", started["handle"])
        self.assertEqual(restarted.poll(request).state, "UNKNOWN")
        request = self._request("process.terminate", started["handle"])
        self.assertEqual(restarted.terminate(request).state, "UNKNOWN")
        restarted.close()
        self.assertIsNone(unrelated.poll())
        request = self._request("process.poll", started["handle"])
        self.assertEqual(self._decode(request, executor.poll(request))["process_state"], "RUNNING")

    def test_ambiguous_spawn_keeps_intent_unknown_and_never_retries(self):
        executor = self._executor(self._profile("import time; time.sleep(10)"))
        request = self._request("process.start", "python")
        with mock.patch.object(module, "_spawn_process", side_effect=OSError("ambiguous")):
            first = executor.start(request)
        self.assertEqual(first.state, "UNKNOWN")
        self.assertEqual(executor.start(request), first)
        restarted = self._executor(self._profile("import time; time.sleep(10)"))
        self.assertEqual(restarted.start(request).state, "UNKNOWN")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)

    def test_cache_capacity_cannot_evict_start_identity_and_replay(self):
        executor = self._executor(self._profile("print('done')"))
        start, first, started = self._start(executor)
        self._exit(executor, started["handle"])
        for index in range(64):
            request = self._request("process.poll", started["handle"], op="capacity-" + str(index))
            outcome = executor.poll(request)
            self.assertIn(outcome.state, ("SUCCEEDED", "REFUSED"))
        self.assertEqual(executor.start(start), first)
        request = self._request("process.start", "python", op="capacity-new-start")
        self.assertEqual(executor.start(request).state, "REFUSED")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 1)

    def test_restart_conflicting_request_refuses_and_live_dispatch_cap_refuses(self):
        profile = self._profile("import time; time.sleep(10)")
        alternate = self._profile("import time; time.sleep(10)", name="alternate")
        executor = self._executor(profile, alternate)
        request, _, started = self._start(executor)
        restarted = self._executor(profile, alternate)
        conflict = dataclasses.replace(request, target="alternate")
        self.assertEqual(restarted.start(conflict).state, "REFUSED")
        self._start(executor, target="alternate")
        third = self._request("process.start", "python")
        self.assertEqual(executor.start(third).state, "REFUSED")
        poll = self._request("process.poll", started["handle"])
        self.assertEqual(self._decode(poll, executor.poll(poll))["process_state"], "RUNNING")

    def test_ambiguous_terminal_commit_never_retries_or_claims_proven_exit(self):
        executor = self._executor(self._profile("print('exit-marker')"))
        original = executor._store.observe_exit
        def committed_but_lost(*args):
            original(*args)
            raise OSError("lost reply after actual commit")
        with mock.patch.object(executor._store, "observe_exit", side_effect=committed_but_lost):
            _, _, started = self._start(executor)
            time.sleep(.3)
            request = self._request("process.poll", started["handle"])
            result = executor.poll(request)
        self.assertEqual((result.state, result.output), ("UNKNOWN", b""))
        request = self._request("process.poll", started["handle"])
        self.assertEqual(executor.poll(request).state, "UNKNOWN")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 1)

    def test_reaped_child_without_settled_eof_stays_unknown(self):
        # A real external owner holds the injected pipe open. This models a
        # violated no-descendants profile without discovering or killing a PID.
        executor = self._executor(self._profile("pass"))
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"],
                                   stdin=subprocess.DEVNULL, stdout=write_fd,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        os.close(write_fd)
        def cleanup():
            if outside.poll() is None:
                outside.terminate()
            outside.wait(timeout=3)
        self.addCleanup(cleanup)
        original = module._spawn_process
        def unsettled(profile):
            process = original(profile)
            process.stdout.close()
            process.stdout = stream
            return process
        with mock.patch.object(module, "_spawn_process", side_effect=unsettled):
            _, _, started = self._start(executor)
        time.sleep(.7)
        request = self._request("process.poll", started["handle"])
        result = executor.poll(request)
        self.assertEqual((result.state, result.output), ("UNKNOWN", b""))
        self.assertIsNone(outside.poll())
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)

    def test_watchdog_kills_direct_child_that_ignores_terminate(self):
        executor = self._executor(self._profile("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready'); time.sleep(10)", deadline=.15))
        _, _, started = self._start(executor)
        time.sleep(.65)
        request = self._request("process.poll", started["handle"])
        final = self._decode(request, executor.poll(request))
        self.assertEqual((final["process_state"], final["exit_code"], final["reason"]),
                         ("EXITED", -9, "deadline"))

    def test_sqlite_readback_failure_after_intent_commit_is_unknown_without_spawn(self):
        executor = self._executor()
        request = self._request("process.start", "python")
        with mock.patch.object(executor._store, "_readback",
                               side_effect=sqlite3.OperationalError("private sqlite failure")):
            result = executor.start(request)
        self.assertEqual((result.state, result.output), ("UNKNOWN", b""))
        self.assertEqual(executor.start(request), result)
        restarted = self._executor()
        self.assertEqual(restarted.start(request).state, "UNKNOWN")
        self.assertEqual(len(executor._children), 0)
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)

    def test_sqlite_terminal_failure_stays_unknown_with_no_terminal_proof(self):
        executor = self._executor()
        with mock.patch.object(executor._store, "observe_exit",
                               side_effect=sqlite3.OperationalError("private sqlite failure")):
            _, _, started = self._start(executor)
            time.sleep(.3)
            request = self._request("process.poll", started["handle"])
            result = executor.poll(request)
        self.assertEqual((result.state, result.output), ("UNKNOWN", b""))
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 0)

    def test_supervisor_and_initial_cleanup_failure_remain_unknown_with_held_child(self):
        executor = self._executor(self._profile("import time; time.sleep(10)"))
        request = self._request("process.start", "python")
        with mock.patch.object(module.threading.Thread, "start", side_effect=RuntimeError("launch failure")), \
                mock.patch.object(subprocess.Popen, "kill", side_effect=OSError("signal failure")):
            result = executor.start(request)
        self.assertEqual((result.state, result.output), ("UNKNOWN", b""))
        self.assertEqual(len(executor._children), 1)
        held = next(iter(executor._children.values())).process
        self.assertIsNone(held.poll())
        executor.close()
        self.assertIsNotNone(held.returncode)

    def test_authority_revoked_after_durable_dispatch_prevents_spawn(self):
        executor = self._executor()
        original = executor._store.dispatch
        def revoke_after_dispatch(*args):
            intent = original(*args)
            self.cap.chmod(0o644)
            return intent
        request = self._request("process.start", "python")
        with mock.patch.object(executor._store, "dispatch", side_effect=revoke_after_dispatch):
            result = executor.start(request)
        self.assertEqual((result.state, result.output), ("UNKNOWN", b""))
        self.assertEqual(len(executor._children), 0)
        self.cap.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
