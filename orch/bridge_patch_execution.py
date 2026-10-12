"""Bounded immutable patch execution evidence and cooperative runtime locking.

The lock serializes participating local callers; it is not an OS fence against
other actors. Records contain metadata only and never confer mutation/replay
authority. An uncertain append retains evidence and returns UNKNOWN.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Callable
import uuid

from .bridge_operation_journal import (
    JOURNAL_DIRECTORY, JOURNAL_MAX_RECORDS, JOURNAL_MAX_RECORDS_PER_RUN,
    JOURNAL_RECORD_MAX_BYTES, OperationIntentJournal, journal_inventory,
    validate_intent_record, _Absent, _check_directory, _decode as _decode_intent,
    _directory_at, _encode, _id_hash, _identity, _inode, _names, _open_home,
    _request,
)
from .worker_operations import WORKER_MAX_OUTPUT_BYTES, WorkerOperationRequest, WorkerRunBinding


EXECUTION_DIRECTORY = "patch_execution"
EXECUTION_RECORD_MAX_BYTES = 8192
EXECUTION_MAX_OPERATIONS = 1024
EXECUTION_MAX_OPERATIONS_PER_RUN = 64
_PHASE_FILES = {"dispatch": "dispatch.json", "publication": "publication.json", "result": "result.json"}
_COMMON_KEYS = {"schema_version", "phase", "request", "intent_fingerprint", "created_at"}
_SIGNATURE_KEYS = {"dev", "ino", "mode", "uid", "gid", "nlink", "size", "mtime_ns", "ctime_ns", "flags"}
_HEX = re.compile(r"[0-9a-f]{64}", re.ASCII)
_LOCKS = threading.local()


def _keys(value, keys) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("patch_execution_invalid_record")


def _digest(value) -> None:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError("patch_execution_invalid_record")


def _pair(value) -> None:
    if (not isinstance(value, list) or len(value) != 2
            or any(type(item) is not int or item < 0 for item in value)
            or value[1] == 0):
        raise ValueError("patch_execution_invalid_anchor")


def _signature(value) -> dict:
    _keys(value, _SIGNATURE_KEYS)
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise ValueError("patch_execution_invalid_signature")
    if (not stat.S_ISREG(value["mode"]) or value["mode"] & 0o7000
            or value["uid"] != os.getuid() or value["ino"] == 0
            or value["nlink"] != 1 or value["flags"] != 0
            or value["size"] > WORKER_MAX_OUTPUT_BYTES):
        raise ValueError("patch_execution_invalid_signature")
    return dict(value)


def _content(value) -> dict:
    _keys(value, {"identity", "sha256", "size_bytes", "xattrs_digest"})
    identity = _signature(value["identity"])
    _digest(value["sha256"])
    _digest(value["xattrs_digest"])
    if type(value["size_bytes"]) is not int or value["size_bytes"] != identity["size"]:
        raise ValueError("patch_execution_invalid_signature")
    return dict(value, identity=identity)


def _stable_signature(value) -> dict:
    return {key: item for key, item in value.items() if key not in {"mtime_ns", "ctime_ns"}}


def _record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("patch_execution_invalid_record")
    phase = record.get("phase")
    extra = {"DISPATCH": {"anchors", "original", "temp_name"},
             "PUBLICATION": {"dispatch_sha256", "staged"},
             "RESULT": {"dispatch_sha256", "publication_sha256", "disposition", "basis"}}
    if not isinstance(phase, str) or phase not in extra:
        raise ValueError("patch_execution_invalid_phase")
    _keys(record, _COMMON_KEYS | extra[phase])
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("patch_execution_invalid_record")
    request = _request(record["request"])
    _digest(record["intent_fingerprint"])
    created = record["created_at"]
    if (not isinstance(created, str) or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", created) is None):
        raise ValueError("patch_execution_invalid_record")
    try:
        datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError("patch_execution_invalid_record") from exc
    checked = dict(record, request=asdict(request))
    if phase == "DISPATCH":
        anchors = record["anchors"]
        _keys(anchors, {"root", "parents", "parent"})
        _pair(anchors["root"])
        _pair(anchors["parent"])
        if not isinstance(anchors["parents"], list) or not 1 <= len(anchors["parents"]) <= 512:
            raise ValueError("patch_execution_invalid_anchor")
        for pair in anchors["parents"]:
            _pair(pair)
        if anchors["parents"][0] != anchors["root"] or anchors["parents"][-1] != anchors["parent"]:
            raise ValueError("patch_execution_invalid_anchor")
        checked["anchors"] = {"root": list(anchors["root"]),
                              "parents": [list(pair) for pair in anchors["parents"]],
                              "parent": list(anchors["parent"])}
        checked["original"] = _content(record["original"])
        if (not isinstance(record["temp_name"], str) or re.fullmatch(
                r"\.orch-patch-[0-9a-f]{32}\.tmp", record["temp_name"], re.ASCII) is None):
            raise ValueError("patch_execution_invalid_temp")
    else:
        _digest(record["dispatch_sha256"])
        if phase == "PUBLICATION":
            checked["staged"] = _content(record["staged"])
        else:
            if record["publication_sha256"] is not None:
                _digest(record["publication_sha256"])
            if record["disposition"] not in ("APPLIED", "NOT_APPLIED"):
                raise ValueError("patch_execution_invalid_result")
            basis = record["basis"]
            _keys(basis, {"kind", "target", "temp_absent"})
            if basis["kind"] not in ("APPLY", "RECONCILE_APPLIED", "RECONCILE_STAGED",
                                      "RECONCILE_NO_PUBLICATION", "FAILED_BEFORE_PUBLICATION"):
                raise ValueError("patch_execution_invalid_result")
            if type(basis["temp_absent"]) is not bool or not basis["temp_absent"]:
                raise ValueError("patch_execution_invalid_result")
            checked["basis"] = dict(basis, target=_content(basis["target"]))
    if len(_encode(checked)) > EXECUTION_RECORD_MAX_BYTES:
        raise ValueError("patch_execution_record_too_large")
    return checked


def execution_record_hash(record: dict) -> str:
    """Canonical predecessor digest, independent of JSON mapping order."""
    return hashlib.sha256(_encode(_record(record))).hexdigest()


def validate_execution_records(records: dict, intent: dict) -> dict:
    """Validate the complete prefix/terminal chain against its exact intent."""
    if (not isinstance(records, dict) or not records or "dispatch" not in records
            or not set(records).issubset(_PHASE_FILES)):
        raise ValueError("patch_execution_invalid_chain")
    source = validate_intent_record(intent)
    checked = {}
    for phase, record in records.items():
        value = _record(record)
        if (value["phase"] != phase.upper() or value["request"] != source["request"]
                or value["intent_fingerprint"] != source["fingerprint"]):
            raise ValueError("patch_execution_intent_mismatch")
        checked[phase] = value
    dispatch = checked["dispatch"]
    original = dispatch["original"]
    baseline = source["replacement"]["baseline"]
    if any(original[key] != baseline[key] for key in ("sha256", "size_bytes")):
        raise ValueError("patch_execution_original_mismatch")
    dispatch_hash = execution_record_hash(dispatch)
    publication = checked.get("publication")
    if publication is not None:
        staged = publication["staged"]
        if (publication["dispatch_sha256"] != dispatch_hash
                or any(staged[key] != source["replacement"][key] for key in ("sha256", "size_bytes"))
                or any(staged["identity"][key] != original["identity"][key]
                       for key in ("dev", "mode", "uid", "gid", "nlink", "flags"))
                or staged["identity"]["ino"] == original["identity"]["ino"]
                or staged["xattrs_digest"] != original["xattrs_digest"]):
            raise ValueError("patch_execution_publication_mismatch")
    result = checked.get("result")
    if result is not None:
        if (result["dispatch_sha256"] != dispatch_hash
                or result["publication_sha256"] != (
                    execution_record_hash(publication) if publication is not None else None)):
            raise ValueError("patch_execution_predecessor_mismatch")
        target = result["basis"]["target"]
        kind = result["basis"]["kind"]
        if result["disposition"] == "APPLIED":
            if (publication is None or kind not in ("APPLY", "RECONCILE_APPLIED")
                    or _stable_signature(target["identity"]) != _stable_signature(publication["staged"]["identity"])
                    or any(target[key] != publication["staged"][key]
                           for key in ("sha256", "size_bytes", "xattrs_digest"))):
                raise ValueError("patch_execution_result_mismatch")
        else:
            kinds = ("APPLY", "RECONCILE_STAGED") if publication else (
                "APPLY", "RECONCILE_NO_PUBLICATION", "FAILED_BEFORE_PUBLICATION")
            if target != original or kind not in kinds:
                raise ValueError("patch_execution_result_mismatch")
    return checked


def _check_anchors(home_path: Path, home_identity: dict, anchors) -> None:
    fresh = _open_home(home_path)
    try:
        if (_identity(os.fstat(fresh), boundary=True) != home_identity
                or _identity(os.fstat(anchors[0][2]), boundary=True) != home_identity):
            raise ValueError("patch_execution_anchor_changed")
    finally:
        os.close(fresh)
    for parent, name, child in anchors[1:]:
        current, pinned = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(child)
        _check_directory(current)
        _check_directory(pinned)
        if _inode(current) != _inode(pinned):
            raise ValueError("patch_execution_anchor_changed")


class _RuntimeLock:
    def __init__(self, home, home_identity, anchors):
        self.home, self.home_identity, self.anchors = home, home_identity, anchors
        self.pid = os.getpid()
        self.active = True

    def check(self):
        if not self.active or self.pid != os.getpid():
            raise ValueError("patch_execution_lock_required")
        _check_anchors(self.home, self.home_identity, self.anchors)


@contextmanager
def runtime_operation_lock(home: Path):
    """Lock the existing private runtime inode without creation or repair.

    Acquire this before any SQLite write transaction. Append checks ownership
    on the calling process/thread and never acquires a second recursive lock.
    """
    path = Path(home)
    with ExitStack() as owned:
        home_fd = _open_home(path)
        owned.callback(os.close, home_fd)
        home_identity = _identity(os.fstat(home_fd), boundary=True)
        runtime_fd = _directory_at(home_fd, ".runtime")
        owned.callback(os.close, runtime_fd)
        anchors = [(None, None, home_fd), (home_fd, ".runtime", runtime_fd)]
        _check_anchors(path, home_identity, anchors)
        locks = getattr(_LOCKS, "held", {})
        if any(token.active and token.pid == os.getpid()
               and _inode(os.fstat(token.anchors[-1][2])) == _inode(os.fstat(runtime_fd))
               for token in locks.values()):
            raise ValueError("patch_execution_busy")
        try:
            fcntl.flock(runtime_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("patch_execution_busy") from exc
        token = _RuntimeLock(path, home_identity, anchors)
        locks = dict(locks)
        locks[str(path)] = token
        _LOCKS.held = locks
        try:
            token.check()
            yield token
        finally:
            try:
                token.check()
            finally:
                token.active = False
                current = dict(getattr(_LOCKS, "held", {}))
                if current.get(str(path)) is token:
                    current.pop(str(path))
                _LOCKS.held = current
                fcntl.flock(runtime_fd, fcntl.LOCK_UN)


def _require_lock(home: Path, home_identity: dict) -> _RuntimeLock:
    token = getattr(_LOCKS, "held", {}).get(str(home))
    if token is None or token.home_identity != home_identity:
        raise ValueError("patch_execution_lock_required")
    token.check()
    return token


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("patch_execution_invalid_json")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("patch_execution_invalid_json")


def _read_record_bytes(parent: int, name: str, *, limit=EXECUTION_RECORD_MAX_BYTES):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    def check(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= limit):
            raise ValueError("patch_execution_unsafe_record")
    check(before)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        check(opened)
        if _identity(before) != _identity(opened):
            raise ValueError("patch_execution_record_changed")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(8192, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        check(after)
        check(current)
        if (_identity(opened) != _identity(after) or _identity(opened) != _identity(current)
                or len(raw) != opened.st_size):
            raise ValueError("patch_execution_record_changed")
        return bytes(raw), {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                            "identity": _identity(after)}
    finally:
        os.close(fd)


def _decode_record(raw: bytes) -> dict:
    try:
        return _record(json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                                  parse_constant=_invalid_constant))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("patch_execution_invalid_json") from exc


def _read_record(parent: int, name: str):
    raw, metadata = _read_record_bytes(parent, name)
    return _decode_record(raw), metadata


class PatchExecutionStore:
    """Strict append-once phase records, independently linked to intent bytes."""

    def __init__(self, home: Path):
        self.home = Path(home)
        fd = _open_home(self.home)
        try:
            self._home_identity = _identity(os.fstat(fd), boundary=True)
        finally:
            os.close(fd)

    @contextmanager
    def _anchors(self, *, create=False, run_hash=None, operation_hash=None):
        with ExitStack() as owned:
            home_fd = _open_home(self.home)
            owned.callback(os.close, home_fd)
            anchors = [(None, None, home_fd)]
            _check_anchors(self.home, self._home_identity, anchors)
            parent = home_fd
            names = [".runtime", EXECUTION_DIRECTORY]
            if run_hash is not None:
                names.append(run_hash)
            if operation_hash is not None:
                names.append(operation_hash)
            for name in names:
                try:
                    # Runtime is always an existing lock anchor, never created.
                    child = _directory_at(parent, name, create=create and name != ".runtime")
                except FileNotFoundError as exc:
                    self._confirm_absent(anchors, parent, name)
                    raise _Absent() from exc
                owned.callback(os.close, child)
                anchors.append((parent, name, child))
                parent = child
            _check_anchors(self.home, self._home_identity, anchors)
            yield anchors
            _check_anchors(self.home, self._home_identity, anchors)

    def _confirm_absent(self, anchors, parent, name):
        _check_anchors(self.home, self._home_identity, anchors)
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            _check_anchors(self.home, self._home_identity, anchors)
            return
        raise ValueError("patch_execution_absence_changed")

    def _operation(self, fd: int, run_hash: str, operation_hash: str):
        names = _names(fd, 3)
        if not set(names).issubset(_PHASE_FILES.values()):
            raise ValueError("patch_execution_unexpected_entry")
        records, files = {}, {}
        before = _identity(os.fstat(fd))
        for phase, name in _PHASE_FILES.items():
            if name in names:
                records[phase], files[name] = _read_record(fd, name)
        if names != _names(fd, 3) or before != _identity(os.fstat(fd)):
            raise ValueError("patch_execution_inventory_changed")
        if not records:
            raise ValueError("patch_execution_partial_directory")
        dispatch = records.get("dispatch")
        if dispatch is None:
            raise ValueError("patch_execution_invalid_chain")
        request = dispatch["request"]
        if (_id_hash(request["binding"]["run_id"]) != run_hash
                or _id_hash(request["operation_id"]) != operation_hash):
            raise ValueError("patch_execution_identifier_mismatch")
        intent = OperationIntentJournal(self.home).inspect(request["binding"]["run_id"], request["operation_id"])
        if intent["status"] != "RECORDED":
            raise ValueError("patch_execution_intent_unknown")
        return validate_execution_records(records, intent["record"]), files

    def inspect(self, run_id: str, operation_id: str) -> dict:
        try:
            run_hash, operation_hash = _id_hash(run_id), _id_hash(operation_id)
            with self._anchors(run_hash=run_hash, operation_hash=operation_hash) as anchors:
                records, _ = self._operation(anchors[-1][2], run_hash, operation_hash)
            return {"status": "RECORDED", "records": records, "terminal": "result" in records}
        except _Absent:
            return {"status": "ABSENT"}
        except (OSError, ValueError, TypeError, RecursionError):
            return {"status": "UNKNOWN", "error_code": "patch_execution_inspection_unknown"}

    def _inventory(self) -> dict:
        files, directories, operations = {}, {}, []
        prefix = ".runtime/" + EXECUTION_DIRECTORY
        with self._anchors() as anchors:
            root_fd = anchors[-1][2]
            before = _identity(os.fstat(root_fd))
            directories[prefix] = before
            runs = _names(root_fd, EXECUTION_MAX_OPERATIONS)
            if not runs:
                raise ValueError("patch_execution_partial_directory")
            for run_hash in runs:
                if _HEX.fullmatch(run_hash) is None:
                    raise ValueError("patch_execution_unexpected_entry")
                run_fd = _directory_at(root_fd, run_hash)
                try:
                    run_before = _identity(os.fstat(run_fd))
                    run_prefix = prefix + "/" + run_hash
                    directories[run_prefix] = run_before
                    names = _names(run_fd, EXECUTION_MAX_OPERATIONS_PER_RUN)
                    if not names:
                        raise ValueError("patch_execution_partial_directory")
                    for operation_hash in names:
                        if _HEX.fullmatch(operation_hash) is None:
                            raise ValueError("patch_execution_unexpected_entry")
                        op_fd = _directory_at(run_fd, operation_hash)
                        try:
                            op_before = _identity(os.fstat(op_fd))
                            records, metadata = self._operation(op_fd, run_hash, operation_hash)
                            if not records:
                                raise ValueError("patch_execution_partial_directory")
                            op_prefix = run_prefix + "/" + operation_hash
                            directories[op_prefix] = op_before
                            files.update({op_prefix + "/" + name: item for name, item in metadata.items()})
                            request = records["dispatch"]["request"]
                            operations.append({"run_id": request["binding"]["run_id"],
                                               "operation_id": request["operation_id"],
                                               "intent_fingerprint": records["dispatch"]["intent_fingerprint"],
                                               "request": request, "records": records,
                                               "terminal": "result" in records})
                            if len(operations) > EXECUTION_MAX_OPERATIONS:
                                raise ValueError("patch_execution_capacity_exceeded")
                            if (op_before != _identity(os.fstat(op_fd))
                                    or op_before != _identity(os.stat(operation_hash, dir_fd=run_fd, follow_symlinks=False))):
                                raise ValueError("patch_execution_inventory_changed")
                        finally:
                            os.close(op_fd)
                    if (names != _names(run_fd, EXECUTION_MAX_OPERATIONS_PER_RUN)
                            or run_before != _identity(os.fstat(run_fd))
                            or run_before != _identity(os.stat(run_hash, dir_fd=root_fd, follow_symlinks=False))):
                        raise ValueError("patch_execution_inventory_changed")
                finally:
                    os.close(run_fd)
            if runs != _names(root_fd, EXECUTION_MAX_OPERATIONS) or before != _identity(os.fstat(root_fd)):
                raise ValueError("patch_execution_inventory_changed")
            boundary = {"home_identity": self._home_identity,
                        "runtime_identity": _identity(os.fstat(anchors[1][2]), boundary=True)}
        return {"status": "READY", "operations": operations, "files": files,
                "directories": directories, "boundary": boundary}

    def _acknowledge_intent(self, expected_intent: dict, dispatch: dict, *,
                            authorize: Callable[[], None]) -> dict:
        """Renew exact intent evidence before creating a new dispatch namespace."""
        lock = _require_lock(self.home, self._home_identity)
        if not callable(authorize):
            raise ValueError("patch_execution_authority_required")
        checked = _record(dispatch)
        if checked["phase"] != "DISPATCH":
            raise ValueError("patch_execution_invalid_phase")
        expected = validate_intent_record(expected_intent)
        validate_execution_records({"dispatch": checked}, expected)
        request = checked["request"]
        run_hash = _id_hash(request["binding"]["run_id"])
        operation_hash = _id_hash(request["operation_id"])
        final_name = operation_hash + ".json"

        def authority():
            try:
                authorize()
            except Exception as exc:
                raise ValueError("patch_execution_authority_denied") from exc

        authority()
        with ExitStack() as owned:
            # Reuse the caller's exact locked home/runtime descriptors. The
            # intent branch is opened without creation or journal API changes.
            anchors = list(lock.anchors)
            for name in (JOURNAL_DIRECTORY, run_hash):
                parent = anchors[-1][2]
                child = _directory_at(parent, name)
                owned.callback(os.close, child)
                anchors.append((parent, name, child))
            run_fd = anchors[-1][2]

            def directories():
                return [(depth, fd, _identity(os.fstat(fd), boundary=depth < 2))
                        for depth, (_, _, fd) in enumerate(anchors)]

            def names():
                runs = _names(anchors[2][2], JOURNAL_MAX_RECORDS)
                records = _names(run_fd, JOURNAL_MAX_RECORDS_PER_RUN)
                if (not runs or any(_HEX.fullmatch(name) is None for name in runs)
                        or not records or any(len(name) != 69 or not name.endswith(".json")
                            or _HEX.fullmatch(name[:-5]) is None for name in records)):
                    raise ValueError("patch_execution_unexpected_entry")
                return runs, records

            def check_file(metadata):
                if _identity(os.stat(final_name, dir_fd=run_fd, follow_symlinks=False)) != metadata["identity"]:
                    raise ValueError("patch_execution_evidence_changed")

            def snapshot():
                lock.check()
                _check_anchors(self.home, self._home_identity, anchors)
                pinned, entries = directories(), names()
                raw, metadata = _read_record_bytes(run_fd, final_name, limit=JOURNAL_RECORD_MAX_BYTES)
                source = _decode_intent(raw)
                # Full request/binding and fingerprint equality are checked by
                # the strict chain validator, alongside replacement metadata.
                validate_execution_records({"dispatch": checked}, source)
                if source != expected:
                    raise ValueError("patch_execution_existing_changed")
                check_file(metadata)
                if pinned != directories() or entries != names():
                    raise ValueError("patch_execution_evidence_changed")
                _check_anchors(self.home, self._home_identity, anchors)
                return source, raw, metadata, pinned, entries

            first = snapshot()
            fd = os.open(final_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=run_fd)
            try:
                if _identity(os.fstat(fd)) != first[2]["identity"]:
                    raise ValueError("patch_execution_evidence_changed")
                check_file(first[2])
                os.fsync(fd)
                if _identity(os.fstat(fd)) != first[2]["identity"]:
                    raise ValueError("patch_execution_evidence_changed")
                check_file(first[2])
            finally:
                os.close(fd)
            for depth, fd, identity in reversed(first[3]):
                lock.check()
                _check_anchors(self.home, self._home_identity, anchors)
                if _identity(os.fstat(fd), boundary=depth < 2) != identity:
                    raise ValueError("patch_execution_evidence_changed")
                os.fsync(fd)
            # Independently reopen and read the complete final record. Compare
            # raw bytes as well as identities and the validated linked intent.
            fresh = snapshot()
            if first != fresh:
                raise ValueError("patch_execution_evidence_changed")
            authority()
            lock.check()
            _check_anchors(self.home, self._home_identity, anchors)
            check_file(fresh[2])
            if first[3:] != (directories(), names()):
                raise ValueError("patch_execution_evidence_changed")
            return fresh[0]

    def acknowledge_existing(self, run_id: str, operation_id: str, expected_records: dict, *,
                             authorize: Callable[[], None], expected_request=None,
                             expected_binding=None) -> dict:
        """Renew durability of exact immutable evidence under the caller's lock.

        No acknowledgement is cached or persisted. Each final file, its source
        intent and every directory in both pinned branches must sync, followed
        by independent full-byte readback. Any uncertainty raises to the caller.
        """
        lock = _require_lock(self.home, self._home_identity)
        if not callable(authorize):
            raise ValueError("patch_execution_authority_required")
        run_hash, operation_hash = _id_hash(run_id), _id_hash(operation_id)
        if isinstance(expected_request, WorkerOperationRequest):
            expected_request = asdict(expected_request)
        if expected_request is not None:
            expected_request = asdict(_request(expected_request))
        if expected_binding is not None:
            expected_binding = _binding(expected_binding)

        def authority():
            try:
                authorize()
            except Exception as exc:
                raise ValueError("patch_execution_authority_denied") from exc

        def directories(execution, intent):
            # The two branches share the exact home/runtime descriptors. Their
            # boundary identities omit timestamps affected by unrelated state.
            values = []
            for depth, (_, _, fd) in enumerate(execution):
                values.append((depth, fd, _identity(os.fstat(fd), boundary=depth < 2)))
            for depth, (_, _, fd) in enumerate(intent[2:], start=2):
                values.append((depth, fd, _identity(os.fstat(fd))))
            return values

        def names(execution, intent):
            roots = ((execution[2][2], EXECUTION_MAX_OPERATIONS, "hash"),
                     (execution[3][2], EXECUTION_MAX_OPERATIONS_PER_RUN, "hash"),
                     (execution[4][2], 3, "phase"),
                     (intent[2][2], JOURNAL_MAX_RECORDS, "hash"),
                     (intent[3][2], JOURNAL_MAX_RECORDS_PER_RUN, "intent"))
            observed = []
            for fd, limit, kind in roots:
                entries = _names(fd, limit)
                if not entries or any(
                        (_HEX.fullmatch(name) is None if kind == "hash" else
                         name not in _PHASE_FILES.values() if kind == "phase" else
                         len(name) != 69 or not name.endswith(".json")
                         or _HEX.fullmatch(name[:-5]) is None) for name in entries):
                    raise ValueError("patch_execution_unexpected_entry")
                observed.append(entries)
            return observed

        def check_evidence(execution, intent, evidence):
            # A later read or authority callback can change a predecessor that
            # was already read. Recheck every named identity before accepting
            # the complete observation, rather than only the last file.
            for phase, (_, metadata) in evidence.items():
                parent = intent[-1][2] if phase == "intent" else execution[-1][2]
                name = operation_hash + ".json" if phase == "intent" else _PHASE_FILES[phase]
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != metadata["identity"]:
                    raise ValueError("patch_execution_evidence_changed")

        def snapshot(execution, intent):
            lock.check()
            _check_anchors(self.home, self._home_identity, execution)
            _check_anchors(self.home, self._home_identity, intent)
            if (_identity(os.fstat(execution[1][2]), boundary=True)
                    != _identity(os.fstat(lock.anchors[1][2]), boundary=True)):
                raise ValueError("patch_execution_lock_required")
            pinned, entries = directories(execution, intent), names(execution, intent)
            records, evidence = {}, {}
            for phase, name in _PHASE_FILES.items():
                if name in entries[2]:
                    raw, metadata = _read_record_bytes(execution[-1][2], name)
                    records[phase] = _decode_record(raw)
                    evidence[phase] = raw, metadata
            raw, metadata = _read_record_bytes(intent[-1][2], operation_hash + ".json",
                                               limit=JOURNAL_RECORD_MAX_BYTES)
            source = _decode_intent(raw)
            evidence["intent"] = raw, metadata
            if (source["request"]["binding"]["run_id"] != run_id
                    or source["request"]["operation_id"] != operation_id):
                raise ValueError("patch_execution_identifier_mismatch")
            checked = validate_execution_records(records, source)
            expected = validate_execution_records(expected_records, source)
            if (checked != expected
                    or (expected_request is not None and source["request"] != expected_request)
                    or (expected_binding is not None and source["request"]["binding"] != expected_binding)):
                raise ValueError("patch_execution_existing_changed")
            check_evidence(execution, intent, evidence)
            if pinned != directories(execution, intent) or entries != names(execution, intent):
                raise ValueError("patch_execution_evidence_changed")
            _check_anchors(self.home, self._home_identity, execution)
            _check_anchors(self.home, self._home_identity, intent)
            return checked, source, evidence, pinned, entries

        authority()
        try:
            with self._anchors(create=False, run_hash=run_hash, operation_hash=operation_hash) as execution, \
                    ExitStack() as owned:
                # Open the intent branch from the same locked runtime FD. Do
                # not call create or alter the protected journal implementation.
                journal_fd = _directory_at(execution[1][2], JOURNAL_DIRECTORY)
                owned.callback(os.close, journal_fd)
                intent_run_fd = _directory_at(journal_fd, run_hash)
                owned.callback(os.close, intent_run_fd)
                intent = execution[:2] + [(execution[1][2], JOURNAL_DIRECTORY, journal_fd),
                                         (journal_fd, run_hash, intent_run_fd)]
                first = snapshot(execution, intent)
                for phase, (_, metadata) in first[2].items():
                    parent = intent_run_fd if phase == "intent" else execution[-1][2]
                    name = operation_hash + ".json" if phase == "intent" else _PHASE_FILES[phase]
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                 dir_fd=parent)
                    try:
                        if (_identity(os.fstat(fd)) != metadata["identity"]
                                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
                                != metadata["identity"]):
                            raise ValueError("patch_execution_evidence_changed")
                        os.fsync(fd)
                        if (_identity(os.fstat(fd)) != metadata["identity"]
                                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
                                != metadata["identity"]):
                            raise ValueError("patch_execution_evidence_changed")
                    finally:
                        os.close(fd)
                for _, fd, identity in sorted(first[3], key=lambda item: item[0], reverse=True):
                    lock.check()
                    _check_anchors(self.home, self._home_identity, execution)
                    _check_anchors(self.home, self._home_identity, intent)
                    if _identity(os.fstat(fd), boundary=fd in (execution[0][2], execution[1][2])) != identity:
                        raise ValueError("patch_execution_evidence_changed")
                    os.fsync(fd)
                # A fresh snapshot independently opens, reads and validates all
                # bytes, including predecessors and source intent. Raw bytes
                # stay bounded in this call and never enter public inventory.
                fresh = snapshot(execution, intent)
                if first != fresh:
                    raise ValueError("patch_execution_evidence_changed")
                authority()
                lock.check()
                _check_anchors(self.home, self._home_identity, execution)
                _check_anchors(self.home, self._home_identity, intent)
                check_evidence(execution, intent, fresh[2])
                if first[3:] != (directories(execution, intent), names(execution, intent)):
                    raise ValueError("patch_execution_evidence_changed")
                return fresh[0]
        except _Absent as exc:
            raise ValueError("patch_execution_evidence_unknown") from exc

    @staticmethod
    def _remove_temp(parent, name, identity):
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or _inode(current) != identity:
            raise ValueError("patch_execution_temp_changed")
        os.unlink(name, dir_fd=parent)

    def append(self, record: dict, *, authorize: Callable[[], None]) -> dict:
        """Append under the caller's runtime lock; do not overwrite or replay."""
        try:
            checked = _record(record)
            if not callable(authorize):
                raise ValueError("patch_execution_authority_required")
            lock = _require_lock(self.home, self._home_identity)
        except (OSError, ValueError, TypeError, RecursionError):
            return {"status": "REFUSED", "error_code": "patch_execution_invalid_or_unlocked"}
        try:
            authorize()
        except Exception:
            return {"status": "REFUSED", "error_code": "patch_execution_authority_denied"}
        request = checked["request"]
        run_id, operation_id = request["binding"]["run_id"], request["operation_id"]
        run_hash, operation_hash = _id_hash(run_id), _id_hash(operation_id)
        phase, final_name = checked["phase"].lower(), _PHASE_FILES[checked["phase"].lower()]
        temp_name = ".execution-" + uuid.uuid4().hex + ".tmp"
        temp_identity, published = None, False
        try:
            lock.check()
            census = execution_inventory(self.home)
            if census["status"] not in ("ABSENT", "READY"):
                raise ValueError("patch_execution_inventory_unknown")
            found = self.inspect(run_id, operation_id)
            if found["status"] not in ("ABSENT", "RECORDED"):
                raise ValueError("patch_execution_existing_unknown")
            records = found.get("records", {})
            if phase in records:
                if records[phase] == checked:
                    renewed = self.acknowledge_existing(run_id, operation_id, records,
                        authorize=authorize, expected_request=request,
                        expected_binding=request["binding"])
                    return {"status": "EXISTING", "record": renewed[phase]}
                return {"status": "CONFLICT", "error_code": "patch_execution_phase_conflict"}
            if "result" in records:
                return {"status": "CONFLICT", "error_code": "patch_execution_terminal"}
            intent = OperationIntentJournal(self.home).inspect(run_id, operation_id)
            if intent["status"] != "RECORDED":
                return {"status": "REFUSED", "error_code": "patch_execution_intent_required"}
            try:
                validate_execution_records(dict(records, **{phase: checked}), intent["record"])
            except (ValueError, TypeError):
                return {"status": "CONFLICT" if records else "REFUSED",
                        "error_code": "patch_execution_chain_mismatch"}
            if not records and (len(census["operations"]) >= EXECUTION_MAX_OPERATIONS
                    or sum(item["run_id"] == run_id for item in census["operations"])
                    >= EXECUTION_MAX_OPERATIONS_PER_RUN):
                return {"status": "REFUSED", "error_code": "patch_execution_capacity_exceeded"}
            if phase == "dispatch":
                self._acknowledge_intent(intent["record"], checked, authorize=authorize)
            with self._anchors(create=True, run_hash=run_hash, operation_hash=operation_hash) as anchors:
                op_fd = anchors[-1][2]
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                    fd = os.open(temp_name, flags, 0o600, dir_fd=op_fd)
                    try:
                        os.fchmod(fd, 0o600)
                        temp_identity = _inode(os.fstat(fd))
                        raw = _encode(checked)
                        offset = 0
                        while offset < len(raw):
                            written = os.write(fd, raw[offset:])
                            if written <= 0:
                                raise OSError("short execution write")
                            offset += written
                        os.fsync(fd)
                        info = os.fstat(fd)
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                                or _inode(info) != temp_identity or info.st_size != len(raw)):
                            raise ValueError("patch_execution_temp_changed")
                    finally:
                        os.close(fd)
                    try:
                        authorize()
                    except Exception:
                        return {"status": "REFUSED", "error_code": "patch_execution_authority_denied"}
                    lock.check()
                    _check_anchors(self.home, self._home_identity, anchors)
                    current = os.stat(temp_name, dir_fd=op_fd, follow_symlinks=False)
                    if _inode(current) != temp_identity or current.st_nlink != 1:
                        raise ValueError("patch_execution_temp_changed")
                    os.link(temp_name, final_name, src_dir_fd=op_fd, dst_dir_fd=op_fd, follow_symlinks=False)
                    published = True
                    self._remove_temp(op_fd, temp_name, temp_identity)
                    temp_identity = None
                    observed, _ = _read_record(op_fd, final_name)
                    if observed != checked:
                        raise ValueError("patch_execution_publication_changed")
                    full_records, full_metadata = self._operation(op_fd, run_hash, operation_hash)
                    if full_records.get(phase) != checked:
                        raise ValueError("patch_execution_publication_changed")
                    for _, _, directory_fd in reversed(anchors):
                        os.fsync(directory_fd)
                    if (full_records, full_metadata) != self._operation(op_fd, run_hash, operation_hash):
                        raise ValueError("patch_execution_publication_changed")
                    lock.check()
                    _check_anchors(self.home, self._home_identity, anchors)
                    return {"status": "CREATED", "record": observed}
                finally:
                    if temp_identity is not None:
                        self._remove_temp(op_fd, temp_name, temp_identity)
        except (OSError, ValueError, TypeError, RecursionError):
            return {"status": "UNKNOWN", "error_code": "patch_execution_publication_unknown"
                    if published else "patch_execution_storage_unknown"}


