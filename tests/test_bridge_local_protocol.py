"""Strict framing and complete-response bounds, using real socket pairs."""

import base64
import dataclasses
import json
import socket
import struct
import threading
import time
import unittest

from orch.bridge_local_protocol import (
    LocalServiceLimits, encode_payload, native_payload_budget, receive_frame,
    send_frame, validate_hello, validate_request,
)


class LocalProtocolTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)
        self.limits = LocalServiceLimits(io_timeout_sec=0.1)

    def _raw(self, payload):
        self.left.sendall(struct.pack("!I", len(payload)) + payload)

    def test_length_prefix_and_binary_safe_round_trip(self):
        value = {"name": "данные", "bytes": base64.b64encode(b"\x00\xff").decode("ascii")}
        send_frame(self.left, value, self.limits)
        self.assertEqual(receive_frame(self.right, self.limits), value)

    def test_fragmented_header_and_body_are_read_exactly(self):
        payload = b'{"value":7}'
        ready = threading.Event()

        def writer():
            ready.wait(1)
            for piece in (b"\x00", b"\x00\x00", bytes([len(payload)]), payload[:2], payload[2:]):
                self.left.sendall(piece)

        thread = threading.Thread(target=writer)
        thread.start()
        ready.set()
        self.assertEqual(receive_frame(self.right, self.limits), {"value": 7})
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_oversize_length_is_refused_without_waiting_for_body(self):
        self.left.sendall(struct.pack("!I", 65537))
        with self.assertRaisesRegex(ValueError, "local_bridge_invalid_frame"):
            receive_frame(self.right, self.limits)

    def test_duplicate_invalid_json_utf8_and_nonobjects_are_refused(self):
        for payload in (b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff", b"[]", b"null", b"{"):
            with self.subTest(payload=payload):
                self._raw(payload)
                with self.assertRaisesRegex(ValueError, "local_bridge_invalid_frame"):
                    receive_frame(self.right, self.limits)

    def test_truncated_frame_is_distinct_from_clean_eof(self):
        self.left.sendall(b"\x00\x00")
        self.left.shutdown(socket.SHUT_WR)
        with self.assertRaisesRegex(ValueError, "local_bridge_invalid_frame"):
            receive_frame(self.right, self.limits)

    def test_clean_eof_and_stalled_peer_have_bounded_exit(self):
        with self.assertRaises(TimeoutError):
            receive_frame(self.right, self.limits)
        self.left.shutdown(socket.SHUT_WR)
        with self.assertRaises(EOFError):
            receive_frame(self.right, self.limits)

    def test_dripped_frame_does_not_refresh_deadline_per_received_byte(self):
        ready = threading.Event()
        finished = threading.Event()

        def writer():
            ready.wait(1)
            for byte in struct.pack("!I", 2) + b"{}":
                self.left.sendall(bytes([byte]))
                if finished.wait(0.04):
                    break

        thread = threading.Thread(target=writer)
        thread.start()
        started = time.monotonic()
        ready.set()
        try:
            with self.assertRaises(TimeoutError):
                receive_frame(self.right, self.limits)
            self.assertLess(time.monotonic() - started, 0.18)
        finally:
            finished.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_hello_rejects_unknown_fields_and_noninteger_version(self):
        hello = {"schema_version": 1, "request_id": "hello-1", "type": "hello", "transport_token": "x" * 32}
        self.assertEqual(validate_hello(hello), hello)
        for update in ({"root": "/tmp"}, {"schema_version": True}, {"type": "call"}, {"transport_token": ""}):
            with self.subTest(update=update):
                with self.assertRaisesRegex(ValueError, "local_bridge_invalid_request"):
                    validate_hello(dict(hello, **update))

    def test_call_rejects_mutations_binding_overrides_and_invalid_budgets(self):
        call = {"schema_version": 1, "request_id": "call-1", "session_id": "s" * 32,
                "operation": "file.read", "target": "docs/a.bin", "max_response_bytes": 4096}
        self.assertEqual(validate_request(call, self.limits), call)
        for update in ({"operation": "file.patch"}, {"binding": {}}, {"target": "../private"},
                       {"max_response_bytes": 4095}, {"max_response_bytes": True},
                       {"max_response_bytes": 65537}, {"operation": "session.status"}):
            with self.subTest(update=update):
                with self.assertRaisesRegex(ValueError, "local_bridge_invalid_request"):
                    validate_request(dict(call, **update), self.limits)
        self.assertEqual(validate_request(dict(call, operation="session.status", target=None), self.limits)["target"], None)

    def test_payload_budget_counts_entire_ascii_envelope_and_base64_padding(self):
        envelope = {"schema_version": 1, "request_id": "request", "session_id": "s" * 32,
                    "service_incarnation": "i" * 32, "operation": "file.read", "state": "SUCCEEDED",
                    "output_base64": "", "error_code": None}
        overhead = len(json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii"))
        budget = native_payload_budget(envelope, overhead + 8)
        self.assertEqual(budget, 6)
        envelope["output_base64"] = base64.b64encode(b"a" * budget).decode("ascii")
        self.assertLessEqual(len(encode_payload(envelope)), overhead + 8)
        envelope["output_base64"] = base64.b64encode(b"a" * (budget + 1)).decode("ascii")
        self.assertGreater(len(encode_payload(envelope)), overhead + 8)

    def test_trusted_limits_are_frozen_and_cannot_exceed_protocol_bounds(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.limits.io_timeout_sec = 1
        for kwargs in ({"max_frame_bytes": 65537}, {"max_response_bytes": 65537},
                       {"io_timeout_sec": 0}, {"io_timeout_sec": float("nan")},
                       {"min_response_bytes": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    LocalServiceLimits(**kwargs)


if __name__ == "__main__":
    unittest.main()
