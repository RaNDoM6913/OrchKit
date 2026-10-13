"""Real SQLite dispatch, immutable exit evidence and native closure guards."""
import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from orch.core import Orchestrator
from orch.bridge_patch_execution import runtime_operation_lock
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding
try:
    from orch import bridge_process_store as module
except ImportError:
    module = None


class ProcessStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, "Durable process store is not implemented")
        temporary = tempfile.TemporaryDirectory(prefix="orch-process-store-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.orch, self.binding, self.claim, self.lease, self.receipt = self._fixture("primary")
        self.store = module.ProcessOperationStore(self.orch)

    def _fixture(self, label, *, project_id="process-fixture"):
        home, workspace = self.base / label, self.base / (label + "-workspace")
        workspace.mkdir()
        orch = Orchestrator(home)
        task = {"id": "TASK-" + label, "goal": "Process closure fixture",
                "workspace": str(workspace), "dependencies": [],
                "allowed_paths": ["result.json"], "protected_paths": {},
                "checks": [], "required_review": False, "owner_acceptance": False,
                "publication": {"kind": "none"}, "max_attempts": 2}
        if project_id is not None:
            task["project_id"] = project_id
        plan = home / "fixture-plan.json"
        plan.write_text(json.dumps({"schema_version": 1, "plan_revision": label, "tasks": [task]}))
        orch.load_plan(plan)
        claim = orch.claim("fixture")
        self.assertEqual(claim["status"], "CLAIMED", claim)
        with orch.connect() as conn:
            row = conn.execute("SELECT r.run_id,r.task_id,r.attempt,t.project_id,t.writer_key "
                               "FROM runs r JOIN tasks t ON t.task_id=r.task_id WHERE r.run_id=?",
                               (claim["run_id"],)).fetchone()
        binding = WorkerRunBinding(**dict(row)) if project_id is not None else dict(row)
        lease = orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({"run_id": claim["run_id"], "task_id": claim["task_id"],
                                      "changed_paths": ["result.json"], "summary": "fixture"}))
        return orch, binding, claim, lease, receipt

    def _request(self, operation_id="start-1", **updates):
        values = dict(binding=self.binding, operation_id=operation_id,
                      kind="process.start", target="disposable-profile")
        values.update(updates)
        return WorkerOperationRequest(**values)

    def _dispatch(self, operation_id="start-1", **updates):
        with runtime_operation_lock(self.orch.root):
            return self.store.dispatch(self._request(operation_id, **updates), "a" * 64,
                                       "b" * 64, "owned-" + operation_id)

    def _exit(self, intent, **updates):
        values = dict(exit_code=0, reason="exited", output_bytes=9, truncated=False)
        values.update(updates)
        with runtime_operation_lock(self.orch.root):
            return self.store.observe_exit(intent, **values)

    def _rows(self, kind=None):
        with self.orch.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM events WHERE kind LIKE 'PROCESS_%'" + (" AND kind=?" if kind else ""),
                (kind,) if kind else ()).fetchall()]

    def _insert(self, kind, payload, *, run_id=None, task_id=None):
        with self.orch.connect() as conn:
            conn.execute("INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES(?,?,?,?,?)",
                         ("fixture", kind, task_id or self.binding.task_id,
                          run_id or self.binding.run_id,
                          payload if isinstance(payload, str) else json.dumps(payload)))

    def test_dispatch_is_safe_metadata_and_independently_visible_after_full_commit(self):
        intent = self._dispatch()
        self.assertEqual(intent["request"], dataclasses.asdict(self._request()))
        self.assertGreater(intent["seq"], 0)
        payload = {key: value for key, value in intent.items() if key not in ("seq", "hash")}
        expected_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                      allow_nan=False, separators=(",", ":")).encode("ascii")).hexdigest()
        self.assertEqual(intent["hash"], expected_hash)
        self.assertEqual(set(payload), {"schema_version", "request", "session_hash", "profile_hash", "handle"})
        with runtime_operation_lock(self.orch.root):
            self.assertEqual(module.ProcessOperationStore(self.orch).intents(self.binding), [intent])
        self.assertEqual(len(self._rows("PROCESS_DISPATCH_INTENT")), 1)

    def test_duplicate_dispatch_cannot_authorize_respawn_or_replace_metadata(self):
        self._dispatch()
        for updates in ({}, {"target": "other-profile"}, {"max_output_bytes": 123}):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "process_"):
                self._dispatch(**updates)
        self.assertEqual(len(self._rows("PROCESS_DISPATCH_INTENT")), 1)

    def test_capacity_limits_unresolved_and_total_without_erasing_history(self):
        first, second = self._dispatch("start-1"), self._dispatch("start-2")
        with self.assertRaisesRegex(ValueError, "process_capacity"):
            self._dispatch("start-3")
        self._exit(first)
        self._exit(second)
        for number in range(3, 65):
            self._exit(self._dispatch("start-" + str(number)))
        with self.assertRaisesRegex(ValueError, "process_capacity"):
            self._dispatch("start-65")
        self.assertEqual(len(self._rows("PROCESS_DISPATCH_INTENT")), 64)
        self.assertEqual(self.orch.submit(self.binding.run_id, self.lease, self.receipt)["status"], "RESULT_SUBMITTED")
        self.assertEqual(self.orch.quiesce(self.binding.run_id, self.lease)["status"], "VERIFYING")

    def test_immutable_binding_session_and_handle_conflicts_fail_closed(self):
        self._dispatch()
        for field, value in (("attempt", 2), ("task_id", "foreign"), ("writer_key", "foreign")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "process_"):
                self._dispatch("other-" + field, binding=dataclasses.replace(self.binding, **{field: value}))
        with runtime_operation_lock(self.orch.root):
            with self.assertRaisesRegex(ValueError, "process_"):
                self.store.dispatch(self._request("other"), "c" * 64, "b" * 64, "owned-start-1")
        with self.orch.connect() as conn:
            conn.execute("UPDATE runs SET state='RESULT_SUBMITTED' WHERE run_id=?", (self.binding.run_id,))
        with self.assertRaisesRegex(ValueError, "process_"):
            self._dispatch("inactive")

    def test_exit_links_exact_intent_and_duplicate_or_conflict_is_refused(self):
        intent = self._dispatch()
        forged = dict(intent, hash="f" * 64)
        with self.assertRaisesRegex(ValueError, "process_"):
            self._exit(forged)
        terminal = self._exit(intent, exit_code=-15, reason="terminated", output_bytes=1000000, truncated=True)
        self.assertEqual((terminal["intent_seq"], terminal["intent_hash"]), (intent["seq"], intent["hash"]))
        self.assertEqual((terminal["exit_code"], terminal["output_bytes"], terminal["truncated"]), (-15, 1000000, True))
        for updates in ({}, {"exit_code": 1}):
            with self.assertRaisesRegex(ValueError, "process_"):
                self._exit(intent, **updates)
        self.assertEqual(len(self._rows("PROCESS_EXIT_OBSERVED")), 1)

    def test_terminal_metadata_validation_does_not_persist_unproven_exit(self):
        intent = self._dispatch()
        for updates in ({"exit_code": None}, {"exit_code": True}, {"reason": "secret output"},
                        {"reason": "unknown"}, {"output_bytes": -1}, {"truncated": 1}):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "process_"):
                self._exit(intent, **updates)
        self.assertEqual(self._rows("PROCESS_EXIT_OBSERVED"), [])

    def test_malformed_orphan_and_cross_binding_records_refuse_closure(self):
        cases = ("malformed", "orphan", "cross-binding", "duplicate", "hidden-run")
        for case in cases:
            with self.subTest(case=case):
                orch, binding, claim, lease, receipt = self._fixture(case)
                store = module.ProcessOperationStore(orch)
                request = WorkerOperationRequest(binding, "start", "process.start", "profile")
                with runtime_operation_lock(orch.root):
                    intent = store.dispatch(request, "a" * 64, "b" * 64, "owned-handle")
                    terminal = store.observe_exit(intent, 0, "exited", 0, False)
                payload = {k: v for k, v in terminal.items() if k not in ("seq", "hash")}
                if case == "malformed": payload = "{"
                elif case == "orphan": payload["intent_seq"] += 500
                elif case in ("cross-binding", "hidden-run"):
                    payload["request"] = copy.deepcopy(payload["request"])
                    if case == "cross-binding": payload["request"]["binding"]["attempt"] += 1
                with orch.connect() as conn:
                    conn.execute("INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES(?,?,?,?,?)",
                        ("fixture", "PROCESS_EXIT_OBSERVED", binding.task_id,
                         "foreign-row" if case == "hidden-run" else binding.run_id,
                         payload if isinstance(payload, str) else json.dumps(payload)))
                with self.assertRaisesRegex(ValueError, "process_"):
                    orch.submit(claim["run_id"], lease, receipt)

    def test_pending_intent_blocks_submit_abort_and_boolean_waiver(self):
        self._dispatch()
        for invoke in (lambda: self.orch.submit(self.binding.run_id, self.lease, self.receipt),
                       lambda: self.orch.abort(self.binding.run_id, "fixture"),
                       lambda: self.orch.abort(self.binding.run_id, "fixture", process_inactivity_confirmed=True)):
            with self.assertRaisesRegex(ValueError, "process_unresolved"):
                invoke()
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM runs WHERE run_id=?", (self.binding.run_id,)).fetchone()[0], "RUNNING")
        self.assertTrue(Path(self.claim["capability_file"]).exists())

    def test_exit_and_native_authority_release_share_full_acknowledgement(self):
        intent = self._dispatch()
        terminal = self._exit(intent)
        self.assertEqual(self.orch.submit(self.binding.run_id, self.lease, self.receipt)["status"], "RESULT_SUBMITTED")
        self.assertEqual(self.orch.quiesce(self.binding.run_id, self.lease)["status"], "VERIFYING")
        acknowledgements = self._rows("PROCESS_CLOSURE_ACKNOWLEDGED")
        self.assertEqual(len(acknowledgements), 2)
        for row in acknowledgements:
            self.assertEqual(json.loads(row["payload_json"])["exits"], [{"intent_seq": intent["seq"],
                              "intent_hash": intent["hash"], "exit_seq": terminal["seq"], "exit_hash": terminal["hash"]}])
        self.assertFalse(Path(self.claim["capability_file"]).exists())

    def test_quiesce_refuses_new_unknown_evidence_even_after_submission(self):
        self.orch.submit(self.binding.run_id, self.lease, self.receipt)
        self._insert("PROCESS_DISPATCH_INTENT", {"schema_version": 1, "request": dataclasses.asdict(self._request()),
                     "session_hash": "a" * 64, "profile_hash": "b" * 64, "handle": "owned-unknown"})
        with self.assertRaisesRegex(ValueError, "process_unresolved"):
            self.orch.quiesce(self.binding.run_id, self.lease)
        self.assertTrue(Path(self.claim["capability_file"]).exists())

    def test_legacy_absence_keeps_native_transitions_usable(self):
        orch, _, claim, lease, receipt = self._fixture("legacy", project_id=None)
        self.assertEqual(orch.submit(claim["run_id"], lease, receipt)["status"], "RESULT_SUBMITTED")
        self.assertEqual(orch.quiesce(claim["run_id"], lease)["status"], "VERIFYING")

    def test_guard_requires_existing_full_transaction_and_rejects_weaker_durability(self):
        self._exit(self._dispatch())
        with self.orch.connect() as conn:
            with self.assertRaisesRegex(ValueError, "process_"):
                module.require_processes_resolved(conn, self.binding.run_id, self.binding)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "process_"):
                module.require_processes_resolved(conn, self.binding.run_id, self.binding)
            conn.execute("ROLLBACK")

    def test_commit_response_loss_preserves_intent_and_prevents_repeat_dispatch(self):
        connect = self.orch.connect
        class CommitResponseLoss:
            def __init__(self): self.inner = connect()
            def __enter__(self): return self
            def __exit__(self, *args): return self.inner.__exit__(*args)
            def __getattr__(self, name): return getattr(self.inner, name)
            def execute(self, sql, *args):
                result = self.inner.execute(sql, *args)
                if sql == "COMMIT": raise sqlite3.OperationalError("lost commit response")
                return result
        with mock.patch.object(self.orch, "connect", side_effect=CommitResponseLoss):
            with self.assertRaises((ValueError, sqlite3.Error)):
                self._dispatch()
        self.assertEqual(len(self._rows("PROCESS_DISPATCH_INTENT")), 1)
        with self.assertRaisesRegex(ValueError, "process_"):
            self._dispatch()

    def test_failed_native_commit_does_not_release_authority_or_acknowledge_exit(self):
        self._exit(self._dispatch())
        connect = self.orch.connect
        class FailedCommit:
            def __init__(self): self.inner = connect()
            def __enter__(self): return self
            def __exit__(self, *args): return self.inner.__exit__(*args)
            def __getattr__(self, name): return getattr(self.inner, name)
            def execute(self, sql, *args):
                if sql == "COMMIT": raise sqlite3.OperationalError("commit failed")
                return self.inner.execute(sql, *args)
        with mock.patch.object(self.orch, "connect", side_effect=FailedCommit):
            with self.assertRaises(sqlite3.Error):
                self.orch.submit(self.binding.run_id, self.lease, self.receipt)
        self.assertEqual(self._rows("PROCESS_CLOSURE_ACKNOWLEDGED"), [])
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM runs WHERE run_id=?", (self.binding.run_id,)).fetchone()[0], "RUNNING")
        self.assertTrue(Path(self.claim["capability_file"]).exists())

    def test_native_acknowledgement_must_read_back_exact_bytes_inside_transaction(self):
        self._exit(self._dispatch())
        with self.orch.connect() as conn:
            conn.execute("CREATE TRIGGER corrupt_process_ack AFTER INSERT ON events "
                         "WHEN NEW.kind='PROCESS_CLOSURE_ACKNOWLEDGED' BEGIN "
                         "UPDATE events SET payload_json='{}' WHERE seq=NEW.seq; END")
        with self.assertRaisesRegex(ValueError, "process_unresolved"):
            self.orch.submit(self.binding.run_id, self.lease, self.receipt)
        self.assertEqual(self._rows("PROCESS_CLOSURE_ACKNOWLEDGED"), [])
        with self.orch.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM runs WHERE run_id=?", (self.binding.run_id,)).fetchone()[0], "RUNNING")

    def test_independent_readback_loss_never_returns_dispatch_as_spawn_authority(self):
        connect = self.orch.connect
        class ReadbackLoss:
            def __init__(self): self.inner = connect()
            def __enter__(self): return self
            def __exit__(self, *args): return self.inner.__exit__(*args)
            def __getattr__(self, name): return getattr(self.inner, name)
            def execute(self, sql, *args):
                result = self.inner.execute(sql, *args)
                if sql == "COMMIT":
                    self.inner.execute("UPDATE events SET payload_json='{}' WHERE kind='PROCESS_DISPATCH_INTENT'")
                return result
        with mock.patch.object(self.orch, "connect", side_effect=ReadbackLoss):
            with self.assertRaisesRegex(ValueError, "process_"):
                self._dispatch()
        with self.assertRaisesRegex(ValueError, "process_"):
            self._dispatch()

    def test_backup_restore_preserves_process_metadata_without_schema_migration(self):
        from orch.state import backup_state, restore_backup_archive
        intent = self._dispatch()
        self._exit(intent)
        self.assertEqual(self.orch.abort(self.binding.run_id, "backup fixture")["status"], "ABORTED")
        backup = self.base / "backup.zip"
        self.assertEqual(backup_state(self.orch, backup)["status"], "BACKED_UP")
        restored_home = self.base / "restored"
        self.assertEqual(restore_backup_archive(backup, restored_home)["status"], "RESTORED")
        restored = Orchestrator(restored_home)
        with runtime_operation_lock(restored.root):
            self.assertEqual(module.ProcessOperationStore(restored).intents(self.binding), [intent])
        with restored.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='PROCESS_EXIT_OBSERVED'").fetchone()[0], 1)

    def test_store_requires_runtime_lock_for_every_operation(self):
        with self.assertRaisesRegex(ValueError, "lock_required"):
            self.store.dispatch(self._request(), "a" * 64, "b" * 64, "owned-handle")
        intent = self._dispatch()
        with self.assertRaisesRegex(ValueError, "lock_required"):
            self.store.intents(self.binding)
        with self.assertRaisesRegex(ValueError, "lock_required"):
            self.store.observe_exit(intent, 0, "exited", 0, False)
