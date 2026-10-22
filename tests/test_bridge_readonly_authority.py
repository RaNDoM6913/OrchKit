"""Real disposable native ledgers for the local read-only authority view."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from orch.bridge_authorization import LocalRunSessionAuthority
from orch.bridge_file_scope import LocalFileScope
from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding


class ReadOnlyAuthorityLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-readonly-authority-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        # Reserved URI characters must remain literal filesystem characters.
        self.home = self.base / "home #?%"
        self.home.mkdir(mode=0o700)
        self.orch = Orchestrator(self.home)
        self.db = self.home / ".runtime" / "orch.sqlite3"

    def _view(self, root=None):
        return ReadOnlyAuthorityLedger(root or self.home)

    def _metadata(self, path):
        info = path.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                info.st_gid, info.st_nlink)

    def _database_snapshot(self):
        with closing(sqlite3.connect(str(self.db))) as conn:
            return (conn.execute("PRAGMA user_version").fetchone()[0],
                    tuple(conn.iterdump()))

    def test_existing_inspector_can_write_and_repairs_unsafe_mode(self):
        self.db.chmod(0o644)
        with self.orch.connect() as conn:
            conn.execute("INSERT INTO events(ts,kind,payload_json) VALUES(?,?,?)",
                         ("fixture", "writable-inspection", "{}"))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='writable-inspection'"
            ).fetchone()[0], 1)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_current_private_schema_opens_without_native_mutation(self):
        before = self._database_snapshot()
        identities = {p: self._metadata(p) for p in
                      (self.home, self.db.parent, self.db)}
        view = self._view()
        with closing(view.connect()) as conn:
            self.assertIsInstance(conn, sqlite3.Connection)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT MAX(attempt) AS latest FROM runs WHERE task_id=?",
                ("absent",),
            ).fetchone()["latest"], None)
        self.assertEqual(self._database_snapshot(), before)
        self.assertEqual({p: self._metadata(p) for p in identities}, identities)

    def test_authority_context_releases_its_database_connection(self):
        with self._view().connect() as conn:
            self.assertEqual(conn.execute("SELECT MAX(attempt) FROM runs").fetchone()[0],
                             None)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT MAX(attempt) FROM runs")
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with self._view().connect() as failed:
                raise RuntimeError("fixture failure")
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute("SELECT MAX(attempt) FROM runs")

    def test_missing_home_runtime_or_database_is_not_created(self):
        roots = (self.base / "absent", self.base / "empty")
        roots[1].mkdir(mode=0o700)
        for root in roots:
            with self.subTest(root=root.name):
                before = tuple(root.iterdir()) if root.exists() else None
                with self.assertRaisesRegex(ValueError, "bridge_authority_"):
                    with closing(self._view(root).connect()):
                        pass
                self.assertEqual(tuple(root.iterdir()) if root.exists() else None,
                                 before)
        self.db.unlink()
        before = tuple(self.db.parent.iterdir())
        with self.assertRaisesRegex(ValueError, "bridge_authority_"):
            with closing(self._view().connect()):
                pass
        self.assertEqual(tuple(self.db.parent.iterdir()), before)

    def test_unsafe_modes_are_refused_without_repair(self):
        for path, unsafe, private in ((self.home, 0o755, 0o700),
                                     (self.db.parent, 0o755, 0o700),
                                     (self.db, 0o644, 0o600)):
            with self.subTest(path=path.name):
                path.chmod(unsafe)
                before = self._metadata(path)
                with self.assertRaisesRegex(ValueError, "bridge_authority_unsafe"):
                    with closing(self._view().connect()):
                        pass
                self.assertEqual(self._metadata(path), before)
                path.chmod(private)

    def test_symlink_home_runtime_database_and_sidecars_are_refused(self):
        for path in (self.home, self.db.parent, self.db,
                     self.db.with_name(self.db.name + "-wal"),
                     self.db.with_name(self.db.name + "-shm")):
            with self.subTest(path=path.name):
                existed = path.exists()
                saved = path.with_name(path.name + ".saved")
                if existed:
                    path.rename(saved)
                else:
                    saved.write_bytes(b"fixture")
                    saved.chmod(0o600)
                path.symlink_to(saved)
                try:
                    with self.assertRaisesRegex(ValueError, "bridge_authority_unsafe"):
                        with closing(self._view().connect()):
                            pass
                    self.assertTrue(path.is_symlink())
                finally:
                    path.unlink()
                    if existed:
                        saved.rename(path)
                    else:
                        saved.unlink()

    def test_unsafe_existing_sidecars_are_not_chmodded(self):
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix):
                path = self.db.with_name(self.db.name + suffix)
                path.write_bytes(b"unsafe-fixture")
                path.chmod(0o644)
                before = self._metadata(path), path.read_bytes()
                try:
                    with self.assertRaisesRegex(ValueError, "bridge_authority_unsafe"):
                        with closing(self._view().connect()):
                            pass
                    self.assertEqual((self._metadata(path), path.read_bytes()), before)
                finally:
                    path.unlink()

    def test_foreign_owner_is_refused_without_permission_repair(self):
        # Real private paths, with a different expected local account identity.
        # No fixture chown or privileged operation is needed for this refusal.
        view = self._view()
        before = self._metadata(self.db)
        with mock.patch("orch.bridge_readonly_authority.os.getuid",
                        return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "bridge_authority_unsafe"):
                with closing(view.connect()):
                    pass
        self.assertEqual(self._metadata(self.db), before)

    def test_hardlinked_database_is_refused_without_unlinking_alias(self):
        alias = self.base / "db-alias"
        os.link(self.db, alias)
        before = self._metadata(self.db)
        with self.assertRaisesRegex(ValueError, "bridge_authority_unsafe"):
            with closing(self._view().connect()):
                pass
        self.assertEqual(self._metadata(self.db), before)
        self.assertEqual(alias.stat().st_ino, self.db.stat().st_ino)

    def test_old_or_unsupported_schema_is_refused_without_migration(self):
        for version in (0, 1, 2, 3, 5):
            with self.subTest(version=version):
                with closing(sqlite3.connect(str(self.db))) as conn:
                    conn.execute("PRAGMA user_version=" + str(version))
                before = self._database_snapshot()
                with self.assertRaisesRegex(ValueError, "bridge_authority_schema"):
                    with closing(self._view().connect()):
                        pass
                self.assertEqual(self._database_snapshot(), before)
        with closing(sqlite3.connect(str(self.db))) as conn:
            conn.execute("PRAGMA user_version=3")
        Orchestrator(self.home)
        self.assertEqual(self._database_snapshot()[0], 4)

    def test_incomplete_current_schema_is_refused_without_initialization(self):
        with closing(sqlite3.connect(str(self.db))) as conn:
            conn.execute("ALTER TABLE runs RENAME TO old_runs")
            conn.execute("CREATE TABLE runs(run_id TEXT, task_id TEXT)")
        before = self._database_snapshot()
        with self.assertRaisesRegex(ValueError, "bridge_authority_schema"):
            with closing(self._view().connect()):
                pass
        self.assertEqual(self._database_snapshot(), before)

    def test_invalid_database_refusal_does_not_forward_sqlite_text(self):
        # Finish fixture-owned WAL writes before corrupting the main database.
        with closing(sqlite3.connect(str(self.db))) as conn:
            self.assertEqual(
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(),
                (0, 0, 0),
            )
        self.db.write_bytes(b"fixture is not a database")
        with self.assertRaisesRegex(ValueError, "^bridge_authority_schema$"):
            with closing(self._view().connect()):
                pass
        self.assertEqual(self.db.read_bytes(), b"fixture is not a database")

    def test_sql_writes_attach_functions_and_reconfiguration_are_denied(self):
        before = self._database_snapshot()
        output = self.base / "forbidden.db"
        statements = (
            "INSERT INTO events(ts,kind,payload_json) VALUES('x','x','{}')",
            "UPDATE runs SET state='ABORTED'",
            "DELETE FROM tasks", "CREATE TABLE unauthorized(value TEXT)",
            "DROP TABLE tasks", "ALTER TABLE runs ADD COLUMN bad TEXT",
            "ATTACH DATABASE '" + str(output) + "' AS extra", "DETACH DATABASE main",
            "PRAGMA query_only=OFF", "PRAGMA user_version=3",
            "PRAGMA journal_mode=WAL", "PRAGMA writable_schema=ON",
            "PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA foreign_keys=ON",
            "SELECT load_extension('no-such-extension')",
            "SELECT payload_json FROM events", "SELECT heartbeat_at FROM runs",
        )
        with closing(self._view().connect()) as conn:
            for statement in statements:
                with self.subTest(statement=statement.split()[0:3]):
                    with self.assertRaises(sqlite3.DatabaseError):
                        conn.execute(statement).fetchall()
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
        self.assertFalse(output.exists())
        self.assertEqual(self._database_snapshot(), before)

    def test_no_native_mutating_api_is_exposed(self):
        view = self._view()
        self.assertNotIsInstance(view, Orchestrator)
        for name in ("claim", "heartbeat", "submit", "quiesce", "abort", "publish",
                     "load_plan", "_initialize", "status", "context"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(view, name))


class ReadOnlyAuthorityCompatibilityTests(unittest.TestCase):
    _view = ReadOnlyAuthorityLedgerTests._view
    _metadata = ReadOnlyAuthorityLedgerTests._metadata
    _database_snapshot = ReadOnlyAuthorityLedgerTests._database_snapshot

    def setUp(self):
        ReadOnlyAuthorityLedgerTests.setUp(self)
        self.repo = self.base / "repo"
        self._git("init", "-q", "-b", "main", str(self.repo))
        self._git("-C", str(self.repo), "config", "user.name", "Fixture")
        self._git("-C", str(self.repo), "config", "user.email", "fixture@example.invalid")
        (self.repo / "result.txt").write_bytes(b"native-read\x00\xff\n")
        (self.repo / "secret.txt").write_bytes(b"out-of-scope\n")
        self._git("-C", str(self.repo), "add", ".")
        self._git("-C", str(self.repo), "commit", "-qm", "fixture")
        self.project = ProjectRegistry(self.home).add(
            self.repo, name="Readonly fixture", profile="safe", review_mode="off",
            allow_commit=True, allow_push=False,
        )["project"]
        plan = build_single_task_plan(
            self.project, task_id="READONLY-AUTHORITY-TEST", goal="Fixture",
            allowed_paths=["result.txt"], plan_revision="readonly-fixture",
        )
        plan_path = self.base / "plan.json"
        write_plan(plan_path, plan)
        self.orch.load_plan(plan_path)
        claim = self.orch.claim("fixture", project_id=self.project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED")
        self.cap = Path(claim["capability_file"])
        self.binding = WorkerRunBinding(
            project_id=self.project["project_id"], writer_key=self.project["writer_key"],
            task_id=claim["task_id"], run_id=claim["run_id"], attempt=claim["attempt"],
        )

    def _git(self, *args):
        env = dict(os.environ)
        env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              capture_output=True, text=True, env=env,
                              check=True, timeout=12)

    def test_capability_validation_matches_native_path_and_bounded_reader(self):
        view = self._view()
        run = self.binding.run_id
        self.assertEqual(view.lease_from_capability(run, self.cap),
                         self.orch.lease_from_capability(run, self.cap))
        wrong = self.base / "wrong.json"
        for path in (wrong, self.cap.parent / "other.json"):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ValueError, "^invalid_capability_file$"):
                    view.lease_from_capability(run, path)
        for bad_run in ("../other", "", "bad/run"):
            with self.subTest(run=bad_run):
                with self.assertRaisesRegex(ValueError, "^invalid_run_id$"):
                    view.lease_from_capability(bad_run, self.cap)
        original = self.cap.read_bytes()
        for raw, mode, expected in (
            (original, 0o644, "invalid_capability_mode"),
            (b"{}", 0o600, "capability_identity_mismatch"),
            (b"{", 0o600, "capability_file_invalid_json"),
            (b" " * 65537, 0o600, "capability_file_too_large"),
        ):
            with self.subTest(expected=expected):
                self.cap.write_bytes(raw)
                self.cap.chmod(mode)
                with self.assertRaisesRegex(ValueError, "^" + expected + "$"):
                    view.lease_from_capability(run, self.cap)
                self.assertEqual(self.cap.stat().st_mode & 0o777, mode)
        self.cap.write_bytes(original)
        self.cap.chmod(0o600)
        saved = self.cap.with_suffix(".saved")
        self.cap.rename(saved)
        self.cap.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, "^invalid_capability_file$"):
            view.lease_from_capability(run, self.cap)

    def test_unchanged_authority_and_scope_read_without_durable_state_change(self):
        view = self._view()
        before = self._database_snapshot()
        cap_before = self.cap.read_bytes(), self._metadata(self.cap)
        authority = LocalRunSessionAuthority(view)
        sid = authority.open_session(self.binding, self.cap)
        authority.require_session(sid, self.binding, self.cap)
        with LocalFileScope(authority, sid, self.binding, self.cap) as scope:
            request = WorkerOperationRequest(
                binding=self.binding, operation_id="readonly-read", kind="file.read",
                target="result.txt",
            )
            with scope.open_readonly(request) as fd:
                self.assertEqual(os.read(fd, 64), b"native-read\x00\xff\n")
            denied = WorkerOperationRequest(
                binding=self.binding, operation_id="readonly-denied", kind="file.read",
                target="secret.txt",
            )
            with self.assertRaisesRegex(ValueError, "bridge_file_scope_path_denied"):
                with scope.open_readonly(denied):
                    pass
        authority.close_session(sid)
        self.assertEqual(self._database_snapshot(), before)
        self.assertEqual((self.cap.read_bytes(), self._metadata(self.cap)), cap_before)

    def test_later_committed_wal_state_and_lease_revocation_are_visible(self):
        # Keep the native writer connection live to ensure the updates stay in WAL.
        with closing(self.orch.connect()) as writer:
            view = self._view()
            authority = LocalRunSessionAuthority(view)
            sid = authority.open_session(self.binding, self.cap)
            with closing(view.connect()) as reader:
                writer.execute("UPDATE runs SET state='RESULT_SUBMITTED' WHERE run_id=?",
                               (self.binding.run_id,))
                self.assertEqual(reader.execute(
                    "SELECT state FROM runs WHERE run_id=?", (self.binding.run_id,),
                ).fetchone()["state"], "RESULT_SUBMITTED")
                with self.assertRaisesRegex(ValueError, "bridge_run_not_active"):
                    authority.require_session(sid, self.binding, self.cap)
                writer.execute("UPDATE runs SET state='RUNNING', lease_token=? WHERE run_id=?",
                               ("revoked-fixture-lease", self.binding.run_id))
                with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
                    authority.require_session(sid, self.binding, self.cap)

    def test_current_branch_and_head_remain_authoritative(self):
        authority = LocalRunSessionAuthority(self._view())
        sid = authority.open_session(self.binding, self.cap)
        self._git("-C", str(self.repo), "switch", "-q", "-c", "drift")
        with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
            authority.require_session(sid, self.binding, self.cap)
        self._git("-C", str(self.repo), "switch", "-q", "main")
        (self.repo / "result.txt").write_text("new head\n")
        self._git("-C", str(self.repo), "add", "result.txt")
        self._git("-C", str(self.repo), "commit", "-qm", "drift")
        with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
            authority.require_session(sid, self.binding, self.cap)


if __name__ == "__main__":
    unittest.main()
