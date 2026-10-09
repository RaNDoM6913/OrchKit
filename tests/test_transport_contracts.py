"""Version-1 in-memory RDC marker and route-evidence compatibility contracts."""

import json
import unittest

from orch import dispatcher, overview, readiness, transport_contracts
from orch.transport_contracts import (
    ROUTE_OBSERVATION_VALUES,
    _bounded_marker_text,
    validate_rdc_marker,
    validate_route_evidence,
)


def marker_v1():
    return {
        "schema_version": 1,
        "device_id": "device-1",
        "device_name": "Mac.test",
        "recorded_at": "2026-10-09T12:00:00+00:00",
        "source": "chatgpt_rdc_bootstrap",
    }


def evidence_v1():
    return {
        "schema_version": 1,
        "run_id": "RUN-A1",
        "task_id": "TASK-A",
        "surface": "ordinary_chat",
        "transport": "rdc",
        "device_id": "device-1",
        "device_name": "Mac.test",
        "model": "UNKNOWN",
        "reasoning": "UNKNOWN",
        "usage": "UNKNOWN",
        "observed_at": "2026-10-09T12:00:00+00:00",
        "source": "operator_observed",
        "work_used": "unknown",
        "codex_execution_used": "unknown",
        "model_api_used": "unknown",
        "external_provider_used": "unknown",
    }


