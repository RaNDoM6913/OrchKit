"""Bounded immutable patch-intent metadata; no workspace writes or replay.

The trusted caller supplies fresh authority. Descriptor checks and advisory
creator locking do not fence a malicious same-user process or promise storage
durability beyond the local filesystem's fsync/link implementation.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable
import uuid

from .worker_operations import WorkerOperationRequest, WorkerRunBinding, WORKER_MAX_OUTPUT_BYTES


JOURNAL_DIRECTORY = "operation_journal"
JOURNAL_RECORD_MAX_BYTES = 8192
JOURNAL_MAX_RECORDS_PER_RUN = 64
JOURNAL_MAX_RECORDS = 1024
_HEX = re.compile(r"[0-9a-f]{64}", re.ASCII)
_REQUEST_KEYS = {"binding", "operation_id", "kind", "target", "max_output_bytes", "schema_version"}
_BINDING_KEYS = {"project_id", "writer_key", "task_id", "run_id", "attempt"}
_DIR_FD_PRIMITIVES = (os.open, os.stat, os.mkdir, os.link, os.unlink)
_STAT_PRIMITIVE, _LINK_PRIMITIVE = os.stat, os.link


class _Absent(FileNotFoundError):
    """A missing child observed beneath still-valid pinned ancestors."""


def _keys(value, keys) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("journal_invalid_record")


def _one(value) -> None:
    if type(value) is not int or value != 1:
        raise ValueError("journal_invalid_record")


def _digest(value) -> None:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError("journal_invalid_record")


def _size(value) -> None:
    if type(value) is not int or not 0 <= value <= WORKER_MAX_OUTPUT_BYTES:
        raise ValueError("journal_invalid_record")


def _replacement(value: dict) -> dict:
    _keys(value, {"schema_version", "baseline", "sha256", "size_bytes"})
    _one(value["schema_version"])
    _keys(value["baseline"], {"sha256", "size_bytes"})
    for item in (value, value["baseline"]):
        _digest(item["sha256"])
        _size(item["size_bytes"])
    if (value["sha256"] == value["baseline"]["sha256"]
            and value["size_bytes"] == value["baseline"]["size_bytes"]):
        raise ValueError("journal_unchanged_replacement")
    return {"schema_version": 1, "baseline": dict(value["baseline"]),
            "sha256": value["sha256"], "size_bytes": value["size_bytes"]}


def _request(value: dict) -> WorkerOperationRequest:
    _keys(value, _REQUEST_KEYS)
    _keys(value["binding"], _BINDING_KEYS)
    try:
        request = WorkerOperationRequest(**dict(value, binding=WorkerRunBinding(**value["binding"])))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("journal_invalid_record") from exc
    if request.kind != "file.patch":
        raise ValueError("journal_invalid_record")
    return request


def _encode(value: dict) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("journal_invalid_record") from exc


def request_fingerprint(request: WorkerOperationRequest, replacement_metadata: dict) -> str:
    """Bind the complete v1 request and replacement metadata, never bytes."""
    if not isinstance(request, WorkerOperationRequest):
        raise ValueError("journal_invalid_request")
    checked = _request(asdict(request))
    return hashlib.sha256(_encode({"request": asdict(checked),
                                  "replacement": _replacement(replacement_metadata)})).hexdigest()


def build_intent_record(request: WorkerOperationRequest, replacement_metadata: dict) -> dict:
    fingerprint = request_fingerprint(request, replacement_metadata)
    return validate_intent_record({
        "schema_version": 1, "state": "INTENT_RECORDED", "request": asdict(request),
        "replacement": _replacement(replacement_metadata), "fingerprint": fingerprint,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    })


def validate_intent_record(record: dict) -> dict:
    """Reject incomplete, unbound or unbounded metadata; return a fresh copy."""
    _keys(record, {"schema_version", "state", "request", "replacement", "fingerprint", "created_at"})
    _one(record["schema_version"])
    if record["state"] != "INTENT_RECORDED":
        raise ValueError("journal_invalid_record")
    request = _request(record["request"])
    replacement = _replacement(record["replacement"])
    _digest(record["fingerprint"])
    if record["fingerprint"] != request_fingerprint(request, replacement):
        raise ValueError("journal_fingerprint_mismatch")
    created = record["created_at"]
    if (not isinstance(created, str) or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", created) is None):
        raise ValueError("journal_invalid_record")
    try:
        datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError("journal_invalid_record") from exc
    result = {"schema_version": 1, "state": "INTENT_RECORDED", "request": asdict(request),
              "replacement": replacement, "fingerprint": record["fingerprint"], "created_at": created}
    if len(_encode(result)) > JOURNAL_RECORD_MAX_BYTES:
        raise ValueError("journal_record_too_large")
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("journal_invalid_json")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("journal_invalid_json")


def _decode(raw: bytes) -> dict:
    try:
        return validate_intent_record(json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                                                 parse_constant=_invalid_constant))
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ValueError("journal_invalid_json") from exc


def _id_hash(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 200
            or re.fullmatch(r"[A-Za-z0-9._-]+", value, flags=re.ASCII) is None):
        raise ValueError("journal_invalid_identifier")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _identity(info, *, boundary=False) -> dict:
    value = {"dev": info.st_dev, "ino": info.st_ino, "mode": stat.S_IMODE(info.st_mode),
             "uid": info.st_uid, "gid": info.st_gid}
    if not boundary:
        value.update(nlink=info.st_nlink, size=info.st_size,
                     mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
    return value


def _inode(info) -> tuple:
    return info.st_dev, info.st_ino


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _require_primitives() -> None:
    if (not all(getattr(os, name, 0) for name in
                ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK"))
            or any(func not in os.supports_dir_fd for func in _DIR_FD_PRIMITIVES)
            or _STAT_PRIMITIVE not in os.supports_follow_symlinks
            or _LINK_PRIMITIVE not in os.supports_follow_symlinks):
        raise ValueError("journal_unsupported")


def _check_directory(info, *, private=True) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or (private and stat.S_IMODE(info.st_mode) != 0o700)):
        raise ValueError("journal_unsafe_directory")


def _open_home(path: Path) -> int:
    """Walk every component without following symlinks; do not repair modes."""
    _require_primitives()
    if (not path.is_absolute() or str(path) != os.path.abspath(str(path))
            or path.parts[0] != "/"):
        raise ValueError("journal_invalid_home")
    fd = os.open("/", _directory_flags())
    try:
        for name in path.parts[1:]:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise ValueError("journal_unsafe_directory")
            child = os.open(name, _directory_flags(), dir_fd=fd)
            try:
                if _inode(before) != _inode(os.fstat(child)):
                    raise ValueError("journal_anchor_changed")
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        _check_directory(os.fstat(fd), private=False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _directory_at(parent: int, name: str, *, create=False) -> int:
    created = False
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _check_directory(before)
    try:
        fd = os.open(name, _directory_flags(), dir_fd=parent)
    except FileNotFoundError as exc:
        raise ValueError("journal_anchor_changed") from exc
    try:
        _check_directory(os.fstat(fd))
        if _inode(before) != _inode(os.fstat(fd)):
            raise ValueError("journal_anchor_changed")
        if created:
            os.fsync(fd)
            os.fsync(parent)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_record(parent: int, name: str, *, run_hash: str, operation_hash: str):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    def check(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("journal_unsafe_record")
        if info.st_size > JOURNAL_RECORD_MAX_BYTES:
            raise ValueError("journal_record_too_large")
    check(before)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError as exc:
        raise ValueError("journal_record_changed") from exc
    try:
        opened = os.fstat(fd)
        check(opened)
        if _identity(before) != _identity(opened):
            raise ValueError("journal_record_changed")
        raw = bytearray()
        while len(raw) <= JOURNAL_RECORD_MAX_BYTES:
            chunk = os.read(fd, min(8192, JOURNAL_RECORD_MAX_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > JOURNAL_RECORD_MAX_BYTES:
            raise ValueError("journal_record_too_large")
        after = os.fstat(fd)
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError("journal_record_changed") from exc
        check(after)
        check(current)
        if (_identity(opened) != _identity(after) or _identity(opened) != _identity(current)
                or len(raw) != opened.st_size):
            raise ValueError("journal_record_changed")
        record = _decode(bytes(raw))
        if (_id_hash(record["request"]["binding"]["run_id"]) != run_hash
                or _id_hash(record["request"]["operation_id"]) != operation_hash):
            raise ValueError("journal_identifier_mismatch")
        return record, {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                        "identity": _identity(after)}
    finally:
        os.close(fd)


def _names(fd: int, limit: int) -> list:
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > limit:
                raise ValueError("journal_capacity_exceeded")
    return sorted(names)


class OperationIntentJournal:
    """Run-scoped immutable store; each operation reopens and checks anchors."""

    def __init__(self, home: Path):
        self.home = Path(home)
        try:
            fd = _open_home(self.home)
            try:
                self._home_identity = _identity(os.fstat(fd), boundary=True)
            finally:
                os.close(fd)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("journal_home_denied") from exc

    @contextmanager
    def _anchors(self, *, create=False, run_hash=None):
        with ExitStack() as owned:
            try:
                home = _open_home(self.home)
            except FileNotFoundError as exc:
                raise ValueError("journal_anchor_changed") from exc
            owned.callback(os.close, home)
            if _identity(os.fstat(home), boundary=True) != self._home_identity:
                raise ValueError("journal_anchor_changed")
            anchors = [(None, None, home)]
            parent = home
            for name in (".runtime", JOURNAL_DIRECTORY):
                try:
                    child = _directory_at(parent, name, create=create)
                except FileNotFoundError as exc:
                    self._confirm_absent(anchors, parent, name)
                    raise _Absent() from exc
                owned.callback(os.close, child)
                anchors.append((parent, name, child))
                parent = child
            if run_hash is not None:
                try:
                    child = _directory_at(parent, run_hash, create=create)
                except FileNotFoundError as exc:
                    self._confirm_absent(anchors, parent, run_hash)
                    raise _Absent() from exc
                owned.callback(os.close, child)
                anchors.append((parent, run_hash, child))
            self._check_anchors(anchors)
            yield anchors
            self._check_anchors(anchors)

    def _confirm_absent(self, anchors, parent, name) -> None:
        self._check_anchors(anchors)
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            self._check_anchors(anchors)
            return
        raise ValueError("journal_absence_changed")

    def _check_anchors(self, anchors) -> None:
        home = _open_home(self.home)
        try:
            if (_identity(os.fstat(home), boundary=True) != self._home_identity
                    or _identity(os.fstat(anchors[0][2]), boundary=True) != self._home_identity):
                raise ValueError("journal_anchor_changed")
        finally:
            os.close(home)
        for parent, name, child in anchors[1:]:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            pinned = os.fstat(child)
            _check_directory(current)
            _check_directory(pinned)
            if _inode(current) != _inode(pinned):
                raise ValueError("journal_anchor_changed")

    def inspect(self, run_id: str, operation_id: str) -> dict:
        try:
            run_hash, operation_hash = _id_hash(run_id), _id_hash(operation_id)
            with self._anchors(run_hash=run_hash) as anchors:
                try:
                    record, _ = _read_record(anchors[-1][2], operation_hash + ".json",
                                             run_hash=run_hash, operation_hash=operation_hash)
                except FileNotFoundError as exc:
                    self._confirm_absent(anchors, anchors[-1][2], operation_hash + ".json")
                    raise _Absent() from exc
                self._check_anchors(anchors)
            return {"status": "RECORDED", "record": record}
        except _Absent:
            return {"status": "ABSENT"}
        except (OSError, ValueError, TypeError):
            return {"status": "UNKNOWN", "error_code": "journal_inspection_unknown"}

    def _inventory(self) -> dict:
        files, directories, records = {}, {}, []
        prefix = ".runtime/" + JOURNAL_DIRECTORY
        with self._anchors() as anchors:
            journal_fd = anchors[-1][2]
            journal_before = _identity(os.fstat(journal_fd))
            directories[prefix] = journal_before
            run_names = _names(journal_fd, JOURNAL_MAX_RECORDS)
            for run_hash in run_names:
                if _HEX.fullmatch(run_hash) is None:
                    raise ValueError("journal_unsafe_entry")
                run_fd = _directory_at(journal_fd, run_hash)
                try:
                    run_before = _identity(os.fstat(run_fd))
                    directories[prefix + "/" + run_hash] = run_before
                    names = _names(run_fd, JOURNAL_MAX_RECORDS_PER_RUN)
                    for name in names:
                        if len(name) != 69 or not name.endswith(".json") or _HEX.fullmatch(name[:-5]) is None:
                            raise ValueError("journal_unsafe_entry")
                        record, metadata = _read_record(run_fd, name, run_hash=run_hash, operation_hash=name[:-5])
                        records.append(record)
                        files[prefix + "/" + run_hash + "/" + name] = metadata
                        if len(records) > JOURNAL_MAX_RECORDS:
                            raise ValueError("journal_capacity_exceeded")
                    if (names != _names(run_fd, JOURNAL_MAX_RECORDS_PER_RUN)
                            or run_before != _identity(os.fstat(run_fd))
                            or _identity(os.stat(run_hash, dir_fd=journal_fd, follow_symlinks=False)) != run_before):
                        raise ValueError("journal_inventory_changed")
                finally:
                    os.close(run_fd)
            if (run_names != _names(journal_fd, JOURNAL_MAX_RECORDS)
                    or journal_before != _identity(os.fstat(journal_fd))):
                raise ValueError("journal_inventory_changed")
            self._check_anchors(anchors)
            boundary = {"home_identity": self._home_identity,
                        "runtime_identity": _identity(os.fstat(anchors[1][2]), boundary=True)}
        return {"status": "READY", "records": records, "files": files,
                "directories": directories, "boundary": boundary}

    def create(self, record: dict, *, authorize: Callable[[], None]) -> dict:
        """Publish exactly once; a lost acknowledgement remains UNKNOWN."""
        try:
            checked = validate_intent_record(record)
            if not callable(authorize):
                raise ValueError("journal_authority_required")
        except (ValueError, TypeError):
            return {"status": "REFUSED", "error_code": "journal_invalid_intent"}
        try:
            authorize()
        except Exception:
            return {"status": "REFUSED", "error_code": "journal_authority_denied"}
        run_hash = _id_hash(checked["request"]["binding"]["run_id"])
        operation_hash = _id_hash(checked["request"]["operation_id"])
        final_name = operation_hash + ".json"
        temp_name = ".intent-" + uuid.uuid4().hex + ".tmp"
        temp_identity = None
        published = False
        try:
            with self._anchors(create=True) as anchors:
                journal_fd = anchors[-1][2]
                try:
                    fcntl.flock(journal_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"status": "UNKNOWN", "error_code": "journal_busy"}
                try:
                    census = self._inventory()
                    if census != self._inventory():
                        raise ValueError("journal_inventory_changed")
                    # Look up before capacity refusal: full stores retain known duplicates.
                    existing = self.inspect(checked["request"]["binding"]["run_id"],
                                            checked["request"]["operation_id"])
                    if existing["status"] == "RECORDED":
                        previous = existing["record"]
                        if previous["fingerprint"] == checked["fingerprint"]:
                            return {"status": "EXISTING", "record": previous}
                        return {"status": "CONFLICT", "error_code": "journal_operation_conflict"}
                    if existing["status"] != "ABSENT":
                        raise ValueError("journal_existing_unknown")
                    run_count = sum(item["request"]["binding"]["run_id"] == checked["request"]["binding"]["run_id"]
                                    for item in census["records"])
                    if (run_count >= JOURNAL_MAX_RECORDS_PER_RUN
                            or len(census["records"]) >= JOURNAL_MAX_RECORDS
                            or (".runtime/" + JOURNAL_DIRECTORY + "/" + run_hash not in census["directories"]
                                and len(census["directories"]) - 1 >= JOURNAL_MAX_RECORDS)):
                        return {"status": "REFUSED", "error_code": "journal_capacity_exceeded"}
                    run_fd = _directory_at(journal_fd, run_hash, create=True)
                    anchors.append((journal_fd, run_hash, run_fd))
                    try:
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                        fd = os.open(temp_name, flags, 0o600, dir_fd=run_fd)
                        try:
                            os.fchmod(fd, 0o600)
                            temp_identity = _inode(os.fstat(fd))
                            raw = _encode(checked)
                            offset = 0
                            while offset < len(raw):
                                written = os.write(fd, raw[offset:])
                                if written <= 0:
                                    raise OSError("short journal write")
                                offset += written
                            os.fsync(fd)
                            info = os.fstat(fd)
                            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                                    or _inode(info) != temp_identity or info.st_size != len(raw)):
                                raise ValueError("journal_temp_changed")
                        finally:
                            os.close(fd)
                        try:
                            authorize()
                        except Exception:
                            return {"status": "REFUSED", "error_code": "journal_authority_denied"}
                        self._check_anchors(anchors)
                        current = os.stat(temp_name, dir_fd=run_fd, follow_symlinks=False)
                        if _inode(current) != temp_identity or current.st_nlink != 1:
                            raise ValueError("journal_temp_changed")
                        try:
                            os.link(temp_name, final_name, src_dir_fd=run_fd, dst_dir_fd=run_fd,
                                    follow_symlinks=False)
                            published = True
                        except FileExistsError:
                            # Only remove our own alias before inspecting a collision.
                            self._remove_temp(run_fd, temp_name, temp_identity)
                            temp_identity = None
                            observed, _ = _read_record(run_fd, final_name, run_hash=run_hash,
                                                       operation_hash=operation_hash)
                            self._check_anchors(anchors)
                            if observed["fingerprint"] == checked["fingerprint"]:
                                return {"status": "EXISTING", "record": observed}
                            return {"status": "CONFLICT", "error_code": "journal_operation_conflict"}
                        self._remove_temp(run_fd, temp_name, temp_identity)
                        temp_identity = None
                        observed, _ = _read_record(run_fd, final_name, run_hash=run_hash,
                                                   operation_hash=operation_hash)
                        if observed != checked:
                            raise ValueError("journal_publication_changed")
                        self._check_anchors(anchors)
                        # Sync every pinned parent too: a previous uncertain
                        # mkdir must not make a later CREATED acknowledgement
                        # depend on an unsynced ancestor surviving a crash.
                        for _, _, directory_fd in reversed(anchors):
                            os.fsync(directory_fd)
                        self._check_anchors(anchors)
                        return {"status": "CREATED", "record": observed}
                    finally:
                        try:
                            if temp_identity is not None:
                                self._remove_temp(run_fd, temp_name, temp_identity)
                        finally:
                            anchors.pop()
                            os.close(run_fd)
                finally:
                    fcntl.flock(journal_fd, fcntl.LOCK_UN)
        except (OSError, ValueError, TypeError):
            return {"status": "UNKNOWN", "error_code":
                    "journal_publication_unknown" if published else "journal_storage_unknown"}

    @staticmethod
    def _remove_temp(parent: int, name: str, owned_identity) -> None:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or _inode(current) != owned_identity:
            raise ValueError("journal_temp_changed")
        os.unlink(name, dir_fd=parent)


def journal_inventory(home: Path) -> dict:
    """Complete bounded read-only census, never partial success or cleanup."""
    try:
        store = OperationIntentJournal(home)
        first = None
        try:
            first = store._inventory()
            if first != store._inventory():
                raise ValueError("journal_inventory_changed")
            return first
        except _Absent:
            if first is not None:
                raise ValueError("journal_inventory_changed")
            home_fd = _open_home(store.home)
            try:
                if _identity(os.fstat(home_fd), boundary=True) != store._home_identity:
                    raise ValueError("journal_anchor_changed")
                try:
                    runtime_fd = _directory_at(home_fd, ".runtime")
                except FileNotFoundError:
                    store._confirm_absent([(None, None, home_fd)], home_fd, ".runtime")
                    runtime = None
                else:
                    try:
                        runtime = _identity(os.fstat(runtime_fd), boundary=True)
                        try:
                            os.stat(JOURNAL_DIRECTORY, dir_fd=runtime_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            store._confirm_absent([(None, None, home_fd), (home_fd, ".runtime", runtime_fd)],
                                                  runtime_fd, JOURNAL_DIRECTORY)
                        else:
                            raise ValueError("journal_inventory_changed")
                    finally:
                        os.close(runtime_fd)
                return {"status": "ABSENT", "records": [], "files": {}, "directories": {},
                        "absence": {"path": ".runtime/" + JOURNAL_DIRECTORY,
                                    "home_identity": store._home_identity, "runtime_identity": runtime}}
            finally:
                os.close(home_fd)
    except (OSError, ValueError, TypeError):
        return {"status": "UNKNOWN", "error_code": "journal_inventory_unknown"}
