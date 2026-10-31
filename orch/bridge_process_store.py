"""Safe process metadata in the existing ledger, never spawn/replay authority.

The trusted caller holds runtime_operation_lock. A dispatch is usable only
after FULL commit and independent exact readback; uncertainty never retries a
mutation. Exit evidence is supplied only by an owner retaining a reaped Popen
and a settled pipe. No PID, command, environment or output is stored here.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3

from .bridge_operation_journal import _identity
from .bridge_patch_execution import _require_lock
from .worker_operations import WorkerOperationRequest, WorkerRunBinding


_DISPATCH = "PROCESS_DISPATCH_INTENT"
_EXIT = "PROCESS_EXIT_OBSERVED"
_ACK = "PROCESS_CLOSURE_ACKNOWLEDGED"
_COMMON = {"schema_version", "request", "session_hash", "profile_hash", "handle"}
_TERMINAL = {"intent_seq", "intent_hash", "exit_code", "reason", "output_bytes", "truncated"}
_LINK = {"intent_seq", "intent_hash", "exit_seq", "exit_hash"}
_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9._-]{1,200}", re.ASCII)
_REASONS = {"exited", "terminated", "deadline", "owner_cleanup"}
_WRITER_STATES = ("RUNNING", "RESULT_SUBMITTED", "QUIESCING", "VERIFYING", "REVIEWING", "VERIFIED")
_MAX_DISPATCHES = 64
DEFAULT_MAX_PROCESSES_TOTAL = 6
DEFAULT_MAX_PROCESSES_PER_RUN = 2
_MAX_RECORD_BYTES = 8192
_MAX_ACK_BYTES = 32768  # Includes all 64 exact exit links and maximal binding IDs.
_MAX_RUN_ROWS = 132  # 64 starts + 64 exits + submit/quiesce/abort acknowledgements.


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("process_invalid_record")


def _digest(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError("process_invalid_record")


def _positive(value):
    if type(value) is not int or not 1 <= value <= 2 ** 63 - 1:
        raise ValueError("process_invalid_record")


def _binding(value):
    if isinstance(value, WorkerRunBinding):
        value = asdict(value)
    _keys(value, {"project_id", "writer_key", "task_id", "run_id", "attempt"})
    return asdict(WorkerRunBinding(**value))


def _request(value):
    _keys(value, {"binding", "operation_id", "kind", "target", "max_output_bytes", "schema_version"})
    request = WorkerOperationRequest(**dict(value, binding=WorkerRunBinding(**_binding(value["binding"]))))
    if request.kind != "process.start":
        raise ValueError("process_invalid_kind")
    return asdict(request)


def _encode(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True,
                     allow_nan=False, separators=(",", ":")).encode("ascii")
    maximum = _MAX_ACK_BYTES if set(value) == {"schema_version", "binding", "exits"} else _MAX_RECORD_BYTES
    if len(raw) > maximum:
        raise ValueError("process_invalid_record")
    return raw


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("process_invalid_record")
        value[key] = item
    return value


def _payload(value, kind):
    if kind == _ACK:
        _keys(value, {"schema_version", "binding", "exits"})
        _binding(value["binding"])
        if not isinstance(value["exits"], list) or not 1 <= len(value["exits"]) <= _MAX_DISPATCHES:
            raise ValueError("process_invalid_record")
        for link in value["exits"]:
            _keys(link, _LINK)
            _positive(link["intent_seq"])
            _positive(link["exit_seq"])
            _digest(link["intent_hash"])
            _digest(link["exit_hash"])
    else:
        _keys(value, _COMMON | (_TERMINAL if kind == _EXIT else set()))
        _request(value["request"])
        _digest(value["session_hash"])
        _digest(value["profile_hash"])
        if not isinstance(value["handle"], str) or _ID.fullmatch(value["handle"]) is None:
            raise ValueError("process_invalid_handle")
        if kind == _EXIT:
            _positive(value["intent_seq"])
            _digest(value["intent_hash"])
            if (type(value["exit_code"]) is not int or not -(2 ** 31) <= value["exit_code"] < 2 ** 31
                    or not isinstance(value["reason"], str) or value["reason"] not in _REASONS
                    or type(value["output_bytes"]) is not int or not 0 <= value["output_bytes"] <= 2 ** 63 - 1
                    or type(value["truncated"]) is not bool):
                raise ValueError("process_invalid_exit")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("process_invalid_record")
    _encode(value)
    return value


def _record(row):
    raw = row["payload_json"]
    maximum = _MAX_ACK_BYTES if row["kind"] == _ACK else _MAX_RECORD_BYTES
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum:
        raise ValueError("process_invalid_record")
    try:
        payload = json.loads(raw, object_pairs_hook=_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("process_invalid_record")))
        _payload(payload, row["kind"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("process_invalid_record") from exc
    _positive(row["seq"])
    binding = payload["binding"] if row["kind"] == _ACK else payload["request"]["binding"]
    if row["run_id"] != binding["run_id"] or row["task_id"] != binding["task_id"]:
        raise ValueError("process_cross_binding_record")
    return dict(payload, seq=row["seq"], hash=hashlib.sha256(_encode(payload)).hexdigest())


def _ledger_binding(conn, run_id):
    row = conn.execute("SELECT r.run_id,r.task_id,r.attempt,r.state,t.project_id,t.writer_key,t.status AS task_state "
                       "FROM runs r JOIN tasks t ON t.task_id=r.task_id WHERE r.run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("process_run_unknown")
    return row, {key: row[key] for key in ("project_id", "writer_key", "task_id", "run_id", "attempt")}


def _chains(conn, run_id, expected_binding):
    # Match payload identity too: corrupting the row columns cannot hide a
    # conflicting chain. Malformed process JSON is conservatively unresolved.
    rows = conn.execute(
        "SELECT seq,kind,task_id,run_id,payload_json FROM events WHERE kind IN (?,?,?) AND "
        "(run_id=? OR CASE WHEN json_valid(payload_json) THEN "
        "COALESCE(json_extract(payload_json,'$.request.binding.run_id'),json_extract(payload_json,'$.binding.run_id')) "
        "ELSE ? END=?) ORDER BY seq LIMIT ?",
        (_DISPATCH, _EXIT, _ACK, run_id, run_id, run_id, _MAX_RUN_ROWS + 1)).fetchall()
    if len(rows) > _MAX_RUN_ROWS:
        raise ValueError("process_capacity")
    if not rows:
        return [], {}
    expected = _binding(expected_binding)
    _, actual = _ledger_binding(conn, run_id)
    if actual != expected:
        raise ValueError("process_binding_mismatch")
    intents, terminals, operations, handles = [], {}, set(), set()
    by_seq = {}
    for row in rows:
        record = _record(row)
        if row["kind"] == _ACK:
            if record["binding"] != expected:
                raise ValueError("process_binding_mismatch")
            seen = set()
            for link in record["exits"]:
                start = by_seq.get(link["intent_seq"])
                terminal = terminals.get(link["intent_seq"])
                if (start is None or terminal is None or link["intent_seq"] in seen
                        or link != _link(start, terminal)):
                    raise ValueError("process_invalid_acknowledgement")
                seen.add(link["intent_seq"])
            continue
        if record["request"]["binding"] != expected:
            raise ValueError("process_binding_mismatch")
        if row["kind"] == _DISPATCH:
            operation = record["request"]["operation_id"]
            if operation in operations or record["handle"] in handles:
                raise ValueError("process_duplicate_dispatch")
            if len(intents) >= _MAX_DISPATCHES:
                raise ValueError("process_capacity")
            operations.add(operation)
            handles.add(record["handle"])
            intents.append(record)
            by_seq[record["seq"]] = record
        else:
            start = by_seq.get(record["intent_seq"])
            if (start is None or record["intent_seq"] in terminals
                    or record["intent_hash"] != start["hash"]
                    or any(record[key] != start[key] for key in _COMMON)):
                raise ValueError("process_invalid_exit_chain")
            terminals[start["seq"]] = record
    return intents, terminals


def _link(intent, terminal):
    return {"intent_seq": intent["seq"], "intent_hash": intent["hash"],
            "exit_seq": terminal["seq"], "exit_hash": terminal["hash"]}


def _unresolved_processes(conn):
    """Count this home's durable chains, including inactive/unknown owners.

    Run state, session closure and a missing local Popen are never exit proof.
    Validate every process-bearing run so malformed, orphan or cross-binding
    history cannot silently disappear from the shared admission count.
    """
    total = 0
    runs = conn.execute("SELECT DISTINCT run_id FROM events WHERE kind IN (?,?,?)",
                        (_DISPATCH, _EXIT, _ACK))
    for row in runs:
        _, binding = _ledger_binding(conn, row["run_id"])
        intents, terminals = _chains(conn, row["run_id"], binding)
        total += len(intents) - len(terminals)
    return total


def configure_process_durability(conn):
    """Configure before BEGIN, never silently retain NORMAL/OFF durability."""
    # SQLite itself rejects safety-level changes inside an existing transaction.
    conn.execute("PRAGMA synchronous=FULL")
    if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
        raise ValueError("process_full_durability_required")


def require_processes_resolved(conn, run_id, expected_binding):
    """Validate and acknowledge exit chains in the native FULL transaction.

