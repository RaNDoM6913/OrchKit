"""Bounded metadata rejection, privacy and actual native preservation."""

import ctypes
import errno
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from orch import bridge_patch_metadata as module


class FakeNative:
    def __init__(self, attributes=None):
        self.attributes = dict(attributes or {})
        self.acl = False
        self.actions = []

    def require_no_acl(self, fd):
        if self.acl:
            raise ValueError("patch_metadata_acl_unsupported")

    def list_names(self, fd):
        return list(self.attributes)

    def get_value(self, fd, name):
        return self.attributes[name]

    def set_value(self, fd, name, value):
        self.actions.append(("set", stat.S_IMODE(os.fstat(fd).st_mode)))
        self.attributes[name] = value

    def remove_value(self, fd, name):
        self.actions.append(("remove", stat.S_IMODE(os.fstat(fd).st_mode)))
        del self.attributes[name]


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-patch-metadata-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "file"
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.fd)

    def test_signature_has_exact_fields_and_stable_identity_ignores_only_times(self):
        info = os.fstat(self.fd)
        signature = module.file_signature(info)
        self.assertEqual(set(signature), {
            "dev", "ino", "mode", "uid", "gid", "nlink", "size",
            "mtime_ns", "ctime_ns", "flags",
        })
        changed = dict(signature, mtime_ns=0, ctime_ns=0)
        self.assertEqual(module.stable_identity(signature), module.stable_identity(changed))
        for field in set(signature) - {"mtime_ns", "ctime_ns"}:
            self.assertNotEqual(module.stable_identity(signature),
                                module.stable_identity(dict(signature, **{field: -1})))

    def test_private_attributes_are_canonical_and_never_repr_or_json_data(self):
        native = FakeNative({b"user.secret": b"private-value", b"user.a": b"a"})
        with mock.patch.object(module, "_native_api", return_value=native):
            metadata = module.read_file_metadata(self.fd)
            native.attributes = dict(reversed(list(native.attributes.items())))
            reordered = module.read_file_metadata(self.fd)
        self.assertEqual(metadata["xattrs_digest"], reordered["xattrs_digest"])
        self.assertEqual(metadata["xattrs"][b"user.secret"], b"private-value")
        self.assertNotIn("private-value", repr(metadata))
        self.assertNotIn("user.secret", repr(metadata))
        with self.assertRaises(TypeError):
            json.dumps(metadata)

    def test_metadata_bounds_accept_exact_limits_and_reject_overflow(self):
        for attributes, accepted in (
            ({b"a": b"x" * 65536}, True),
            ({b"a": b"x" * 65537}, False),
            ({str(index).encode(): b"" for index in range(32)}, True),
            ({str(index).encode(): b"" for index in range(33)}, False),
            ({b"a" * 4096: b""}, True),
            ({b"a" * 4097: b""}, False),
            ({b"a": b"x" * 32769, b"b": b"y" * 32768}, False),
        ):
            with self.subTest(accepted=accepted, count=len(attributes)):
                with mock.patch.object(module, "_native_api", return_value=FakeNative(attributes)):
                    if accepted:
                        self.assertEqual(len(module.read_file_metadata(self.fd)["xattrs_digest"]), 64)
                    else:
                        with self.assertRaisesRegex(ValueError, "patch_metadata_"):
                            module.read_file_metadata(self.fd)

    def test_owner_hardlink_special_bits_flags_and_acl_are_rejected(self):
        actual = module.file_signature(os.fstat(self.fd))
        native = FakeNative()
        for changed in ({"uid": actual["uid"] + 1}, {"nlink": 2},
                        {"mode": actual["mode"] | stat.S_ISUID},
                        {"mode": actual["mode"] | stat.S_ISGID},
                        {"mode": actual["mode"] | stat.S_ISVTX}, {"flags": 1},
                        {"mode": stat.S_IFIFO | 0o600}):
            with self.subTest(changed=changed):
                with mock.patch.object(module, "_native_api", return_value=native), \
                     mock.patch.object(module, "file_signature", return_value=dict(actual, **changed)):
                    with self.assertRaisesRegex(ValueError, "patch_metadata_"):
                        module.read_file_metadata(self.fd)
        native.acl = True
        with mock.patch.object(module, "_native_api", return_value=native):
            with self.assertRaisesRegex(ValueError, "patch_metadata_acl_unsupported"):
                module.read_file_metadata(self.fd)

    def test_copy_keeps_initial_private_mode_and_removes_only_temp_extras(self):
        native = FakeNative({b"user.retained": b"value"})
        os.fchmod(self.fd, 0o640)
        with mock.patch.object(module, "_native_api", return_value=native):
            metadata = module.read_file_metadata(self.fd)
            os.fchmod(self.fd, 0o600)
            native.attributes = {b"user.extra": b"extra"}
            module.copy_file_metadata(metadata, self.fd)
            self.assertEqual(native.attributes, {b"user.retained": b"value"})
            self.assertEqual(stat.S_IMODE(os.fstat(self.fd).st_mode), 0o640)
            self.assertEqual(native.actions, [("remove", 0o600), ("set", 0o600)])

    def test_copy_rejects_nonprivate_temp_and_failed_exact_readback(self):
        native = FakeNative({b"a": b"value"})
        with mock.patch.object(module, "_native_api", return_value=native):
            metadata = module.read_file_metadata(self.fd)
            os.fchmod(self.fd, 0o644)
            with self.assertRaisesRegex(ValueError, "patch_metadata_temp_unsafe"):
                module.copy_file_metadata(metadata, self.fd)
            os.fchmod(self.fd, 0o600)
            native.attributes = {}
            with mock.patch.object(native, "set_value", return_value=None):
                with self.assertRaisesRegex(ValueError, "patch_metadata_copy_unverified"):
                    module.copy_file_metadata(metadata, self.fd)

    def test_metadata_observation_race_and_native_error_fail_closed_without_details(self):
        native = FakeNative()
        signature = module.file_signature(os.fstat(self.fd))
        with mock.patch.object(module, "_native_api", return_value=native), \
             mock.patch.object(module, "file_signature", side_effect=[signature, dict(signature, ctime_ns=-1)]):
            with self.assertRaisesRegex(ValueError, "patch_metadata_changed"):
                module.read_file_metadata(self.fd)
        with mock.patch.object(module, "_native_api", return_value=native), \
             mock.patch.object(native, "list_names", side_effect=OSError("private-name private-value")):
            try:
                module.read_file_metadata(self.fd)
            except ValueError as exc:
                self.assertEqual(str(exc), "patch_metadata_unreadable")
                self.assertTrue(exc.__suppress_context__)
            else:
                self.fail("native failure was accepted")

    def test_unsupported_platform_refuses_metadata_only(self):
        with mock.patch.object(module.sys, "platform", "unsupported"):
            with self.assertRaisesRegex(ValueError, "patch_metadata_unsupported"):
                module.read_file_metadata(self.fd)

    def test_native_acl_null_requires_existing_fd_and_enoent(self):
        native = module._MacMetadataAPI.__new__(module._MacMetadataAPI)
        native._acl_get_fd_np = lambda *_: ctypes.set_errno(errno.ENOENT)
        native._acl_get_entry = lambda *_: 0
        native._acl_free = lambda *_: 0
        ctypes.set_errno(errno.ENOENT)
        native.require_no_acl(self.fd)
        with self.assertRaisesRegex(ValueError, "patch_metadata_acl_unreadable"):
            native.require_no_acl(-1)
        native._acl_get_fd_np = lambda *_: ctypes.set_errno(errno.EPERM)
        with self.assertRaisesRegex(ValueError, "patch_metadata_acl_unreadable"):
            native.require_no_acl(self.fd)

    def test_native_acl_entry_zero_is_an_acl_and_pointer_always_freed(self):
        native = module._MacMetadataAPI.__new__(module._MacMetadataAPI)
        native._acl_get_fd_np = lambda *_: 123
        native._acl_get_entry = mock.Mock(return_value=0)
        native._acl_free = mock.Mock(return_value=0)
        with self.assertRaisesRegex(ValueError, "patch_metadata_acl_unsupported"):
            native.require_no_acl(self.fd)
        self.assertEqual(native._acl_get_entry.call_args.args[1], 0)
        native._acl_free.assert_called_once_with(123)

    @unittest.skipUnless(sys.platform == "darwin", "native macOS ACL contract")
    def test_actual_native_acl_and_parent_inheritance_are_rejected(self):
        user = pwd.getpwuid(os.geteuid()).pw_name
        for path, entry, is_directory in (
            (self.path, "user:%s allow read" % user, False),
            (Path(self.temp.name), "user:%s allow read,file_inherit,directory_inherit" % user, True),
        ):
            with self.subTest(parent=is_directory):
                result = subprocess.run(["/bin/chmod", "+a", entry, str(path)],
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, "native ACL fixture setup failed")
                fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if is_directory else 0))
                try:
                    with self.assertRaisesRegex(ValueError, "patch_metadata_acl_unsupported"):
                        module.require_no_acl(fd)
                finally:
                    os.close(fd)
                    subprocess.run(["/bin/chmod", "-N", str(path)], capture_output=True,
                                   check=True, timeout=5)

    @unittest.skipUnless(sys.platform == "darwin", "native macOS metadata contract")
    def test_actual_native_resource_fork_and_provenance_are_preserved(self):
        native = module._native_api()
        native.set_value(self.fd, b"com.apple.ResourceFork", b"resource-fork\0value")
        native.set_value(self.fd, b"user.orch-private", b"private-value")
        os.fchmod(self.fd, 0o640)
        metadata = module.read_file_metadata(self.fd)
        other = os.open(Path(self.temp.name) / "owned-temp", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            native.set_value(other, b"user.extra", b"remove")
            module.copy_file_metadata(metadata, other)
            self.assertEqual(module.read_file_metadata(other), metadata)
            self.assertEqual(metadata["xattrs"][b"com.apple.ResourceFork"], b"resource-fork\0value")
        finally:
            os.close(other)


if __name__ == "__main__":
    unittest.main()
