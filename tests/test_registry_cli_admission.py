import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import unittest

from orch.cli import main as cli_main


class RegistryCliAdmissionTests(unittest.TestCase):
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

    def make_repo(self, name):
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
             "user.name", "Registry CLI Fixture"],
            check=True,
        )
        (repo / "README.md").write_text("registry cli fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "README.md"], check=True,
        )
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

    def register(self, home, repo, *, name=None):
        args = [
            "--root", str(home),
            "project", "add", str(repo),
            "--profile", "standard",
            "--review-mode", "off",
        ]
        if name is not None:
            args.extend(["--name", name])
        rc, result = self.invoke(*args)
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "REGISTERED")
        return result["project"], Path(result["config_path"])

    def task_ids(self, home):
        db_path = home / ".runtime" / "orch.sqlite3"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            return [
                row[0]
                for row in db.execute("SELECT task_id FROM tasks ORDER BY task_id")
            ]

    def plan_snapshot(self, home):
        plans = home / "plans"
        if not plans.exists():
            return {}
        return {
            path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in sorted(plans.iterdir())
        }

    def make_two_project_fixture(self, label):
        home = self.base / f"{label}-home"
        repo_a = self.make_repo(f"{label}-repo-a")
        repo_b = self.make_repo(f"{label}-repo-b")
        config_a, _ = self.register(home, repo_a)
        config_b, path_b = self.register(home, repo_b)
        return home, repo_a, repo_b, config_a, config_b, path_b

    def corrupt_foreign(self, case, repo_a, config_b, path_b):
        if case == "mode_0644":
            os.chmod(path_b, 0o644)
            return

        data = json.loads(path_b.read_text(encoding="utf-8"))
        if case == "identity_mismatch":
            data["project_id"] = "wrong-project-id"
        elif case == "relative_root":
            data["root"] = "relative-repo-b"
        elif case == "root_digest_mismatch":
            repo_c = self.make_repo(f"{case}-repo-c")
            self.assertNotEqual(repo_c.resolve(), repo_a.resolve())
            new_digest = hashlib.sha256(
                str(repo_c.resolve()).encode("utf-8")
            ).hexdigest()[:8]
            self.assertFalse(config_b["project_id"].endswith("-" + new_digest))
            data["root"] = str(repo_c.resolve())
        elif case == "schema_unknown":
            data["schema_version"] = 2
        elif case == "schema_bool":
            data["schema_version"] = True
        else:
            self.fail(f"unknown corruption case: {case}")

        path_b.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path_b, 0o600)

    def assert_blocked_operations_preserve_state(
        self, label, home, project_a, path_b
    ):
        before_tasks = self.task_ids(home)
        before_plans = self.plan_snapshot(home)
        before_bytes = path_b.read_bytes()
        before_mode = path_b.stat().st_mode & 0o777

        manifest = self.base / f"{label}-manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": f"{label}-batch-v1",
            "tasks": [{
                "id": f"{label.upper()}-BATCH",
                "goal": "must be blocked by foreign registry admission",
                "allowed_paths": ["result.json"],
            }],
        }) + "\n", encoding="utf-8")
        output = self.base / f"{label}-make-plan.json"

        operations = [
            (
                "enqueue",
                (
                    "--root", str(home),
                    "queue", "enqueue", project_a["project_id"],
                    "--task-id", f"{label.upper()}-QUEUE",
                    "--goal", "must be blocked by foreign registry admission",
                    "--allowed-path", "result.json",
                ),
            ),
            (
                "enqueue-batch",
                (
                    "--root", str(home),
                    "queue", "enqueue-batch", project_a["project_id"],
                    str(manifest),
                ),
            ),
            (
                "make-plan",
                (
                    "--root", str(home),
                    "project", "make-plan", project_a["project_id"],
                    "--task-id", f"{label.upper()}-PLAN",
                    "--goal", "must be blocked by foreign registry admission",
                    "--allowed-path", "result.json",
                    "--output", str(output),
                ),
            ),
        ]

        for operation, args in operations:
            with self.subTest(case=label, operation=operation):
                rc, result = self.invoke(*args)
                self.assertEqual(rc, 0)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["reason"], "project_readiness_blocked")
                self.assertEqual(result["project_id"], project_a["project_id"])
                self.assertEqual(self.task_ids(home), before_tasks)
                self.assertEqual(self.plan_snapshot(home), before_plans)
                self.assertEqual(path_b.read_bytes(), before_bytes)
                self.assertEqual(path_b.stat().st_mode & 0o777, before_mode)
                self.assertFalse(output.exists())

    def test_foreign_registry_corruption_blocks_public_cli_admission(self):
        cases = (
            "identity_mismatch",
            "relative_root",
            "root_digest_mismatch",
            "schema_unknown",
            "schema_bool",
            "mode_0644",
        )
        for case in cases:
            with self.subTest(case=case):
                home, repo_a, _, config_a, config_b, path_b = (
                    self.make_two_project_fixture(case)
                )
                self.corrupt_foreign(case, repo_a, config_b, path_b)
                self.assert_blocked_operations_preserve_state(
                    case, home, config_a, path_b
                )

    def test_valid_foreign_registry_allows_public_cli_admission(self):
        home, _, _, config_a, _, _ = self.make_two_project_fixture("positive")
        before = self.task_ids(home)

        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config_a["project_id"],
            "--task-id", "VALID-ADMISSION",
            "--goal", "valid registry should admit",
            "--allowed-path", "result.json",
        )

        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "ENQUEUED")
        self.assertNotEqual(result["audit_status"], "BLOCKED")
        self.assertEqual(result["load"]["status"], "OK")
        self.assertEqual(result["load"]["queued_count"], 1)
        self.assertEqual(self.task_ids(home), before + ["VALID-ADMISSION"])
        self.assertTrue(Path(result["plan_path"]).is_file())

    def test_maximum_valid_ids_generate_bounded_default_revision_and_load(self):
        home = self.base / "maximum-id-home"
        repo = self.make_repo("maximum-id-repo")
        project, _ = self.register(home, repo, name="p" * 151)
        task_id = "T" * 160

        self.assertEqual(len(project["project_id"]), 160)
        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue", project["project_id"],
            "--task-id", task_id,
            "--goal", "maximum valid IDs must load",
            "--allowed-path", "result.json",
        )

        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "ENQUEUED")
        revision = result["plan_revision"]
        self.assertLessEqual(len(revision), 200)
        self.assertIsNotNone(re.fullmatch(r"[A-Za-z0-9._-]{1,200}", revision))
        self.assertEqual(result["load"]["status"], "OK")
        self.assertEqual(result["load"]["queued_count"], 1)
        self.assertEqual(result["load"]["plan_revision"], revision)
        self.assertEqual(self.task_ids(home), [task_id])
        plan = json.loads(Path(result["plan_path"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_revision"], revision)
        self.assertEqual(plan["tasks"][0]["id"], task_id)


if __name__ == "__main__":
    unittest.main()