The caller's subsequent FULL COMMIT commits this acknowledgement and its
authority transition together. No caller boolean can waive missing evidence.
Legacy runs without process rows retain compatibility.
"""
    if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
        raise ValueError("process_full_transaction_required")
    active = getattr(conn, "in_transaction", None)
    if active is None:
        # SQL-only connection wrappers preserve the SQLite transaction check:
        # even an unchanged safety level is forbidden inside a transaction.
        try:
            conn.execute("PRAGMA synchronous=FULL")
        except sqlite3.OperationalError as exc:
            active = str(exc).lower() == "safety level may not be changed inside a transaction"
    if active is not True:
        raise ValueError("process_full_transaction_required")
    try:
        intents, terminals = _chains(conn, run_id, expected_binding)
        if len(intents) != len(terminals):
            raise ValueError("process_unresolved")
        if intents:
            payload = {"schema_version": 1, "binding": _binding(expected_binding),
                       "exits": [_link(intent, terminals[intent["seq"]]) for intent in intents]}
            _insert(conn, _ACK, payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("process_unresolved") from exc


def _insert(conn, kind, payload):
    _payload(payload, kind)
    binding = payload["binding"] if kind == _ACK else payload["request"]["binding"]
    cursor = conn.execute("INSERT INTO events(ts,kind,task_id,run_id,payload_json) VALUES(?,?,?,?,?)",
                          (datetime.now(timezone.utc).isoformat(), kind, binding["task_id"],
                           binding["run_id"], _encode(payload).decode("ascii")))
    result = dict(payload, seq=cursor.lastrowid, hash=hashlib.sha256(_encode(payload)).hexdigest())
    row = conn.execute("SELECT seq,kind,task_id,run_id,payload_json FROM events WHERE seq=?",
                       (result["seq"],)).fetchone()
    if (row is None or row["kind"] != kind or row["payload_json"] != _encode(payload).decode("ascii")
            or _record(row) != result):
        raise ValueError("process_readback_unknown")
    return result


class ProcessOperationStore:
    """Trusted local ledger companion; contains no process execution code.

    Callers sharing an authoritative home supply the same trusted limits.
    Configuration is local constructor policy, never worker/wire authority.
    """
    def __init__(self, orch, *, max_processes_total=DEFAULT_MAX_PROCESSES_TOTAL,
                 max_processes_per_run=DEFAULT_MAX_PROCESSES_PER_RUN):
        if any(type(value) is not int or value <= 0
               for value in (max_processes_total, max_processes_per_run)):
            raise ValueError("process_invalid_limits")
        self._max_processes_total = max_processes_total
        self._max_processes_per_run = max_processes_per_run
        self._orch = orch
        self._home = orch.root
        self._home_identity = _identity(os.stat(self._home, follow_symlinks=False), boundary=True)

    @classmethod
    def open_existing(cls, root):
        """Open an existing private home without initialization or repair."""
        from .bridge_process_ledger import ExistingProcessLedger
        return cls(ExistingProcessLedger(root))

    def require_authority(self, authority):
        """Refuse a different or replaced authority home and database."""
        from .bridge_process_ledger import ExistingProcessLedger
        if not isinstance(self._orch, ExistingProcessLedger) or not self._orch.matches(authority):
            raise ValueError("process_ledger_authority_mismatch")

    def _lock(self):
        return _require_lock(self._home, self._home_identity)

    def _readback(self, expected, kind):
        with self._orch.connect() as conn:
            configure_process_durability(conn)
            row = conn.execute("SELECT seq,kind,task_id,run_id,payload_json FROM events WHERE seq=?",
                               (expected["seq"],)).fetchone()
            payload = {key: value for key, value in expected.items() if key not in ("seq", "hash")}
            if (row is None or row["kind"] != kind or row["payload_json"] != _encode(payload).decode("ascii")
                    or _record(row) != expected):
                raise ValueError("process_readback_unknown")
            intents, terminals = _chains(conn, expected["request"]["binding"]["run_id"],
                                         expected["request"]["binding"])
            found = next((intent for intent in intents if intent["seq"] == expected["seq"]), None) if kind == _DISPATCH else terminals.get(expected["intent_seq"])
            if found != expected:
                raise ValueError("process_readback_unknown")
        self._lock().check()
        return expected

    def intents(self, binding):
        self._lock()
        expected = _binding(binding)
        with self._orch.connect() as conn:
            intents, _ = _chains(conn, expected["run_id"], expected)
        self._lock().check()
        return intents

    def dispatch(self, request, session_hash, profile_hash, handle):
        self._lock()
        if not isinstance(request, WorkerOperationRequest):
            raise ValueError("process_invalid_request")
        payload = {"schema_version": 1, "request": asdict(request), "session_hash": session_hash,
                   "profile_hash": profile_hash, "handle": handle}
        _payload(payload, _DISPATCH)
        expected = payload["request"]["binding"]
        with self._orch.connect() as conn:
            configure_process_durability(conn)
            conn.execute("BEGIN IMMEDIATE")
            row, actual = _ledger_binding(conn, expected["run_id"])
            if actual != expected or row["state"] != "RUNNING" or row["task_state"] != "IN_PROGRESS":
                raise ValueError("process_run_not_active")
            latest = conn.execute("SELECT MAX(attempt) FROM runs WHERE task_id=?", (expected["task_id"],)).fetchone()[0]
            writers = conn.execute("SELECT r.run_id FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                                   "WHERE t.writer_key=? AND r.state IN (?,?,?,?,?,?)",
                                   (expected["writer_key"], *_WRITER_STATES)).fetchall()
            if latest != expected["attempt"] or len(writers) != 1 or writers[0][0] != expected["run_id"]:
                raise ValueError("process_writer_not_exclusive")
            intents, terminals = _chains(conn, expected["run_id"], expected)
            if any(intent["request"]["operation_id"] == request.operation_id or intent["handle"] == handle for intent in intents):
                raise ValueError("process_duplicate_dispatch")
            if (len(intents) >= _MAX_DISPATCHES
                    or len(intents) - len(terminals) >= self._max_processes_per_run
                    or _unresolved_processes(conn) >= self._max_processes_total):
                raise ValueError("process_capacity")
            result = _insert(conn, _DISPATCH, payload)
            self._lock().check()
            conn.execute("COMMIT")
        return self._readback(result, _DISPATCH)

    def observe_exit(self, intent, exit_code, reason, output_bytes, truncated):
        self._lock()
        _keys(intent, _COMMON | {"seq", "hash"})
        _positive(intent["seq"])
        _digest(intent["hash"])
        common = {key: intent[key] for key in _COMMON}
        _payload(common, _DISPATCH)
        payload = dict(common, intent_seq=intent["seq"], intent_hash=intent["hash"],
                       exit_code=exit_code, reason=reason, output_bytes=output_bytes, truncated=truncated)
        _payload(payload, _EXIT)
        binding = common["request"]["binding"]
        with self._orch.connect() as conn:
            configure_process_durability(conn)
            conn.execute("BEGIN IMMEDIATE")
            intents, terminals = _chains(conn, binding["run_id"], binding)
            found = next((item for item in intents if item["seq"] == intent["seq"]), None)
            if found != intent or intent["seq"] in terminals:
                raise ValueError("process_exit_intent_mismatch")
            result = _insert(conn, _EXIT, payload)
            self._lock().check()
            conn.execute("COMMIT")
        return self._readback(result, _EXIT)