def execution_inventory(home: Path) -> dict:
    """Read-only complete two-observation census; uncertain coverage is UNKNOWN."""
    try:
        store = PatchExecutionStore(home)
        first = None
        try:
            first = store._inventory()
            if first != store._inventory():
                raise ValueError("patch_execution_inventory_changed")
            return first
        except _Absent:
            if first is not None:
                raise ValueError("patch_execution_inventory_changed")
            home_fd = _open_home(store.home)
            try:
                anchors = [(None, None, home_fd)]
                _check_anchors(store.home, store._home_identity, anchors)
                try:
                    runtime_fd = _directory_at(home_fd, ".runtime")
                except FileNotFoundError:
                    store._confirm_absent(anchors, home_fd, ".runtime")
                    runtime = None
                else:
                    try:
                        anchors.append((home_fd, ".runtime", runtime_fd))
                        runtime = _identity(os.fstat(runtime_fd), boundary=True)
                        store._confirm_absent(anchors, runtime_fd, EXECUTION_DIRECTORY)
                    finally:
                        os.close(runtime_fd)
                return {"status": "ABSENT", "operations": [], "files": {}, "directories": {},
                        "absence": {"path": ".runtime/" + EXECUTION_DIRECTORY,
                                    "home_identity": store._home_identity, "runtime_identity": runtime}}
            finally:
                os.close(home_fd)
    except (OSError, ValueError, TypeError, RecursionError):
        return {"status": "UNKNOWN", "error_code": "patch_execution_inventory_unknown"}


