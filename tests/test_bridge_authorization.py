"""Disposable local-only P1-C1 run/session authorization acceptance tests."""

import ast
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
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
            self._add_project(label)
        self.auth = LocalRunSessionAuthority(self.orch)

    def _add_project(self, label, *, repo=None, claim=True):
        if repo is None:
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
        if not claim:
            return
        admitted = self.orch.claim(
            "disposable-" + label, project_id=project["project_id"]
        )
        self.assertEqual(admitted["status"], "CLAIMED", admitted)
        self.claims[label] = admitted
        self.bindings[label] = WorkerRunBinding(
            project_id=project["project_id"],
            writer_key=project["writer_key"],
            task_id=admitted["task_id"],
            run_id=admitted["run_id"],
            attempt=admitted["attempt"],
        )

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

    def _native_authority_snapshot(self):
        # Compare all logical ledger state without exposing capability contents.
        with self.orch.connect() as conn:
            ledger_digest = hashlib.sha256(
                "\n".join(conn.iterdump()).encode("utf-8")
            ).hexdigest()
        capabilities = {}
        for label in self.claims:
            cap = self._cap(label)
            info = cap.stat()
            capabilities[label] = (
                str(cap), info.st_dev, info.st_ino, info.st_mode,
                info.st_size, info.st_mtime_ns,
                hashlib.sha256(cap.read_bytes()).hexdigest(),
            )
        return ledger_digest, capabilities

    def _race_open(self, labels):
        # Force unlocked contenders past duplicate/capacity checks together.
        # A serialized generator cannot meet its peer: its bounded barrier
        # expires, then the waiting admission must observe the first insertion.
        from orch import bridge_authorization as module
        start = threading.Barrier(len(labels) + 1)
        generation = threading.Barrier(len(labels))
        original_token = module.secrets.token_urlsafe

        def synchronized_token(size):
            try:
                generation.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass
            return original_token(size)

        def open_one(label):
            start.wait(timeout=10)
            try:
                return "admitted", self._open(label)
            except ValueError as exc:
                return "refused", str(exc)

        with mock.patch.object(
            module.secrets, "token_urlsafe", side_effect=synchronized_token,
        ):
            with ThreadPoolExecutor(max_workers=len(labels)) as pool:
                futures = [pool.submit(open_one, label) for label in labels]
                start.wait(timeout=10)
                return [future.result(timeout=15) for future in futures]

    def test_p1d1_default_limit_close_isolation_and_native_identity(self):
        self._add_project("c")
        self._add_project("d")
        before = self._native_authority_snapshot()
        sessions = {label: self._open(label) for label in ("a", "b", "c")}
        self.assertEqual(len(set(sessions.values())), 3)
        with self.assertRaisesRegex(ValueError, "^bridge_session_capacity_exhausted$"):
            self._open("d")
        self.assertEqual(self._native_authority_snapshot(), before)
        for label, sid in sessions.items():
            self._require(sid, label)

        self.auth.close_session(sessions["b"])
        with self.assertRaisesRegex(ValueError, "^bridge_session_mismatch$"):
            self._require(sessions["b"], "b")
        with self.assertRaisesRegex(ValueError, "^bridge_session_unknown$"):
            self.auth.close_session(sessions["b"])
        for label in ("a", "c"):
            self._require(sessions[label], label)
        replacement = self._open("d")
        self.assertNotIn(replacement, sessions.values())
        self._require(replacement, "d")
        with self.assertRaisesRegex(ValueError, "^bridge_session_capacity_exhausted$"):
            self._open("b")
        self.assertEqual(self._native_authority_snapshot(), before)

    def test_p1d1_configurable_limit_and_closed_slot_reuse(self):
        self.auth = LocalRunSessionAuthority(self.orch, max_worker_sessions=1)
        before = self._native_authority_snapshot()
        first = self._open("a")
        with self.assertRaisesRegex(ValueError, "^bridge_session_capacity_exhausted$"):
            self._open("b")
        self.auth.close_session(first)
        second = self._open("b")
        self.assertNotEqual(first, second)
        self._require(second, "b")
        with self.assertRaisesRegex(ValueError, "^bridge_session_mismatch$"):
            self._require(first, "a")
        self.assertEqual(self._native_authority_snapshot(), before)

        self._add_project("c")
        self._add_project("d")
        larger = LocalRunSessionAuthority(self.orch, max_worker_sessions=4)
        sessions = {
            label: larger.open_session(self.bindings[label], self._cap(label))
            for label in ("a", "b", "c", "d")
        }
        self.assertEqual(len(set(sessions.values())), 4)
        for label, sid in sessions.items():
            larger.require_session(sid, self.bindings[label], self._cap(label))

    def test_p1d1_invalid_worker_session_limits(self):
        before = self._native_authority_snapshot()
        for invalid in (None, True, False, 0, -1, 1.0, "3", [], {}):
            with self.subTest(limit=invalid):
                with self.assertRaisesRegex(
                    ValueError, "^bridge_invalid_worker_session_limit$",
                ):
                    LocalRunSessionAuthority(self.orch, max_worker_sessions=invalid)
        self.assertEqual(self._native_authority_snapshot(), before)

    def test_p1d1_concurrent_final_slot_admission_is_bounded(self):
        self._add_project("c")
        self._add_project("d")
        sessions = {label: self._open(label) for label in ("a", "b")}
        before = self._native_authority_snapshot()
        results = self._race_open(("c", "d"))
        admitted = [i for i, result in enumerate(results) if result[0] == "admitted"]
        refused = [result[1] for result in results if result[0] == "refused"]
        self.assertEqual(len(admitted), 1, results)
        self.assertEqual(refused, ["bridge_session_capacity_exhausted"])
        winner = ("c", "d")[admitted[0]]
        loser = "d" if winner == "c" else "c"
        sid = results[admitted[0]][1]
        self._require(sid, winner)
        for label, existing in sessions.items():
            self._require(existing, label)
        with self.assertRaisesRegex(ValueError, "^bridge_session_capacity_exhausted$"):
            self._open(loser)
        self.auth.close_session(sid)
        replacement = self._open(loser)
        self.assertNotEqual(replacement, sid)
        self._require(replacement, loser)
        with self.assertRaisesRegex(ValueError, "^bridge_session_mismatch$"):
            self._require(sid, winner)
        self.assertEqual(self._native_authority_snapshot(), before)

    def test_p1d1_concurrent_duplicate_admission_has_one_winner(self):
        existing = self._open("a")
        before = self._native_authority_snapshot()
        results = self._race_open(("b", "b"))
        admitted = [result[1] for result in results if result[0] == "admitted"]
        refused = [result[1] for result in results if result[0] == "refused"]
        self.assertEqual(len(admitted), 1, results)
        self.assertEqual(refused, ["bridge_run_already_bound_locally"])
        self._require(admitted[0], "b")
        self._require(existing, "a")
        self.auth.close_session(admitted[0])
        replacement = self._open("b")
        self.assertNotEqual(replacement, admitted[0])
        self._require(replacement, "b")
        with self.assertRaisesRegex(ValueError, "^bridge_session_mismatch$"):
            self._require(admitted[0], "b")
        self.assertEqual(self._native_authority_snapshot(), before)

    def test_p1d1_rejected_admissions_do_not_consume_capacity(self):
        self._add_project("c")
        self._add_project("d")
        before = self._native_authority_snapshot()
        with self.assertRaisesRegex(ValueError, "^bridge_invalid_run_binding$"):
            self.auth.open_session(None, self._cap("a"))
        forged = dataclasses.replace(
            self.bindings["a"], project_id=self.bindings["b"].project_id,
        )
        with self.assertRaisesRegex(ValueError, "^bridge_run_scope_mismatch$"):
            self.auth.open_session(forged, self._cap("a"))
        with self.assertRaisesRegex(ValueError, "^bridge_capability_denied$"):
            self.auth.open_session(self.bindings["a"], self._cap("b"))
        first = self._open("a")
        with self.assertRaisesRegex(ValueError, "^bridge_session_mismatch$"):
            self._require("forged-session", "a")
        with self.assertRaisesRegex(ValueError, "^bridge_session_mismatch$"):
            self._require(first, "b")
        with self.assertRaisesRegex(ValueError, "^bridge_capability_denied$"):
            self._require(first, "a", cap_label="b")
        with self.assertRaisesRegex(ValueError, "^bridge_run_already_bound_locally$"):
            self._open("a")
        with mock.patch("orch.bridge_authorization.secrets.token_urlsafe", return_value=first):
            with self.assertRaisesRegex(ValueError, "^bridge_session_id_collision$"):
                self._open("b")
        second, third = self._open("b"), self._open("c")
        for label, sid in (("a", first), ("b", second), ("c", third)):
            self._require(sid, label)
        with self.assertRaisesRegex(ValueError, "^bridge_run_already_bound_locally$"):
            self._open("a")
        with self.assertRaisesRegex(ValueError, "^bridge_capability_denied$"):
            self.auth.open_session(self.bindings["d"], self._cap("a"))
        with self.assertRaisesRegex(ValueError, "^bridge_session_capacity_exhausted$"):
            self._open("d")
        self.assertEqual(self._native_authority_snapshot(), before)

    def test_p1d1_revoked_and_drifted_admissions_do_not_take_slots(self):
        self._add_project("c")
        self._add_project("d")
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
        with self.assertRaisesRegex(ValueError, "^bridge_run_not_active$"):
            self._open("a")
        self.assertEqual(self.orch.quiesce(claim["run_id"], lease)["status"], "VERIFYING")
        with self.assertRaisesRegex(ValueError, "^bridge_capability_denied$"):
            self._open("a")
        repo = self.projects["b"]["root"]
        self._git("-C", repo, "switch", "-q", "-c", "drifted")
        with self.assertRaisesRegex(ValueError, "^bridge_project_binding_denied$"):
            self._open("b")
        self._git("-C", repo, "switch", "-q", "main")
        sessions = {label: self._open(label) for label in ("b", "c", "d")}
        self.assertEqual(len(set(sessions.values())), 3)
        for label, sid in sessions.items():
            self._require(sid, label)

    def test_p1d1_fresh_owner_rejects_every_old_session_id(self):
        self._add_project("c")
        sessions = {label: self._open(label) for label in ("a", "b", "c")}
        before = self._native_authority_snapshot()
        fresh = LocalRunSessionAuthority(Orchestrator(self.home))
        fresh_ids = []
        for label, sid in sessions.items():
            with self.assertRaisesRegex(ValueError, "^bridge_session_mismatch$"):
                fresh.require_session(sid, self.bindings[label], self._cap(label))
            replacement = fresh.open_session(self.bindings[label], self._cap(label))
            self.assertNotIn(replacement, sessions.values())
            fresh_ids.append(replacement)
            fresh.require_session(replacement, self.bindings[label], self._cap(label))
            self._require(sid, label)
        self.assertEqual(len(set(fresh_ids)), 3)
        self.assertEqual(self._native_authority_snapshot(), before)

    def test_p1d1_paused_project_retains_active_isolated_authority(self):
        self._add_project("c")
        sessions = {label: self._open(label) for label in ("a", "b", "c")}
        project_b = self.projects["b"]["project_id"]
        self.assertEqual(self.orch.pause_project(project_b, "fixture hold")["status"],
                         "PROJECT_PAUSED")
        self.assertEqual(self.orch.claim("blocked-b", project_id=project_b)["status"],
                         "PROJECT_PAUSED")
        before = self._native_authority_snapshot()
        for label, sid in sessions.items():
            self._require(sid, label)
        self.auth.close_session(sessions["b"])
        replacement = self._open("b")
        self.assertNotEqual(replacement, sessions["b"])
        self._require(replacement, "b")
        for label in ("a", "c"):
            self._require(sessions[label], label)
        self.assertEqual(self._native_authority_snapshot(), before)

    def test_p1d1_free_session_slot_does_not_release_shared_git_writer(self):
        sid = self._open("a")
        linked = self.base / "linked-a"
        self._git("-C", self.projects["a"]["root"], "worktree", "add",
                  "-q", "-b", "fixture-linked", str(linked))
        self._add_project("linked", repo=linked, claim=False)
        self.assertEqual(self.projects["linked"]["writer_key"], self.bindings["a"].writer_key)
        before = self._native_authority_snapshot()
        self.auth.close_session(sid)
        blocked = self.orch.claim("linked-worker", project_id=self.projects["linked"]["project_id"])
        self.assertEqual(blocked["status"], "BUSY")
        self.assertEqual(blocked["active"]["run_id"], self.bindings["a"].run_id)
        forged = dataclasses.replace(
            self.bindings["a"], project_id=self.projects["linked"]["project_id"],
        )
        with self.assertRaisesRegex(ValueError, "^bridge_run_scope_mismatch$"):
            self.auth.open_session(forged, self._cap("a"))
        reopened = self._open("a")
        self.assertNotEqual(reopened, sid)
        self._require(reopened, "a")
        self.assertEqual(self._native_authority_snapshot(), before)

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
