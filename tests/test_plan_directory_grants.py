"""Plan-builder directory intent and unchanged permission-model regressions."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orch.core import PLAN_MAX_ALLOWED_PATHS, path_allowed
from orch.plan import build_batch_plan, build_single_task_plan


class PlanDirectoryGrantsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="orch-plan-grants-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = {
            "project_id": "fixture-project-12345678",
            "root": str(self.root), "writer_key": None, "git": {},
            "review": {"mode": "off", "reviewer": "none",
                       "placement": "pre_publish"},
            "protected_paths": {}, "checks": [],
        }
        # Only pure generation uses a policy stub; live adapter fixtures use
        # actual Git registration, plan admission, claims and capabilities.
        policy = mock.patch(
            "orch.plan.evaluate_project_git_policy",
            return_value={"status": "READY", "can_push": False,
                          "can_commit": False, "safety_blockers": []},
        )
        policy.start()
        self.addCleanup(policy.stop)

    def _plan(self, paths):
        return build_single_task_plan(
            self.project, task_id="DIRECTORY-GRANTS", goal="Test path intent",
            allowed_paths=paths, plan_revision="directory-grants-fixture",
        )

    def _allowed(self, paths):
        return self._plan(paths)["tasks"][0]["allowed_paths"]

    def test_normalization_preserves_only_explicit_directory_markers(self):
        paths = ["docs/", "./src//pkg/./", "README.md", "./notes//one.txt"]
        original = list(paths)
        self.assertEqual(
            self._allowed(paths),
            ["docs/", "src/pkg/", "README.md", "notes/one.txt"],
        )
        self.assertEqual(paths, original)

    def test_existing_directory_without_marker_is_not_a_subtree_grant(self):
        (self.root / "docs").mkdir()
        allowed = self._allowed(["docs", "notes/one.txt"])
        self.assertEqual(allowed, ["docs", "notes/one.txt"])
        self.assertTrue(path_allowed("notes/one.txt", allowed))
        for target in ("docs/child.txt", "notes/two.txt", "notes/one.txt/child"):
            with self.subTest(target=target):
                self.assertFalse(path_allowed(target, allowed))

    def test_explicit_directory_retains_existing_subtree_semantics(self):
        allowed = self._allowed(["docs/"])
        for target in ("docs", "docs/one.txt", "docs/deep/two.txt"):
            with self.subTest(target=target):
                self.assertTrue(path_allowed(target, allowed))
        for target in ("docs-extra/one.txt", "secret.txt", "../docs/one.txt"):
            with self.subTest(target=target):
                self.assertFalse(path_allowed(target, allowed))
        self.assertFalse((self.root / "docs").exists())

    def test_normalized_duplicates_and_file_directory_conflicts_rejected(self):
        for paths in (
            ["docs/", "docs/"], ["docs/", "./docs//"],
            ["docs", "docs/"], ["docs/", "docs"],
            ["docs/./", "./docs"], ["README.md", "./README.md"],
        ):
            with self.subTest(paths=paths), self.assertRaisesRegex(
                ValueError, "duplicate_allowed_path"
            ):
                self._plan(paths)

    def test_parent_child_and_file_grants_remain_distinct(self):
        paths = ["docs/", "docs/deep/", "docs/one.txt"]
        self.assertEqual(self._allowed(paths), paths)
        # A parent grant does not manufacture an explicit child-list grant.
        self.assertNotIn("docs/deep/", self._allowed(["docs/"]))
        self.assertNotIn("docs/", self._allowed(["docs/one.txt"]))

    def test_traversal_absolute_and_control_values_rejected(self):
        for value in (
            "/", "/etc/", "//etc/", "../docs/", "docs/../private/",
            "./../docs/", "./", "././", ".//etc/", "docs/..",
            "docs/\x00/", "docs/\n/", "docs/\r/",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self._plan([value])

    def test_invalid_elements_do_not_become_grants(self):
        for value in (None, True, 7, [], {}, b"docs/", Path("docs")):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "invalid_allowed_path"
            ):
                self._plan([value])

    def test_single_string_is_not_an_iterable_of_file_grants(self):
        for value in ("docs", b"docs"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self._plan(value)

    def test_empty_values_and_supported_iterables_keep_compatibility(self):
        for paths in ([], ["", "   ", "\t"]):
            with self.subTest(paths=paths), self.assertRaisesRegex(
                ValueError, "allowed_paths_required"
            ):
                self._plan(paths)
        self.assertEqual(self._allowed(["", "docs/", " "]), ["docs/"])
        self.assertEqual(self._allowed(("docs/", "README.md")),
                         ["docs/", "README.md"])
        self.assertEqual(self._allowed(iter(["docs/"])), ["docs/"])

    def test_directory_marker_counts_toward_existing_utf8_byte_limit(self):
        for value in ("a" * 1023 + "/", "é" * 511 + "/"):
            with self.subTest(length=len(value.encode("utf-8"))):
                self.assertEqual(self._allowed([value]), [value])
        for value in ("a" * 1024 + "/", "é" * 512 + "/"):
            with self.subTest(length=len(value.encode("utf-8"))), self.assertRaisesRegex(
                ValueError, "invalid_allowed_path_too_large"
            ):
                self._plan([value])

    def test_existing_path_count_limit_is_not_weakened(self):
        paths = ["dir-" + str(i) + "/" for i in range(PLAN_MAX_ALLOWED_PATHS)]
        self.assertEqual(self._allowed(paths), paths)
        with self.assertRaisesRegex(ValueError, "allowed_paths_required"):
            self._plan(paths + ["extra/"])

    def test_batch_builder_preserves_grants_without_mutating_manifest(self):
        manifest = {"schema_version": 1, "plan_revision": "batch-grants",
                    "tasks": [{"id": "BATCH-GRANTS", "goal": "Test batch",
                               "allowed_paths": ["./docs//", "README.md"]}]}
        original = copy.deepcopy(manifest)
        plan = build_batch_plan(self.project, manifest, source_digest="0" * 64)
        self.assertEqual(plan["tasks"][0]["allowed_paths"], ["docs/", "README.md"])
        self.assertEqual(manifest, original)
        manifest["tasks"][0]["allowed_paths"] = ["docs/", "docs"]
        with self.assertRaisesRegex(ValueError, "duplicate_allowed_path"):
            build_batch_plan(self.project, manifest, source_digest="0" * 64)


if __name__ == "__main__":
    unittest.main()
