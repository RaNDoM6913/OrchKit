import gc
import hashlib
from contextlib import closing
import dataclasses
import json
import os
import sqlite3
from pathlib import Path
import select
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from orch.core import Orchestrator, _begin_immediate, canonical_json
from orch.codex_review import prepare_review, run_review, _binary_info
from orch import bridge_operation_journal, bridge_patch_execution
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'orch'; self.root.mkdir()
        self.ws=Path(self.tmp.name)/'ws'; self.ws.mkdir()
        self.orch=Orchestrator(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def review_preflight_fixture(self):
        binary = self.root / 'fixture-codex'
        binary.write_bytes(b'fixture executable\n')
        binary.chmod(0o700)
        return {'status': 'PASS', 'binary': _binary_info(binary),
                'routing': {'model': 'gpt-6-astra', 'reasoning_effort': 'high'}}

    def load(self,tasks,revision='p1'):
        path=self.root/'plan.json'
        path.write_text(json.dumps({'schema_version':1,'plan_revision':revision,'tasks':tasks}),encoding='utf-8')
        return self.orch.load_plan(path)

    def task(self,tid='T1',deps=None,checks=None,review=False,protected=None,max_attempts=2):
        return {'id':tid,'goal':'test','workspace':str(self.ws),'dependencies':deps or [],'allowed_paths':[f'{tid}.json'],
                'protected_paths':protected or {},'checks':checks or [],'required_review':review,'owner_acceptance':False,
                'publication':{'kind':'none'},'max_attempts':max_attempts}

    def write_result(self,claim,tid='T1',value=1):
        (self.ws/f'{tid}.json').write_text(json.dumps({'value':value})+'\n',encoding='utf-8')
        receipt=Path(claim["receipt_file"])
        receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':tid,'changed_paths':[f'{tid}.json']})+'\n',encoding='utf-8')
        cap=Path(claim['capability_file'])
        lease=self.orch.lease_from_capability(claim['run_id'],cap)
        self.orch.submit(claim['run_id'],lease,receipt)
        self.orch.quiesce(claim['run_id'],lease)
        return self.orch.verify(claim['run_id'])

    def _patch_transition_fixture(self, transition, *, project_id="guard-project"):
        # Each case uses ordinary admission/claim/receipt APIs and independent
        # immutable evidence; no ledger states or capabilities are forged.
        number = len(list(self.root.glob("patch-home-*"))) + 1
        home = (self.root / f"patch-home-{number}").resolve()
        home.mkdir()
        orch = Orchestrator(home)
        tid = f"PATCH-GUARD-{number}"
        target = self.ws / f"{tid}.json"
        target.write_bytes(b"original")
        task = self.task(tid)
        if project_id is not None:
            task["project_id"] = project_id
        plan = home / "plan.json"
        plan.write_text(json.dumps({"schema_version": 1,
                                   "plan_revision": "patch-guard",
                                   "tasks": [task]}))
        orch.load_plan(plan)
        claim = orch.claim("patch-transition-worker")
        capability = Path(claim["capability_file"])
        lease = orch.lease_from_capability(claim["run_id"], capability)
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({"run_id": claim["run_id"],
                                       "task_id": tid,
                                       "changed_paths": [target.name]}))
        if transition == "quiesce":
            orch.submit(claim["run_id"], lease, receipt)
        with orch.connect() as conn:
            row = conn.execute(
                "SELECT r.task_id,r.attempt,t.project_id,t.writer_key "
                "FROM runs r JOIN tasks t ON t.task_id=r.task_id WHERE r.run_id=?",
                (claim["run_id"],),
            ).fetchone()
        binding = dict(project_id=row["project_id"], writer_key=row["writer_key"],
                       task_id=row["task_id"], run_id=claim["run_id"],
                       attempt=row["attempt"])
        return {"orch": orch, "home": home, "claim": claim, "lease": lease,
                "capability": capability, "receipt": receipt,
                "target": target, "binding": binding, "transition": transition}

    @staticmethod
    def _patch_transition_invoke(fixture):
        orch, run_id = fixture["orch"], fixture["claim"]["run_id"]
        if fixture["transition"] == "submit":
            return orch.submit(run_id, fixture["lease"], fixture["receipt"])
        if fixture["transition"] == "quiesce":
            return orch.quiesce(run_id, fixture["lease"])
        return orch.abort(run_id, "patch transition fixture")

    @staticmethod
    def _patch_transition_state(fixture):
        with fixture["orch"].connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?",
                               (fixture["claim"]["run_id"],)).fetchone()
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                (run["task_id"],)).fetchone()
            events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY seq")]
        return dict(run), dict(task), events, fixture["capability"].read_bytes()

    def _patch_transition_records(self, fixture, *, disposition=None, binding_changes=None,
                                  operation_id="guard-operation", fail_result_sync=False):
        binding = dict(fixture["binding"], **(binding_changes or {}))
        request = WorkerOperationRequest(WorkerRunBinding(**binding), operation_id,
                                         "file.patch", fixture["target"].name)
        original_bytes, replacement_bytes = b"original", b"replacement"
        replacement = {"schema_version": 1,
                       "baseline": {"sha256": hashlib.sha256(original_bytes).hexdigest(),
                                    "size_bytes": len(original_bytes)},
                       "sha256": hashlib.sha256(replacement_bytes).hexdigest(),
                       "size_bytes": len(replacement_bytes)}
        intent = bridge_operation_journal.build_intent_record(request, replacement)
        created = bridge_operation_journal.OperationIntentJournal(fixture["home"]).create(
            intent, authorize=lambda: None)
        self.assertEqual(created["status"], "CREATED", created)

        def content(path, raw):
            info = path.stat()
            signature = {"dev": info.st_dev, "ino": info.st_ino, "mode": info.st_mode,
                         "uid": info.st_uid, "gid": info.st_gid, "nlink": info.st_nlink,
                         "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                         "ctime_ns": info.st_ctime_ns, "flags": getattr(info, "st_flags", 0)}
            return {"identity": signature, "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw), "xattrs_digest": "0" * 64}

        info = self.ws.stat()
        parent = [info.st_dev, info.st_ino]
        common = {"schema_version": 1, "request": dataclasses.asdict(request),
                  "intent_fingerprint": intent["fingerprint"],
                  "created_at": "2026-10-11T12:00:00.000000Z"}
        dispatch = dict(common, phase="DISPATCH",
                        anchors={"root": parent, "parents": [parent], "parent": parent},
                        original=content(fixture["target"], original_bytes),
                        temp_name=".orch-patch-" + "a" * 32 + ".tmp")
        store = bridge_patch_execution.PatchExecutionStore(fixture["home"])
        with bridge_patch_execution.runtime_operation_lock(fixture["home"]):
            self.assertEqual(store.append(dispatch, authorize=lambda: None)["status"], "CREATED")
            if disposition is not None:
                publication = None
                target = dispatch["original"]
                basis = "RECONCILE_NO_PUBLICATION"
                if disposition == "APPLIED":
                    staged = self.ws / dispatch["temp_name"]
                    staged.write_bytes(replacement_bytes)
                    staged.chmod(stat.S_IMODE(fixture["target"].stat().st_mode))
                    publication = dict(common, phase="PUBLICATION",
                                       dispatch_sha256=bridge_patch_execution.execution_record_hash(dispatch),
                                       staged=content(staged, replacement_bytes))
                    self.assertEqual(store.append(publication, authorize=lambda: None)["status"], "CREATED")
                    os.replace(staged, fixture["target"])
                    target, basis = content(fixture["target"], replacement_bytes), "APPLY"
                result = dict(common, phase="RESULT",
                              dispatch_sha256=bridge_patch_execution.execution_record_hash(dispatch),
                              publication_sha256=(bridge_patch_execution.execution_record_hash(publication)
                                                  if publication else None),
                              disposition=disposition,
                              basis={"kind": basis, "target": target, "temp_absent": True})
                if fail_result_sync:
                    operation = (fixture["home"] / ".runtime" / "patch_execution"
                                 / hashlib.sha256(binding["run_id"].encode()).hexdigest()
                                 / hashlib.sha256(operation_id.encode()).hexdigest())
                    real_sync = os.fsync
                    def sync(fd):
                        info, directory = os.fstat(fd), operation.stat()
                        if (operation / "result.json").exists() and (
                                info.st_dev, info.st_ino) == (directory.st_dev, directory.st_ino):
                            raise OSError("retained RESULT directory acknowledgement failed")
                        return real_sync(fd)
                    with mock.patch.object(bridge_patch_execution.os, "fsync", side_effect=sync):
                        self.assertEqual(store.append(result, authorize=lambda: None)["status"], "UNKNOWN")
                    self.assertTrue(store.inspect(binding["run_id"], operation_id)["terminal"])
                else:
                    self.assertEqual(store.append(result, authorize=lambda: None)["status"], "CREATED")
        return store

    @staticmethod
    def _patch_transition_lock_probe(home):
        code = (
            "import sys; sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[1])) + "); "
            "from pathlib import Path; from orch.bridge_patch_execution import runtime_operation_lock; "
            "\ntry:\n with runtime_operation_lock(Path(sys.argv[1])): print('ACQUIRED')"
            "\nexcept ValueError as error: print(str(error))"
        )
        return subprocess.run([sys.executable, "-I", "-B", "-c", code, str(home)],
                              capture_output=True, text=True, timeout=10)

    def test_patch_transition_unresolved_dispatch_preserves_run_task_events_and_capability(self):
        for transition in ("submit", "quiesce", "abort"):
            with self.subTest(transition=transition):
                fixture = self._patch_transition_fixture(transition)
                store = self._patch_transition_records(fixture)
                before = self._patch_transition_state(fixture)
                with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                    self._patch_transition_invoke(fixture)
                self.assertEqual(self._patch_transition_state(fixture), before)
                self.assertFalse(store.inspect(fixture["claim"]["run_id"], "guard-operation")["terminal"])

    def test_patch_transition_unknown_inventory_preserves_run_and_capability(self):
        for transition in ("submit", "quiesce", "abort"):
            with self.subTest(transition=transition):
                fixture = self._patch_transition_fixture(transition)
                directory = fixture["home"] / ".runtime" / "patch_execution"
                directory.mkdir(mode=0o700)
                (directory / "unrecognized-evidence").write_bytes(b"partial")
                before = self._patch_transition_state(fixture)
                self.assertEqual(bridge_patch_execution.execution_inventory(fixture["home"])["status"], "UNKNOWN")
                with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                    self._patch_transition_invoke(fixture)
                self.assertEqual(self._patch_transition_state(fixture), before)

    def test_patch_transition_terminal_foreign_ledger_binding_is_blocked(self):
        changes = (({"project_id": "foreign-project"}, "guard-project"),
                   ({"writer_key": "foreign:key"}, "guard-project"),
                   ({"task_id": "FOREIGN-TASK"}, "guard-project"),
                   ({"attempt": 2}, "guard-project"),
                   ({"project_id": "guard-project"}, None),
                   ({"project_id": "guard-project"}, "LegacyProject"))
        for transition in ("submit", "quiesce", "abort"):
            for change, project_id in changes:
                with self.subTest(transition=transition, change=change, project_id=project_id):
                    fixture = self._patch_transition_fixture(transition, project_id=project_id)
                    self._patch_transition_records(fixture, disposition="NOT_APPLIED", binding_changes=change)
                    before = self._patch_transition_state(fixture)
                    with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                        self._patch_transition_invoke(fixture)
                    self.assertEqual(self._patch_transition_state(fixture), before)

    def test_patch_transition_terminal_chains_and_legacy_absence_pass(self):
        cases = (("APPLIED", "guard-project"), ("NOT_APPLIED", "guard-project"),
                 (None, None), (None, "LegacyProject"))
        expected = {"submit": "RESULT_SUBMITTED", "quiesce": "VERIFYING", "abort": "ABORTED"}
        for transition in ("submit", "quiesce", "abort"):
            for disposition, project_id in cases:
                with self.subTest(transition=transition, disposition=disposition, project_id=project_id):
                    fixture = self._patch_transition_fixture(transition, project_id=project_id)
                    if disposition:
                        self._patch_transition_records(fixture, disposition=disposition)
                    result = self._patch_transition_invoke(fixture)
                    self.assertEqual(result["status"], expected[transition])
                    self.assertEqual(fixture["capability"].exists(), transition == "submit")
                    if disposition is None:
                        self.assertFalse((fixture["home"] / ".runtime" / "patch_execution").exists())

    def test_patch_transition_retained_result_and_multioperation_sync_failure_refuse_atomically(self):
        for transition in ("submit", "quiesce", "abort"):
            for disposition in ("APPLIED", "NOT_APPLIED"):
                with self.subTest(transition=transition, disposition=disposition):
                    fixture = self._patch_transition_fixture(transition)
                    self._patch_transition_records(fixture, disposition="NOT_APPLIED",
                                                   operation_id="acknowledged-first")
                    self._patch_transition_records(fixture, disposition=disposition,
                                                   fail_result_sync=True)
                    census = bridge_patch_execution.execution_inventory(fixture["home"])
                    before = self._patch_transition_state(fixture)
                    failed_result = (fixture["home"] / ".runtime" / "patch_execution"
                                     / hashlib.sha256(fixture["claim"]["run_id"].encode()).hexdigest()
                                     / hashlib.sha256(b"guard-operation").hexdigest() / "result.json")
                    failures = []
                    real_sync = os.fsync
                    def sync(fd):
                        info, retained = os.fstat(fd), failed_result.stat()
                        if (info.st_dev, info.st_ino) == (retained.st_dev, retained.st_ino):
                            failures.append(fd)
                            raise OSError("persistent terminal record sync failure")
                        return real_sync(fd)
                    with mock.patch.object(bridge_patch_execution.os, "fsync", side_effect=sync):
                        with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                            self._patch_transition_invoke(fixture)
                    self.assertEqual(len(failures), 1)
                    self.assertEqual(self._patch_transition_state(fixture), before)
                    self.assertEqual(bridge_patch_execution.execution_inventory(fixture["home"]), census)
                    with fixture["orch"].connect() as conn:
                        locks = fixture["orch"]._active_writer_locks(conn)
                    self.assertIn(fixture["binding"]["writer_key"], locks)
                    self.assertEqual(self._patch_transition_invoke(fixture)["status"],
                                     {"submit": "RESULT_SUBMITTED", "quiesce": "VERIFYING",
                                      "abort": "ABORTED"}[transition])

    def test_patch_transition_renewal_rechecks_caller_authority_before_and_after(self):
        for transition in ("submit", "quiesce", "abort"):
            for boundary in ("before", "after"):
                with self.subTest(transition=transition, boundary=boundary):
                    fixture = self._patch_transition_fixture(transition)
                    self._patch_transition_records(fixture, disposition="NOT_APPLIED")
                    before = self._patch_transition_state(fixture)
                    original_guard = bridge_patch_execution.require_execution_resolved
                    synced = [];real_sync = os.fsync
                    def sync(fd):
                        real_sync(fd);synced.append(fd)
                    def guard(home, run_id, binding, *, authorize=None):
                        self.assertTrue(callable(authorize), "transition lacks live caller authority")
                        calls = []
                        def authority():
                            calls.append(None)
                            if boundary == "before" or synced:
                                raise ValueError("caller authority revoked")
                            return authorize()
                        return original_guard(home, run_id, binding, authorize=authority)
                    with mock.patch("orch.core.require_execution_resolved", side_effect=guard), \
                         mock.patch.object(bridge_patch_execution.os, "fsync", side_effect=sync):
                        with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                            self._patch_transition_invoke(fixture)
                    self.assertEqual(self._patch_transition_state(fixture), before)
                    self.assertEqual(bool(synced), boundary == "after")

    def test_patch_transition_authority_callback_reads_fresh_transaction_state(self):
        for transition in ("submit", "quiesce", "abort"):
            for drift in ("lease", "state", "binding"):
                with self.subTest(transition=transition, drift=drift):
                    fixture = self._patch_transition_fixture(transition)
                    self._patch_transition_records(fixture, disposition="NOT_APPLIED")
                    before = self._patch_transition_state(fixture)
                    real_connect = fixture["orch"].connect
                    connections = []
                    def connect():
                        connection = real_connect();connections.append(connection)
                        return connection
                    real_sync = os.fsync;injected = []
                    def sync(fd):
                        real_sync(fd)
                        if not injected:
                            if drift == "lease":
                                connections[-1].execute("UPDATE runs SET lease_token=? WHERE run_id=?",
                                                       ("changed during sync", fixture["claim"]["run_id"]))
                            elif drift == "state":
                                connections[-1].execute("UPDATE runs SET state='ABORTED' WHERE run_id=?",
                                                       (fixture["claim"]["run_id"],))
                            else:
                                connections[-1].execute("UPDATE tasks SET writer_key=? WHERE task_id=?",
                                                       ("foreign:writer", fixture["claim"]["task_id"]))
                            injected.append(True)
                    with mock.patch.object(fixture["orch"], "connect", side_effect=connect), \
                         mock.patch.object(bridge_patch_execution.os, "fsync", side_effect=sync):
                        with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                            self._patch_transition_invoke(fixture)
                    self.assertTrue(injected)
                    self.assertEqual(self._patch_transition_state(fixture), before)

    def test_patch_transition_external_flock_blocks_before_connect_or_receipt_read(self):
        code = "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDONLY); fcntl.flock(fd,fcntl.LOCK_EX); print('LOCKED',flush=True); sys.stdin.read(1)"
        for transition in ("submit", "quiesce", "abort"):
            with self.subTest(transition=transition):
                fixture = self._patch_transition_fixture(transition)
                before = self._patch_transition_state(fixture)
                process = subprocess.Popen([sys.executable, "-I", "-B", "-c", code,
                                            str(fixture["home"] / ".runtime")],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 10)[0],
                                    "external flock holder did not become ready")
                    self.assertEqual(process.stdout.readline().strip(), "LOCKED")
                    with mock.patch.object(fixture["orch"], "connect", side_effect=AssertionError("connect before lock")):
                        with mock.patch.object(fixture["orch"], "_load_worker_receipt", side_effect=AssertionError("receipt before lock")):
                            with self.assertRaisesRegex(ValueError, "patch_execution_busy"):
                                self._patch_transition_invoke(fixture)
                    self.assertEqual(self._patch_transition_state(fixture), before)
                finally:
                    process.communicate("x", timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(self._patch_transition_invoke(fixture)["status"],
                                 {"submit": "RESULT_SUBMITTED", "quiesce": "VERIFYING", "abort": "ABORTED"}[transition])

    def test_patch_transition_lock_covers_validation_census_commit_and_revocation(self):
        for transition in ("submit", "quiesce", "abort"):
            with self.subTest(transition=transition):
                fixture = self._patch_transition_fixture(transition)
                self._patch_transition_records(fixture, disposition="NOT_APPLIED")
                orch = fixture["orch"]
                observed = []

                def locked(stage):
                    observed.append(stage)
                    probe = self._patch_transition_lock_probe(fixture["home"])
                    self.assertEqual(probe.returncode, 0, probe.stderr)
                    self.assertEqual(probe.stdout.strip(), "patch_execution_busy", stage)

                connect, revoke, receipt_load = orch.connect, orch._revoke_capability, orch._load_worker_receipt
                real_sync = os.fsync

                class TracedConnection:
                    def __init__(self, connection):
                        self.connection = connection
                    def __enter__(self):
                        self.connection.__enter__()
                        return self
                    def __exit__(self, *args):
                        return self.connection.__exit__(*args)
                    def execute(self, sql, *args):
                        if sql.startswith("UPDATE ") or sql.startswith("INSERT "):
                            locked("mutation")
                        result = self.connection.execute(sql, *args)
                        if sql in ("BEGIN IMMEDIATE", "COMMIT"):
                            locked(sql)
                        return result

                def traced_connect():
                    locked("connect")
                    return TracedConnection(connect())

                def census(home, run_id, binding, *, authorize=None):
                    locked("census")
                    self.assertEqual(home, fixture["home"])
                    self.assertEqual(run_id, fixture["claim"]["run_id"])
                    self.assertEqual(binding, WorkerRunBinding(**fixture["binding"]))
                    def authority():
                        locked("authority")
                        return authorize()
                    return bridge_patch_execution.require_execution_resolved(
                        home, run_id, binding, authorize=authority)

                def traced_revoke(run_id):
                    locked("revoke")
                    return revoke(run_id)

                def traced_receipt(*args):
                    locked("receipt")
                    return receipt_load(*args)

                def traced_sync(fd):
                    if "sync" not in observed:
                        locked("sync")
                    return real_sync(fd)

                with mock.patch.object(orch, "connect", side_effect=traced_connect), \
                        mock.patch.object(orch, "_revoke_capability", side_effect=traced_revoke), \
                        mock.patch.object(orch, "_load_worker_receipt", side_effect=traced_receipt), \
                        mock.patch("orch.core.require_execution_resolved", side_effect=census), \
                        mock.patch.object(bridge_patch_execution.os, "fsync", side_effect=traced_sync):
                    self._patch_transition_invoke(fixture)
                self.assertLess(observed.index("census"), observed.index("COMMIT"))
                self.assertLess(observed.index("census"), observed.index("mutation"))
                self.assertLess(observed.index("sync"), observed.index("mutation"))
                self.assertGreater(observed.count("authority"), 1)
                self.assertEqual(observed[:3], (["receipt", "connect", "BEGIN IMMEDIATE"]
                                                if transition == "submit" else ["connect", "BEGIN IMMEDIATE", "census"]))
                if transition != "submit":
                    self.assertGreater(observed.index("revoke"), observed.index("COMMIT"))
                probe = self._patch_transition_lock_probe(fixture["home"])
                self.assertEqual(probe.stdout.strip(), "ACQUIRED", probe.stderr)

    def test_patch_transition_existing_validation_precedes_census(self):
        for transition in ("submit", "quiesce", "abort"):
            with self.subTest(transition=transition):
                fixture = self._patch_transition_fixture(transition)
                self._patch_transition_records(fixture)
                if transition == "abort":
                    fixture["orch"].checkpoint(fixture["claim"]["run_id"], fixture["lease"],
                                                "unknown external worker", "unknown")
                    error = "process_inactivity_confirmation_required"
                else:
                    fixture["lease"] = "incorrect-lease"
                    error = "invalid_lease"
                before = self._patch_transition_state(fixture)
                with mock.patch("orch.core.require_execution_resolved") as census:
                    with self.assertRaisesRegex(ValueError, error):
                        self._patch_transition_invoke(fixture)
                census.assert_not_called()
                self.assertEqual(self._patch_transition_state(fixture), before)

    def test_connect_retries_transient_protocol_without_rewriting_wal_mode(self):
        original_connect = sqlite3.connect

        class TracedConnection(sqlite3.Connection):
            statements = []
            injected = False

            def execute(self, sql, *args, **kwargs):
                self.statements.append(sql)
                if sql == "PRAGMA foreign_keys=ON" and not self.injected:
                    type(self).injected = True
                    raise sqlite3.OperationalError("locking protocol")
                return super().execute(sql, *args, **kwargs)

        def connect_with_trace(*args, **kwargs):
            return original_connect(*args, factory=TracedConnection, **kwargs)

        with mock.patch("orch.core.sqlite3.connect", side_effect=connect_with_trace) as opened:
            with mock.patch("orch.core.time.sleep") as sleep:
                conn = self.orch.connect()
                try:
                    self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                finally:
                    conn.close()
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once()
        self.assertTrue(TracedConnection.injected)
        self.assertNotIn("PRAGMA journal_mode=WAL", TracedConnection.statements)

    def test_connect_protocol_retry_exhaustion_and_unrelated_error(self):
        for message, expected_attempts in (("locking protocol", 5),
                                           ("database is locked", 1)):
            with self.subTest(message=message):
                with mock.patch("orch.core.sqlite3.connect",
                                side_effect=sqlite3.OperationalError(message)) as opened:
                    with mock.patch("orch.core.time.sleep") as sleep:
                        with self.assertRaisesRegex(sqlite3.OperationalError, message):
                            self.orch.connect()
                self.assertEqual(opened.call_count, expected_attempts)
                self.assertEqual(sleep.call_count, expected_attempts - 1)

    def test_begin_immediate_retries_protocol_before_any_mutation(self):
        conn = mock.Mock()
        conn.execute.side_effect = [sqlite3.OperationalError("locking protocol"), None]
        with mock.patch("orch.core.time.sleep") as sleep:
            _begin_immediate(conn)
        self.assertEqual(conn.execute.call_count, 2)
        conn.execute.assert_called_with("BEGIN IMMEDIATE")
        sleep.assert_called_once()

        for message, expected_attempts in (("locking protocol", 5),
                                           ("database is locked", 1)):
            conn = mock.Mock()
            conn.execute.side_effect = sqlite3.OperationalError(message)
            with mock.patch("orch.core.time.sleep"):
                with self.assertRaisesRegex(sqlite3.OperationalError, message):
                    _begin_immediate(conn)
            self.assertEqual(conn.execute.call_count, expected_attempts)

        self.load([self.task("CLAIM-PROTOCOL")])
        with mock.patch("orch.core._begin_immediate", wraps=_begin_immediate) as begin:
            claimed = self.orch.claim("claim-protocol")
        self.assertEqual(claimed["status"], "CLAIMED")
        begin.assert_called_once()

    def test_dependency_sequence_and_no_work(self):
        self.load([self.task('T1'),self.task('T2',deps=['T1'])])
        c1=self.orch.claim('w1'); self.assertEqual(c1['task_id'],'T1')
        self.write_result(c1,'T1'); self.orch.complete(c1['run_id'])
        c2=self.orch.claim('w2'); self.assertEqual(c2['task_id'],'T2')
        self.write_result(c2,'T2'); self.orch.complete(c2['run_id'])
        self.assertEqual(self.orch.claim('w3')['status'],'NO_WORK')

    def test_dependency_can_reference_task_from_earlier_plan(self):
        self.load([self.task("CROSS-1")], revision="cross-p1")
        path = self.root / "cross-p2.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "cross-p2",
            "tasks": [self.task("CROSS-2", deps=["CROSS-1"])],
        }) + "\n")
        loaded = self.orch.load_plan(path)
        self.assertEqual(loaded["queued_count"], 1)
        first = self.orch.claim("w1")
        self.assertEqual(first["task_id"], "CROSS-1")
        self.write_result(first, "CROSS-1")
        self.orch.complete(first["run_id"])
        second = self.orch.claim("w2")
        self.assertEqual(second["task_id"], "CROSS-2")

    def test_cross_plan_unknown_dependency_is_rejected_atomically(self):
        path = self.root / "missing-dep.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "missing-dep",
            "tasks": [self.task("CROSS-MISSING", deps=["NO-SUCH-TASK"])],
        }) + "\n")
        with self.assertRaisesRegex(ValueError, "invalid_dependency:NO-SUCH-TASK"):
            self.orch.load_plan(path)
        self.assertEqual(self.orch.status()["tasks"], [])

    def test_single_writer_busy(self):
        self.load([self.task('T1'),self.task('T2')])
        first=self.orch.claim('w1')
        second=self.orch.claim('w2')
        self.assertEqual(first['status'],'CLAIMED'); self.assertEqual(second['status'],'BUSY')
        self.assertEqual(second['active']['run_id'],first['run_id'])

    def test_checkpoint_persists_without_releasing_writer_or_capability(self):
        self.load([self.task("CHECKPOINT-1"), self.task("CHECKPOINT-2")],
                  revision="checkpoint-persist")
        first = self.orch.claim("worker-checkpoint")
        cap = Path(first["capability_file"])
        lease = self.orch.lease_from_capability(first["run_id"], cap)

        recorded = self.orch.checkpoint(
            first["run_id"], lease, "context threshold approaching", "unknown"
        )

        self.assertEqual(recorded["status"], "CHECKPOINTED")
        self.assertFalse(recorded["details"]["writer_reservation_released"])
        self.assertFalse(recorded["details"]["capability_revoked"])
        self.assertFalse(recorded["details"]["safe_to_resume_elsewhere"])
        self.assertTrue(cap.is_file())
        status = self.orch.status()
        run = next(item for item in status["runs"]
                   if item["run_id"] == first["run_id"])
        self.assertEqual(run["state"], "RUNNING")
        blocked = self.orch.claim("second-worker")
        self.assertEqual(blocked["status"], "BUSY")
        self.assertEqual(blocked["active"]["run_id"], first["run_id"])

        with self.orch.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM settings WHERE key=?",
                (f"run_checkpoint:{first['run_id']}",),
            ).fetchone()
        saved = json.loads(row["value_json"])
        self.assertEqual(saved["reason"], "context threshold approaching")
        self.assertEqual(saved["process_state"], "unknown")

    def test_checkpoint_updates_latest_record_and_rejects_unsafe_states(self):
        self.load([self.task("CHECKPOINT-LATEST")],
                  revision="checkpoint-latest")
        claim = self.orch.claim("worker-checkpoint")
        cap = Path(claim["capability_file"])
        lease = self.orch.lease_from_capability(claim["run_id"], cap)

        first = self.orch.checkpoint(
            claim["run_id"], lease, "first checkpoint", "unknown"
        )
        second = self.orch.checkpoint(
            claim["run_id"], lease, "worker still active", "active"
        )
        self.assertEqual(first["status"], "CHECKPOINTED")
        self.assertEqual(second["details"]["reason"], "worker still active")
        with self.orch.connect() as conn:
            rows = conn.execute(
                "SELECT value_json FROM settings WHERE key=?",
                (f"run_checkpoint:{claim['run_id']}",),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["value_json"])["process_state"], "active")

        with self.assertRaisesRegex(ValueError, "invalid_checkpoint_process_state"):
            self.orch.checkpoint(claim["run_id"], lease, "bad", "stopped")
        with self.assertRaisesRegex(ValueError, "checkpoint_reason_required"):
            self.orch.checkpoint(claim["run_id"], lease, "   ")

        receipt = Path(claim["receipt_file"])
        (self.ws / "CHECKPOINT-LATEST.json").write_text(
            "{}\n", encoding="utf-8"
        )
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"],
            "task_id": "CHECKPOINT-LATEST",
            "changed_paths": ["CHECKPOINT-LATEST.json"],
        }) + "\n", encoding="utf-8")
        self.orch.submit(claim["run_id"], lease, receipt)
        with self.assertRaisesRegex(ValueError, "run_not_running"):
            self.orch.checkpoint(claim["run_id"], lease, "too late")

    def test_verified_run_keeps_writer_lock_until_completion(self):
        self.load([self.task("VERIFY-1"), self.task("VERIFY-2")], revision="verified-lock")
        first = self.orch.claim("w1")
        verified = self.write_result(first, "VERIFY-1")
        self.assertEqual(verified["status"], "VERIFIED")
        blocked = self.orch.claim("w2")
        self.assertEqual(blocked["status"], "BUSY")
        self.assertEqual(blocked["active"]["run_id"], first["run_id"])
        self.assertEqual(blocked["active"]["state"], "VERIFIED")
        reconciled = self.orch.reconcile()
        self.assertEqual(reconciled["status"], "ATTENTION")
        self.assertEqual(reconciled["active_runs"], [])
        self.assertEqual(reconciled["writer_locks"][0]["run_id"], first["run_id"])
        self.assertEqual(self.orch.complete(first["run_id"])["status"], "COMPLETE")
        second = self.orch.claim("w2")
        self.assertEqual(second["status"], "CLAIMED")
        self.assertEqual(second["task_id"], "VERIFY-2")

    def _make_git_workspace(self, name="claim-base-repo"):
        repo = Path(self.tmp.name) / name
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)], check=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email",
             "fixture@example.invalid"], check=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name",
             "Claim Base Fixture"], check=True
        )
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "initial"], check=True
        )
        return repo

    def test_dynamic_git_task_binds_head_at_claim(self):
        repo = self._make_git_workspace("dynamic-claim")
        task = self.task("CLAIM-DYNAMIC")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "claim dynamic",
        }
        self.load([task], revision="claim-dynamic")
        expected = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "CLAIMED")
        self.assertEqual(claim["context"]["claim_git_head"], expected)
        with self.orch.connect() as conn:
            row = conn.execute(
                "SELECT claim_git_head FROM runs WHERE run_id=?",
                (claim["run_id"],),
            ).fetchone()
        self.assertEqual(row["claim_git_head"], expected)

    def _snapshot_repair_fixture(self, name="repair", *, max_attempts=2,
                                 protected=False, checks=False, publication_kind="git_local"):
        self.orch = Orchestrator(self.root / name)
        repo = self._make_git_workspace(name)
        sentinel = repo / "owner.txt"
        support = repo / "check.py"
        if protected or checks:
            sentinel.write_text("owner bytes\n", encoding="utf-8")
            support.write_text("raise SystemExit(0)\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "owner.txt", "check.py"],
                           check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "support"],
                           check=True)
        task = self.task(name, review=True, max_attempts=max_attempts,
                         protected={"owner.txt": hashlib.sha256(
                             sentinel.read_bytes()).hexdigest()} if protected else {},
                         checks=([{"id": "support", "argv": [sys.executable, "check.py"],
                                   "cwd": ".", "timeout_sec": 5}] if checks is True
                                 else checks if isinstance(checks, list) else []))
        task["workspace"] = str(repo)
        task["allowed_paths"] = [name + ".json", "extra.json"]
        if publication_kind != "none":
            task["publication"] = {"kind": publication_kind, "branch": "main",
                                   "commit_message": "repair fixture"}
        self.load([task], revision=name)
        first = self.orch.claim("first")
        source = repo / (name + ".json")
        source.write_bytes(b'{"retained":true}\n')
        receipt = Path(first["receipt_file"])
        receipt.write_text(json.dumps({"run_id": first["run_id"], "task_id": name,
                                       "changed_paths": [name + ".json"]}))
        lease = self.orch.lease_from_capability(first["run_id"],
                                               Path(first["capability_file"]))
        self.orch.submit(first["run_id"], lease, receipt)
        self.orch.quiesce(first["run_id"], lease)
        verified = self.orch.verify(first["run_id"])
        self.assertEqual(verified["status"], "REVIEWING")
        report = Path(prepare_review(self.orch, first["run_id"])["report"])
        report.write_text(json.dumps({"run_id": first["run_id"],
                                      "snapshot_id": verified["snapshot_id"],
                                      "verdict": "NEEDS_FIX", "findings": [{
                                          "severity": "important", "path": name + ".json",
                                          "evidence": "retained edit requires repair",
                                          "impact": "repair fixture"}], "uncertainty": []}))
        self.orch.import_review(first["run_id"], report)
        return first, verified, repo, source

    def test_snapshot_repair_safe_profile_captures_git_identity_without_publication(self):
        first, verified, repo, source = self._snapshot_repair_fixture(
            "repair-safe", publication_kind="none")
        original = source.read_bytes()
        expected_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True).stdout.strip()
        admitted = self._claim_snapshot_repair(first)
        self.assertEqual(admitted["status"], "CLAIMED", admitted)
        self.assertEqual(first["context"]["claim_git_head"], expected_head)
        self.assertEqual(admitted["context"]["claim_git_head"], expected_head)
        self.assertEqual(admitted["context"]["publication"], {"kind": "none"})
        self.assertEqual(admitted["context"]["previous_snapshot_id"], verified["snapshot_id"])
        self.assertEqual(admitted["attempt"], 2)
        self.assertNotEqual(admitted["run_id"], first["run_id"])
        self.assertEqual(source.read_bytes(), original)
        with self.orch.connect() as conn:
            prior = conn.execute("SELECT verify_status,review_status,state FROM runs WHERE run_id=?",
                                 (first["run_id"],)).fetchone()
            self.assertEqual(tuple(prior), ("PASS", "NEEDS_FIX", "NEEDS_FIX"))
            branches = [json.loads(row["payload_json"])["claim_branch"] for row in conn.execute(
                "SELECT payload_json FROM events WHERE kind='RUN_CLAIMED' ORDER BY seq")]
            publication = conn.execute("SELECT * FROM publications").fetchall()
        self.assertEqual(branches, ["main", "main"])
        self.assertEqual(publication, [])

    def test_safe_profile_non_git_claim_preserves_ordinary_completion(self):
        self.load([self.task("SAFE-PLAIN")], revision="safe-plain")
        first = self.orch.claim("ordinary-safe")
        self.assertEqual(first["status"], "CLAIMED")
        self.assertIsNone(first["context"]["claim_git_head"])
        with closing(self.orch.connect()) as conn:
            claimed_event = conn.execute("SELECT payload_json FROM events WHERE kind='RUN_CLAIMED'").fetchone()
        self.assertIsNone(json.loads(claimed_event["payload_json"])["claim_branch"])
        self.assertEqual(self.write_result(first, "SAFE-PLAIN")["status"], "VERIFIED")
        self.assertEqual(self.orch.complete(first["run_id"])["status"], "COMPLETE")

    def test_safe_profile_ordinary_git_claim_preserves_detached_and_unborn_compatibility(self):
        for case in ("detached", "unborn"):
            with self.subTest(case=case):
                name = "safe-ordinary-" + case
                self.orch = Orchestrator(self.root / name)
                if case == "detached":
                    repo = self._make_git_workspace(name)
                    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"],
                                   check=True)
                    expected_head = subprocess.run(
                        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                        capture_output=True, text=True).stdout.strip()
                else:
                    repo = Path(self.tmp.name) / name
                    repo.mkdir()
                    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
                    expected_head = None
                task = self.task(name)
                task["workspace"] = str(repo)
                self.load([task], revision=name)
                claimed = self.orch.claim("ordinary-safe")
                self.assertEqual(claimed["status"], "CLAIMED", claimed)
                self.assertEqual(claimed["context"]["claim_git_head"], expected_head)
                with closing(self.orch.connect()) as conn:
                    claimed_event = conn.execute("SELECT payload_json FROM events WHERE kind='RUN_CLAIMED'").fetchone()
                self.assertIsNone(json.loads(claimed_event["payload_json"])["claim_branch"])
                self.assertEqual(claimed["context"]["publication"], {"kind": "none"})

    def test_snapshot_repair_safe_profile_rejects_drift_and_missing_prior_identity(self):
        executable = self.root / "safe-repair-check"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        cases = (("head", "repair_snapshot_base_mismatch"),
                 ("branch", "repair_snapshot_branch_mismatch"),
                 ("authority", "check_authority_changed:support:executable_hash_changed"),
                 ("legacy-head", "repair_claim_git_identity_missing"),
                 ("legacy-branch", "repair_claim_git_identity_missing"))
        for (case, reason), first, verified, repo, source in self._snapshot_repair_cases(
                cases, "repair-safe-drift", publication_kind="none", checks=[{
                    "id": "support", "argv": [str(executable)], "cwd": ".", "timeout_sec": 5}]):
            with self.subTest(case=case):
                executable.write_bytes(b"#!/bin/sh\nexit 0\n")
                if case == "head":
                    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty",
                                    "-qm", "foreign"], check=True)
                elif case == "branch":
                    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "foreign"],
                                   check=True)
                elif case == "authority":
                    executable.write_bytes(b"#!/bin/sh\nexit 9\n")
                else:
                    with closing(self.orch.connect()) as conn:
                        if case == "legacy-head":
                            conn.execute("UPDATE runs SET claim_git_head=NULL WHERE run_id=?",
                                         (first["run_id"],))
                        else:
                            event = conn.execute(
                                "SELECT seq,payload_json FROM events WHERE run_id=? AND kind='RUN_CLAIMED'",
                                (first["run_id"],)).fetchone()
                            payload = json.loads(event["payload_json"])
                            payload["claim_branch"] = None
                            conn.execute("UPDATE events SET payload_json=? WHERE seq=?",
                                         (canonical_json(payload), event["seq"]))
                before = self._repair_admission_state(first)
                refused = self._claim_snapshot_repair(first)
                self.assertEqual(refused["status"], "BLOCKED", refused)
                self.assertEqual(refused["reason"], reason)
                self.assertEqual(self._repair_admission_state(first), before)

    def _snapshot_repair_cases(self, cases, name, *, fresh_cases=None, **kwargs):
        # Prepare via the real lifecycle, then restore only disposable test bytes.
        # Every case still runs current Git, snapshot and check-authority guards.
        first, verified, repo, source = self._snapshot_repair_fixture(name, **kwargs)
        prepared_orch = self.orch
        fixture_root = self.orch.root
        archive = Path(self.tmp.name) / (name + "-prepared")
        archive.mkdir()
        with closing(self.orch.connect()) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copytree(fixture_root, archive / "control", symlinks=True)
        shutil.copytree(repo, archive / "repo", symlinks=True)
        for index, case in enumerate(cases):
            if fresh_cases and case in fresh_cases:
                yield (case, *self._snapshot_repair_fixture(
                    name + "-" + case, **dict(kwargs, **fresh_cases[case])))
                continue
            if index:
                self.orch = prepared_orch
                shutil.rmtree(fixture_root)
                shutil.rmtree(repo)
                shutil.copytree(archive / "control", fixture_root, symlinks=True)
                shutil.copytree(archive / "repo", repo, symlinks=True)
            yield case, first, verified, repo, source

    def _readmission_fixture(self, name="readmission", *, project_sentinel_only=False,
                             source_mode=None):
        from orch.project import ProjectRegistry
        self.orch = Orchestrator(self.root / name)
        repo = self._make_git_workspace(name)
        for relative, data in {"check.py": b"raise SystemExit(0)\n",
                               "authority.dat": b"old authority\n",
                               "owner.txt": b"owner bytes\n"}.items():
            (repo / relative).write_bytes(data)
        def git(where, *args):
            return subprocess.run(["git", "-C", str(where), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git(repo, "add", "check.py", "authority.dat", "owner.txt")
        git(repo, "commit", "-qm", "readmission base")
        git(repo, "remote", "add", "origin", "https://example.invalid/readmission.git")
        base = git(repo, "rev-parse", "HEAD")
        registry = ProjectRegistry(self.orch.root)
        if project_sentinel_only:
            (repo / "owner.txt").write_bytes(b"registered owner sentinel\n")
        old_project = registry.add(repo, profile="standard", review_mode="required",
                                   allow_push=False)["project"]
        if project_sentinel_only:
            git(repo, "commit", "-qam", "preserve registered sentinel bytes")
            base = git(repo, "rev-parse", "HEAD")
        protected = {} if project_sentinel_only else {"owner.txt": hashlib.sha256(
            (repo / "owner.txt").read_bytes()).hexdigest()}
        old = self.task(name, review=True, protected=protected, checks=[{
                "id": "support", "argv": [sys.executable, "check.py"], "cwd": ".",
                "timeout_sec": 600, "authority_paths": ["authority.dat"]}])
        old.update(workspace=str(repo), project_id=old_project["project_id"],
                   writer_key=old_project["writer_key"],
                   allowed_paths=["candidate.json", "extra.json"] + (["owner.txt"] if project_sentinel_only else []),
                   review={"mode": "required", "reviewer": "codex", "placement": "pre_publish"},
                   publication={"kind": "git_local", "branch": "main", "expected_base": base,
                                "commit_message": "previous candidate"})
        self.load([old], revision=name + "-old")
        first = self.orch.claim("source-worker")
        source = repo / "candidate.json"
        source.write_bytes(b'{"retained":true}\n')
        if source_mode is not None:
            source.chmod(source_mode)
        def submit(claim, changed):
            receipt = Path(claim["receipt_file"])
            receipt.write_text(json.dumps({"run_id": claim["run_id"],
                                           "task_id": claim["task_id"], "changed_paths": changed}))
            lease = self.orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
            self.orch.submit(claim["run_id"], lease, receipt)
            self.orch.quiesce(claim["run_id"], lease)
            return self.orch.verify(claim["run_id"])
        def review(claim, verified, verdict):
            report = Path(prepare_review(self.orch, claim["run_id"])["report"])
            report.write_text(json.dumps({"run_id": claim["run_id"],
                                          "snapshot_id": verified["snapshot_id"], "verdict": verdict,
                                          "findings": [] if verdict == "PASS" else [{
                                              "severity": "important", "path": "candidate.json",
                                              "evidence": "retained candidate needs repair",
                                              "impact": "fixture repair"}], "uncertainty": []}))
            self.orch.import_review(claim["run_id"], report)
        verified = submit(first, [source.name])
        self.assertEqual(verified["status"], "REVIEWING")
        review(first, verified, "NEEDS_FIX")
        dep_repo = Path(self.tmp.name) / (name + "-dependency")
        git(repo, "worktree", "add", "-qb", name + "-dependency", str(dep_repo), base)
        dep_project = registry.add(dep_repo, profile="standard", review_mode="required",
                                   allow_push=False)["project"]
        dependency = self.task(name + "-dependency", review=True,
                               checks=[{"id": "dependency", "argv": [sys.executable, "-c", "pass"],
                                        "timeout_sec": 600}])
        dependency.update(workspace=str(dep_repo), project_id=dep_project["project_id"],
                          writer_key=dep_project["writer_key"], allowed_paths=["authority.dat"],
                          review=old["review"], publication={"kind": "git_local",
                              "branch": name + "-dependency", "expected_base": base,
                              "commit_message": "accepted dependency"})
        self.load([dependency], revision=name + "-dependency")
        dep = self.orch.claim("dependency-worker", project_id=dep_project["project_id"])
        (dep_repo / "authority.dat").write_bytes(b"accepted dependency authority\n")
        dep_verified = submit(dep, ["authority.dat"])
        self.assertEqual(dep_verified["status"], "REVIEWING")
        review(dep, dep_verified, "PASS")
        publication = self.orch.publish(dep["run_id"])
        self.assertEqual(publication["status"], "COMPLETE")
        new_repo = Path(self.tmp.name) / (name + "-new")
        git(repo, "worktree", "add", "-qb", name + "-new", str(new_repo), publication["commit"])
        new_project = registry.add(new_repo, profile="standard", review_mode="required",
                                   allow_push=False)["project"]
        new_task = json.loads(json.dumps(old))
        new_task.update(id=name + "-new", workspace=str(new_repo),
                        project_id=new_project["project_id"], writer_key=new_project["writer_key"],
                        dependencies=[dep["task_id"]], max_attempts=1)
        new_task["publication"].update(branch=name + "-new", expected_base=publication["commit"])
        plan = {"schema_version": 1, "plan_revision": name + "-new", "tasks": [new_task]}
        path = self.orch.root / "readmission.json"
        path.write_text(json.dumps(plan))
        return {"first": first, "verified": verified, "repo": repo, "source": source,
                "dep": dep, "dep_verified": dep_verified, "dep_repo": dep_repo,
                "commit": publication["commit"], "new_repo": new_repo, "task": new_task,
                "plan": plan, "path": path, "old_project": old_project,
                "new_project": new_project, "base": base}

    def _readmit_fixture(self, fixture, **kwargs):
        self.assertTrue(callable(getattr(self.orch, "readmit", None)),
                        "operator readmission is not implemented")
        return self.orch.readmit(fixture["first"]["run_id"], fixture["dep"]["run_id"],
                                fixture["path"], operator_authorization_confirmed=True,
                                process_inactivity_confirmed=True, **kwargs)

    def _readmission_state(self):
        with closing(self.orch.connect()) as conn:
            return {table: [dict(row) for row in conn.execute("SELECT * FROM " + table)]
                    for table in ("plans", "tasks", "runs", "snapshots", "events", "publications")}

    def _readmission_cases(self, cases, name):
        fixture = self._readmission_fixture(name)
        archive = Path(self.tmp.name) / (name + "-archive")
        archive.mkdir()
        paths = [self.orch.root, fixture["repo"], fixture["dep_repo"], fixture["new_repo"]]
        # Keep fixture WAL sidecars alive while copytree uses its enumerated paths.
        with closing(self.orch.connect()) as conn:
            self.assertEqual(tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()),
                             (0, 0, 0))
            for index, path in enumerate(paths):
                shutil.copytree(path, archive / str(index), symlinks=True)
        for index, case in enumerate(cases):
            if index:
                for offset, path in enumerate(paths):
                    shutil.rmtree(path)
                    shutil.copytree(archive / str(offset), path, symlinks=True)
            yield case, fixture

    def test_readmission_archive_survives_connection_collection(self):
        original_copytree = shutil.copytree
        observed = []

        def collect_after_enumeration(source, names):
            if Path(source) == self.orch.runtime:
                observed.append(set(names))
                gc.collect()
            return ()

        def copy_with_collection(*args, **kwargs):
            if len(args) > 3:
                args = (*args[:3], collect_after_enumeration, *args[4:])
            else:
                kwargs["ignore"] = collect_after_enumeration
            return original_copytree(*args, **kwargs)

        enabled = gc.isenabled()
        gc.disable()
        states = []
        try:
            with mock.patch.object(shutil, "copytree", side_effect=copy_with_collection):
                for _, fixture in self._readmission_cases(("first", "restored"),
                                                          "readmission-archive-gc"):
                    states.append(self._readmission_state())
                    self.assertEqual(self._readmit_fixture(fixture)["status"], "READMITTED")
            self.assertEqual(len(observed), 1)
            self.assertTrue({"orch.sqlite3-wal", "orch.sqlite3-shm"} <= observed[0])
            self.assertEqual(len(states), 2)
            self.assertEqual(states[0], states[1])
        finally:
            gc.collect()
            if enabled:
                gc.enable()

    def test_readmission_real_published_dependency_preserves_history_and_remaining_budget(self):
        f = self._readmission_fixture()
        before = self._readmission_state()
        original = f["source"].read_bytes()
        result = self._readmit_fixture(f)
        self.assertEqual(result["status"], "READMITTED")
        self.assertEqual(result["remaining_attempts"], 1)
        self.assertEqual(result["dependency_commit"], f["commit"])
        after = self._readmission_state()
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(after["snapshots"], before["snapshots"])
        old = next(t for t in after["tasks"] if t["task_id"] == f["first"]["task_id"])
        self.assertEqual(old["status"], "CANCELLED")
        self.assertEqual(old["payload_json"], before["tasks"][0]["payload_json"])
        new = next(t for t in after["tasks"] if t["task_id"] == f["task"]["id"])
        admitted = json.loads(new["payload_json"])
        self.assertEqual(admitted["max_attempts"], 1)
        self.assertEqual(admitted["checks"][0]["timeout_sec"], 600)
        authority = {b["path"]: b["sha256"] for b in admitted["checks"][0]["authority_files"]}
        self.assertEqual(authority["authority.dat"], hashlib.sha256(
            (f["new_repo"] / "authority.dat").read_bytes()).hexdigest())
        self.assertNotEqual(authority["authority.dat"], hashlib.sha256(
            (f["repo"] / "authority.dat").read_bytes()).hexdigest())
        claimed = self.orch.claim("fresh-readmitted-worker", project_id=f["task"]["project_id"])
        self.assertEqual(claimed["status"], "CLAIMED", claimed)
        self.assertEqual(claimed["attempt"], 1)
        self.assertEqual(claimed["context"]["previous_run_id"], f["first"]["run_id"])
        self.assertEqual(claimed["context"]["previous_snapshot_id"], f["verified"]["snapshot_id"])
        self.assertEqual(claimed["context"]["feedback"]["verdict"], "NEEDS_FIX")
        self.assertEqual(claimed["context"]["readmission"]["dependency_commit"], f["commit"])
        self.assertEqual(self.orch.context(claimed["run_id"]), claimed["context"])
        self.assertEqual(f["source"].read_bytes(), original)
        (f["new_repo"] / "candidate.json").write_bytes(b"new worker bytes\n")
        replay_before = self._readmission_state()
        self.assertEqual(self._readmit_fixture(f), result)
        self.assertEqual(self._readmission_state(), replay_before)

    def test_readmission_none_publication_claim_binds_accepted_dependency(self):
        f = self._readmission_fixture("readmission-none-accepted")
        f["task"]["publication"] = {"kind": "none"}
        f["path"].write_text(json.dumps(f["plan"]))
        admitted = self._readmit_fixture(f)
        self.assertEqual(admitted["status"], "READMITTED")
        claimed = self.orch.claim("readmitted-none-worker", project_id=f["task"]["project_id"])
        self.assertEqual(claimed["status"], "CLAIMED", claimed)
        self.assertEqual(claimed["attempt"], 1)
        self.assertEqual(claimed["context"]["publication"], {"kind": "none"})
        self.assertEqual(claimed["context"]["claim_git_head"], f["commit"])
        self.assertEqual(claimed["context"]["readmission"]["dependency_commit"], f["commit"])
        self.assertEqual(claimed["context"]["previous_run_id"], f["first"]["run_id"])
        self.assertEqual(claimed["context"]["previous_snapshot_id"], f["verified"]["snapshot_id"])
        self.assertEqual(claimed["context"]["feedback"]["verdict"], "NEEDS_FIX")
        with closing(self.orch.connect()) as conn:
            run = conn.execute("SELECT claim_git_head FROM runs WHERE run_id=?",
                               (claimed["run_id"],)).fetchone()
        self.assertEqual(run["claim_git_head"], f["commit"])
        (f["new_repo"] / "candidate.json").write_bytes(b"new worker bytes\n")
        for args in (("add", "--", "candidate.json"), ("commit", "-qm", "later worker fixture bytes")):
            subprocess.run(["git", "-C", str(f["new_repo"]), *args], check=True,
                           capture_output=True, text=True)
        later_head = subprocess.run(["git", "-C", str(f["new_repo"]), "rev-parse", "HEAD"],
                                    check=True, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(later_head, f["commit"])
        self.assertEqual(self.orch.context(claimed["run_id"]), claimed["context"])
        before_replay = self._readmission_state()
        self.assertEqual(self._readmit_fixture(f), admitted)
        self.assertEqual(self._readmission_state(), before_replay)

    def _assert_readmission_claim_unissued(self, fixture, before, capabilities):
        after = self._readmission_state()
        for table in ("runs", "snapshots", "publications"):
            self.assertEqual(after[table], before[table], table)
        self.assertFalse(any(run["task_id"] == fixture["task"]["id"] for run in after["runs"]))
        task = next(task for task in after["tasks"] if task["task_id"] == fixture["task"]["id"])
        self.assertEqual(json.loads(task["payload_json"])["max_attempts"], 1)
        self.assertEqual(sorted((self.orch.runtime / "claims").glob("*.json")), capabilities)
        with closing(self.orch.connect()) as conn:
            self.assertEqual(self.orch._active_writer_locks(conn), {})

    def test_readmission_none_publication_rejects_changed_clean_claim_head(self):
        f = self._readmission_fixture("readmission-none-foreign-head")
        f["task"]["publication"] = {"kind": "none"}
        f["path"].write_text(json.dumps(f["plan"]))
        self.assertEqual(self._readmit_fixture(f)["status"], "READMITTED")
        (f["new_repo"] / "candidate.json").write_bytes(b'{"unreviewed_base":true}\n')
        for args in (("add", "--", "candidate.json"),
                     ("commit", "-qm", "unreviewed clean destination base")):
            subprocess.run(["git", "-C", str(f["new_repo"]), *args], check=True,
                           capture_output=True, text=True)
        head = subprocess.run(["git", "-C", str(f["new_repo"]), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(head, f["commit"])
        clean = subprocess.run(["git", "-C", str(f["new_repo"]), "status", "--porcelain"],
                               check=True, capture_output=True, text=True).stdout
        self.assertEqual(clean, "")
        before = self._readmission_state()
        capabilities = sorted((self.orch.runtime / "claims").glob("*.json"))
        refused = self.orch.claim("foreign-base-worker", project_id=f["task"]["project_id"])
        self.assertEqual(refused["status"], "BLOCKED", refused.get("reason"))
        self.assertEqual(refused["reason"], "readmission_claim_base_mismatch")
        self._assert_readmission_claim_unissued(f, before, capabilities)

    def test_readmission_claim_rechecks_head_and_branch_after_context_preparation(self):
        for case, f in self._readmission_cases(("head", "branch"), "readmission-claim-race"):
            with self.subTest(case=case):
                f["task"]["publication"] = {"kind": "none"}
                f["path"].write_text(json.dumps(f["plan"]))
                self.assertEqual(self._readmit_fixture(f)["status"], "READMITTED")
                before = self._readmission_state()
                capabilities = sorted((self.orch.runtime / "claims").glob("*.json"))
                original = self.orch._build_context_pack
                def build_then_change(*args, **kwargs):
                    pack = original(*args, **kwargs)
                    if case == "head":
                        git_args = ("commit", "--allow-empty", "-qm", "foreign context-preparation head")
                    else:
                        git_args = ("checkout", "-qb", "foreign-context-preparation-branch")
                    subprocess.run(["git", "-C", str(f["new_repo"]), *git_args], check=True,
                                   capture_output=True, text=True)
                    return pack
                with mock.patch.object(self.orch, "_build_context_pack", side_effect=build_then_change):
                    refused = self.orch.claim("context-race-worker", project_id=f["task"]["project_id"])
                self.assertEqual(refused["status"], "BLOCKED", refused.get("reason"))
                self.assertEqual(refused["reason"], "claim_git_head_changed_during_preflight"
                                 if case == "head" else "claim_branch_changed_during_preflight")
                self._assert_readmission_claim_unissued(f, before, capabilities)

    def test_ordinary_claim_rechecks_git_identity_after_context_preparation(self):
        for kind in ("none", "git_local", "git"):
            for case in ("head", "branch"):
                with self.subTest(kind=kind, case=case):
                    name = "ordinary-claim-race-" + kind + "-" + case
                    self.orch = Orchestrator(self.root / name)
                    repo = self._make_git_workspace(name)
                    task = self.task(name)
                    task["workspace"] = str(repo)
                    task["publication"] = {"kind": kind}
                    if kind != "none":
                        task["publication"].update(branch="main", commit_message="dynamic claim")
                    self.load([task], revision=name)
                    original = self.orch._build_context_pack
                    def build_then_change(*args, **kwargs):
                        pack = original(*args, **kwargs)
                        git_args = (("commit", "--allow-empty", "-qm", "foreign context-preparation head")
                                    if case == "head" else ("checkout", "-qb", "foreign-context-preparation-branch"))
                        subprocess.run(["git", "-C", str(repo), *git_args], check=True,
                                       capture_output=True, text=True)
                        return pack
                    with mock.patch.object(self.orch, "_build_context_pack", side_effect=build_then_change):
                        refused = self.orch.claim("ordinary-context-race-worker")
                    self.assertEqual(refused["status"], "BLOCKED", refused.get("reason"))
                    self.assertEqual(refused["reason"], "claim_git_head_changed_during_preflight"
                                     if case == "head" else "claim_branch_changed_during_preflight")
                    with closing(self.orch.connect()) as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
                        self.assertEqual(self.orch._active_writer_locks(conn), {})
                    self.assertEqual(list((self.orch.runtime / "claims").glob("*.json")), [])

    def test_readmission_cli_requires_operator_assertion(self):
        from orch.cli import build_parser
        args = build_parser().parse_args(["readmit", "--previous-run", "PREVIOUS",
                                   "--dependency-run", "DEPENDENCY", "--plan", "plan.json",
                                   "--operator-authorization-confirmed"])
        self.assertTrue(args.operator_authorization_confirmed)
        f = self._readmission_fixture("readmission-operator")
        self.assertTrue(callable(getattr(self.orch, "readmit", None)))
        before = self._readmission_state()
        with self.assertRaisesRegex(ValueError, "readmission_operator_authorization_required"):
            self.orch.readmit(f["first"]["run_id"], f["dep"]["run_id"], f["path"])
        self.assertEqual(self._readmission_state(), before)

    def test_readmission_rejects_widened_contracts_without_any_ledger_mutation(self):
        cases = ("allowed", "directory-grant", "protected", "check-argv", "check-deadline",
                 "check-action", "check-output", "authority", "review", "owner",
                 "attempts", "dependency", "publication", "transport", "project-push",
                 "project-review", "project-owner", "multi-task", "existing-id", "existing-revision")
        for case, f in self._readmission_cases(cases, "readmission-contract"):
            with self.subTest(case=case):
                plan = json.loads(f["path"].read_text())
                task = plan["tasks"][0]
                if case == "allowed": task["allowed_paths"].append("foreign.txt")
                elif case == "directory-grant": task["allowed_paths"] = ["candidate.json/"]
                elif case == "protected": task["protected_paths"] = {}
                elif case == "check-argv": task["checks"][0]["argv"] = [sys.executable, "-c", "pass"]
                elif case == "check-deadline": task["checks"][0]["timeout_sec"] = 599
                elif case == "check-action": task["checks"][0]["timeout_action"] = "ignore"
                elif case == "check-output": task["checks"][0]["output_tail_chars"] = 9000
                elif case == "authority": task["checks"][0]["authority_paths"] = []
                elif case == "review": task["review"]["mode"] = "off"
                elif case == "owner":
                    with closing(self.orch.connect()) as conn:
                        row = conn.execute("SELECT payload_json FROM tasks WHERE task_id=?", (f["first"]["task_id"],)).fetchone()
                        old = json.loads(row["payload_json"]); old["owner_acceptance"] = True
                        conn.execute("UPDATE tasks SET payload_json=? WHERE task_id=?", (canonical_json(old), f["first"]["task_id"]))
                elif case == "attempts": task["max_attempts"] = 2
                elif case == "dependency": task["dependencies"] = []
                elif case == "publication": task["publication"]["kind"] = "git"
                elif case == "transport": task["publication"]["remote_url"] = "https://foreign.invalid/repo.git"
                elif case.startswith("project-"):
                    path = self.orch.root / "projects" / (task["project_id"] + ".json")
                    project = json.loads(path.read_text())
                    if case == "project-push": project["git"]["allow_push"] = True
                    elif case == "project-review": project["review"]["mode"] = "off"
                    else:
                        old_path = self.orch.root / "projects" / (f["old_project"]["project_id"] + ".json")
                        old_project = json.loads(old_path.read_text()); old_project["approval"]["default"] = "required"
                        old_path.write_text(json.dumps(old_project))
                        project["approval"]["default"] = "only_required_gates"
                    path.write_text(json.dumps(project))
                elif case == "multi-task": plan["tasks"].append(dict(task, id="EXTRA"))
                elif case == "existing-id": task["id"] = f["dep"]["task_id"]
                else: plan["plan_revision"] = "readmission-contract-old"
                f["path"].write_text(json.dumps(plan))
                before = self._readmission_state()
                with self.assertRaises(ValueError): self._readmit_fixture(f)
                self.assertEqual(self._readmission_state(), before)

    def test_readmission_rejects_destination_publication_branch_or_ref_atomically(self):
        for field, f in self._readmission_cases(("branch", "ref"), "readmission-publication-destination"):
            with self.subTest(field=field):
                plan = json.loads(f["path"].read_text())
                plan["tasks"][0]["publication"][field] = "foreign-destination"
                f["path"].write_text(json.dumps(plan))
                before = self._readmission_state()
                with self.assertRaisesRegex(ValueError, "readmission_publication_destination_mismatch"):
                    self._readmit_fixture(f)
                self.assertEqual(self._readmission_state(), before)

    def test_readmission_requires_separate_process_inactivity_assertion(self):
        from orch.cli import build_parser
        args = build_parser().parse_args([
            "readmit", "--previous-run", "PREVIOUS", "--dependency-run", "DEPENDENCY",
            "--plan", "plan.json", "--operator-authorization-confirmed",
        ])
        self.assertFalse(args.process_inactivity_confirmed)
        f = self._readmission_fixture("readmission-process-inactivity")
        before = self._readmission_state()
        with self.assertRaisesRegex(ValueError, "readmission_process_inactivity_confirmation_required"):
            self.orch.readmit(f["first"]["run_id"], f["dep"]["run_id"], f["path"],
                             operator_authorization_confirmed=True,
                             process_inactivity_confirmed=False)
        self.assertEqual(self._readmission_state(), before)

    def test_readmission_preserves_project_only_sentinel_in_new_task(self):
        f = self._readmission_fixture("readmission-project-sentinel", project_sentinel_only=True)
        digest = hashlib.sha256((f["repo"] / "owner.txt").read_bytes()).hexdigest()
        self.assertEqual(f["old_project"]["protected_paths"], {"owner.txt": digest})
        self.assertEqual(f["task"]["protected_paths"], {})
        before = self._readmission_state()
        with self.assertRaisesRegex(ValueError, "readmission_protected_paths_widened"):
            self._readmit_fixture(f)
        self.assertEqual(self._readmission_state(), before)
        plan = json.loads(f["path"].read_text())
        plan["tasks"][0]["protected_paths"]["owner.txt"] = digest
        f["path"].write_text(json.dumps(plan))
        self.assertEqual(self._readmit_fixture(f)["status"], "READMITTED")
        new = next(row for row in self._readmission_state()["tasks"]
                   if row["task_id"] == f["task"]["id"])
        self.assertEqual(json.loads(new["payload_json"])["protected_paths"], {"owner.txt": digest})

    def test_readmission_accepts_exact_verified_private_source_permissions(self):
        f = self._readmission_fixture("readmission-private-mode", source_mode=0o600)
        original = f["source"].read_bytes()
        before = self._readmission_state()
        manifest = next(json.loads(row["manifest_json"]) for row in before["snapshots"]
                        if row["run_id"] == f["first"]["run_id"])
        self.assertEqual(manifest["files"]["candidate.json"]["mode"], "0o600")
        self.assertEqual(self._readmit_fixture(f)["status"], "READMITTED")
        self.assertEqual(self._readmission_state()["snapshots"], before["snapshots"])
        self.assertEqual(f["source"].read_bytes(), original)
        self.assertEqual(f["source"].stat().st_mode & 0o777, 0o600)

    def test_readmission_rejects_changed_source_or_incomplete_dependency_evidence(self):
        cases = ("source-bytes", "source-mode", "source-extra", "source-authority", "source-protected",
                 "source-receipt", "source-review", "source-evidence", "source-manifest", "source-latest",
                 "source-blocked", "source-checkpoint", "source-capability", "source-running", "budget",
                 "dependency-state", "dependency-verify", "dependency-review", "dependency-report",
                 "dependency-evidence", "dependency-manifest", "dependency-publication", "dependency-commit",
                 "dependency-base", "dependency-capability", "pending-publication", "paused", "project-paused",
                 "new-dirty", "new-staged", "new-ita", "new-head", "new-branch", "new-origin", "source-origin")
        for case, f in self._readmission_cases(cases, "readmission-proof"):
            with self.subTest(case=case):
                run_id = f["dep"]["run_id"] if case.startswith("dependency-") else f["first"]["run_id"]
                if case == "source-bytes": f["source"].write_bytes(b"foreign bytes\n")
                elif case == "source-mode": f["source"].chmod(0o700)
                elif case == "source-extra": (f["repo"] / "extra.json").write_text("foreign\n")
                elif case == "source-authority": (f["repo"] / "authority.dat").write_text("foreign\n")
                elif case == "source-protected": (f["repo"] / "owner.txt").write_text("foreign\n")
                elif case == "source-receipt": Path(f["first"]["receipt_file"]).write_text("{}")
                elif case in {"source-review", "dependency-report"}: self.orch._review_report_path(run_id).write_text("{}")
                elif case in {"source-evidence", "dependency-evidence"}: (self.orch.logs / (run_id + "-scope.json")).write_text("{}")
                elif case in {"source-capability", "dependency-capability"}: self.orch._capability_path(run_id).write_bytes(b"private fixture")
                elif case in {"new-dirty", "new-staged", "new-ita"}:
                    (f["new_repo"] / "candidate.json").write_text("foreign\n")
                    if case != "new-dirty":
                        args = ["git", "-C", str(f["new_repo"]), "add"]
                        if case == "new-ita": args.append("-N")
                        subprocess.run(args + ["candidate.json"], check=True)
                elif case == "new-head": subprocess.run(["git", "-C", str(f["new_repo"]), "commit", "--allow-empty", "-qm", "foreign"], check=True)
                elif case == "new-branch": subprocess.run(["git", "-C", str(f["new_repo"]), "checkout", "-qb", "foreign"], check=True)
                elif case in {"new-origin", "source-origin"}: subprocess.run(["git", "-C", str(f["repo"]), "remote", "set-url", "origin", "https://foreign.invalid/repo.git"], check=True)
                else:
                    with closing(self.orch.connect()) as conn:
                        if case in {"source-manifest", "dependency-manifest"}:
                            sid = (f["dep_verified"] if case.startswith("dependency") else f["verified"])["snapshot_id"]
                            row = conn.execute("SELECT manifest_json FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
                            manifest = json.loads(row["manifest_json"]); manifest["created_at"] = "foreign"
                            conn.execute("UPDATE snapshots SET manifest_json=? WHERE snapshot_id=?", (canonical_json(manifest), sid))
                        elif case == "source-latest":
                            conn.execute("INSERT INTO runs(run_id,task_id,attempt,worker_id,state,lease_token,started_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?)", ("foreign-run", f["first"]["task_id"], 2, "fixture", "ABORTED", "private", "now", "now"))
                        elif case == "source-blocked":
                            conn.execute("UPDATE tasks SET status='BLOCKED' WHERE task_id=?", (f["first"]["task_id"],))
                            self.orch._event(conn, "TASK_BLOCKED_CONTEXT", task_id=f["first"]["task_id"])
                        elif case == "source-checkpoint": self.orch._event(conn, "WORKER_CHECKPOINT_RECORDED", task_id=f["first"]["task_id"], run_id=run_id, payload={"process_state": "unknown"})
                        elif case in {"source-running", "dependency-state"}: conn.execute("UPDATE runs SET state='RUNNING' WHERE run_id=?", (run_id,))
                        elif case == "dependency-verify": conn.execute("UPDATE runs SET verify_status='FAIL' WHERE run_id=?", (run_id,))
                        elif case == "dependency-review": conn.execute("UPDATE runs SET review_status='BLOCKED' WHERE run_id=?", (run_id,))
                        elif case == "dependency-publication": conn.execute("UPDATE publications SET status='COMMITTED' WHERE run_id=?", (run_id,))
                        elif case == "dependency-commit": conn.execute("UPDATE publications SET commit_id=? WHERE run_id=?", (f["base"], run_id))
                        elif case == "dependency-base": conn.execute("UPDATE publications SET expected_base=? WHERE run_id=?", ("0" * 40, run_id))
                        elif case == "pending-publication": conn.execute("UPDATE publications SET status='INTENT' WHERE run_id=?", (f["dep"]["run_id"],))
                        elif case in {"paused", "project-paused"}:
                            key = "paused" if case == "paused" else "project_paused:" + f["task"]["project_id"]
                            conn.execute("INSERT INTO settings VALUES(?,?,?)", (key, "{}", "now"))
                        elif case == "budget":
                            row = conn.execute("SELECT payload_json FROM tasks WHERE task_id=?", (f["first"]["task_id"],)).fetchone()
                            payload = json.loads(row["payload_json"]); payload["max_attempts"] = 1
                            conn.execute("UPDATE tasks SET payload_json=? WHERE task_id=?", (canonical_json(payload), f["first"]["task_id"]))
                before = self._readmission_state()
                with self.assertRaises(ValueError): self._readmit_fixture(f)
                self.assertEqual(self._readmission_state(), before)

    def test_readmission_dirty_claim_blocked_source_and_duplicate_conflicts(self):
        f = self._readmission_fixture("readmission-blocked")
        self.assertEqual(self.orch.claim("ordinary", project_id=f["old_project"]["project_id"])["status"], "BLOCKED")
        result = self._readmit_fixture(f)
        self.assertEqual(result["status"], "READMITTED")
        before = self._readmission_state()
        f["path"].write_text(f["path"].read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "readmission_replay_conflict"):
            self._readmit_fixture(f)
        self.assertEqual(self._readmission_state(), before)

    def test_readmission_rechecks_before_insertion_and_rolls_back_atomic_failures(self):
        for case, f in self._readmission_cases(("source", "new", "event"), "readmission-race"):
            with self.subTest(case=case):
                before = self._readmission_state()
                if case == "event":
                    original = self.orch._event
                    def fail_proof(conn, kind, **kwargs):
                        if kind == "TASK_READMITTED": raise RuntimeError("proof write failed")
                        return original(conn, kind, **kwargs)
                    with mock.patch.object(self.orch, "_event", side_effect=fail_proof):
                        with self.assertRaisesRegex(RuntimeError, "proof write failed"):
                            self._readmit_fixture(f)
                else:
                    original = self.orch._build_context_pack
                    def build_then_change(*args, **kwargs):
                        pack = original(*args, **kwargs)
                        target = f["source"] if case == "source" else f["new_repo"] / "candidate.json"
                        target.write_bytes(b"raced bytes\n")
                        return pack
                    with mock.patch.object(self.orch, "_build_context_pack", side_effect=build_then_change):
                        with self.assertRaises(ValueError): self._readmit_fixture(f)
                self.assertEqual(self._readmission_state(), before)

    def _claim_snapshot_repair(self, first, **kwargs):
        try:
            return self.orch.claim("repair-worker", repair_from_run_id=first["run_id"],
                                   **kwargs)
        except TypeError as exc:
            self.fail("Snapshot repair failed before producing an admission result: " + str(exc))

    def _repair_admission_state(self, first):
        with self.orch.connect() as conn:
            task = dict(conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                     (first["task_id"],)).fetchone())
            runs = [dict(row) for row in conn.execute(
                "SELECT * FROM runs WHERE task_id=? ORDER BY attempt",
                (first["task_id"],))]
            snapshots = [dict(row) for row in conn.execute(
                "SELECT * FROM snapshots WHERE run_id=?", (first["run_id"],))]
            events = [dict(row) for row in conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY seq",
                (first["task_id"],))]
        return task, runs, snapshots, events, sorted(
            str(path) for path in (self.orch.runtime / "claims").glob("*.json"))

    def test_snapshot_repair_claim_admits_verified_retained_bytes_in_fresh_run(self):
        first, verified, repo, source = self._snapshot_repair_fixture()
        before = self._repair_admission_state(first)
        original = source.read_bytes()
        admitted = self._claim_snapshot_repair(first)
        self.assertEqual(admitted["status"], "CLAIMED")
        self.assertEqual(admitted["task_id"], first["task_id"])
        self.assertEqual(admitted["attempt"], 2)
        self.assertNotEqual(admitted["run_id"], first["run_id"])
        self.assertEqual(admitted["context"]["previous_run_id"], first["run_id"])
        self.assertEqual(admitted["context"]["previous_snapshot_id"], verified["snapshot_id"])
        self.assertEqual(admitted["context"]["feedback"]["kind"], "review")
        after = self._repair_admission_state(first)
        self.assertEqual(after[1][0], before[1][0])
        self.assertEqual(after[2], before[2])
        self.assertEqual(after[0]["payload_json"], before[0]["payload_json"])
        claimed = json.loads(after[3][-1]["payload_json"])
        self.assertEqual(claimed["repair_from_run_id"], first["run_id"])
        self.assertEqual(claimed["repair_snapshot_id"], verified["snapshot_id"])
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(Path(admitted["capability_file"]).is_file())
        source.write_bytes(b'{"retained":true,"fixed":true}\n')
        receipt = Path(admitted["receipt_file"])
        receipt.write_text(json.dumps({"run_id": admitted["run_id"],
                                       "task_id": first["task_id"],
                                       "changed_paths": [source.name]}))
        lease = self.orch.lease_from_capability(admitted["run_id"],
                                               Path(admitted["capability_file"]))
        self.assertEqual(self.orch.submit(admitted["run_id"], lease, receipt)["status"],
                         "RESULT_SUBMITTED")

    def test_snapshot_repair_claim_recovers_only_dirty_claim_blocked_task(self):
        first, verified, repo, source = self._snapshot_repair_fixture("repair-blocked")
        refused = self.orch.claim("ordinary-second")
        self.assertEqual(refused["status"], "BLOCKED")
        self.assertEqual(refused["reason"], "claim_workspace_dirty:repair-blocked.json")
        admitted = self._claim_snapshot_repair(first)
        self.assertEqual(admitted["status"], "CLAIMED")
        self.assertEqual(admitted["attempt"], 2)
        self.assertEqual(source.read_bytes(), b'{"retained":true}\n')

    def test_snapshot_repair_rejects_source_and_git_drift_without_consuming_attempt(self):
        cases = ("bytes", "size", "mode", "missing", "extra", "staged", "intent-to-add", "head",
                 "branch", "symlink", "broken-symlink", "hardlink", "directory",
                 "sentinel", "authority", "evidence")
        for case, first, verified, repo, source in self._snapshot_repair_cases(
                cases, "repair-drift", protected=True, checks=True):
            with self.subTest(case=case):
                if case == "bytes":
                    source.write_bytes(b'{"retained":null}\n')
                elif case == "size":
                    source.write_bytes(source.read_bytes() + b" ")
                elif case == "mode":
                    source.chmod(0o700)
                elif case == "missing":
                    source.unlink()
                elif case == "extra":
                    (repo / "extra.json").write_bytes(b"{}\n")
                elif case == "staged":
                    subprocess.run(["git", "-C", str(repo), "add", source.name], check=True)
                elif case == "intent-to-add":
                    subprocess.run(["git", "-C", str(repo), "add", "-N", source.name], check=True)
                elif case == "head":
                    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty",
                                    "-qm", "foreign"], check=True)
                elif case == "branch":
                    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "foreign"],
                                   check=True)
                elif case in {"symlink", "broken-symlink", "hardlink", "directory"}:
                    original = source.read_bytes()
                    source.unlink()
                    target = self.root / (case + "-target")
                    if case != "broken-symlink":
                        target.write_bytes(original)
                    if case in {"symlink", "broken-symlink"}:
                        source.symlink_to(target)
                    elif case == "hardlink":
                        os.link(target, source)
                    else:
                        source.mkdir()
                elif case == "sentinel":
                    (repo / "owner.txt").write_text("foreign\n")
                elif case == "authority":
                    (repo / "check.py").write_text("raise SystemExit(9)\n")
                elif case == "evidence":
                    (self.orch.logs / (first["run_id"] + "-scope.json")).write_text("{}\n")
                before = self._repair_admission_state(first)
                result = self._claim_snapshot_repair(first)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(self._repair_admission_state(first), before)

    def test_snapshot_repair_rejects_ledger_binding_and_authority_conflicts(self):
        cases = ("snapshot-hash", "snapshot-workspace", "snapshot-size", "receipt-invalid",
                 "not-verified", "not-terminal", "snapshot-missing", "live-capability",
                 "pending-publication", "arbitrary-block", "divergent-event", "max-attempts")
        for case, first, verified, repo, source in self._snapshot_repair_cases(
                cases, "repair-ledger", fresh_cases={"max-attempts": {"max_attempts": 1}}):
            with self.subTest(case=case):
                with closing(self.orch.connect()) as conn:
                    if case.startswith("snapshot-") and case != "snapshot-missing":
                        snap = conn.execute("SELECT manifest_json FROM snapshots WHERE snapshot_id=?",
                                            (verified["snapshot_id"],)).fetchone()
                        manifest = json.loads(snap["manifest_json"])
                        if case == "snapshot-hash":
                            manifest["created_at"] = "changed"
                        elif case == "snapshot-workspace":
                            manifest["workspace"] = str(self.ws)
                        else:
                            manifest["files"][source.name]["bytes"] += 1
                        if case == "snapshot-hash":
                            conn.execute("UPDATE snapshots SET manifest_json=? WHERE snapshot_id=?",
                                         (canonical_json(manifest), verified["snapshot_id"]))
                        else:
                            new_id = "sha256:" + hashlib.sha256(
                                canonical_json(manifest).encode()).hexdigest()
                            conn.execute("UPDATE snapshots SET snapshot_id=?,manifest_json=? WHERE snapshot_id=?",
                                         (new_id, canonical_json(manifest), verified["snapshot_id"]))
                            conn.execute("UPDATE runs SET snapshot_id=? WHERE run_id=?",
                                         (new_id, first["run_id"]))
                    elif case == "not-verified":
                        conn.execute("UPDATE runs SET verify_status='FAIL' WHERE run_id=?",
                                     (first["run_id"],))
                    elif case == "receipt-invalid":
                        conn.execute("UPDATE runs SET receipt_json=? WHERE run_id=?",
                                     (json.dumps({"run_id": first["run_id"], "task_id": first["task_id"],
                                                  "changed_paths": 42}), first["run_id"]))
                    elif case == "not-terminal":
                        conn.execute("UPDATE runs SET state='REVIEWING' WHERE run_id=?",
                                     (first["run_id"],))
                    elif case == "snapshot-missing":
                        conn.execute("DELETE FROM snapshots WHERE snapshot_id=?",
                                     (verified["snapshot_id"],))
                    elif case == "arbitrary-block":
                        conn.execute("UPDATE tasks SET status='BLOCKED' WHERE task_id=?",
                                     (first["task_id"],))
                        self.orch._event(conn, "TASK_BLOCKED_CONTEXT", task_id=first["task_id"],
                                         payload={"reason": "foreign"})
                if case == "live-capability":
                    Path(first["capability_file"]).write_bytes(b"private stale authority")
                elif case == "pending-publication":
                    self.orch._publication_update(first["run_id"], status="INTENT",
                                                  operation_id="fixture", kind="git_local",
                                                  expected_base=first["context"]["claim_git_head"])
                elif case == "divergent-event":
                    self.assertEqual(self.orch.claim("ordinary")["status"], "BLOCKED")
                    with closing(self.orch.connect()) as conn:
                        self.orch._event(conn, "TASK_BLOCKED_CONTEXT", task_id=first["task_id"],
                                         payload={"reason": "divergent"})
                before = self._repair_admission_state(first)
                result = self._claim_snapshot_repair(first)
                self.assertIn(result["status"], {"BLOCKED", "BUSY"})
                self.assertEqual(self._repair_admission_state(first), before)

    def test_snapshot_repair_rereads_workspace_before_run_insert(self):
        executable = self.root / "repair-check"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        first, verified, repo, source = self._snapshot_repair_fixture("repair-race", checks=[{
            "id": "support", "argv": [str(executable)], "cwd": ".", "timeout_sec": 5}])
        original = self.orch._build_context_pack
        for case in ("bytes", "branch", "staged", "executable"):
            with self.subTest(case=case):
                before = self._repair_admission_state(first)
                def build_then_change(*args, **kwargs):
                    pack = original(*args, **kwargs)
                    if case == "bytes":
                        source.write_bytes(b'{"foreign":true}\n')
                    elif case == "branch":
                        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "foreign"],
                                       check=True)
                    elif case == "staged":
                        subprocess.run(["git", "-C", str(repo), "add", source.name], check=True)
                    else:
                        executable.write_bytes(b"#!/bin/sh\nexit 9\n")
                    return pack
                try:
                    with mock.patch.object(self.orch, "_build_context_pack", side_effect=build_then_change):
                        result = self._claim_snapshot_repair(first)
                    self.assertEqual(result["status"], "BLOCKED")
                    self.assertEqual(self._repair_admission_state(first), before)
                finally:
                    source.write_bytes(b'{"retained":true}\n')
                    (repo / "check.py").write_text("raise SystemExit(0)\n")
                    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
                    if case == "branch":
                        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
                    if case == "staged":
                        subprocess.run(["git", "-C", str(repo), "restore", "--staged", "--", source.name],
                                       check=True)

    def test_snapshot_repair_preserves_pause_dependency_and_project_constraints(self):
        first, verified, repo, source = self._snapshot_repair_fixture("repair-gates")
        self.orch.pause("fixture pause")
        before = self._repair_admission_state(first)
        self.assertEqual(self._claim_snapshot_repair(first)["status"], "PAUSED")
        self.assertEqual(self._repair_admission_state(first), before)
        self.orch.resume()
        before = self._repair_admission_state(first)
        self.assertEqual(self._claim_snapshot_repair(first, project_id="other")["status"], "BLOCKED")
        self.assertEqual(self._repair_admission_state(first), before)
        with self.orch.connect() as conn:
            original = conn.execute("SELECT payload_json FROM tasks WHERE task_id=?",
                                    (first["task_id"],)).fetchone()[0]
            payload = json.loads(original)
            payload["dependencies"] = ["uncompleted-dependency"]
            conn.execute("UPDATE tasks SET payload_json=? WHERE task_id=?",
                         (canonical_json(payload), first["task_id"]))
        before = self._repair_admission_state(first)
        self.assertEqual(self._claim_snapshot_repair(first)["reason"], "repair_dependencies_not_done")
        self.assertEqual(self._repair_admission_state(first), before)
        with self.orch.connect() as conn:
            conn.execute("UPDATE tasks SET payload_json=?,project_id='repair-project' WHERE task_id=?",
                         (original, first["task_id"]))
            conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                         ("project_paused:repair-project", '{"reason":"fixture pause"}', "fixture"))
        before = self._repair_admission_state(first)
        self.assertEqual(self._claim_snapshot_repair(first)["status"], "PROJECT_PAUSED")
        self.assertEqual(self._repair_admission_state(first), before)

    def test_snapshot_repair_rejects_nonlatest_and_cross_run_snapshot(self):
        first, verified, repo, source = self._snapshot_repair_fixture("repair-latest", max_attempts=3)
        admitted = self._claim_snapshot_repair(first)
        self.assertEqual(admitted["status"], "CLAIMED")
        before = self._repair_admission_state(first)
        self.assertEqual(self._claim_snapshot_repair(first)["status"], "BUSY")
        self.assertEqual(self._repair_admission_state(first), before)
        self.orch.abort(admitted["run_id"], "fixture completed inactivity", retry=True)
        before = self._repair_admission_state(first)
        self.assertEqual(self._claim_snapshot_repair(first)["reason"], "repair_run_not_latest")
        self.assertEqual(self._repair_admission_state(first), before)
        with self.orch.connect() as conn:
            conn.execute("UPDATE runs SET state='NEEDS_FIX',verify_status='PASS',snapshot_id=? WHERE run_id=?",
                         (verified["snapshot_id"], admitted["run_id"]))
        before = self._repair_admission_state(first)
        self.assertEqual(self._claim_snapshot_repair(admitted)["reason"], "repair_snapshot_run_mismatch")
        self.assertEqual(self._repair_admission_state(first), before)

    def test_snapshot_repair_rejects_same_inode_mutation_after_source_read(self):
        source = self.ws / "read-race.json"
        source.write_bytes(b'{"retained":true}\n')
        original_bytes = source.read_bytes()
        recorded = {"deleted": False, "sha256": hashlib.sha256(original_bytes).hexdigest(),
                    "bytes": len(original_bytes), "mode": oct(source.stat().st_mode & 0o777)}
        original = os.fdopen
        def close_then_mutate(fd, *args, **kwargs):
            handle = original(fd, *args, **kwargs)
            class MutatingClose:
                def __enter__(self):
                    return handle.__enter__()
                def __exit__(self, *exc):
                    result = handle.__exit__(*exc)
                    source.write_bytes(original_bytes.replace(b"true", b"null"))
                    return result
            return MutatingClose()
        with mock.patch("orch.core.os.fdopen", side_effect=close_then_mutate):
            with self.assertRaisesRegex(ValueError, "repair_snapshot_stale:read-race.json"):
                self.orch._repair_source_current(self.ws, source.name,
                                                recorded["sha256"], recorded=recorded)

    def test_foreign_commit_after_dynamic_claim_blocks_verification(self):
        repo = self._make_git_workspace("dynamic-race")
        task = self.task("CLAIM-RACE")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "claim race",
        }
        self.load([task], revision="claim-race")
        claim = self.orch.claim("worker")
        original = claim["context"]["claim_git_head"]
        (repo / "CLAIM-RACE.json").write_text('{"worker":true}\n')
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"],
            "task_id": "CLAIM-RACE",
            "changed_paths": ["CLAIM-RACE.json"],
        }))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        self.orch.submit(claim["run_id"], lease, receipt)
        self.orch.quiesce(claim["run_id"], lease)

        foreign = repo / "foreign.txt"
        foreign.write_text("foreign\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "foreign.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "foreign",
             "--", "foreign.txt"],
            check=True,
        )
        result = self.orch.verify(claim["run_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "workspace_base_changed")
        evidence = result["feedback"]["evidence"]
        self.assertEqual(evidence["claim_git_head"], original)
        self.assertNotEqual(evidence["git_head"], original)

    def test_stale_explicit_base_blocks_before_capability_is_created(self):
        repo = self._make_git_workspace("stale-claim")
        base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        task = self.task("CLAIM-STALE")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "expected_base": base,
            "commit_message": "claim stale",
        }
        self.load([task], revision="claim-stale")
        (repo / "advance.txt").write_text("advance\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "advance.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "advance"], check=True
        )
        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "BLOCKED")
        self.assertEqual(claim["reason"], "claim_expected_base_changed")
        self.assertEqual(claim["task_id"], "CLAIM-STALE")
        claims = self.orch.runtime / "claims"
        self.assertEqual(list(claims.glob("*.json")), [])
        status = self.orch.status()
        task_row = next(
            item for item in status["tasks"] if item["task_id"] == "CLAIM-STALE"
        )
        self.assertEqual(task_row["status"], "BLOCKED")

    def test_foreign_workspace_bytes_block_before_capability(self):
        repo = self._make_git_workspace("preclaim-dirty")
        task = self.task("PRECLAIM-DIRTY")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "preclaim dirty",
        }
        self.load([task], revision="preclaim-dirty")
        (repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")

        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "BLOCKED")
        self.assertEqual(
            claim["reason"], "claim_workspace_dirty:foreign.txt"
        )
        self.assertEqual(
            claim["details"]["scope"]["observed_paths"], ["foreign.txt"]
        )
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )

    def test_unchanged_protected_preexisting_dirty_bytes_allow_claim(self):
        repo = self._make_git_workspace("preclaim-protected")
        owner = repo / "owner-note.txt"
        owner.write_text("owner dirty\n", encoding="utf-8")
        import hashlib
        digest = hashlib.sha256(owner.read_bytes()).hexdigest()
        task = self.task("PRECLAIM-PROTECTED", protected={"owner-note.txt": digest})
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "preclaim protected",
        }
        self.load([task], revision="preclaim-protected")

        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "CLAIMED")
        self.assertTrue(Path(claim["capability_file"]).is_file())

    def test_check_authority_drift_blocks_before_capability(self):
        support = self.ws / "preclaim_check.py"
        support.write_text("raise SystemExit(0)\n", encoding="utf-8")
        check = {
            "id": "preclaim-support",
            "argv": [sys.executable, "preclaim_check.py"],
            "cwd": ".",
            "timeout_sec": 5,
        }
        self.load(
            [self.task("PRECLAIM-AUTH", checks=[check])],
            revision="preclaim-auth",
        )
        support.write_text("raise SystemExit(9)\n", encoding="utf-8")

        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "BLOCKED")
        self.assertIn(
            "check_authority_changed:preclaim-support:"
            "authority_file:preclaim_check.py:HASH_CHANGED",
            claim["reason"],
        )
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )

    def test_fifo_order_is_preserved_across_separately_loaded_plans(self):
        self.load([self.task("Z-FIRST")], revision="fifo-p1")
        path = self.root / "fifo-p2.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "fifo-p2",
            "tasks": [self.task("A-SECOND")],
        }) + "\n")
        self.orch.load_plan(path)
        first = self.orch.claim("w1")
        self.assertEqual(first["task_id"], "Z-FIRST")
        status = self.orch.status()
        queued = {item["task_id"]: item["queue_seq"] for item in status["tasks"]}
        self.assertLess(queued["Z-FIRST"], queued["A-SECOND"])

    def test_independent_workspaces_can_be_claimed_concurrently(self):
        ws2 = Path(self.tmp.name) / "ws2"
        ws2.mkdir()
        first = self.task("P1-T1")
        first["project_id"] = "project-one"
        second = self.task("P2-T1")
        second["project_id"] = "project-two"
        second["workspace"] = str(ws2)
        self.load([first, second], revision="multi-project")
        c1 = self.orch.claim("w1")
        c2 = self.orch.claim("w2")
        self.assertEqual(c1["task_id"], "P1-T1")
        self.assertEqual(c2["task_id"], "P2-T1")
        self.assertNotEqual(c1["context"]["writer_key"], c2["context"]["writer_key"])
        self.assertEqual(len(self.orch.reconcile()["active_runs"]), 2)

    def test_linked_git_worktrees_share_derived_writer_lock(self):
        repo = Path(self.tmp.name) / "shared-repo"
        linked = Path(self.tmp.name) / "shared-worktree"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "linked", str(linked)],
            check=True,
        )
        first = self.task("SHARED-1")
        first["workspace"] = str(repo)
        second = self.task("SHARED-2")
        second["workspace"] = str(linked)
        self.load([first, second], revision="shared-worktree")
        status = self.orch.status()
        keys = {item["task_id"]: item["writer_key"] for item in status["tasks"]}
        self.assertEqual(keys["SHARED-1"], keys["SHARED-2"])
        self.assertTrue(keys["SHARED-1"].startswith("git:"))
        c1 = self.orch.claim("w1")
        c2 = self.orch.claim("w2")
        self.assertEqual(c1["status"], "CLAIMED")
        self.assertEqual(c2["status"], "BUSY")
        self.assertEqual(c2["active"]["run_id"], c1["run_id"])

    def test_plan_cannot_spoof_writer_key(self):
        task = self.task("SPOOF-1")
        task["writer_key"] = "workspace:" + ("0" * 32)
        with self.assertRaisesRegex(ValueError, "writer_key_mismatch"):
            self.load([task], revision="spoof-writer")
        self.assertEqual(self.orch.status()["tasks"], [])

    def test_project_scoped_claim_can_skip_earlier_other_project(self):
        ws2 = Path(self.tmp.name) / "ws2"
        ws2.mkdir()
        first = self.task("P1-FIRST")
        first["project_id"] = "project-one"
        second = self.task("P2-ONLY")
        second["project_id"] = "project-two"
        second["workspace"] = str(ws2)
        self.load([first, second], revision="scoped-claim")
        selected = self.orch.claim("w2", project_id="project-two")
        self.assertEqual(selected["task_id"], "P2-ONLY")
        self.assertEqual(selected["project_id"], "project-two")
        next_one = self.orch.next_work(project_id="project-one")
        self.assertEqual(next_one["status"], "READY")
        self.assertEqual(next_one["task_id"], "P1-FIRST")

    def test_project_pause_skips_only_that_project_and_persists(self):
        ws2 = Path(self.tmp.name) / "pause-ws2"
        ws2.mkdir()
        one = self.task("PAUSE-P1")
        one["project_id"] = "project-one"
        two = self.task("PAUSE-P2")
        two["project_id"] = "project-two"
        two["workspace"] = str(ws2)
        self.load([one, two], revision="project-pause")

        paused = self.orch.pause_project("project-one", "maintenance")
        self.assertEqual(paused["status"], "PROJECT_PAUSED")
        scoped = self.orch.claim("w1", project_id="project-one")
        self.assertEqual(scoped["status"], "PROJECT_PAUSED")

        other = self.orch.claim("w2")
        self.assertEqual(other["task_id"], "PAUSE-P2")
        restarted = Orchestrator(self.root)
        view = restarted.queue_view(project_id="project-one")
        self.assertEqual(view["status"], "PROJECT_PAUSED")
        self.assertEqual(view["tasks"][0]["queue_state"], "PAUSED_PROJECT")
        self.assertEqual(view["summary"]["paused_project_count"], 1)
        self.assertEqual(
            restarted.next_work()["status"], "PROJECTS_PAUSED"
        )

        resumed = restarted.resume_project("project-one")
        self.assertEqual(resumed["status"], "PROJECT_RESUMED")
        first = restarted.claim("w3")
        self.assertEqual(first["status"], "CLAIMED")
        self.assertEqual(first["task_id"], "PAUSE-P1")

    def test_project_pause_does_not_interrupt_existing_writer(self):
        first = self.task("ACTIVE-P1")
        first["project_id"] = "project-one"
        second = self.task("AFTER-P1")
        second["project_id"] = "project-one"
        self.load([first, second], revision="project-pause-active")
        claim = self.orch.claim("worker")
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        self.orch.pause_project("project-one", "hold new work")
        self.assertEqual(
            self.orch.heartbeat(claim["run_id"], lease)["status"], "OK"
        )
        self.assertEqual(
            self.orch.next_work(project_id="project-one")["status"],
            "PROJECT_PAUSED",
        )
        self.orch.resume_project("project-one")
        busy = self.orch.next_work(project_id="project-one")
        self.assertEqual(busy["status"], "BUSY")
        self.assertEqual(busy["active"]["run_id"], claim["run_id"])

    def test_project_pause_requires_existing_project_queue(self):
        with self.assertRaisesRegex(ValueError, "unknown_project_queue"):
            self.orch.pause_project("missing-project", "nothing loaded")
        resumed = self.orch.resume_project("missing-project")
        self.assertTrue(resumed["already_resumed"])

    def test_queue_view_explains_writer_and_dependency_waits(self):
        first = self.task("QUEUE-1")
        second = self.task("QUEUE-2")
        third = self.task("QUEUE-3", deps=["QUEUE-2"])
        self.load([first, second, third], revision="queue-view")
        claim = self.orch.claim("worker")
        self.assertEqual(claim["task_id"], "QUEUE-1")
        view = self.orch.queue_view()
        by_id = {item["task_id"]: item for item in view["tasks"]}
        self.assertEqual(by_id["QUEUE-1"]["queue_state"], "ACTIVE")
        self.assertEqual(by_id["QUEUE-2"]["queue_state"], "WAITING_WRITER")
        self.assertEqual(by_id["QUEUE-2"]["writer_lock"]["run_id"], claim["run_id"])
        self.assertEqual(by_id["QUEUE-3"]["queue_state"], "WAITING_DEPENDENCY")
        self.assertEqual(by_id["QUEUE-3"]["waiting_dependencies"], ["QUEUE-2"])
        self.assertEqual(view["summary"]["active_writer_count"], 1)

    def test_queue_view_is_bounded_and_project_filterable(self):
        ws2 = Path(self.tmp.name) / "queue-ws2"
        ws2.mkdir()
        one = self.task("QUEUE-P1")
        one["project_id"] = "project-one"
        two = self.task("QUEUE-P2")
        two["project_id"] = "project-two"
        two["workspace"] = str(ws2)
        self.load([one, two], revision="queue-filter")
        filtered = self.orch.queue_view(project_id="project-two", limit=1)
        self.assertEqual(filtered["summary"]["total"], 1)
        self.assertEqual(filtered["tasks"][0]["task_id"], "QUEUE-P2")
        all_tasks = self.orch.queue_view(limit=1)
        self.assertEqual(all_tasks["summary"]["total"], 2)
        self.assertEqual(all_tasks["shown"], 1)
        self.assertTrue(all_tasks["truncated"])

    def test_cancel_task_only_applies_before_execution(self):
        self.load([self.task("CANCEL-1"), self.task("ACTIVE-1")], revision="cancel")
        cancelled = self.orch.cancel_task("CANCEL-1", "operator removed obsolete work")
        self.assertEqual(cancelled["status"], "CANCELLED")
        view = self.orch.queue_view()
        by_id = {item["task_id"]: item for item in view["tasks"]}
        self.assertEqual(by_id["CANCEL-1"]["task_status"], "CANCELLED")
        claim = self.orch.claim("worker")
        self.assertEqual(claim["task_id"], "ACTIVE-1")
        with self.assertRaisesRegex(ValueError, "task_active"):
            self.orch.cancel_task("ACTIVE-1", "must not cancel an active writer")
        repeated = self.orch.cancel_task("CANCEL-1", "idempotent")
        self.assertTrue(repeated["already_cancelled"])

    def test_check_timeout_retains_decoded_bounded_partial_output(self):
        check = {
            "id": "timeout-output",
            "argv": [sys.executable, "-c", (
                "import os, time\n"
                "os.write(1, b'discarded stdout prefix ' + "
                "'\u20ac-STDOUT-TAIL'.encode('utf-8') + b'\\xff')\n"
                "os.write(2, b'discarded stderr prefix ' + "
                "'\u0416-STDERR-TAIL'.encode('utf-8') + b'\\xff')\n"
                "time.sleep(10)\n"
            )],
            "executable_path": str(Path(sys.executable).resolve()),
            "timeout_sec": 1,
            "output_tail_chars": 14,
        }
        result = self.orch._run_check(self.ws, "timeout-output-run", check)

        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(check["timeout_sec"], 1)
        self.assertGreaterEqual(result["duration_ms"], 900)
        self.assertLess(result["duration_ms"], 5000)
        self.assertEqual(result["output_tail_chars"], 14)
        self.assertEqual(result["stdout"], "\u20ac-STDOUT-TAIL\ufffd")
        self.assertEqual(result["stderr"], "\u0416-STDERR-TAIL\ufffd")
        logged = json.loads(Path(result["log_path"]).read_text(encoding="utf-8"))
        self.assertEqual(logged["stdout"], "\u20ac-STDOUT-TAIL\ufffd")
        self.assertEqual(logged["stderr"], "\u0416-STDERR-TAIL\ufffd")

    def test_check_executable_is_bound_and_executed_by_absolute_path(self):
        runner = self.ws / "runner.sh"
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        check = {
            "id": "runner", "argv": ["./runner.sh"],
            "cwd": ".", "timeout_sec": 5,
        }
        self.load([self.task("AUTH-EXEC", checks=[check])], revision="auth-exec")
        claim = self.orch.claim("worker")
        context_check = claim["context"]["checks"][0]
        self.assertEqual(context_check["executable_path"], str(runner.resolve()))
        self.assertEqual(len(context_check["executable_sha256"]), 64)
        result = self.write_result(claim, "AUTH-EXEC")
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(
            result["checks"][0]["executed_argv"][0], str(runner.resolve())
        )
        self.assertEqual(result["check_authority"]["status"], "PASS")

    def test_check_executable_mutation_blocks_before_execution(self):
        sentinel = self.ws / "executed.txt"
        runner = self.ws / "mutable-runner.sh"
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        check = {
            "id": "mutable", "argv": ["./mutable-runner.sh"],
            "cwd": ".", "timeout_sec": 5,
        }
        self.load([self.task("AUTH-MUT", checks=[check])], revision="auth-mut")
        claim = self.orch.claim("worker")
        runner.write_text(
            "#!/bin/sh\necho executed > " + str(sentinel) + "\nexit 0\n",
            encoding="utf-8",
        )
        runner.chmod(0o755)
        result = self.write_result(claim, "AUTH-MUT")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn(
            "check_authority_changed:mutable:executable_hash_changed",
            result["reason"],
        )
        self.assertFalse(sentinel.exists())

    def test_check_support_file_mutation_blocks_before_execution(self):
        support = self.ws / "check_support.py"
        support.write_text("raise SystemExit(0)\n", encoding="utf-8")
        check = {
            "id": "support", "argv": [sys.executable, "check_support.py"],
            "cwd": ".", "timeout_sec": 5,
        }
        self.load([self.task("AUTH-SUPPORT", checks=[check])], revision="auth-support")
        claim = self.orch.claim("worker")
        bound = claim["context"]["checks"][0]["authority_files"]
        self.assertEqual(bound[0]["path"], "check_support.py")
        original = bound[0]["sha256"]
        self.assertEqual(len(original), 64)
        support.write_text("raise SystemExit(7)\n", encoding="utf-8")
        result = self.write_result(claim, "AUTH-SUPPORT")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn(
            "check_authority_changed:support:authority_file:check_support.py:HASH_CHANGED",
            result["reason"],
        )

    def test_oversized_context_blocks_without_run_or_capability(self):
        task = self.task("CONTEXT-LARGE")
        task["allowed_paths"] = [
            f"context/{index:03d}-" + ("x" * 80) + ".json"
            for index in range(400)
        ]
        self.load([task], revision="context-large")

        result = self.orch.claim("worker")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["reason"].startswith("context_pack_too_large:"))
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )
        status = self.orch.status()
        self.assertEqual(status["runs"], [])
        task_row = next(
            item for item in status["tasks"]
            if item["task_id"] == "CONTEXT-LARGE"
        )
        self.assertEqual(task_row["status"], "BLOCKED")
        self.assertEqual(self.orch.reconcile()["status"], "CLEAN")

    def test_claim_creates_private_exact_capability(self):
        self.load(
            [self.task("CAP-ATOMIC")],
            revision="cap-atomic",
        )
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        self.assertEqual(
            cap,
            self.orch.runtime / "claims" / f"{claim['run_id']}.json",
        )
        self.assertEqual(cap.stat().st_mode & 0o777, 0o600)
        payload = json.loads(cap.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"run_id", "lease_token"})
        self.assertEqual(payload["run_id"], claim["run_id"])
        self.assertTrue(payload["lease_token"])
        self.assertEqual(
            self.orch.lease_from_capability(claim["run_id"], cap),
            payload["lease_token"],
        )

    def test_capability_write_failure_aborts_run_and_releases_writer(self):
        self.load(
            [self.task("CAP-WRITE-FAIL")],
            revision="cap-write-fail",
        )
        with mock.patch(
            "orch.core.atomic_write_json",
            side_effect=OSError("synthetic capability write failure"),
        ):
            result = self.orch.claim("worker")

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "capability_create_failed")
        self.assertIn("run_id", result)
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )
        with self.orch.connect() as conn:
            run = conn.execute(
                "SELECT state,error,completed_at FROM runs WHERE run_id=?",
                (result["run_id"],),
            ).fetchone()
            task = conn.execute(
                "SELECT status FROM tasks WHERE task_id='CAP-WRITE-FAIL'"
            ).fetchone()
        self.assertEqual(run["state"], "ABORTED")
        self.assertIn("synthetic capability write failure", run["error"])
        self.assertTrue(run["completed_at"])
        self.assertEqual(task["status"], "BLOCKED")
        self.assertEqual(self.orch.reconcile()["status"], "CLEAN")

    def test_capability_read_rejects_symlink_and_alias_paths(self):
        self.load([self.task("CAP-LINK")], revision="cap-link")
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        alias = self.root / "capability-alias.json"
        alias.symlink_to(cap)
        with self.assertRaisesRegex(ValueError, "invalid_capability_file"):
            self.orch.lease_from_capability(claim["run_id"], alias)

        external = self.root / "external-capability.json"
        cap.rename(external)
        cap.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "invalid_capability_file"):
            self.orch.lease_from_capability(claim["run_id"], cap)
        self.assertTrue(cap.is_symlink())

    def test_capability_read_requires_private_bounded_exact_schema(self):
        self.load([self.task("CAP-BOUND")], revision="cap-bound")
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        cap.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "invalid_capability_mode"):
            self.orch.lease_from_capability(claim["run_id"], cap)

        cap.write_bytes(b"{" + b"x" * 4096)
        cap.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "capability_file_too_large"):
            self.orch.lease_from_capability(claim["run_id"], cap)

    def test_claim_exposes_exact_receipt_path_and_submit_records_digest(self):
        self.load([self.task("RECEIPT-OK")], revision="receipt-ok")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        self.assertEqual(
            receipt,
            self.orch.runtime / "worker_receipts" / f"{claim['run_id']}.json",
        )
        self.assertEqual(claim["context"]["receipt_file"], str(receipt))
        (self.ws / "RECEIPT-OK.json").write_text('{"ok":true}\n')
        raw = (
            json.dumps({
                "schema_version": 1,
                "run_id": claim["run_id"],
                "task_id": "RECEIPT-OK",
                "changed_paths": ["./RECEIPT-OK.json"],
                "summary": "bounded receipt",
            })
            + "\n"
        ).encode("utf-8")
        receipt.write_bytes(raw)
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        submitted = self.orch.submit(claim["run_id"], lease, receipt)
        self.assertEqual(submitted["status"], "RESULT_SUBMITTED")
        self.assertEqual(submitted["receipt_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(submitted["receipt_bytes"], len(raw))
        with self.orch.connect() as conn:
            stored = conn.execute(
                "SELECT receipt_json FROM runs WHERE run_id=?",
                (claim["run_id"],),
            ).fetchone()["receipt_json"]
        stored_data = json.loads(stored)
        self.assertEqual(stored_data["schema_version"], 1)
        self.assertEqual(stored_data["changed_paths"], ["RECEIPT-OK.json"])

    def test_submit_rejects_receipt_outside_claim_path(self):
        self.load([self.task("RECEIPT-PATH")], revision="receipt-path")
        claim = self.orch.claim("worker")
        wrong = self.root / "wrong-receipt.json"
        wrong.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-PATH",
            "changed_paths": ["RECEIPT-PATH.json"],
        }))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "invalid_receipt_path"):
            self.orch.submit(claim["run_id"], lease, wrong)
        self.assertEqual(
            next(
                item for item in self.orch.status()["runs"]
                if item["run_id"] == claim["run_id"]
            )["state"],
            "RUNNING",
        )

    def test_submit_rejects_symlinked_receipt(self):
        self.load([self.task("RECEIPT-LINK")], revision="receipt-link")
        claim = self.orch.claim("worker")
        external = self.root / "external-receipt.json"
        external.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-LINK",
            "changed_paths": ["RECEIPT-LINK.json"],
        }))
        receipt = Path(claim["receipt_file"])
        receipt.symlink_to(external)
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "receipt_file_missing_or_unsafe"):
            self.orch.submit(claim["run_id"], lease, receipt)
        self.assertTrue(receipt.is_symlink())

    def test_submit_rejects_fifo_receipt_without_blocking(self):
        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("FIFO nonblocking open unavailable")
        self.load([self.task("RECEIPT-FIFO")], revision="receipt-fifo")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        os.mkfifo(receipt)
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )

        def timeout_handler(_signum, _frame):
            raise TimeoutError("fifo_open_blocked")

        previous = signal.signal(signal.SIGALRM, timeout_handler)
        try:
            signal.alarm(2)
            with self.assertRaisesRegex(
                ValueError, "receipt_file_missing_or_unsafe"
            ):
                self.orch.submit(claim["run_id"], lease, receipt)
            signal.alarm(0)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertTrue(receipt.exists())

    def test_submit_rejects_oversized_receipt(self):
        self.load([self.task("RECEIPT-BIG")], revision="receipt-big")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        receipt.write_bytes(b"{" + b"x" * (64 * 1024 + 1))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "receipt_too_large"):
            self.orch.submit(claim["run_id"], lease, receipt)

    def test_submit_rejects_future_receipt_schema_and_unknown_fields(self):
        self.load([self.task("RECEIPT-SCHEMA")], revision="receipt-schema")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        receipt.write_text(json.dumps({
            "schema_version": 2,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-SCHEMA",
            "changed_paths": ["RECEIPT-SCHEMA.json"],
        }))
        with self.assertRaisesRegex(ValueError, "invalid_receipt_schema"):
            self.orch.submit(claim["run_id"], lease, receipt)
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-SCHEMA",
            "changed_paths": ["RECEIPT-SCHEMA.json"],
            "unexpected": True,
        }))
        with self.assertRaisesRegex(ValueError, "receipt_unknown_field:unexpected"):
            self.orch.submit(claim["run_id"], lease, receipt)

    def test_submit_bounds_changed_path_count(self):
        task = self.task("RECEIPT-MANY")
        task["allowed_paths"] = ["out/"]
        self.load([task], revision="receipt-many")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-MANY",
            "changed_paths": [f"out/{index}.json" for index in range(257)],
        }))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "receipt_changed_paths_too_many"):
            self.orch.submit(claim["run_id"], lease, receipt)

    def test_scope_escape_rejected(self):
        self.load([self.task('T1')]); c=self.orch.claim('w')
        receipt=Path(c["receipt_file"]); receipt.write_text(json.dumps({'run_id':c['run_id'],'task_id':'T1','changed_paths':['../escape']})+'\n')
        lease=self.orch.lease_from_capability(c['run_id'],Path(c['capability_file']))
        with self.assertRaisesRegex(ValueError,'path_not_allowed'):
            self.orch.submit(c['run_id'],lease,receipt)

    def test_failed_check_feedback_moves_to_new_chat_attempt(self):
        check={'id':'fail','argv':[sys.executable,'-c','import sys; sys.exit(7)'],'cwd':'.','timeout_sec':5}
        self.load([self.task('T1',checks=[check])])
        c1=self.orch.claim('w1'); result=self.write_result(c1,'T1')
        self.assertEqual(result['status'],'NEEDS_FIX')
        c2=self.orch.claim('w2')
        self.assertEqual(c2['attempt'],2)
        self.assertEqual(c2['context']['feedback']['kind'],'verification_failure')
        self.assertEqual(c2['context']['previous_snapshot_id'],result['snapshot_id'])

    def test_protected_change_blocks_verification_without_stuck_writer(self):
        sentinel=self.ws/'owner.txt'; sentinel.write_text('keep\n')
        digest=hashlib.sha256(sentinel.read_bytes()).hexdigest()
        self.load([self.task('T1',protected={'owner.txt':digest})])
        c=self.orch.claim('w'); (self.ws/'T1.json').write_text('{"value":1}\n'); sentinel.write_text('changed\n')
        receipt=Path(c["receipt_file"]); receipt.write_text(json.dumps({'run_id':c['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        cap=Path(c['capability_file']); lease=self.orch.lease_from_capability(c['run_id'],cap)
        self.orch.submit(c['run_id'],lease,receipt); quiesced=self.orch.quiesce(c['run_id'],lease)
        self.assertTrue(quiesced['capability_revoked']); self.assertFalse(cap.exists())
        blocked=self.orch.verify(c['run_id'])
        self.assertEqual(blocked['status'],'BLOCKED')
        self.assertIn('protected_path_changed',blocked['reason'])
        self.assertEqual(self.orch.reconcile()['status'],'CLEAN')
        self.assertEqual(self.orch.next_work()['status'],'NO_WORK')

    def test_review_is_snapshot_bound_and_feedback_reappears(self):
        self.load([self.task('T1',review=True)])
        c1=self.orch.claim('w1'); verified=self.write_result(c1,'T1')
        self.assertEqual(verified['status'],'REVIEWING')
        self.assertEqual(
            Path(verified['review_report_file']),
            self.orch.runtime / 'review_exports' / c1['run_id'] / 'review.json',
        )
        prepared=prepare_review(self.orch,c1['run_id'])
        report=Path(prepared['report'])
        self.assertEqual(report, Path(verified['review_report_file']))
        report.write_text(json.dumps({
            'run_id':c1['run_id'],
            'snapshot_id':'sha256:' + ('0' * 64),
            'verdict':'PASS','findings':[],'uncertainty':[]
        })+'\n')
        with self.assertRaisesRegex(ValueError,'stale_review'):
            self.orch.import_review(c1['run_id'],report)
        raw=(json.dumps({
            'run_id':c1['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'NEEDS_FIX',
            'findings':[{
                'severity':'important','path':'T1.json',
                'evidence':'fixture','impact':'repair'
            }],
            'uncertainty':[]
        })+'\n').encode('utf-8')
        report.write_bytes(raw)
        imported=self.orch.import_review(c1['run_id'],report)
        self.assertEqual(imported['report_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(imported['report_bytes'],len(raw))
        c2=self.orch.claim('w2')
        self.assertEqual(c2['context']['feedback']['kind'],'review')
        self.assertEqual(c2['context']['feedback']['findings'][0]['path'],'T1.json')

    def _review_fixture(self, task_id):
        self.load([self.task(task_id,review=True)],revision=f'{task_id}-plan')
        claim=self.orch.claim('review-worker')
        verified=self.write_result(claim,task_id)
        self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id'])
        return claim,verified,Path(prepared['report'])

    def test_review_import_rejects_alternate_report_path(self):
        claim,verified,expected=self._review_fixture('REVIEW-PATH')
        alternate=self.root/'alternate-review.json'
        alternate.write_text(json.dumps({
            'run_id':claim['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'PASS','findings':[],'uncertainty':[]
        }))
        with self.assertRaisesRegex(ValueError,'invalid_review_report_path'):
            self.orch.import_review(claim['run_id'],alternate)
        self.assertFalse(expected.exists())

    def test_review_import_rejects_symlinked_and_oversized_report(self):
        claim,verified,report=self._review_fixture('REVIEW-FILE')
        external=self.root/'external-review.json'
        external.write_text(json.dumps({
            'run_id':claim['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'PASS','findings':[],'uncertainty':[]
        }))
        report.symlink_to(external)
        with self.assertRaisesRegex(ValueError,'review_report_missing_or_unsafe'):
            self.orch.import_review(claim['run_id'],report)
        report.unlink()
        report.write_bytes(b'{' + (b'x' * (256 * 1024 + 1)))
        with self.assertRaisesRegex(ValueError,'review_report_too_large'):
            self.orch.import_review(claim['run_id'],report)

    def test_review_import_bounds_structure(self):
        claim,verified,report=self._review_fixture('REVIEW-STRUCT')
        base={
            'run_id':claim['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'PASS','findings':[],'uncertainty':[]
        }
        unknown=dict(base); unknown['extra']=True
        report.write_text(json.dumps(unknown))
        with self.assertRaisesRegex(ValueError,'review_report_unknown_field:extra'):
            self.orch.import_review(claim['run_id'],report)
        many=dict(base)
        many['findings']=[
            {'severity':'info','path':'x','evidence':'e','impact':'i'}
            for _ in range(101)
        ]
        report.write_text(json.dumps(many))
        with self.assertRaisesRegex(ValueError,'review_findings_too_many'):
            self.orch.import_review(claim['run_id'],report)

    def test_owner_approval_is_snapshot_bound(self):
        task=self.task('T1'); task['owner_acceptance']=True
        self.load([task])
        c=self.orch.claim('w'); self.write_result(c,'T1')
        self.assertEqual(self.orch.complete(c['run_id'])['status'],'WAITING_OWNER')
        approved=self.orch.approve(c['run_id'],'fixture approval')
        self.assertEqual(approved['status'],'APPROVED')
        self.assertEqual(self.orch.complete(c['run_id'])['status'],'COMPLETE')

    def test_pause_blocks_dispatch_until_resume(self):
        self.load([self.task('T1')])
        paused=self.orch.pause('fixture maintenance')
        self.assertEqual(paused['status'],'PAUSED')
        self.assertEqual(self.orch.claim('w')['status'],'PAUSED')
        self.assertEqual(self.orch.next_work()['status'],'PAUSED')
        self.assertEqual(self.orch.resume()['status'],'RESUMED')
        self.assertEqual(self.orch.claim('w')['status'],'CLAIMED')

    def test_checkpointed_abort_requires_process_inactivity_confirmation(self):
        self.load([self.task("CHECKPOINT-ABORT")], revision="checkpoint-abort")
        first = self.orch.claim("worker-one")
        cap = Path(first["capability_file"])
        lease = self.orch.lease_from_capability(first["run_id"], cap)
        self.orch.checkpoint(
            first["run_id"], lease, "worker handoff requested", "unknown"
        )

        with self.assertRaisesRegex(
            ValueError, "process_inactivity_confirmation_required"
        ):
            self.orch.abort(
                first["run_id"], "handoff", retry=True
            )
        self.assertTrue(cap.is_file())
        with self.orch.connect() as conn:
            run = conn.execute(
                "SELECT state FROM runs WHERE run_id=?", (first["run_id"],)
            ).fetchone()
            locks = self.orch._active_writer_locks(conn)
        self.assertEqual(run["state"], "RUNNING")
        self.assertEqual(
            next(iter(locks.values()))["run_id"], first["run_id"]
        )

        aborted = self.orch.abort(
            first["run_id"], "external process independently confirmed stopped",
            retry=True, process_inactivity_confirmed=True,
        )
        self.assertEqual(aborted["status"], "ABORTED")
        self.assertEqual(aborted["task_status"], "NEEDS_FIX")
        self.assertTrue(aborted["checkpoint_present"])
        self.assertEqual(
            aborted["process_inactivity_confirmation"], "operator_asserted"
        )
        self.assertFalse(aborted["process_fencing"])
        self.assertFalse(cap.exists())
        second = self.orch.claim("worker-two")
        self.assertEqual(second["status"], "CLAIMED")
        self.assertEqual(second["attempt"], 2)

    def test_abort_retry_releases_writer_and_creates_new_attempt(self):
        self.load([self.task('T1')])
        first=self.orch.claim('w1')
        aborted=self.orch.abort(first['run_id'],'synthetic crash recovery',retry=True)
        self.assertEqual(aborted['task_status'],'NEEDS_FIX')
        second=self.orch.claim('w2')
        self.assertEqual(second['status'],'CLAIMED')
        self.assertEqual(second['attempt'],2)
        self.assertNotEqual(second['run_id'],first['run_id'])

    def test_review_export_contains_check_support_and_verifier_evidence(self):
        checks_dir=self.ws/'checks'; checks_dir.mkdir()
        check_file=checks_dir/'check.py'
        check_file.write_text("print('support-ok')\n",encoding='utf-8')
        check={'id':'support','argv':[sys.executable,'checks/check.py'],'cwd':'.','timeout_sec':5}
        self.load([self.task('T1',checks=[check],review=True)])
        claim=self.orch.claim('w')
        verified=self.write_result(claim,'T1')
        self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id'])
        workspace=Path(prepared['workspace'])
        prompt=json.loads(Path(prepared['prompt']).read_text())
        self.assertTrue((workspace/'checks/check.py').is_file())
        check_evidence = prompt['verification_evidence'][0]['evidence_file']
        scope_evidence = prompt['scope_evidence']['evidence_file']
        authority_evidence = prompt['check_authority_evidence']['evidence_file']
        self.assertTrue((workspace/check_evidence).is_file())
        self.assertTrue((workspace/scope_evidence).is_file())
        self.assertTrue((workspace/authority_evidence).is_file())
        internal_root = Path(scope_evidence).parts[0]
        self.assertTrue(internal_root.startswith('.orch-review-evidence-'))
        self.assertEqual(Path(authority_evidence).parts[0], internal_root)
        self.assertEqual(Path(check_evidence).parts[0], internal_root)
        self.assertEqual(Path(check_evidence).parts[1], 'checks')
        self.assertIn('checks/check.py',prompt['support_files'])
        self.assertEqual(prompt['verification_evidence'][0]['exit_code'],0)
        self.assertEqual(prompt['scope_evidence']['status'],'NON_GIT_UNAVAILABLE')
        self.assertEqual(
            prompt['check_authority_evidence']['status'], 'PASS'
        )
        with self.orch.connect() as conn:
            snap = conn.execute(
                'SELECT manifest_json FROM snapshots WHERE snapshot_id=?',
                (verified['snapshot_id'],),
            ).fetchone()
        manifest = json.loads(snap['manifest_json'])
        self.assertIn('verification_evidence', manifest)
        self.assertEqual(
            verified['snapshot_id'],
            'sha256:' + hashlib.sha256(
                canonical_json(manifest).encode('utf-8')
            ).hexdigest(),
        )

    def test_review_export_internal_evidence_never_overwrites_project_path(self):
        relative = 'verification_evidence/scope.json'
        task = self.task('EVIDENCE-NAMESPACE', review=True)
        task['allowed_paths'] = [relative]
        self.load([task], revision='review-evidence-namespace')
        claim = self.orch.claim('w')
        project_file = self.ws / relative
        project_file.parent.mkdir(parents=True)
        original = b'{"project":"preserve"}\n'
        project_file.write_bytes(original)
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'EVIDENCE-NAMESPACE',
            'changed_paths': [relative],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)
        verified = self.orch.verify(claim['run_id'])
        self.assertEqual(verified['status'], 'REVIEWING')

        prepared = prepare_review(self.orch, claim['run_id'])
        workspace = Path(prepared['workspace'])
        prompt = json.loads(Path(prepared['prompt']).read_text())
        exported_project_file = workspace / relative
        self.assertEqual(exported_project_file.read_bytes(), original)
        scope_evidence = prompt['scope_evidence']['evidence_file']
        self.assertNotEqual(scope_evidence, relative)
        self.assertTrue((workspace / scope_evidence).is_file())
        self.assertTrue(
            Path(scope_evidence).parts[0].startswith(
                '.orch-review-evidence-'
            )
        )

    def test_review_export_check_evidence_cannot_alias_system_evidence(self):
        checks = [
            {
                'id': check_id,
                'argv': [sys.executable, '-c', 'print("ok")'],
                'cwd': '.',
                'timeout_sec': 5,
            }
            for check_id in ('scope', 'authority')
        ]
        self.load(
            [self.task('EVIDENCE-CHECK-ID', checks=checks, review=True)],
            revision='review-evidence-check-id',
        )
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'EVIDENCE-CHECK-ID')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        prompt = json.loads(Path(prepared['prompt']).read_text())
        workspace = Path(prepared['workspace'])

        scope_path = Path(prompt['scope_evidence']['evidence_file'])
        authority_path = Path(
            prompt['check_authority_evidence']['evidence_file']
        )
        scope_payload = json.loads(
            (workspace / scope_path).read_text(encoding='utf-8')
        )
        authority_payload = json.loads(
            (workspace / authority_path).read_text(encoding='utf-8')
        )
        self.assertEqual(scope_path.name, 'scope.json')
        self.assertEqual(authority_path.name, 'check-authority.json')
        self.assertEqual(
            scope_payload['status'], 'NON_GIT_UNAVAILABLE'
        )
        self.assertEqual(authority_payload['status'], 'PASS')

        by_id = {
            item['id']: item for item in prompt['verification_evidence']
        }
        self.assertEqual(set(by_id), {'scope', 'authority'})
        for check_id in ('scope', 'authority'):
            check_path = Path(by_id[check_id]['evidence_file'])
            self.assertEqual(
                check_path.name, f'check-result-{check_id}.json'
            )
            self.assertEqual(check_path.parent.name, 'checks')
            self.assertNotIn(check_path, {scope_path, authority_path})
            check_payload = json.loads(
                (workspace / check_path).read_text(encoding='utf-8')
            )
            self.assertEqual(check_payload['id'], check_id)
            self.assertEqual(check_payload['exit_code'], 0)
            self.assertEqual(by_id[check_id]['exit_code'], 0)

    def test_review_export_rejects_workspace_drift_after_verify(self):
        self.load([self.task('T1', review=True)], revision='review-drift')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        (self.ws / 'T1.json').write_text(
            '{"value":999}\n', encoding='utf-8'
        )
        with self.assertRaisesRegex(
            ValueError, 'snapshot_stale:T1.json'
        ):
            prepare_review(self.orch, claim['run_id'])

    def test_review_export_rejects_support_drift_after_verify(self):
        checks_dir = self.ws / 'checks'
        checks_dir.mkdir()
        check_file = checks_dir / 'check.py'
        check_file.write_text(
            "print('support-ok')\n", encoding='utf-8'
        )
        check = {
            'id': 'support-drift',
            'argv': [sys.executable, 'checks/check.py'],
            'cwd': '.',
            'timeout_sec': 5,
        }
        self.load(
            [self.task('T1', checks=[check], review=True)],
            revision='review-support-drift',
        )
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        check_file.write_text(
            "print('mutated-after-verify')\n", encoding='utf-8'
        )
        with self.assertRaisesRegex(
            ValueError, 'review_support_stale:checks/check.py'
        ):
            prepare_review(self.orch, claim['run_id'])

    def test_review_export_rejects_tampered_verifier_evidence(self):
        self.load(
            [self.task('T1', review=True)],
            revision='review-evidence-drift',
        )
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        scope_log = (
            self.orch.logs / f"{claim['run_id']}-scope.json"
        )
        scope_log.write_text(
            '{"status":"tampered"}\n', encoding='utf-8'
        )
        with self.assertRaisesRegex(
            ValueError, 'review_evidence_stale:scope'
        ):
            prepare_review(self.orch, claim['run_id'])
        self.assertFalse(
            (self.orch.runtime / 'review_exports' / claim['run_id']).exists()
        )

    def test_review_execution_rejects_tampered_prepared_prompt(self):
        self.load([self.task('T1', review=True)], revision='review-prompt-bind')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        Path(prepared['prompt']).write_text(
            '{"tampered":true}\n', encoding='utf-8'
        )
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_prompt_stale'):
                run_review(self.orch, claim['run_id'], execute=True,
                           model='gpt-6-astra', reasoning_effort='high')
        run.assert_not_called()

    def test_review_execution_rejects_tampered_prepared_schema(self):
        self.load([self.task('T1', review=True)], revision='review-schema-bind')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        Path(prepared['schema']).write_text(
            '{"tampered":true}\n', encoding='utf-8'
        )
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_schema_stale'):
                run_review(self.orch, claim['run_id'], execute=True,
                           model='gpt-6-astra', reasoning_effort='high')
        run.assert_not_called()

    def test_review_execution_refuses_preexisting_events_symlink(self):
        self.load([self.task('T1', review=True)], revision='review-events-link')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        victim = self.root / 'events-victim.txt'
        victim.write_text('owner-preserve\n', encoding='utf-8')
        events = Path(prepared['export']) / 'events.jsonl'
        events.symlink_to(victim)
        before = victim.read_bytes()
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value=self.review_preflight_fixture(),
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_events_unsafe'):
                run_review(self.orch, claim['run_id'], execute=True,
                           model='gpt-6-astra', reasoning_effort='high')
        run.assert_not_called()
        self.assertEqual(victim.read_bytes(), before)
        self.assertTrue(events.is_symlink())

    def test_review_execution_refuses_preexisting_report_symlink(self):
        self.load([self.task('T1', review=True)], revision='review-report-link')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        victim = self.root / 'report-victim.json'
        victim.write_text('owner-preserve\n', encoding='utf-8')
        report = Path(prepared['report'])
        report.symlink_to(victim)
        before = victim.read_bytes()
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value=self.review_preflight_fixture(),
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_report_preexisting'):
                run_review(self.orch, claim['run_id'], execute=True,
                           model='gpt-6-astra', reasoning_effort='high')
        run.assert_not_called()
        self.assertEqual(victim.read_bytes(), before)
        self.assertTrue(report.is_symlink())

    def test_review_execution_blocks_oversized_event_stream(self):
        self.load([self.task('T1', review=True)], revision='review-events-big')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])

        def fake_run(cmd, **kwargs):
            kwargs['stdout'].write('0123456789abcdef')
            kwargs['stdout'].flush()
            return subprocess.CompletedProcess(cmd, 0, stderr='')

        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value=self.review_preflight_fixture(),
        ), mock.patch(
            'orch.codex_review.REVIEW_EVENTS_MAX_BYTES', 8
        ), mock.patch(
            'orch.codex_review.subprocess.run', side_effect=fake_run
        ):
            result = run_review(self.orch, claim['run_id'], execute=True,
                           model='gpt-6-astra', reasoning_effort='high')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'review_events_too_large')
        self.assertFalse(Path(prepared['report']).exists())

    def test_review_execution_imports_mocked_bound_report(self):
        self.load([self.task('T1', review=True)], revision='review-exec-ok')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])

        def fake_run(cmd, **kwargs):
            self.assertEqual(cmd[cmd.index('--model') + 1], 'gpt-6-astra')
            self.assertIn('model_reasoning_effort="high"', cmd)
            self.assertIn('model_provider="openai"', cmd)
            self.assertIn('--ignore-user-config', cmd)
            self.assertEqual(cmd[cmd.index('--sandbox') + 1], 'read-only')
            self.assertEqual(cmd[cmd.index('-a') + 1], 'never')
            self.assertIn('plugins', cmd)
            kwargs['stdout'].write('{"event":"complete"}\n')
            kwargs['stdout'].flush()
            report = Path(cmd[cmd.index('-o') + 1])
            report.write_text(json.dumps({
                'run_id': claim['run_id'],
                'snapshot_id': verified['snapshot_id'],
                'verdict': 'PASS',
                'findings': [],
                'uncertainty': [],
            }) + '\n', encoding='utf-8')
            return subprocess.CompletedProcess(cmd, 0, stderr='')

        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value=self.review_preflight_fixture(),
        ), mock.patch(
            'orch.codex_review.subprocess.run', side_effect=fake_run
        ):
            result = run_review(self.orch, claim['run_id'], execute=True,
                           model='gpt-6-astra', reasoning_effort='high')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['imported']['status'], 'PASS')
        events = Path(result['events'])
        self.assertEqual(events.stat().st_mode & 0o777, 0o600)
        self.assertGreater(result['events_bytes'], 0)

    def test_review_execution_timeout_is_blocked(self):
        self.load([self.task('T1', review=True)], revision='review-exec-timeout')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])

        def timeout_run(cmd, **kwargs):
            kwargs['stdout'].write('{"event":"started"}\n')
            kwargs['stdout'].flush()
            raise subprocess.TimeoutExpired(cmd, 180, stderr='timed out')

        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value=self.review_preflight_fixture(),
        ), mock.patch(
            'orch.codex_review.subprocess.run', side_effect=timeout_run
        ):
            result = run_review(self.orch, claim['run_id'], execute=True,
                           model='gpt-6-astra', reasoning_effort='high')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'review_timeout')
        self.assertTrue(Path(result['events']).is_file())

    def test_verifier_blocks_check_mutation_of_snapshotted_file(self):
        mutator = self.ws / 'mutate.py'
        mutator.write_text(
            "from pathlib import Path\n"
            "Path('T1.json').write_text("
            "'{\\\"value\\\":999}\\n', encoding='utf-8')\n",
            encoding='utf-8',
        )
        check = {
            'id': 'mutates-output',
            'argv': [sys.executable, 'mutate.py'],
            'cwd': '.',
            'timeout_sec': 5,
        }
        self.load(
            [self.task('T1', checks=[check], review=True)],
            revision='review-check-mutation',
        )
        claim = self.orch.claim('w')
        result = self.write_result(claim, 'T1')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'snapshot_stale:T1.json')

    def test_verifier_rejects_dangling_symlink_in_declared_output(self):
        self.load([self.task('T1')], revision='dangling-output-symlink')
        claim = self.orch.claim('w')
        (self.ws / 'T1.json').symlink_to(self.ws / 'missing-output.json')
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)

        result = self.orch.verify(claim['run_id'])

        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'symlink_not_allowed:T1.json')

    def test_completion_rejects_dangling_symlink_after_verified_deletion(self):
        output = self.ws / 'T1.json'
        output.write_text('{"value": 1}\n', encoding='utf-8')
        self.load([self.task('T1')], revision='dangling-after-deletion')
        claim = self.orch.claim('w')
        output.unlink()
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)
        self.assertEqual(self.orch.verify(claim['run_id'])['status'], 'VERIFIED')
        output.symlink_to(self.ws / 'missing-output.json')

        with self.assertRaisesRegex(ValueError, 'snapshot_stale:T1.json'):
            self.orch.complete(claim['run_id'])

    def test_verifier_rejects_dangling_ancestor_symlink_in_declared_output(self):
        task = self.task('T1')
        task['allowed_paths'] = ['outputs/']
        self.load([task], revision='dangling-output-ancestor-symlink')
        claim = self.orch.claim('w')
        (self.ws / 'outputs').symlink_to(
            self.ws / 'missing-output-dir', target_is_directory=True
        )
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['outputs/T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)

        result = self.orch.verify(claim['run_id'])

        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(
            result['reason'], 'symlink_not_allowed:outputs/T1.json'
        )

    def test_completion_rejects_dangling_ancestor_symlink_after_deletion(self):
        output = self.ws / 'outputs' / 'T1.json'
        output.parent.mkdir()
        output.write_text('{"value": 1}\n', encoding='utf-8')
        task = self.task('T1')
        task['allowed_paths'] = ['outputs/']
        self.load([task], revision='dangling-after-ancestor-deletion')
        claim = self.orch.claim('w')
        output.unlink()
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['outputs/T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)
        self.assertEqual(self.orch.verify(claim['run_id'])['status'], 'VERIFIED')
        output.parent.rmdir()
        output.parent.symlink_to(
            self.ws / 'missing-output-dir', target_is_directory=True
        )

        with self.assertRaisesRegex(
            ValueError, 'snapshot_stale:outputs/T1.json'
        ):
            self.orch.complete(claim['run_id'])
        with self.orch.connect() as conn:
            state = conn.execute(
                'SELECT state FROM runs WHERE run_id=?', (claim['run_id'],)
            ).fetchone()['state']
        self.assertEqual(state, 'VERIFIED')

    def test_dependency_cycle_is_rejected_at_plan_load(self):
        one=self.task('T1',deps=['T2']); two=self.task('T2',deps=['T1'])
        with self.assertRaisesRegex(ValueError,'dependency_cycle'):
            self.load([one,two])

    def test_task_id_conflict_across_plan_revisions_is_rejected(self):
        self.load([self.task('T1')],revision='p1')
        path=self.root/'p2.json'
        path.write_text(json.dumps({'schema_version':1,'plan_revision':'p2','tasks':[self.task('T1')]})+'\n')
        with self.assertRaisesRegex(ValueError,'task_id_conflict:T1'):
            self.orch.load_plan(path)

    def test_capability_is_revoked_on_quiesce_and_abort(self):
        self.load([self.task('T1'),self.task('T2')])
        c1=self.orch.claim('w1'); cap1=Path(c1['capability_file']); lease1=self.orch.lease_from_capability(c1['run_id'],cap1)
        (self.ws/'T1.json').write_text('{"value":1}\n')
        receipt=Path(c1["receipt_file"]); receipt.write_text(json.dumps({'run_id':c1['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        self.orch.submit(c1['run_id'],lease1,receipt); self.orch.quiesce(c1['run_id'],lease1)
        self.assertFalse(cap1.exists())
        self.orch.verify(c1['run_id']); self.orch.complete(c1['run_id'])
        c2=self.orch.claim('w2'); cap2=Path(c2['capability_file'])
        self.assertTrue(cap2.exists()); self.orch.abort(c2['run_id'],'stop',retry=False)
        self.assertFalse(cap2.exists())

    def test_invalid_check_and_protected_hash_are_rejected_at_plan_load(self):
        bad_check=self.task('T1',checks=[{'id':'bad','argv':[],'cwd':'.'}])
        with self.assertRaisesRegex(ValueError,'invalid_check_argv'):
            self.load([bad_check],revision='bad-check')
        bad_hash=self.task('T2',protected={'owner.txt':'not-a-sha'})
        with self.assertRaisesRegex(ValueError,'invalid_protected_hash'):
            self.load([bad_hash],revision='bad-hash')

    def test_review_export_records_deleted_files_without_recreating_them(self):
        task=self.task('T1',review=True); self.load([task])
        target=self.ws/'T1.json'; target.write_text('{"old":true}\n',encoding='utf-8')
        claim=self.orch.claim('w'); target.unlink()
        receipt=Path(claim["receipt_file"])
        receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        lease=self.orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        self.orch.submit(claim['run_id'],lease,receipt); self.orch.quiesce(claim['run_id'],lease)
        verified=self.orch.verify(claim['run_id']); self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id']); prompt=json.loads(Path(prepared['prompt']).read_text())
        self.assertEqual(prompt['deleted_files'],['T1.json'])
        self.assertTrue(prompt['file_manifest']['T1.json']['deleted'])
        self.assertFalse((Path(prepared['workspace'])/'T1.json').exists())

    def test_plan_revision_digest_conflict(self):
        self.load([self.task('T1')],revision='same')
        path=self.root/'other.json'; path.write_text(json.dumps({'schema_version':1,'plan_revision':'same','tasks':[self.task('T2')]})+'\n')
        with self.assertRaisesRegex(ValueError,'plan_revision_digest_conflict'):
            self.orch.load_plan(path)

if __name__=='__main__':
    unittest.main()
