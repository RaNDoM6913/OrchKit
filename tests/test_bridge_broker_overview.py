"""Self-contained B04 logical read-only projection and metadata parsing proof.

Synthetic durable process records exercise parsing, never physical child exits.
Every public projection is enclosed by actual protected-state/SHM observations.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from orch import bridge_local_broker as broker_module
from orch import bridge_process as process_module
from orch import overview
from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_scope import LocalFileScope
from orch.config import configure_home
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding


class BrokerOverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-b04-overview-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        configure_home(self.home, profile="safe")
        self.orch = Orchestrator(self.home)
        # A retained fixture connection keeps committed WAL data stable while
        # projections open and explicitly close their own read-only readers.
        self.writer = self.orch.connect()
        self.addCleanup(self.writer.close)
        self.bindings, self.caps, self.projects = {}, {}, {}
        for label in ("a", "b", "c"):
            self._add_project(label)
        self.brokers = []
        self.addCleanup(self._cleanup_brokers)
        self.broker = self._broker()
        self.b04_read_only_observations = []

    def _git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              capture_output=True, text=True, check=True,
                              env=env, timeout=12)

    def _add_project(self, label):
        root = self.base / ("repo-" + label)
        self._git("init", "-q", "-b", "main", str(root))
        self._git("-C", str(root), "config", "user.email", "fixture@example.invalid")
        self._git("-C", str(root), "config", "user.name", "Fixture")
        (root / "README.md").write_text("safe-" + label + "\n", encoding="ascii")
        self._git("-C", str(root), "add", "README.md")
        self._git("-C", str(root), "commit", "-qm", "fixture")
        project = ProjectRegistry(self.home).add(
            root, name="B04 fixture " + label, profile="safe", review_mode="off",
            allow_commit=True, allow_push=False,
        )["project"]
        self.projects[label] = project
        plan = build_single_task_plan(
            project, task_id="B04-" + label.upper(), goal="Inspect recorded state",
            allowed_paths=["README.md"], plan_revision="b04-" + label,
        )
        path = self.base / (label + "-plan.json")
        write_plan(path, plan)
        self.orch.load_plan(path)
        claim = self.orch.claim("fixture-b04-" + label, project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED", claim)
        self.caps[label] = Path(claim["capability_file"])
        self.bindings[label] = WorkerRunBinding(
            project_id=project["project_id"], writer_key=project["writer_key"],
            task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"],
        )

    def _broker(self):
        broker = broker_module.LocalRunBroker(self.orch)
        self.brokers.append(broker)
        return broker

    def _cleanup_brokers(self):
        for broker in reversed(self.brokers):
            try:
                broker.close()
            except ValueError as exc:
                # Retained-history fixtures really close each scope before
                # injecting the ambiguous failure. No descriptor is leaked.
                if str(exc) != "bridge_broker_cleanup_unresolved":
                    raise

    def _open(self, label, broker=None):
        return (broker or self.broker).open_context(self.bindings[label], self.caps[label])

    @staticmethod
    def _path_snapshot(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {"exists": False}
        result = {"exists": True, "dev": info.st_dev, "ino": info.st_ino,
                  "mode": info.st_mode}
        if stat.S_ISREG(info.st_mode):
            result.update(size=info.st_size,
                          sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISLNK(info.st_mode):
            result["target"] = os.readlink(path)
        return result

    def _snapshot(self, shm_paths):
        # The complete fixture tree, including Git/project/capability bytes,
        # directory identities and absent roots. Only exact ledger SHM paths
        # are excluded; their actual snapshots are retained separately.
        paths = [self.base] + sorted(self.base.rglob("*"))
        return {str(path): self._path_snapshot(path)
                for path in paths if path not in shm_paths}

    @contextmanager
    def _observe(self, subject, *, databases=None, forbid_operations=False,
                 text_factory=None, authorizer=None):
        databases = databases if databases is not None else [self.orch.db_path]
        shm_paths = [Path(str(path) + "-shm") for path in databases]
        before = self._snapshot(shm_paths)
        shm_before = {path: self._path_snapshot(path) for path in shm_paths}
        modes, opened = [], []
        real_connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            projection_closed = False

            def close(connection):
                connection.projection_closed = True
                return super().close()

        def connect(database, *args, **kwargs):
            mode = "ro" if kwargs.get("uri") and "?mode=ro" in str(database) else "WRITABLE"
            modes.append(mode)
            connection = real_connect(database, *args, factory=ObservedConnection, **kwargs)
            opened.append(connection)
            if text_factory is not None:
                connection.text_factory = text_factory
            if authorizer is not None:
                connection.set_authorizer(authorizer)
            return connection

        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(sqlite3, "connect", side_effect=connect))
                if forbid_operations:
                    denied = AssertionError("projection invoked a mutable authority/resource operation")
                    for owner, name in (
                        (Orchestrator, "__init__"),
                        (Orchestrator, "connect"), (Orchestrator, "lease_from_capability"),
                        (LocalRunSessionAuthority, "require_session"),
                        (LocalRunSessionAuthority, "close_session"),
                        (LocalFileScope, "close"), (process_module, "_spawn_process"),
                        (process_module.LocalOwnedProcessOperations, "start"),
                        (process_module.LocalOwnedProcessOperations, "poll"),
                        (process_module.LocalOwnedProcessOperations, "terminate"),
                        (process_module.LocalOwnedProcessOperations, "close"),
                        (broker_module.LocalRunBroker, "close"),
                        (broker_module.LocalRunBroker, "_close_context"),
                    ):
                        stack.enter_context(mock.patch.object(owner, name, side_effect=denied))
                    real_open = Path.open

                    def open_path(path, *args, **kwargs):
                        if path in self.caps.values():
                            raise AssertionError("projection read a capability file")
                        return real_open(path, *args, **kwargs)

                    stack.enter_context(mock.patch.object(Path, "open", new=open_path))
                yield
        finally:
            after = self._snapshot(shm_paths)
            record = {
                "subject": subject, "before": before, "after": after,
                "connection_modes": modes,
                "sqlite_shm_observations": [
                    {"path": str(path), "before": shm_before[path],
                     "after": self._path_snapshot(path)} for path in shm_paths
                ],
            }
            self.b04_read_only_observations.append(record)
            self.assertEqual(before, after, subject)
            self.assertTrue(all(mode == "ro" for mode in modes), modes)
            self.assertTrue(all(conn.projection_closed for conn in opened), subject)

    def _overview(self, **kwargs):
        return overview.operator_overview(self.home, **kwargs)["bridge"]

    def _event(self, kind, binding, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"))
        cursor = self.writer.execute(
            "INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES('fixture',?,?,?,?)",
            (kind, binding.task_id, binding.run_id, raw),
        )
        return cursor.lastrowid

    def _intent(self, label="b"):
        binding = self.bindings[label]
        request = WorkerOperationRequest(binding, "recorded-operation", "process.start", "private-profile")
        payload = {"schema_version": 1, "request": asdict(request),
                   "session_hash": "1" * 64, "profile_hash": "2" * 64,
                   "handle": "private-process-handle"}
        seq = self._event("PROCESS_DISPATCH_INTENT", binding, payload)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                          separators=(",", ":")).encode("ascii")).hexdigest()
        return payload, seq, digest

    @staticmethod
    def _by_task(rows):
        return {row["binding"]["task_id"]: row for row in rows}

    def test_b04_separates_native_routes_and_unmatched_dispatch_without_completion(self):
        for label in ("a", "b", "c"):
            self._open(label)
        self.writer.execute("UPDATE runs SET state='RESULT_SUBMITTED' WHERE run_id=?",
                            (self.bindings["b"].run_id,))
        for binding in self.bindings.values():
            self._event("CHECKPOINT", binding, {"status": "COMPLETE"})
        self._intent()
        with self._observe("A/B/C independent dimensions", forbid_operations=True):
            self.assertTrue(callable(getattr(self.broker, "overview_projection", None)),
                            "B04 bounded broker projection is missing")
            result = self._overview(broker=self.broker)
            native = self._by_task(result["native"]["runs"])
            routes = self._by_task(result["routing"]["contexts"])
            processes = self._by_task(result["processes"]["runs"])
            self.assertEqual([native["B04-" + label]["run_state"] for label in "ABC"],
                             ["RUNNING", "RESULT_SUBMITTED", "RUNNING"])
            self.assertEqual(routes["B04-B"]["route_state"], "OPEN_IN_THIS_BROKER")
            self.assertEqual(routes["B04-B"]["current_native_authority"], "UNKNOWN")
            self.assertEqual(processes["B04-B"]["status"], "UNKNOWN")
            self.assertEqual(processes["B04-B"]["unresolved_dispatch_count"], 1)
            self.assertEqual(processes["B04-A"]["status"], "NO_PROCESS_RECORDS")
            self.assertEqual(result["external_connectivity"], "UNKNOWN")

    def test_b04_missing_broker_restart_and_new_empty_broker_preserve_unknown_connectivity(self):
        self._open("a")
        replacement = self._broker()
        for label, broker in (("no broker", None), ("empty replacement", replacement)):
            with self._observe(label, forbid_operations=True):
                result = self._overview(broker=broker)
                self.assertEqual(result["routing"]["scope"], "THIS_BROKER_ONLY")
                self.assertEqual(result["routing"]["contexts"], [])
                self.assertEqual(result["routing"]["unlisted_route_state"], "UNKNOWN")
                self.assertEqual(result["external_connectivity"], "UNKNOWN")
        # Recreating the ledger adapter is fixture setup, never a projection.
        restarted = broker_module.LocalRunBroker(Orchestrator(self.home))
        self.brokers.append(restarted)
        with self._observe("restarted adapter", forbid_operations=True):
            self.assertEqual(self._overview(broker=restarted)["routing"]["contexts"], [])

    def test_b04_closed_b_remains_uncertain_while_a_and_c_are_usable(self):
        contexts = {label: self._open(label) for label in "abc"}
        self._intent()
        contexts["b"].close()
        with self._observe("closed B unresolved metadata", forbid_operations=True):
            result = self._overview(broker=self.broker)
            routes = self._by_task(result["routing"]["contexts"])
            self.assertEqual(set(routes), {"B04-A", "B04-C"})
            self.assertEqual(result["processes"]["observed_unresolved_dispatch_count"], 1)
            self.assertEqual(self._by_task(result["native"]["runs"])["B04-B"]["run_state"], "RUNNING")
        for label in "ac":
            self.assertEqual(contexts[label].status(), {
                "binding": asdict(self.bindings[label]),
                "native": {"run_state": "RUNNING", "task_state": "IN_PROGRESS"},
            })
            request = WorkerOperationRequest(self.bindings[label], "read-after-overview", "file.read", "README.md")
            self.assertEqual(contexts[label].read(request).output, ("safe-" + label + "\n").encode())

    def test_b04_valid_linked_exit_resolves_recorded_uncertainty_only(self):
        version, octets = self.writer.execute(
            "SELECT sqlite_version(), octet_length(?)", ("\u00e9",),
        ).fetchone()
        print("B04_SQLITE_CAPABILITY " + json.dumps({
            "sqlite_version": version,
            "octet_length_available": True,
            "octet_length_result": octets,
        }, sort_keys=True), flush=True)
        self.assertEqual(version, sqlite3.sqlite_version)
        self.assertEqual(octets, 2)
        payload, seq, digest = self._intent()
        terminal = dict(payload, intent_seq=seq, intent_hash=digest, exit_code=0,
                        reason="exited", output_bytes=0, truncated=False)
        self._event("PROCESS_EXIT_OBSERVED", self.bindings["b"], terminal)
        with self._observe("synthetic linked exit metadata", forbid_operations=True):
            result = self._overview()
            process = self._by_task(result["processes"]["runs"])["B04-B"]
            self.assertEqual(process["status"], "RECORDED_EXITS")
            self.assertEqual(process["unresolved_dispatch_count"], 0)
            self.assertEqual(result["external_connectivity"], "UNKNOWN")
            self.assertEqual(self._by_task(result["native"]["runs"])["B04-B"]["run_state"], "RUNNING")

    def test_b04_malformed_oversized_duplicate_and_cross_binding_records_are_blocked(self):
        for problem in ("malformed", "oversized", "duplicate_json", "duplicate_dispatch", "cross_binding", "orphan", "bad_exit"):
            with self.subTest(problem=problem):
                self.writer.execute("DELETE FROM events WHERE kind LIKE 'PROCESS_%'")
                payload, seq, digest = self._intent()
                if problem == "malformed":
                    raw = "{broken"
                elif problem == "oversized":
                    raw = " " * 9000 + json.dumps(payload)
                elif problem == "duplicate_json":
                    raw = json.dumps(payload)[:-1] + ',"schema_version":1}'
                elif problem == "cross_binding":
                    payload["request"]["binding"] = asdict(self.bindings["a"])
                    raw = json.dumps(payload)
                else:
                    raw = json.dumps(payload)
                if problem == "duplicate_dispatch":
                    self._event("PROCESS_DISPATCH_INTENT", self.bindings["b"], raw)
                elif problem == "bad_exit":
                    terminal = dict(payload, intent_seq=seq, intent_hash="9" * 64,
                                    exit_code=0, reason="exited", output_bytes=0, truncated=False)
                    self._event("PROCESS_EXIT_OBSERVED", self.bindings["b"], terminal)
                elif problem == "orphan":
                    self.writer.execute("UPDATE events SET run_id='orphan-run' WHERE seq=?", (seq,))
                else:
                    self.writer.execute("UPDATE events SET payload_json=? WHERE seq=?", (raw, seq))
                with self._observe("blocked metadata " + problem, forbid_operations=True):
                    result = self._overview()
                    self.assertEqual(result["processes"]["status"], "BLOCKED")
                    self.assertIsNone(result["processes"]["observed_unresolved_dispatch_count"])
                    self.assertEqual(len(result["native"]["runs"]), 3)
                    self.assertNotIn("private-process-handle", json.dumps(result))

    def test_b04_run_and_retained_history_overflow_are_explicit(self):
        self._intent()
        with self._observe("bounded native and process runs", forbid_operations=True):
            result = self._overview(limit=1)
            self.assertEqual(result["native"]["coverage"], "TRUNCATED")
            self.assertEqual(result["native"]["status"], "UNKNOWN")
            self.assertEqual(len(result["native"]["runs"]), 1)
            self.assertEqual(result["processes"]["coverage"], "TRUNCATED")
            self.assertEqual(result["processes"]["status"], "UNKNOWN")
            self.assertEqual(result["processes"]["count_scope"], "VALIDATED_VISIBLE_CHAINS_ONLY")
        real_close = LocalFileScope.close

        def closed_then_uncertain(scope):
            real_close(scope)
            raise OSError("private ambiguous cleanup")

        for _ in range(2):
            contexts = [self._open(label) for label in "abc"]
            with mock.patch.object(LocalFileScope, "close", new=closed_then_uncertain):
                for context in contexts:
                    with self.assertRaisesRegex(ValueError, "bridge_broker_cleanup_unresolved"):
                        context.close()
        with self._observe("six retained owners bounded at four", forbid_operations=True):
            routes = self.broker.overview_projection(limit=4)
            self.assertEqual(routes["coverage"], "TRUNCATED")
            self.assertEqual(routes["status"], "UNKNOWN")
            self.assertEqual(len(routes["contexts"]), 4)
            self.assertTrue(all(item["route_state"] == "CLOSED_IN_THIS_BROKER" for item in routes["contexts"]))
            self.assertTrue(all(item["cleanup_unresolved"] for item in routes["contexts"]))

    def test_b04_invalid_native_binding_cannot_become_complete_clean_process_coverage(self):
        self.writer.execute("UPDATE tasks SET project_id='invalid/project' WHERE task_id='B04-B'")
        with self._observe("invalid native binding keeps process coverage unknown", forbid_operations=True):
            result = self._overview()
            self.assertEqual(result["native"]["status"], "BLOCKED")
            self.assertEqual(len(result["native"]["runs"]), 2)
            self.assertEqual(result["processes"]["status"], "UNKNOWN")
            self.assertEqual(result["processes"]["coverage"], "UNKNOWN")

    def test_b04_missing_unsafe_and_old_schema_are_preserved_without_repair(self):
        phantom = self.base / "missing-home"
        with self._observe("absent home", databases=[phantom / ".runtime" / "orch.sqlite3"], forbid_operations=True):
            self.assertEqual(overview.operator_overview(phantom)["bridge"]["native"]["status"], "NOT_INITIALIZED")
            self.assertFalse(phantom.exists())
        claims = self.home / ".runtime" / "claims"
        original = claims.stat().st_mode & 0o777
        claims.chmod(0o755)
        with self._observe("unsafe state mode", forbid_operations=True):
            self.assertEqual(self._overview()["native"]["status"], "BLOCKED")
            self.assertEqual(claims.stat().st_mode & 0o777, 0o755)
        claims.chmod(original)
        hooks = self.home / ".runtime" / "git-hooks-disabled"
        hooks.rmdir()
        with self._observe("missing required state directory", forbid_operations=True):
            self.assertEqual(self._overview()["native"]["status"], "BLOCKED")
            self.assertFalse(hooks.exists())
        hooks.mkdir(mode=0o700)
        self.writer.execute("PRAGMA user_version=3")
        with self._observe("old schema no migration", forbid_operations=True):
            native = self._overview()["native"]
            self.assertEqual((native["status"], native["reason"]), ("BLOCKED", "state_schema_mismatch"))
        self.assertEqual(self.writer.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_b04_reads_current_wal_only_commit_with_ro_connections_closed(self):
        # Fixture setup checkpoints once, then commits a row change into WAL.
        # Main database bytes remain identical; the projection must see WAL.
        self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        main_before = hashlib.sha256(self.orch.db_path.read_bytes()).hexdigest()
        self.writer.execute("UPDATE runs SET state='RESULT_SUBMITTED' WHERE run_id=?", (self.bindings["a"].run_id,))
        self.assertEqual(main_before, hashlib.sha256(self.orch.db_path.read_bytes()).hexdigest())
        self.assertGreater(Path(str(self.orch.db_path) + "-wal").stat().st_size, 0)
        with self._observe("WAL-only committed native state", forbid_operations=True):
            result = self._overview()
            self.assertEqual(self._by_task(result["native"]["runs"])["B04-A"]["run_state"], "RESULT_SUBMITTED")
        self.assertEqual(self.b04_read_only_observations[-1]["connection_modes"], ["ro"])

    def test_b04_metadata_gate_prevents_oversized_native_text_materialization(self):
        # A late Python validation would already receive the oversized bytes.
        # These modest fixtures make that failure observable without an OOM.
        for table, column, key, key_value, cap in (
            ("runs", "run_id", "task_id", "B04-A", 200),
            ("runs", "state", "task_id", "B04-A", 128),
            ("tasks", "writer_key", "task_id", "B04-A", 200),
            ("tasks", "status", "task_id", "B04-A", 128),
        ):
            with self.subTest(table=table, column=column):
                original = self.writer.execute(
                    f"SELECT {column} FROM {table} WHERE {key}=?", (key_value,),
                ).fetchone()[0]
                self.writer.execute(
                    f"UPDATE {table} SET {column}=? WHERE {key}=?",
                    ("x" * 262144, key_value),
                )
                decoded_sizes, oversized = [], []

                def bounded_text(raw):
                    decoded_sizes.append(len(raw))
                    if len(raw) > cap:
                        oversized.append(len(raw))
                        raise AssertionError("oversized native text reached Python")
                    return raw.decode("utf-8")

                try:
                    with self._observe("native bytes before fetch " + column,
                                       forbid_operations=True, text_factory=bounded_text):
                        result = self._overview()
                        self.assertEqual(result["native"]["status"], "BLOCKED")
                        self.assertEqual(result["processes"]["status"], "BLOCKED")
                        self.assertIsNone(result["processes"]["observed_unresolved_dispatch_count"])
                        self.assertEqual(oversized, [])
                        self.assertTrue(decoded_sizes)
                        self.assertLessEqual(max(decoded_sizes), cap)
                finally:
                    self.writer.execute(
                        f"UPDATE {table} SET {column}=? WHERE {key}=?", (original, key_value),
                    )

    def test_b04_metadata_gate_prevents_global_process_text_fetch_and_json_scan(self):
        # Observe Python text conversion and real SQLite JSON-function access,
        # rather than asserting a particular SQL statement or mocking _chains.
        cases = (
            ("PROCESS_DISPATCH_INTENT", "payload_json", 262144, False, 8192, False),
            ("PROCESS_DISPATCH_INTENT", "payload_json", 8193, False, 8192, False),
            ("PROCESS_EXIT_OBSERVED", "payload_json", 8193, False, 8192, False),
            ("PROCESS_CLOSURE_ACKNOWLEDGED", "payload_json", 32769, False, 32768, False),
            ("PROCESS_DISPATCH_INTENT", "payload_json", 262144, True, 8192, False),
            ("PROCESS_CLOSURE_ACKNOWLEDGED", "payload_json", 262144, True, 32768, False),
            ("PROCESS_DISPATCH_INTENT", "run_id", 262144, True, 200, False),
            ("PROCESS_DISPATCH_INTENT", "task_id", 262144, True, 200, False),
            # Simulate an unavailable metadata function without substituting a
            # permissive reader. The whole native projection must fail closed.
            ("PROCESS_DISPATCH_INTENT", "payload_json", 262144, False, 8192, True),
        )
        for kind, column, size, foreign, cap, deny_metadata in cases:
            with self.subTest(kind=kind, column=column, size=size, foreign=foreign,
                              deny_metadata=deny_metadata):
                self.writer.execute("DELETE FROM events WHERE kind IN (?,?,?)", (
                    "PROCESS_DISPATCH_INTENT", "PROCESS_EXIT_OBSERVED",
                    "PROCESS_CLOSURE_ACKNOWLEDGED",
                ))
                seq = self._event(kind, self.bindings["b"], "{}")
                if foreign:
                    self.writer.execute("UPDATE events SET run_id='unregistered-run',task_id='unregistered-task' WHERE seq=?", (seq,))
                value = "{}" + " " * (size - 2) if column == "payload_json" else "x" * size
                self.writer.execute(f"UPDATE events SET {column}=? WHERE seq=?", (value, seq))
                decoded_sizes, oversized, json_functions = [], [], []

                def bounded_text(raw):
                    decoded_sizes.append(len(raw))
                    if len(raw) > cap:
                        oversized.append(len(raw))
                        raise AssertionError("oversized process text reached Python")
                    return raw.decode("utf-8")

                def observe_functions(action, first, second, database, trigger):
                    if action == sqlite3.SQLITE_FUNCTION:
                        name = (second or "").lower()
                        if name in ("json_valid", "json_extract"):
                            json_functions.append(name)
                        if name == "octet_length" and deny_metadata:
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                with self._observe("global process bytes before fetch and JSON scan",
                                   forbid_operations=True, text_factory=bounded_text,
                                   authorizer=observe_functions):
                    result = self._overview()
                    self.assertEqual(result["processes"]["status"], "BLOCKED")
                    self.assertIsNone(result["processes"]["observed_unresolved_dispatch_count"])
                    self.assertEqual(oversized, [])
                    self.assertEqual(json_functions, [])
                    self.assertTrue(decoded_sizes)
                    self.assertLessEqual(max(decoded_sizes), cap)
                    if deny_metadata:
                        self.assertEqual(result["native"]["status"], "BLOCKED")

    def test_b04_large_event_scan_exhausts_budget_without_claiming_clean_coverage(self):
        # The frozen validator searches JSON across events. A returned-row
        # LIMIT alone cannot bound a scan through unrelated durable history.
        self.writer.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<300000) "
            "INSERT INTO events(ts,kind,payload_json) SELECT 'fixture','UNRELATED','{}' FROM n"
        )
        with self._observe("bounded large durable history scan", forbid_operations=True):
            result = self._overview()
            self.assertEqual(result["processes"]["status"], "BLOCKED")
            self.assertIsNone(result["processes"]["observed_unresolved_dispatch_count"])
            self.assertNotEqual(result["processes"]["coverage"], "COMPLETE")

    def test_b04_redacts_unrecognized_states_and_all_non_binding_private_metadata(self):
        self._open("a")
        payload, _, _ = self._intent()
        self.writer.execute("UPDATE runs SET state='private-native-state',lease_token='private-token',receipt_json=? WHERE run_id=?",
                            (json.dumps({"output": "private-output"}), self.bindings["a"].run_id))
        with self._observe("whitelisted new projection", forbid_operations=True):
            result = self._overview(broker=self.broker)
            encoded = json.dumps(result)
            self.assertEqual(self._by_task(result["native"]["runs"])["B04-A"]["run_state"], "UNKNOWN")
            for secret in ("private-native-state", "private-token", "private-output", "private-profile", "private-process-handle",
                           payload["session_hash"], payload["profile_hash"], str(self.caps["a"]), self._open_session_id()):
                self.assertNotIn(secret, encoded)

    def _open_session_id(self):
        return self.broker._LocalRunBroker__owned[0].session_id

    def test_b04_rejects_foreign_home_invalid_limits_and_wrong_owner(self):
        self._open("a")
        for limit in (True, 0, -1, 1.5, "2", None, 129):
            with self.subTest(limit=limit), self._observe("invalid limit " + repr(limit), forbid_operations=True):
                with self.assertRaisesRegex(ValueError, "bridge_overview_invalid_limit"):
                    self._overview(broker=self.broker, limit=limit)
                with self.assertRaisesRegex(ValueError, "bridge_overview_invalid_limit"):
                    self.broker.overview_projection(limit=limit)
        with self._observe("foreign home denied", forbid_operations=True):
            with self.assertRaisesRegex(ValueError, "bridge_broker_home_mismatch"):
                overview.operator_overview(self.base / "foreign", broker=self.broker)
        with self._observe("wrong creating PID denied", forbid_operations=True):
            with mock.patch.object(broker_module.os, "getpid", return_value=os.getpid() + 1000):
                with self.assertRaisesRegex(ValueError, "bridge_broker_owner_mismatch"):
                    self.broker.overview_projection()
        errors = []

        def other_thread():
            try:
                self.broker.overview_projection()
            except ValueError as exc:
                errors.append(str(exc))

        with self._observe("wrong creating thread denied", forbid_operations=True):
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["bridge_broker_owner_mismatch"])


if __name__ == "__main__":
    unittest.main()