def _binding(value) -> dict:
    if isinstance(value, WorkerRunBinding):
        return asdict(value)
    if not isinstance(value, dict):
        raise ValueError("patch_execution_invalid_binding")
    return asdict(WorkerRunBinding(**value))


def require_execution_resolved(home: Path, run_id: str, expected_binding=None, *, authorize=None) -> None:
    """Refuse unsafe closure; an authorized caller renews all terminal evidence.

    Legacy absence or a run without execution keeps observational compatibility.
    Matching terminal evidence requires a live authority callback and lock.
    """
    census = execution_inventory(home)
    if census["status"] not in ("ABSENT", "READY"):
        raise ValueError("patch_execution_unresolved")
    if isinstance(expected_binding, WorkerRunBinding):
        expected_binding = asdict(expected_binding)
    operations = [operation for operation in census["operations"] if operation["run_id"] == run_id]
    for operation in operations:
        if (not operation["terminal"]
                or (expected_binding is not None and operation["request"]["binding"] != expected_binding)):
            raise ValueError("patch_execution_unresolved")
    if authorize is None:
        if operations:
            raise ValueError("patch_execution_unresolved")
        return
    try:
        if not callable(authorize):
            raise ValueError("patch_execution_authority_required")
        store = PatchExecutionStore(home)
        lock = _require_lock(store.home, store._home_identity)
        authorize()
        if census["status"] == "ABSENT":
            if census != execution_inventory(home):
                raise ValueError("patch_execution_inventory_changed")
            authorize()
            lock.check()
            return
        intents = journal_inventory(home)
        if intents["status"] != "READY":
            raise ValueError("patch_execution_intent_unknown")
        for operation in operations:
            store.acknowledge_existing(run_id, operation["operation_id"], operation["records"],
                authorize=authorize, expected_request=operation["request"],
                expected_binding=expected_binding)
        authorize()
        if census != execution_inventory(home) or intents != journal_inventory(home):
            raise ValueError("patch_execution_inventory_changed")
        lock.check()
    except Exception as exc:
        raise ValueError("patch_execution_unresolved") from exc
