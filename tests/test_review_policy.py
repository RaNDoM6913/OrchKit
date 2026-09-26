import unittest

from orch.review_policy import decide_review


class ReviewPolicyTests(unittest.TestCase):
    def test_auth_and_security_named_files_require_risk_based_review(self):
        payload = {
            "review": {"mode": "risk_based", "reviewer": "codex"}
        }
        for path in ("auth.py", "src/security.ts"):
            with self.subTest(path=path):
                decision = decide_review(
                    payload, {"files": {path: {"bytes": 1}}}
                )
                self.assertTrue(decision["required"])
                self.assertIn(
                    "sensitive_path:" + path, decision["signals"]
                )


if __name__ == "__main__":
    unittest.main()