class TransportContractTests(unittest.TestCase):
    def assert_invalid(self, validator, value, expected):
        with self.assertRaises(ValueError) as raised:
            validator(value)
        self.assertEqual(str(raised.exception), expected)

    def test_dispatcher_and_inspection_imports_remain_identical(self):
        for name in (
            "validate_rdc_marker", "validate_route_evidence",
            "_bounded_marker_text", "ROUTE_OBSERVATION_VALUES",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(dispatcher, name),
                    getattr(transport_contracts, name),
                )
        self.assertIs(overview.validate_rdc_marker, validate_rdc_marker)
        self.assertIs(readiness.validate_rdc_marker, validate_rdc_marker)

    def test_marker_v1_is_accepted_without_normalizing_or_copying(self):
        value = marker_v1()
        value["device_id"] = "  device-1  "
        before = json.dumps(value, sort_keys=True, ensure_ascii=False)
        self.assertIs(validate_rdc_marker(value), value)
        self.assertEqual(json.dumps(value, sort_keys=True, ensure_ascii=False), before)
        self.assertEqual(value["device_id"], "  device-1  ")

    def test_marker_schema_rejects_unknown_versions_and_bool(self):
        for version in (None, 0, 2, True, False, 1.0, "1", [], {}):
            with self.subTest(version=repr(version)):
                value = marker_v1()
                value["schema_version"] = version
                self.assert_invalid(
                    validate_rdc_marker, value, "rdc_marker_invalid_schema"
                )

    def test_marker_unknown_keys_are_rejected_in_sorted_order(self):
        value = marker_v1()
        value["z_extension"] = "future"
        value["a_extension"] = "future"
        self.assert_invalid(
            validate_rdc_marker, value, "rdc_marker_unknown_field:a_extension"
        )
        del value["a_extension"]
        self.assert_invalid(
            validate_rdc_marker, value, "rdc_marker_unknown_field:z_extension"
        )

    def test_marker_required_fields_and_types(self):
        for field in ("device_id", "device_name", "recorded_at"):
            for bad in (None, "", " \t ", 0, False, [], {}):
                with self.subTest(field=field, bad=repr(bad)):
                    value = marker_v1()
                    value[field] = bad
                    self.assert_invalid(
                        validate_rdc_marker, value, "rdc_" + field + "_required"
                    )
            missing = marker_v1()
            del missing[field]
            self.assert_invalid(
                validate_rdc_marker, missing, "rdc_" + field + "_required"
            )

    def test_marker_utf8_byte_boundaries(self):
        for field, limit in (
            ("device_id", 512), ("device_name", 512), ("recorded_at", 128),
        ):
            with self.subTest(field=field):
                value = marker_v1()
                value[field] = "x" * limit
                self.assertIs(validate_rdc_marker(value), value)
                value[field] = "x" * (limit + 1)
                self.assert_invalid(
                    validate_rdc_marker, value, "rdc_" + field + "_too_large"
                )
                value[field] = "é" * (limit // 2)
                self.assertIs(validate_rdc_marker(value), value)
                value[field] += "é"
                self.assert_invalid(
                    validate_rdc_marker, value, "rdc_" + field + "_too_large"
                )

    def test_marker_control_characters_rejected(self):
        for field in ("device_id", "device_name", "recorded_at"):
            for char in ("\x00", "\r", "\n"):
                with self.subTest(field=field, char=repr(char)):
                    value = marker_v1()
                    value[field] = "left" + char + "right"
                    self.assert_invalid(
                        validate_rdc_marker, value,
                        "rdc_" + field + "_control_character",
                    )

    def test_marker_rejects_invalid_fixed_source(self):
        for source in (None, "", "UNKNOWN", "rdc", "chatgpt_rdc_bootstrap "):
            value = marker_v1()
            value["source"] = source
            self.assert_invalid(
                validate_rdc_marker, value, "rdc_marker_invalid_source"
            )

    def test_bounded_string_helper_preserves_legacy_error_order(self):
        self.assertEqual(
            _bounded_marker_text("  Mac.test  ", "device_name", max_bytes=512),
            "Mac.test",
        )
        with self.assertRaisesRegex(ValueError, "^rdc_model_too_large$"):
            _bounded_marker_text("x" * 257 + "\n", "model", max_bytes=256)
        with self.assertRaisesRegex(ValueError, "^rdc_model_control_character$"):
            _bounded_marker_text("x\n", "model", max_bytes=256)

    def test_route_v1_unknown_telemetry_is_valid_but_not_upgraded(self):
        value = evidence_v1()
        self.assertIs(validate_route_evidence(value), value)
        self.assertEqual(value["transport"], "rdc")
        self.assertEqual(value["surface"], "ordinary_chat")
        self.assertEqual(value["model"], "UNKNOWN")
        self.assertEqual(value["reasoning"], "UNKNOWN")
        self.assertEqual(value["usage"], "UNKNOWN")
        self.assertEqual(
            {value[k] for k in (
                "work_used", "codex_execution_used",
                "model_api_used", "external_provider_used",
            )},
            {"unknown"},
        )
        self.assertEqual(ROUTE_OBSERVATION_VALUES, {"yes", "no", "unknown"})

    def test_route_v1_json_round_trip_is_unchanged(self):
        value = evidence_v1()
        serialized = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        recovered = json.loads(serialized)
        self.assertIs(validate_route_evidence(recovered), recovered)
        self.assertEqual(
            (
                json.dumps(recovered, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8"),
            serialized,
        )

    def test_route_schema_rejects_other_versions_and_bool(self):
        for version in (None, 0, 2, True, False, 1.0, "1", [], {}):
            with self.subTest(version=repr(version)):
                value = evidence_v1()
                value["schema_version"] = version
                self.assert_invalid(
                    validate_route_evidence, value,
                    "route_evidence_invalid_schema",
                )

    def test_route_rejects_unknown_json_keys_deterministically(self):
        value = evidence_v1()
        value["z_extension"] = 1
        value["a_extension"] = 1
        self.assert_invalid(
            validate_route_evidence, value,
            "route_evidence_unknown_field:a_extension",
        )

    def test_route_rejects_unsupported_surface_and_transport(self):
        for field, invalid, code in (
            ("surface", "work", "route_evidence_invalid_surface"),
            ("surface", "UNKNOWN", "route_evidence_invalid_surface"),
            ("surface", None, "route_evidence_invalid_surface"),
            ("transport", "bridge", "route_evidence_invalid_transport"),
            ("transport", "mcp", "route_evidence_invalid_transport"),
            ("transport", "UNKNOWN", "route_evidence_invalid_transport"),
            ("transport", None, "route_evidence_invalid_transport"),
        ):
            with self.subTest(field=field, invalid=invalid):
                value = evidence_v1()
                value[field] = invalid
                self.assert_invalid(validate_route_evidence, value, code)

    def test_route_identity_ascii_bounds_and_invalid_values(self):
        for field in ("run_id", "task_id"):
            with self.subTest(field=field, case="boundary"):
                value = evidence_v1()
                value[field] = "A" * 200
                self.assertIs(validate_route_evidence(value), value)
                value[field] = "A" * 201
                self.assert_invalid(
                    validate_route_evidence, value,
                    "route_evidence_invalid_" + field,
                )
            for invalid in (
                None, "", "a b", "a/b", "é", "RUN\nA", 0, True, [], {},
            ):
                with self.subTest(field=field, invalid=repr(invalid)):
                    value = evidence_v1()
                    value[field] = invalid
                    self.assert_invalid(
                        validate_route_evidence, value,
                        "route_evidence_invalid_" + field,
                    )
        for acceptable in (".", "_", "-", "RUN_01.part-two"):
            value = evidence_v1()
            value["run_id"] = acceptable
            self.assertIs(validate_route_evidence(value), value)

    def test_route_utf8_text_field_boundaries_and_error_codes(self):
        for field, limit in (
            ("device_id", 512), ("device_name", 512), ("model", 256),
            ("reasoning", 128), ("usage", 256),
            ("observed_at", 128), ("source", 128),
        ):
            with self.subTest(field=field):
                value = evidence_v1()
                value[field] = "é" * (limit // 2)
                self.assertIs(validate_route_evidence(value), value)
                value[field] += "é"
                self.assert_invalid(
                    validate_route_evidence, value,
                    "rdc_" + field + "_too_large",
                )
                value[field] = ""
                self.assert_invalid(
                    validate_route_evidence, value,
                    "rdc_" + field + "_required",
                )
                value[field] = "safe\x00unsafe"
                self.assert_invalid(
                    validate_route_evidence, value,
                    "rdc_" + field + "_control_character",
                )

    def test_route_observation_tri_states_are_exact(self):
        fields = (
            "work_used", "codex_execution_used",
            "model_api_used", "external_provider_used",
        )
        for field in fields:
            for valid in ("yes", "no", "unknown"):
                with self.subTest(field=field, valid=valid):
                    value = evidence_v1()
                    value[field] = valid
                    self.assertIs(validate_route_evidence(value), value)
            for invalid in (None, "", "UNKNOWN", "YES", "No", True, 0):
                with self.subTest(field=field, invalid=repr(invalid)):
                    value = evidence_v1()
                    value[field] = invalid
                    self.assert_invalid(
                        validate_route_evidence, value,
                        "route_evidence_invalid_observation:" + field,
                    )

    def test_route_validation_order_preserves_original_error(self):
        value = evidence_v1()
        value["schema_version"] = 2
        value["unknown_key"] = "x"
        self.assert_invalid(
            validate_route_evidence, value,
            "route_evidence_unknown_field:unknown_key",
        )
        del value["unknown_key"]
        value["schema_version"] = 1
        value["run_id"] = "invalid/run"
        value["transport"] = "bridge"
        self.assert_invalid(
            validate_route_evidence, value, "route_evidence_invalid_run_id"
        )


if __name__ == "__main__":
    unittest.main()
