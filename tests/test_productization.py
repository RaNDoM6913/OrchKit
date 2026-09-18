import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch.cli import main as cli_main
from orch.config import configure_home
from orch.doctor import run_doctor
from orch.dispatcher import render_dispatcher
from orch.git_policy import evaluate_project_git_policy
from orch.core import Orchestrator, path_allowed
from orch.plan import build_single_task_plan
from orch.project import ProjectRegistry
from orch.review_policy import decide_review, normalize_review_policy


class ProductizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "initial"], check=True, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_setup_persists_default_profile(self):
        configured = configure_home(self.home, profile="standard")
        data = json.loads(Path(configured["config"]).read_text())
        self.assertEqual(data["default_profile"], "standard")
        self.assertEqual(data["billing"]["model_api_budget"], 0)

    def test_review_off_never_requires_codex(self):
        payload = {"review": {"mode": "off", "reviewer": "none"}}
        manifest = {"files": {"security/auth.py": {"bytes": 99}}}
        decision = decide_review(payload, manifest, attempt=2)
        self.assertFalse(decision["required"])
        self.assertEqual(decision["reviewer"], "none")

    def test_risk_based_security_tag_requires_codex(self):
        payload = {"review": {"mode": "risk_based", "reviewer": "codex", "risk_tags": ["security"]}}
        decision = decide_review(payload, {"files": {"app.py": {"bytes": 12}}})
        self.assertTrue(decision["required"])
        self.assertEqual(decision["reviewer"], "codex")
        self.assertIn("risk_tag:security", decision["signals"])

    def test_legacy_required_review_is_compatible(self):
        policy = normalize_review_policy({"required_review": True})
        self.assertEqual(policy["mode"], "required")
        self.assertEqual(policy["reviewer"], "codex")

    def test_dotfile_scope_is_not_equivalent_to_plain_filename(self):
        self.assertFalse(path_allowed(".env", ["env"]))
        self.assertTrue(path_allowed(".env", [".env"]))
        self.assertFalse(path_allowed("../escape", ["escape"]))

    def test_enabled_review_rejects_none_reviewer(self):
        with self.assertRaisesRegex(ValueError, "reviewer_required"):
            ProjectRegistry(self.home).add(self.repo, profile="standard", review_mode="required", reviewer="none")

    def test_project_registration_protects_existing_dirty_bytes(self):
        dirty = self.repo / "owner-note.txt"
        dirty.write_text("owner work\n", encoding="utf-8")
        registry = ProjectRegistry(self.home)
        registered = registry.add(self.repo, profile="standard", review_mode="off")
        config = registered["project"]
        self.assertEqual(config["review"]["mode"], "off")
        self.assertEqual(config["review"]["reviewer"], "none")
        self.assertTrue(config["git"]["allow_commit"])
        self.assertTrue(config["git"]["allow_push"])
        expected = hashlib.sha256(dirty.read_bytes()).hexdigest()
        self.assertEqual(config["protected_paths"]["owner-note.txt"], expected)
        policy = evaluate_project_git_policy(config)
        self.assertEqual(policy["status"], "READY")
        dirty.write_text("foreign change\n", encoding="utf-8")
        blocked = evaluate_project_git_policy(config)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("protected_baseline_changed", blocked["safety_blockers"])

    def test_safe_profile_denies_publication_by_default(self):
        registry = ProjectRegistry(self.home)
        config = registry.add(self.repo, profile="safe")["project"]
        policy = evaluate_project_git_policy(config)
        self.assertFalse(policy["can_commit"])
        self.assertFalse(policy["can_push"])
        self.assertFalse(policy["force_push_allowed"])

    def test_standard_without_remote_can_commit_but_not_push(self):
        registry = ProjectRegistry(self.home)
        config = registry.add(self.repo, profile="standard")["project"]
        policy = evaluate_project_git_policy(config)
        self.assertTrue(policy["can_commit"])
        self.assertFalse(policy["can_push"])
        self.assertIn("remote_missing", policy["push_blockers"])

    def test_setup_default_profile_applies_when_project_profile_omitted(self):
        configure_home(self.home, profile="standard")
        config = ProjectRegistry(self.home).add(self.repo)["project"]
        self.assertEqual(config["profile"], "standard")
        self.assertTrue(config["git"]["allow_commit"])

    def test_registered_project_plan_carries_stable_writer_identity(self):
        registry = ProjectRegistry(self.home)
        config = registry.add(self.repo, profile="standard", review_mode="off")["project"]
        plan = build_single_task_plan(
            config, task_id="IDENTITY-1", goal="identity", allowed_paths=["identity.json"]
        )
        task = plan["tasks"][0]
        self.assertEqual(task["project_id"], config["project_id"])
        self.assertEqual(task["writer_key"], config["writer_key"])
        self.assertTrue(task["writer_key"].startswith("git:"))
        self.assertEqual(config["inventory_at_registration"]["writer_key"], config["writer_key"])

    def test_queue_enqueue_compiles_registered_project_and_cross_plan_dependency(self):
        registry = ProjectRegistry(self.home)
        config = registry.add(self.repo, profile="standard", review_mode="off")["project"]

        def enqueue(*extra):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = cli_main([
                    "--root", str(self.home), "queue", "enqueue", config["project_id"],
                    *extra,
                ])
            return rc, json.loads(output.getvalue())

        rc1, first = enqueue(
            "--task-id", "ENQ-1", "--goal", "first",
            "--allowed-path", "one.json", "--plan-revision", "enqueue-one",
        )
        self.assertEqual(rc1, 0)
        self.assertEqual(first["status"], "ENQUEUED")
        self.assertEqual(first["load"]["queued_count"], 1)
        plan1 = Path(first["plan_path"])
        self.assertEqual(plan1.stat().st_mode & 0o777, 0o600)

        rc2, second = enqueue(
            "--task-id", "ENQ-2", "--goal", "second",
            "--allowed-path", "two.json", "--depends", "ENQ-1",
            "--max-attempts", "3", "--plan-revision", "enqueue-two",
        )
        self.assertEqual(rc2, 0)
        self.assertEqual(second["load"]["queued_count"], 1)
        compiled = json.loads(Path(second["plan_path"]).read_text())
        task = compiled["tasks"][0]
        self.assertEqual(task["dependencies"], ["ENQ-1"])
        self.assertEqual(task["max_attempts"], 3)
        self.assertNotIn("expected_base", task["publication"])

        view = Orchestrator(self.home).queue_view()
        by_id = {item["task_id"]: item for item in view["tasks"]}
        self.assertEqual(by_id["ENQ-1"]["queue_state"], "READY")
        self.assertEqual(by_id["ENQ-2"]["queue_state"], "WAITING_DEPENDENCY")

        rc3, repeated = enqueue(
            "--task-id", "ENQ-1", "--goal", "first",
            "--allowed-path", "one.json", "--plan-revision", "enqueue-one",
        )
        self.assertEqual(rc3, 0)
        self.assertEqual(repeated["plan_artifact_status"], "EXISTS")
        self.assertEqual(repeated["load"]["queued_count"], 0)

        original = plan1.read_text()
        rc4, conflict = enqueue(
            "--task-id", "ENQ-1", "--goal", "changed goal",
            "--allowed-path", "one.json", "--plan-revision", "enqueue-one",
        )
        self.assertEqual(rc4, 1)
        self.assertEqual(conflict["error"], "plan_artifact_conflict")
        self.assertEqual(plan1.read_text(), original)

    def test_generated_plan_uses_git_local_when_commit_allowed_without_remote(self):
        config = ProjectRegistry(self.home).add(self.repo, profile="standard", review_mode="off")["project"]
        plan = build_single_task_plan(config, task_id="TASK-1", goal="create result", allowed_paths=["result.json"])
        task = plan["tasks"][0]
        self.assertEqual(task["publication"]["kind"], "git_local")
        orch = Orchestrator(self.home)
        plan_path = self.home / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        orch.load_plan(plan_path)
        claim = orch.claim("fixture")
        (self.repo / "result.json").write_text('{"ok":true}\n', encoding="utf-8")
        receipt = self.home / "receipt.json"
        receipt.write_text(json.dumps({"run_id": claim["run_id"], "task_id": "TASK-1", "changed_paths": ["result.json"]}), encoding="utf-8")
        lease = orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        verified = orch.verify(claim["run_id"])
        self.assertEqual(verified["status"], "VERIFIED")
        published = orch.publish(claim["run_id"])
        self.assertEqual(published["status"], "COMPLETE")
        self.assertIsNone(published["remote_commit"])
        self.assertEqual((self.repo / "result.json").read_text(), '{"ok":true}\n')

    def test_git_local_publication_verifies_binary_staged_bytes(self):
        config = ProjectRegistry(self.home).add(self.repo, profile="standard", review_mode="off")["project"]
        plan = build_single_task_plan(config, task_id="BIN-1", goal="write binary", allowed_paths=["blob.bin"])
        orch = Orchestrator(self.home)
        plan_path = self.home / "binary-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        orch.load_plan(plan_path)
        claim = orch.claim("fixture")
        (self.repo / "blob.bin").write_bytes(bytes([0, 255, 10, 128, 42]))
        receipt = self.home / "binary-receipt.json"
        receipt.write_text(json.dumps({"run_id": claim["run_id"], "task_id": "BIN-1", "changed_paths": ["blob.bin"]}), encoding="utf-8")
        lease = orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        self.assertEqual(orch.verify(claim["run_id"])["status"], "VERIFIED")
        self.assertEqual(orch.publish(claim["run_id"])["status"], "COMPLETE")

    def test_git_local_publication_supports_tracked_deletion(self):
        legacy=self.repo/'legacy.txt'; legacy.write_text('remove me\n',encoding='utf-8')
        subprocess.run(['git','-C',str(self.repo),'add','legacy.txt'],check=True)
        subprocess.run(['git','-C',str(self.repo),'commit','-m','add legacy'],check=True,capture_output=True)
        config=ProjectRegistry(self.home).add(self.repo,profile='standard',review_mode='off')['project']
        plan=build_single_task_plan(config,task_id='DEL-1',goal='remove legacy',allowed_paths=['legacy.txt'])
        orch=Orchestrator(self.home); plan_path=self.home/'delete-plan.json'
        plan_path.write_text(json.dumps(plan),encoding='utf-8'); orch.load_plan(plan_path)
        claim=orch.claim('fixture'); legacy.unlink()
        receipt=self.home/'delete-receipt.json'
        receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'DEL-1','changed_paths':['legacy.txt']}),encoding='utf-8')
        lease=orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        orch.submit(claim['run_id'],lease,receipt); orch.quiesce(claim['run_id'],lease)
        verified=orch.verify(claim['run_id']); self.assertEqual(verified['status'],'VERIFIED')
        published=orch.publish(claim['run_id']); self.assertEqual(published['status'],'COMPLETE')
        self.assertFalse(legacy.exists())
        probe=subprocess.run(['git','-C',str(self.repo),'show','HEAD:legacy.txt'],capture_output=True)
        self.assertNotEqual(probe.returncode,0)

    def test_verifier_binds_git_base_when_plan_base_is_dynamic(self):
        config = ProjectRegistry(self.home).add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        plan = build_single_task_plan(
            config, task_id="BASE-DYNAMIC", goal="dynamic base",
            allowed_paths=["dynamic.json"],
        )
        plan["tasks"][0]["publication"].pop("expected_base", None)
        orch = Orchestrator(self.home)
        plan_path = self.home / "dynamic-base-plan.json"
        plan_path.write_text(json.dumps(plan))
        orch.load_plan(plan_path)
        claim = orch.claim("fixture")
        base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        (self.repo / "dynamic.json").write_text('{"ok":true}\n')
        receipt = self.home / "dynamic-base-receipt.json"
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"], "task_id": "BASE-DYNAMIC",
            "changed_paths": ["dynamic.json"],
        }))
        lease = orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        verified = orch.verify(claim["run_id"])
        self.assertEqual(verified["status"], "VERIFIED")
        with orch.connect() as conn:
            snap = conn.execute(
                "SELECT manifest_json FROM snapshots WHERE run_id=?", (claim["run_id"],)
            ).fetchone()
        self.assertEqual(json.loads(snap["manifest_json"])["git_head"], base)
        published = orch.publish(claim["run_id"])
        self.assertEqual(published["status"], "COMPLETE")

    def test_verifier_blocks_head_change_from_declared_plan_base(self):
        config = ProjectRegistry(self.home).add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        plan = build_single_task_plan(
            config, task_id="BASE-DRIFT", goal="detect drift",
            allowed_paths=["result.json"],
        )
        orch = Orchestrator(self.home)
        plan_path = self.home / "base-drift-plan.json"
        plan_path.write_text(json.dumps(plan))
        orch.load_plan(plan_path)
        claim = orch.claim("fixture")
        (self.repo / "result.json").write_text('{"worker":true}\n')
        foreign = self.repo / "foreign.txt"
        foreign.write_text("foreign\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "foreign.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "foreign", "--", "foreign.txt"],
            check=True, capture_output=True,
        )
        receipt = self.home / "base-drift-receipt.json"
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"], "task_id": "BASE-DRIFT",
            "changed_paths": ["result.json"],
        }))
        lease = orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        result = orch.verify(claim["run_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "workspace_base_changed")
        self.assertNotEqual(
            result["feedback"]["evidence"]["git_head"],
            result["feedback"]["evidence"]["expected_base"],
        )

    def test_publication_ref_cas_prevents_commit_on_foreign_head(self):
        config = ProjectRegistry(self.home).add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        plan = build_single_task_plan(
            config, task_id="CAS-RACE", goal="cas race", allowed_paths=["result.json"]
        )
        orch = Orchestrator(self.home)
        plan_path = self.home / "cas-plan.json"
        plan_path.write_text(json.dumps(plan))
        orch.load_plan(plan_path)
        claim = orch.claim("fixture")
        (self.repo / "result.json").write_text('{"worker":true}\n')
        receipt = self.home / "cas-receipt.json"
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"], "task_id": "CAS-RACE",
            "changed_paths": ["result.json"],
        }))
        lease = orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        self.assertEqual(orch.verify(claim["run_id"])["status"], "VERIFIED")
        original_cas = orch._cas_update_branch
        foreign_commit = {"id": None}

        def inject_foreign_commit(workspace, *, branch, commit_id, expected_base):
            foreign = self.repo / "foreign-race.txt"
            foreign.write_text("foreign race\n")
            subprocess.run(["git", "-C", str(self.repo), "add", "foreign-race.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(self.repo), "commit", "-m", "foreign race", "--", "foreign-race.txt"],
                check=True, capture_output=True,
            )
            foreign_commit["id"] = subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            return original_cas(
                workspace, branch=branch, commit_id=commit_id, expected_base=expected_base
            )

        with mock.patch.object(
            orch, "_cas_update_branch", side_effect=inject_foreign_commit
        ):
            with self.assertRaisesRegex(ValueError, "publication_base_changed_during_commit"):
                orch.publish(claim["run_id"])
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head, foreign_commit["id"])
        show = subprocess.run(
            ["git", "-C", str(self.repo), "show", "--format=", "--name-only", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertIn("foreign-race.txt", show)
        self.assertNotIn("result.json", show)
        with orch.connect() as conn:
            journal = conn.execute(
                "SELECT status,commit_id FROM publications WHERE run_id=?",
                (claim["run_id"],),
            ).fetchone()
        self.assertEqual(journal["status"], "PREPARED")
        self.assertTrue(journal["commit_id"])
        reconciled = orch.reconcile_publication(claim["run_id"])
        self.assertEqual(reconciled["status"], "BLOCKED")
        self.assertEqual(reconciled["reason"], "publication_base_changed")

    def test_prepared_commit_recovers_after_restart_without_recommit(self):
        config = ProjectRegistry(self.home).add(
            self.repo, profile="standard", review_mode="off"
        )["project"]
        plan = build_single_task_plan(
            config, task_id="CAS-RECOVER", goal="recover prepared",
            allowed_paths=["prepared.json"],
        )
        orch = Orchestrator(self.home)
        plan_path = self.home / "prepared-plan.json"
        plan_path.write_text(json.dumps(plan))
        orch.load_plan(plan_path)
        claim = orch.claim("fixture")
        (self.repo / "prepared.json").write_text('{"prepared":true}\n')
        receipt = self.home / "prepared-receipt.json"
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"], "task_id": "CAS-RECOVER",
            "changed_paths": ["prepared.json"],
        }))
        lease = orch.lease_from_capability(claim["run_id"], Path(claim["capability_file"]))
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        self.assertEqual(orch.verify(claim["run_id"])["status"], "VERIFIED")

        with mock.patch.object(
            orch, "_cas_update_branch", side_effect=RuntimeError("synthetic crash before ref update")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                orch.publish(claim["run_id"])
        restarted = Orchestrator(self.home)
        pending = restarted.reconcile_publication(claim["run_id"])
        self.assertEqual(pending["status"], "PREPARED_PENDING_REF_UPDATE")
        commit_id = pending["commit"]
        finished = restarted.reconcile_publication(claim["run_id"], resume=True)
        self.assertEqual(finished["status"], "COMPLETE")
        self.assertEqual(finished["commit"], commit_id)
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head, commit_id)

    def test_publication_intent_without_side_effect_is_safe_to_retry(self):
        config=ProjectRegistry(self.home).add(self.repo,profile='standard',review_mode='off')['project']
        plan=build_single_task_plan(config,task_id='REC-1',goal='write result',allowed_paths=['result.json'])
        orch=Orchestrator(self.home); plan_path=self.home/'rec-plan.json'
        plan_path.write_text(json.dumps(plan),encoding='utf-8'); orch.load_plan(plan_path)
        claim=orch.claim('fixture'); (self.repo/'result.json').write_text('{"ok":1}\n')
        receipt=self.home/'rec-receipt.json'; receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'REC-1','changed_paths':['result.json']}))
        lease=orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        orch.submit(claim['run_id'],lease,receipt); orch.quiesce(claim['run_id'],lease); orch.verify(claim['run_id'])
        pub=claim['context']['publication']
        orch._publication_update(claim['run_id'],status='INTENT',operation_id='fixture-intent',kind='git_local',expected_base=pub['expected_base'])
        self.assertEqual(orch.reconcile()['status'],'ATTENTION')
        reconciled=orch.reconcile_publication(claim['run_id'])
        self.assertEqual(reconciled['status'],'SAFE_TO_RETRY')
        published=orch.publish(claim['run_id'])
        self.assertEqual(published['status'],'COMPLETE')
        self.assertEqual(orch.reconcile()['status'],'CLEAN')

    def test_publication_reconcile_resumes_exact_staged_snapshot(self):
        config=ProjectRegistry(self.home).add(self.repo,profile='standard',review_mode='off')['project']
        plan=build_single_task_plan(config,task_id='REC-2',goal='write staged',allowed_paths=['stage.json'])
        orch=Orchestrator(self.home); plan_path=self.home/'stage-plan.json'
        plan_path.write_text(json.dumps(plan),encoding='utf-8'); orch.load_plan(plan_path)
        claim=orch.claim('fixture'); (self.repo/'stage.json').write_text('{"ok":2}\n')
        receipt=self.home/'stage-receipt.json'; receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'REC-2','changed_paths':['stage.json']}))
        lease=orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        orch.submit(claim['run_id'],lease,receipt); orch.quiesce(claim['run_id'],lease); orch.verify(claim['run_id'])
        subprocess.run(['git','-C',str(self.repo),'add','--','stage.json'],check=True)
        pub=claim['context']['publication']
        orch._publication_update(claim['run_id'],status='STAGED',operation_id='fixture-staged',kind='git_local',expected_base=pub['expected_base'],staged_paths=['stage.json'])
        pending=orch.reconcile_publication(claim['run_id'])
        self.assertEqual(pending['status'],'STAGED_PENDING_COMMIT')
        finished=orch.reconcile_publication(claim['run_id'],resume=True)
        self.assertEqual(finished['status'],'COMPLETE')

    def test_publication_reconcile_adopts_commit_after_journal_gap(self):
        config=ProjectRegistry(self.home).add(self.repo,profile='standard',review_mode='off')['project']
        plan=build_single_task_plan(config,task_id='REC-3',goal='write committed',allowed_paths=['commit.json'])
        orch=Orchestrator(self.home); plan_path=self.home/'commit-plan.json'
        plan_path.write_text(json.dumps(plan),encoding='utf-8'); orch.load_plan(plan_path)
        claim=orch.claim('fixture'); (self.repo/'commit.json').write_text('{"ok":3}\n')
        receipt=self.home/'commit-receipt.json'; receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'REC-3','changed_paths':['commit.json']}))
        lease=orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        orch.submit(claim['run_id'],lease,receipt); orch.quiesce(claim['run_id'],lease); orch.verify(claim['run_id'])
        subprocess.run(['git','-C',str(self.repo),'add','--','commit.json'],check=True)
        subprocess.run(['git','-C',str(self.repo),'commit','-m','fixture uncertain commit','--','commit.json'],check=True,capture_output=True)
        pub=claim['context']['publication']
        orch._publication_update(claim['run_id'],status='STAGED',operation_id='fixture-gap',kind='git_local',expected_base=pub['expected_base'],staged_paths=['commit.json'])
        finished=orch.reconcile_publication(claim['run_id'])
        self.assertEqual(finished['status'],'COMPLETE')
        self.assertEqual(finished['commit'],subprocess.run(['git','-C',str(self.repo),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip())

    def test_verifier_blocks_unreported_git_change_outside_allowlist(self):
        config=ProjectRegistry(self.home).add(self.repo,profile='standard',review_mode='off')['project']
        plan=build_single_task_plan(config,task_id='SCOPE-1',goal='bounded write',allowed_paths=['result.json'])
        orch=Orchestrator(self.home); plan_path=self.home/'scope-plan.json'
        plan_path.write_text(json.dumps(plan),encoding='utf-8'); orch.load_plan(plan_path)
        claim=orch.claim('fixture')
        (self.repo/'result.json').write_text('{"ok":true}\n'); (self.repo/'rogue.txt').write_text('rogue\n')
        receipt=self.home/'scope-receipt.json'; receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'SCOPE-1','changed_paths':['result.json']}))
        lease=orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        orch.submit(claim['run_id'],lease,receipt); orch.quiesce(claim['run_id'],lease)
        result=orch.verify(claim['run_id'])
        self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(result['reason'],'workspace_scope_violation:rogue.txt')
        self.assertIn('rogue.txt',result['feedback']['evidence']['outside_allowlist'])

    def test_verifier_blocks_receipt_path_with_no_observed_git_change(self):
        config=ProjectRegistry(self.home).add(self.repo,profile='standard',review_mode='off')['project']
        plan=build_single_task_plan(config,task_id='SCOPE-2',goal='must really change',allowed_paths=['README.md'])
        orch=Orchestrator(self.home); plan_path=self.home/'phantom-plan.json'
        plan_path.write_text(json.dumps(plan),encoding='utf-8'); orch.load_plan(plan_path)
        claim=orch.claim('fixture')
        receipt=self.home/'phantom-receipt.json'; receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'SCOPE-2','changed_paths':['README.md']}))
        lease=orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        orch.submit(claim['run_id'],lease,receipt); orch.quiesce(claim['run_id'],lease)
        result=orch.verify(claim['run_id'])
        self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(result['reason'],'receipt_scope_mismatch')
        self.assertEqual(result['feedback']['evidence']['declared_but_unobserved'],['README.md'])

    def test_verifier_subtracts_unchanged_protected_preexisting_dirty_path(self):
        owner=self.repo/'owner-note.txt'; owner.write_text('owner dirty\n',encoding='utf-8')
        config=ProjectRegistry(self.home).add(self.repo,profile='standard',review_mode='off')['project']
        plan=build_single_task_plan(config,task_id='SCOPE-3',goal='safe write',allowed_paths=['result.json'])
        orch=Orchestrator(self.home); plan_path=self.home/'protected-scope-plan.json'
        plan_path.write_text(json.dumps(plan),encoding='utf-8'); orch.load_plan(plan_path)
        claim=orch.claim('fixture'); (self.repo/'result.json').write_text('{"safe":true}\n')
        receipt=self.home/'protected-scope-receipt.json'; receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'SCOPE-3','changed_paths':['result.json']}))
        lease=orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        orch.submit(claim['run_id'],lease,receipt); orch.quiesce(claim['run_id'],lease)
        result=orch.verify(claim['run_id'])
        self.assertEqual(result['status'],'VERIFIED')
        self.assertEqual(result['scope_evidence']['observed_paths'],['result.json'])
        self.assertEqual(result['scope_evidence']['protected_preexisting_paths'],['owner-note.txt'])

    def test_dispatcher_requires_publication_reconciliation_before_retry(self):
        with mock.patch.dict(os.environ, {'ORCH_EXECUTABLE':'/tmp/orch'}, clear=False):
            rendered=render_dispatcher(self.home)
        text=Path(rendered['path']).read_text(encoding='utf-8')
        self.assertIn('publish-reconcile --run-id <run_id>',text)
        self.assertIn('DO NOT blindly call publish again',text)
        self.assertIn('COMMIT_PROVEN_REMOTE_PENDING',text)
        self.assertIn('PREPARED_PENDING_REF_UPDATE',text)

    def test_doctor_without_rdc_marker_is_attention_not_hard_block(self):
        result = run_doctor(self.home, check_codex=False)
        self.assertIn(result["status"], {"ATTENTION", "READY"})
        rdc = next(item for item in result["checks"] if item["id"] == "rdc_chat_bridge")
        self.assertEqual(rdc["status"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
