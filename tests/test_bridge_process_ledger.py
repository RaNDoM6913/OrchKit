"""Existing-home process journal seam with a read-only native authority."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from orch.bridge_local_broker import LocalRunBroker
from orch.bridge_patch_execution import runtime_operation_lock
from orch.bridge_process import OwnedProcessProfile
from orch.bridge_process_store import ProcessOperationStore
from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WORKER_MAX_OUTPUT_BYTES, WorkerOperationRequest, WorkerRunBinding


class ExistingProcessLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-process-ledger-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home #?%"
        self.home.mkdir(mode=0o700)
        self.orch = Orchestrator(self.home)
        self.repo = self.base / "repo"
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        def git(*args):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], env=env,
                           check=True, capture_output=True, timeout=12)
        git("init", "-q", "-b", "main", str(self.repo))
        git("-C", str(self.repo), "config", "user.email", "fixture@example.invalid")
        git("-C", str(self.repo), "config", "user.name", "Fixture")
        (self.repo / "README.md").write_text("fixture\n")
        git("-C", str(self.repo), "add", ".")
        git("-C", str(self.repo), "commit", "-qm", "fixture")
        project = ProjectRegistry(self.home).add(self.repo, name="fixture", profile="safe",
                                                  review_mode="off", allow_commit=True,
                                                  allow_push=False)["project"]
        plan = build_single_task_plan(project, task_id="P-LEDGER", goal="test process seam",
                                      allowed_paths=["README.md"], plan_revision="p-ledger")
        path = self.base / "plan.json"
        write_plan(path, plan)
        self.orch.load_plan(path)
        claim = self.orch.claim("fixture", project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED")
        self.cap = Path(claim["capability_file"])
        self.binding = WorkerRunBinding(project_id=project["project_id"], writer_key=project["writer_key"],
                                        task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"])

    def test_open_missing_or_unsafe_home_never_initializes_or_repairs(self):
        absent = self.base / "missing"
        with self.assertRaises(ValueError):
            ProcessOperationStore.open_existing(absent)
        self.assertFalse(absent.exists())
        db = self.home / ".runtime" / "orch.sqlite3"
        db.chmod(0o644)
        with self.assertRaises(ValueError):
            ProcessOperationStore.open_existing(self.home)
        self.assertEqual(db.stat().st_mode & 0o777, 0o644)

    def test_real_journal_commit_and_exit_with_readonly_authority(self):
        store = ProcessOperationStore.open_existing(self.home)
        request = WorkerOperationRequest(self.binding, "start-1", "process.start", "profile")
        with runtime_operation_lock(self.home):
            intent = store.dispatch(request, "a" * 64, "b" * 64, "owned-1")
            with self.assertRaises(ValueError):
                store.dispatch(request, "a" * 64, "b" * 64, "owned-1")
            store.observe_exit(intent, 0, "exited", 4, False)
        with self.orch.connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT kind FROM events WHERE kind LIKE 'PROCESS_%' ORDER BY seq")],
                             ["PROCESS_DISPATCH_INTENT", "PROCESS_EXIT_OBSERVED"])
            self.assertEqual(conn.execute("SELECT state FROM runs WHERE run_id=?", (self.binding.run_id,)).fetchone()[0], "RUNNING")
        with ReadOnlyAuthorityLedger(self.home).connect() as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE runs SET state='ABORTED'")

    def test_trigger_cannot_turn_process_append_into_native_mutation(self):
        with self.orch.connect() as conn:
            conn.execute("CREATE TRIGGER fixture_trigger AFTER INSERT ON events BEGIN UPDATE runs SET state='ABORTED'; END")
        store = ProcessOperationStore.open_existing(self.home)
        request = WorkerOperationRequest(self.binding, "start-trigger", "process.start", "profile")
        with runtime_operation_lock(self.home), self.assertRaises(sqlite3.DatabaseError):
            store.dispatch(request, "a" * 64, "b" * 64, "owned-trigger")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM runs WHERE run_id=?", (self.binding.run_id,)).fetchone()[0], "RUNNING")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 0)

    def test_unresolved_dispatches_remain_charged_across_store_reopen(self):
        store = ProcessOperationStore.open_existing(self.home)
        def dispatch(operation):
            request = WorkerOperationRequest(self.binding, operation, "process.start", "profile")
            return store.dispatch(request, "a" * 64, "b" * 64, "owned-" + operation)
        with runtime_operation_lock(self.home):
            first = dispatch("first")
            dispatch("second")
        store = ProcessOperationStore.open_existing(self.home)
        with runtime_operation_lock(self.home):
            with self.assertRaisesRegex(ValueError, "process_capacity"):
                dispatch("third")
            store.observe_exit(first, 0, "exited", 0, False)
            dispatch("third")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 1)

    def test_connection_refuses_native_mutation_and_unrelated_events(self):
        store = ProcessOperationStore.open_existing(self.home)
        with store._orch.connect() as conn:
            for sql, params in (
                ("UPDATE runs SET state='ABORTED'", ()),
                ("DELETE FROM events", ()),
                ("INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES(?,?,?,?,?)",
                 ("fixture", "NATIVE_EVENT", self.binding.task_id, self.binding.run_id, "{}")),
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql, params)
            with self.assertRaises(sqlite3.DatabaseError):
                conn.cursor().execute(
                    "INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES(?,?,?,?,?)",
                    ("fixture", "NATIVE_EVENT", self.binding.task_id, self.binding.run_id, "{}"))
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='NATIVE_EVENT'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT state FROM runs WHERE run_id=?", (self.binding.run_id,)).fetchone()[0], "RUNNING")

    def test_replaced_database_is_refused_without_process_journal_write(self):
        store = ProcessOperationStore.open_existing(self.home)
        db = self.home / ".runtime" / "orch.sqlite3"
        replacement = db.with_name("replacement.sqlite3")
        with sqlite3.connect(str(db)) as source, sqlite3.connect(str(replacement)) as destination:
            source.backup(destination)
        replacement.chmod(0o600)
        os.replace(replacement, db)
        request = WorkerOperationRequest(self.binding, "start-1", "process.start", "profile")
        with runtime_operation_lock(self.home), self.assertRaises(ValueError):
            store.dispatch(request, "a" * 64, "b" * 64, "owned-1")
        with sqlite3.connect(str(db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_DISPATCH_INTENT'").fetchone()[0], 0)

    def test_close_retains_owner_until_stuck_supervisor_finishes(self):
        from orch.bridge_process import LocalOwnedProcessOperations
        entered, release = threading.Event(), threading.Event()
        original = LocalOwnedProcessOperations._supervise
        def delayed(owner, child):
            entered.set()
            release.wait(5)
            original(owner, child)
        ledger = ReadOnlyAuthorityLedger(self.home)
        broker = LocalRunBroker(ledger, process_store=ProcessOperationStore.open_existing(self.home))
        profile = OwnedProcessProfile("python", Path(sys.executable).resolve(),
                                      ("-c", "import time; time.sleep(60)"), self.repo,
                                      {"LANG": "C"}, 60, WORKER_MAX_OUTPUT_BYTES)
        try:
            with mock.patch.object(LocalOwnedProcessOperations, "_supervise", delayed):
                context = broker.open_context(self.binding, self.cap, profiles=(profile,))
                request = WorkerOperationRequest(self.binding, "start", "process.start", "python")
                outcome = context.process_start(request)
                self.assertEqual(outcome.state, "SUCCEEDED")
                handle = json.loads(outcome.output)["handle"]
                owner = broker._LocalRunBroker__owned[0].processes
                child = owner._children[handle]
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                    context.close()
                self.assertTrue(broker.overview_projection()["contexts"][0]["cleanup_unresolved"])
        finally:
            release.set()
            broker.close()
        self.assertIsNotNone(child.process.poll())
        self.assertTrue(child.process.stdout.closed)
        self.assertFalse(child.thread.is_alive())

    def test_foreign_store_refused_before_context_admission(self):
        (self.base / "foreign").mkdir(mode=0o700)
        foreign = Orchestrator(self.base / "foreign")
        store = ProcessOperationStore.open_existing(foreign.root)
        with self.assertRaises(ValueError):
            LocalRunBroker(ReadOnlyAuthorityLedger(self.home), process_store=store)

    def test_real_child_runs_and_reaps_through_readonly_broker(self):
        ledger = ReadOnlyAuthorityLedger(self.home)
        store = ProcessOperationStore.open_existing(self.home)
        broker = LocalRunBroker(ledger, process_store=store)
        self.addCleanup(broker.close)
        profile = OwnedProcessProfile("python", Path(sys.executable).resolve(), ("-c", "print('seam-ok')"),
                                      self.repo, {"LANG": "C"}, 5, WORKER_MAX_OUTPUT_BYTES)
        context = broker.open_context(self.binding, self.cap, profiles=(profile,))
        start = WorkerOperationRequest(self.binding, "child-start", "process.start", "python")
        outcome = context.process_start(start)
        self.assertEqual(outcome.state, "SUCCEEDED", outcome)
        handle = json.loads(outcome.output)["handle"]
        for number in range(100):
            request = WorkerOperationRequest(self.binding, f"poll-{number}", "process.poll", handle)
            outcome = context.process_poll(request)
            if outcome.state == "SUCCEEDED" and json.loads(outcome.output)["process_state"] == "EXITED":
                break
            time.sleep(.02)
        self.assertEqual(outcome.state, "SUCCEEDED", outcome)
        self.assertEqual(json.loads(outcome.output)["process_state"], "EXITED")
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 1)
