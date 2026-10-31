"""Restricted existing-home SQLite access for the process journal only.

This trusted-local seam does not initialize, migrate, repair permissions, or
expose native lifecycle operations. It is not a fence against the same user.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .bridge_readonly_authority import ReadOnlyAuthorityLedger
from .core import STATE_SCHEMA_VERSION

_COLUMNS = {
    "runs": frozenset({"run_id", "task_id", "attempt", "state"}),
    "tasks": frozenset({"task_id", "project_id", "writer_key", "status"}),
    "events": frozenset({"seq", "ts", "kind", "task_id", "run_id", "payload_json"}),
    "sqlite_master": frozenset({"name", "type"}),
}
_INSERT = "INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES(?,?,?,?,?)"
_KINDS = frozenset({"PROCESS_DISPATCH_INTENT", "PROCESS_EXIT_OBSERVED"})


def _authorizer(connection, action, first, second, database, source):
    if source is not None:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and database == "main" and second in _COLUMNS.get(first, ()):
        return sqlite3.SQLITE_OK
    if (action == sqlite3.SQLITE_INSERT and database == "main" and first == "events"
            and connection._allow_process_insert):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and second in {"max", "json_valid", "json_extract", "coalesce"}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_TRANSACTION and first in {"BEGIN", "COMMIT", "ROLLBACK"}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        if first == "synchronous" and second in (None, "FULL"):
            return sqlite3.SQLITE_OK
        if first == "user_version" and second is None:
            return sqlite3.SQLITE_OK
        if first == "table_info" and second in {"runs", "tasks", "events"}:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class _Connection(sqlite3.Connection):
    _allow_process_insert = False

    def execute(self, sql, parameters=()):
        # The authorizer cannot inspect values. Permit only the exact append
        # statement with a process kind, and deny cursor/executescript bypass.
        is_insert = sql == _INSERT
        if is_insert and (not isinstance(parameters, (tuple, list)) or len(parameters) != 5
                          or parameters[1] not in _KINDS):
            raise sqlite3.DatabaseError("process_ledger_insert_denied")
        self._ledger.check_identity()
        self._allow_process_insert = is_insert
        try:
            result = super().execute(sql, parameters)
        finally:
            self._allow_process_insert = False
        self._ledger.check_identity()
        return result

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class ExistingProcessLedger:
    """Pinned private existing home, with narrowly authorized connections."""
    def __init__(self, root: Path):
        self._inspection = ReadOnlyAuthorityLedger(root)
        self.root = self._inspection.root
        self.runtime = self._inspection.runtime
        self.db_path = self._inspection.db_path
        self._identity = self._inspection._inspect_state()
        with self.connect():
            pass

    def check_identity(self):
        if self._inspection._inspect_state() != self._identity:
            raise ValueError("process_ledger_identity_changed")

    def matches(self, authority):
        self.check_identity()
        try:
            return (Path(authority.root) == self.root
                    and Path(authority.runtime) == self.runtime
                    and Path(authority.db_path) == self.db_path
                    and ReadOnlyAuthorityLedger(authority.root)._inspect_state() == self._identity)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def connect(self):
        self.check_identity()
        conn = None
        try:
            conn = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True,
                                   timeout=5, isolation_level=None, factory=_Connection,
                                   cached_statements=0)
            conn._ledger = self
            conn.row_factory = sqlite3.Row
            conn.set_authorizer(lambda *args: _authorizer(conn, *args))
            if conn.execute("PRAGMA user_version").fetchone()[0] != STATE_SCHEMA_VERSION:
                raise ValueError("process_ledger_schema")
            rows = conn.execute("SELECT name,type FROM sqlite_master WHERE name IN ('runs','tasks','events')").fetchall()
            if {r["name"]: r["type"] for r in rows} != {name: "table" for name in ("runs", "tasks", "events")}:
                raise ValueError("process_ledger_schema")
            for table in ("runs", "tasks", "events"):
                columns = {r["name"] for r in conn.execute("PRAGMA table_info(" + table + ")")}
                if not _COLUMNS[table].issubset(columns):
                    raise ValueError("process_ledger_schema")
            self.check_identity()
            return conn
        except BaseException:
            if conn is not None:
                conn.close()
            raise
