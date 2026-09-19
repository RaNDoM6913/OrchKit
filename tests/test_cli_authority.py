import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from orch.cli import main as cli_main
from orch.core import Orchestrator
from orch.project import ProjectRegistry


class CliLedgerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(list(args))
        return rc, json.loads(output.getvalue())

    def make_repo(self, name="repo"):
        repo = self.base / name
        remote = self.base / f"{name}-remote.git"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(repo)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config",
             "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config",
             "user.name", "CLI Authority Fixture"],
            check=True,
        )
        (repo / "README.md").write_text("authority fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "initial"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
            check=True,
        )
        return repo

    def register_via_cli(self, home, repo):
        rc, result = self.invoke(
            "--root", str(home),
            "project", "add", str(repo),
            "--profile", "standard",
            "--review-mode", "off",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "REGISTERED")
        return result["project"]

    def test_claim_missing_ledger_fails_without_creating_home(self):
        home = self.base / "missing-claim-home"
        rc, result = self.invoke(
            "--root", str(home), "claim", "--worker", "fixture"
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertFalse(home.exists())

    def test_state_check_missing_ledger_fails_without_initializing(self):
        home = self.base / "missing-state-home"
        rc, result = self.invoke("--root", str(home), "state", "check")
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertFalse(home.exists())

    def test_init_refuses_existing_registry_without_ledger(self):
        home = self.base / "registry-init-refusal"
        repo = self.make_repo("registry-init-refusal-repo")
        ProjectRegistry(home).add(
            repo, profile="standard", review_mode="off"
        )
        self.assertFalse((home / ".runtime").exists())

        rc, result = self.invoke("--root", str(home), "init")
        self.assertEqual(rc, 1)
        self.assertEqual(
            result["error"], "registered_project_authority_without_ledger"
        )
        self.assertFalse((home / ".runtime").exists())

    def test_project_add_refuses_registry_authority_without_ledger(self):
        home = self.base / "registry-add-refusal"
        repo_one = self.make_repo("registry-add-existing")
        repo_two = self.make_repo("registry-add-new")
        existing = ProjectRegistry(home).add(
            repo_one, profile="standard", review_mode="off"
        )["project"]
        self.assertFalse((home / ".runtime").exists())

        rc, result = self.invoke(
            "--root", str(home),
            "project", "add", str(repo_two),
            "--profile", "standard",
            "--review-mode", "off",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(
            result["error"], "registered_project_authority_without_ledger"
        )
        self.assertFalse((home / ".runtime").exists())
        projects = ProjectRegistry(home).list()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project_id"], existing["project_id"])

    def test_queue_enqueue_registry_without_ledger_does_not_create_runtime(self):
        home = self.base / "registry-only-home"
        repo = self.make_repo("registry-only-repo")
        config = ProjectRegistry(home).add(
            repo, profile="standard", review_mode="off"
        )["project"]
        self.assertFalse((home / ".runtime").exists())
        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "NO-LEDGER-1",
            "--goal", "must not create ledger",
            "--allowed-path", "result.json",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertFalse((home / ".runtime").exists())
        self.assertFalse((home / "plans").exists())

    def test_explicit_init_creates_ledger_and_operational_status_works(self):
        home = self.base / "explicit-init-home"
        rc, initialized = self.invoke("--root", str(home), "init")
        self.assertEqual(rc, 0)
        self.assertEqual(initialized["status"], "OK")
        self.assertTrue((home / ".runtime" / "orch.sqlite3").is_file())

        rc, status = self.invoke("--root", str(home), "status")
        self.assertEqual(rc, 0)
        self.assertEqual(status["variant"], "B_NEW_CHAT_PER_ATTEMPT")

    def test_state_backup_cli_preserves_symlink_boundary(self):
        home = self.base / "backup-symlink-home"
        Orchestrator(home)
        victim = self.base / "backup-cli-victim.zip"
        victim.write_bytes(b"owner preserve")
        link = self.base / "backup-cli-link.zip"
        link.symlink_to(victim)

        rc, result = self.invoke(
            "--root", str(home),
            "state", "backup",
            "--output", str(link),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "backup_output_unsafe")
        self.assertEqual(victim.read_bytes(), b"owner preserve")
        self.assertTrue(link.is_symlink())

    def test_load_plan_cli_preserves_no_follow_symlink_boundary(self):
        home = self.base / "load-plan-symlink-home"
        workspace = self.base / "load-plan-workspace"
        workspace.mkdir()
        Orchestrator(home)
        target = self.base / "load-plan-real.json"
        target.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "cli-symlink-v1",
            "tasks": [{
                "id": "CLI-SYMLINK",
                "goal": "must not follow cli symlink",
                "workspace": str(workspace),
                "dependencies": [],
                "allowed_paths": ["result.json"],
                "protected_paths": {},
                "checks": [],
                "required_review": False,
                "owner_acceptance": False,
                "publication": {"kind": "none"},
                "max_attempts": 2,
            }],
        }) + "\n", encoding="utf-8")
        link = self.base / "load-plan-link.json"
        link.symlink_to(target)

        rc, result = self.invoke(
            "--root", str(home), "load-plan", str(link)
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "plan_file_missing_or_unsafe")
        self.assertEqual(Orchestrator(home).status()["tasks"], [])
        self.assertTrue(link.is_symlink())

    def test_symlinked_ledger_is_refused_without_touching_target(self):
        home = self.base / "symlink-home"
        runtime = home / ".runtime"
        runtime.mkdir(parents=True)
        external = self.base / "external.sqlite3"
        db = sqlite3.connect(str(external))
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel(value) VALUES('keep')")
        db.commit()
        db.close()
        before = external.read_bytes()
        (runtime / "orch.sqlite3").symlink_to(external)

        rc, result = self.invoke("--root", str(home), "state", "check")
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertEqual(external.read_bytes(), before)
        self.assertTrue((runtime / "orch.sqlite3").is_symlink())

    def test_queue_enqueue_blocks_foreign_workspace_before_plan_write(self):
        home = self.base / "foreign-enqueue-home"
        repo = self.make_repo("foreign-enqueue-repo")
        config = self.register_via_cli(home, repo)
        (repo / "foreign.txt").write_text("owner concurrent bytes\n", encoding="utf-8")

        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "FOREIGN-QUEUE",
            "--goal", "must be blocked by readiness",
            "--allowed-path", "result.json",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "project_readiness_blocked")
        self.assertIn(
            "workspace_baseline",
            " ".join(result["audit"]["blockers"]),
        )
        plans = home / "plans"
        self.assertFalse(plans.exists() and any(plans.iterdir()))

    def _finish_git_task(self, orch, repo, claim, task_id, relative):
        (repo / relative).write_text(
            json.dumps({"task": task_id}) + "\n", encoding="utf-8"
        )
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"],
            "task_id": task_id,
            "changed_paths": [relative],
        }) + "\n", encoding="utf-8")
        lease = orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        verified = orch.verify(claim["run_id"])
        self.assertEqual(verified["status"], "VERIFIED")
        return orch.publish(claim["run_id"])

    def test_independent_prequeued_git_tasks_bind_base_at_execution_boundary(self):
        home = self.base / "dynamic-base-home"
        repo = self.make_repo("dynamic-base-repo")
        config = self.register_via_cli(home, repo)

        rc, first = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "QUEUE-BASE-1",
            "--goal", "first publication",
            "--allowed-path", "one.json",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(first["base_binding"], "admission_head")

        rc, second = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "QUEUE-BASE-2",
            "--goal", "second publication",
            "--allowed-path", "two.json",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(second["base_binding"], "dynamic_at_verify")
        second_plan = json.loads(
            Path(second["plan_path"]).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "expected_base", second_plan["tasks"][0]["publication"]
        )

        orch = Orchestrator(home)
        claim_one = orch.claim("worker-one", project_id=config["project_id"])
        published_one = self._finish_git_task(
            orch, repo, claim_one, "QUEUE-BASE-1", "one.json"
        )
        self.assertEqual(published_one["status"], "COMPLETE")

        claim_two = orch.claim("worker-two", project_id=config["project_id"])
        published_two = self._finish_git_task(
            orch, repo, claim_two, "QUEUE-BASE-2", "two.json"
        )
        self.assertEqual(published_two["status"], "COMPLETE")
        self.assertNotEqual(published_one["commit"], published_two["commit"])
        remote = self.base / "dynamic-base-repo-remote.git"
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, published_two["commit"])

    def test_batch_independent_git_tasks_only_bind_first_admission_base(self):
        home = self.base / "batch-base-home"
        repo = self.make_repo("batch-base-repo")
        config = self.register_via_cli(home, repo)
        manifest = self.base / "batch-base.json"
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "batch-base-v1",
            "tasks": [
                {
                    "id": "BATCH-BASE-1",
                    "goal": "first",
                    "allowed_paths": ["first.json"],
                },
                {
                    "id": "BATCH-BASE-2",
                    "goal": "second",
                    "allowed_paths": ["second.json"],
                },
            ],
        }) + "\n", encoding="utf-8")
        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue-batch", config["project_id"], str(manifest),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["base_binding"], "first_task_admission_head")
        plan = json.loads(Path(result["plan_path"]).read_text(encoding="utf-8"))
        self.assertIn("expected_base", plan["tasks"][0]["publication"])
        self.assertNotIn("expected_base", plan["tasks"][1]["publication"])

    def test_project_make_plan_blocks_foreign_workspace_before_output(self):
        home = self.base / "foreign-plan-home"
        repo = self.make_repo("foreign-plan-repo")
        config = self.register_via_cli(home, repo)
        (repo / "rogue.txt").write_text("rogue\n", encoding="utf-8")
        output = self.base / "blocked-plan.json"

        rc, result = self.invoke(
            "--root", str(home),
            "project", "make-plan", config["project_id"],
            "--task-id", "FOREIGN-PLAN",
            "--goal", "must be blocked",
            "--allowed-path", "result.json",
            "--output", str(output),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "project_readiness_blocked")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
