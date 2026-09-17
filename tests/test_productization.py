import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from orch.config import configure_home
from orch.doctor import run_doctor
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

    def test_doctor_without_rdc_marker_is_attention_not_hard_block(self):
        result = run_doctor(self.home, check_codex=False)
        self.assertIn(result["status"], {"ATTENTION", "READY"})
        rdc = next(item for item in result["checks"] if item["id"] == "rdc_chat_bridge")
        self.assertEqual(rdc["status"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
