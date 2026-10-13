"""Local, bounded macOS descriptor metadata for existing-file replacement.

Attribute names and values are private in-memory data, never journal fields.
An unsupported or unreadable metadata mechanism refuses before publication.
"""

from __future__ import annotations

from collections.abc import Mapping
import ctypes
import errno
import hashlib
import os
import stat
import sys
from typing import Iterator, Optional


MAX_XATTR_NAMES = 32
MAX_XATTR_NAME_BYTES = 4096
MAX_XATTR_VALUE_BYTES = 65536
_SIGNATURE_FIELDS = ("dev", "ino", "mode", "uid", "gid", "nlink", "size",
                     "mtime_ns", "ctime_ns", "flags")


def file_signature(info: os.stat_result) -> dict:
    return {"dev": info.st_dev, "ino": info.st_ino, "mode": info.st_mode,
            "uid": info.st_uid, "gid": info.st_gid, "nlink": info.st_nlink,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "flags": getattr(info, "st_flags", 0)}


def stable_identity(signature: dict) -> tuple:
    """Use timestamps for observation stability, not rename provenance."""
    return tuple(signature[key] for key in _SIGNATURE_FIELDS
                 if key not in ("mtime_ns", "ctime_ns"))


class _PrivateXattrs(Mapping):
    """Mapping access remains local; repr and JSON cannot disclose attributes."""

    def __init__(self, values: dict):
        self.__values = dict(values)

    def __getitem__(self, name: bytes) -> bytes:
        return self.__values[name]

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    def __repr__(self) -> str:
        return "<private bounded xattrs>"


def _xattrs_digest(values: Mapping) -> str:
    digest = hashlib.sha256()
    digest.update(b"orch-patch-xattrs-v1\0")
    for name in sorted(values):
        value = values[name]
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _bounded_names(names: list) -> None:
    if (len(names) > MAX_XATTR_NAMES or len(set(names)) != len(names)
            or any(not isinstance(name, bytes) or not name or b"\0" in name
                   for name in names)
            or sum(len(name) for name in names) > MAX_XATTR_NAME_BYTES):
        raise ValueError("patch_metadata_xattrs_too_large")


class _MacMetadataAPI:
    """Bind native FD APIs explicitly; Python's path xattr APIs are not used."""

    def __init__(self):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            self._flistxattr = libc.flistxattr
            self._flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
            self._flistxattr.restype = ctypes.c_ssize_t
            self._fgetxattr = libc.fgetxattr
            self._fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
            self._fgetxattr.restype = ctypes.c_ssize_t
            self._fsetxattr = libc.fsetxattr
            self._fsetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
            self._fsetxattr.restype = ctypes.c_int
            self._fremovexattr = libc.fremovexattr
            self._fremovexattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            self._fremovexattr.restype = ctypes.c_int
            self._acl_get_fd_np = libc.acl_get_fd_np
            self._acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
            self._acl_get_fd_np.restype = ctypes.c_void_p
            self._acl_get_entry = libc.acl_get_entry
            self._acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                           ctypes.POINTER(ctypes.c_void_p)]
            self._acl_get_entry.restype = ctypes.c_int
            self._acl_free = libc.acl_free
            self._acl_free.argtypes = [ctypes.c_void_p]
            self._acl_free.restype = ctypes.c_int
        except (AttributeError, OSError, TypeError):
            raise ValueError("patch_metadata_unsupported") from None

    def require_no_acl(self, fd: int) -> None:
        try:
            before = file_signature(os.fstat(fd))
        except (OSError, TypeError, ValueError):
            raise ValueError("patch_metadata_acl_unreadable") from None
        ctypes.set_errno(0)
        acl = self._acl_get_fd_np(fd, 0x100)  # macOS ACL_TYPE_EXTENDED
        if not acl:
            if ctypes.get_errno() != errno.ENOENT:
                raise ValueError("patch_metadata_acl_unreadable")
        else:
            try:
                entry = ctypes.c_void_p()
                # macOS success is 0, unlike the Linux 1-success convention.
                if self._acl_get_entry(acl, 0, ctypes.byref(entry)) == 0:
                    raise ValueError("patch_metadata_acl_unsupported")
                raise ValueError("patch_metadata_acl_unreadable")
            finally:
                self._acl_free(acl)
        try:
            if file_signature(os.fstat(fd)) != before:
                raise ValueError("patch_metadata_changed")
        except (OSError, TypeError):
            raise ValueError("patch_metadata_acl_unreadable") from None

    def list_names(self, fd: int) -> list:
        size = self._flistxattr(fd, None, 0, 0)
        if size < 0:
            raise ValueError("patch_metadata_xattrs_unreadable")
        if size > MAX_XATTR_NAME_BYTES + MAX_XATTR_NAMES:
            raise ValueError("patch_metadata_xattrs_too_large")
        buffer = ctypes.create_string_buffer(max(size, 1))
        if self._flistxattr(fd, buffer, size, 0) != size:
            raise ValueError("patch_metadata_changed")
        raw = buffer.raw[:size]
        if raw and not raw.endswith(b"\0"):
            raise ValueError("patch_metadata_xattrs_unreadable")
        names = raw[:-1].split(b"\0") if raw else []
        _bounded_names(names)
        return names

    def get_value(self, fd: int, name: bytes) -> bytes:
        size = self._fgetxattr(fd, name, None, 0, 0, 0)
        if size < 0:
            raise ValueError("patch_metadata_xattrs_unreadable")
        if size > MAX_XATTR_VALUE_BYTES:
            raise ValueError("patch_metadata_xattrs_too_large")
        buffer = ctypes.create_string_buffer(max(size, 1))
        if self._fgetxattr(fd, name, buffer, size, 0, 0) != size:
            raise ValueError("patch_metadata_changed")
        return buffer.raw[:size]

    def set_value(self, fd: int, name: bytes, value: bytes) -> None:
        buffer = ctypes.create_string_buffer(value, len(value) + 1)
        if self._fsetxattr(fd, name, buffer, len(value), 0, 0) != 0:
            raise ValueError("patch_metadata_copy_failed")

    def remove_value(self, fd: int, name: bytes) -> None:
        if self._fremovexattr(fd, name, 0) != 0:
            raise ValueError("patch_metadata_copy_failed")


