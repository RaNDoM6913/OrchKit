"""Bounded trusted-local direct children; no shell, PID recovery or OS sandbox.

Operator profiles must avoid descendants and secret output. A frozen executable
observation is a cooperative same-user check, not protection against hostile
path replacement between the last observation and the operating system's exec.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
from types import MappingProxyType
from typing import Mapping

from .bridge_authorization import LocalRunSessionAuthority
from .bridge_patch_execution import runtime_operation_lock
from .bridge_process_store import (
    DEFAULT_MAX_PROCESSES_PER_RUN, DEFAULT_MAX_PROCESSES_TOTAL, ProcessOperationStore,
)
from .config import read_bounded_json_object
from .project import PROJECT_CONFIG_MAX_BYTES, ProjectRegistry
from .worker_operations import (
    WORKER_MAX_OUTPUT_BYTES, WorkerOperationOutcome, WorkerOperationRequest,
    WorkerRunBinding, require_matching_worker_run, validate_worker_outcome,
)


_SAFE_ENV = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TZ", "PATH",
                       "PYTHONIOENCODING", "PYTHONUNBUFFERED"})
_MAX_CACHE = 64
_READ_BYTES = 8192
_SIGNAL_GRACE = .2
_REAP_GRACE = .5
_EOF_GRACE = .5


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _canonical(value, *, directory: bool) -> Path:
    path = Path(value)
    if (not path.is_absolute() or str(path) != os.path.abspath(str(path))
            or str(path) != os.path.realpath(str(path))):
        raise ValueError("process_profile_unsafe_path")
    info = os.stat(path, follow_symlinks=False)
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("process_profile_unsafe_type")
    return path


def _executable_observation(path: Path) -> tuple[tuple[int, ...], str]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111
                or not 0 < before.st_size <= 128 * 1024 * 1024):
            raise ValueError("process_profile_unsafe_executable")
        def signature(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            digest.update(chunk)
        if (signature(before) != signature(os.fstat(fd))
                or signature(before) != signature(os.stat(path, follow_symlinks=False))):
            raise ValueError("process_profile_executable_changed")
        return signature(before), digest.hexdigest()
    finally:
        os.close(fd)


@dataclass(frozen=True)
class OwnedProcessProfile:
    """Immutable operator-owned argv/environment and pinned local identities."""

    profile_id: str
    executable: Path
    args: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    deadline_sec: float
    max_output_bytes: int
    _executable_identity: tuple[int, ...] = field(init=False, repr=False)
    _executable_digest: str = field(init=False, repr=False)
    _cwd_identity: tuple[int, int] = field(init=False, repr=False)
    profile_hash: str = field(init=False)

    def __post_init__(self):
        if (not isinstance(self.profile_id, str) or len(self.profile_id) > 200
                or re.fullmatch(r"[A-Za-z0-9._-]+", self.profile_id, re.ASCII) is None):
            raise ValueError("process_profile_invalid_id")
        if (isinstance(self.deadline_sec, bool)
                or not isinstance(self.deadline_sec, (int, float))
                or not math.isfinite(self.deadline_sec)
                or not .05 <= self.deadline_sec <= 600):
            raise ValueError("process_profile_invalid_deadline")
        if (type(self.max_output_bytes) is not int
                or not 1 <= self.max_output_bytes <= WORKER_MAX_OUTPUT_BYTES):
            raise ValueError("process_profile_invalid_output_limit")
        if not isinstance(self.args, (tuple, list)):
            raise ValueError("process_profile_invalid_args")
        args = tuple(self.args)
        if (len(args) > 64 or any(not isinstance(arg, str) or "\0" in arg for arg in args)
                or sum(len(arg.encode("utf-8")) for arg in args) > 8192):
            raise ValueError("process_profile_invalid_args")
        if not isinstance(self.env, Mapping) or len(self.env) > len(_SAFE_ENV):
            raise ValueError("process_profile_invalid_environment")
        env = dict(self.env)
        if any(key not in _SAFE_ENV or not isinstance(value, str)
               or "\0" in value or len(value.encode("utf-8")) > 1024
               for key, value in env.items()):
            raise ValueError("process_profile_invalid_environment")
        executable, cwd = _canonical(self.executable, directory=False), _canonical(self.cwd, directory=True)
        signature, digest = _executable_observation(executable)
        cwd_identity = _identity(os.stat(cwd, follow_symlinks=False))
        for key, value in (("executable", executable), ("cwd", cwd),
                           ("args", args), ("env", MappingProxyType(env)),
                           ("_executable_identity", signature),
                           ("_executable_digest", digest), ("_cwd_identity", cwd_identity)):
            object.__setattr__(self, key, value)
        object.__setattr__(self, "profile_hash", hashlib.sha256(_json({
            "profile_id": self.profile_id, "executable": str(executable),
            "executable_identity": signature, "executable_sha256": digest,
            "args": args, "cwd": str(cwd), "cwd_identity": cwd_identity,
            "env": env, "deadline_sec": self.deadline_sec,
            "max_output_bytes": self.max_output_bytes,
        })).hexdigest())

    def _recheck(self, root: Path):
        if (self.cwd != root or _canonical(self.cwd, directory=True) != root
                or _identity(os.stat(self.cwd, follow_symlinks=False)) != self._cwd_identity
                or _canonical(self.executable, directory=False) != self.executable
                or _executable_observation(self.executable)
                != (self._executable_identity, self._executable_digest)):
            raise ValueError("process_profile_changed")


def _spawn_process(profile: OwnedProcessProfile):
    return subprocess.Popen(
        [str(profile.executable), *profile.args], cwd=profile.cwd,
        env=dict(profile.env), shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True,
    )


class _OwnedChild:
    def __init__(self, process, profile, intent, deadline):
        self.process, self.profile, self.intent = process, profile, intent
        self.deadline = deadline
        self.lock = threading.RLock()
        self.output = bytearray()
        self.total = 0
        self.exit_code = None
        self.reason = None
        self.stop_reason = None
        self.settled = self.unknown = self.recorded = self.record_attempted = False
        self.terminate_reserved = self.terminal_reserved = True
        self.thread = None

    def stop(self, reason):
        with self.lock:
            if not self.settled and self.stop_reason is None:
                self.stop_reason = reason


def _body(handle, state, exit_code, reason, cursor, data, total, retained,
          truncated, has_more):
    return _json({
        "schema_version": 1, "handle": handle, "process_state": state,
        "exit_code": exit_code, "reason": reason, "cursor": cursor,
        "next_cursor": cursor + len(data),
        "output_base64": base64.b64encode(data).decode("ascii"),
        "total_output_bytes": total, "retained_output_bytes": retained,
        "truncated": truncated, "has_more": has_more,
    })


def _minimum_fits(request, handle):
    # Reserve the largest fixed terminal metadata before any spawn/signal.
    return len(_body(handle, "RUNNING", -2147483648, "owner_cleanup", 65536,
                     b"", 9223372036854775807, 65536, False, False)) <= request.max_output_bytes


class LocalOwnedProcessOperations:
    """One local owner's bounded Popen objects and non-evicting observations.

    Normal calls require fresh full run/session authority under the native
    runtime lock. Cleanup uses only this owner's retained Popen objects after
    authority loss; restart has no process discovery or signal capability.
    """

    def __init__(self, authority: LocalRunSessionAuthority, session_id: str,
                 binding: WorkerRunBinding, capability_file: Path, profiles, *,
                 max_processes_total: int = DEFAULT_MAX_PROCESSES_TOTAL,
                 max_processes_per_run: int = DEFAULT_MAX_PROCESSES_PER_RUN,
                 process_store: ProcessOperationStore | None = None):
        if not isinstance(authority, LocalRunSessionAuthority):
            raise ValueError("process_invalid_authority")
        self._authority, self._session, self._binding = authority, session_id, binding
        self._capability = capability_file
        self._orch = authority._orch
        if process_store is None:
            self._store = ProcessOperationStore(
                self._orch, max_processes_total=max_processes_total,
                max_processes_per_run=max_processes_per_run,
            )
        else:
            if not isinstance(process_store, ProcessOperationStore):
                raise ValueError("process_invalid_store")
            process_store.require_authority(self._orch)
            self._store = process_store
        self._owner_pid = os.getpid()
        self._lock = threading.RLock()
        self._children, self._outcomes = {}, {}
        self._closed = False
        profiles = tuple(profiles)
        if (not 1 <= len(profiles) <= 64
                or any(not isinstance(item, OwnedProcessProfile) for item in profiles)
                or len({item.profile_id for item in profiles}) != len(profiles)):
            raise ValueError("process_invalid_profiles")
        self._profiles = {item.profile_id: item for item in profiles}
        self._session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        with runtime_operation_lock(self._orch.root):
            self._root = self._live()
            for item in profiles:
                item._recheck(self._root)

    def _live(self):
        if self._owner_pid != os.getpid() or self._closed:
            raise ValueError("process_owner_unavailable")
        self._authority.require_session(self._session, self._binding, self._capability)
        registry = ProjectRegistry.open_readonly(self._orch.root)
        project, meta = read_bounded_json_object(
            registry.projects_dir / (self._binding.project_id + ".json"),
            max_bytes=PROJECT_CONFIG_MAX_BYTES, unsafe_error="process_project_denied",
            too_large_error="process_project_denied", invalid_error="process_project_denied",
        )
        root = _canonical(project["root"], directory=True)
        if meta["mode"] != 0o600 or (hasattr(self, "_root") and self._root != root):
            raise ValueError("process_project_denied")
        return root

    def _outcome(self, request, state, code):
        return WorkerOperationOutcome(request.operation_id, state, error_code=code)

    def _cache(self, request, cursor, outcome):
        self._outcomes[request.operation_id] = (request, cursor, outcome)
        return outcome

    def _occupied(self):
        # Reservations share the same bound as cached identities. Background
        # exit does not release them; only a delivered matching reply does.
        return len(self._outcomes) + sum(
            child.terminate_reserved + child.terminal_reserved
            for child in self._children.values())

    def start(self, request):
        return self._operate(request, "process.start", 0)

    def poll(self, request, *, cursor=0):
        return self._operate(request, "process.poll", cursor)

    def terminate(self, request):
        return self._operate(request, "process.terminate", 0)

    def _operate(self, request, kind, cursor):
        if not isinstance(request, WorkerOperationRequest):
            raise ValueError("process_invalid_request")
        if request.kind != kind or type(cursor) is not int or cursor < 0:
            return self._outcome(request, "REFUSED", "process_request_denied")
        try:
            require_matching_worker_run(self._binding, request)
        except ValueError:
            return self._outcome(request, "REFUSED", "process_scope_mismatch")
        with self._lock:
            try:
                with runtime_operation_lock(self._orch.root):
                    try:
                        root = self._live()
                    except (OSError, ValueError, TypeError, KeyError):
                        self._cleanup_requests()
                        return self._outcome(request, "REFUSED", "process_authority_denied")
                    cached = self._outcomes.get(request.operation_id)
                    if cached is not None:
                        if cached[:2] != (request, cursor):
                            return self._outcome(request, "REFUSED", "process_operation_conflict")
                        return cached[2]
                    if kind == "process.start":
                        # The reply and both future lifecycle replies must fit
                        # before any durable dispatch or owned-child creation.
                        if self._occupied() + 3 > _MAX_CACHE:
                            return self._outcome(request, "REFUSED", "process_operation_capacity")
                        outcome = self._start(request, root)
                    else:
                        terminate = kind == "process.terminate"
                        child = self._children.get(request.target)
                        ordinary = self._occupied() < _MAX_CACHE
                        reserved = child is not None and (
                            child.terminate_reserved if terminate else child.terminal_reserved)
                        if not ordinary and not reserved:
                            return self._outcome(request, "REFUSED", "process_operation_capacity")
                        outcome = self._observe(request, cursor, terminate=terminate,
                                                reserved_only=not ordinary)
                        if child is not None and outcome.state == "SUCCEEDED":
                            if terminate:
                                child.terminate_reserved = False
                            elif json.loads(outcome.output)["process_state"] == "EXITED":
                                child.terminal_reserved = False
                        elif not ordinary:
                            # Invalid/unknown replies cannot consume a child's
                            # safety slot or grow a cache of rejected IDs.
                            return outcome
                    return self._cache(request, cursor, outcome)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                return self._outcome(request, "REFUSED", "process_runtime_denied")

    def _start(self, request, root):
        profile = self._profiles.get(request.target)
        if profile is None:
            return self._outcome(request, "REFUSED", "process_profile_denied")
        profile._recheck(root)
        records = self._store.intents(self._binding)
        old = next((item for item in records if item["request"]["operation_id"] == request.operation_id), None)
        if old is not None:
            if (old["request"] != asdict(request) or old["session_hash"] != self._session_hash
                    or old["profile_hash"] != profile.profile_hash):
                return self._outcome(request, "REFUSED", "process_operation_conflict")
            return self._outcome(request, "UNKNOWN", "process_dispatch_already_recorded")
        handle = "proc-" + secrets.token_hex(16)
        if not _minimum_fits(request, handle):
            return self._outcome(request, "REFUSED", "process_envelope_too_small")
        try:
            intent = self._store.dispatch(request, self._session_hash, profile.profile_hash, handle)
        except ValueError as exc:
            if str(exc) == "process_capacity":
                return self._outcome(request, "REFUSED", "process_dispatch_capacity")
            return self._outcome(request, "UNKNOWN", "process_dispatch_unconfirmed")
        except (OSError, sqlite3.Error):
            # A failed return can follow a committed intent. Never spawn here.
            return self._outcome(request, "UNKNOWN", "process_dispatch_unconfirmed")
        try:
            profile._recheck(self._live())
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            self._cleanup_requests()
            return self._outcome(request, "UNKNOWN", "process_spawn_authority_unconfirmed")
        deadline = time.monotonic() + profile.deadline_sec
        try:
            process = _spawn_process(profile)
        except (OSError, ValueError, subprocess.SubprocessError):
            return self._outcome(request, "UNKNOWN", "process_spawn_unconfirmed")
        child = _OwnedChild(process, profile, intent, deadline)
        self._children[handle] = child
        try:
            child.thread = threading.Thread(target=self._supervise, args=(child,), daemon=True,
                                            name="orch-owned-child")
            child.thread.start()
        except (RuntimeError, OSError):
            # Ownership is retained even when launching the supervisor fails.
            with child.lock:
                child.unknown = True
                try:
                    process.kill()
                    process.wait(timeout=_REAP_GRACE)
                except (OSError, subprocess.SubprocessError):
                    pass
            return self._outcome(request, "UNKNOWN", "process_supervisor_unavailable")
        return self._envelope(request, handle, child, 0)

    def _observe(self, request, cursor, *, terminate, reserved_only=False):
        child = self._children.get(request.target)
        if child is None:
            records = self._store.intents(self._binding)
            old = next((item for item in records if item["handle"] == request.target), None)
            if old is not None and old["session_hash"] == self._session_hash:
                return self._outcome(request, "UNKNOWN", "process_owner_unavailable")
            return self._outcome(request, "REFUSED", "process_handle_denied")
        if not _minimum_fits(request, request.target):
            return self._outcome(request, "REFUSED", "process_envelope_too_small")
        with child.lock:
            if cursor > len(child.output):
                return self._outcome(request, "REFUSED", "process_cursor_denied")
        self._record_exit(child)
        with child.lock:
            if reserved_only and (child.unknown or (
                    not terminate and not (child.settled and child.recorded))):
                return self._outcome(request, "REFUSED", "process_operation_capacity")
            if terminate and not child.settled and child.stop_reason is None:
                child.stop_reason = "terminated"
            # Preserve identity for an admitted stop request even if the
            # supervisor subsequently loses exit proof. No RUNNING poll may
            # spend the terminal slot, and a signal is never terminal proof.
            return self._envelope(request, request.target, child, cursor)

    def _envelope(self, request, handle, child, cursor):
        with child.lock:
            if child.unknown:
                return self._outcome(request, "UNKNOWN", "process_exit_unconfirmed")
            data = bytes(child.output[cursor:])
            def encode(count):
                return _body(handle, "EXITED" if child.settled and child.recorded else "RUNNING",
                             child.exit_code if child.settled and child.recorded else None,
                             child.reason, cursor, data[:count], child.total,
                             len(child.output), child.total > len(child.output), count < len(data))
            low, high = 0, len(data)
            if len(encode(0)) > request.max_output_bytes:
                return self._outcome(request, "REFUSED", "process_envelope_too_small")
            while low < high:
                mid = (low + high + 1) // 2
                if len(encode(mid)) <= request.max_output_bytes:
                    low = mid
                else:
                    high = mid - 1
            return validate_worker_outcome(request, WorkerOperationOutcome(
                request.operation_id, "SUCCEEDED", output=encode(low)))

    def _record_exit(self, child):
        # The caller holds runtime_operation_lock. Claim the one append before
        # releasing child.lock; even an ambiguous store return cannot be retried.
        with child.lock:
            if not child.settled or child.unknown or child.record_attempted:
                return
            child.record_attempted = True
            values = (child.exit_code, child.reason, child.total,
                      child.total > len(child.output))
        try:
            self._store.observe_exit(child.intent, *values)
        except (OSError, ValueError, sqlite3.Error):
            with child.lock:
                child.unknown = True
        else:
            with child.lock:
                child.recorded = True

    def _supervise(self, child):
        process = child.process
        stream = process.stdout
        reaped_at = signaled_at = killed_at = None
        eof = False
        try:
            fd = stream.fileno()
            os.set_blocking(fd, False)
            while True:
                now = time.monotonic()
                with child.lock:
                    # All Popen wait/poll/signal calls share this same lock.
                    code = process.poll()
                    if code is None:
                        if now >= child.deadline and child.stop_reason is None:
                            child.stop_reason = "deadline"
                        if child.stop_reason and signaled_at is None:
                            child.reason = child.stop_reason
                            process.terminate()
                            signaled_at = now
                        if signaled_at is not None and now - signaled_at >= _SIGNAL_GRACE and killed_at is None:
                            process.kill()
                            killed_at = now
                        if killed_at is not None and now - killed_at >= _REAP_GRACE:
                            child.unknown = True
                            return
                    else:
                        child.exit_code = code
                        child.reason = child.reason or "exited"
                        reaped_at = reaped_at or now
                        if eof:
                            child.settled = True
                            break
                        if now - reaped_at >= _EOF_GRACE:
                            child.unknown = True
                            return
                if not eof and select.select([fd], [], [], .01)[0]:
                    chunk = os.read(fd, _READ_BYTES)
                    with child.lock:
                        if chunk:
                            child.total += len(chunk)
                            available = child.profile.max_output_bytes - len(child.output)
                            child.output.extend(chunk[:available])
                        else:
                            eof = True
                elif eof:
                    time.sleep(.01)
        except (OSError, ValueError):
            with child.lock:
                child.unknown = True
                try:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=_REAP_GRACE)
                except (OSError, subprocess.SubprocessError):
                    pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    with child.lock:
                        child.unknown = True
        # Busy lock acquisition has no write effect and may be retried. The
        # store append itself is attempted once, including during live cleanup.
        expires = time.monotonic() + 1.0
        while True:
            try:
                # Match normal-call lock order; the supervisor never competes
                # with its own caller for the nonblocking native runtime lock.
                with self._lock:
                    with runtime_operation_lock(self._orch.root):
                        self._record_exit(child)
                return
            except ValueError as exc:
                if str(exc) != "patch_execution_busy" or time.monotonic() >= expires:
                    with child.lock:
                        child.unknown = True
                    return
                time.sleep(.01)
            except OSError:
                with child.lock:
                    child.unknown = True
                return

    def _cleanup_requests(self):
        if self._owner_pid == os.getpid():
            for child in self._children.values():
                child.stop("owner_cleanup")

    def close(self):
        """Bounded cleanup by this Popen owner; never discovers a saved PID."""
        with self._lock:
            if self._owner_pid != os.getpid():
                return
            self._closed = True
            self._cleanup_requests()
            children = tuple(self._children.values())
        cleanup_unresolved = False
        for child in children:
            if child.thread is not None and child.thread.ident is not None:
                child.thread.join(timeout=1.5)
            with child.lock:
                try:
                    if child.process.poll() is None:
                        child.process.kill()
                        child.process.wait(timeout=_REAP_GRACE)
                except (OSError, subprocess.SubprocessError):
                    child.unknown = True
                    cleanup_unresolved = True
                if child.thread is None or child.thread.ident is None:
                    if child.process.stdout is not None:
                        child.process.stdout.close()
                elif child.thread.is_alive():
                    cleanup_unresolved = True
        if cleanup_unresolved:
            raise ValueError("process_cleanup_unresolved")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
