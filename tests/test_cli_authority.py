import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from orch.cli import main as cli_main
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
