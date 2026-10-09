"""Disposable multi-project writer reservation acceptance tests.

The fixture uses three independent local-only Git repositories, a private
ORCH_HOME, and spawned OS processes synchronized before their claim attempts.
It does not start ChatGPT workers, use production remotes, or finish runs.
"""

import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry


def _claim_worker(home, project_id, worker_id, barrier, output):
    """Send only non-secret reservation metadata to the parent process."""
    try:
        orch = Orchestrator(Path(home))
        barrier.wait(timeout=15)
        claim = orch.claim(worker_id, project_id=project_id)
        output.put({
            "pid": os.getpid(),
            "requested_project_id": project_id,
            "status": claim["status"],
            "project_id": claim.get("project_id"),
            "task_id": claim.get("task_id"),
            "run_id": claim.get("run_id"),
            "writer_key": claim.get("context", {}).get("writer_key"),
            "active_run_id": claim.get("active", {}).get("run_id"),
            "reason": claim.get("reason"),
        })
    except Exception as exc:
        output.put({
            "pid": os.getpid(),
            "error": f"{type(exc).__name__}: {exc}",
        })


class MultiProjectAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-p1-a1-1-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "isolated-orch-home"
        env = mock.patch.dict(os.environ, {"ORCH_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.orch = Orchestrator(self.home)
        self.registry = ProjectRegistry(self.home)
        self.projects = {}
        self.repos = {}
        self.owned_processes = []
        self.addCleanup(self._stop_owned_processes)
        for label in ("A", "B", "C"):
            repo = self._make_git_repo(label)
            self._register(label, repo)

    def _git(self, *args):
        env = dict(os.environ)
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return subprocess.run(
            ["git", *args], check=True, text=True, capture_output=True,
            timeout=12, env=env,
        )

    def _make_git_repo(self, label):
        repo = self.base / f"project-{label.lower()}"
        self._git("init", "-q", "-b", "main", str(repo))
        self._git("-C", str(repo), "config", "user.email", "fixture@example.invalid")
        self._git("-C", str(repo), "config", "user.name", "Acceptance Fixture")
        (repo / "README.md").write_text("local-only fixture\n", encoding="utf-8")
        self._git("-C", str(repo), "add", "README.md")
        self._git("-c", "core.hooksPath=/dev/null", "-C", str(repo),
                  "commit", "-q", "--no-gpg-sign", "-m", "fixture root")
        self.assertEqual(self._git("-C", str(repo), "remote").stdout.strip(), "")
        return repo

    def _register(self, label, repo):
        result = self.registry.add(
            repo, name=f"Project {label}", profile="safe",
            review_mode="off", allow_commit=False, allow_push=False,
        )
        self.assertEqual(result["status"], "REGISTERED")
        self.projects[label] = result["project"]
        self.repos[label] = repo

    def _enqueue(self, label, task_id):
        plan = build_single_task_plan(
            self.projects[label], task_id=task_id,
            goal="Exercise local-only writer reservation",
            allowed_paths=["result.txt"],
            plan_revision=f"acceptance-{task_id.lower()}",
        )
        plans = self.home / "plans"
        plans.mkdir(mode=0o700, exist_ok=True)
        path = plans / f"{plan['plan_revision']}.json"
        self.assertEqual(write_plan(path, plan)["status"], "CREATED")
        self.assertEqual(self.orch.load_plan(path)["queued_count"], 1)

    def _stop_owned_processes(self):
        remaining = []
        for process in self.owned_processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            if process.is_alive():
                remaining.append(process.pid)
            else:
                process.join(timeout=0)
        self.assertFalse(remaining, f"owned claim children still alive: {remaining}")

    def _parallel_claims(self, labels):
        """Release independent spawn processes together, then collect exits."""
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(labels) + 1)
        output = context.Queue()
        children = []
        try:
            for label in labels:
                child = context.Process(
                    target=_claim_worker,
                    args=(str(self.home), self.projects[label]["project_id"],
                          f"fixture-worker-{label}", barrier, output),
                    name=f"acceptance-claim-{label}",
                )
                child.start()
                self.owned_processes.append(child)
                children.append(child)
            # Every child has constructed its own Orchestrator before release.
            barrier.wait(timeout=15)
            for child in children:
                child.join(timeout=18)
            exits = {child.pid: child.exitcode for child in children}
            self.assertFalse(
                [child.pid for child in children if child.is_alive()],
                f"claim children timed out: {exits}",
            )
            self.assertTrue(all(code == 0 for code in exits.values()), exits)
            records = [output.get(timeout=3) for _ in children]
            self.assertEqual({r["pid"] for r in records}, set(exits), records)
            self.assertFalse([r for r in records if "error" in r], records)
            return {label: next(
                record for record in records
                if record["requested_project_id"] == self.projects[label]["project_id"]
            ) for label in labels}
        finally:
            barrier.abort()
            self._stop_owned_processes()
            output.close()
            output.join_thread()

    def test_three_scoped_claims_race_and_survive_reopen(self):
        for label in ("A", "B", "C"):
            self._enqueue(label, f"{label}-FIRST")
        before = {row["task_id"]: row for row in self.orch.queue_view()["tasks"]}
        self.assertLess(before["A-FIRST"]["queue_seq"],
                        before["B-FIRST"]["queue_seq"])
        self.assertEqual(
            self.orch.next_work(project_id=self.projects["B"]["project_id"])["task_id"],
            "B-FIRST",
        )
        results = self._parallel_claims(("B", "A", "C"))
        for label, result in results.items():
            self.assertEqual(result["status"], "CLAIMED", results)
            self.assertEqual(result["task_id"], f"{label}-FIRST")
            self.assertEqual(result["project_id"], self.projects[label]["project_id"])
        self.assertEqual(len({r["pid"] for r in results.values()}), 3)
        self.assertEqual(len({r["run_id"] for r in results.values()}), 3)
        writer_keys = {r["writer_key"] for r in results.values()}
        self.assertEqual(len(writer_keys), 3)
        self.assertTrue(all(key.startswith("git:") for key in writer_keys))
        reopened = Orchestrator(self.home)
        view = reopened.queue_view()
        self.assertEqual(view["summary"]["active_writer_count"], 3)
        self.assertEqual({item["writer_key"] for item in view["active_writers"]},
                         writer_keys)
        self.assertEqual({item["run_id"] for item in view["active_writers"]},
                         {r["run_id"] for r in results.values()})
        for label in ("A", "B", "C"):
            self.assertEqual(reopened.claim(
                f"following-{label}", project_id=self.projects[label]["project_id"]
            )["status"], "NO_WORK")

    def test_linked_worktree_concurrent_claims_share_writer(self):
        linked = self.base / "linked-a"
        self._git("-C", str(self.repos["A"]), "worktree", "add",
                  "-q", "-b", "acceptance-linked", str(linked))
        self._register("A-linked", linked)
        self._enqueue("A", "A-BASE")
        self._enqueue("A-linked", "A-LINKED")
        tasks = {row["task_id"]: row for row in self.orch.queue_view()["tasks"]}
        self.assertEqual(tasks["A-BASE"]["writer_key"],
                         tasks["A-LINKED"]["writer_key"])
        self.assertTrue(tasks["A-BASE"]["writer_key"].startswith("git:"))
        results = self._parallel_claims(("A", "A-linked"))
        winners = [r for r in results.values() if r["status"] == "CLAIMED"]
        losers = [r for r in results.values() if r["status"] == "BUSY"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertEqual(losers[0]["active_run_id"], winners[0]["run_id"])
        reopened = Orchestrator(self.home)
        self.assertEqual(reopened.queue_view()["summary"]["active_writer_count"], 1)
        losing_label = next(label for label, result in results.items()
                            if result["status"] == "BUSY")
        blocked = reopened.claim(
            "retry-linked", project_id=self.projects[losing_label]["project_id"]
        )
        self.assertEqual(blocked["status"], "BUSY")
        self.assertEqual(blocked["active"]["run_id"], winners[0]["run_id"])

    def test_pausing_b_keeps_its_writer_and_allows_a_c(self):
        self._enqueue("B", "B-ACTIVE")
        self._enqueue("B", "B-NEXT")
        self._enqueue("A", "A-READY")
        self._enqueue("C", "C-READY")
        project_b = self.projects["B"]["project_id"]
        held = self.orch.claim("holder-b", project_id=project_b)
        self.assertEqual(held["status"], "CLAIMED")
        self.assertEqual(self.orch.pause_project(project_b, "fixture hold")["status"],
                         "PROJECT_PAUSED")
        self.assertEqual(
            self.orch.claim("blocked-b", project_id=project_b)["status"],
            "PROJECT_PAUSED",
        )
        results = self._parallel_claims(("A", "C"))
        for label, record in results.items():
            self.assertEqual(record["status"], "CLAIMED", results)
            self.assertEqual(record["task_id"], f"{label}-READY")
        reopened = Orchestrator(self.home)
        view = reopened.queue_view(project_id=project_b)
        self.assertEqual(view["status"], "PROJECT_PAUSED")
        self.assertEqual(view["summary"]["active_writer_count"], 3)
        self.assertEqual(
            next(item for item in view["tasks"]
                 if item["task_id"] == "B-NEXT")["queue_state"], "PAUSED_PROJECT",
        )
        self.assertIn(held["run_id"],
                      {item["run_id"] for item in view["active_writers"]})
        self.assertEqual(reopened.resume_project(project_b)["status"],
                         "PROJECT_RESUMED")
        busy = reopened.claim("after-resume-b", project_id=project_b)
        self.assertEqual(busy["status"], "BUSY")
        self.assertEqual(busy["active"]["run_id"], held["run_id"])
        after = reopened.queue_view(project_id=project_b)
        self.assertEqual(
            next(item for item in after["tasks"]
                 if item["task_id"] == "B-NEXT")["queue_state"], "WAITING_WRITER",
        )
        self.assertEqual(after["summary"]["active_writer_count"], 3)


if __name__ == "__main__":
    unittest.main()
