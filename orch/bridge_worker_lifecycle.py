"""Opt-in, narrowly scoped local claim/context/checkpoint authority.

Only trusted local code constructs authorities and admissions. Route and session
identifiers are routing aids, never substitutes for native capability checks.
The SQLite transaction cannot fence other processes running as the same user.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import uuid
from typing import Literal

from .bridge_authorization import LocalRunSessionAuthority
from .bridge_local_protocol import open_private_directory
from .bridge_readonly_authority import ReadOnlyAuthorityLedger
from .core import Orchestrator, READY_TASK_STATES, STATE_SCHEMA_VERSION, WRITER_LOCK_RUN_STATES, _begin_immediate, canonical_json, utc_now
from .worker_operations import WorkerRunBinding

_GRANT_PREFIX = 'b10:v1:grant:'
_OP_PREFIX = 'b10:v1:op:'
_ID = re.compile(r'[A-Za-z0-9._-]{1,128}\Z', re.ASCII)
_GRANT_ID = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_SHA = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_RECORD_MAX = 4096
_GRANT_MAX = 4096
_CHECKPOINT_MAX = 32


def _id(value: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError('lifecycle_invalid_operation_id')
    return value


def _grant_id(value: str) -> str:
    if not isinstance(value, str) or _GRANT_ID.fullmatch(value) is None:
        raise ValueError('lifecycle_invalid_grant')
    return value


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class LifecycleGrantSpec:
    project_id: str
    writer_key: str
    task_id: str
    plan_revision: str
    payload_sha256: str
    attempt: int
    worker_id: str

    def __post_init__(self):
        for value in (self.project_id, self.writer_key, self.task_id, self.plan_revision, self.worker_id):
            if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 200:
                raise ValueError('lifecycle_invalid_spec')
        if (not isinstance(self.payload_sha256, str) or _SHA.fullmatch(self.payload_sha256) is None
                or type(self.attempt) is not int or not 1 <= self.attempt <= 20):
            raise ValueError('lifecycle_invalid_spec')
        # Native run admission accepts only its own canonical identifiers.
        WorkerRunBinding(self.project_id, self.writer_key, self.task_id, 'validation', self.attempt)
        if re.fullmatch(r'[A-Za-z0-9._-]{1,200}', self.plan_revision, re.ASCII) is None:
            raise ValueError('lifecycle_invalid_spec')
        if re.fullmatch(r'[A-Za-z0-9._-]{1,200}', self.worker_id, re.ASCII) is None:
            raise ValueError('lifecycle_invalid_spec')


@dataclass(frozen=True)
class LifecycleAdmission:
    grant_id: str
    credential_file: Path

    def __post_init__(self):
        _grant_id(self.grant_id)
        if not isinstance(self.credential_file, Path):
            raise ValueError('lifecycle_invalid_admission')


@dataclass(frozen=True)
class LifecycleResult:
    state: Literal['SUCCEEDED', 'REFUSED', 'UNKNOWN']
    code: str
    operation_id: str | None
    run_id: str | None
    attempt: int | None


def _load(conn, key, kind):
    row = conn.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()
    if row is None:
        return None
    raw = row['value_json']
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > _RECORD_MAX:
            raise ValueError()
        def unique(pairs):
            result = {}
            for k, v in pairs:
                if k in result:
                    raise ValueError()
                result[k] = v
            return result
        obj = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(obj, dict) or type(obj.get('version')) is not int
                or obj['version'] != 1):
            raise ValueError()
        if kind == 'grant':
            if set(obj) != {'version', 'grant_id', 'spec', 'status', 'claim_id', 'checkpoint_ids'}:
                raise ValueError()
            _grant_id(obj['grant_id'])
            if key != _GRANT_PREFIX + obj['grant_id'] or obj['status'] not in {'ISSUED', 'REVOKED', 'RESERVED', 'CONSUMED'}:
                raise ValueError()
            if not isinstance(obj['spec'], dict):
                raise ValueError()
            LifecycleGrantSpec(**obj['spec'])
            anchors = obj['checkpoint_ids']
            if (type(anchors) is not list or len(anchors) > _CHECKPOINT_MAX
                    or any(not isinstance(item, str) or _SHA.fullmatch(item) is None for item in anchors)
                    or len(set(anchors)) != len(anchors)
                    or (anchors and obj['status'] != 'CONSUMED')):
                raise ValueError()
            if obj['status'] in {'RESERVED', 'CONSUMED'}:
                _id(obj['claim_id'])
            elif obj['claim_id'] is not None:
                raise ValueError()
        else:
            if set(obj) != {'version', 'grant_id', 'operation_id', 'kind', 'fingerprint', 'phase', 'run_id', 'attempt', 'code'}:
                raise ValueError()
            _grant_id(obj['grant_id']); _id(obj['operation_id'])
            if key != _op_key(obj['grant_id'], obj['operation_id']):
                raise ValueError()
            if not isinstance(obj['fingerprint'], str) or _SHA.fullmatch(obj['fingerprint']) is None:
                raise ValueError()
            if obj['kind'] not in {'claim', 'checkpoint'} or obj['phase'] not in {'PREPARED', 'PENDING', 'READY', 'REFUSED', 'DONE'}:
                raise ValueError()
            if (obj['kind'] == 'claim' and obj['phase'] == 'DONE') or (obj['kind'] == 'checkpoint' and obj['phase'] not in {'PREPARED', 'DONE'}):
                raise ValueError()
            if not isinstance(obj['code'], str) or _ID.fullmatch(obj['code']) is None:
                raise ValueError()
            if obj['phase'] == 'REFUSED' or (obj['kind'] == 'claim' and obj['phase'] == 'PREPARED'):
                if obj['run_id'] is not None or obj['attempt'] is not None:
                    raise ValueError()
            elif (not isinstance(obj['run_id'], str) or not obj['run_id'] or len(obj['run_id']) > 200
                  or type(obj['attempt']) is not int or not 1 <= obj['attempt'] <= 20):
                raise ValueError()
        return obj
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError('lifecycle_record_invalid') from None


def _store(conn, key, obj, *, insert=False):
    raw = canonical_json(obj)
    if len(raw.encode('utf-8')) > _RECORD_MAX:
        raise ValueError('lifecycle_record_too_large')
    if insert:
        conn.execute('INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)', (key, raw, utc_now()))
    else:
        cursor = conn.execute('UPDATE settings SET value_json=?,updated_at=? WHERE key=?', (raw, utc_now(), key))
        if cursor.rowcount != 1:
            raise ValueError('lifecycle_record_missing')


def _op_key(grant_id, operation_id):
    return _OP_PREFIX + grant_id + ':' + operation_id


def _result(record):
    state = {'PREPARED': 'UNKNOWN', 'PENDING': 'UNKNOWN', 'READY': 'SUCCEEDED', 'REFUSED': 'REFUSED', 'DONE': 'SUCCEEDED'}[record['phase']]
    return LifecycleResult(state, record['code'], record['operation_id'], record['run_id'], record['attempt'])


def _unknown(op=None):
    return LifecycleResult('UNKNOWN', 'lifecycle_unknown', op, None, None)


def _refused(op, code='lifecycle_refused'):
    return LifecycleResult('REFUSED', code, op, None, None)


def _record(grant_id, op, kind, fingerprint, phase, run_id=None, attempt=None, code='lifecycle_unknown'):
    return dict(version=1, grant_id=grant_id, operation_id=op, kind=kind,
                fingerprint=fingerprint, phase=phase, run_id=run_id, attempt=attempt, code=code)


def _matches(row, spec):
    if row is None:
        return False
    try:
        return (row['task_id'] == spec.task_id and row['project_id'] == spec.project_id
                and row['writer_key'] == spec.writer_key and row['plan_revision'] == spec.plan_revision
                and _digest(json.loads(row['payload_json'])) == spec.payload_sha256)
    except (ValueError, TypeError, KeyError, UnicodeError):
        return False


def _publish_capability(orch, run_id, lease):
    """Exclusive creation: an existing or uncertain artifact is never adopted."""
    directory = orch.runtime / 'claims'
    parent = open_private_directory(directory)
    fd = -1
    try:
        name = orch._capability_path(run_id).name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        data = canonical_json({'run_id': run_id, 'lease_token': lease}).encode('utf-8')
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError('short capability write')
            view = view[count:]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        info = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError('lifecycle_capability_changed')
        os.fsync(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


class _LifecycleConnection(sqlite3.Connection):
    """Transactions close their own descriptor, including on exceptions."""
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class _LifecycleOrchestrator(Orchestrator):
    """Private existing-state writer. Never initialize, migrate, or repair."""
    def __init__(self, ledger: ReadOnlyAuthorityLedger):
        self.root = ledger.root
        self.runtime = ledger.runtime
        self.db_path = ledger.db_path
        self.logs = self.runtime / 'logs'
        self.__ledger = ledger
        with self.connect():
            pass

    def _disabled_git_hooks_path(self) -> Path:
        directory = self.runtime / 'git-hooks-disabled'
        fd = open_private_directory(directory)
        try:
            if os.listdir(fd):
                raise ValueError('git_hooks_guard_not_empty')
        finally:
            os.close(fd)
        return directory

    def _receipt_path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or re.fullmatch(r'[A-Za-z0-9._-]+', run_id, re.ASCII) is None:
            raise ValueError('lifecycle_invalid_run')
        directory = self.runtime / 'worker_receipts'
        fd = open_private_directory(directory)
        os.close(fd)
        return directory / (run_id + '.json')

    def connect(self) -> sqlite3.Connection:
        # The metadata check is point-in-time, not an OS fence against same-user
        # replacement; match the existing read-only authority's assumptions.
        before = self.__ledger._inspect_state()
        conn = None
        try:
            conn = sqlite3.connect(self.db_path.as_uri() + '?mode=rw', uri=True,
                                   timeout=5, isolation_level=None, factory=_LifecycleConnection)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                raise ValueError('lifecycle_ledger_journal')
            try:
                conn.execute('PRAGMA synchronous=FULL')
                synchronous = conn.execute('PRAGMA synchronous').fetchone()[0]
                if type(synchronous) is not int or synchronous != 2:
                    raise ValueError('lifecycle_ledger_synchronous')
            except Exception:
                raise ValueError('lifecycle_ledger_synchronous') from None
            if conn.execute('PRAGMA user_version').fetchone()[0] != STATE_SCHEMA_VERSION:
                raise ValueError('lifecycle_ledger_schema')
            tables = conn.execute("SELECT name,type FROM sqlite_master WHERE name IN ('runs','tasks','settings','events')").fetchall()
            if {row['name']: row['type'] for row in tables} != {
                    'runs': 'table', 'tasks': 'table', 'settings': 'table', 'events': 'table'}:
                raise ValueError('lifecycle_ledger_schema')
            settings = {row['name']: row for row in conn.execute('PRAGMA table_info(settings)')}
            if (not {'key', 'value_json', 'updated_at'}.issubset(settings)
                    or settings['key']['pk'] != 1):
                raise ValueError('lifecycle_ledger_schema')
            if self.__ledger._inspect_state() != before:
                raise ValueError('lifecycle_ledger_changed')
            return conn
        except Exception:
            if conn is not None:
                conn.close()
            raise


class LifecycleAuthority:
    """Trusted local factory; the worker-facing route exposes only B10 operations."""
    def __init__(self, root: Path):
        # Validate existing metadata before the native writer adapter is opened.
        self.__ledger = ReadOnlyAuthorityLedger(Path(root))
        with self.__ledger.connect():
            pass  # Enforce the existing schema before opening any writer.
        self.__orch = _LifecycleOrchestrator(self.__ledger)

    def create_grant(self, spec: LifecycleGrantSpec) -> str:
        if type(spec) is not LifecycleGrantSpec:
            raise ValueError('lifecycle_invalid_spec')
        grant_id = secrets.token_hex(16)
        with self.__orch.connect() as conn:
            _begin_immediate(conn)
            count = conn.execute('SELECT COUNT(*) FROM settings WHERE key >= ? AND key < ?',
                                 (_GRANT_PREFIX, _GRANT_PREFIX + '\uffff')).fetchone()[0]
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (spec.task_id,)).fetchone()
            attempts = conn.execute('SELECT COUNT(*) FROM runs WHERE task_id=?', (spec.task_id,)).fetchone()[0]
            if count >= _GRANT_MAX or not _matches(row, spec) or row['status'] not in READY_TASK_STATES or attempts + 1 != spec.attempt:
                conn.execute('ROLLBACK')
                raise ValueError('lifecycle_grant_denied')
            grant = dict(version=1, grant_id=grant_id, spec=asdict(spec), status='ISSUED',
                         claim_id=None, checkpoint_ids=[])
            _store(conn, _GRANT_PREFIX + grant_id, grant, insert=True)
            conn.execute('COMMIT')
        return grant_id

    def revoke_grant(self, grant_id: str) -> None:
        _grant_id(grant_id)
        with self.__orch.connect() as conn:
            _begin_immediate(conn)
            grant = _load(conn, _GRANT_PREFIX + grant_id, 'grant')
            if grant is None or grant['status'] != 'ISSUED':
                conn.execute('ROLLBACK')
                raise ValueError('lifecycle_grant_denied')
            grant['status'] = 'REVOKED'
            _store(conn, _GRANT_PREFIX + grant_id, grant)
            conn.execute('COMMIT')

    def open_route(self, grant_id: str) -> LifecycleRoute:
        _grant_id(grant_id)
        route = LifecycleRoute(self.__orch, self.__ledger, grant_id)
        route._admit()
        return route


class LifecycleRoute:
    def __init__(self, orch, ledger, grant_id):
        self.__orch = orch
        self.__ledger = ledger
        self.__grant_id = grant_id
        self.__auth = LocalRunSessionAuthority(ledger)
        self.__native_session = None
        self.__binding = None
        self.__closed = False
        self.session_id = secrets.token_urlsafe(24)

    def __grant(self, conn):
        grant = _load(conn, _GRANT_PREFIX + self.__grant_id, 'grant')
        if grant is None or grant['status'] == 'REVOKED':
            raise ValueError('lifecycle_grant_denied')
        return grant

    def _admit(self):
        with self.__orch.connect() as conn:
            grant = self.__grant(conn)
            if grant['status'] != 'CONSUMED':
                return
            record = _load(conn, _op_key(self.__grant_id, grant['claim_id']), 'operation')
            if record is None or record['kind'] != 'claim':
                raise ValueError('lifecycle_record_invalid')
            spec = LifecycleGrantSpec(**grant['spec'])
            if record['phase'] in {'PENDING', 'READY'} and record['attempt'] != spec.attempt:
                raise ValueError('lifecycle_binding_denied')
            if record['phase'] != 'READY':
                self.status()
                return
            binding = WorkerRunBinding(spec.project_id, spec.writer_key, spec.task_id, record['run_id'], record['attempt'])
        cap = self.__orch._capability_path(binding.run_id)
        native_session = self.__auth.open_session(binding, cap)
        self.__binding, self.__native_session = binding, native_session
        try:
            self.status()
        except Exception:
            self.__auth.close_session(native_session)
            self.__binding = self.__native_session = None
            raise

    def status(self) -> None:
        if self.__closed:
            raise ValueError('lifecycle_route_closed')
        with self.__orch.connect() as conn:
            grant = self.__grant(conn)
            if grant['status'] in {'RESERVED', 'CONSUMED'}:
                claim = _load(conn, _op_key(self.__grant_id, grant['claim_id']), 'operation')
                if (claim is None or claim['kind'] != 'claim'
                        or (grant['status'] == 'RESERVED' and claim['phase'] != 'PREPARED')
                        or (grant['status'] == 'CONSUMED' and claim['phase'] == 'PREPARED')):
                    raise ValueError('lifecycle_record_invalid')
                spec = LifecycleGrantSpec(**grant['spec'])
                if claim['phase'] in {'PENDING', 'READY'}:
                    run = conn.execute('SELECT task_id,worker_id,attempt FROM runs WHERE run_id=?',
                                       (claim['run_id'],)).fetchone()
                    if (claim['attempt'] != spec.attempt or run is None
                            or run['task_id'] != spec.task_id or run['worker_id'] != spec.worker_id
                            or run['attempt'] != spec.attempt):
                        raise ValueError('lifecycle_binding_denied')
                if self.__binding is None:
                    if claim['phase'] == 'READY':
                        raise ValueError('lifecycle_readmission_required')
                    return
                if (claim['phase'] != 'READY' or claim['run_id'] != self.__binding.run_id
                        or claim['attempt'] != self.__binding.attempt
                        or self.__binding.attempt != spec.attempt):
                    raise ValueError('lifecycle_binding_denied')
                row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (spec.task_id,)).fetchone()
                if not _matches(row, spec):
                    raise ValueError('lifecycle_binding_denied')
            elif self.__binding is not None:
                raise ValueError('lifecycle_binding_denied')
        if self.__binding is not None:
            self.__auth.require_session(self.__native_session, self.__binding, self.__orch._capability_path(self.__binding.run_id))

    def close(self) -> None:
        if not self.__closed:
            if self.__native_session is not None:
                self.__auth.close_session(self.__native_session)
            self.__closed = True

    def claim(self, operation_id: str) -> LifecycleResult:
        op = _id(operation_id)
        try:
            self.status()
            result = self.__claim(op)
            if result.state == 'SUCCEEDED':
                self.status()
            return result
        except Exception:
            return _unknown(op)

    def __claim(self, op):
        fingerprint = _digest({'kind': 'claim'})
        with self.__orch.connect() as conn:
            _begin_immediate(conn)
            grant = self.__grant(conn)
            previous = _load(conn, _op_key(self.__grant_id, op), 'operation')
            if previous is not None:
                conn.execute('COMMIT')
                if previous['kind'] != 'claim' or previous['fingerprint'] != fingerprint:
                    return _refused(op, 'lifecycle_conflict')
                if grant['status'] not in {'RESERVED', 'CONSUMED'} or grant['claim_id'] != op:
                    return _unknown(op)
                return _result(previous)
            if grant['status'] != 'ISSUED':
                conn.execute('COMMIT')
                return _refused(op, 'lifecycle_consumed')
            # Reserve an operation durably before dispatch. A crash or exception
            # after this point cannot turn an unknown request into a replay.
            grant['status'], grant['claim_id'] = 'RESERVED', op
            prepared = _record(self.__grant_id, op, 'claim', fingerprint, 'PREPARED')
            _store(conn, _GRANT_PREFIX + self.__grant_id, grant)
            _store(conn, _op_key(self.__grant_id, op), prepared, insert=True)
            conn.execute('COMMIT')
        with self.__orch.connect() as conn:
            _begin_immediate(conn)
            grant = self.__grant(conn)
            existing = _load(conn, _op_key(self.__grant_id, op), 'operation')
            if grant['status'] != 'RESERVED' or grant['claim_id'] != op or existing != prepared:
                raise ValueError('lifecycle_record_changed')
            spec = LifecycleGrantSpec(**grant['spec'])
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (spec.task_id,)).fetchone()
            attempts = conn.execute('SELECT COUNT(*) FROM runs WHERE task_id=?', (spec.task_id,)).fetchone()[0]
            refusal = None
            if (not _matches(row, spec) or row['status'] not in READY_TASK_STATES
                    or attempts + 1 != spec.attempt):
                refusal = 'lifecycle_stale'
            elif conn.execute("SELECT 1 FROM settings WHERE key='paused'").fetchone():
                refusal = 'lifecycle_paused'
            elif spec.project_id in self.__orch._project_pauses(conn):
                refusal = 'lifecycle_paused'
            else:
                payload = self.__orch._task_payload(row)
                if (not self.__orch._dependencies_done(conn, payload)
                        or attempts >= int(payload.get('max_attempts', 2))
                        or spec.writer_key in self.__orch._active_writer_locks(conn)):
                    refusal = 'lifecycle_blocked'
            run_id = f'{spec.task_id}-A{spec.attempt}-{uuid.uuid4().hex[:10]}'
            if refusal is None:
                try:
                    if (payload.get('publication') or {}).get('kind') in {'git', 'git_local'}:
                        head, branch = self.__orch._claim_git_state(payload)
                        guard = self.__orch._claim_workspace_guard(payload, head)
                    else:
                        guard = self.__orch._claim_workspace_guard(payload, None)
                        head, branch = (None, None)
                        if guard['status'] == 'PASS':
                            head, branch = self.__orch._claim_git_state(payload, git_scope=guard['scope'])
                    if guard['status'] != 'PASS':
                        raise ValueError('guard')
                    self.__orch._build_context_pack(conn, row, payload, run_id=run_id,
                        attempt=spec.attempt, claim_git_head=head, run_persisted=False)
                    guard = self.__orch._claim_workspace_guard(payload, head)
                    if guard['status'] != 'PASS':
                        raise ValueError('guard')
                    current_head, current_branch = self.__orch._claim_git_state(payload, git_scope=guard['scope'])
                    if current_head != head or current_branch != branch:
                        raise ValueError('git_changed')
                except (ValueError, OSError, KeyError, TypeError):
                    refusal = 'lifecycle_preflight_denied'
            grant['status'], grant['claim_id'] = 'CONSUMED', op
            _store(conn, _GRANT_PREFIX + self.__grant_id, grant)
            if refusal:
                record = _record(self.__grant_id, op, 'claim', fingerprint, 'REFUSED', code=refusal)
                _store(conn, _op_key(self.__grant_id, op), record)
                conn.execute('COMMIT')
                return _result(record)
            lease, now = secrets.token_urlsafe(24), utc_now()
            conn.execute('INSERT INTO runs(run_id,task_id,attempt,worker_id,state,lease_token,started_at,heartbeat_at,claim_git_head) VALUES(?,?,?,?,?,?,?,?,?)',
                         (run_id, spec.task_id, spec.attempt, spec.worker_id, 'RUNNING', lease, now, now, head))
            conn.execute("UPDATE tasks SET status='IN_PROGRESS',updated_at=? WHERE task_id=?", (now, spec.task_id))
            self.__orch._event(conn, 'RUN_CLAIMED', task_id=spec.task_id, run_id=run_id,
                payload=dict(attempt=spec.attempt, worker_id=spec.worker_id, project_id=spec.project_id,
                             writer_key=spec.writer_key, queue_seq=row['queue_seq'], claim_git_head=head, claim_branch=branch))
            record = _record(self.__grant_id, op, 'claim', fingerprint, 'PENDING', run_id, spec.attempt)
            _store(conn, _op_key(self.__grant_id, op), record)
            conn.execute('COMMIT')
        # No exception after the native commit can release the reservation or replay.
        try:
            _publish_capability(self.__orch, run_id, lease)
            binding = WorkerRunBinding(spec.project_id, spec.writer_key, spec.task_id, run_id, spec.attempt)
            cap = self.__orch._capability_path(run_id)
            native_session = self.__auth.open_session(binding, cap)
            self.__auth.require_session(native_session, binding, cap)
            with self.__orch.connect() as conn:
                _begin_immediate(conn)
                current_grant = self.__grant(conn)
                current = _load(conn, _op_key(self.__grant_id, op), 'operation')
                self.__validate_native(conn, spec, binding, cap)
                if current_grant != grant or current != record:
                    raise ValueError('lifecycle_record_changed')
                record['phase'], record['code'] = 'READY', 'lifecycle_claimed'
                _store(conn, _op_key(self.__grant_id, op), record)
                conn.execute('COMMIT')
            self.__binding, self.__native_session = binding, native_session
            self.status()
            return _result(record)
        except Exception:
            if 'native_session' in locals() and self.__native_session is None:
                try:
                    self.__auth.close_session(native_session)
                except Exception:
                    pass
            return _unknown(op)

    def __validate_native(self, conn, spec, binding, cap):
        lease = self.__ledger.lease_from_capability(binding.run_id, cap)
        run = conn.execute('SELECT * FROM runs WHERE run_id=?', (binding.run_id,)).fetchone()
        task = conn.execute('SELECT * FROM tasks WHERE task_id=?', (spec.task_id,)).fetchone()
        latest = conn.execute('SELECT MAX(attempt) FROM runs WHERE task_id=?', (spec.task_id,)).fetchone()[0]
        placeholders = ','.join('?' for _ in WRITER_LOCK_RUN_STATES)
        writers = conn.execute('SELECT r.run_id FROM runs r JOIN tasks t ON t.task_id=r.task_id '
            'WHERE t.writer_key=? AND r.state IN (' + placeholders + ')',
            (spec.writer_key, *sorted(WRITER_LOCK_RUN_STATES))).fetchall()
        if (binding.project_id != spec.project_id or binding.writer_key != spec.writer_key
                or binding.task_id != spec.task_id or binding.attempt != spec.attempt
                or not _matches(task, spec) or task['status'] != 'IN_PROGRESS' or run is None
                or run['state'] != 'RUNNING' or run['task_id'] != spec.task_id
                or run['worker_id'] != spec.worker_id or run['attempt'] != spec.attempt
                or latest != spec.attempt or not secrets.compare_digest(run['lease_token'], lease)
                or len(writers) != 1 or writers[0]['run_id'] != binding.run_id):
            raise ValueError('lifecycle_binding_denied')

    def context(self, max_response_bytes: int) -> dict:
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 65536:
            raise ValueError('lifecycle_invalid_budget')
        self.status()
        if self.__binding is None:
            raise ValueError('lifecycle_not_bound')
        pack = self.__orch.context(self.__binding.run_id)
        if len(canonical_json(pack).encode('utf-8')) > 32768:
            raise ValueError('lifecycle_context_too_large')
        self.status()
        return pack

    def disposition(self, operation_id: str) -> LifecycleResult:
        op = _id(operation_id)
        try:
            self.status()
            with self.__orch.connect() as conn:
                grant = self.__grant(conn)
                record = _load(conn, _op_key(self.__grant_id, op), 'operation')
                if record is None:
                    return _unknown(op)
                if grant['status'] not in {'RESERVED', 'CONSUMED'}:
                    return _unknown(op)
                spec = LifecycleGrantSpec(**grant['spec'])
                if record['attempt'] is not None and record['attempt'] != spec.attempt:
                    return _unknown(op)
                if record['kind'] == 'claim':
                    if grant['claim_id'] != op:
                        return _unknown(op)
                else:
                    if hashlib.sha256(op.encode('ascii')).hexdigest() not in grant['checkpoint_ids']:
                        return _unknown(op)
                    claim = _load(conn, _op_key(self.__grant_id, grant['claim_id']), 'operation')
                    if (claim is None or claim['kind'] != 'claim' or claim['phase'] != 'READY'
                            or record['run_id'] != claim['run_id'] or record['attempt'] != claim['attempt']):
                        return _unknown(op)
                result = _result(record)
            self.status()
            return result
        except Exception:
            return _unknown(op)

    def checkpoint(self, operation_id: str, reason: str, process_state: str) -> LifecycleResult:
        op = _id(operation_id)
        try:
            if (not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500
                    or len(reason.strip().encode('utf-8')) > 2000 or not isinstance(process_state, str)
                    or process_state not in {'active', 'unknown'}):
                return _refused(op, 'lifecycle_invalid_checkpoint')
        except UnicodeError:
            return _refused(op, 'lifecycle_invalid_checkpoint')
        reason = reason.strip()
        fingerprint = _digest({'kind': 'checkpoint', 'reason': reason, 'process_state': process_state})
        try:
            self.status()
            if self.__binding is None:
                return _refused(op, 'lifecycle_not_bound')
            with self.__orch.connect() as conn:
                _begin_immediate(conn)
                grant = self.__grant(conn)
                spec = LifecycleGrantSpec(**grant['spec'])
                claim = _load(conn, _op_key(self.__grant_id, grant['claim_id']), 'operation')
                if claim is None or claim['phase'] != 'READY' or claim['run_id'] != self.__binding.run_id:
                    raise ValueError('lifecycle_binding_denied')
                self.__validate_native(conn, spec, self.__binding, self.__orch._capability_path(self.__binding.run_id))
                previous = _load(conn, _op_key(self.__grant_id, op), 'operation')
                identity = hashlib.sha256(op.encode('ascii')).hexdigest()
                anchored = identity in grant['checkpoint_ids']
                if previous is not None:
                    conn.execute('COMMIT')
                    if previous['kind'] != 'checkpoint':
                        return _refused(op, 'lifecycle_conflict')
                    if not anchored:
                        return _unknown(op)
                    if previous['fingerprint'] != fingerprint:
                        return _refused(op, 'lifecycle_conflict')
                    if previous['run_id'] != self.__binding.run_id or previous['attempt'] != self.__binding.attempt:
                        return _unknown(op)
                    self.status()
                    return _result(previous)
                if anchored:
                    conn.execute('COMMIT')
                    return _unknown(op)
                if len(grant['checkpoint_ids']) >= _CHECKPOINT_MAX:
                    conn.execute('COMMIT')
                    return _refused(op, 'lifecycle_capacity')
                prepared = _record(self.__grant_id, op, 'checkpoint', fingerprint, 'PREPARED',
                                   self.__binding.run_id, spec.attempt)
                grant['checkpoint_ids'].append(identity)
                _store(conn, _GRANT_PREFIX + self.__grant_id, grant)
                _store(conn, _op_key(self.__grant_id, op), prepared, insert=True)
                conn.execute('COMMIT')
            # A prepared operation is never dispatched again after uncertainty.
            with self.__orch.connect() as conn:
                _begin_immediate(conn)
                current_grant = self.__grant(conn)
                current = _load(conn, _op_key(self.__grant_id, op), 'operation')
                current_claim = _load(conn, _op_key(self.__grant_id, current_grant['claim_id']), 'operation')
                immutable = ('version', 'grant_id', 'spec', 'status', 'claim_id')
                if (any(current_grant[key] != grant[key] for key in immutable)
                        or not set(grant['checkpoint_ids']).issubset(current_grant['checkpoint_ids'])
                        or current != prepared or current_claim != claim):
                    raise ValueError('lifecycle_record_changed')
                self.__validate_native(conn, spec, self.__binding, self.__orch._capability_path(self.__binding.run_id))
                now = utc_now()
                details = dict(run_id=self.__binding.run_id, task_id=spec.task_id, reason=reason,
                    process_state=process_state, recorded_at=now, writer_reservation_released=False,
                    capability_revoked=False, safe_to_resume_elsewhere=False)
                conn.execute('INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES(?,?,?)',
                             ('run_checkpoint:' + self.__binding.run_id, canonical_json(details), now))
                conn.execute('UPDATE runs SET heartbeat_at=? WHERE run_id=?', (now, self.__binding.run_id))
                self.__orch._event(conn, 'WORKER_CHECKPOINT_RECORDED', task_id=spec.task_id,
                    run_id=self.__binding.run_id, payload={k: details[k] for k in
                    ('reason', 'process_state', 'writer_reservation_released', 'capability_revoked', 'safe_to_resume_elsewhere')})
                record = _record(self.__grant_id, op, 'checkpoint', fingerprint, 'DONE',
                                 self.__binding.run_id, spec.attempt, 'lifecycle_checkpointed')
                _store(conn, _op_key(self.__grant_id, op), record)
                conn.execute('COMMIT')
            self.status()
            return _result(record)
        except Exception:
            return _unknown(op)
