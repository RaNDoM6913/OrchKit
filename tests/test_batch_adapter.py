import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from orch.cli import main as cli_main
from orch.core import Orchestrator
from orch.plan import BATCH_MANIFEST_MAX_BYTES, PLAN_MAX_TASKS


class BatchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(self.repo)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config",
             "user.email", "fixture@example.invalid"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config",
             "user.name", "Batch Fixture"], check=True,
        )
        (self.repo / "README.md").write_text("batch fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "initial"],
            check=True, capture_output=True,
        )
        rc, registered = self.invoke(
            "--root", str(self.home),
            "project", "add", str(self.repo),
            "--profile", "safe", "--review-mode", "off",
        )
        self.assertEqual(rc, 0)
        self.project_id = registered["project"]["project_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(list(args))
        return rc, json.loads(output.getvalue())

    def write_manifest(self, tasks, *, revision=None, name="batch.json"):
        data = {"schema_version": 1, "tasks": tasks}
        if revision is not None:
            data["plan_revision"] = revision
        path = self.base / name
        raw = json.dumps(data, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_batch_enqueue_loads_dag_atomically_with_provenance(self):
        manifest, digest = self.write_manifest([
            {
                "id": "BATCH-1", "goal": "first",
                "allowed_paths": ["one.json"],
            },
            {
                "id": "BATCH-2", "goal": "second",
                "allowed_paths": ["two.json"],
                "dependencies": ["BATCH-1"],
                "risk_tags": ["architecture"],
            },
        ], revision="fixture-batch-v1")
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "ENQUEUED")
        self.assertEqual(result["task_ids"], ["BATCH-1", "BATCH-2"])
        self.assertEqual(result["task_count"], 2)
        self.assertEqual(result["source_sha256"], digest)
        self.assertEqual(result["source_bytes"], manifest.stat().st_size)
        self.assertEqual(result["load"]["queued_count"], 2)
        plan = json.loads(Path(result["plan_path"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["adapter"]["kind"], "batch_manifest_v1")
        self.assertEqual(plan["adapter"]["source_sha256"], digest)
        self.assertEqual(plan["adapter"]["source_bytes"], manifest.stat().st_size)
        self.assertEqual(plan["tasks"][1]["dependencies"], ["BATCH-1"])
        queue = Orchestrator(self.home).queue_view(project_id=self.project_id)
        by_id = {item["task_id"]: item for item in queue["tasks"]}
        self.assertEqual(by_id["BATCH-1"]["queue_state"], "READY")
        self.assertEqual(
            by_id["BATCH-2"]["queue_state"], "WAITING_DEPENDENCY"
        )

    def test_repeated_batch_revision_is_idempotent(self):
        manifest, _ = self.write_manifest([
            {
                "id": "REPEAT-1", "goal": "repeat",
                "allowed_paths": ["repeat.json"],
            }
        ], revision="repeat-batch-v1")
        rc1, first = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc1, 0)
        self.assertEqual(first["plan_artifact_status"], "CREATED")
        self.assertEqual(first["load"]["queued_count"], 1)

        rc2, second = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc2, 0)
        self.assertEqual(second["plan_artifact_status"], "EXISTS")
        self.assertEqual(second["load"]["queued_count"], 0)
        queue = Orchestrator(self.home).queue_view(project_id=self.project_id)
        self.assertEqual(queue["summary"]["total"], 1)

    def test_unknown_dependency_leaves_no_partial_tasks_or_plan(self):
        manifest, _ = self.write_manifest([
            {
                "id": "BAD-DEP", "goal": "bad dependency",
                "allowed_paths": ["bad.json"],
                "dependencies": ["DOES-NOT-EXIST"],
            }
        ], revision="bad-dep-v1")
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "invalid_dependency:DOES-NOT-EXIST")
        self.assertEqual(Orchestrator(self.home).status()["tasks"], [])
        plan = self.home / "plans" / "bad-dep-v1.json"
        self.assertFalse(plan.exists())
    def test_dependency_cycle_leaves_no_partial_tasks_or_plan(self):
        manifest, _ = self.write_manifest([
            {
                "id": "CYCLE-A", "goal": "a",
                "allowed_paths": ["a.json"],
                "dependencies": ["CYCLE-B"],
            },
            {
                "id": "CYCLE-B", "goal": "b",
                "allowed_paths": ["b.json"],
                "dependencies": ["CYCLE-A"],
            },
        ], revision="cycle-v1")
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "dependency_cycle")
        self.assertEqual(Orchestrator(self.home).status()["tasks"], [])
        self.assertFalse((self.home / "plans" / "cycle-v1.json").exists())

    def test_duplicate_task_id_is_rejected_before_plan_write(self):
        manifest, _ = self.write_manifest([
            {"id": "DUP-1", "goal": "one", "allowed_paths": ["one.json"]},
            {"id": "DUP-1", "goal": "two", "allowed_paths": ["two.json"]},
        ])
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "duplicate_batch_task_id:DUP-1")
        self.assertEqual(Orchestrator(self.home).status()["tasks"], [])

    def test_symlink_manifest_is_refused(self):
        real, _ = self.write_manifest([
            {"id": "LINK-1", "goal": "link", "allowed_paths": ["link.json"]}
        ], name="real.json")
        link = self.base / "manifest-link.json"
        link.symlink_to(real)
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(link),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "batch_manifest_not_regular")
        self.assertEqual(Orchestrator(self.home).status()["tasks"], [])

    def test_oversized_manifest_is_refused_before_json_parse(self):
        manifest = self.base / "oversized.json"
        manifest.write_bytes(b"{" + b"x" * BATCH_MANIFEST_MAX_BYTES)
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "batch_manifest_too_large")
        self.assertEqual(Orchestrator(self.home).status()["tasks"], [])

    def test_unknown_manifest_and_task_fields_fail_closed(self):
        manifest = self.base / "unknown-top.json"
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "tasks": [{
                "id": "UNKNOWN-TOP", "goal": "top",
                "allowed_paths": ["top.json"],
            }],
            "surprise": True,
        }), encoding="utf-8")
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "unknown_batch_manifest_field:surprise")

        manifest, _ = self.write_manifest([{
            "id": "UNKNOWN-TASK", "goal": "task",
            "allowed_paths": ["task.json"], "surprise": True,
        }], revision="unknown-task-v1", name="unknown-task.json")
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "unknown_batch_task_field:surprise")
        self.assertEqual(Orchestrator(self.home).status()["tasks"], [])
        self.assertFalse((self.home / "plans" / "unknown-task-v1.json").exists())

    def test_batch_task_count_is_bounded_before_plan_write(self):
        tasks = [
            {
                "id": f"MANY-{index:03d}", "goal": "bounded",
                "allowed_paths": [f"result-{index:03d}.json"],
            }
            for index in range(PLAN_MAX_TASKS + 1)
        ]
        manifest, _ = self.write_manifest(
            tasks, revision="too-many-batch-v1", name="too-many.json"
        )
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "batch_manifest_too_many_tasks")
        self.assertEqual(Orchestrator(self.home).status()["tasks"], [])
        self.assertFalse((self.home / "plans" / "too-many-batch-v1.json").exists())

    def test_generated_tasks_are_bounded_before_plan_write(self):
        manifest, _ = self.write_manifest([{
            "id": "GOAL-LARGE", "goal": "x" * (16 * 1024 + 1),
            "allowed_paths": ["large.json"],
        }], revision="goal-large-batch-v1", name="goal-large.json")
        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue-batch", self.project_id, str(manifest),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "invalid_goal_too_large")
        self.assertFalse((self.home / "plans" / "goal-large-batch-v1.json").exists())

        rc, result = self.invoke(
            "--root", str(self.home),
            "queue", "enqueue", self.project_id,
            "--task-id", "SINGLE-GOAL-LARGE",
            "--goal", "x" * (16 * 1024 + 1),
            "--allowed-path", "single-large.json",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "invalid_goal_too_large")
        plans = list((self.home / "plans").glob("*.json"))
        self.assertEqual(plans, [])


if __name__ == "__main__":
    unittest.main()
