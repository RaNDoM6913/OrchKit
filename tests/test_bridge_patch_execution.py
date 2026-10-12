"""Real filesystem execution chains, stable locking, and append faults."""

import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from orch import bridge_operation_journal as journal
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding
try:
    from orch import bridge_patch_execution as module
except ImportError:
    module = None


class PatchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, "Bounded execution store is not implemented")
        temporary = tempfile.TemporaryDirectory(prefix="orch-patch-execution-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o755)
        self.runtime = self.home / ".runtime"
        self.runtime.mkdir(mode=0o700)
        self.binding = WorkerRunBinding("fixture", "fixture:key", "TASK-1", "RUN-1", 1)
        self.request = WorkerOperationRequest(self.binding, "operation-1", "file.patch", "README.md")
        self.replacement = {"schema_version": 1,
                            "baseline": {"sha256": hashlib.sha256(b"original").hexdigest(), "size_bytes": 8},
                            "sha256": hashlib.sha256(b"replacement").hexdigest(), "size_bytes": 11}
        self.intent = journal.build_intent_record(self.request, self.replacement)
        self.assertEqual(journal.OperationIntentJournal(self.home).create(
            self.intent, authorize=lambda: None)["status"], "CREATED")
        self.store = module.PatchExecutionStore(self.home)
        original = {"dev": 1, "ino": 2, "mode": stat.S_IFREG | 0o644,
                    "uid": os.getuid(), "gid": os.getgid(), "nlink": 1,
                    "size": 8, "mtime_ns": 100, "ctime_ns": 100, "flags": 0}
        common = {"schema_version": 1, "request": dataclasses.asdict(self.request),
                  "intent_fingerprint": self.intent["fingerprint"],
                  "created_at": "2026-10-11T12:00:00.000000Z"}
        self.dispatch = dict(common, phase="DISPATCH", anchors={"root": [1, 10],
                             "parents": [[1, 10]], "parent": [1, 10]},
                             original={"identity": original,
                                       "sha256": self.replacement["baseline"]["sha256"],
                                       "size_bytes": 8, "xattrs_digest": "0" * 64},
                             temp_name=".orch-patch-" + "a" * 32 + ".tmp")
        staged = dict(original, ino=3, size=11, mode=stat.S_IFREG | 0o644,
                      mtime_ns=200, ctime_ns=200)
        self.publication = dict(common, phase="PUBLICATION",
                                dispatch_sha256=self._hash(self.dispatch),
                                staged={"identity": staged,
                                        "sha256": self.replacement["sha256"],
                                        "size_bytes": 11, "xattrs_digest": "0" * 64})
        self.applied = dict(common, phase="RESULT", dispatch_sha256=self._hash(self.dispatch),
                            publication_sha256=self._hash(self.publication), disposition="APPLIED",
                            basis={"kind": "APPLY", "target": copy.deepcopy(self.publication["staged"]),
                                   "temp_absent": True})
        self.negative = dict(common, phase="RESULT", dispatch_sha256=self._hash(self.dispatch),
                             publication_sha256=None, disposition="NOT_APPLIED",
                             basis={"kind": "RECONCILE_NO_PUBLICATION",
                                    "target": copy.deepcopy(self.dispatch["original"]), "temp_absent": True})

    @staticmethod
    def _hash(record):
        return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()

    def _path(self, phase="dispatch", request=None):
        request = request or self.request
        return (self.runtime / "patch_execution"
                / hashlib.sha256(request.binding.run_id.encode()).hexdigest()
                / hashlib.sha256(request.operation_id.encode()).hexdigest() / (phase + ".json"))

    def _append(self, record, authorize=lambda: None):
        with module.runtime_operation_lock(self.home):
            return self.store.append(record, authorize=authorize)

    def _inspect(self):
        return self.store.inspect(self.binding.run_id, self.request.operation_id)

    def _write(self, path, raw):
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)

    def _terminal(self, disposition="APPLIED"):
        records = [self.dispatch]
        if disposition == "APPLIED":
            records.extend((self.publication, self.applied))
        else:
            records.append(self.negative)
        for record in records:
            self.assertEqual(self._append(record)["status"], "CREATED")
        return self._inspect()["records"]

    def _intent_path(self, request=None):
        request = request or self.request
        return (self.runtime / "operation_journal"
                / hashlib.sha256(request.binding.run_id.encode()).hexdigest()
                / (hashlib.sha256(request.operation_id.encode()).hexdigest() + ".json"))

    def _ack(self, records, **kwargs):
        with module.runtime_operation_lock(self.home):
            return self.store.acknowledge_existing(self.binding.run_id, self.request.operation_id,
                records, authorize=kwargs.pop("authorize", lambda: None), **kwargs)

    def _evidence(self):
        paths = [self._path(phase) for phase in ("dispatch", "publication", "result")
                 if self._path(phase).exists()] + [self._intent_path()]
        result = {}
        for path in paths:
            raw, info = path.read_bytes(), path.stat()
            identity = tuple(getattr(info, field) for field in ("st_dev", "st_ino", "st_mode",
                "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))
            result[path] = raw, identity
        return result

    def test_retained_intent_failed_sync_blocks_new_dispatch_before_namespace_creation(self):
        path = self._intent_path()
        path.unlink()
        real_fsync = os.fsync
        identity = (path.parent.stat().st_dev, path.parent.stat().st_ino)
        attempted = []

        def fail_intent_run(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == identity:
                attempted.append(None)
                raise OSError("linked intent directory acknowledgement failed")
            return real_fsync(fd)

        with mock.patch.object(module.os, "fsync", side_effect=fail_intent_run):
            intents = journal.OperationIntentJournal(self.home)
            self.assertEqual(intents.create(self.intent, authorize=lambda: None)["status"], "UNKNOWN")
            self.assertEqual(intents.inspect(self.binding.run_id, self.request.operation_id)["status"],
                             "RECORDED")
            self.assertEqual(intents.create(self.intent, authorize=lambda: None)["status"], "EXISTING")
            retained = self._evidence()
            attempted.clear()
            result = self._append(self.dispatch)
            self.assertEqual(result["status"], "UNKNOWN", result)
            self.assertEqual(len(attempted), 1, "dispatch must independently renew the intent")
            self.assertFalse((self.runtime / "patch_execution").exists())
            self.assertEqual(self._evidence(), retained)

    def test_each_linked_intent_sync_failure_blocks_dispatch_without_creating_evidence(self):
        path = self._intent_path()
        retained = self._evidence()
        targets = (path, path.parent, path.parents[1], self.runtime, self.home)
        real_fsync = os.fsync
        for target in targets:
            with self.subTest(target=target.relative_to(self.home)):
                if (self.runtime / "patch_execution").exists():
                    shutil.rmtree(self.runtime / "patch_execution")
                identity = (target.stat().st_dev, target.stat().st_ino)
                attempted = []

                def refuse(fd):
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) == identity:
                        attempted.append(None)
                        raise OSError("linked intent acknowledgement failure")
                    return real_fsync(fd)

                with mock.patch.object(module.os, "fsync", side_effect=refuse):
                    result = self._append(self.dispatch)
                self.assertEqual(result["status"], "UNKNOWN", result)
                self.assertEqual(len(attempted), 1)
                self.assertFalse((self.runtime / "patch_execution").exists())
                self.assertEqual(self._evidence(), retained)

    def test_linked_intent_renewal_precedes_all_new_execution_writes(self):
        path = self._intent_path()
        # Renewal preserves the exact readable bytes, including noncanonical whitespace.
        path.write_bytes(path.read_bytes() + b"\n")
        retained = self._evidence()
        targets = (path, path.parent, path.parents[1], self.runtime, self.home)
        identities = {(p.stat().st_dev, p.stat().st_ino): p for p in targets}
        before_creation, events = [], []
        real_fsync, real_mkdir, real_read = os.fsync, os.mkdir, module._read_record_bytes

        def authority():
            if not (self.runtime / "patch_execution").exists():
                events.append("authority")

        def readback(parent, name, **kwargs):
            value = real_read(parent, name, **kwargs)
            if name == path.name and not (self.runtime / "patch_execution").exists():
                events.append("read")
            return value

        def synced(fd):
            info = os.fstat(fd)
            if not (self.runtime / "patch_execution").exists():
                before_creation.append(identities[(info.st_dev, info.st_ino)])
                events.append("sync")
            return real_fsync(fd)

        def created(*args, **kwargs):
            self.assertEqual(before_creation, list(targets), "intent must sync file then deepest directories")
            self.assertEqual(events, ["authority", "authority", "read"] + ["sync"] * 5
                             + ["read", "authority"], "independent readback must precede final live authority")
            self.assertEqual(self._evidence(), retained)
            return real_mkdir(*args, **kwargs)

        with mock.patch.object(module.os, "fsync", side_effect=synced), \
                mock.patch.object(module.os, "mkdir", side_effect=created), \
                mock.patch.object(module, "_read_record_bytes", side_effect=readback):
            result = self._append(self.dispatch, authorize=authority)
        self.assertEqual(result["status"], "CREATED", result)
        self.assertEqual(self._evidence()[path], retained[path])

    def test_linked_intent_byte_and_inode_drift_blocks_dispatch_before_namespace_creation(self):
        path = self._intent_path()
        real_fsync = os.fsync
        for kind in ("raw", "inode"):
            with self.subTest(kind=kind):
                if (self.runtime / "patch_execution").exists():
                    shutil.rmtree(self.runtime / "patch_execution")
                original = path.read_bytes()
                identity = (path.stat().st_dev, path.stat().st_ino)
                changed = []

                def mutate(fd):
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) == identity and not changed:
                        changed.append(None)
                        if kind == "raw":
                            path.write_bytes(original + b"\n")
                        else:
                            path.rename(self.base / "retained-intent")
                            self._write(path, original)
                    return real_fsync(fd)

                with mock.patch.object(module.os, "fsync", side_effect=mutate):
                    result = self._append(self.dispatch)
                self.assertEqual(result["status"], "UNKNOWN", result)
                self.assertEqual(len(changed), 1)
                self.assertFalse((self.runtime / "patch_execution").exists())
                self.assertEqual(path.read_bytes(), original + (b"\n" if kind == "raw" else b""))
                if kind == "raw":
                    path.write_bytes(original)

    def test_linked_intent_directory_substitution_blocks_dispatch_before_namespace_creation(self):
        path = self._intent_path()
        real_fsync = os.fsync
        for index, directory in enumerate((path.parent, path.parents[1])):
            with self.subTest(directory=directory):
                if (self.runtime / "patch_execution").exists():
                    shutil.rmtree(self.runtime / "patch_execution")
                identity = (path.stat().st_dev, path.stat().st_ino)
                retained = self.base / ("retained-intent-directory-" + str(index))
                original = path.read_bytes()
                changed = []

                def substitute(fd):
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) == identity and not changed:
                        changed.append(None)
                        directory.rename(retained)
                        shutil.copytree(retained, directory)
                    return real_fsync(fd)

                with mock.patch.object(module.os, "fsync", side_effect=substitute):
                    result = self._append(self.dispatch)
                self.assertEqual(result["status"], "UNKNOWN", result)
                self.assertEqual(len(changed), 1)
                self.assertFalse((self.runtime / "patch_execution").exists())
                self.assertEqual(path.read_bytes(), original)
                self.assertTrue(retained.is_dir())

    def test_linked_intent_request_binding_and_fingerprint_drift_blocks_new_dispatch(self):
        path = self._intent_path()
        real_fsync = os.fsync
        variants = {
            "request": journal.build_intent_record(dataclasses.replace(self.request, max_output_bytes=1),
                                                    self.replacement),
            "binding": journal.build_intent_record(dataclasses.replace(self.request,
                binding=dataclasses.replace(self.binding, attempt=2)), self.replacement),
            "fingerprint": dict(self.intent, fingerprint="0" * 64),
        }
        original = path.read_bytes()
        for kind, replacement in variants.items():
            with self.subTest(kind=kind):
                if (self.runtime / "patch_execution").exists():
                    shutil.rmtree(self.runtime / "patch_execution")
                path.write_bytes(original)
                identity = (path.stat().st_dev, path.stat().st_ino)
                changed = []

                def mutate(fd):
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) == identity and not changed:
                        changed.append(None)
                        path.write_bytes(json.dumps(replacement).encode())
                    return real_fsync(fd)

                with mock.patch.object(module.os, "fsync", side_effect=mutate):
                    result = self._append(self.dispatch)
                self.assertEqual(result["status"], "UNKNOWN", result)
                self.assertEqual(len(changed), 1)
                self.assertFalse((self.runtime / "patch_execution").exists())
                self.assertEqual(json.loads(path.read_bytes()), replacement)
        path.write_bytes(original)

    def test_linked_intent_final_full_readback_failure_and_later_drift_block_dispatch(self):
        path = self._intent_path()
        real_read = module._read_record_bytes
        original = path.read_bytes()
        for fault in ("read_failure", "after_read_drift"):
            with self.subTest(fault=fault):
                if (self.runtime / "patch_execution").exists():
                    shutil.rmtree(self.runtime / "patch_execution")
                path.write_bytes(original)
                reads = []

                def reread(parent, name, **kwargs):
                    value = real_read(parent, name, **kwargs)
                    if name == path.name:
                        reads.append(None)
                        if len(reads) == 2:
                            if fault == "read_failure":
                                raise OSError("intent readback failed")
                            path.write_bytes(original + b"\n")
                    return value

                with mock.patch.object(module, "_read_record_bytes", side_effect=reread):
                    result = self._append(self.dispatch)
                self.assertEqual(result["status"], "UNKNOWN", result)
                self.assertEqual(len(reads), 2)
                self.assertFalse((self.runtime / "patch_execution").exists())

    def test_linked_intent_final_authority_revocation_or_drift_blocks_dispatch(self):
        path = self._intent_path()
        original = path.read_bytes()
        real_fsync = os.fsync
        for fault in ("revoked", "drift"):
            with self.subTest(fault=fault):
                if (self.runtime / "patch_execution").exists():
                    shutil.rmtree(self.runtime / "patch_execution")
                path.write_bytes(original)
                synced, altered = [], []

                def sync(fd):
                    if not (self.runtime / "patch_execution").exists():
                        synced.append(None)
                    return real_fsync(fd)

                def authority():
                    if len(synced) == 5 and not altered:
                        altered.append(None)
                        if fault == "revoked":
                            raise ValueError("intent authority revoked")
                        path.write_bytes(original + b"\n")

                with mock.patch.object(module.os, "fsync", side_effect=sync):
                    result = self._append(self.dispatch, authorize=authority)
                self.assertEqual(result["status"], "UNKNOWN", result)
                self.assertEqual(len(altered), 1)
                self.assertFalse((self.runtime / "patch_execution").exists())

    def test_private_intent_ack_preserves_evidence_and_reuses_exact_locked_boundary_fds(self):
        path = self._intent_path()
        path.write_bytes(path.read_bytes() + b"\n")
        retained = self._evidence()
        objects = (path, path.parent, path.parents[1], self.runtime, self.home)
        identities = {(p.stat().st_dev, p.stat().st_ino): p for p in objects}
        observed, reads, authorities = [], [], []
        real_fsync, real_read = os.fsync, module._read_record_bytes

        with module.runtime_operation_lock(self.home) as lock:
            boundaries = {self.home: lock.anchors[0][2], self.runtime: lock.anchors[1][2]}

            def synced(fd):
                info = os.fstat(fd)
                target = identities[(info.st_dev, info.st_ino)]
                observed.append(target)
                if target in boundaries:
                    self.assertEqual(fd, boundaries[target], "acknowledgement must reuse exact lock FDs")
                return real_fsync(fd)

            def readback(parent, name, **kwargs):
                result = real_read(parent, name, **kwargs)
                reads.append(result)
                return result

            with mock.patch.object(module.os, "fsync", side_effect=synced), \
                    mock.patch.object(module, "_read_record_bytes", side_effect=readback), \
                    mock.patch.object(module.os, "write", side_effect=AssertionError("ack wrote")), \
                    mock.patch.object(module.os, "link", side_effect=AssertionError("ack linked")), \
                    mock.patch.object(module.os, "unlink", side_effect=AssertionError("ack unlinked")), \
                    mock.patch.object(module.os, "mkdir", side_effect=AssertionError("ack created directory")), \
                    mock.patch.object(module.os, "fchmod", side_effect=AssertionError("ack changed mode")), \
                    mock.patch.object(module.os, "rename", side_effect=AssertionError("ack renamed")):
                renewed = self.store._acknowledge_intent(self.intent, self.dispatch,
                    authorize=lambda: authorities.append(None))
        self.assertEqual(renewed, self.intent)
        self.assertEqual(observed, list(objects))
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0], reads[1])
        self.assertEqual(reads[0][0], retained[path][0])
        self.assertEqual(len(authorities), 2)
        self.assertEqual(self._evidence(), retained)
        self.assertFalse((self.runtime / "patch_execution").exists())

    def test_private_intent_ack_requires_lock_and_exact_dispatch_intent_linkage(self):
        retained = self._evidence()
        with self.assertRaisesRegex(ValueError, "patch_execution_lock_required"):
            self.store._acknowledge_intent(self.intent, self.dispatch, authorize=lambda: None)
        variants = (dict(self.dispatch, intent_fingerprint="0" * 64),
                    dict(self.dispatch, request=dataclasses.asdict(dataclasses.replace(self.request,
                        max_output_bytes=1))),
                    dict(self.dispatch, request=dataclasses.asdict(dataclasses.replace(self.request,
                        binding=dataclasses.replace(self.binding, attempt=2)))), self.publication)
        with module.runtime_operation_lock(self.home), \
                mock.patch.object(module.os, "fsync", side_effect=AssertionError("invalid linkage synced")):
            for candidate in variants:
                with self.subTest(candidate=candidate["phase"]), self.assertRaises(ValueError):
                    self.store._acknowledge_intent(self.intent, candidate, authorize=lambda: None)
        self.assertEqual(self._evidence(), retained)
        self.assertFalse((self.runtime / "patch_execution").exists())

    def test_retained_result_failed_directory_sync_requires_renewed_acknowledgement(self):
        # The wrong branch is an EXISTING/closure reply after a visible RESULT
        # whose final operation-directory fsync never acknowledged publication.
        for disposition in ("APPLIED", "NOT_APPLIED"):
            with self.subTest(disposition=disposition):
                if (self.runtime / "patch_execution").exists():
                    shutil.rmtree(self.runtime / "patch_execution")
                self.assertEqual(self._append(self.dispatch)["status"], "CREATED")
                if disposition == "APPLIED":
                    self.assertEqual(self._append(self.publication)["status"], "CREATED")
                result_record = self.applied if disposition == "APPLIED" else self.negative
                real_fsync, attempted = os.fsync, []
                def fail_opdir(fd):
                    info = os.fstat(fd)
                    if self._path("result").exists() and (info.st_dev, info.st_ino) == (
                            self._path().parent.stat().st_dev, self._path().parent.stat().st_ino):
                        attempted.append(None)
                        raise OSError("result directory acknowledgement failed")
                    return real_fsync(fd)
                with mock.patch.object(module.os, "fsync", side_effect=fail_opdir):
                    self.assertEqual(self._append(result_record)["status"], "UNKNOWN")
                    self.assertEqual(len(attempted), 1, "new append must not retry its failed acknowledgement")
                    found = self._inspect()
                    self.assertTrue(found["terminal"])
                    evidence = self._evidence()
                    self.assertEqual(self._append(result_record)["status"], "UNKNOWN")
                    with module.runtime_operation_lock(self.home), self.assertRaisesRegex(
                            ValueError, "patch_execution_unresolved"):
                        module.require_execution_resolved(self.home, self.binding.run_id,
                                                          self.binding, authorize=lambda: None)
                self.assertEqual(self._evidence(), evidence)
                self.assertEqual(self._ack(found["records"]), found["records"])
                self.assertEqual(self._append(result_record)["status"], "EXISTING")
                self.assertEqual(self._evidence(), evidence)

    def test_renewal_syncs_each_exact_record_intent_and_directory_without_mutation(self):
        records = self._terminal()
        # Noncanonical existing bytes are immutable evidence too.
        for path in self._evidence():
            path.write_bytes(path.read_bytes() + b"\n")
        before = self._evidence()
        directories = [self.home, self.runtime, self._path().parents[2],
                       self._path().parents[1], self._path().parent,
                       self._intent_path().parents[1], self._intent_path().parent]
        objects = list(before) + directories
        identities = {(p.stat().st_dev, p.stat().st_ino): p for p in objects}
        observed, real_fsync = [], os.fsync
        def synced(fd):
            info = os.fstat(fd)
            observed.append(identities[(info.st_dev, info.st_ino)])
            return real_fsync(fd)
        with mock.patch.object(module.os, "fsync", side_effect=synced), \
                mock.patch.object(module.os, "write", side_effect=AssertionError("renewal wrote")), \
                mock.patch.object(module.os, "link", side_effect=AssertionError("renewal linked")), \
                mock.patch.object(module.os, "unlink", side_effect=AssertionError("renewal unlinked")), \
                mock.patch.object(module.os, "mkdir", side_effect=AssertionError("renewal created directory")):
            renewed = self._ack(records, expected_request=dataclasses.asdict(self.request),
                                expected_binding=dataclasses.asdict(self.binding))
        self.assertEqual(renewed, records)
        self.assertEqual(set(observed), set(objects))
        self.assertEqual(len(observed), len(objects), "every object has its own acknowledgement")
        for child in directories[2:]:
            self.assertLess(observed.index(child), observed.index(child.parent))
        self.assertLess(observed.index(self.runtime), observed.index(self.home))
        self.assertEqual(self._evidence(), before)

    def test_every_record_and_directory_sync_failure_blocks_existing_and_closure(self):
        records = self._terminal()
        evidence = self._evidence()
        targets = list(evidence) + [self.home, self.runtime, self._path().parents[2],
                   self._path().parents[1], self._path().parent,
                   self._intent_path().parents[1], self._intent_path().parent]
        real_fsync = os.fsync
        for target in targets:
            with self.subTest(target=target.relative_to(self.home)):
                identity = (target.stat().st_dev, target.stat().st_ino)
                def refuse(fd):
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) == identity:
                        raise OSError("exact evidence acknowledgement failure")
                    return real_fsync(fd)
                with mock.patch.object(module.os, "fsync", side_effect=refuse):
                    with self.assertRaises((OSError, ValueError)):
                        self._ack(records)
                    self.assertEqual(self._append(self.applied)["status"], "UNKNOWN")
                    with module.runtime_operation_lock(self.home), self.assertRaisesRegex(
                            ValueError, "patch_execution_unresolved"):
                        module.require_execution_resolved(self.home, self.binding.run_id,
                                                          self.binding, authorize=lambda: None)
                self.assertEqual(self._evidence(), evidence)

    def test_renewal_rejects_raw_byte_and_equal_byte_inode_drift_during_sync(self):
        records = self._terminal()
        real_fsync = os.fsync
        for kind in ("raw", "inode"):
            for phase in ("dispatch", "publication", "result", "intent"):
                with self.subTest(kind=kind, phase=phase):
                    path = self._intent_path() if phase == "intent" else self._path(phase)
                    before = path.read_bytes()
                    changed = []
                    def mutate(fd):
                        if stat.S_ISDIR(os.fstat(fd).st_mode) and not changed:
                            changed.append(None)
                            if kind == "raw":
                                path.write_bytes(before + b"\n")
                            else:
                                retained = self.base / (phase + "-retained")
                                path.rename(retained)
                                self._write(path, before)
                        return real_fsync(fd)
                    with mock.patch.object(module.os, "fsync", side_effect=mutate), \
                            self.assertRaises((OSError, ValueError)):
                        self._ack(records)
                    self.assertTrue(changed)
                    self.assertEqual(path.read_bytes(), before + (b"\n" if kind == "raw" else b""))
                    if kind == "raw":
                        path.write_bytes(before)
                    else:
                        self.assertEqual((self.base / (phase + "-retained")).read_bytes(), before)

    def test_renewal_rejects_execution_and_intent_directory_substitution(self):
        records = self._terminal()
        real_fsync = os.fsync
        for index, path in enumerate((self._path().parent, self._path().parents[1],
                self._path().parents[2], self._intent_path().parent, self._intent_path().parents[1])):
            with self.subTest(directory=path):
                changed = []
                retained = self.base / ("retained-directory-" + str(index))
                def mutate(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode) and not changed:
                        changed.append(None)
                        path.rename(retained)
                        shutil.copytree(retained, path)
                    return real_fsync(fd)
                with mock.patch.object(module.os, "fsync", side_effect=mutate), \
                        self.assertRaises((OSError, ValueError)):
                    self._ack(records)
                self.assertTrue(retained.is_dir())
                self.assertEqual(self._inspect()["records"], records)

    def test_renewal_rejects_earlier_record_drift_during_final_full_readback(self):
        records = self._terminal()
        real_read = module._read_record_bytes
        for phase in ("dispatch", "publication", "result", "intent"):
            with self.subTest(phase=phase):
                intent_reads = []
                target = self._intent_path() if phase == "intent" else self._path(phase)
                def change_after_final_intent_read(parent, name, **kwargs):
                    value = real_read(parent, name, **kwargs)
                    if name == self._intent_path().name:
                        intent_reads.append(None)
                        if len(intent_reads) == 2:
                            target.write_bytes(target.read_bytes() + b"\n")
                    return value
                with mock.patch.object(module, "_read_record_bytes", side_effect=change_after_final_intent_read), \
                        self.assertRaises((OSError, ValueError)):
                    self._ack(records)
                self.assertEqual(len(intent_reads), 2)

    def test_renewal_rejects_record_drift_in_final_authority_callback(self):
        records = self._terminal()
        calls = []
        def authority():
            calls.append(None)
            if len(calls) == 2:
                self._path().write_bytes(self._path().read_bytes() + b"\n")
        with self.assertRaises((OSError, ValueError)):
            self._ack(records, authorize=authority)
        self.assertEqual(len(calls), 2)

    def test_renewal_requires_exact_request_binding_prefix_and_safe_names(self):
        records = self._terminal()
        before = self._evidence()
        for field, replacement in (("project_id", "foreign"), ("writer_key", "foreign:key"),
                ("task_id", "TASK-2"), ("run_id", "RUN-2"), ("attempt", 2)):
            with self.subTest(binding=field), self.assertRaises(ValueError):
                self._ack(records, expected_binding=dataclasses.replace(self.binding, **{field: replacement}))
        with self.assertRaises(ValueError):
            self._ack(records, expected_request=dataclasses.replace(self.request, max_output_bytes=1))
        with self.assertRaises(ValueError):
            self._ack({"result": self.applied})
        with self.assertRaises(ValueError):
            self._ack(dict(records, dispatch=dict(self.dispatch, temp_name=".orch-patch-" + "b" * 32 + ".tmp")))
        for path in (self._path().parent / ".execution-leftover.tmp",
                     self._intent_path().parent / ".journal-leftover.tmp"):
            self._write(path, b"unknown")
            with self.assertRaises(ValueError):
                self._ack(records)
            path.unlink()
        for original in (self._path(), self._intent_path()):
            alias = original.with_name("alias")
            os.link(original, alias)
            with self.assertRaises(ValueError):
                self._ack(records)
            alias.unlink()
        self.assertEqual({p: raw for p, (raw, _) in self._evidence().items()},
                         {p: raw for p, (raw, _) in before.items()})

    def test_renewal_checks_live_authority_before_sync_and_after_readback(self):
        records = self._terminal()
        before = self._evidence()
        for deny_at in (1, 2):
            calls = []
            def authority():
                calls.append(None)
                if len(calls) == deny_at:
                    raise ValueError("authority revoked")
            with self.subTest(deny_at=deny_at), self.assertRaises(ValueError):
                self._ack(records, authorize=authority)
            self.assertEqual(len(calls), deny_at)
        self.assertEqual(self._evidence(), before)

    def test_acknowledgement_requires_same_process_thread_runtime_lock(self):
        records = self._terminal()
        with self.assertRaisesRegex(ValueError, "patch_execution_lock_required"):
            self.store.acknowledge_existing(self.binding.run_id, self.request.operation_id,
                                           records, authorize=lambda: None)
        import concurrent.futures
        with module.runtime_operation_lock(self.home), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.store.acknowledge_existing, self.binding.run_id,
                                     self.request.operation_id, records, authorize=lambda: None)
            with self.assertRaisesRegex(ValueError, "patch_execution_lock_required"):
                future.result()

    @unittest.skipUnless(hasattr(os, "fork"), "requires process fork")
    def test_forked_child_cannot_acknowledge_existing_evidence(self):
        records = self._terminal()
        read_fd, write_fd = os.pipe()
        with module.runtime_operation_lock(self.home):
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(read_fd)
                    try:
                        self.store.acknowledge_existing(self.binding.run_id, self.request.operation_id,
                                                       records, authorize=lambda: None)
                    except ValueError as exc:
                        os.write(write_fd, str(exc).encode("ascii"))
                    else:
                        os.write(write_fd, b"ACCEPTED")
                finally:
                    os._exit(0)
            os.close(write_fd)
            try:
                result = os.read(read_fd, 128)
                _, exit_status = os.waitpid(pid, 0)
            finally:
                os.close(read_fd)
        self.assertEqual(exit_status, 0)
        self.assertEqual(result, b"patch_execution_lock_required")

    def test_closure_final_execution_and_intent_census_detects_earlier_drift(self):
        self._terminal("NOT_APPLIED")
        request = dataclasses.replace(self.request, operation_id="operation-2")
        intent = journal.build_intent_record(request, self.replacement)
        self.assertEqual(journal.OperationIntentJournal(self.home).create(intent, authorize=lambda: None)["status"], "CREATED")
        dispatch = dict(self.dispatch, request=dataclasses.asdict(request), intent_fingerprint=intent["fingerprint"])
        negative = dict(self.negative, request=dataclasses.asdict(request), intent_fingerprint=intent["fingerprint"],
                        dispatch_sha256=self._hash(dispatch))
        for record in (dispatch, negative):
            self.assertEqual(self._append(record)["status"], "CREATED")
        real_ack = module.PatchExecutionStore.acknowledge_existing
        for namespace in ("execution", "intent"):
            acknowledged = []
            def drift(store, run_id, operation_id, records, **kwargs):
                result = real_ack(store, run_id, operation_id, records, **kwargs)
                acknowledged.append(operation_id)
                if len(acknowledged) == 2:
                    earlier = self.request if acknowledged[0] == self.request.operation_id else request
                    path = self._path(request=earlier) if namespace == "execution" else self._intent_path(earlier)
                    path.write_bytes(path.read_bytes() + b"\n")
                return result
            with self.subTest(namespace=namespace), module.runtime_operation_lock(self.home), \
                    mock.patch.object(module.PatchExecutionStore, "acknowledge_existing", new=drift), \
                    self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                module.require_execution_resolved(self.home, self.binding.run_id,
                                                  self.binding, authorize=lambda: None)
            self.assertEqual(len(acknowledged), 2)

    def test_observational_inspection_and_legacy_guard_do_not_acknowledge(self):
        self._terminal()
        with mock.patch.object(module.os, "fsync", side_effect=AssertionError("observation fsynced")):
            self.assertTrue(self._inspect()["terminal"])
            self.assertEqual(module.execution_inventory(self.home)["status"], "READY")
            with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
                module.require_execution_resolved(self.home, self.binding.run_id, self.binding)

    def test_legacy_absence_and_foreign_run_keep_observational_binding_compatibility(self):
        binding = {"project_id": None, "writer_key": "LEGACY:key", "task_id": "TASK-1",
                   "run_id": "RUN-1", "attempt": 1}
        module.require_execution_resolved(self.home, self.binding.run_id, binding)
        self.assertFalse((self.runtime / "patch_execution").exists())
        self._terminal()
        module.require_execution_resolved(self.home, "different-run", binding)

    def test_closure_cannot_bypass_acknowledgement_without_explicit_authority(self):
        self._terminal()
        with module.runtime_operation_lock(self.home), self.assertRaisesRegex(
                ValueError, "patch_execution_unresolved"):
            module.require_execution_resolved(self.home, self.binding.run_id, self.binding)

    def test_absent_inspection_is_read_only_and_legacy_guard_accepts(self):
        self.assertEqual(self._inspect()["status"], "ABSENT")
        census = module.execution_inventory(self.home)
        self.assertEqual(census["status"], "ABSENT")
        self.assertEqual(census["operations"], [])
        self.assertFalse((self.runtime / "patch_execution").exists())
        module.require_execution_resolved(self.home, self.binding.run_id)

    def test_lock_never_creates_or_repairs_runtime(self):
        self.runtime.chmod(0o755)
        with self.assertRaises(ValueError):
            with module.runtime_operation_lock(self.home):
                self.fail("unsafe runtime locked")
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o755)
        self.runtime.chmod(0o700)
        other = self.base / "missing-home"
        with self.assertRaises((OSError, ValueError)):
            with module.runtime_operation_lock(other):
                self.fail("missing home locked")
        self.assertFalse(other.exists())

    def test_process_lock_contention_and_release_use_same_runtime_inode(self):
        before = self.runtime.stat().st_ino
        code = ("import sys; sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[1])) + "); "
                "from pathlib import Path; from orch.bridge_patch_execution import runtime_operation_lock; "
                "\ntry:\n with runtime_operation_lock(Path(sys.argv[1])): print('LOCKED')"
                "\nexcept ValueError as e: print(str(e))")
        with module.runtime_operation_lock(self.home):
            process = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(self.home)],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.strip(), "patch_execution_busy")
            with self.assertRaisesRegex(ValueError, "patch_execution_busy"):
                with module.runtime_operation_lock(self.home):
                    self.fail("recursive lock acquired")
        process = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(self.home)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.stdout.strip(), "LOCKED", process.stderr)
        self.assertEqual(self.runtime.stat().st_ino, before)

    def test_runtime_replacement_while_locked_is_not_accepted(self):
        with self.assertRaises(ValueError):
            with module.runtime_operation_lock(self.home):
                self.runtime.rename(self.home / "old-runtime")
                self.runtime.mkdir(mode=0o700)

    def test_append_requires_process_and_thread_lock_ownership(self):
        result = self.store.append(self.dispatch, authorize=lambda: None)
        self.assertEqual(result["status"], "REFUSED", result)
        self.assertFalse((self.runtime / "patch_execution").exists())
        with module.runtime_operation_lock(self.home):
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(self.store.append, self.dispatch,
                                         authorize=lambda: None).result()
            self.assertEqual(result["status"], "REFUSED", result)

    def test_append_chain_is_private_immutable_and_terminal(self):
        for record in (self.dispatch, self.publication, self.applied):
            result = self._append(record)
            self.assertEqual(result["status"], "CREATED", result)
        found = self._inspect()
        self.assertEqual(found["status"], "RECORDED", found)
        self.assertTrue(found["terminal"])
        self.assertEqual(found["records"], {"dispatch": self.dispatch,
                                             "publication": self.publication, "result": self.applied})
        for phase, record in found["records"].items():
            path = self._path(phase)
            before = path.read_bytes()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(self._append(record)["status"], "EXISTING")
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self._path().parent.iterdir()),
                         ["dispatch.json", "publication.json", "result.json"])
        with module.runtime_operation_lock(self.home):
            module.require_execution_resolved(self.home, self.binding.run_id, self.binding,
                                              authorize=lambda: None)
        census = module.execution_inventory(self.home)
        self.assertEqual(census["status"], "READY", census)
        self.assertEqual(len(census["operations"]), 1)
        self.assertEqual(len(census["files"]), 3)
        self.assertEqual(len(census["directories"]), 3)
        self.assertEqual(census, module.execution_inventory(self.home))
        self.assertNotIn("replacement", json.dumps(census["operations"][0]["records"]))

    def test_unresolved_dispatch_blocks_guard_but_negative_closes_id(self):
        self.assertEqual(self._append(self.dispatch)["status"], "CREATED")
        with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
            module.require_execution_resolved(self.home, self.binding.run_id)
        module.require_execution_resolved(self.home, "different-run")
        self.assertEqual(self._append(self.negative)["status"], "CREATED")
        with module.runtime_operation_lock(self.home):
            module.require_execution_resolved(self.home, self.binding.run_id, authorize=lambda: None)
        self.assertEqual(self._append(self.publication)["status"], "CONFLICT")

    def test_expected_ledger_binding_is_checked_even_for_terminal(self):
        self._append(self.dispatch)
        self._append(self.negative)
        with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
            module.require_execution_resolved(self.home, self.binding.run_id,
                                             dataclasses.replace(self.binding, attempt=2))

    def test_strict_codec_rejects_splicing_and_incomplete_proof(self):
        valid = {"dispatch": self.dispatch, "publication": self.publication, "result": self.applied}
        self.assertEqual(module.validate_execution_records(valid, self.intent), valid)
        mutations = [lambda r: r["publication"].update(dispatch_sha256="1" * 64),
                     lambda r: r["result"].update(publication_sha256="2" * 64),
                     lambda r: r["result"]["request"].update(max_output_bytes=1),
                     lambda r: r["dispatch"].update(intent_fingerprint="0" * 64),
                     lambda r: r["dispatch"]["original"].update(size_bytes=9),
                     lambda r: r["publication"]["staged"]["identity"].update(mode=stat.S_IFREG | 0o600),
                     lambda r: r["publication"]["staged"].update(xattrs_digest="1" * 64),
                     lambda r: r["result"]["basis"]["target"]["identity"].update(ino=99),
                     lambda r: r["result"]["basis"].update(temp_absent=False),
                     lambda r: r["dispatch"].update(temp_name="../elsewhere"),
                     lambda r: r["dispatch"]["anchors"].update(parent=[1, 99]),
                     lambda r: r["dispatch"]["original"]["identity"].update(nlink=2),
                     lambda r: r["dispatch"]["original"]["identity"].update(flags=1),
                     lambda r: r["dispatch"]["original"]["identity"].update(uid=True),
                     lambda r: r["dispatch"].update(extra="private"),
                     lambda r: r["dispatch"].update(created_at="yesterday")]
        for mutate in mutations:
            records = copy.deepcopy(valid)
            mutate(records)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                module.validate_execution_records(records, self.intent)
        for records in ({"publication": self.publication}, {"dispatch": self.dispatch, "result": self.applied}):
            with self.assertRaises(ValueError):
                module.validate_execution_records(records, self.intent)

    def test_codec_allows_rename_timestamp_change_but_not_other_metadata_drift(self):
        applied = copy.deepcopy(self.applied)
        applied["basis"]["target"]["identity"].update(ctime_ns=300, mtime_ns=300)
        records = {"dispatch": self.dispatch, "publication": self.publication, "result": applied}
        self.assertEqual(module.validate_execution_records(records, self.intent), records)
        negative = copy.deepcopy(self.negative)
        negative["basis"]["target"]["identity"].update(ctime_ns=300)
        with self.assertRaises(ValueError):
            module.validate_execution_records({"dispatch": self.dispatch, "result": negative}, self.intent)

    def test_canonical_record_hash_is_independent_of_mapping_order(self):
        expected = self._hash(self.dispatch)
        self.assertEqual(module.execution_record_hash(self.dispatch), expected)
        self.assertEqual(module.execution_record_hash(dict(reversed(list(self.dispatch.items())))), expected)

    def test_fresh_authority_and_conflicts_never_overwrite_dispatch(self):
        self.assertEqual(self._append(self.dispatch)["status"], "CREATED")
        original = self._path().read_bytes()
        changed = copy.deepcopy(self.dispatch)
        changed["temp_name"] = ".orch-patch-" + "b" * 32 + ".tmp"
        self.assertEqual(self._append(changed)["status"], "CONFLICT")
        def refuse():
            raise ValueError("private-capability-value")
        result = self._append(self.dispatch, refuse)
        self.assertEqual(result["status"], "REFUSED")
        self.assertNotIn("private", str(result))
        self.assertEqual(self._path().read_bytes(), original)

    def test_missing_or_changed_source_intent_makes_execution_unknown(self):
        self._append(self.dispatch)
        intent_path = next((self.runtime / "operation_journal").rglob("*.json"))
        intent_path.unlink()
        self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "patch_execution_unresolved"):
            module.require_execution_resolved(self.home, self.binding.run_id)

    def test_unexpected_entries_partial_chains_and_record_aliases_are_unknown(self):
        self._append(self.dispatch)
        parent = self._path().parent
        alias = parent / ".append-interrupted.tmp"
        os.link(self._path(), alias)
        self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")
        alias.unlink()
        self._path().unlink()
        self._write(self._path("publication"), json.dumps(self.publication).encode())
        self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")

    def test_unsafe_record_types_and_strict_json_are_unknown_without_read_side_effects(self):
        self._append(self.dispatch)
        original = self._path().read_bytes()
        for kind in ("symlink", "fifo", "mode", "partial", "duplicate", "oversize"):
            with self.subTest(kind=kind):
                self._path().unlink()
                if kind == "symlink":
                    self._path().symlink_to(self.base / "secret")
                elif kind == "fifo":
                    os.mkfifo(self._path(), 0o600)
                else:
                    raw = {"mode": original, "partial": b'{"phase":',
                           "duplicate": original[:-1] + b',"schema_version":1}',
                           "oversize": b"x" * 8193}[kind]
                    self._write(self._path(), raw)
                    if kind == "mode": self._path().chmod(0o644)
                self.assertEqual(self._inspect()["status"], "UNKNOWN")
                self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")
        self._path().unlink()
        self._write(self._path(), original)

    def test_hashed_namespace_rejects_spliced_operation_directory(self):
        self._append(self.dispatch)
        self._path().parent.rename(self._path().parent.with_name("f" * 64))
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")
        self.assertEqual(self._append(self.dispatch)["status"], "UNKNOWN")

    def test_append_faults_before_and_after_publication_do_not_claim_success(self):
        for fault in ("write", "file_fsync", "link", "unlink", "parent_fsync"):
            with self.subTest(fault=fault):
                temp = tempfile.TemporaryDirectory(prefix="orch-execution-fault-")
                self.addCleanup(temp.cleanup)
                home = Path(temp.name).resolve()
                (home / ".runtime").mkdir(mode=0o700)
                journal.OperationIntentJournal(home).create(self.intent, authorize=lambda: None)
                store = module.PatchExecutionStore(home)
                path = home / self._path().relative_to(self.home)
                fsync = os.fsync
                def fail_fsync(fd):
                    is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if (fault == "file_fsync" and not is_dir) or (
                            fault == "parent_fsync" and is_dir and path.exists()):
                        raise OSError("injected fsync failure")
                    return fsync(fd)
                target = {"write": "write", "file_fsync": "fsync", "link": "link",
                          "unlink": "unlink", "parent_fsync": "fsync"}[fault]
                effect = fail_fsync if target == "fsync" else OSError("injected failure")
                with module.runtime_operation_lock(home), mock.patch.object(module.os, target, side_effect=effect):
                    result = store.append(self.dispatch, authorize=lambda: None)
                self.assertEqual(result["status"], "UNKNOWN", result)
                if fault == "parent_fsync":
                    self.assertEqual(store.inspect(self.binding.run_id, self.request.operation_id)["status"], "RECORDED")
                    self.assertEqual(path.read_bytes(), json.dumps(self.dispatch, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True).encode())
                elif fault == "unlink":
                    self.assertTrue(path.exists())
                    self.assertEqual(store.inspect(self.binding.run_id, self.request.operation_id)["status"], "UNKNOWN")
                else:
                    self.assertFalse(path.exists())

    def test_final_authority_denial_leaves_no_published_record(self):
        calls = []
        def authorize():
            calls.append(None)
            if self._path().parent.exists() and any(
                    entry.name.startswith(".execution-") for entry in self._path().parent.iterdir()):
                raise ValueError("revoked")
        result = self._append(self.dispatch, authorize)
        self.assertEqual(result["status"], "REFUSED", result)
        self.assertFalse(self._path().exists())
        self.assertEqual(self._inspect()["status"], "UNKNOWN")

    def test_existing_empty_operation_directory_is_partial_evidence(self):
        for path in (self._path().parents[2], self._path().parents[1], self._path().parent):
            path.mkdir(mode=0o700)
        self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")
        self.assertEqual(self._append(self.dispatch)["status"], "UNKNOWN")

    def test_empty_namespace_and_run_directories_are_incomplete_census(self):
        execution = self.runtime / "patch_execution"
        execution.mkdir(mode=0o700)
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")
        self.assertEqual(self._append(self.dispatch)["status"], "UNKNOWN")
        (execution / self._path().parents[1].name).mkdir(mode=0o700)
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")

    def test_uncertain_execution_directory_creation_blocks_later_dispatch(self):
        fsync = os.fsync
        def refuse_new_execution_dir(fd):
            if (self.runtime / "patch_execution").exists() and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("uncertain new directory")
            return fsync(fd)
        with mock.patch.object(module.os, "fsync", side_effect=refuse_new_execution_dir):
            result = self._append(self.dispatch)
        self.assertEqual(result["status"], "UNKNOWN", result)
        self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")
        self.assertEqual(self._append(self.dispatch)["status"], "UNKNOWN")
        self.assertFalse(self._path().exists())

    @unittest.skipUnless(hasattr(os, "fork"), "requires process fork")
    def test_forked_child_cannot_inherit_append_authority(self):
        read_fd, write_fd = os.pipe()
        with module.runtime_operation_lock(self.home):
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(read_fd)
                    result = self.store.append(self.dispatch, authorize=lambda: None)
                    os.write(write_fd, result["status"].encode("ascii"))
                finally:
                    os._exit(0)
            os.close(write_fd)
            try:
                status = os.read(read_fd, 32)
                _, exit_status = os.waitpid(pid, 0)
            finally:
                os.close(read_fd)
        self.assertEqual(exit_status, 0)
        self.assertEqual(status, b"REFUSED")
        self.assertFalse(self._path().exists())

    def test_record_changed_during_directory_ack_is_unknown(self):
        fsync = os.fsync
        changed = [False]
        def modify(fd):
            if self._path().exists() and stat.S_ISDIR(os.fstat(fd).st_mode) and not changed[0]:
                changed[0] = True
                raw = json.loads(self._path().read_text())
                raw["created_at"] = "2026-10-11T13:00:00.000000Z"
                self._write(self._path(), json.dumps(raw).encode())
            return fsync(fd)
        with mock.patch.object(module.os, "fsync", side_effect=modify):
            result = self._append(self.dispatch)
        self.assertEqual(result["status"], "UNKNOWN", result)

    def test_capacity_blocks_new_operations_and_preserves_known_duplicates(self):
        self._append(self.dispatch)
        request = dataclasses.replace(self.request, operation_id="operation-2")
        intent = journal.build_intent_record(request, self.replacement)
        journal.OperationIntentJournal(self.home).create(intent, authorize=lambda: None)
        candidate = copy.deepcopy(self.dispatch)
        candidate.update(request=dataclasses.asdict(request), intent_fingerprint=intent["fingerprint"])
        for name in ("EXECUTION_MAX_OPERATIONS", "EXECUTION_MAX_OPERATIONS_PER_RUN"):
            with mock.patch.object(module, name, 1):
                self.assertEqual(self._append(candidate)["status"], "REFUSED")
                self.assertEqual(self._append(self.dispatch)["status"], "EXISTING")
        self.assertFalse(self._path(request=request).exists())

    def test_inventory_does_not_accept_changing_record_between_observations(self):
        self._append(self.dispatch)
        read = module.PatchExecutionStore._inventory
        count = [0]
        def change(store):
            result = read(store)
            count[0] += 1
            if count[0] == 1:
                raw = json.loads(self._path().read_text())
                raw["created_at"] = "2026-10-11T13:00:00.000000Z"
                self._write(self._path(), json.dumps(raw).encode())
            return result
        with mock.patch.object(module.PatchExecutionStore, "_inventory", new=change):
            self.assertEqual(module.execution_inventory(self.home)["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
