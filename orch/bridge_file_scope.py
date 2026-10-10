"""Descriptor-anchored, read-only file-scope foundation for a local Bridge.

Only existing regular-file file.read targets can acquire a descriptor.
No contents are transferred, no worker adapter executes, and no permission
survives outside the returned context. Not an OS sandbox or same-user fence.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from .bridge_authorization import LocalRunSessionAuthority
from .config import read_bounded_json_object
from .core import path_allowed
from .project import (
    PROJECT_CONFIG_MAX_BYTES, ProjectRegistry, project_root_identity_matches,
    validate_project_schema,
)
from .worker_operations import (
    WorkerOperationRequest, WorkerRunBinding, require_matching_worker_run,
)


def _require_primitives() -> None:
    """Refuse platforms unable to anchor every component to a descriptor."""
    if (not all(getattr(os, name, 0) for name in
                ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks):
        raise ValueError("bridge_file_scope_unsupported")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _entry(parent_fd: int, name: str, *, directory: bool) -> os.stat_result:
    """Precheck type/symlink; fstat and identity rechecks remain mandatory."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("bridge_file_scope_unsafe_type")
    return info


def _open_registered_root(root: Path) -> int:
    """Walk from / without following symlinks even in root's ancestors."""
    if (not root.is_absolute() or str(root) != os.path.abspath(str(root))
            or os.path.realpath(str(root)) != str(root)
            or not root.parts or root.parts[0] != "/"):
        raise ValueError("bridge_file_scope_noncanonical_root")
    fd = os.open("/", _directory_flags())
    try:
        for part in root.parts[1:]:
            _entry(fd, part, directory=True)
            nxt = os.open(part, _directory_flags(), dir_fd=fd)
            try:
                if not stat.S_ISDIR(os.fstat(nxt).st_mode):
                    raise ValueError("bridge_file_scope_unsafe_type")
            except BaseException:
                os.close(nxt)
                raise
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


