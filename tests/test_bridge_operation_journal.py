"""Immutable intent persistence, adversarial entries, and durability faults."""

import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from orch import bridge_operation_journal as module
from orch.worker_operations import WorkerOperationRequest, WorkerRunBinding


class OperationIntentJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orch-intent-journal-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o755)
        (self.home / ".runtime").mkdir(mode=0o700)
        self.binding = WorkerRunBinding("fixture", "fixture:key", "TASK-1", "RUN-1", 1)
        self.request = WorkerOperationRequest(self.binding, "operation-1", "file.patch", "README.md")
        self.replacement = {
            "schema_version": 1,
            "baseline": {"sha256": hashlib.sha256(b"original").hexdigest(), "size_bytes": 8},
            "sha256": hashlib.sha256(b"private-replacement-payload").hexdigest(), "size_bytes": 27,
        }
        self.record = module.build_intent_record(self.request, self.replacement)
        self.store = module.OperationIntentJournal(self.home)

    def _path(self, request=None):
        request = request or self.request
        return (self.home / ".runtime" / "operation_journal"
                / hashlib.sha256(request.binding.run_id.encode()).hexdigest()
                / (hashlib.sha256(request.operation_id.encode()).hexdigest() + ".json"))

    def _create(self, record=None, authorize=lambda: None):
        return self.store.create(record or self.record, authorize=authorize)

    def _inspect(self):
        return self.store.inspect(self.binding.run_id, self.request.operation_id)

    def _write_raw(self, raw):
        path = self._path()
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.parent.chmod(0o700)
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def test_creation_is_metadata_only_private_and_immutable_across_restart(self):
        before = stat.S_IMODE(self.home.stat().st_mode)
        created = self._create()
        self.assertEqual(created["status"], "CREATED", created)
        self.assertEqual(created["record"], self.record)
        path = self._path()
        raw = path.read_bytes()
        self.assertNotIn(b"private-replacement-payload", raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), before)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertEqual(self._inspect(), {"status": "RECORDED", "record": self.record})
        fresh = module.OperationIntentJournal(self.home)
        rebuilt = module.build_intent_record(self.request, copy.deepcopy(self.replacement))
        result = fresh.create(rebuilt, authorize=lambda: None)
        self.assertEqual(result, {"status": "EXISTING", "record": self.record})
        self.assertEqual(path.read_bytes(), raw)

    def test_duplicate_and_conflict_still_require_fresh_authority(self):
        self.assertEqual(self._create()["status"], "CREATED")
        changed = module.build_intent_record(dataclasses.replace(self.request, target="other.txt"),
                                           self.replacement)
        self.assertEqual(self._create(changed)["status"], "CONFLICT")
        def refuse():
            raise ValueError("secret-capability-details")
        for record in (self.record, changed):
            result = self._create(record, refuse)
            self.assertEqual(result["status"], "REFUSED", result)
            self.assertNotIn("secret", str(result))

    def test_inspect_and_absent_census_do_not_create_or_repair_directories(self):
        (self.home / ".runtime").rmdir()
        before = list(self.home.iterdir())
        self.assertEqual(self._inspect()["status"], "ABSENT")
        first = module.journal_inventory(self.home)
        self.assertEqual(first["status"], "ABSENT", first)
        self.assertEqual(first, module.journal_inventory(self.home))
        self.assertEqual(list(self.home.iterdir()), before)
        (self.home / ".runtime").mkdir(mode=0o700)
        self.assertNotEqual(first, module.journal_inventory(self.home))

    def test_complete_request_and_replacement_binding_is_validated(self):
        for mutate in (
            lambda r: r.update(extra=True),
            lambda r: r["request"].pop("max_output_bytes"),
            lambda r: r["request"]["binding"].update(attempt=True),
            lambda r: r["request"].update(kind="file.read"),
            lambda r: r["request"].update(operation_id="../escape"),
            lambda r: r["request"]["binding"].update(run_id="../escape"),
            lambda r: r["replacement"].update(size_bytes=True),
            lambda r: r["replacement"]["baseline"].update(sha256="A" * 64),
            lambda r: r.update(fingerprint="0" * 64),
            lambda r: r.update(created_at="yesterday"),
            lambda r: r.update(state="SUCCEEDED"),
        ):
            record = copy.deepcopy(self.record)
            mutate(record)
            with self.subTest(record=record), self.assertRaises(ValueError):
                module.validate_intent_record(record)
        same = copy.deepcopy(self.replacement)
        same.update(same["baseline"])
        with self.assertRaises(ValueError):
            module.build_intent_record(self.request, same)

    def test_fingerprint_covers_all_request_metadata_but_not_timestamp(self):
        first = module.request_fingerprint(self.request, self.replacement)
        for changed in (dataclasses.replace(self.request, max_output_bytes=1),
                        dataclasses.replace(self.request, operation_id="different"),
                        dataclasses.replace(self.request, binding=dataclasses.replace(self.binding, attempt=2))):
            self.assertNotEqual(first, module.request_fingerprint(changed, self.replacement))
        second = copy.deepcopy(self.record)
        second["created_at"] = "2026-01-01T00:00:00.000000Z"
        self.assertEqual(module.validate_intent_record(second)["fingerprint"], first)

    def test_unsafe_record_types_modes_and_link_count_are_unknown(self):
        for kind in ("symlink", "fifo", "hardlink", "mode"):
            with self.subTest(kind=kind):
                path = self._write_raw(json.dumps(self.record).encode())
                if kind == "symlink":
                    path.unlink()
                    path.symlink_to(self.base / "private")
                elif kind == "fifo":
                    path.unlink()
                    os.mkfifo(path, 0o600)
                elif kind == "hardlink":
                    os.link(path, self.base / "extra-link")
                else:
                    path.chmod(0o644)
                self.assertEqual(self._inspect()["status"], "UNKNOWN")
                self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")
                path.unlink()
                if kind == "hardlink":
                    (self.base / "extra-link").unlink()

    def test_strict_codec_corrupt_oversized_duplicate_keys_and_filename_mismatch(self):
        for raw in (b"not-json", b"x" * 8193,
                    json.dumps(self.record).encode().replace(b'"state":', b'"state":"INTENT_RECORDED","state":', 1),
                    b'{"schema_version":NaN}'):
            path = self._write_raw(raw)
            self.assertEqual(self._inspect()["status"], "UNKNOWN")
            self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")
            path.unlink()
        path = self._write_raw(json.dumps(self.record).encode())
        path.rename(path.with_name("0" * 64 + ".json"))
        self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")

    def test_census_is_stable_after_read_and_changes_on_record_or_directory_mutation(self):
        self._create()
        initial = module.journal_inventory(self.home)
        self.assertEqual(initial["status"], "READY", initial)
        self.assertEqual(initial, module.journal_inventory(self.home))
        relative = self._path().relative_to(self.home).as_posix()
        self.assertEqual(set(initial["files"]), {relative})
        self.assertEqual(initial["files"][relative]["sha256"],
                         hashlib.sha256(self._path().read_bytes()).hexdigest())
        for identity in initial["directories"].values():
            self.assertNotIn("atime_ns", identity)
            self.assertIn("mtime_ns", identity)
            self.assertIn("ctime_ns", identity)
        (self._path().parent / ".foreign-temp").write_text("private")
        result = module.journal_inventory(self.home)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("private", str(result))

    def test_runtime_journal_run_and_home_anchor_substitutions_are_refused(self):
        self._create()
        for directory in (self.home / ".runtime", self._path().parent.parent, self._path().parent):
            directory.chmod(0o755)
            self.assertEqual(self._inspect()["status"], "UNKNOWN")
            self.assertEqual(self._create()["status"], "UNKNOWN")
            directory.chmod(0o700)
        old = self.base / "old-home"
        self.home.rename(old)
        self.home.mkdir(mode=0o755)
        (self.home / ".runtime").mkdir(mode=0o700)
        self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(self._create()["status"], "UNKNOWN")

    def test_authority_revoked_immediately_before_publication_removes_only_owned_temp(self):
        calls = []
        def authorize():
            calls.append(True)
            if len(calls) == 2:
                raise ValueError("revoked-secret")
        result = self._create(authorize=authorize)
        self.assertEqual(result["status"], "REFUSED", result)
        self.assertFalse(self._path().exists())
        self.assertEqual(list(self._path().parent.iterdir()), [])

    def test_file_fsync_failure_does_not_publish_and_directory_fsync_failure_preserves_final(self):
        original = os.fsync
        def fail_file(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("private-file-sync")
            return original(fd)
        with mock.patch.object(module.os, "fsync", side_effect=fail_file):
            result = self._create()
        self.assertEqual(result["status"], "UNKNOWN", result)
        self.assertFalse(self._path().exists())
        self.assertEqual(list(self._path().parent.iterdir()), [])
        def fail_after_publication(fd):
            if self._path().exists() and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("private-dir-sync")
            return original(fd)
        with mock.patch.object(module.os, "fsync", side_effect=fail_after_publication):
            result = self._create()
        self.assertEqual(result["status"], "UNKNOWN", result)
        self.assertTrue(self._path().exists())
        self.assertEqual(self._inspect()["status"], "RECORDED")
        self.assertEqual(self._create()["status"], "EXISTING")

    def test_capacity_limits_do_not_evict_and_duplicate_survives_full_capacity(self):
        with mock.patch.object(module, "JOURNAL_MAX_RECORDS_PER_RUN", 2):
            self.assertEqual(self._create()["status"], "CREATED")
            second = module.build_intent_record(dataclasses.replace(self.request, operation_id="two"),
                                                self.replacement)
            self.assertEqual(self._create(second)["status"], "CREATED")
            third = module.build_intent_record(dataclasses.replace(self.request, operation_id="three"),
                                               self.replacement)
            self.assertEqual(self._create(third)["status"], "REFUSED")
            self.assertEqual(self._create()["status"], "EXISTING")
        with mock.patch.object(module, "JOURNAL_MAX_RECORDS", 2):
            foreign = module.build_intent_record(dataclasses.replace(self.request,
                binding=dataclasses.replace(self.binding, run_id="another")), self.replacement)
            self.assertEqual(self._create(foreign)["status"], "REFUSED")
        self.assertEqual(len(module.journal_inventory(self.home)["records"]), 2)

    def test_actual_per_run_capacity_and_oversized_census_fail_closed(self):
        for number in range(64):
            request = dataclasses.replace(self.request, operation_id="entry-" + str(number))
            record = module.build_intent_record(request, self.replacement)
            path = self._path(request)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.parent.parent.chmod(0o700)
            path.write_bytes(json.dumps(record).encode())
            path.chmod(0o600)
        self.assertEqual(module.journal_inventory(self.home)["status"], "READY")
        self.assertEqual(self._create()["status"], "REFUSED")
        record = module.build_intent_record(dataclasses.replace(self.request, operation_id="entry-64"),
                                           self.replacement)
        path = self._path(dataclasses.replace(self.request, operation_id="entry-64"))
        path.write_bytes(json.dumps(record).encode())
        path.chmod(0o600)
        self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")
        self.assertEqual(self._create()["status"], "UNKNOWN")

    def test_ancestor_sync_failure_does_not_publish_and_retry_remains_fresh(self):
        original = os.fsync
        failed = []
        def fail_parent(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
                failed.append(True)
                raise OSError("private-parent-sync")
            return original(fd)
        with mock.patch.object(module.os, "fsync", side_effect=fail_parent):
            self.assertEqual(self._create()["status"], "UNKNOWN")
        self.assertFalse(self._path().exists())
        calls = []
        synced = []
        def sync(fd):
            synced.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return original(fd)
        with mock.patch.object(module.os, "fsync", side_effect=sync):
            result = self._create(authorize=lambda: calls.append(True))
        self.assertEqual(result["status"], "CREATED")
        self.assertEqual(len(calls), 2)
        for directory in (self.home, self.home / ".runtime", self._path().parent.parent, self._path().parent):
            info = directory.stat()
            self.assertIn((info.st_dev, info.st_ino), synced)

    def test_no_replace_publication_collision_observes_exact_record_without_overwrite(self):
        original = os.link
        with mock.patch.object(module.os, "link", side_effect=lambda source, destination, **kw:
                               self._external_collision(original, source, destination, kw)):
            result = self._create()
        self.assertEqual(result["status"], "EXISTING", result)
        self.assertEqual(list(self._path().parent.iterdir()), [self._path()])
        self.assertEqual(self._inspect()["record"], self.record)

    @staticmethod
    def _external_collision(link, source, destination, kwargs):
        link(source, destination, **kwargs)
        raise FileExistsError("collision-private")

    def test_link_error_before_and_after_actual_link_never_deletes_final_evidence(self):
        original = os.link
        with mock.patch.object(module.os, "link", side_effect=OSError("private-before-link")):
            result = self._create()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(self._path().exists())
        self.assertEqual(list(self._path().parent.iterdir()), [])
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("private-lost-link-ack")
        with mock.patch.object(module.os, "link", side_effect=lost_ack):
            result = self._create()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(list(self._path().parent.iterdir()), [self._path()])
        self.assertEqual(module.OperationIntentJournal(self.home).inspect(
            self.binding.run_id, self.request.operation_id)["status"], "RECORDED")

    def test_lost_temp_unlink_preserves_both_aliases_and_fails_census_closed(self):
        with mock.patch.object(module.os, "unlink", side_effect=OSError("private-temp-unlink")):
            result = self._create()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(self._path().exists())
        self.assertEqual(self._path().stat().st_nlink, 2)
        self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")
        self.assertEqual(len(list(self._path().parent.iterdir())), 2)

    def test_partial_writes_are_completed_and_zero_write_is_unknown(self):
        original = os.write
        calls = []
        def partial(fd, data):
            calls.append(len(data))
            return original(fd, data[:7])
        with mock.patch.object(module.os, "write", side_effect=partial):
            self.assertEqual(self._create()["status"], "CREATED")
        self.assertGreater(len(calls), 1)
        self.assertEqual(self._inspect()["record"], self.record)
        self._path().unlink()
        with mock.patch.object(module.os, "write", return_value=0):
            self.assertEqual(self._create()["status"], "UNKNOWN")
        self.assertFalse(self._path().exists())
        self.assertEqual(list(self._path().parent.iterdir()), [])

    def test_foreign_temp_replacement_is_preserved_and_not_published(self):
        calls = []
        foreign = b"foreign-private-evidence"
        def substitute():
            calls.append(True)
            if len(calls) == 2:
                temp = next(self._path().parent.glob(".intent-*.tmp"))
                temp.rename(self.base / "original-temp")
                temp.write_bytes(foreign)
                temp.chmod(0o600)
        result = self._create(authorize=substitute)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(self._path().exists())
        temp = next(self._path().parent.glob(".intent-*.tmp"))
        self.assertEqual(temp.read_bytes(), foreign)
        self.assertNotIn("foreign-private", str(result))
        self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")

    def test_anchor_swap_at_final_authority_does_not_write_replacement_anchor(self):
        calls = []
        journal = self.home / ".runtime" / "operation_journal"
        old = self.base / "old-journal"
        def substitute():
            calls.append(True)
            if len(calls) == 2:
                journal.rename(old)
                journal.mkdir(mode=0o700)
        result = self._create(authorize=substitute)
        self.assertEqual(result["status"], "UNKNOWN", result)
        self.assertEqual(list(journal.iterdir()), [])
        self.assertEqual(list(next(old.iterdir()).iterdir()), [])

    def test_home_anchor_disappearance_is_unknown_not_absent(self):
        (self.home / ".runtime").rmdir()
        self.home.rmdir()
        self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(self._create()["status"], "UNKNOWN")
        self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")

    def test_census_detects_content_change_after_first_complete_record_read(self):
        self._create()
        original = module._read_record
        calls = []
        def changed(*args, **kwargs):
            observed = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                later = copy.deepcopy(self.record)
                later["created_at"] = "2026-01-01T00:00:00.000000Z"
                self._path().write_bytes(json.dumps(later).encode())
            return observed
        with mock.patch.object(module, "_read_record", side_effect=changed):
            self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")

    def test_record_disappearance_after_read_is_unknown_not_absent(self):
        self._create()
        original = os.read
        removed = []
        def disappear(fd, size):
            data = original(fd, size)
            if data and not removed:
                removed.append(True)
                self._path().unlink()
            return data
        with mock.patch.object(module.os, "read", side_effect=disappear):
            self.assertEqual(self._inspect()["status"], "UNKNOWN")
        self.assertEqual(self._inspect()["status"], "ABSENT")

    def test_census_journal_disappearance_between_passes_is_unknown(self):
        self._create()
        original = module.OperationIntentJournal._inventory
        observed = []
        def disappear(store):
            result = original(store)
            if not observed:
                observed.append(True)
                self._path().unlink()
                self._path().parent.rmdir()
                self._path().parent.parent.rmdir()
            return result
        with mock.patch.object(module.OperationIntentJournal, "_inventory", new=disappear):
            self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")
        self.assertEqual(module.journal_inventory(self.home)["status"], "ABSENT")

    def test_empty_journal_run_directory_and_absent_appearance_boundary(self):
        absent = module.journal_inventory(self.home)
        self.assertEqual(absent["status"], "ABSENT")
        self._path().parent.mkdir(parents=True, mode=0o700)
        self._path().parent.parent.chmod(0o700)
        present = module.journal_inventory(self.home)
        self.assertEqual(present["status"], "READY", present)
        self.assertEqual(present["records"], [])
        self.assertNotEqual(absent, present)
        self._path().parent.chmod(0o755)
        self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")

    def test_invalid_inspection_ids_fail_closed_without_touching_raw_paths(self):
        for run, operation in (("../outside", "operation-1"), ("RUN-1", "../outside"),
                               ("RUN-1", "x" * 201), (None, "operation-1")):
            self.assertEqual(self.store.inspect(run, operation)["status"], "UNKNOWN")
        self.assertFalse((self.home / ".runtime" / "operation_journal").exists())

    def test_record_owner_and_directory_owner_must_match_current_user(self):
        self._create()
        original = os.getuid()
        with mock.patch.object(module.os, "getuid", return_value=original + 1):
            self.assertEqual(self._inspect()["status"], "UNKNOWN")
            self.assertEqual(self._create()["status"], "UNKNOWN")
            self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")

    def test_runtime_and_journal_symlink_or_fifo_never_follow_or_block(self):
        for path in (self.home / ".runtime", self.home / ".runtime" / "operation_journal"):
            for kind in ("symlink", "fifo"):
                with self.subTest(path=path, kind=kind):
                    if path.exists():
                        path.rmdir()
                    if kind == "symlink":
                        path.symlink_to(self.base)
                    else:
                        os.mkfifo(path, 0o600)
                    self.assertEqual(self._inspect()["status"], "UNKNOWN")
                    self.assertEqual(self._create()["status"], "UNKNOWN")
                    self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")
                    path.unlink()
                if path.name == ".runtime":
                    path.mkdir(mode=0o700)

    def test_process_collision_publishes_one_record_and_busy_is_readback_only(self):
        # The first child holds the private journal descriptor lock at its final
        # authorize boundary; another independent process must not mutate/retry.
        child = r'''
import json, sys, time
from pathlib import Path
from orch.bridge_operation_journal import OperationIntentJournal
home, record_file, ready_file, release_file = map(Path, sys.argv[1:])
record = json.loads(record_file.read_text())
calls = []
def authorize():
    calls.append(True)
    if len(calls) == 2:
        ready_file.write_text("ready")
        deadline = time.monotonic() + 8
        while not release_file.exists():
            if time.monotonic() > deadline:
                raise ValueError("fixture_timeout")
            time.sleep(.01)
print(json.dumps(OperationIntentJournal(home).create(record, authorize=authorize)))
'''
        record_file = self.base / "record.json"
        record_file.write_text(json.dumps(self.record))
        ready, release = self.base / "ready", self.base / "release"
        process = subprocess.Popen([sys.executable, "-c", child, str(self.home), str(record_file),
                                    str(ready), str(release)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        import time
        deadline = time.monotonic() + 8
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        result = self._create()
        self.assertEqual(result, {"status": "UNKNOWN", "error_code": "journal_busy"})
        self.assertFalse(self._path().exists())
        self.assertEqual(module.journal_inventory(self.home)["status"], "UNKNOWN")
        release.write_text("release")
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "CREATED")
        self.assertEqual(self._inspect()["status"], "RECORDED")
        self.assertEqual(list(self._path().parent.iterdir()), [self._path()])
        self.assertEqual(module.journal_inventory(self.home)["status"], "READY")


if __name__ == "__main__":
    unittest.main()
