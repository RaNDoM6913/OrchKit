"""Existing private native authority, opened without initialization or repair.

SQLite may create or use WAL shared-memory sidecars and locks while reading.
This view preserves durable authority and pre-existing permissions; it is not
an OS fence against other processes running as the same local user.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
from typing import Optional

from .core import Orchestrator, STATE_SCHEMA_VERSION


_AUTHORITY_COLUMNS = {
    "runs": frozenset({
        "run_id", "task_id", "attempt", "state", "lease_token", "claim_git_head",
    }),
    "tasks": frozenset({
        "task_id", "project_id", "writer_key", "status", "payload_json",
    }),
}


def _readonly_authorizer(action, first, second, database, source):
    """Admit only the native authority projections and their schema checks."""
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and database == "main" and source is None:
        allowed = _AUTHORITY_COLUMNS.get(first, frozenset())
        if first == "sqlite_master":
            allowed = frozenset({"name", "type"})
        if second in allowed:
            return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and second == "max":
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        if first in {"user_version", "query_only"} and second is None:
            return sqlite3.SQLITE_OK
        if first == "table_info" and second in _AUTHORITY_COLUMNS:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _private_metadata(path: Path, *, directory: bool = False,
                      optional: bool = False) -> Optional[os.stat_result]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        if optional:
            return None
        raise ValueError("bridge_authority_missing") from exc
    except OSError as exc:
        raise ValueError("bridge_authority_unsafe") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    expected_mode = 0o700 if directory else 0o600
    if (not expected_type(info.st_mode)
            or stat.S_IMODE(info.st_mode) != expected_mode
            or info.st_uid != os.getuid()
            or (not directory and info.st_nlink != 1)):
        raise ValueError("bridge_authority_unsafe")
    return info


class _ReadOnlyConnection(sqlite3.Connection):
    """Close the fresh connection when unchanged authority contexts unwind."""

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class ReadOnlyAuthorityLedger:
    """The three-member ledger interface required by local Bridge authority.

    ``runtime`` and ``db_path`` support the reused pure capability helpers.
    No native lifecycle, migration, permission-repair or publication API is
    inherited. Each connection checks the existing state anew.
    """

    def __init__(self, root: Path):
        supplied = Path(os.path.abspath(os.path.expanduser(str(root))))
        # Check before resolve: resolving a supplied symlink would hide it.
        _private_metadata(supplied, directory=True)
        self.root = supplied.resolve()
        self.runtime = self.root / ".runtime"
        self.db_path = self.runtime / "orch.sqlite3"
        self._inspect_state()

    def _inspect_state(self):
        identities = []
        for path, directory in ((self.root, True), (self.runtime, True),
                                (self.db_path, False)):
            info = _private_metadata(path, directory=directory)
            identities.append((info.st_dev, info.st_ino))
        for suffix in ("-wal", "-shm"):
            _private_metadata(self.db_path.with_name(self.db_path.name + suffix),
                              optional=True)
        return tuple(identities)

    def connect(self) -> sqlite3.Connection:
        before = self._inspect_state()
        conn = None
        try:
            # Path.as_uri escapes reserved URI characters. mode=ro refuses a
            # missing DB; immutable is deliberately absent so WAL stays live.
            conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True,
                                   timeout=5, isolation_level=None,
                                   factory=_ReadOnlyConnection)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.set_authorizer(_readonly_authorizer)
            if conn.execute("PRAGMA user_version").fetchone()[0] != STATE_SCHEMA_VERSION:
                raise ValueError("bridge_authority_schema")
            tables = conn.execute(
                "SELECT name,type FROM sqlite_master WHERE name IN ('runs','tasks')"
            ).fetchall()
            if {row["name"]: row["type"] for row in tables} != {
                    "runs": "table", "tasks": "table"}:
                raise ValueError("bridge_authority_schema")
            for table, required in _AUTHORITY_COLUMNS.items():
                columns = {row["name"] for row in
                           conn.execute("PRAGMA table_info(" + table + ")")}
                if not required.issubset(columns):
                    raise ValueError("bridge_authority_schema")
            if self._inspect_state() != before:
                raise ValueError("bridge_authority_unsafe")
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise ValueError("bridge_authority_schema") from exc
        except Exception:
            if conn is not None:
                conn.close()
            raise

    def _capability_path(self, run_id: str) -> Path:
        return Orchestrator._capability_path(self, run_id)

    def lease_from_capability(self, run_id: str, capability_file: Path) -> str:
        return Orchestrator.lease_from_capability(self, run_id, capability_file)