_API: Optional[_MacMetadataAPI] = None


def _native_api() -> _MacMetadataAPI:
    global _API
    if sys.platform != "darwin":
        raise ValueError("patch_metadata_unsupported")
    if _API is None:
        _API = _MacMetadataAPI()
    return _API


def require_no_acl(fd: int) -> None:
    """Reject target and parent ACLs, including parent inheritance entries."""
    try:
        _native_api().require_no_acl(fd)
    except (OSError, TypeError, AttributeError):
        raise ValueError("patch_metadata_acl_unreadable") from None


def _require_regular(signature: dict) -> None:
    if (not stat.S_ISREG(signature["mode"])
            or signature["uid"] != os.geteuid() or signature["nlink"] != 1
            or signature["mode"] & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            or signature["flags"] != 0
            or not 0 <= signature["size"] <= MAX_XATTR_VALUE_BYTES):
        raise ValueError("patch_metadata_target_unsafe")


def read_file_metadata(fd: int) -> dict:
    """Observe allowed existing-file metadata without disclosing raw xattrs."""
    try:
        api = _native_api()
        before = file_signature(os.fstat(fd))
        _require_regular(before)
        api.require_no_acl(fd)
        names = api.list_names(fd)
        _bounded_names(names)
        values = {}
        total = 0
        for name in names:
            value = api.get_value(fd, name)
            if not isinstance(value, bytes):
                raise ValueError("patch_metadata_xattrs_unreadable")
            total += len(value)
            if total > MAX_XATTR_VALUE_BYTES:
                raise ValueError("patch_metadata_xattrs_too_large")
            values[name] = value
        if set(api.list_names(fd)) != set(names) or file_signature(os.fstat(fd)) != before:
            raise ValueError("patch_metadata_changed")
        return {"mode": before["mode"], "uid": before["uid"], "gid": before["gid"],
                "flags": before["flags"], "xattrs": _PrivateXattrs(values),
                "xattrs_digest": _xattrs_digest(values)}
    except (OSError, TypeError, AttributeError, KeyError):
        raise ValueError("patch_metadata_unreadable") from None


def copy_file_metadata(metadata: dict, temp_fd: int) -> None:
    """Prepare a caller-owned private temp and independently verify metadata.

    Caller must establish the exclusive scratch inode ownership before calling.
    This helper never removes attributes on an original or arbitrary pathname.
    """
    try:
        if (type(metadata) is not dict or set(metadata) != {
                "mode", "uid", "gid", "flags", "xattrs", "xattrs_digest"}
                or not isinstance(metadata["xattrs"], _PrivateXattrs)
                or metadata["xattrs_digest"] != _xattrs_digest(metadata["xattrs"])):
            raise ValueError("patch_metadata_invalid")
        for key in ("mode", "uid", "gid", "flags"):
            if type(metadata[key]) is not int:
                raise ValueError("patch_metadata_invalid")
        _require_regular(dict(metadata, nlink=1, size=0))
        before = file_signature(os.fstat(temp_fd))
        _require_regular(before)
        if stat.S_IMODE(before["mode"]) != 0o600:
            raise ValueError("patch_metadata_temp_unsafe")
        api = _native_api()
        api.require_no_acl(temp_fd)
        names = api.list_names(temp_fd)
        _bounded_names(names)
        wanted = metadata["xattrs"]
        _bounded_names(list(wanted))
        if sum(len(wanted[name]) for name in wanted) > MAX_XATTR_VALUE_BYTES:
            raise ValueError("patch_metadata_xattrs_too_large")
        for name in names:
            if name not in wanted:
                api.remove_value(temp_fd, name)
        for name in wanted:
            api.set_value(temp_fd, name, wanted[name])
        os.fchown(temp_fd, metadata["uid"], metadata["gid"])
        os.fchmod(temp_fd, stat.S_IMODE(metadata["mode"]))
        after = file_signature(os.fstat(temp_fd))
        if ((after["dev"], after["ino"], after["size"])
                != (before["dev"], before["ino"], before["size"])
                or read_file_metadata(temp_fd) != metadata):
            raise ValueError("patch_metadata_copy_unverified")
    except (OSError, TypeError, AttributeError, KeyError):
        raise ValueError("patch_metadata_copy_failed") from None
