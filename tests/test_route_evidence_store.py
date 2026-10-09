"""Regression tests for the versioned route-evidence storage seam."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from orch.route_evidence_store import (
    ROUTE_EVIDENCE_MAX_BYTES, RouteEvidenceCodec, RouteEvidenceStore,
)


def v1_record(**overrides):
    evidence = {
        "schema_version": 1,
        "run_id": "RUN-V1",
        "task_id": "TASK-V1",
        "surface": "ordinary_chat",
        "transport": "rdc",
        "device_id": "device-1",
        "device_name": "Mac.test",
        "model": "UNKNOWN",
        "reasoning": "UNKNOWN",
        "usage": "UNKNOWN",
        "observed_at": "2026-10-09T00:00:00+00:00",
        "source": "operator_observed",
        "work_used": "no",
        "codex_execution_used": "no",
        "model_api_used": "no",
        "external_provider_used": "no",
    }
    evidence.update(overrides)
    return evidence


class RouteEvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.store = RouteEvidenceStore(self.home)

    def test_v1_codec_retains_identity_and_exact_json_bytes(self):
        record = v1_record(device_name="Mac café")
        self.assertIs(RouteEvidenceCodec.validate(record), record)
        expected = (
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        self.assertEqual(RouteEvidenceCodec.encode(record), expected)
        path = self.store.write_once(record)
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        result = self.store.read("RUN-V1")
        self.assertEqual(result["status"], "RECORDED")
        self.assertEqual(result["path"], str(path))
        self.assertEqual(result["route_evidence"], record)
        self.assertEqual(result["bytes"], len(expected))
        self.assertEqual(result["mode"], "0o600")
        self.assertNotIn("acceptance", result)  # Only caller provides that decision.

    def test_version_dispatch_rejects_unknowns_with_historical_error_order(self):
        cases = (
            ({"schema_version": 2}, "route_evidence_invalid_schema"),
            ({"schema_version": True}, "route_evidence_invalid_schema"),
            ({"schema_version": "1"}, "route_evidence_invalid_schema"),
            ({"schema_version": 2, "unknown_field": "x"},
             "route_evidence_unknown_field:unknown_field"),
            ({"transport": "bridge"}, "route_evidence_invalid_transport"),
            ({"surface": "work"}, "route_evidence_invalid_surface"),
            ({"work_used": "UNKNOWN"},
             "route_evidence_invalid_observation:work_used"),
            ({"device_id": ""}, "rdc_device_id_required"),
        )
        for overrides, error in cases:
            with self.subTest(overrides=overrides):
                record = v1_record(**overrides)
                with self.assertRaises(ValueError) as cm:
                    self.store.write_once(record)
                self.assertEqual(str(cm.exception), error)
                self.assertFalse((self.home / "route-evidence").exists())

    def test_missing_read_does_not_create_home_and_bad_id_fails_closed(self):
        self.assertEqual(
            self.store.read("RUN-V1"),
            {
                "status": "UNVERIFIED",
                "path": str(self.home.resolve() / "route-evidence" / "RUN-V1.json"),
            },
        )
        self.assertFalse(self.home.exists())
        for run_id in ("../RUN-V1", "", "invalid/name", "x" * 201):
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(
                    ValueError, "^route_evidence_invalid_run_id$"
                ):
                    self.store.read(run_id)
        self.assertFalse(self.home.exists())

    def test_duplicate_write_retains_original_bytes_and_permissions(self):
        path = self.store.write_once(v1_record())
        before = path.read_bytes()
        with self.assertRaisesRegex(
            ValueError, "^route_evidence_already_recorded$"
        ):
            self.store.write_once(v1_record(model="CHANGED"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_historical_v1_read_only_repairs_mode_not_content(self):
        path = self.store.write_once(v1_record())
        original = path.read_bytes()
        os.chmod(path, 0o644)
        result = self.store.read("RUN-V1")
        self.assertEqual(result["mode"], "0o600")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_bytes(), original)

    def test_wrong_file_run_id_fails_after_safe_decode(self):
        path = self.store.write_once(v1_record())
        original = path.read_bytes()
        wrong = path.with_name("RUN-DIFFERENT.json")
        wrong.write_bytes(original)
        with self.assertRaisesRegex(
            ValueError, "^route_evidence_run_binding_mismatch$"
        ):
            self.store.read("RUN-DIFFERENT")
        self.assertEqual(wrong.read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)

    def test_symlink_record_is_unsafe_without_touching_target(self):
        directory = self.home / "route-evidence"
        directory.mkdir(parents=True, mode=0o700)
        target = self.root / "external.json"
        target.write_bytes(RouteEvidenceCodec.encode(v1_record()))
        path = directory / "RUN-V1.json"
        path.symlink_to(target)
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "^route_evidence_unsafe$"):
            self.store.read("RUN-V1")
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(path.is_symlink())

    def test_nonregular_record_is_rejected(self):
        directory = self.home / "route-evidence"
        path = directory / "RUN-V1.json"
        path.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "^route_evidence_unsafe$"):
            self.store.read("RUN-V1")
        self.assertTrue(path.is_dir())

    def test_oversize_record_is_rejected_without_overwrite(self):
        directory = self.home / "route-evidence"
        directory.mkdir(parents=True)
        path = directory / "RUN-V1.json"
        payload = b"x" * (ROUTE_EVIDENCE_MAX_BYTES + 1)
        path.write_bytes(payload)
        with self.assertRaisesRegex(ValueError, "^route_evidence_too_large$"):
            self.store.read("RUN-V1")
        self.assertEqual(path.read_bytes(), payload)

    def test_invalid_json_and_unknown_version_do_not_rewrite_history(self):
        directory = self.home / "route-evidence"
        directory.mkdir(parents=True)
        path = directory / "RUN-V1.json"
        for raw, error in (
            (b"{", "route_evidence_invalid_json"),
            (RouteEvidenceCodec.encode(v1_record()).replace(
                b'"schema_version": 1', b'"schema_version": 2'
            ), "route_evidence_invalid_schema"),
        ):
            with self.subTest(error=error):
                path.write_bytes(raw)
                with self.assertRaises(ValueError) as cm:
                    self.store.read("RUN-V1")
                self.assertEqual(str(cm.exception), error)
                self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
