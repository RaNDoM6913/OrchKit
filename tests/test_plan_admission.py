import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

from orch.config import atomic_write_json
from orch.core import PLAN_MAX_BYTES, Orchestrator, sha256_file
from orch.plan import existing_plan_initial_base_binding, write_plan


class PlanAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.workspace = self.base / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        self.orch = Orchestrator(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, task_id="PLAN-1"):
        return {
            "id": task_id,
            "goal": "bounded plan fixture",
            "workspace": str(self.workspace),
            "dependencies": [],
            "allowed_paths": ["result.json"],
            "protected_paths": {},
            "checks": [],
            "required_review": False,
            "owner_acceptance": False,
            "publication": {"kind": "none"},
            "max_attempts": 2,
        }

    def execution_budget(self):
        return {
            "schema_version": 1,
            "enforcement": "advisory",
            "estimated_work_minutes": {"min": 15, "max": 30},
            "context_budget_tokens": None,
            "capacity_source": "UNKNOWN",
            "usage_source": "UNKNOWN",
            "observed_at": "UNKNOWN",
            "checkpoint_action": "Persist a checkpoint before unsafe handoff.",
        }

    def write_plan(self, data, name="plan.json"):
        path = self.base / name
        raw = (json.dumps(data, sort_keys=True) + "\n").encode("utf-8")
        path.write_bytes(raw)
        return path, raw

    def plan(self, tasks=None, revision="bounded-plan-v1"):
        return {
            "schema_version": 1,
            "plan_revision": revision,
            "adapter": {"kind": "fixture"},
            "tasks": tasks or [self.task()],
        }

    def assert_no_tasks(self):
        self.assertEqual(self.orch.status()["tasks"], [])

    def test_valid_plan_digest_and_bytes_bind_parsed_source(self):
        path, raw = self.write_plan(self.plan())
        loaded = self.orch.load_plan(path)
        self.assertEqual(loaded["status"], "OK")
        self.assertEqual(loaded["task_count"], 1)
        self.assertEqual(loaded["queued_count"], 1)
        self.assertEqual(loaded["plan_bytes"], len(raw))
        self.assertEqual(loaded["digest"], hashlib.sha256(raw).hexdigest())

    def test_execution_budget_is_versioned_advisory_and_persisted(self):
        task = self.task("BUDGET-VALID")
        budget = self.execution_budget()
        task["execution_budget"] = budget
        path, _ = self.write_plan(
            self.plan(tasks=[task], revision="budget-valid"),
            name="budget-valid.json",
        )

        loaded = self.orch.load_plan(path)

        self.assertEqual(loaded["status"], "OK")
        with self.orch.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM tasks WHERE task_id=?",
                ("BUDGET-VALID",),
            ).fetchone()
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["execution_budget"], budget)

    def test_execution_budget_rejects_invalid_contract_without_admission(self):
        cases = []

        value = self.execution_budget()
        value["schema_version"] = 2
        cases.append(("schema", value, "invalid_execution_budget_schema"))

        value = self.execution_budget()
        value["schema_version"] = True
        cases.append((
            "bool-schema", value, "invalid_execution_budget_schema"
        ))

        value = self.execution_budget()
        value["enforcement"] = "automatic"
        cases.append((
            "enforcement", value, "invalid_execution_budget_enforcement"
        ))

        value = self.execution_budget()
        value["surprise"] = True
        cases.append((
            "unknown", value, "unknown_execution_budget_field:surprise"
        ))

        value = self.execution_budget()
        value.pop("capacity_source")
        cases.append((
            "missing-capacity-source", value,
            "missing_execution_budget_field:capacity_source",
        ))

        value = self.execution_budget()
        value.pop("usage_source")
        cases.append((
            "missing-usage-source", value,
            "missing_execution_budget_field:usage_source",
        ))

        value = self.execution_budget()
        value.pop("observed_at")
        cases.append((
            "missing-observed-at", value,
            "missing_execution_budget_field:observed_at",
        ))

        value = self.execution_budget()
        value["observed_at"] = "2026-09-26T12:00:00"
        cases.append((
            "naive-observed-at", value, "invalid_observed_at"
        ))

        value = self.execution_budget()
        value["observed_at"] = "not-a-timestamp"
        cases.append((
            "invalid-observed-at", value, "invalid_observed_at"
        ))

        value = self.execution_budget()
        value["estimated_work_minutes"]["min"] = -1
        cases.append((
            "negative-minutes", value, "invalid_estimated_work_minutes"
        ))

        value = self.execution_budget()
        value["estimated_work_minutes"]["max"] = float("inf")
        cases.append((
            "nonfinite-minutes", value, "invalid_estimated_work_minutes"
        ))

        value = self.execution_budget()
        value["estimated_work_minutes"] = {"min": 30, "max": 15}
        cases.append((
            "reversed-minutes", value, "invalid_estimated_work_minutes"
        ))

        value = self.execution_budget()
        value["context_budget_tokens"] = -1
        cases.append((
            "negative-context", value, "invalid_context_budget_tokens"
        ))

        value = self.execution_budget()
        value["context_budget_tokens"] = 1.5
        cases.append((
            "fractional-context", value, "invalid_context_budget_tokens"
        ))

        value = self.execution_budget()
        value["checkpoint_action"] = "x" * 4097
        cases.append((
            "oversized-action", value, "invalid_checkpoint_action_too_large"
        ))

        for index, (name, budget, error) in enumerate(cases):
            with self.subTest(name=name):
                task = self.task(f"BUDGET-BAD-{index}")
                task["execution_budget"] = budget
                path, _ = self.write_plan(
                    self.plan(
                        tasks=[task],
                        revision=f"budget-bad-{index}",
                    ),
                    name=f"budget-bad-{index}.json",
                )
                with self.assertRaisesRegex(ValueError, error):
                    self.orch.load_plan(path)
                self.assert_no_tasks()

    def test_legacy_task_without_execution_budget_remains_admitted(self):
        path, _ = self.write_plan(
            self.plan(
                tasks=[self.task("BUDGET-LEGACY")],
                revision="budget-legacy",
            ),
            name="budget-legacy.json",
        )

        loaded = self.orch.load_plan(path)

        self.assertEqual(loaded["status"], "OK")
        self.assertEqual(loaded["queued_count"], 1)

    def test_review_policy_rejects_string_values_before_admission(self):
        malformed = (
            ("risk_tags", "security"),
            ("trigger_tags", "security"),
            ("sensitive_patterns", "security/**"),
            ("review_on_retry", ""),
        )
        for index, (field, value) in enumerate(malformed):
            with self.subTest(field=field):
                task = self.task(f"BAD-REVIEW-{index}")
                task["review"] = {
                    "mode": "risk_based",
                    "reviewer": "codex",
                    field: value,
                }
                path, _ = self.write_plan(
                    self.plan(
                        tasks=[task], revision=f"bad-review-{index}"
                    ),
                    name=f"bad-review-{index}.json",
                )
                with self.assertRaisesRegex(
                    ValueError, "invalid_review_" + field
                ):
                    self.orch.load_plan(path)
                self.assert_no_tasks()

    def test_symlink_plan_is_rejected_without_state(self):
        target, _ = self.write_plan(self.plan(), name="target.json")
        link = self.base / "plan-link.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "plan_file_missing_or_unsafe"):
            self.orch.load_plan(link)
        self.assert_no_tasks()

    def test_plan_and_hash_fifo_fail_closed_without_blocking(self):
        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("FIFO nonblocking open unavailable")
        fifo = self.base / "authority.fifo"
        os.mkfifo(fifo)

        def timeout_handler(_signum, _frame):
            raise TimeoutError("fifo_open_blocked")

        previous = signal.signal(signal.SIGALRM, timeout_handler)
        try:
            signal.alarm(2)
            with self.assertRaisesRegex(
                ValueError, "plan_file_missing_or_unsafe"
            ):
                self.orch.load_plan(fifo)
            signal.alarm(0)

            signal.alarm(2)
            with self.assertRaisesRegex(ValueError, "hash_file_unsafe"):
                sha256_file(fifo)
            signal.alarm(0)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assert_no_tasks()

    def test_plan_artifact_symlink_is_never_followed_or_replaced(self):
        victim = self.base / "victim.json"
        victim.write_text('{"owner":"preserve"}\n', encoding="utf-8")
        link = self.base / "artifact-link.json"
        link.symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "plan_artifact_not_regular"):
            write_plan(link, self.plan(), replace=True)
        with self.assertRaisesRegex(ValueError, "plan_artifact_not_regular"):
            existing_plan_initial_base_binding(link)
        self.assertEqual(
            victim.read_text(encoding="utf-8"), '{"owner":"preserve"}\n'
        )
        self.assertTrue(link.is_symlink())

    def test_sha256_file_is_no_follow_and_detects_identity_drift(self):
        target = self.base / "hash-target.bin"
        raw = b"bound-bytes\n"
        target.write_bytes(raw)
        self.assertEqual(sha256_file(target), hashlib.sha256(raw).hexdigest())

        link = self.base / "hash-link.bin"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "hash_file_unsafe"):
            sha256_file(link)

        real_fstat = os.fstat
        calls = 0

        def changed_fstat(fd):
            nonlocal calls
            info = real_fstat(fd)
            calls += 1
            if calls != 2:
                return info
            changed = mock.Mock()
            for field in (
                "st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns",
            ):
                setattr(changed, field, getattr(info, field))
            changed.st_ctime_ns = info.st_ctime_ns + 1
            return changed

        with mock.patch("orch.core.os.fstat", side_effect=changed_fstat):
            with self.assertRaisesRegex(
                ValueError, "hash_file_changed_during_read"
            ):
                sha256_file(target)

    def test_atomic_json_write_ignores_predictable_temp_symlink_trap(self):
        target = self.base / "authority.json"
        victim = self.base / "temp-victim.json"
        victim.write_text("owner preserve\n", encoding="utf-8")
        old_predictable = target.with_name(
            target.name + f".tmp-{os.getpid()}"
        )
        old_predictable.symlink_to(victim)
        atomic_write_json(target, {"safe": True})
        self.assertEqual(victim.read_text(encoding="utf-8"), "owner preserve\n")
        self.assertTrue(old_predictable.is_symlink())
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")), {"safe": True}
        )
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_generated_plan_artifact_is_bounded_before_write(self):
        plan = self.plan(revision="generated-too-large")
        plan["adapter"]["padding"] = "x" * PLAN_MAX_BYTES
        output = self.base / "generated-too-large.json"
        with self.assertRaisesRegex(ValueError, "plan_artifact_too_large"):
            write_plan(output, plan)
        self.assertFalse(output.exists())

    def test_oversized_plan_is_rejected_before_json_parse(self):
        path = self.base / "oversized.json"
        path.write_bytes(b"{" + b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "plan_too_large"):
            self.orch.load_plan(path)
        self.assert_no_tasks()

    def test_plan_revision_and_task_ids_are_admission_safe(self):
        bad_revision, _ = self.write_plan(
            self.plan(revision="../unsafe"), name="bad-revision.json"
        )
        with self.assertRaisesRegex(ValueError, "invalid_plan"):
            self.orch.load_plan(bad_revision)
        bad_task, _ = self.write_plan(
            self.plan(tasks=[self.task("unsafe task")], revision="safe-revision"),
            name="bad-task.json",
        )
        with self.assertRaisesRegex(ValueError, "invalid_task_ids"):
            self.orch.load_plan(bad_task)
        self.assert_no_tasks()

    def test_unknown_plan_task_check_and_publication_fields_fail_closed(self):
        top = self.plan()
        top["surprise"] = True
        path, _ = self.write_plan(top, name="unknown-top.json")
        with self.assertRaisesRegex(ValueError, "unknown_plan_field:surprise"):
            self.orch.load_plan(path)

        task = self.task("UNKNOWN-TASK")
        task["surprise"] = True
        path, _ = self.write_plan(
            self.plan(tasks=[task], revision="unknown-task"),
            name="unknown-task.json",
        )
        with self.assertRaisesRegex(ValueError, "unknown_task_field:surprise"):
            self.orch.load_plan(path)

        check_task = self.task("UNKNOWN-CHECK")
        check_task["checks"] = [{
            "id": "check",
            "argv": ["/usr/bin/true"],
            "cwd": ".",
            "mystery": True,
        }]
        path, _ = self.write_plan(
            self.plan(tasks=[check_task], revision="unknown-check"),
            name="unknown-check.json",
        )
        with self.assertRaisesRegex(ValueError, "unknown_check_field:mystery"):
            self.orch.load_plan(path)

        pub_task = self.task("UNKNOWN-PUB")
        pub_task["publication"] = {"kind": "none", "mystery": True}
        path, _ = self.write_plan(
            self.plan(tasks=[pub_task], revision="unknown-pub"),
            name="unknown-pub.json",
        )
        with self.assertRaisesRegex(
            ValueError, "unknown_publication_field:mystery"
        ):
            self.orch.load_plan(path)
        self.assert_no_tasks()

    def test_check_ids_are_filename_safe_and_unique(self):
        unsafe = self.task("CHECK-ID-UNSAFE")
        unsafe["checks"] = [{
            "id": "lint/unit",
            "argv": ["/usr/bin/true"],
            "cwd": ".",
        }]
        path, _ = self.write_plan(
            self.plan(tasks=[unsafe], revision="unsafe-check-id"),
            name="unsafe-check-id.json",
        )
        with self.assertRaisesRegex(ValueError, "invalid_check_id"):
            self.orch.load_plan(path)

        duplicate = self.task("CHECK-ID-DUP")
        duplicate["checks"] = [
            {"id": "verify", "argv": ["/usr/bin/true"], "cwd": "."},
            {"id": "verify", "argv": ["/usr/bin/true"], "cwd": "."},
        ]
        path, _ = self.write_plan(
            self.plan(tasks=[duplicate], revision="duplicate-check-id"),
            name="duplicate-check-id.json",
        )
        with self.assertRaisesRegex(ValueError, "duplicate_check_id:verify"):
            self.orch.load_plan(path)
        self.assert_no_tasks()

    def test_goal_and_check_argv_are_bounded_at_admission(self):
        goal_task = self.task("GOAL-LARGE")
        goal_task["goal"] = "x" * (16 * 1024 + 1)
        path, _ = self.write_plan(
            self.plan(tasks=[goal_task], revision="goal-large"),
            name="goal-large.json",
        )
        with self.assertRaisesRegex(ValueError, "invalid_goal_too_large"):
            self.orch.load_plan(path)

        argv_task = self.task("ARGV-LARGE")
        argv_task["checks"] = [{
            "id": "check",
            "argv": ["/usr/bin/true"] + ["x"] * 64,
            "cwd": ".",
        }]
        path, _ = self.write_plan(
            self.plan(tasks=[argv_task], revision="argv-large"),
            name="argv-large.json",
        )
        with self.assertRaisesRegex(ValueError, "invalid_check_argv"):
            self.orch.load_plan(path)
        self.assert_no_tasks()

    def test_task_count_is_bounded_before_task_preparation(self):
        tasks = [self.task(f"MANY-{index:03d}") for index in range(513)]
        path, _ = self.write_plan(
            self.plan(tasks=tasks, revision="too-many-tasks"),
            name="too-many.json",
        )
        with self.assertRaisesRegex(ValueError, "plan_too_many_tasks"):
            self.orch.load_plan(path)
        self.assert_no_tasks()


if __name__ == "__main__":
    unittest.main()