class LocalFileScope:
    """Own a pinned root descriptor for one volatile run/session.

    Construct only within trusted local OrchKit code. The root fd belongs to
    this object and must be closed. Each yielded target fd belongs to its
    open_readonly context and is closed even if caller code raises.
    Do not share instances across threads or fork boundaries.
    """

    def __init__(
        self, authority: LocalRunSessionAuthority, session_id: str,
        binding: WorkerRunBinding, capability_file: Path,
    ):
        if not isinstance(authority, LocalRunSessionAuthority):
            raise ValueError("bridge_file_scope_invalid_authority")
        _require_primitives()
        self._authority = authority
        self._session_id = session_id
        self._binding = binding
        self._capability_file = capability_file
        self._root_fd = -1
        self._active_handles = 0
        root, _ = self._live_scope()
        try:
            self._root_fd = _open_registered_root(root)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("bridge_file_scope_root_denied") from exc
        self._root_path = root
        try:
            self._root_identity = _identity(os.fstat(self._root_fd))
            self._check_root_identity()
        except BaseException:
            self.close()
            raise

    def _live_scope(self) -> tuple[Path, list[str]]:
        # Existing ledger/capability/session decision is authoritative.
        self._authority.require_session(
            self._session_id, self._binding, self._capability_file,
        )
        orch = self._authority._orch
        registry = ProjectRegistry.open_readonly(orch.root)
        project, meta = read_bounded_json_object(
            registry.projects_dir / (self._binding.project_id + ".json"),
            max_bytes=PROJECT_CONFIG_MAX_BYTES,
            unsafe_error="project_config_unsafe",
            too_large_error="project_config_too_large",
            invalid_error="project_config_invalid_json",
        )
        validate_project_schema(project)
        root = Path(project["root"])
        if (meta["mode"] != 0o600 or project["project_id"] != self._binding.project_id
                or project["writer_key"] != self._binding.writer_key
                or not project_root_identity_matches(self._binding.project_id, root)):
            raise ValueError("bridge_file_scope_project_denied")
        with orch.connect() as conn:
            row = conn.execute(
                "SELECT r.state AS run_state, t.status AS task_state, "
                "t.project_id, t.writer_key, t.payload_json "
                "FROM runs r JOIN tasks t ON r.task_id=t.task_id "
                "WHERE r.run_id=? AND t.task_id=? AND r.attempt=?",
                (self._binding.run_id, self._binding.task_id, self._binding.attempt),
            ).fetchone()
        if (row is None or row["run_state"] != "RUNNING"
                or row["task_state"] != "IN_PROGRESS"
                or row["project_id"] != self._binding.project_id
                or row["writer_key"] != self._binding.writer_key):
            raise ValueError("bridge_file_scope_run_denied")
        payload = json.loads(row["payload_json"])
        allowed = payload.get("allowed_paths")
        if (payload.get("workspace") != str(root)
                or not isinstance(allowed, list)
                or not allowed
                or any(not isinstance(item, str) for item in allowed)):
            raise ValueError("bridge_file_scope_task_denied")
        return root, allowed

    def _check_root_identity(self) -> None:
        if self._root_fd < 0:
            raise ValueError("bridge_file_scope_closed")
        _require_primitives()
        # New lookup must still designate the pinned inode.
        try:
            current = _open_registered_root(self._root_path)
            try:
                if (_identity(os.fstat(current)) != self._root_identity
                        or _identity(os.fstat(self._root_fd)) != self._root_identity):
                    raise ValueError("bridge_file_scope_root_changed")
            finally:
                os.close(current)
        except (OSError, TypeError) as exc:
            raise ValueError("bridge_file_scope_root_denied") from exc

    @contextmanager
    def open_readonly(self, request: WorkerOperationRequest) -> Iterator[int]:
        """Yield an owned read-only fd, never a reusable approved path.

        The leaf and all parents must already exist. Missing components,
        symlinks and nonregular targets are refused; no mkdir, O_CREAT or
        write occurs. file.list and file.patch are not implemented.
        """
        if self._root_fd < 0:
            raise ValueError("bridge_file_scope_closed")
        require_matching_worker_run(self._binding, request)
        if request.kind != "file.read":
            raise ValueError("bridge_file_scope_kind_unsupported")
        parts = request.target.split("/")
        root, allowed = self._live_scope()
        if root != self._root_path or not path_allowed(request.target, allowed):
            raise ValueError("bridge_file_scope_path_denied")
        self._check_root_identity()
        self._active_handles += 1
        try:
            with ExitStack() as owned:
                parent_fd = self._root_fd
                ancestors = []
                try:
                    for part in parts[:-1]:
                        _entry(parent_fd, part, directory=True)
                        child = os.open(part, _directory_flags(), dir_fd=parent_fd)
                        owned.callback(os.close, child)
                        if not stat.S_ISDIR(os.fstat(child).st_mode):
                            raise ValueError("bridge_file_scope_unsafe_type")
                        ancestors.append((parent_fd, part, child))
                        parent_fd = child
                    leaf = parts[-1]
                    _entry(parent_fd, leaf, directory=False)
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    fd = os.open(leaf, flags, dir_fd=parent_fd)
                    owned.callback(os.close, fd)
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError("bridge_file_scope_unsafe_type")
                    for parent, name, child in ancestors:
                        if (_identity(_entry(parent, name, directory=True))
                                != _identity(os.fstat(child))):
                            raise ValueError("bridge_file_scope_path_changed")
                    if (_identity(_entry(parent_fd, leaf, directory=False))
                            != _identity(os.fstat(fd))):
                        raise ValueError("bridge_file_scope_path_changed")
                    # Capability can be revoked while the walk is running.
                    fresh_root, fresh_allowed = self._live_scope()
                    if (fresh_root != self._root_path
                            or not path_allowed(request.target, fresh_allowed)):
                        raise ValueError("bridge_file_scope_scope_changed")
                    self._check_root_identity()
                except (OSError, TypeError) as exc:
                    raise ValueError("bridge_file_scope_path_denied") from exc
                yield fd
        finally:
            self._active_handles -= 1

    def close(self) -> None:
        if self._active_handles:
            raise ValueError("bridge_file_scope_busy")
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> "LocalFileScope":
        if self._root_fd < 0:
            raise ValueError("bridge_file_scope_closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
