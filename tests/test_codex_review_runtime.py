"""Bundled reviewer discovery, protocol and explicit-routing regressions."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch import codex_review as review
from orch.cli import build_parser


class RPCProcess:
    def __init__(self, messages):
        read_fd, write_fd = os.pipe()
        raw = b"".join(json.dumps(x).encode() + b"\n" for x in messages)
        os.write(write_fd, raw)
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb")
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.returncode = 0

    def close(self):
        self.stdout.close()
        self.stdin.close()
        self.stderr.close()


def responses(account_type="chatgpt", *, credits=False, usage_allowed=True):
    rates = {"rateLimitReachedType": None, "primary": {"usedPercent": 10},
             "credits": {"hasCredits": credits, "balance": "0"}}
    return [
        {"id": 0, "result": {}},
        {"id": 1, "result": {"account": {"type": account_type},
                              "requiresOpenaiAuth": True}},
        {"id": 2, "result": {"ordinaryUsageAllowed": usage_allowed,
                              "rateLimits": rates,
                              "rateLimitsByLimitId": {"codex": rates}}},
        {"id": 3, "result": {"data": [{"model": "gpt-5.6-sol",
                                         "supportedReasoningEfforts": [
                                             {"reasoningEffort": "high"}]}],
                              "nextCursor": None}},
    ]


class ReviewerRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="orch-review-runtime-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.legacy = self.root / "legacy"
        self.current = self.root / "current"
        self.current.write_bytes(b"fixture executable\n")
        self.current.chmod(0o700)

    def _preflight(self, messages=None, **route):
        process = RPCProcess(messages if messages is not None else responses())
        self.addCleanup(process.close)
        with mock.patch.object(review, "CODEX_BIN", self.legacy), \
                mock.patch.object(review, "CURRENT_CODEX_BIN", self.current, create=True), \
                mock.patch.object(review.subprocess, "Popen", return_value=process), \
                mock.patch.object(review.subprocess, "run", return_value=
                                  subprocess.CompletedProcess([], 0, "codex-cli fixture\n", "")), \
                mock.patch.dict(os.environ, {}, clear=True):
            result = review.subscription_preflight(self.root, **route)
        return result

    def test_current_bundle_runs_preflight_when_legacy_path_is_missing(self):
        result = self._preflight()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["binary"]["path"], str(self.current))

    def test_legacy_bundle_remains_supported(self):
        self.current.rename(self.legacy)
        result = self._preflight()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["binary"]["path"], str(self.legacy))

    def test_missing_nonexecutable_and_symlink_binaries_fail_closed(self):
        self.current.unlink()
        self.assertEqual(self._preflight()["status"], "BLOCKED")
        self.current.write_bytes(b"not executable")
        self.current.chmod(0o600)
        self.assertEqual(self._preflight()["status"], "BLOCKED")
        self.current.unlink()
        target = self.root / "target"
        target.write_bytes(b"fixture executable")
        target.chmod(0o700)
        self.current.symlink_to(target)
        self.assertEqual(self._preflight()["status"], "BLOCKED")

    def test_subscription_only_billing_gates_preserve_refusals(self):
        for messages in (responses("apiKey"), responses(credits=True),
                         responses(usage_allowed=False)):
            with self.subTest(messages=messages):
                self.assertEqual(self._preflight(messages)["status"], "BLOCKED")

    def test_unknown_credit_or_missing_protocol_evidence_never_passes(self):
        messages = responses()
        messages[2]["result"]["rateLimits"]["credits"] = {}
        self.assertEqual(self._preflight(messages)["status"], "BLOCKED")
        self.assertEqual(self._preflight([])["status"], "BLOCKED")

    def test_explicit_route_is_checked_against_available_model_and_effort(self):
        result = self._preflight(model="gpt-5.6-sol", reasoning_effort="high")
        self.assertEqual(result["status"], "PASS", result)
        for route in (dict(model="other", reasoning_effort="high"),
                      dict(model="gpt-5.6-sol", reasoning_effort="low")):
            self.assertEqual(self._preflight(**route)["status"], "BLOCKED")

    def test_preflight_does_not_return_private_usage_or_credit_balances(self):
        result = self._preflight()
        self.assertEqual(result["status"], "PASS", result)
        self.assertNotIn("used_percent", result)
        self.assertNotIn("credit_balance", result)

    def test_cli_accepts_explicit_review_and_preflight_routing(self):
        parser = build_parser()
        args = parser.parse_args(["codex-review", "--run-id", "fixture",
                                  "--model", "gpt-5.6-sol", "--reasoning-effort", "high"])
        self.assertEqual((args.model, args.reasoning_effort), ("gpt-5.6-sol", "high"))
        args = parser.parse_args(["codex-preflight", "--model", "gpt-5.6-sol",
                                  "--reasoning-effort", "high"])
        self.assertEqual(args.model, "gpt-5.6-sol")

    def test_malformed_duplicate_and_oversized_rpc_outputs_are_blocked(self):
        duplicate = responses()
        duplicate.insert(1, duplicate[0])
        self.assertEqual(self._preflight(duplicate)["status"], "BLOCKED")
        malformed = responses()
        malformed[1] = {"id": 1, "result": "not-an-account"}
        self.assertEqual(self._preflight(malformed)["status"], "BLOCKED")
        with mock.patch.object(review, "PREFLIGHT_MAX_BYTES", 12):
            self.assertEqual(self._preflight()["status"], "BLOCKED")

    def test_missing_routing_cannot_prepare_or_start_review(self):
        with mock.patch.object(review, "prepare_review", side_effect=AssertionError("unexpected export")):
            result = review.run_review(object(), "fixture", execute=True)
        self.assertEqual((result['status'], result['reason']),
                         ('BLOCKED', 'review_routing_required'))

    def test_invalid_route_is_rejected_before_launch(self):
        for model, effort in (("a;command", "high"), ("gpt-5.6-sol", "invalid"),
                              (None, "high"), ("gpt-5.6-sol", None)):
            with self.subTest(model=model, effort=effort):
                self.assertEqual(self._preflight(model=model, reasoning_effort=effort)['status'],
                                 'BLOCKED')

    def test_account_error_is_not_disclosed_or_accepted(self):
        messages = responses()
        messages[1] = {'id': 1, 'error': {'message': 'private account information'}}
        result = self._preflight(messages)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertNotIn('private', json.dumps(result))


if __name__ == "__main__":
    unittest.main()
