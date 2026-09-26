import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from orch.core import Orchestrator, canonical_json
from orch.codex_review import prepare_review, run_review


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'orch'; self.root.mkdir()
        self.ws=Path(self.tmp.name)/'ws'; self.ws.mkdir()
        self.orch=Orchestrator(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self,tasks,revision='p1'):
        path=self.root/'plan.json'
        path.write_text(json.dumps({'schema_version':1,'plan_revision':revision,'tasks':tasks}),encoding='utf-8')
        return self.orch.load_plan(path)

    def task(self,tid='T1',deps=None,checks=None,review=False,protected=None,max_attempts=2):
        return {'id':tid,'goal':'test','workspace':str(self.ws),'dependencies':deps or [],'allowed_paths':[f'{tid}.json'],
                'protected_paths':protected or {},'checks':checks or [],'required_review':review,'owner_acceptance':False,
                'publication':{'kind':'none'},'max_attempts':max_attempts}

    def write_result(self,claim,tid='T1',value=1):
        (self.ws/f'{tid}.json').write_text(json.dumps({'value':value})+'\n',encoding='utf-8')
        receipt=Path(claim["receipt_file"])
        receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':tid,'changed_paths':[f'{tid}.json']})+'\n',encoding='utf-8')
        cap=Path(claim['capability_file'])
        lease=self.orch.lease_from_capability(claim['run_id'],cap)
        self.orch.submit(claim['run_id'],lease,receipt)
        self.orch.quiesce(claim['run_id'],lease)
        return self.orch.verify(claim['run_id'])

    def test_dependency_sequence_and_no_work(self):
        self.load([self.task('T1'),self.task('T2',deps=['T1'])])
        c1=self.orch.claim('w1'); self.assertEqual(c1['task_id'],'T1')
        self.write_result(c1,'T1'); self.orch.complete(c1['run_id'])
        c2=self.orch.claim('w2'); self.assertEqual(c2['task_id'],'T2')
        self.write_result(c2,'T2'); self.orch.complete(c2['run_id'])
        self.assertEqual(self.orch.claim('w3')['status'],'NO_WORK')

    def test_dependency_can_reference_task_from_earlier_plan(self):
        self.load([self.task("CROSS-1")], revision="cross-p1")
        path = self.root / "cross-p2.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "cross-p2",
            "tasks": [self.task("CROSS-2", deps=["CROSS-1"])],
        }) + "\n")
        loaded = self.orch.load_plan(path)
        self.assertEqual(loaded["queued_count"], 1)
        first = self.orch.claim("w1")
        self.assertEqual(first["task_id"], "CROSS-1")
        self.write_result(first, "CROSS-1")
        self.orch.complete(first["run_id"])
        second = self.orch.claim("w2")
        self.assertEqual(second["task_id"], "CROSS-2")

    def test_cross_plan_unknown_dependency_is_rejected_atomically(self):
        path = self.root / "missing-dep.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "missing-dep",
            "tasks": [self.task("CROSS-MISSING", deps=["NO-SUCH-TASK"])],
        }) + "\n")
        with self.assertRaisesRegex(ValueError, "invalid_dependency:NO-SUCH-TASK"):
            self.orch.load_plan(path)
        self.assertEqual(self.orch.status()["tasks"], [])

    def test_single_writer_busy(self):
        self.load([self.task('T1'),self.task('T2')])
        first=self.orch.claim('w1')
        second=self.orch.claim('w2')
        self.assertEqual(first['status'],'CLAIMED'); self.assertEqual(second['status'],'BUSY')
        self.assertEqual(second['active']['run_id'],first['run_id'])

    def test_verified_run_keeps_writer_lock_until_completion(self):
        self.load([self.task("VERIFY-1"), self.task("VERIFY-2")], revision="verified-lock")
        first = self.orch.claim("w1")
        verified = self.write_result(first, "VERIFY-1")
        self.assertEqual(verified["status"], "VERIFIED")
        blocked = self.orch.claim("w2")
        self.assertEqual(blocked["status"], "BUSY")
        self.assertEqual(blocked["active"]["run_id"], first["run_id"])
        self.assertEqual(blocked["active"]["state"], "VERIFIED")
        reconciled = self.orch.reconcile()
        self.assertEqual(reconciled["status"], "ATTENTION")
        self.assertEqual(reconciled["active_runs"], [])
        self.assertEqual(reconciled["writer_locks"][0]["run_id"], first["run_id"])
        self.assertEqual(self.orch.complete(first["run_id"])["status"], "COMPLETE")
        second = self.orch.claim("w2")
        self.assertEqual(second["status"], "CLAIMED")
        self.assertEqual(second["task_id"], "VERIFY-2")

    def _make_git_workspace(self, name="claim-base-repo"):
        repo = Path(self.tmp.name) / name
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)], check=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email",
             "fixture@example.invalid"], check=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name",
             "Claim Base Fixture"], check=True
        )
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "initial"], check=True
        )
        return repo

    def test_dynamic_git_task_binds_head_at_claim(self):
        repo = self._make_git_workspace("dynamic-claim")
        task = self.task("CLAIM-DYNAMIC")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "claim dynamic",
        }
        self.load([task], revision="claim-dynamic")
        expected = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "CLAIMED")
        self.assertEqual(claim["context"]["claim_git_head"], expected)
        with self.orch.connect() as conn:
            row = conn.execute(
                "SELECT claim_git_head FROM runs WHERE run_id=?",
                (claim["run_id"],),
            ).fetchone()
        self.assertEqual(row["claim_git_head"], expected)

    def test_foreign_commit_after_dynamic_claim_blocks_verification(self):
        repo = self._make_git_workspace("dynamic-race")
        task = self.task("CLAIM-RACE")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "claim race",
        }
        self.load([task], revision="claim-race")
        claim = self.orch.claim("worker")
        original = claim["context"]["claim_git_head"]
        (repo / "CLAIM-RACE.json").write_text('{"worker":true}\n')
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"],
            "task_id": "CLAIM-RACE",
            "changed_paths": ["CLAIM-RACE.json"],
        }))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        self.orch.submit(claim["run_id"], lease, receipt)
        self.orch.quiesce(claim["run_id"], lease)

        foreign = repo / "foreign.txt"
        foreign.write_text("foreign\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "foreign.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "foreign",
             "--", "foreign.txt"],
            check=True,
        )
        result = self.orch.verify(claim["run_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "workspace_base_changed")
        evidence = result["feedback"]["evidence"]
        self.assertEqual(evidence["claim_git_head"], original)
        self.assertNotEqual(evidence["git_head"], original)

    def test_stale_explicit_base_blocks_before_capability_is_created(self):
        repo = self._make_git_workspace("stale-claim")
        base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        task = self.task("CLAIM-STALE")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "expected_base": base,
            "commit_message": "claim stale",
        }
        self.load([task], revision="claim-stale")
        (repo / "advance.txt").write_text("advance\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "advance.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "advance"], check=True
        )
        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "BLOCKED")
        self.assertEqual(claim["reason"], "claim_expected_base_changed")
        self.assertEqual(claim["task_id"], "CLAIM-STALE")
        claims = self.orch.runtime / "claims"
        self.assertEqual(list(claims.glob("*.json")), [])
        status = self.orch.status()
        task_row = next(
            item for item in status["tasks"] if item["task_id"] == "CLAIM-STALE"
        )
        self.assertEqual(task_row["status"], "BLOCKED")

    def test_foreign_workspace_bytes_block_before_capability(self):
        repo = self._make_git_workspace("preclaim-dirty")
        task = self.task("PRECLAIM-DIRTY")
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "preclaim dirty",
        }
        self.load([task], revision="preclaim-dirty")
        (repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")

        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "BLOCKED")
        self.assertEqual(
            claim["reason"], "claim_workspace_dirty:foreign.txt"
        )
        self.assertEqual(
            claim["details"]["scope"]["observed_paths"], ["foreign.txt"]
        )
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )

    def test_unchanged_protected_preexisting_dirty_bytes_allow_claim(self):
        repo = self._make_git_workspace("preclaim-protected")
        owner = repo / "owner-note.txt"
        owner.write_text("owner dirty\n", encoding="utf-8")
        import hashlib
        digest = hashlib.sha256(owner.read_bytes()).hexdigest()
        task = self.task("PRECLAIM-PROTECTED", protected={"owner-note.txt": digest})
        task["workspace"] = str(repo)
        task["publication"] = {
            "kind": "git_local",
            "branch": "main",
            "commit_message": "preclaim protected",
        }
        self.load([task], revision="preclaim-protected")

        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "CLAIMED")
        self.assertTrue(Path(claim["capability_file"]).is_file())

    def test_check_authority_drift_blocks_before_capability(self):
        support = self.ws / "preclaim_check.py"
        support.write_text("raise SystemExit(0)\n", encoding="utf-8")
        check = {
            "id": "preclaim-support",
            "argv": [sys.executable, "preclaim_check.py"],
            "cwd": ".",
            "timeout_sec": 5,
        }
        self.load(
            [self.task("PRECLAIM-AUTH", checks=[check])],
            revision="preclaim-auth",
        )
        support.write_text("raise SystemExit(9)\n", encoding="utf-8")

        claim = self.orch.claim("worker")
        self.assertEqual(claim["status"], "BLOCKED")
        self.assertIn(
            "check_authority_changed:preclaim-support:"
            "authority_file:preclaim_check.py:HASH_CHANGED",
            claim["reason"],
        )
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )

    def test_fifo_order_is_preserved_across_separately_loaded_plans(self):
        self.load([self.task("Z-FIRST")], revision="fifo-p1")
        path = self.root / "fifo-p2.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "fifo-p2",
            "tasks": [self.task("A-SECOND")],
        }) + "\n")
        self.orch.load_plan(path)
        first = self.orch.claim("w1")
        self.assertEqual(first["task_id"], "Z-FIRST")
        status = self.orch.status()
        queued = {item["task_id"]: item["queue_seq"] for item in status["tasks"]}
        self.assertLess(queued["Z-FIRST"], queued["A-SECOND"])

    def test_independent_workspaces_can_be_claimed_concurrently(self):
        ws2 = Path(self.tmp.name) / "ws2"
        ws2.mkdir()
        first = self.task("P1-T1")
        first["project_id"] = "project-one"
        second = self.task("P2-T1")
        second["project_id"] = "project-two"
        second["workspace"] = str(ws2)
        self.load([first, second], revision="multi-project")
        c1 = self.orch.claim("w1")
        c2 = self.orch.claim("w2")
        self.assertEqual(c1["task_id"], "P1-T1")
        self.assertEqual(c2["task_id"], "P2-T1")
        self.assertNotEqual(c1["context"]["writer_key"], c2["context"]["writer_key"])
        self.assertEqual(len(self.orch.reconcile()["active_runs"]), 2)

    def test_linked_git_worktrees_share_derived_writer_lock(self):
        repo = Path(self.tmp.name) / "shared-repo"
        linked = Path(self.tmp.name) / "shared-worktree"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "linked", str(linked)],
            check=True,
        )
        first = self.task("SHARED-1")
        first["workspace"] = str(repo)
        second = self.task("SHARED-2")
        second["workspace"] = str(linked)
        self.load([first, second], revision="shared-worktree")
        status = self.orch.status()
        keys = {item["task_id"]: item["writer_key"] for item in status["tasks"]}
        self.assertEqual(keys["SHARED-1"], keys["SHARED-2"])
        self.assertTrue(keys["SHARED-1"].startswith("git:"))
        c1 = self.orch.claim("w1")
        c2 = self.orch.claim("w2")
        self.assertEqual(c1["status"], "CLAIMED")
        self.assertEqual(c2["status"], "BUSY")
        self.assertEqual(c2["active"]["run_id"], c1["run_id"])

    def test_plan_cannot_spoof_writer_key(self):
        task = self.task("SPOOF-1")
        task["writer_key"] = "workspace:" + ("0" * 32)
        with self.assertRaisesRegex(ValueError, "writer_key_mismatch"):
            self.load([task], revision="spoof-writer")
        self.assertEqual(self.orch.status()["tasks"], [])

    def test_project_scoped_claim_can_skip_earlier_other_project(self):
        ws2 = Path(self.tmp.name) / "ws2"
        ws2.mkdir()
        first = self.task("P1-FIRST")
        first["project_id"] = "project-one"
        second = self.task("P2-ONLY")
        second["project_id"] = "project-two"
        second["workspace"] = str(ws2)
        self.load([first, second], revision="scoped-claim")
        selected = self.orch.claim("w2", project_id="project-two")
        self.assertEqual(selected["task_id"], "P2-ONLY")
        self.assertEqual(selected["project_id"], "project-two")
        next_one = self.orch.next_work(project_id="project-one")
        self.assertEqual(next_one["status"], "READY")
        self.assertEqual(next_one["task_id"], "P1-FIRST")

    def test_project_pause_skips_only_that_project_and_persists(self):
        ws2 = Path(self.tmp.name) / "pause-ws2"
        ws2.mkdir()
        one = self.task("PAUSE-P1")
        one["project_id"] = "project-one"
        two = self.task("PAUSE-P2")
        two["project_id"] = "project-two"
        two["workspace"] = str(ws2)
        self.load([one, two], revision="project-pause")

        paused = self.orch.pause_project("project-one", "maintenance")
        self.assertEqual(paused["status"], "PROJECT_PAUSED")
        scoped = self.orch.claim("w1", project_id="project-one")
        self.assertEqual(scoped["status"], "PROJECT_PAUSED")

        other = self.orch.claim("w2")
        self.assertEqual(other["task_id"], "PAUSE-P2")
        restarted = Orchestrator(self.root)
        view = restarted.queue_view(project_id="project-one")
        self.assertEqual(view["status"], "PROJECT_PAUSED")
        self.assertEqual(view["tasks"][0]["queue_state"], "PAUSED_PROJECT")
        self.assertEqual(view["summary"]["paused_project_count"], 1)
        self.assertEqual(
            restarted.next_work()["status"], "PROJECTS_PAUSED"
        )

        resumed = restarted.resume_project("project-one")
        self.assertEqual(resumed["status"], "PROJECT_RESUMED")
        first = restarted.claim("w3")
        self.assertEqual(first["status"], "CLAIMED")
        self.assertEqual(first["task_id"], "PAUSE-P1")

    def test_project_pause_does_not_interrupt_existing_writer(self):
        first = self.task("ACTIVE-P1")
        first["project_id"] = "project-one"
        second = self.task("AFTER-P1")
        second["project_id"] = "project-one"
        self.load([first, second], revision="project-pause-active")
        claim = self.orch.claim("worker")
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        self.orch.pause_project("project-one", "hold new work")
        self.assertEqual(
            self.orch.heartbeat(claim["run_id"], lease)["status"], "OK"
        )
        self.assertEqual(
            self.orch.next_work(project_id="project-one")["status"],
            "PROJECT_PAUSED",
        )
        self.orch.resume_project("project-one")
        busy = self.orch.next_work(project_id="project-one")
        self.assertEqual(busy["status"], "BUSY")
        self.assertEqual(busy["active"]["run_id"], claim["run_id"])

    def test_project_pause_requires_existing_project_queue(self):
        with self.assertRaisesRegex(ValueError, "unknown_project_queue"):
            self.orch.pause_project("missing-project", "nothing loaded")
        resumed = self.orch.resume_project("missing-project")
        self.assertTrue(resumed["already_resumed"])

    def test_queue_view_explains_writer_and_dependency_waits(self):
        first = self.task("QUEUE-1")
        second = self.task("QUEUE-2")
        third = self.task("QUEUE-3", deps=["QUEUE-2"])
        self.load([first, second, third], revision="queue-view")
        claim = self.orch.claim("worker")
        self.assertEqual(claim["task_id"], "QUEUE-1")
        view = self.orch.queue_view()
        by_id = {item["task_id"]: item for item in view["tasks"]}
        self.assertEqual(by_id["QUEUE-1"]["queue_state"], "ACTIVE")
        self.assertEqual(by_id["QUEUE-2"]["queue_state"], "WAITING_WRITER")
        self.assertEqual(by_id["QUEUE-2"]["writer_lock"]["run_id"], claim["run_id"])
        self.assertEqual(by_id["QUEUE-3"]["queue_state"], "WAITING_DEPENDENCY")
        self.assertEqual(by_id["QUEUE-3"]["waiting_dependencies"], ["QUEUE-2"])
        self.assertEqual(view["summary"]["active_writer_count"], 1)

    def test_queue_view_is_bounded_and_project_filterable(self):
        ws2 = Path(self.tmp.name) / "queue-ws2"
        ws2.mkdir()
        one = self.task("QUEUE-P1")
        one["project_id"] = "project-one"
        two = self.task("QUEUE-P2")
        two["project_id"] = "project-two"
        two["workspace"] = str(ws2)
        self.load([one, two], revision="queue-filter")
        filtered = self.orch.queue_view(project_id="project-two", limit=1)
        self.assertEqual(filtered["summary"]["total"], 1)
        self.assertEqual(filtered["tasks"][0]["task_id"], "QUEUE-P2")
        all_tasks = self.orch.queue_view(limit=1)
        self.assertEqual(all_tasks["summary"]["total"], 2)
        self.assertEqual(all_tasks["shown"], 1)
        self.assertTrue(all_tasks["truncated"])

    def test_cancel_task_only_applies_before_execution(self):
        self.load([self.task("CANCEL-1"), self.task("ACTIVE-1")], revision="cancel")
        cancelled = self.orch.cancel_task("CANCEL-1", "operator removed obsolete work")
        self.assertEqual(cancelled["status"], "CANCELLED")
        view = self.orch.queue_view()
        by_id = {item["task_id"]: item for item in view["tasks"]}
        self.assertEqual(by_id["CANCEL-1"]["task_status"], "CANCELLED")
        claim = self.orch.claim("worker")
        self.assertEqual(claim["task_id"], "ACTIVE-1")
        with self.assertRaisesRegex(ValueError, "task_active"):
            self.orch.cancel_task("ACTIVE-1", "must not cancel an active writer")
        repeated = self.orch.cancel_task("CANCEL-1", "idempotent")
        self.assertTrue(repeated["already_cancelled"])

    def test_check_executable_is_bound_and_executed_by_absolute_path(self):
        runner = self.ws / "runner.sh"
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        check = {
            "id": "runner", "argv": ["./runner.sh"],
            "cwd": ".", "timeout_sec": 5,
        }
        self.load([self.task("AUTH-EXEC", checks=[check])], revision="auth-exec")
        claim = self.orch.claim("worker")
        context_check = claim["context"]["checks"][0]
        self.assertEqual(context_check["executable_path"], str(runner.resolve()))
        self.assertEqual(len(context_check["executable_sha256"]), 64)
        result = self.write_result(claim, "AUTH-EXEC")
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(
            result["checks"][0]["executed_argv"][0], str(runner.resolve())
        )
        self.assertEqual(result["check_authority"]["status"], "PASS")

    def test_check_executable_mutation_blocks_before_execution(self):
        sentinel = self.ws / "executed.txt"
        runner = self.ws / "mutable-runner.sh"
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        check = {
            "id": "mutable", "argv": ["./mutable-runner.sh"],
            "cwd": ".", "timeout_sec": 5,
        }
        self.load([self.task("AUTH-MUT", checks=[check])], revision="auth-mut")
        claim = self.orch.claim("worker")
        runner.write_text(
            "#!/bin/sh\necho executed > " + str(sentinel) + "\nexit 0\n",
            encoding="utf-8",
        )
        runner.chmod(0o755)
        result = self.write_result(claim, "AUTH-MUT")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn(
            "check_authority_changed:mutable:executable_hash_changed",
            result["reason"],
        )
        self.assertFalse(sentinel.exists())

    def test_check_support_file_mutation_blocks_before_execution(self):
        support = self.ws / "check_support.py"
        support.write_text("raise SystemExit(0)\n", encoding="utf-8")
        check = {
            "id": "support", "argv": [sys.executable, "check_support.py"],
            "cwd": ".", "timeout_sec": 5,
        }
        self.load([self.task("AUTH-SUPPORT", checks=[check])], revision="auth-support")
        claim = self.orch.claim("worker")
        bound = claim["context"]["checks"][0]["authority_files"]
        self.assertEqual(bound[0]["path"], "check_support.py")
        original = bound[0]["sha256"]
        self.assertEqual(len(original), 64)
        support.write_text("raise SystemExit(7)\n", encoding="utf-8")
        result = self.write_result(claim, "AUTH-SUPPORT")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn(
            "check_authority_changed:support:authority_file:check_support.py:HASH_CHANGED",
            result["reason"],
        )

    def test_oversized_context_blocks_without_run_or_capability(self):
        task = self.task("CONTEXT-LARGE")
        task["allowed_paths"] = [
            f"context/{index:03d}-" + ("x" * 80) + ".json"
            for index in range(400)
        ]
        self.load([task], revision="context-large")

        result = self.orch.claim("worker")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["reason"].startswith("context_pack_too_large:"))
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )
        status = self.orch.status()
        self.assertEqual(status["runs"], [])
        task_row = next(
            item for item in status["tasks"]
            if item["task_id"] == "CONTEXT-LARGE"
        )
        self.assertEqual(task_row["status"], "BLOCKED")
        self.assertEqual(self.orch.reconcile()["status"], "CLEAN")

    def test_claim_creates_private_exact_capability(self):
        self.load(
            [self.task("CAP-ATOMIC")],
            revision="cap-atomic",
        )
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        self.assertEqual(
            cap,
            self.orch.runtime / "claims" / f"{claim['run_id']}.json",
        )
        self.assertEqual(cap.stat().st_mode & 0o777, 0o600)
        payload = json.loads(cap.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"run_id", "lease_token"})
        self.assertEqual(payload["run_id"], claim["run_id"])
        self.assertTrue(payload["lease_token"])
        self.assertEqual(
            self.orch.lease_from_capability(claim["run_id"], cap),
            payload["lease_token"],
        )

    def test_capability_write_failure_aborts_run_and_releases_writer(self):
        self.load(
            [self.task("CAP-WRITE-FAIL")],
            revision="cap-write-fail",
        )
        with mock.patch(
            "orch.core.atomic_write_json",
            side_effect=OSError("synthetic capability write failure"),
        ):
            result = self.orch.claim("worker")

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "capability_create_failed")
        self.assertIn("run_id", result)
        self.assertEqual(
            list((self.orch.runtime / "claims").glob("*.json")), []
        )
        with self.orch.connect() as conn:
            run = conn.execute(
                "SELECT state,error,completed_at FROM runs WHERE run_id=?",
                (result["run_id"],),
            ).fetchone()
            task = conn.execute(
                "SELECT status FROM tasks WHERE task_id='CAP-WRITE-FAIL'"
            ).fetchone()
        self.assertEqual(run["state"], "ABORTED")
        self.assertIn("synthetic capability write failure", run["error"])
        self.assertTrue(run["completed_at"])
        self.assertEqual(task["status"], "BLOCKED")
        self.assertEqual(self.orch.reconcile()["status"], "CLEAN")

    def test_capability_read_rejects_symlink_and_alias_paths(self):
        self.load([self.task("CAP-LINK")], revision="cap-link")
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        alias = self.root / "capability-alias.json"
        alias.symlink_to(cap)
        with self.assertRaisesRegex(ValueError, "invalid_capability_file"):
            self.orch.lease_from_capability(claim["run_id"], alias)

        external = self.root / "external-capability.json"
        cap.rename(external)
        cap.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "invalid_capability_file"):
            self.orch.lease_from_capability(claim["run_id"], cap)
        self.assertTrue(cap.is_symlink())

    def test_capability_read_requires_private_bounded_exact_schema(self):
        self.load([self.task("CAP-BOUND")], revision="cap-bound")
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        cap.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "invalid_capability_mode"):
            self.orch.lease_from_capability(claim["run_id"], cap)

        cap.write_bytes(b"{" + b"x" * 4096)
        cap.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "capability_file_too_large"):
            self.orch.lease_from_capability(claim["run_id"], cap)

    def test_claim_exposes_exact_receipt_path_and_submit_records_digest(self):
        self.load([self.task("RECEIPT-OK")], revision="receipt-ok")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        self.assertEqual(
            receipt,
            self.orch.runtime / "worker_receipts" / f"{claim['run_id']}.json",
        )
        self.assertEqual(claim["context"]["receipt_file"], str(receipt))
        (self.ws / "RECEIPT-OK.json").write_text('{"ok":true}\n')
        raw = (
            json.dumps({
                "schema_version": 1,
                "run_id": claim["run_id"],
                "task_id": "RECEIPT-OK",
                "changed_paths": ["./RECEIPT-OK.json"],
                "summary": "bounded receipt",
            })
            + "\n"
        ).encode("utf-8")
        receipt.write_bytes(raw)
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        submitted = self.orch.submit(claim["run_id"], lease, receipt)
        self.assertEqual(submitted["status"], "RESULT_SUBMITTED")
        self.assertEqual(submitted["receipt_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(submitted["receipt_bytes"], len(raw))
        with self.orch.connect() as conn:
            stored = conn.execute(
                "SELECT receipt_json FROM runs WHERE run_id=?",
                (claim["run_id"],),
            ).fetchone()["receipt_json"]
        stored_data = json.loads(stored)
        self.assertEqual(stored_data["schema_version"], 1)
        self.assertEqual(stored_data["changed_paths"], ["RECEIPT-OK.json"])

    def test_submit_rejects_receipt_outside_claim_path(self):
        self.load([self.task("RECEIPT-PATH")], revision="receipt-path")
        claim = self.orch.claim("worker")
        wrong = self.root / "wrong-receipt.json"
        wrong.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-PATH",
            "changed_paths": ["RECEIPT-PATH.json"],
        }))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "invalid_receipt_path"):
            self.orch.submit(claim["run_id"], lease, wrong)
        self.assertEqual(
            next(
                item for item in self.orch.status()["runs"]
                if item["run_id"] == claim["run_id"]
            )["state"],
            "RUNNING",
        )

    def test_submit_rejects_symlinked_receipt(self):
        self.load([self.task("RECEIPT-LINK")], revision="receipt-link")
        claim = self.orch.claim("worker")
        external = self.root / "external-receipt.json"
        external.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-LINK",
            "changed_paths": ["RECEIPT-LINK.json"],
        }))
        receipt = Path(claim["receipt_file"])
        receipt.symlink_to(external)
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "receipt_file_missing_or_unsafe"):
            self.orch.submit(claim["run_id"], lease, receipt)
        self.assertTrue(receipt.is_symlink())

    def test_submit_rejects_fifo_receipt_without_blocking(self):
        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("FIFO nonblocking open unavailable")
        self.load([self.task("RECEIPT-FIFO")], revision="receipt-fifo")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        os.mkfifo(receipt)
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )

        def timeout_handler(_signum, _frame):
            raise TimeoutError("fifo_open_blocked")

        previous = signal.signal(signal.SIGALRM, timeout_handler)
        try:
            signal.alarm(2)
            with self.assertRaisesRegex(
                ValueError, "receipt_file_missing_or_unsafe"
            ):
                self.orch.submit(claim["run_id"], lease, receipt)
            signal.alarm(0)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertTrue(receipt.exists())

    def test_submit_rejects_oversized_receipt(self):
        self.load([self.task("RECEIPT-BIG")], revision="receipt-big")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        receipt.write_bytes(b"{" + b"x" * (64 * 1024 + 1))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "receipt_too_large"):
            self.orch.submit(claim["run_id"], lease, receipt)

    def test_submit_rejects_future_receipt_schema_and_unknown_fields(self):
        self.load([self.task("RECEIPT-SCHEMA")], revision="receipt-schema")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        receipt.write_text(json.dumps({
            "schema_version": 2,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-SCHEMA",
            "changed_paths": ["RECEIPT-SCHEMA.json"],
        }))
        with self.assertRaisesRegex(ValueError, "invalid_receipt_schema"):
            self.orch.submit(claim["run_id"], lease, receipt)
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-SCHEMA",
            "changed_paths": ["RECEIPT-SCHEMA.json"],
            "unexpected": True,
        }))
        with self.assertRaisesRegex(ValueError, "receipt_unknown_field:unexpected"):
            self.orch.submit(claim["run_id"], lease, receipt)

    def test_submit_bounds_changed_path_count(self):
        task = self.task("RECEIPT-MANY")
        task["allowed_paths"] = ["out/"]
        self.load([task], revision="receipt-many")
        claim = self.orch.claim("worker")
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": "RECEIPT-MANY",
            "changed_paths": [f"out/{index}.json" for index in range(257)],
        }))
        lease = self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        with self.assertRaisesRegex(ValueError, "receipt_changed_paths_too_many"):
            self.orch.submit(claim["run_id"], lease, receipt)

    def test_scope_escape_rejected(self):
        self.load([self.task('T1')]); c=self.orch.claim('w')
        receipt=Path(c["receipt_file"]); receipt.write_text(json.dumps({'run_id':c['run_id'],'task_id':'T1','changed_paths':['../escape']})+'\n')
        lease=self.orch.lease_from_capability(c['run_id'],Path(c['capability_file']))
        with self.assertRaisesRegex(ValueError,'path_not_allowed'):
            self.orch.submit(c['run_id'],lease,receipt)

    def test_failed_check_feedback_moves_to_new_chat_attempt(self):
        check={'id':'fail','argv':[sys.executable,'-c','import sys; sys.exit(7)'],'cwd':'.','timeout_sec':5}
        self.load([self.task('T1',checks=[check])])
        c1=self.orch.claim('w1'); result=self.write_result(c1,'T1')
        self.assertEqual(result['status'],'NEEDS_FIX')
        c2=self.orch.claim('w2')
        self.assertEqual(c2['attempt'],2)
        self.assertEqual(c2['context']['feedback']['kind'],'verification_failure')
        self.assertEqual(c2['context']['previous_snapshot_id'],result['snapshot_id'])

    def test_protected_change_blocks_verification_without_stuck_writer(self):
        sentinel=self.ws/'owner.txt'; sentinel.write_text('keep\n')
        digest=hashlib.sha256(sentinel.read_bytes()).hexdigest()
        self.load([self.task('T1',protected={'owner.txt':digest})])
        c=self.orch.claim('w'); (self.ws/'T1.json').write_text('{"value":1}\n'); sentinel.write_text('changed\n')
        receipt=Path(c["receipt_file"]); receipt.write_text(json.dumps({'run_id':c['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        cap=Path(c['capability_file']); lease=self.orch.lease_from_capability(c['run_id'],cap)
        self.orch.submit(c['run_id'],lease,receipt); quiesced=self.orch.quiesce(c['run_id'],lease)
        self.assertTrue(quiesced['capability_revoked']); self.assertFalse(cap.exists())
        blocked=self.orch.verify(c['run_id'])
        self.assertEqual(blocked['status'],'BLOCKED')
        self.assertIn('protected_path_changed',blocked['reason'])
        self.assertEqual(self.orch.reconcile()['status'],'CLEAN')
        self.assertEqual(self.orch.next_work()['status'],'NO_WORK')

    def test_review_is_snapshot_bound_and_feedback_reappears(self):
        self.load([self.task('T1',review=True)])
        c1=self.orch.claim('w1'); verified=self.write_result(c1,'T1')
        self.assertEqual(verified['status'],'REVIEWING')
        self.assertEqual(
            Path(verified['review_report_file']),
            self.orch.runtime / 'review_exports' / c1['run_id'] / 'review.json',
        )
        prepared=prepare_review(self.orch,c1['run_id'])
        report=Path(prepared['report'])
        self.assertEqual(report, Path(verified['review_report_file']))
        report.write_text(json.dumps({
            'run_id':c1['run_id'],
            'snapshot_id':'sha256:' + ('0' * 64),
            'verdict':'PASS','findings':[],'uncertainty':[]
        })+'\n')
        with self.assertRaisesRegex(ValueError,'stale_review'):
            self.orch.import_review(c1['run_id'],report)
        raw=(json.dumps({
            'run_id':c1['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'NEEDS_FIX',
            'findings':[{
                'severity':'important','path':'T1.json',
                'evidence':'fixture','impact':'repair'
            }],
            'uncertainty':[]
        })+'\n').encode('utf-8')
        report.write_bytes(raw)
        imported=self.orch.import_review(c1['run_id'],report)
        self.assertEqual(imported['report_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(imported['report_bytes'],len(raw))
        c2=self.orch.claim('w2')
        self.assertEqual(c2['context']['feedback']['kind'],'review')
        self.assertEqual(c2['context']['feedback']['findings'][0]['path'],'T1.json')

    def _review_fixture(self, task_id):
        self.load([self.task(task_id,review=True)],revision=f'{task_id}-plan')
        claim=self.orch.claim('review-worker')
        verified=self.write_result(claim,task_id)
        self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id'])
        return claim,verified,Path(prepared['report'])

    def test_review_import_rejects_alternate_report_path(self):
        claim,verified,expected=self._review_fixture('REVIEW-PATH')
        alternate=self.root/'alternate-review.json'
        alternate.write_text(json.dumps({
            'run_id':claim['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'PASS','findings':[],'uncertainty':[]
        }))
        with self.assertRaisesRegex(ValueError,'invalid_review_report_path'):
            self.orch.import_review(claim['run_id'],alternate)
        self.assertFalse(expected.exists())

    def test_review_import_rejects_symlinked_and_oversized_report(self):
        claim,verified,report=self._review_fixture('REVIEW-FILE')
        external=self.root/'external-review.json'
        external.write_text(json.dumps({
            'run_id':claim['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'PASS','findings':[],'uncertainty':[]
        }))
        report.symlink_to(external)
        with self.assertRaisesRegex(ValueError,'review_report_missing_or_unsafe'):
            self.orch.import_review(claim['run_id'],report)
        report.unlink()
        report.write_bytes(b'{' + (b'x' * (256 * 1024 + 1)))
        with self.assertRaisesRegex(ValueError,'review_report_too_large'):
            self.orch.import_review(claim['run_id'],report)

    def test_review_import_bounds_structure(self):
        claim,verified,report=self._review_fixture('REVIEW-STRUCT')
        base={
            'run_id':claim['run_id'],'snapshot_id':verified['snapshot_id'],
            'verdict':'PASS','findings':[],'uncertainty':[]
        }
        unknown=dict(base); unknown['extra']=True
        report.write_text(json.dumps(unknown))
        with self.assertRaisesRegex(ValueError,'review_report_unknown_field:extra'):
            self.orch.import_review(claim['run_id'],report)
        many=dict(base)
        many['findings']=[
            {'severity':'info','path':'x','evidence':'e','impact':'i'}
            for _ in range(101)
        ]
        report.write_text(json.dumps(many))
        with self.assertRaisesRegex(ValueError,'review_findings_too_many'):
            self.orch.import_review(claim['run_id'],report)

    def test_owner_approval_is_snapshot_bound(self):
        task=self.task('T1'); task['owner_acceptance']=True
        self.load([task])
        c=self.orch.claim('w'); self.write_result(c,'T1')
        self.assertEqual(self.orch.complete(c['run_id'])['status'],'WAITING_OWNER')
        approved=self.orch.approve(c['run_id'],'fixture approval')
        self.assertEqual(approved['status'],'APPROVED')
        self.assertEqual(self.orch.complete(c['run_id'])['status'],'COMPLETE')

    def test_pause_blocks_dispatch_until_resume(self):
        self.load([self.task('T1')])
        paused=self.orch.pause('fixture maintenance')
        self.assertEqual(paused['status'],'PAUSED')
        self.assertEqual(self.orch.claim('w')['status'],'PAUSED')
        self.assertEqual(self.orch.next_work()['status'],'PAUSED')
        self.assertEqual(self.orch.resume()['status'],'RESUMED')
        self.assertEqual(self.orch.claim('w')['status'],'CLAIMED')

    def test_abort_retry_releases_writer_and_creates_new_attempt(self):
        self.load([self.task('T1')])
        first=self.orch.claim('w1')
        aborted=self.orch.abort(first['run_id'],'synthetic crash recovery',retry=True)
        self.assertEqual(aborted['task_status'],'NEEDS_FIX')
        second=self.orch.claim('w2')
        self.assertEqual(second['status'],'CLAIMED')
        self.assertEqual(second['attempt'],2)
        self.assertNotEqual(second['run_id'],first['run_id'])

    def test_review_export_contains_check_support_and_verifier_evidence(self):
        checks_dir=self.ws/'checks'; checks_dir.mkdir()
        check_file=checks_dir/'check.py'
        check_file.write_text("print('support-ok')\n",encoding='utf-8')
        check={'id':'support','argv':[sys.executable,'checks/check.py'],'cwd':'.','timeout_sec':5}
        self.load([self.task('T1',checks=[check],review=True)])
        claim=self.orch.claim('w')
        verified=self.write_result(claim,'T1')
        self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id'])
        workspace=Path(prepared['workspace'])
        prompt=json.loads(Path(prepared['prompt']).read_text())
        self.assertTrue((workspace/'checks/check.py').is_file())
        check_evidence = prompt['verification_evidence'][0]['evidence_file']
        scope_evidence = prompt['scope_evidence']['evidence_file']
        authority_evidence = prompt['check_authority_evidence']['evidence_file']
        self.assertTrue((workspace/check_evidence).is_file())
        self.assertTrue((workspace/scope_evidence).is_file())
        self.assertTrue((workspace/authority_evidence).is_file())
        internal_root = Path(scope_evidence).parts[0]
        self.assertTrue(internal_root.startswith('.orch-review-evidence-'))
        self.assertEqual(Path(authority_evidence).parts[0], internal_root)
        self.assertEqual(Path(check_evidence).parts[0], internal_root)
        self.assertEqual(Path(check_evidence).parts[1], 'checks')
        self.assertIn('checks/check.py',prompt['support_files'])
        self.assertEqual(prompt['verification_evidence'][0]['exit_code'],0)
        self.assertEqual(prompt['scope_evidence']['status'],'NON_GIT_UNAVAILABLE')
        self.assertEqual(
            prompt['check_authority_evidence']['status'], 'PASS'
        )
        with self.orch.connect() as conn:
            snap = conn.execute(
                'SELECT manifest_json FROM snapshots WHERE snapshot_id=?',
                (verified['snapshot_id'],),
            ).fetchone()
        manifest = json.loads(snap['manifest_json'])
        self.assertIn('verification_evidence', manifest)
        self.assertEqual(
            verified['snapshot_id'],
            'sha256:' + hashlib.sha256(
                canonical_json(manifest).encode('utf-8')
            ).hexdigest(),
        )

    def test_review_export_internal_evidence_never_overwrites_project_path(self):
        relative = 'verification_evidence/scope.json'
        task = self.task('EVIDENCE-NAMESPACE', review=True)
        task['allowed_paths'] = [relative]
        self.load([task], revision='review-evidence-namespace')
        claim = self.orch.claim('w')
        project_file = self.ws / relative
        project_file.parent.mkdir(parents=True)
        original = b'{"project":"preserve"}\n'
        project_file.write_bytes(original)
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'EVIDENCE-NAMESPACE',
            'changed_paths': [relative],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)
        verified = self.orch.verify(claim['run_id'])
        self.assertEqual(verified['status'], 'REVIEWING')

        prepared = prepare_review(self.orch, claim['run_id'])
        workspace = Path(prepared['workspace'])
        prompt = json.loads(Path(prepared['prompt']).read_text())
        exported_project_file = workspace / relative
        self.assertEqual(exported_project_file.read_bytes(), original)
        scope_evidence = prompt['scope_evidence']['evidence_file']
        self.assertNotEqual(scope_evidence, relative)
        self.assertTrue((workspace / scope_evidence).is_file())
        self.assertTrue(
            Path(scope_evidence).parts[0].startswith(
                '.orch-review-evidence-'
            )
        )

    def test_review_export_check_evidence_cannot_alias_system_evidence(self):
        checks = [
            {
                'id': check_id,
                'argv': [sys.executable, '-c', 'print("ok")'],
                'cwd': '.',
                'timeout_sec': 5,
            }
            for check_id in ('scope', 'authority')
        ]
        self.load(
            [self.task('EVIDENCE-CHECK-ID', checks=checks, review=True)],
            revision='review-evidence-check-id',
        )
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'EVIDENCE-CHECK-ID')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        prompt = json.loads(Path(prepared['prompt']).read_text())
        workspace = Path(prepared['workspace'])

        scope_path = Path(prompt['scope_evidence']['evidence_file'])
        authority_path = Path(
            prompt['check_authority_evidence']['evidence_file']
        )
        scope_payload = json.loads(
            (workspace / scope_path).read_text(encoding='utf-8')
        )
        authority_payload = json.loads(
            (workspace / authority_path).read_text(encoding='utf-8')
        )
        self.assertEqual(scope_path.name, 'scope.json')
        self.assertEqual(authority_path.name, 'check-authority.json')
        self.assertEqual(
            scope_payload['status'], 'NON_GIT_UNAVAILABLE'
        )
        self.assertEqual(authority_payload['status'], 'PASS')

        by_id = {
            item['id']: item for item in prompt['verification_evidence']
        }
        self.assertEqual(set(by_id), {'scope', 'authority'})
        for check_id in ('scope', 'authority'):
            check_path = Path(by_id[check_id]['evidence_file'])
            self.assertEqual(
                check_path.name, f'check-result-{check_id}.json'
            )
            self.assertEqual(check_path.parent.name, 'checks')
            self.assertNotIn(check_path, {scope_path, authority_path})
            check_payload = json.loads(
                (workspace / check_path).read_text(encoding='utf-8')
            )
            self.assertEqual(check_payload['id'], check_id)
            self.assertEqual(check_payload['exit_code'], 0)
            self.assertEqual(by_id[check_id]['exit_code'], 0)

    def test_review_export_rejects_workspace_drift_after_verify(self):
        self.load([self.task('T1', review=True)], revision='review-drift')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        (self.ws / 'T1.json').write_text(
            '{"value":999}\n', encoding='utf-8'
        )
        with self.assertRaisesRegex(
            ValueError, 'snapshot_stale:T1.json'
        ):
            prepare_review(self.orch, claim['run_id'])

    def test_review_export_rejects_support_drift_after_verify(self):
        checks_dir = self.ws / 'checks'
        checks_dir.mkdir()
        check_file = checks_dir / 'check.py'
        check_file.write_text(
            "print('support-ok')\n", encoding='utf-8'
        )
        check = {
            'id': 'support-drift',
            'argv': [sys.executable, 'checks/check.py'],
            'cwd': '.',
            'timeout_sec': 5,
        }
        self.load(
            [self.task('T1', checks=[check], review=True)],
            revision='review-support-drift',
        )
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        check_file.write_text(
            "print('mutated-after-verify')\n", encoding='utf-8'
        )
        with self.assertRaisesRegex(
            ValueError, 'review_support_stale:checks/check.py'
        ):
            prepare_review(self.orch, claim['run_id'])

    def test_review_export_rejects_tampered_verifier_evidence(self):
        self.load(
            [self.task('T1', review=True)],
            revision='review-evidence-drift',
        )
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        scope_log = (
            self.orch.logs / f"{claim['run_id']}-scope.json"
        )
        scope_log.write_text(
            '{"status":"tampered"}\n', encoding='utf-8'
        )
        with self.assertRaisesRegex(
            ValueError, 'review_evidence_stale:scope'
        ):
            prepare_review(self.orch, claim['run_id'])
        self.assertFalse(
            (self.orch.runtime / 'review_exports' / claim['run_id']).exists()
        )

    def test_review_execution_rejects_tampered_prepared_prompt(self):
        self.load([self.task('T1', review=True)], revision='review-prompt-bind')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        Path(prepared['prompt']).write_text(
            '{"tampered":true}\n', encoding='utf-8'
        )
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_prompt_stale'):
                run_review(self.orch, claim['run_id'], execute=True)
        run.assert_not_called()

    def test_review_execution_rejects_tampered_prepared_schema(self):
        self.load([self.task('T1', review=True)], revision='review-schema-bind')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        Path(prepared['schema']).write_text(
            '{"tampered":true}\n', encoding='utf-8'
        )
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_schema_stale'):
                run_review(self.orch, claim['run_id'], execute=True)
        run.assert_not_called()

    def test_review_execution_refuses_preexisting_events_symlink(self):
        self.load([self.task('T1', review=True)], revision='review-events-link')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        victim = self.root / 'events-victim.txt'
        victim.write_text('owner-preserve\n', encoding='utf-8')
        events = Path(prepared['export']) / 'events.jsonl'
        events.symlink_to(victim)
        before = victim.read_bytes()
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value={'status': 'PASS'},
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_events_unsafe'):
                run_review(self.orch, claim['run_id'], execute=True)
        run.assert_not_called()
        self.assertEqual(victim.read_bytes(), before)
        self.assertTrue(events.is_symlink())

    def test_review_execution_refuses_preexisting_report_symlink(self):
        self.load([self.task('T1', review=True)], revision='review-report-link')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])
        victim = self.root / 'report-victim.json'
        victim.write_text('owner-preserve\n', encoding='utf-8')
        report = Path(prepared['report'])
        report.symlink_to(victim)
        before = victim.read_bytes()
        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value={'status': 'PASS'},
        ), mock.patch('orch.codex_review.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'review_report_preexisting'):
                run_review(self.orch, claim['run_id'], execute=True)
        run.assert_not_called()
        self.assertEqual(victim.read_bytes(), before)
        self.assertTrue(report.is_symlink())

    def test_review_execution_blocks_oversized_event_stream(self):
        self.load([self.task('T1', review=True)], revision='review-events-big')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])

        def fake_run(cmd, **kwargs):
            kwargs['stdout'].write('0123456789abcdef')
            kwargs['stdout'].flush()
            return subprocess.CompletedProcess(cmd, 0, stderr='')

        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value={'status': 'PASS'},
        ), mock.patch(
            'orch.codex_review.REVIEW_EVENTS_MAX_BYTES', 8
        ), mock.patch(
            'orch.codex_review.subprocess.run', side_effect=fake_run
        ):
            result = run_review(self.orch, claim['run_id'], execute=True)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'review_events_too_large')
        self.assertFalse(Path(prepared['report']).exists())

    def test_review_execution_imports_mocked_bound_report(self):
        self.load([self.task('T1', review=True)], revision='review-exec-ok')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])

        def fake_run(cmd, **kwargs):
            kwargs['stdout'].write('{"event":"complete"}\n')
            kwargs['stdout'].flush()
            report = Path(cmd[cmd.index('-o') + 1])
            report.write_text(json.dumps({
                'run_id': claim['run_id'],
                'snapshot_id': verified['snapshot_id'],
                'verdict': 'PASS',
                'findings': [],
                'uncertainty': [],
            }) + '\n', encoding='utf-8')
            return subprocess.CompletedProcess(cmd, 0, stderr='')

        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value={'status': 'PASS'},
        ), mock.patch(
            'orch.codex_review.subprocess.run', side_effect=fake_run
        ):
            result = run_review(self.orch, claim['run_id'], execute=True)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['imported']['status'], 'PASS')
        events = Path(result['events'])
        self.assertEqual(events.stat().st_mode & 0o777, 0o600)
        self.assertGreater(result['events_bytes'], 0)

    def test_review_execution_timeout_is_blocked(self):
        self.load([self.task('T1', review=True)], revision='review-exec-timeout')
        claim = self.orch.claim('w')
        verified = self.write_result(claim, 'T1')
        self.assertEqual(verified['status'], 'REVIEWING')
        prepared = prepare_review(self.orch, claim['run_id'])

        def timeout_run(cmd, **kwargs):
            kwargs['stdout'].write('{"event":"started"}\n')
            kwargs['stdout'].flush()
            raise subprocess.TimeoutExpired(cmd, 180, stderr='timed out')

        with mock.patch(
            'orch.codex_review.prepare_review', return_value=prepared
        ), mock.patch(
            'orch.codex_review.subscription_preflight',
            return_value={'status': 'PASS'},
        ), mock.patch(
            'orch.codex_review.subprocess.run', side_effect=timeout_run
        ):
            result = run_review(self.orch, claim['run_id'], execute=True)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'review_timeout')
        self.assertTrue(Path(result['events']).is_file())

    def test_verifier_blocks_check_mutation_of_snapshotted_file(self):
        mutator = self.ws / 'mutate.py'
        mutator.write_text(
            "from pathlib import Path\n"
            "Path('T1.json').write_text("
            "'{\\\"value\\\":999}\\n', encoding='utf-8')\n",
            encoding='utf-8',
        )
        check = {
            'id': 'mutates-output',
            'argv': [sys.executable, 'mutate.py'],
            'cwd': '.',
            'timeout_sec': 5,
        }
        self.load(
            [self.task('T1', checks=[check], review=True)],
            revision='review-check-mutation',
        )
        claim = self.orch.claim('w')
        result = self.write_result(claim, 'T1')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'snapshot_stale:T1.json')

    def test_verifier_rejects_dangling_symlink_in_declared_output(self):
        self.load([self.task('T1')], revision='dangling-output-symlink')
        claim = self.orch.claim('w')
        (self.ws / 'T1.json').symlink_to(self.ws / 'missing-output.json')
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)

        result = self.orch.verify(claim['run_id'])

        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'symlink_not_allowed:T1.json')

    def test_completion_rejects_dangling_symlink_after_verified_deletion(self):
        output = self.ws / 'T1.json'
        output.write_text('{"value": 1}\n', encoding='utf-8')
        self.load([self.task('T1')], revision='dangling-after-deletion')
        claim = self.orch.claim('w')
        output.unlink()
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)
        self.assertEqual(self.orch.verify(claim['run_id'])['status'], 'VERIFIED')
        output.symlink_to(self.ws / 'missing-output.json')

        with self.assertRaisesRegex(ValueError, 'snapshot_stale:T1.json'):
            self.orch.complete(claim['run_id'])

    def test_verifier_rejects_dangling_ancestor_symlink_in_declared_output(self):
        task = self.task('T1')
        task['allowed_paths'] = ['outputs/']
        self.load([task], revision='dangling-output-ancestor-symlink')
        claim = self.orch.claim('w')
        (self.ws / 'outputs').symlink_to(
            self.ws / 'missing-output-dir', target_is_directory=True
        )
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['outputs/T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)

        result = self.orch.verify(claim['run_id'])

        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(
            result['reason'], 'symlink_not_allowed:outputs/T1.json'
        )

    def test_completion_rejects_dangling_ancestor_symlink_after_deletion(self):
        output = self.ws / 'outputs' / 'T1.json'
        output.parent.mkdir()
        output.write_text('{"value": 1}\n', encoding='utf-8')
        task = self.task('T1')
        task['allowed_paths'] = ['outputs/']
        self.load([task], revision='dangling-after-ancestor-deletion')
        claim = self.orch.claim('w')
        output.unlink()
        receipt = Path(claim['receipt_file'])
        receipt.write_text(json.dumps({
            'run_id': claim['run_id'],
            'task_id': 'T1',
            'changed_paths': ['outputs/T1.json'],
        }) + '\n', encoding='utf-8')
        lease = self.orch.lease_from_capability(
            claim['run_id'], Path(claim['capability_file'])
        )
        self.orch.submit(claim['run_id'], lease, receipt)
        self.orch.quiesce(claim['run_id'], lease)
        self.assertEqual(self.orch.verify(claim['run_id'])['status'], 'VERIFIED')
        output.parent.rmdir()
        output.parent.symlink_to(
            self.ws / 'missing-output-dir', target_is_directory=True
        )

        with self.assertRaisesRegex(
            ValueError, 'snapshot_stale:outputs/T1.json'
        ):
            self.orch.complete(claim['run_id'])
        with self.orch.connect() as conn:
            state = conn.execute(
                'SELECT state FROM runs WHERE run_id=?', (claim['run_id'],)
            ).fetchone()['state']
        self.assertEqual(state, 'VERIFIED')

    def test_dependency_cycle_is_rejected_at_plan_load(self):
        one=self.task('T1',deps=['T2']); two=self.task('T2',deps=['T1'])
        with self.assertRaisesRegex(ValueError,'dependency_cycle'):
            self.load([one,two])

    def test_task_id_conflict_across_plan_revisions_is_rejected(self):
        self.load([self.task('T1')],revision='p1')
        path=self.root/'p2.json'
        path.write_text(json.dumps({'schema_version':1,'plan_revision':'p2','tasks':[self.task('T1')]})+'\n')
        with self.assertRaisesRegex(ValueError,'task_id_conflict:T1'):
            self.orch.load_plan(path)

    def test_capability_is_revoked_on_quiesce_and_abort(self):
        self.load([self.task('T1'),self.task('T2')])
        c1=self.orch.claim('w1'); cap1=Path(c1['capability_file']); lease1=self.orch.lease_from_capability(c1['run_id'],cap1)
        (self.ws/'T1.json').write_text('{"value":1}\n')
        receipt=Path(c1["receipt_file"]); receipt.write_text(json.dumps({'run_id':c1['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        self.orch.submit(c1['run_id'],lease1,receipt); self.orch.quiesce(c1['run_id'],lease1)
        self.assertFalse(cap1.exists())
        self.orch.verify(c1['run_id']); self.orch.complete(c1['run_id'])
        c2=self.orch.claim('w2'); cap2=Path(c2['capability_file'])
        self.assertTrue(cap2.exists()); self.orch.abort(c2['run_id'],'stop',retry=False)
        self.assertFalse(cap2.exists())

    def test_invalid_check_and_protected_hash_are_rejected_at_plan_load(self):
        bad_check=self.task('T1',checks=[{'id':'bad','argv':[],'cwd':'.'}])
        with self.assertRaisesRegex(ValueError,'invalid_check_argv'):
            self.load([bad_check],revision='bad-check')
        bad_hash=self.task('T2',protected={'owner.txt':'not-a-sha'})
        with self.assertRaisesRegex(ValueError,'invalid_protected_hash'):
            self.load([bad_hash],revision='bad-hash')

    def test_review_export_records_deleted_files_without_recreating_them(self):
        task=self.task('T1',review=True); self.load([task])
        target=self.ws/'T1.json'; target.write_text('{"old":true}\n',encoding='utf-8')
        claim=self.orch.claim('w'); target.unlink()
        receipt=Path(claim["receipt_file"])
        receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        lease=self.orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        self.orch.submit(claim['run_id'],lease,receipt); self.orch.quiesce(claim['run_id'],lease)
        verified=self.orch.verify(claim['run_id']); self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id']); prompt=json.loads(Path(prepared['prompt']).read_text())
        self.assertEqual(prompt['deleted_files'],['T1.json'])
        self.assertTrue(prompt['file_manifest']['T1.json']['deleted'])
        self.assertFalse((Path(prepared['workspace'])/'T1.json').exists())

    def test_plan_revision_digest_conflict(self):
        self.load([self.task('T1')],revision='same')
        path=self.root/'other.json'; path.write_text(json.dumps({'schema_version':1,'plan_revision':'same','tasks':[self.task('T2')]})+'\n')
        with self.assertRaisesRegex(ValueError,'plan_revision_digest_conflict'):
            self.orch.load_plan(path)

if __name__=='__main__':
    unittest.main()
