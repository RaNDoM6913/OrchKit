"""Disposable multi-project reservation and lifecycle acceptance tests.

The fixture uses three independent local-only Git repositories, a private
ORCH_HOME, and synchronized spawned OS processes for writer claim attempts.
It also exercises independent completion, Git-local publication and negative
run-scoped authority cases without starting real ChatGPT workers or contacting
production remotes.
"""

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
        self.base = Path(tempfile.mkdtemp(prefix="orch-p1-a1-1-"))
        self.addCleanup(self._cleanup_disposable_fixture)
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

    def _enable_local_publication(self, label):
        result = self.registry.add(
            self.repos[label], name=f"Project {label}", profile="safe",
            review_mode="off", allow_commit=True, allow_push=False,
            replace=True,
        )
        self.assertEqual(result["status"], "REGISTERED")
        self.projects[label] = result["project"]
        self.assertTrue(result["project"]["git"]["allow_commit"])
        self.assertFalse(result["project"]["git"]["allow_push"])

    def _enqueue(self, label, task_id, *, checks=(), allowed_paths=None):
        plan = build_single_task_plan(
            self.projects[label], task_id=task_id,
            goal="Exercise local-only writer reservation",
            allowed_paths=allowed_paths or ["result.txt"],
            plan_revision=f"acceptance-{task_id.lower()}",
        )
        if checks:
            plan["tasks"][0]["checks"] = list(checks)
        plans = self.home / "plans"
        plans.mkdir(mode=0o700, exist_ok=True)
        path = plans / f"{plan['plan_revision']}.json"
        self.assertEqual(write_plan(path, plan)["status"], "CREATED")
        self.assertEqual(self.orch.load_plan(path)["queued_count"], 1)

    def _claim(self, label):
        claim = self.orch.claim(
            f"lifecycle-worker-{label}",
            project_id=self.projects[label]["project_id"],
        )
        self.assertEqual(claim["status"], "CLAIMED", claim)
        self.assertEqual(claim["project_id"], self.projects[label]["project_id"])
        return claim

    def _lease(self, claim):
        return self.orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )

    def _write_receipt(self, claim, paths=("result.txt",), **overrides):
        receipt = {
            "schema_version": 1,
            "run_id": claim["run_id"],
            "task_id": claim["task_id"],
            "changed_paths": list(paths),
        }
        receipt.update(overrides)
        target = Path(claim["receipt_file"])
        target.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        return target

    def _finish_result(self, label, claim, *, publish=False):
        workspace = self.repos[label]
        (workspace / "result.txt").write_text(
            f"completed locally for {label}\n", encoding="utf-8"
        )
        lease = self._lease(claim)
        receipt = self._write_receipt(claim)
        self.assertEqual(
            self.orch.submit(claim["run_id"], lease, receipt)["status"],
            "RESULT_SUBMITTED",
        )
        quiesced = self.orch.quiesce(claim["run_id"], lease)
        self.assertEqual(quiesced["status"], "VERIFYING")
        self.assertTrue(quiesced["capability_revoked"])
        self.assertFalse(Path(claim["capability_file"]).exists())
        verified = self.orch.verify(claim["run_id"])
        self.assertEqual(verified["status"], "VERIFIED", verified)
        if publish:
            self.assertEqual(claim["context"]["publication"]["kind"], "git_local")
            self.assertEqual(
                self.orch.complete(claim["run_id"])["status"],
                "PUBLICATION_REQUIRED",
            )
            finished = self.orch.publish(claim["run_id"])
            self.assertEqual(finished["status"], "COMPLETE")
            self.assertIsNone(finished["remote_commit"])
            self.assertEqual(
                finished["commit"],
                self._git("-C", str(workspace), "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                self._git("-C", str(workspace), "remote").stdout.strip(), ""
            )
            self.assertEqual(
                self._git("-C", str(workspace), "status", "--porcelain").stdout, ""
            )
        else:
            finished = self.orch.complete(claim["run_id"])
            self.assertEqual(finished["status"], "COMPLETE")
        return verified, finished

    def _cleanup_disposable_fixture(self):
        # Do not remove a worker's files while its activity is still unknown.
        unconfirmed = [process.pid for process in getattr(self, "owned_processes", [])
                       if process.exitcode is None or process.is_alive()]
        self.assertFalse(unconfirmed,
                         "disposable fixture retained; children unconfirmed: "
                         f"{unconfirmed}")
        shutil.rmtree(self.base)

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


    def test_checkpointed_b_does_not_change_a_c_local_publications(self):
        for label in ("A", "C"):
            self._enable_local_publication(label)
        for label in ("A", "B", "C"):
            self._enqueue(label, f"{label}-PUBLISH")
        self._enqueue("B", "B-AFTER-CHECKPOINT")
        claims = {label: self._claim(label) for label in ("A", "B", "C")}
        starting_heads = {
            label: self._git("-C", str(self.repos[label]), "rev-parse", "HEAD")
            .stdout.strip()
            for label in ("A", "B", "C")
        }

        lease_b = self._lease(claims["B"])
        checkpoint = self.orch.checkpoint(
            claims["B"]["run_id"], lease_b,
            "fixture deliberately preserves unknown activity", "unknown",
        )
        self.assertEqual(checkpoint["status"], "CHECKPOINTED")
        self.assertFalse(checkpoint["details"]["writer_reservation_released"])
        self.assertFalse(checkpoint["details"]["safe_to_resume_elsewhere"])
        with self.assertRaisesRegex(
            ValueError, "process_inactivity_confirmation_required"
        ):
            self.orch.abort(claims["B"]["run_id"], "unsafe abort", retry=True)

        for label in ("A", "C"):
            self._finish_result(label, claims[label], publish=True)
            self.assertNotEqual(
                self._git("-C", str(self.repos[label]), "rev-parse", "HEAD")
                .stdout.strip(), starting_heads[label],
            )
        reopened = Orchestrator(self.home)
        state = {item["task_id"]: item for item in reopened.status()["tasks"]}
        runs = {item["task_id"]: item for item in reopened.status()["runs"]}
        for label in ("A", "C"):
            self.assertEqual(state[f"{label}-PUBLISH"]["status"], "DONE")
            self.assertEqual(runs[f"{label}-PUBLISH"]["state"], "COMPLETE")
            self.assertEqual(runs[f"{label}-PUBLISH"]["verify_status"], "PASS")
        self.assertEqual(state["B-PUBLISH"]["status"], "IN_PROGRESS")
        self.assertEqual(runs["B-PUBLISH"]["state"], "RUNNING")
        self.assertEqual(reopened.queue_view()["summary"]["active_writer_count"], 1)
        blocked = reopened.claim(
            "cannot-steal-b", project_id=self.projects["B"]["project_id"]
        )
        self.assertEqual(blocked["status"], "BUSY")
        self.assertEqual(blocked["active"]["run_id"], claims["B"]["run_id"])
        self.assertTrue(Path(claims["B"]["capability_file"]).is_file())
        self.assertEqual(
            self._git("-C", str(self.repos["B"]), "rev-parse", "HEAD")
            .stdout.strip(), starting_heads["B"],
        )

    def test_failed_b_verification_does_not_change_a_c_completion(self):
        failing_check = {
            "id": "expected-failure",
            "argv": [sys.executable, "-c", "import sys; sys.exit(7)"],
            "cwd": ".",
            "timeout_sec": 5,
        }
        self._enqueue("A", "A-SUCCESS")
        self._enqueue("B", "B-FAIL", checks=[failing_check])
        self._enqueue("C", "C-SUCCESS")
        claims = {label: self._claim(label) for label in ("A", "B", "C")}
        for label in ("A", "C"):
            self._finish_result(label, claims[label])
        (self.repos["B"] / "result.txt").write_text("fail check\n")
        lease_b = self._lease(claims["B"])
        receipt_b = self._write_receipt(claims["B"])
        self.assertEqual(
            self.orch.submit(claims["B"]["run_id"], lease_b, receipt_b)["status"],
            "RESULT_SUBMITTED",
        )
        self.assertEqual(
            self.orch.quiesce(claims["B"]["run_id"], lease_b)["status"],
            "VERIFYING",
        )
        failed = self.orch.verify(claims["B"]["run_id"])
        self.assertEqual(failed["status"], "NEEDS_FIX", failed)
        self.assertEqual(failed["checks"][0]["exit_code"], 7)

        reopened = Orchestrator(self.home)
        tasks = {item["task_id"]: item for item in reopened.status()["tasks"]}
        runs = {item["task_id"]: item for item in reopened.status()["runs"]}
        for label in ("A", "C"):
            self.assertEqual(tasks[f"{label}-SUCCESS"]["status"], "DONE")
            self.assertEqual(runs[f"{label}-SUCCESS"]["state"], "COMPLETE")
            self.assertEqual(runs[f"{label}-SUCCESS"]["verify_status"], "PASS")
        self.assertEqual(tasks["B-FAIL"]["status"], "NEEDS_FIX")
        self.assertEqual(runs["B-FAIL"]["state"], "NEEDS_FIX")
        self.assertEqual(reopened.queue_view()["summary"]["active_writer_count"], 0)
        # Only remove the known disposable fixture output after the failed run;
        # the claim preflight correctly refuses a dirty Git workspace.
        (self.repos["B"] / "result.txt").unlink()
        repair = reopened.claim(
            "b-repair-attempt", project_id=self.projects["B"]["project_id"]
        )
        self.assertEqual(repair["status"], "CLAIMED", repair)
        self.assertEqual(repair["task_id"], "B-FAIL")
        self.assertEqual(repair["attempt"], 2)
        self.assertNotEqual(repair["run_id"], claims["B"]["run_id"])
        task_states = {
            item["task_id"]: item["status"] for item in reopened.status()["tasks"]
        }
        self.assertEqual(task_states["A-SUCCESS"], "DONE")
        self.assertEqual(task_states["C-SUCCESS"], "DONE")

    def test_foreign_capability_and_receipt_cannot_authorize_other_run(self):
        for label in ("A", "B", "C"):
            self._enqueue(label, f"{label}-AUTH")
        claims = {label: self._claim(label) for label in ("A", "B", "C")}
        a, b = claims["A"], claims["B"]
        lease_a, lease_b = self._lease(a), self._lease(b)
        with self.assertRaisesRegex(ValueError, "invalid_capability_file"):
            self.orch.lease_from_capability(b["run_id"], Path(a["capability_file"]))
        with self.assertRaisesRegex(ValueError, "invalid_lease"):
            self.orch.heartbeat(b["run_id"], lease_a)
        with self.assertRaisesRegex(ValueError, "invalid_lease"):
            self.orch.checkpoint(b["run_id"], lease_a, "foreign lease")
        with self.assertRaisesRegex(ValueError, "invalid_lease"):
            self.orch.quiesce(b["run_id"], lease_a)

        receipt_a = self._write_receipt(a)
        receipt_b = self._write_receipt(b)
        with self.assertRaisesRegex(ValueError, "invalid_receipt_path"):
            self.orch.submit(b["run_id"], lease_b, receipt_a)
        with self.assertRaisesRegex(ValueError, "invalid_lease"):
            self.orch.submit(b["run_id"], lease_a, receipt_b)
        self._write_receipt(b, run_id=a["run_id"], task_id=a["task_id"])
        with self.assertRaisesRegex(ValueError, "receipt_identity_mismatch"):
            self.orch.submit(b["run_id"], lease_b, receipt_b)
        self._write_receipt(b)
        for label in ("A", "B", "C"):
            self.assertEqual(
                next(row["state"] for row in self.orch.status()["runs"]
                     if row["run_id"] == claims[label]["run_id"]),
                "RUNNING",
            )
            self._finish_result(label, claims[label])
        self.assertEqual(
            {item["status"] for item in Orchestrator(self.home).status()["tasks"]},
            {"DONE"},
        )
        with self.assertRaisesRegex(ValueError, "invalid_capability_file"):
            self.orch.lease_from_capability(a["run_id"], Path(a["capability_file"]))

    def test_retried_attempt_rejects_stale_capability_and_receipt(self):
        self._enqueue("A", "A-RETRY-AUTH")
        first = self._claim("A")
        former_lease = self._lease(first)
        former_receipt = self._write_receipt(first)
        stopped = self.orch.abort(
            first["run_id"], "disposable test retry, no active child process",
            retry=True,
        )
        self.assertEqual(stopped["status"], "ABORTED")
        self.assertFalse(Path(first["capability_file"]).exists())

        second = self.orch.claim(
            "new-attempt-worker", project_id=self.projects["A"]["project_id"]
        )
        self.assertEqual(second["status"], "CLAIMED", second)
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(second["run_id"], first["run_id"])
        with self.assertRaisesRegex(ValueError, "invalid_capability_file"):
            self.orch.lease_from_capability(
                second["run_id"], Path(first["capability_file"])
            )
        with self.assertRaisesRegex(ValueError, "invalid_lease"):
            self.orch.heartbeat(second["run_id"], former_lease)
        current_lease = self._lease(second)
        with self.assertRaisesRegex(ValueError, "invalid_receipt_path"):
            self.orch.submit(second["run_id"], current_lease, former_receipt)
        current_receipt = self._write_receipt(
            second, run_id=first["run_id"], task_id=first["task_id"]
        )
        with self.assertRaisesRegex(ValueError, "receipt_identity_mismatch"):
            self.orch.submit(second["run_id"], current_lease, current_receipt)
        self._finish_result("A", second)
        run_states = {
            run["run_id"]: run["state"]
            for run in Orchestrator(self.home).status()["runs"]
        }
        self.assertEqual(run_states[first["run_id"]], "ABORTED")
        self.assertEqual(run_states[second["run_id"]], "COMPLETE")

    def test_undeclared_and_foreign_paths_block_only_a(self):
        for label in ("A", "B", "C"):
            self._enqueue(label, f"{label}-SCOPE")
        claims = {label: self._claim(label) for label in ("A", "B", "C")}
        a = claims["A"]
        lease_a = self._lease(a)
        escaped = self._write_receipt(a, paths=("../project-b/README.md",))
        with self.assertRaisesRegex(ValueError, "path_not_allowed"):
            self.orch.submit(a["run_id"], lease_a, escaped)
        (self.repos["A"] / "result.txt").write_text("allowed\n")
        (self.repos["A"] / "not-allowed.txt").write_text("unauthorized\n")
        receipt = self._write_receipt(a)
        self.assertEqual(
            self.orch.submit(a["run_id"], lease_a, receipt)["status"],
            "RESULT_SUBMITTED",
        )
        self.orch.quiesce(a["run_id"], lease_a)
        blocked = self.orch.verify(a["run_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(
            blocked["reason"], "workspace_scope_violation:not-allowed.txt",
        )
        for label in ("B", "C"):
            self._finish_result(label, claims[label])
        final = {row["task_id"]: row for row in Orchestrator(self.home).status()["tasks"]}
        self.assertEqual(final["A-SCOPE"]["status"], "BLOCKED")
        self.assertEqual(final["B-SCOPE"]["status"], "DONE")
        self.assertEqual(final["C-SCOPE"]["status"], "DONE")

    def test_symlinked_output_cannot_escape_into_another_project(self):
        for label in ("A", "B", "C"):
            self._enqueue(label, f"{label}-LINK")
        claims = {label: self._claim(label) for label in ("A", "B", "C")}
        foreign = self.repos["B"] / "README.md"
        before_sha = hashlib.sha256(foreign.read_bytes()).hexdigest()
        before_head = self._git(
            "-C", str(self.repos["B"]), "rev-parse", "HEAD"
        ).stdout.strip()
        (self.repos["A"] / "result.txt").symlink_to(foreign)
        a = claims["A"]
        lease = self._lease(a)
        self.assertEqual(
            self.orch.submit(
                a["run_id"], lease, self._write_receipt(a)
            )["status"], "RESULT_SUBMITTED",
        )
        self.orch.quiesce(a["run_id"], lease)
        blocked = self.orch.verify(a["run_id"])
        self.assertEqual(blocked["status"], "BLOCKED", blocked)
        self.assertEqual(blocked["reason"], "path_escape")
        self.assertEqual(
            hashlib.sha256(foreign.read_bytes()).hexdigest(), before_sha,
        )
        self.assertEqual(
            self._git("-C", str(self.repos["B"]), "rev-parse", "HEAD")
            .stdout.strip(), before_head,
        )
        for label in ("B", "C"):
            self._finish_result(label, claims[label])
        tasks = {row["task_id"]: row for row in Orchestrator(self.home).status()["tasks"]}
        self.assertEqual(tasks["A-LINK"]["status"], "BLOCKED")
        self.assertEqual(tasks["B-LINK"]["status"], "DONE")
        self.assertEqual(tasks["C-LINK"]["status"], "DONE")


if __name__ == "__main__":
    unittest.main()
