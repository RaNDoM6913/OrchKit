"""Disposable local-only P1-C1 run/session authorization acceptance tests."""

import ast
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch.bridge_authorization import LocalRunSessionAuthority
from orch.config import atomic_write_json
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry
from orch.worker_operations import WorkerRunBinding


class LocalRunSessionAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-bridge-auth-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.orch = Orchestrator(self.home)
        self.registry = ProjectRegistry(self.home)
        self.projects = {}
        self.claims = {}
        self.bindings = {}
        for label in ("a", "b"):
            repo = self.base / ("project-" + label)
            self._git("init", "-q", "-b", "main", str(repo))
            self._git("-C", str(repo), "config", "user.email", "fixture@example.invalid")
            self._git("-C", str(repo), "config", "user.name", "Fixture")
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            self._git("-C", str(repo), "add", "README.md")
            self._git("-C", str(repo), "commit", "-qm", "root")
            project = self.registry.add(
                repo, name="Disposable " + label, profile="safe",
                review_mode="off", allow_commit=True, allow_push=False,
            )["project"]
            self.projects[label] = project
            plan = build_single_task_plan(
                project, task_id="BRIDGE-TEST-" + label.upper(),
                goal="Test local session authority", allowed_paths=["result.txt"],
                plan_revision="bridge-test-" + label,
            )
            path = self.base / (label + "-plan.json")
            self.assertEqual(write_plan(path, plan)["status"], "CREATED")
            self.assertEqual(self.orch.load_plan(path)["queued_count"], 1)
            claim = self.orch.claim(
                "disposable-" + label, project_id=project["project_id"]
            )
            self.assertEqual(claim["status"], "CLAIMED", claim)
            self.claims[label] = claim
            self.bindings[label] = WorkerRunBinding(
                project_id=project["project_id"],
                writer_key=project["writer_key"],
                task_id=claim["task_id"],
                run_id=claim["run_id"],
                attempt=claim["attempt"],
            )
        self.auth = LocalRunSessionAuthority(self.orch)

    def _git(self, *args):
        env = dict(os.environ)
        env.update({
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True, text=True, env=env, check=True, timeout=12,
        )

    def _cap(self, label):
        return Path(self.claims[label]["capability_file"])

    def _open(self, label="a"):
        return self.auth.open_session(self.bindings[label], self._cap(label))

    def _require(self, session_id, label="a", cap_label=None):
        self.auth.require_session(
            session_id, self.bindings[label],
            self._cap(cap_label or label),
        )

    def test_two_distinct_project_sessions_and_repeat_readback(self):
        a, b = self._open("a"), self._open("b")
        self.assertNotEqual(a, b)
        self.assertIsNone(self._require(a, "a"))
        self.assertIsNone(self._require(b, "b"))
        self.assertEqual(len(a) >= 24, True)

    def test_cannot_bind_same_run_twice_in_one_authority(self):
        sid = self._open()
        with self.assertRaisesRegex(ValueError, "bridge_run_already_bound_locally"):
            self._open()
        self._require(sid)

    def test_session_id_is_not_a_bearer_capability(self):
        sid = self._open()
        with self.assertRaisesRegex(ValueError, "bridge_session_mismatch"):
            self._require(sid, "b")
        with self.assertRaisesRegex(ValueError, "bridge_session_mismatch"):
            self._require("forged-session", "a")
        with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
            self._require(sid, "a", cap_label="b")
        self._require(sid)

    def test_caller_identity_fields_do_not_override_ledger(self):
        for field, wrong in (
            ("project_id", self.bindings["b"].project_id),
            ("writer_key", self.bindings["b"].writer_key),
            ("task_id", self.bindings["b"].task_id),
            ("run_id", self.bindings["b"].run_id),
            ("attempt", 2),
        ):
            with self.subTest(field=field):
                forged = dataclasses.replace(self.bindings["a"], **{field: wrong})
                with self.assertRaisesRegex(ValueError, "bridge_"):
                    self.auth.open_session(forged, self._cap("a"))
        self._open("a")

    def test_incorrect_missing_or_foreign_capability_path_is_denied(self):
        for path in (self._cap("b"), self.base / "missing.json"):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
                    self.auth.open_session(self.bindings["a"], path)

    def test_corrupted_capability_does_not_authorize(self):
        cap = self._cap("a")
        cap.write_text('{"run_id":"incorrect","lease_token":"fake"}')
        with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
            self._open()

    def test_unsafe_capability_mode_is_refused_without_repair(self):
        cap = self._cap("a")
        cap.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
            self._open()
        self.assertEqual(cap.stat().st_mode & 0o777, 0o644)

    def test_capability_symlink_is_refused(self):
        cap = self._cap("a")
        other = self.base / "cap-original.json"
        cap.rename(other)
        cap.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
            self._open()

    def test_revoke_after_quiesce_invalidates_existing_session(self):
        sid = self._open()
        claim = self.claims["a"]
        receipt = Path(claim["receipt_file"])
        atomic_write_json(receipt, {
            "run_id": claim["run_id"], "task_id": claim["task_id"],
            "changed_paths": ["result.txt"], "summary": "fixture",
        }, mode=0o600)
        lease = self.orch.lease_from_capability(claim["run_id"], self._cap("a"))
        self.assertEqual(
            self.orch.submit(claim["run_id"], lease, receipt)["status"],
            "RESULT_SUBMITTED",
        )
        with self.assertRaisesRegex(ValueError, "bridge_run_not_active"):
            self._require(sid)
        self.assertEqual(
            self.orch.quiesce(claim["run_id"], lease)["status"], "VERIFYING"
        )
        with self.assertRaisesRegex(ValueError, "bridge_capability_denied"):
            self._require(sid)

    def test_branch_switch_blocks_already_bound_session(self):
        sid = self._open()
        self._git("-C", self.projects["a"]["root"], "switch", "-q", "-c", "other")
        with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
            self._require(sid)

    def test_head_drift_blocks_already_bound_session(self):
        sid = self._open()
        repo = Path(self.projects["a"]["root"])
        (repo / "another.txt").write_text("revision\n", encoding="utf-8")
        self._git("-C", str(repo), "add", "another.txt")
        self._git("-C", str(repo), "commit", "-qm", "drift")
        with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
            self._require(sid)

    def test_writer_drift_blocks_session(self):
        sid = self._open()
        from orch import bridge_authorization as module
        original = module.inspect_project
        def wrong_writer(root):
            current = original(root)
            current["writer_key"] = self.projects["b"]["writer_key"]
            return current
        with mock.patch.object(module, "inspect_project", side_effect=wrong_writer):
            with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
                self._require(sid)

    def test_project_registration_drift_blocks_session(self):
        sid = self._open()
        from orch import bridge_authorization as module
        with mock.patch.object(
            module, "read_bounded_json_object",
            return_value=(self.projects["b"], {"mode": 0o600}),
        ):
            with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
                self._require(sid)

    def test_registry_unsafe_mode_is_denied_without_chmod(self):
        sid = self._open()
        path = self.home / "projects" / (self.bindings["a"].project_id + ".json")
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "bridge_project_binding_denied"):
            self._require(sid)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_active_claim_survives_project_pause(self):
        sid = self._open()
        self.orch.pause_project(self.projects["a"]["project_id"], "fixture pause")
        self._require(sid)

    def test_close_and_restart_do_not_reuse_session_id(self):
        sid = self._open()
        restarted = LocalRunSessionAuthority(Orchestrator(self.home))
        with self.assertRaisesRegex(ValueError, "bridge_session_mismatch"):
            restarted.require_session(sid, self.bindings["a"], self._cap("a"))
        self._require(sid)
        self.auth.close_session(sid)
        with self.assertRaisesRegex(ValueError, "bridge_session_mismatch"):
            self._require(sid)
        with self.assertRaisesRegex(ValueError, "bridge_session_unknown"):
            self.auth.close_session(sid)
        sid2 = self._open()
        self.assertNotEqual(sid, sid2)

    def test_no_registered_check_or_git_publisher_or_worker_io(self):
        source = (Path(__file__).parents[1] / "orch" / "bridge_authorization.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.Import) for alias in node.names
        }
        imported.update({
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        })
        self.assertFalse({"subprocess", "git_transport", "dispatcher"} & imported)
        self.assertFalse(hasattr(self.auth, "execute"))
        self.assertFalse(hasattr(self.auth, "read_file"))
        self.assertFalse(hasattr(self.auth, "start_process"))


if __name__ == "__main__":
    unittest.main()
