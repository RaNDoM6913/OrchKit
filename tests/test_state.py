from pathlib import Path
import hashlib
import json
import os
import signal
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile

import orch.state as state_module
from orch.core import Orchestrator
from orch.state import (backup_state, check_state, inspect_home_replacement,
                        migration_history, prune_capabilities, prune_retention,
                        recovery_inspect, reconcile_home_replacement,
                        replace_home_from_backup, restore_backup_archive,
                        retention_status, verify_backup_archive)


class StateMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orch"
        self.root.mkdir()
        self.orch = Orchestrator(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_state_has_schema_version_and_integrity(self):
        result = check_state(self.orch)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["schema_version"], 4)
        self.assertEqual(result["quick_check"], ["ok"])
        self.assertEqual(result["foreign_key_violations"], [])
        self.assertEqual(result["migration_history"][-1]["version"], 4)
        self.assertIn(result["migration_history"][-1]["details"]["kind"], {
            "transactional_upgrade", "observed_existing_schema",
        })

    def test_schema_version_drift_blocks_state_check(self):
        with self.orch.connect() as conn:
            conn.execute("PRAGMA user_version=99")

        result = check_state(self.orch)

        self.assertEqual(result["schema_version"], 99)
        self.assertEqual(result["expected_schema_version"], 4)
        self.assertEqual(result["quick_check"], ["ok"])
        self.assertEqual(result["status"], "BLOCKED")

    def test_backup_refuses_schema_version_drift_before_output(self):
        with self.orch.connect() as conn:
            conn.execute("PRAGMA user_version=99")

        with self.assertRaisesRegex(ValueError, "state_integrity_blocked"):
            backup_state(self.orch)

        self.assertFalse((self.root / "backups").exists())

    def test_state_check_reports_missing_claims_without_recreating_it(self):
        claims = self.orch.runtime / "claims"
        claims.rmdir()
        prior_umask = os.umask(0o077)
        try:
            result = check_state(self.orch)
        finally:
            os.umask(prior_umask)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["capabilities"]["directory_status"], "MISSING")
        self.assertFalse(claims.exists())
        claim_path = next(
            item for item in result["permissions"]["paths"]
            if item["path"] == str(claims)
        )
        self.assertEqual(claim_path["status"], "MISSING")

    def test_state_check_rejects_file_in_required_directory(self):
        logs = self.orch.runtime / "logs"
        logs.rmdir()
        logs.write_text("wrong object type\n", encoding="utf-8")
        os.chmod(logs, 0o700)

        result = check_state(self.orch)

        self.assertEqual(result["status"], "BLOCKED")
        log_path = next(
            item for item in result["permissions"]["paths"]
            if item["path"] == str(logs)
        )
        self.assertEqual(log_path["status"], "NOT_DIRECTORY")

    def test_state_check_rejects_file_in_optional_directory(self):
        projects = self.root / "projects"
        projects.write_text("wrong object type\n", encoding="utf-8")
        os.chmod(projects, 0o700)

        result = check_state(self.orch)

        self.assertEqual(result["status"], "BLOCKED")
        project_path = next(
            item for item in result["permissions"]["paths"]
            if item["path"] == str(projects.resolve())
        )
        self.assertEqual(project_path["status"], "NOT_DIRECTORY")

    def test_state_check_reports_dangling_optional_directory_symlink(self):
        projects = self.orch.root / "projects"
        projects.symlink_to(
            self.orch.root / "missing-projects", target_is_directory=True
        )

        result = check_state(self.orch)

        self.assertEqual(result["status"], "BLOCKED")
        project_path = next(
            item for item in result["permissions"]["paths"]
            if item["path"] == str(projects)
        )
        self.assertEqual(project_path["status"], "UNSAFE_SYMLINK")

    def test_recovery_inspect_does_not_read_symlinked_claims_directory(self):
        claims = self.orch.runtime / "claims"
        external = Path(self.tmp.name) / "foreign-claims"
        external.mkdir()
        (external / "FOREIGN.json").write_text("foreign\n", encoding="utf-8")
        claims.rmdir()
        claims.symlink_to(external, target_is_directory=True)

        result = recovery_inspect(self.orch)

        self.assertEqual(result["status"], "ATTENTION")
        self.assertEqual(result["capabilities"]["orphan_capabilities"], [])
        self.assertEqual(
            result["capabilities"]["directory_status"], "UNSAFE_SYMLINK"
        )
        self.assertTrue((external / "FOREIGN.json").is_file())

    def test_recovery_inspect_ignores_matching_external_capability(self):
        self._load_recovery_task("RECOVERY-SYMLINK")
        claim = self.orch.claim("worker")
        claims = self.orch.runtime / "claims"
        external = Path(self.tmp.name) / "external-claims"
        external.mkdir()
        external_capability = external / f"{claim['run_id']}.json"
        Path(claim["capability_file"]).rename(external_capability)
        claims.rmdir()
        claims.symlink_to(external, target_is_directory=True)

        result = recovery_inspect(self.orch, run_id=claim["run_id"])

        self.assertEqual(result["status"], "ATTENTION")
        self.assertEqual(result["items"][0]["classification"], "CAPABILITY_MISSING")
        self.assertFalse(result["items"][0]["capability_present"])
        self.assertEqual(
            result["capabilities"]["missing_capabilities"], [claim["run_id"]]
        )
        self.assertTrue(external_capability.is_file())

    def _load_recovery_task(self, task_id="RECOVERY-1", publication=None):
        workspace = Path(self.tmp.name) / f"ws-{task_id.lower()}"
        workspace.mkdir()
        plan = {
            "schema_version": 1,
            "plan_revision": f"{task_id.lower()}-v1",
            "tasks": [{
                "id": task_id,
                "project_id": "recovery-project",
                "goal": "recovery fixture",
                "workspace": str(workspace),
                "dependencies": [],
                "allowed_paths": ["result.json"],
                "protected_paths": {},
                "checks": [],
                "review": {"mode": "off", "reviewer": "none", "placement": "pre_publish"},
                "owner_acceptance": False,
                "publication": publication or {"kind": "none"},
                "max_attempts": 2,
            }],
        }
        path = self.root / f"{task_id}.json"
        path.write_text(json.dumps(plan))
        self.orch.load_plan(path)
        return workspace

    def test_recovery_inspect_running_is_read_only_and_secret_free(self):
        self._load_recovery_task()
        claim = self.orch.claim("scheduled-worker")
        capability = Path(claim["capability_file"])

        result = recovery_inspect(self.orch)
        self.assertEqual(result["status"], "ATTENTION")
        self.assertFalse(result["automatic_expiry"])
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["classification"], "WORKER_MAY_STILL_BE_ACTIVE")
        self.assertTrue(item["capability_present"])
        self.assertTrue(item["capability_expected"])
        self.assertIn("Do not abort/retry", " ".join(item["safe_next_steps"]))
        self.assertNotIn("abort --run-id", " ".join(item["safe_next_steps"]))
        self.assertIsNone(item["checkpoint"])
        self.assertNotIn("lease_token", json.dumps(result))
        self.assertTrue(capability.exists())

    def test_recovery_inspect_exposes_checkpoint_without_releasing_writer(self):
        self._load_recovery_task("RECOVERY-CHECKPOINT")
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        lease = self.orch.lease_from_capability(claim["run_id"], cap)

        self.orch.checkpoint(
            claim["run_id"], lease, "handoff pressure", "unknown"
        )
        unknown = recovery_inspect(self.orch, run_id=claim["run_id"])
        item = unknown["items"][0]
        self.assertEqual(
            item["classification"], "CHECKPOINTED_PROCESS_STATE_UNKNOWN"
        )
        self.assertEqual(item["checkpoint"]["status"], "RECORDED")
        self.assertEqual(item["checkpoint"]["reason"], "handoff pressure")
        self.assertFalse(item["checkpoint"]["writer_reservation_released"])
        self.assertFalse(item["checkpoint"]["capability_revoked"])
        self.assertFalse(item["checkpoint"]["safe_to_resume_elsewhere"])
        self.assertIn("Do not abort/retry", " ".join(item["safe_next_steps"]))
        self.assertTrue(cap.is_file())

        self.orch.checkpoint(
            claim["run_id"], lease, "same worker confirmed active", "active"
        )
        active = recovery_inspect(self.orch, run_id=claim["run_id"])
        active_item = active["items"][0]
        self.assertEqual(
            active_item["classification"], "CHECKPOINTED_WORKER_ACTIVE"
        )
        self.assertEqual(active_item["checkpoint"]["process_state"], "active")
        self.assertIn(
            "same observed worker", " ".join(active_item["safe_next_steps"])
        )

        with self.orch.connect() as conn:
            run = conn.execute(
                "SELECT state FROM runs WHERE run_id=?", (claim["run_id"],)
            ).fetchone()
        self.assertEqual(run["state"], "RUNNING")
        with self.orch.connect() as conn:
            locks = self.orch._active_writer_locks(conn)
        self.assertEqual(
            next(iter(locks.values()))["run_id"], claim["run_id"]
        )

    def test_recovery_inspect_tracks_submit_quiesce_and_verify_stages(self):
        workspace = self._load_recovery_task("RECOVERY-STAGES")
        claim = self.orch.claim("worker")
        (workspace / "result.json").write_text('{"ok":true}\n')
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"],
            "task_id": "RECOVERY-STAGES",
            "changed_paths": ["result.json"],
        }))
        cap = Path(claim["capability_file"])
        lease = self.orch.lease_from_capability(claim["run_id"], cap)
        self.orch.submit(claim["run_id"], lease, receipt)

        submitted = recovery_inspect(self.orch, run_id=claim["run_id"])
        self.assertEqual(
            submitted["items"][0]["classification"], "RESULT_AWAITING_QUIESCE"
        )
        self.assertTrue(submitted["items"][0]["capability_present"])

        self.orch.quiesce(claim["run_id"], lease)
        verifying = recovery_inspect(self.orch, run_id=claim["run_id"])
        self.assertEqual(
            verifying["items"][0]["classification"], "VERIFY_RESUMABLE"
        )
        self.assertFalse(verifying["items"][0]["capability_present"])

        verified = self.orch.verify(claim["run_id"])
        self.assertEqual(verified["status"], "VERIFIED")
        ready = recovery_inspect(self.orch, run_id=claim["run_id"])
        self.assertEqual(
            ready["items"][0]["classification"], "COMPLETION_READY"
        )
        self.assertIn(
            f"orch complete --run-id {claim['run_id']}",
            ready["items"][0]["safe_next_steps"],
        )

    def test_recovery_inspect_missing_capability_never_recreates_it(self):
        self._load_recovery_task("RECOVERY-MISSING")
        claim = self.orch.claim("worker")
        cap = Path(claim["capability_file"])
        cap.unlink()

        result = recovery_inspect(self.orch, run_id=claim["run_id"])
        item = result["items"][0]
        self.assertEqual(item["classification"], "CAPABILITY_MISSING")
        self.assertFalse(item["capability_present"])
        self.assertIn("Do not recreate", item["safe_next_steps"][0])
        self.assertIn("Do not abort/retry", " ".join(item["safe_next_steps"]))
        self.assertNotIn("abort --run-id", " ".join(item["safe_next_steps"]))
        self.assertFalse(cap.exists())

    def test_recovery_inspect_prioritizes_publication_reconciliation(self):
        self._load_recovery_task("RECOVERY-PUB")
        claim = self.orch.claim("worker")
        self.orch._publication_update(
            claim["run_id"], status="INTENT", operation_id="recovery-fixture",
            kind="git_local", expected_base="a" * 40,
        )
        result = recovery_inspect(self.orch, run_id=claim["run_id"])
        item = result["items"][0]
        self.assertEqual(
            item["classification"], "PUBLICATION_RECONCILIATION_REQUIRED"
        )
        self.assertEqual(item["publication"]["status"], "INTENT")
        self.assertIn(
            f"orch publish-reconcile --run-id {claim['run_id']}",
            item["safe_next_steps"][0],
        )

    def test_runtime_permissions_are_private_and_existing_modes_are_repaired(self):
        private_dirs = [
            self.orch.runtime,
            self.orch.runtime / "logs",
            self.orch.runtime / "worker_receipts",
            self.orch.runtime / "claims",
            self.orch.runtime / "review_exports",
            self.orch.runtime / "git-hooks-disabled",
        ]
        for path in private_dirs:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.orch.db_path.stat().st_mode & 0o777, 0o600)

        os.chmod(self.orch.runtime, 0o755)
        os.chmod(self.orch.db_path, 0o644)
        for sidecar in (
            self.orch.db_path.with_name(self.orch.db_path.name + "-wal"),
            self.orch.db_path.with_name(self.orch.db_path.name + "-shm"),
        ):
            if sidecar.exists():
                os.chmod(sidecar, 0o644)

        repaired = Orchestrator(self.root)
        self.assertEqual(repaired.runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual(repaired.db_path.stat().st_mode & 0o777, 0o600)
        for sidecar in (
            repaired.db_path.with_name(repaired.db_path.name + "-wal"),
            repaired.db_path.with_name(repaired.db_path.name + "-shm"),
        ):
            if sidecar.exists():
                self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        health = check_state(repaired)
        self.assertEqual(health["permissions"]["status"], "READY")
        self.assertNotEqual(health["status"], "BLOCKED")

    def test_runtime_symlink_is_rejected(self):
        other = Path(self.tmp.name) / "symlink-root"
        other.mkdir()
        external = Path(self.tmp.name) / "external-runtime"
        external.mkdir()
        (other / ".runtime").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "private_directory_unsafe"):
            Orchestrator(other)

    def test_orphan_capability_is_reported_and_pruned(self):
        claims = self.orch.runtime / "claims"
        claims.mkdir(exist_ok=True)
        stale = claims / "OLD-A1-deadbeef00.json"
        stale.write_text(json.dumps({"run_id": stale.stem, "lease_token": "dead"}) + "\n")
        self.assertEqual(check_state(self.orch)["status"], "ATTENTION")
        pruned = prune_capabilities(self.orch)
        self.assertEqual(pruned["count"], 1)
        self.assertFalse(stale.exists())
        self.assertEqual(check_state(self.orch)["status"], "READY")

    def test_prune_capabilities_refuses_missing_claims_directory(self):
        claims = self.orch.runtime / "claims"
        claims.rmdir()

        with self.assertRaisesRegex(
            ValueError, "capability_directory_unsafe:MISSING"
        ):
            prune_capabilities(self.orch)

        self.assertFalse(claims.exists())

    def test_prune_capabilities_refuses_symlinked_claims_directory(self):
        claims = self.orch.runtime / "claims"
        external = Path(self.tmp.name) / "external-claims-prune"
        external.mkdir()
        foreign = external / "FOREIGN.json"
        foreign.write_text("owner data\n", encoding="utf-8")
        claims.rmdir()
        claims.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(
            ValueError, "capability_directory_unsafe:UNSAFE_SYMLINK"
        ):
            prune_capabilities(self.orch)

        self.assertTrue(claims.is_symlink())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "owner data\n")

    def test_prune_capabilities_does_not_follow_swapped_claims_directory(self):
        claims = self.orch.runtime / "claims"
        orphan = claims / "ORPHAN.json"
        orphan.write_text("orphan data\n", encoding="utf-8")
        moved_claims = self.orch.runtime / "claims-original"
        external = Path(self.tmp.name) / "external-claims-race"
        external.mkdir()
        foreign = external / orphan.name
        foreign.write_text("owner data\n", encoding="utf-8")
        original_check = state_module.check_state

        def swap_after_health(orch):
            result = original_check(orch)
            claims.rename(moved_claims)
            claims.symlink_to(external, target_is_directory=True)
            return result

        with mock.patch.object(
            state_module, "check_state", side_effect=swap_after_health
        ):
            with self.assertRaisesRegex(
                ValueError, "capability_directory_unsafe:CHANGED"
            ):
                prune_capabilities(self.orch)

        self.assertEqual(foreign.read_text(encoding="utf-8"), "owner data\n")
        self.assertEqual(
            (moved_claims / orphan.name).read_text(encoding="utf-8"),
            "orphan data\n",
        )

    def test_prune_capabilities_rejects_replaced_regular_directory(self):
        claims = self.orch.runtime / "claims"
        orphan = claims / "ORPHAN.json"
        orphan.write_text("orphan data\n", encoding="utf-8")
        moved_claims = self.orch.runtime / "claims-original"
        replacement = Path(self.tmp.name) / "replacement-claims"
        replacement.mkdir()
        (replacement / orphan.name).write_text(
            "owner data\n", encoding="utf-8"
        )
        original_check = state_module.check_state

        def replace_after_health(orch):
            result = original_check(orch)
            claims.rename(moved_claims)
            replacement.rename(claims)
            return result

        with mock.patch.object(
            state_module, "check_state", side_effect=replace_after_health
        ):
            with self.assertRaisesRegex(
                ValueError, "capability_directory_unsafe:CHANGED"
            ):
                prune_capabilities(self.orch)

        self.assertEqual(
            (claims / orphan.name).read_text(encoding="utf-8"),
            "owner data\n",
        )
        self.assertEqual(
            (moved_claims / orphan.name).read_text(encoding="utf-8"),
            "orphan data\n",
        )

    def test_prune_capabilities_rejects_replaced_orphan_file(self):
        claims = self.orch.runtime / "claims"
        orphan = claims / "ORPHAN.json"
        orphan.write_text("orphan data\n", encoding="utf-8")
        moved_orphan = claims / "ORPHAN-original.json"
        original_check = state_module.check_state

        def replace_after_health(orch):
            result = original_check(orch)
            orphan.rename(moved_orphan)
            orphan.write_text("owner data\n", encoding="utf-8")
            return result

        with mock.patch.object(
            state_module, "check_state", side_effect=replace_after_health
        ):
            with self.assertRaisesRegex(
                ValueError, "capability_artifact_changed:ORPHAN"
            ):
                prune_capabilities(self.orch)

        self.assertEqual(orphan.read_text(encoding="utf-8"), "owner data\n")
        self.assertEqual(
            moved_orphan.read_text(encoding="utf-8"), "orphan data\n"
        )

    def test_backup_excludes_claims_and_contains_consistent_database(self):
        (self.root / "config.json").write_text('{"schema_version":1}\n')
        (self.orch.runtime / "logs" / "event.json").write_text('{"ok":true}\n')
        claims = self.orch.runtime / "claims"; claims.mkdir(exist_ok=True)
        (claims / "STALE.json").write_text('{"lease_token":"secret"}\n')
        result = backup_state(self.orch)
        archive = Path(result["path"])
        self.assertTrue(archive.is_file())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            self.assertIn("state/orch.sqlite3", names)
            self.assertIn("state/manifest.json", names)
            self.assertIn("files/config.json", names)
            self.assertIn("files/.runtime/logs/event.json", names)
            self.assertFalse(any("claims" in name for name in names))
            extracted = Path(self.tmp.name) / "backup.sqlite3"
            extracted.write_bytes(bundle.read("state/orch.sqlite3"))
        conn = sqlite3.connect(str(extracted))
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
        finally:
            conn.close()

    def test_backup_member_swap_after_census_fails_closed(self):
        config = self.root / "config.json"
        config.write_text('{"safe":true}\n', encoding="utf-8")
        victim = Path(self.tmp.name) / "backup-member-victim.txt"
        victim.write_text("external secret\n", encoding="utf-8")
        output = Path(self.tmp.name) / "member-race.zip"
        original = state_module._backup_members

        def swap_after_census(orch):
            members = original(orch)
            config.unlink()
            config.symlink_to(victim)
            return members

        with mock.patch.object(
            state_module,
            "_backup_members",
            side_effect=swap_after_census,
        ):
            with self.assertRaisesRegex(
                ValueError, "backup_member_unsafe:config.json"
            ):
                backup_state(self.orch, output)
        self.assertFalse(output.exists())
        self.assertTrue(config.is_symlink())
        self.assertEqual(
            victim.read_text(encoding="utf-8"), "external secret\n"
        )

    def test_backup_member_ancestor_symlink_swap_fails_closed(self):
        plans = self.root / "plans"
        plans.mkdir(mode=0o700, exist_ok=True)
        ancestor = plans / "sub"
        ancestor.mkdir(mode=0o700)
        (ancestor / "file.json").write_text(
            '{"source":"orch"}\n', encoding="utf-8"
        )
        external = Path(self.tmp.name) / "external-plans"
        external.mkdir()
        external_file = external / "file.json"
        external_file.write_text(
            '{"source":"external secret"}\n', encoding="utf-8"
        )
        output = Path(self.tmp.name) / "ancestor-race.zip"
        original = state_module._backup_members

        def swap_ancestor_after_census(orch):
            members = original(orch)
            ancestor.rename(ancestor.with_name("sub-original"))
            ancestor.symlink_to(external, target_is_directory=True)
            return members

        with mock.patch.object(
            state_module,
            "_backup_members",
            side_effect=swap_ancestor_after_census,
        ):
            with self.assertRaisesRegex(
                ValueError, "backup_member_unsafe:plans/sub/file.json"
            ):
                backup_state(self.orch, output)
        self.assertFalse(output.exists())
        self.assertEqual(
            external_file.read_text(encoding="utf-8"),
            '{"source":"external secret"}\n',
        )

    def test_backup_member_home_parent_symlink_swap_fails_closed(self):
        live_parent = Path(self.tmp.name) / "nested-live"
        root = live_parent / "orch"
        root.mkdir(parents=True, mode=0o700)
        orch = Orchestrator(root)
        plans = root / "plans"
        plans.mkdir(mode=0o700)
        source = plans / "sub"
        source.mkdir(mode=0o700)
        (source / "file.json").write_text(
            '{"source":"orch"}\n', encoding="utf-8"
        )

        external_parent = Path(self.tmp.name) / "external-parent"
        external = external_parent / "orch" / "plans" / "sub"
        external.mkdir(parents=True)
        external_file = external / "file.json"
        external_file.write_text(
            '{"source":"external secret"}\n', encoding="utf-8"
        )
        output = Path(self.tmp.name) / "home-parent-race.zip"
        moved_parent = Path(self.tmp.name) / "nested-live-original"
        original = state_module._backup_members

        def swap_home_parent_after_census(current_orch):
            members = original(current_orch)

            def swapped_members():
                live_parent.rename(moved_parent)
                live_parent.symlink_to(external_parent, target_is_directory=True)
                try:
                    yield from members
                finally:
                    live_parent.unlink()
                    moved_parent.rename(live_parent)

            return swapped_members()

        with mock.patch.object(
            state_module,
            "_backup_members",
            side_effect=swap_home_parent_after_census,
        ):
            with self.assertRaisesRegex(
                ValueError, "backup_member_unsafe:plans/sub/file.json"
            ):
                backup_state(orch, output)
        self.assertFalse(output.exists())
        self.assertEqual(
            external_file.read_text(encoding="utf-8"),
            '{"source":"external secret"}\n',
        )

    def test_backup_member_fifo_fails_closed_without_blocking(self):
        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("FIFO nonblocking open unavailable")
        config = self.root / "config.json"
        config.write_text('{"profile":"safe"}\n', encoding="utf-8")
        output = Path(self.tmp.name) / "fifo-member-backup.zip"
        original = state_module._backup_members
        swapped = False

        def swap_after_census(orch):
            nonlocal swapped
            members = original(orch)
            if not swapped:
                config.unlink()
                os.mkfifo(config)
                swapped = True
            return members

        def timeout_handler(_signum, _frame):
            raise TimeoutError("fifo_open_blocked")

        previous = signal.signal(signal.SIGALRM, timeout_handler)
        try:
            signal.alarm(2)
            with mock.patch.object(
                state_module,
                "_backup_members",
                side_effect=swap_after_census,
            ):
                with self.assertRaisesRegex(
                    ValueError, "backup_member_unsafe:config.json"
                ):
                    backup_state(self.orch, output)
            signal.alarm(0)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertFalse(output.exists())
        self.assertTrue(config.exists())

    def test_backup_output_is_symlink_safe_and_temp_name_is_exclusive(self):
        victim = Path(self.tmp.name) / "backup-victim.bin"
        victim.write_bytes(b"owner preserve")
        link = Path(self.tmp.name) / "backup-link.zip"
        link.symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "backup_output_unsafe"):
            backup_state(self.orch, link)
        self.assertEqual(victim.read_bytes(), b"owner preserve")
        self.assertTrue(link.is_symlink())

        output = Path(self.tmp.name) / "custom-backup.zip"
        predictable = output.with_name(
            output.name + f".tmp-{os.getpid()}"
        )
        predictable.symlink_to(victim)
        result = backup_state(self.orch, output)
        self.assertEqual(Path(result["path"]), output)
        self.assertEqual(victim.read_bytes(), b"owner preserve")
        self.assertTrue(predictable.is_symlink())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            verify_backup_archive(output)["status"], "VERIFIED"
        )

    def test_backup_digest_refuses_post_publish_symlink_swap(self):
        output = Path(self.tmp.name) / "swapped-backup.zip"
        victim = Path(self.tmp.name) / "backup-swap-victim.bin"
        victim.write_bytes(b"owner preserve")
        before = victim.read_bytes()
        real_replace = os.replace

        def replace_then_swap(source, destination):
            real_replace(source, destination)
            if Path(destination) == output:
                output.unlink()
                output.symlink_to(victim)

        with mock.patch(
            "orch.state.os.replace", side_effect=replace_then_swap
        ):
            with self.assertRaisesRegex(ValueError, "hash_file_unsafe"):
                backup_state(self.orch, output)
        self.assertEqual(victim.read_bytes(), before)
        self.assertTrue(output.is_symlink())

    def test_backup_verifier_accepts_current_secret_free_backup(self):
        result = backup_state(self.orch)
        verified = verify_backup_archive(Path(result["path"]))
        self.assertEqual(verified["status"], "VERIFIED")
        self.assertEqual(verified["schema_version"], 4)
        self.assertEqual(verified["compatibility"], "CURRENT")
        self.assertEqual(verified["quick_check"], ["ok"])
        self.assertEqual(verified["foreign_key_violations"], [])
        self.assertEqual(
            verified["database_sha256"],
            verified["manifest"]["database_sha256"],
        )

    def test_backup_verifier_binds_non_database_member_hashes(self):
        config = self.root / "config.json"
        config.write_text('{"profile":"safe"}\n', encoding="utf-8")
        source_path = Path(backup_state(self.orch)["path"])
        tampered = Path(self.tmp.name) / "tampered-config.zip"
        with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "files/config.json":
                    data = b'{"profile":"tampered"}\n'
                target.writestr(info, data)
        checked = verify_backup_archive(tampered)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertIn(
            "backup_file_evidence_mismatch", checked["errors"]
        )
        self.assertEqual(
            checked["file_evidence_mismatch"], "files/config.json"
        )

    def test_backup_verifier_rejects_symlink_source(self):
        source_path = Path(backup_state(self.orch)["path"])
        link = Path(self.tmp.name) / "backup-source-link.zip"
        link.symlink_to(source_path)
        checked = verify_backup_archive(link)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertEqual(checked["errors"], ["backup_not_regular_file"])
        self.assertEqual(Path(checked["path"]), link)

    def test_backup_verifier_rejects_fifo_source_without_blocking(self):
        if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
            self.skipTest("FIFO nonblocking open unavailable")
        fifo = Path(self.tmp.name) / "backup-source.fifo"
        os.mkfifo(fifo)

        def timeout_handler(_signum, _frame):
            raise TimeoutError("fifo_open_blocked")

        previous = signal.signal(signal.SIGALRM, timeout_handler)
        try:
            signal.alarm(2)
            checked = verify_backup_archive(fifo)
            signal.alarm(0)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertEqual(checked["errors"], ["backup_not_regular_file"])
        self.assertEqual(Path(checked["path"]), fifo)

    def test_restore_blocks_source_change_after_verification(self):
        source_path = Path(backup_state(self.orch)["path"])
        destination = Path(self.tmp.name) / "changed-source-restore"
        real_verify = verify_backup_archive

        def verify_then_mutate(path, **kwargs):
            result = real_verify(path, **kwargs)
            with Path(path).open("ab") as handle:
                handle.write(b"changed-after-verification")
            return result

        with mock.patch.object(
            state_module,
            "verify_backup_archive",
            side_effect=verify_then_mutate,
        ):
            with self.assertRaisesRegex(
                ValueError, "backup_source_changed_after_verification"
            ):
                restore_backup_archive(source_path, destination)
        self.assertFalse(destination.exists())

    def test_backup_verifier_rejects_manifest_database_hash_mismatch(self):
        source_path = Path(backup_state(self.orch)["path"])
        tampered = self.root / "tampered-hash.zip"
        with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "state/manifest.json":
                    manifest = json.loads(data.decode("utf-8"))
                    manifest["database_sha256"] = "0" * 64
                    data = (
                        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
                    ).encode("utf-8")
                target.writestr(info, data)
        checked = verify_backup_archive(tampered)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertIn("backup_database_hash_mismatch", checked["errors"])

    def test_backup_verifier_rejects_path_traversal_and_claim_secret(self):
        source_path = Path(backup_state(self.orch)["path"])

        traversal = self.root / "traversal.zip"
        traversal.write_bytes(source_path.read_bytes())
        with zipfile.ZipFile(traversal, "a") as archive:
            archive.writestr("../escape.txt", "blocked")
        checked = verify_backup_archive(traversal)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertIn("unsafe_backup_member", checked["errors"])

        secret = self.root / "secret-member.zip"
        secret.write_bytes(source_path.read_bytes())
        with zipfile.ZipFile(secret, "a") as archive:
            archive.writestr(
                "files/.runtime/claims/forbidden.json", "{}"
            )
        checked = verify_backup_archive(secret)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertIn("forbidden_secret_member", checked["errors"])

    def test_backup_verifier_rejects_unknown_files_member(self):
        source_path = Path(backup_state(self.orch)["path"])
        extra = self.root / "unknown-member.zip"
        extra.write_bytes(source_path.read_bytes())
        with zipfile.ZipFile(extra, "a") as archive:
            archive.writestr("files/manual-owner-note.json", "{}")
        checked = verify_backup_archive(extra)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertIn("backup_member_not_restorable", checked["errors"])
        self.assertEqual(
            checked["nonrestorable_members"],
            ["files/manual-owner-note.json"],
        )

    def test_backup_verifier_rejects_noncanonical_file_member(self):
        source_path = Path(backup_state(self.orch)["path"])
        legacy = Path(self.tmp.name) / "legacy-noncanonical-member.zip"
        with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(
            legacy, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "state/manifest.json":
                    manifest = json.loads(data.decode("utf-8"))
                    manifest.pop("file_evidence")
                    data = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
                target.writestr(info, data)

        self.assertEqual(verify_backup_archive(legacy)["status"], "VERIFIED")
        with zipfile.ZipFile(legacy, "a") as archive:
            archive.writestr("files/plans/.", b"not a plan file")

        checked = verify_backup_archive(legacy)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertIn("unsafe_backup_member", checked["errors"])
        self.assertIn("files/plans/.", checked["unsafe_members"])

    def test_backup_verifier_rejects_invalid_zip(self):
        invalid = self.root / "invalid-backup.zip"
        invalid.write_bytes(b"not-a-zip")
        checked = verify_backup_archive(invalid)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertEqual(checked["errors"], ["backup_zip_invalid"])

    def test_restore_backup_publishes_fresh_home_atomically(self):
        (self.root / "config.json").write_text('{"schema_version":1}\n')
        (self.root / "dispatcher-prompt.txt").write_text("stale derived prompt\n")
        evidence = self.orch.runtime / "logs" / "restore-fixture.json"
        evidence.write_text('{"restored":true}\n')
        archive = Path(backup_state(self.orch)["path"])
        destination = Path(self.tmp.name) / "restored-home"
        parent = destination.parent
        os.chmod(parent, 0o755)
        parent_mode = parent.stat().st_mode & 0o777

        result = restore_backup_archive(archive, destination)
        self.assertEqual(result["status"], "RESTORED")
        self.assertEqual(result["health"]["status"], "READY")
        self.assertEqual(result["source_schema_version"], 4)
        self.assertEqual(result["restored_schema_version"], 4)
        self.assertEqual(parent.stat().st_mode & 0o777, parent_mode)
        self.assertTrue((destination / ".runtime" / "orch.sqlite3").is_file())
        self.assertEqual(
            (destination / ".runtime" / "logs" / "restore-fixture.json").read_text(),
            '{"restored":true}\n',
        )
        self.assertTrue((destination / "config.json").is_file())
        self.assertFalse((destination / "dispatcher-prompt.txt").exists())
        claims = destination / ".runtime" / "claims"
        self.assertTrue(claims.is_dir())
        self.assertEqual(list(claims.iterdir()), [])
        receipt = destination / "restore-receipt.json"
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        receipt_data = json.loads(receipt.read_text())
        self.assertFalse(receipt_data["dispatcher_prompt_restored"])
        self.assertEqual(
            receipt_data["dispatcher_prompt_action"],
            "regenerate_with_orch_dispatcher_render",
        )
        leftovers = list(parent.glob(f".{destination.name}.restore-*"))
        self.assertEqual(leftovers, [])

    def test_restore_backup_refuses_existing_destination(self):
        archive = Path(backup_state(self.orch)["path"])
        destination = Path(self.tmp.name) / "existing-home"
        destination.mkdir()
        marker = destination / "owner.txt"
        marker.write_text("keep\n")
        with self.assertRaisesRegex(ValueError, "restore_destination_exists"):
            restore_backup_archive(archive, destination)
        self.assertEqual(marker.read_text(), "keep\n")

    def test_restore_backup_cleans_staging_if_atomic_publish_fails(self):
        archive = Path(backup_state(self.orch)["path"])
        destination = Path(self.tmp.name) / "failed-restore"
        with mock.patch("orch.state.os.replace", side_effect=OSError("synthetic rename failure")):
            with self.assertRaisesRegex(OSError, "synthetic rename failure"):
                restore_backup_archive(archive, destination)
        self.assertFalse(destination.exists())
        leftovers = list(destination.parent.glob(f".{destination.name}.restore-*"))
        self.assertEqual(leftovers, [])

    def test_restore_receipt_write_failure_never_publishes_destination(self):
        archive = Path(backup_state(self.orch)["path"])
        destination = Path(self.tmp.name) / "receipt-write-failed"
        with mock.patch(
            "orch.state.atomic_write_json",
            side_effect=OSError("synthetic receipt write failure"),
        ):
            with self.assertRaisesRegex(
                OSError, "synthetic receipt write failure"
            ):
                restore_backup_archive(archive, destination)
        self.assertFalse(destination.exists())
        leftovers = list(
            destination.parent.glob(f".{destination.name}.restore-*")
        )
        self.assertEqual(leftovers, [])

    def test_restore_backup_migrates_verified_schema_v2_archive(self):
        workspace = Path(self.tmp.name) / "legacy-restore-workspace"
        workspace.mkdir()
        legacy_db = Path(self.tmp.name) / "legacy-v2.sqlite3"
        db = sqlite3.connect(str(legacy_db))
        try:
            db.execute(
                "CREATE TABLE tasks ("
                "task_id TEXT PRIMARY KEY, plan_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,"
                "status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            payload = {
                "id": "LEGACY-RESTORE",
                "project_id": "legacy-project",
                "goal": "legacy restore",
                "workspace": str(workspace),
                "dependencies": [],
                "allowed_paths": ["out.json"],
                "protected_paths": {},
                "checks": [],
                "publication": {"kind": "none"},
                "max_attempts": 2,
            }
            db.execute(
                "INSERT INTO tasks(task_id,plan_revision,ordinal,status,payload_json,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "LEGACY-RESTORE", "legacy-v2", 0, "PLANNED",
                    json.dumps(payload), "2026-01-01T00:00:00Z",
                ),
            )
            db.execute("PRAGMA user_version=2")
            db.commit()
        finally:
            db.close()
        digest = hashlib.sha256(legacy_db.read_bytes()).hexdigest()
        archive = Path(self.tmp.name) / "legacy-v2.zip"
        manifest = {
            "schema_version": 2,
            "created_at": "2026-01-01T00:00:00+00:00",
            "source_root": "/legacy/orch",
            "database_sha256": digest,
            "excluded_secret_classes": [
                "claims", "capability_files", "provider_credentials",
            ],
        }
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(legacy_db, "state/orch.sqlite3")
            bundle.writestr(
                "state/manifest.json",
                json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            )
        checked = verify_backup_archive(archive)
        self.assertEqual(checked["status"], "VERIFIED")
        self.assertEqual(checked["compatibility"], "UPGRADE_REQUIRED")

        destination = Path(self.tmp.name) / "restored-v4"
        restored = restore_backup_archive(archive, destination)
        self.assertEqual(restored["status"], "RESTORED")
        self.assertEqual(restored["source_schema_version"], 2)
        self.assertEqual(restored["restored_schema_version"], 4)
        migrated = Orchestrator(destination)
        self.assertEqual(check_state(migrated)["schema_version"], 4)
        history = migration_history(migrated)["migrations"]
        self.assertEqual(history[-1]["from_version"], 2)
        self.assertEqual(
            migrated.status()["tasks"][0]["task_id"], "LEGACY-RESTORE"
        )

    def _replacement_fixture(self, name):
        base = Path(self.tmp.name) / name
        base.mkdir()
        live = base / "live-home"
        source = base / "source-home"
        live.mkdir()
        source.mkdir()
        live_orch = Orchestrator(live)
        source_orch = Orchestrator(source)
        (live_orch.runtime / "logs" / "old.json").write_text('{"old":true}\n')
        (source_orch.runtime / "logs" / "new.json").write_text('{"new":true}\n')
        archive = Path(backup_state(source_orch)["path"])
        return live, source, archive

    def test_replace_backup_keeps_rollback_until_explicit_finalize(self):
        live, _source, archive = self._replacement_fixture("replace-happy")
        result = replace_home_from_backup(archive, live)
        self.assertEqual(result["status"], "REPLACED_ROLLBACK_AVAILABLE")
        rollback = Path(result["rollback_home"])
        journal = Path(result["journal"])
        self.assertTrue(rollback.is_dir())
        self.assertTrue(journal.is_file())
        self.assertTrue((live / ".runtime" / "logs" / "new.json").is_file())
        self.assertFalse((live / ".runtime" / "logs" / "old.json").exists())
        self.assertTrue((rollback / ".runtime" / "logs" / "old.json").is_file())

        observed = reconcile_home_replacement(live)
        self.assertEqual(observed["status"], "REPLACED_ROLLBACK_AVAILABLE")
        finished = reconcile_home_replacement(live, finalize=True)
        self.assertEqual(finished["status"], "COMPLETE")
        self.assertFalse(rollback.exists())
        self.assertFalse(journal.exists())
        self.assertTrue((live / "replacement-receipt.json").is_file())
        self.assertEqual(check_state(Orchestrator(live))["status"], "READY")

    def test_replace_backup_refuses_source_style_or_nonstandalone_home(self):
        live, _source, archive = self._replacement_fixture("replace-foreign")
        (live / "owner-source.py").write_text("keep\n")
        with self.assertRaisesRegex(ValueError, "replacement_home_not_standalone"):
            replace_home_from_backup(archive, live)
        self.assertEqual((live / "owner-source.py").read_text(), "keep\n")

    def test_replace_backup_refuses_nonterminal_queued_work(self):
        live, _source, archive = self._replacement_fixture("replace-queued")
        live_orch = Orchestrator(live)
        workspace = Path(self.tmp.name) / "replace-queued-workspace"
        workspace.mkdir()
        plans_dir = live / "plans"
        plans_dir.mkdir(mode=0o700)
        plan = {
            "schema_version": 1,
            "plan_revision": "replace-queued-v1",
            "tasks": [{
                "id": "REPLACE-QUEUED",
                "goal": "queued",
                "workspace": str(workspace),
                "dependencies": [],
                "allowed_paths": ["out.json"],
                "protected_paths": {},
                "checks": [],
                "publication": {"kind": "none"},
                "max_attempts": 2,
            }],
        }
        path = plans_dir / "replace-queued.json"
        path.write_text(json.dumps(plan))
        os.chmod(path, 0o600)
        live_orch.load_plan(path)
        self.assertEqual(check_state(live_orch)["status"], "READY")
        self.assertEqual(live_orch.reconcile()["status"], "CLEAN")
        self.assertEqual(recovery_inspect(live_orch)["status"], "CLEAN")
        with self.assertRaisesRegex(
            ValueError, "replacement_live_nonterminal_tasks:REPLACE-QUEUED"
        ):
            replace_home_from_backup(archive, live)
        self.assertTrue((live / ".runtime" / "orch.sqlite3").is_file())

    def test_replace_backup_refuses_paused_live_state(self):
        live, _source, archive = self._replacement_fixture("replace-paused")
        live_orch = Orchestrator(live)
        live_orch.pause("operator maintenance")
        self.assertEqual(check_state(live_orch)["status"], "READY")
        with self.assertRaisesRegex(ValueError, "replacement_live_pause_present"):
            replace_home_from_backup(archive, live)

    def test_replace_backup_refuses_active_live_state(self):
        live, _source, archive = self._replacement_fixture("replace-active")
        live_orch = Orchestrator(live)
        workspace = Path(self.tmp.name) / "replace-active-workspace"
        workspace.mkdir()
        plan = {
            "schema_version": 1,
            "plan_revision": "replace-active-v1",
            "tasks": [{
                "id": "REPLACE-ACTIVE",
                "goal": "active",
                "workspace": str(workspace),
                "dependencies": [],
                "allowed_paths": ["out.json"],
                "protected_paths": {},
                "checks": [],
                "publication": {"kind": "none"},
                "max_attempts": 2,
            }],
        }
        plans_dir = live / "plans"
        plans_dir.mkdir(mode=0o700)
        path = plans_dir / "replace-active.json"
        path.write_text(json.dumps(plan))
        os.chmod(path, 0o600)
        live_orch.load_plan(path)
        live_orch.claim("worker")
        with self.assertRaisesRegex(ValueError, "replacement_live_state_not_ready"):
            replace_home_from_backup(archive, live)

    def test_replace_reconcile_resumes_after_second_rename_failure(self):
        live, _source, archive = self._replacement_fixture("replace-resume")
        original = state_module._replacement_rename
        calls = {"count": 0}

        def flaky(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic new-home rename failure")
            return original(source, destination)

        with mock.patch.object(state_module, "_replacement_rename", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "synthetic new-home rename failure"):
                replace_home_from_backup(archive, live)

        self.assertFalse(live.exists())
        observed = reconcile_home_replacement(live)
        self.assertEqual(observed["status"], "OLD_MOVED")
        self.assertTrue(observed["resume_available"])
        resumed = reconcile_home_replacement(live, resume=True)
        self.assertEqual(resumed["status"], "REPLACED_ROLLBACK_AVAILABLE")
        self.assertTrue((live / ".runtime" / "logs" / "new.json").is_file())
        self.assertEqual(
            reconcile_home_replacement(live, finalize=True)["status"],
            "COMPLETE",
        )

    def test_replace_reconcile_adopts_old_moved_after_journal_gap(self):
        live, _source, archive = self._replacement_fixture("replace-gap")
        original_write = state_module._replacement_write
        calls = {"count": 0}

        def flaky(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic journal gap")
            return original_write(path, data)

        with mock.patch.object(state_module, "_replacement_write", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "synthetic journal gap"):
                replace_home_from_backup(archive, live)

        self.assertFalse(live.exists())
        adopted = reconcile_home_replacement(live)
        self.assertEqual(adopted["status"], "OLD_MOVED")
        resumed = reconcile_home_replacement(live, resume=True)
        self.assertEqual(resumed["status"], "REPLACED_ROLLBACK_AVAILABLE")
        self.assertEqual(
            reconcile_home_replacement(live, finalize=True)["status"],
            "COMPLETE",
        )

    def test_replace_finalize_recovers_after_discard_journal_gap(self):
        live, _source, archive = self._replacement_fixture("replace-finalize-gap")
        replace_home_from_backup(archive, live)
        original_write = state_module._replacement_write
        calls = {"count": 0}

        def flaky(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic finalize journal gap")
            return original_write(path, data)

        with mock.patch.object(state_module, "_replacement_write", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "synthetic finalize journal gap"):
                reconcile_home_replacement(live, finalize=True)

        observed = reconcile_home_replacement(live)
        self.assertEqual(observed["status"], "FINALIZE_PENDING_DELETE")
        self.assertTrue(observed["finalize_available"])
        finished = reconcile_home_replacement(live, finalize=True)
        self.assertEqual(finished["status"], "COMPLETE")
        self.assertTrue((live / "replacement-receipt.json").is_file())

    def test_replace_finalize_preserves_replaced_discard_after_crash(self):
        live, _source, archive = self._replacement_fixture(
            "replace-finalize-discard-swap"
        )
        replaced = replace_home_from_backup(archive, live)
        journal = Path(replaced["journal"])
        original_write = state_module._replacement_write
        calls = {"count": 0}

        def fail_after_discard_rename(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic finalize identity gap")
            return original_write(path, data)

        with mock.patch.object(
            state_module,
            "_replacement_write",
            side_effect=fail_after_discard_rename,
        ):
            with self.assertRaisesRegex(
                OSError, "synthetic finalize identity gap"
            ):
                reconcile_home_replacement(live, finalize=True)

        data = json.loads(journal.read_text(encoding="utf-8"))
        discard = Path(data["discard_home"])
        original_discard = discard.with_name(discard.name + "-original")
        discard.rename(original_discard)
        discard.mkdir()
        owner = discard / "owner.txt"
        owner.write_text("preserve owner bytes\n", encoding="utf-8")

        blocked = reconcile_home_replacement(live)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(
            blocked["reason"], "replacement_discard_identity_mismatch"
        )
        self.assertEqual(
            owner.read_text(encoding="utf-8"), "preserve owner bytes\n"
        )
        self.assertTrue(original_discard.is_dir())
        self.assertTrue(journal.is_file())

    def test_replace_finalize_blocks_dangling_discard_symlink(self):
        live, _source, archive = self._replacement_fixture(
            "replace-finalize-dangling-discard"
        )
        replaced = replace_home_from_backup(archive, live)
        journal = Path(replaced["journal"])
        with mock.patch.object(
            state_module,
            "_delete_bound_replacement_discard",
            side_effect=OSError("synthetic delete interruption"),
        ):
            with self.assertRaisesRegex(
                OSError, "synthetic delete interruption"
            ):
                reconcile_home_replacement(live, finalize=True)

        data = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "FINALIZE_PENDING_DELETE")
        discard = Path(data["discard_home"])
        original_discard = discard.with_name(discard.name + "-original")
        discard.rename(original_discard)
        missing = discard.with_name(discard.name + "-missing")
        discard.symlink_to(missing, target_is_directory=True)

        blocked = reconcile_home_replacement(live, finalize=True)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(
            blocked["reason"], "replacement_discard_identity_mismatch"
        )
        self.assertTrue(discard.is_symlink())
        self.assertFalse(discard.exists())
        self.assertTrue(original_discard.is_dir())
        self.assertTrue(journal.is_file())

    def test_replace_rollback_restores_old_home_and_retains_new_copy(self):
        live, _source, archive = self._replacement_fixture("rollback-happy")
        replaced = replace_home_from_backup(archive, live)
        journal = Path(replaced["journal"])
        result = reconcile_home_replacement(live, rollback=True)
        self.assertEqual(result["status"], "ROLLED_BACK_FORWARD_COPY_AVAILABLE")
        failed = Path(result["failed_home"])
        self.assertTrue((live / ".runtime" / "logs" / "old.json").is_file())
        self.assertFalse((live / ".runtime" / "logs" / "new.json").exists())
        self.assertTrue((failed / ".runtime" / "logs" / "new.json").is_file())
        self.assertTrue(journal.is_file())

        finished = reconcile_home_replacement(live, finalize=True)
        self.assertEqual(finished["status"], "COMPLETE")
        self.assertEqual(finished["outcome"], "ROLLED_BACK")
        self.assertFalse(failed.exists())
        self.assertFalse(journal.exists())
        receipt = json.loads((live / "replacement-receipt.json").read_text())
        self.assertEqual(receipt["outcome"], "ROLLED_BACK")
        self.assertEqual(check_state(Orchestrator(live))["status"], "READY")

    def test_replace_rollback_adopts_new_moved_after_journal_gap(self):
        live, _source, archive = self._replacement_fixture("rollback-gap")
        replace_home_from_backup(archive, live)
        original_write = state_module._replacement_write
        calls = {"count": 0}

        def flaky(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic rollback journal gap")
            return original_write(path, data)

        with mock.patch.object(state_module, "_replacement_write", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "synthetic rollback journal gap"):
                reconcile_home_replacement(live, rollback=True)

        self.assertFalse(live.exists())
        adopted = reconcile_home_replacement(live)
        self.assertEqual(adopted["status"], "ROLLBACK_NEW_MOVED")
        self.assertTrue(adopted["rollback_resume_available"])
        resumed = reconcile_home_replacement(live, rollback=True)
        self.assertEqual(resumed["status"], "ROLLED_BACK_FORWARD_COPY_AVAILABLE")
        self.assertTrue((live / ".runtime" / "logs" / "old.json").is_file())
        self.assertEqual(
            reconcile_home_replacement(live, finalize=True)["status"],
            "COMPLETE",
        )

    def test_replace_rollback_resumes_after_old_restore_rename_failure(self):
        live, _source, archive = self._replacement_fixture("rollback-resume")
        replace_home_from_backup(archive, live)
        original = state_module._replacement_rename
        calls = {"count": 0}

        def flaky(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic old-home restore failure")
            return original(source, destination)

        with mock.patch.object(state_module, "_replacement_rename", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "synthetic old-home restore failure"):
                reconcile_home_replacement(live, rollback=True)

        self.assertFalse(live.exists())
        observed = reconcile_home_replacement(live)
        self.assertEqual(observed["status"], "ROLLBACK_NEW_MOVED")
        resumed = reconcile_home_replacement(live, rollback=True)
        self.assertEqual(resumed["status"], "ROLLED_BACK_FORWARD_COPY_AVAILABLE")
        self.assertTrue((live / ".runtime" / "logs" / "old.json").is_file())
        self.assertEqual(
            reconcile_home_replacement(live, finalize=True)["status"],
            "COMPLETE",
        )

    def test_replace_rollback_finalize_recovers_discard_journal_gap(self):
        live, _source, archive = self._replacement_fixture("rollback-finalize-gap")
        replace_home_from_backup(archive, live)
        reconcile_home_replacement(live, rollback=True)
        original_write = state_module._replacement_write
        calls = {"count": 0}

        def flaky(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic rollback finalize gap")
            return original_write(path, data)

        with mock.patch.object(state_module, "_replacement_write", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "synthetic rollback finalize gap"):
                reconcile_home_replacement(live, finalize=True)

        observed = reconcile_home_replacement(live)
        self.assertEqual(observed["status"], "ROLLBACK_FINALIZE_PENDING_DELETE")
        self.assertTrue(observed["finalize_available"])
        finished = reconcile_home_replacement(live, finalize=True)
        self.assertEqual(finished["status"], "COMPLETE")
        self.assertEqual(finished["outcome"], "ROLLED_BACK")

    def test_replace_rollback_finalize_preserves_replaced_discard_after_crash(self):
        live, _source, archive = self._replacement_fixture(
            "rollback-finalize-discard-swap"
        )
        replaced = replace_home_from_backup(archive, live)
        journal = Path(replaced["journal"])
        reconcile_home_replacement(live, rollback=True)
        original_write = state_module._replacement_write
        calls = {"count": 0}

        def fail_after_discard_rename(path, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic rollback finalize identity gap")
            return original_write(path, data)

        with mock.patch.object(
            state_module,
            "_replacement_write",
            side_effect=fail_after_discard_rename,
        ):
            with self.assertRaisesRegex(
                OSError, "synthetic rollback finalize identity gap"
            ):
                reconcile_home_replacement(live, finalize=True)

        data = json.loads(journal.read_text(encoding="utf-8"))
        discard = Path(data["discard_home"])
        original_discard = discard.with_name(discard.name + "-original")
        discard.rename(original_discard)
        discard.mkdir()
        owner = discard / "owner.txt"
        owner.write_text("preserve owner bytes\n", encoding="utf-8")

        blocked = reconcile_home_replacement(live)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(
            blocked["reason"], "replacement_discard_identity_mismatch"
        )
        self.assertEqual(
            owner.read_text(encoding="utf-8"), "preserve owner bytes\n"
        )
        self.assertTrue(original_discard.is_dir())
        self.assertTrue(journal.is_file())

    def test_replace_rollback_finalize_blocks_dangling_discard_symlink(self):
        live, _source, archive = self._replacement_fixture(
            "rollback-finalize-dangling-discard"
        )
        replaced = replace_home_from_backup(archive, live)
        journal = Path(replaced["journal"])
        reconcile_home_replacement(live, rollback=True)
        with mock.patch.object(
            state_module,
            "_delete_bound_replacement_discard",
            side_effect=OSError("synthetic rollback delete interruption"),
        ):
            with self.assertRaisesRegex(
                OSError, "synthetic rollback delete interruption"
            ):
                reconcile_home_replacement(live, finalize=True)

        data = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(
            data["status"], "ROLLBACK_FINALIZE_PENDING_DELETE"
        )
        discard = Path(data["discard_home"])
        original_discard = discard.with_name(discard.name + "-original")
        discard.rename(original_discard)
        missing = discard.with_name(discard.name + "-missing")
        discard.symlink_to(missing, target_is_directory=True)

        blocked = reconcile_home_replacement(live, finalize=True)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(
            blocked["reason"], "replacement_discard_identity_mismatch"
        )
        self.assertTrue(discard.is_symlink())
        self.assertFalse(discard.exists())
        self.assertTrue(original_discard.is_dir())
        self.assertTrue(journal.is_file())

    def test_replacement_recovery_inspect_is_read_only_for_new_active(self):
        live, _source, archive = self._replacement_fixture("inspect-new-active")
        result = replace_home_from_backup(archive, live)
        journal = Path(result["journal"])
        before = journal.read_bytes()
        inspected = inspect_home_replacement(live)
        after = journal.read_bytes()
        self.assertEqual(inspected["status"], "ATTENTION")
        self.assertEqual(
            inspected["classification"], "REPLACEMENT_ROLLBACK_AVAILABLE"
        )
        self.assertIn("--rollback", " ".join(inspected["safe_next_steps"]))
        self.assertIn("--finalize", " ".join(inspected["safe_next_steps"]))
        self.assertEqual(before, after)
        self.assertFalse(inspected["automatic_action"])
        reconcile_home_replacement(live, finalize=True)

    def test_replacement_journal_read_is_bounded_and_mode_bound(self):
        live, _source, archive = self._replacement_fixture(
            "inspect-journal-bounds"
        )
        result = replace_home_from_backup(archive, live)
        journal = Path(result["journal"])
        journal.write_bytes(
            b"{" + b"x" * state_module.REPLACEMENT_JOURNAL_MAX_BYTES
        )
        os.chmod(journal, 0o600)
        inspected = inspect_home_replacement(live)
        self.assertEqual(inspected["status"], "BLOCKED")
        self.assertEqual(
            inspected["classification"], "REPLACEMENT_JOURNAL_INVALID"
        )
        self.assertEqual(inspected["reason"], "replacement_journal_too_large")

        live2, _source2, archive2 = self._replacement_fixture(
            "inspect-journal-mode"
        )
        result2 = replace_home_from_backup(archive2, live2)
        journal2 = Path(result2["journal"])
        os.chmod(journal2, 0o644)
        inspected2 = inspect_home_replacement(live2)
        self.assertEqual(inspected2["status"], "BLOCKED")
        self.assertEqual(
            inspected2["reason"], "replacement_journal_mode_unsafe"
        )

    def test_replacement_recovery_inspect_sees_old_moved_without_creating_dest(self):
        live, _source, archive = self._replacement_fixture("inspect-old-moved")
        original = state_module._replacement_rename
        calls = {"count": 0}

        def flaky(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic activation failure")
            return original(source, destination)

        with mock.patch.object(state_module, "_replacement_rename", side_effect=flaky):
            with self.assertRaisesRegex(OSError, "synthetic activation failure"):
                replace_home_from_backup(archive, live)
        self.assertFalse(live.exists())
        journal = Path(state_module._replacement_journal_path(live))
        before = journal.read_bytes()
        inspected = inspect_home_replacement(live)
        self.assertEqual(inspected["classification"], "REPLACEMENT_OLD_MOVED")
        self.assertIn("--resume", inspected["safe_next_steps"][0])
        self.assertFalse(live.exists())
        self.assertEqual(before, journal.read_bytes())
        reconcile_home_replacement(live, resume=True)
        reconcile_home_replacement(live, finalize=True)

    def test_replacement_recovery_inspect_tracks_rolled_back_forward_copy(self):
        live, _source, archive = self._replacement_fixture("inspect-rolled-back")
        replace_home_from_backup(archive, live)
        reconcile_home_replacement(live, rollback=True)
        inspected = inspect_home_replacement(live)
        self.assertEqual(
            inspected["classification"], "ROLLED_BACK_FORWARD_COPY_AVAILABLE"
        )
        self.assertTrue(inspected["observed"]["destination_is_old"])
        self.assertTrue(inspected["observed"]["failed_is_new"])
        self.assertIn("--finalize", inspected["safe_next_steps"][0])
        reconcile_home_replacement(live, finalize=True)

    def test_replacement_initial_journal_failure_cleans_prepared_home(self):
        live, _source, archive = self._replacement_fixture("initial-journal-fail")
        with mock.patch.object(
            state_module, "_replacement_write",
            side_effect=OSError("synthetic initial journal failure"),
        ):
            with self.assertRaisesRegex(OSError, "synthetic initial journal failure"):
                replace_home_from_backup(archive, live)
        inspected = inspect_home_replacement(live)
        self.assertEqual(inspected["status"], "CLEAN")
        self.assertEqual(inspected["classification"], "NO_REPLACEMENT")
        self.assertEqual(inspected["artifact_census"]["artifacts"], [])
        self.assertTrue((live / ".runtime" / "logs" / "old.json").is_file())

    def test_replacement_inspect_reports_orphan_sibling_without_journal(self):
        live, _source, _archive = self._replacement_fixture("orphan-census")
        orphan = live.parent / f".{live.name}.rollback-orphan"
        orphan.mkdir()
        (orphan / "state.bin").write_bytes(b"x" * 37)
        inspected = inspect_home_replacement(live)
        self.assertEqual(inspected["status"], "ATTENTION")
        self.assertEqual(
            inspected["classification"], "ORPHAN_REPLACEMENT_ARTIFACTS"
        )
        self.assertTrue(orphan.exists())
        census = inspected["artifact_census"]
        self.assertEqual(census["status"], "ATTENTION")
        self.assertGreaterEqual(census["bytes"], 37)
        self.assertEqual(
            Path(census["unmanaged"][0]["path"]).resolve(), orphan.resolve()
        )
        self.assertEqual(inspected["safe_next_steps"], [])

    def test_replacement_inspect_exposes_unmanaged_sibling_with_active_journal(self):
        live, _source, archive = self._replacement_fixture("active-extra-census")
        result = replace_home_from_backup(archive, live)
        extra = live.parent / f".{live.name}.failed-orphan"
        extra.mkdir()
        (extra / "extra.bin").write_bytes(b"extra")
        inspected = inspect_home_replacement(live)
        self.assertEqual(
            inspected["classification"], "REPLACEMENT_ROLLBACK_AVAILABLE"
        )
        census = inspected["artifact_census"]
        self.assertEqual(census["status"], "ATTENTION")
        unmanaged_paths = {item["path"] for item in census["unmanaged"]}
        self.assertIn(str(extra.resolve()), unmanaged_paths)
        referenced_paths = {
            item["path"] for item in census["artifacts"]
            if item["status"] == "REFERENCED"
        }
        self.assertIn(result["rollback_home"], referenced_paths)
        reconcile_home_replacement(live, finalize=True)

    def test_backup_refuses_active_writer(self):
        workspace = Path(self.tmp.name) / "ws"; workspace.mkdir()
        plan = {
            "schema_version": 1,
            "plan_revision": "active-v1",
            "tasks": [{
                "id": "ACTIVE-1", "goal": "fixture", "workspace": str(workspace),
                "dependencies": [], "allowed_paths": ["result.json"], "protected_paths": {},
                "checks": [], "required_review": False, "owner_acceptance": False,
                "publication": {"kind": "none"}, "max_attempts": 2,
            }],
        }
        path = self.root / "plan.json"; path.write_text(json.dumps(plan))
        self.orch.load_plan(path); self.orch.claim("worker")
        with self.assertRaisesRegex(ValueError, "active_runs_present"):
            backup_state(self.orch)
        with self.assertRaisesRegex(ValueError, "active_runs_present"):
            prune_capabilities(self.orch)

    def test_schema_v2_tasks_are_migrated_to_project_queue_v3(self):
        other = Path(self.tmp.name) / "legacy"
        runtime = other / ".runtime"
        runtime.mkdir(parents=True)
        workspace = Path(self.tmp.name) / "legacy-workspace"
        workspace.mkdir()
        db = sqlite3.connect(str(runtime / "orch.sqlite3"))
        try:
            db.execute(
                "CREATE TABLE tasks ("
                "task_id TEXT PRIMARY KEY, plan_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,"
                "status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            payload = {
                "id": "LEGACY-1", "project_id": "legacy-project", "goal": "legacy",
                "workspace": str(workspace), "dependencies": [], "allowed_paths": ["out.json"],
                "protected_paths": {}, "checks": [], "publication": {"kind": "none"},
            }
            db.execute(
                "INSERT INTO tasks(task_id,plan_revision,ordinal,status,payload_json,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                ("LEGACY-1", "legacy-v2", 0, "PLANNED", json.dumps(payload), "2026-01-01T00:00:00Z"),
            )
            db.execute("PRAGMA user_version=2")
            db.commit()
        finally:
            db.close()
        migrated = Orchestrator(other)
        result = check_state(migrated)
        self.assertEqual(result["schema_version"], 4)
        with migrated.connect() as conn:
            row = conn.execute(
                "SELECT project_id,writer_key,queue_seq FROM tasks WHERE task_id='LEGACY-1'"
            ).fetchone()
        self.assertEqual(row["project_id"], "legacy-project")
        self.assertTrue(row["writer_key"].startswith("workspace:"))
        self.assertEqual(row["queue_seq"], 1)
        history = migration_history(migrated)
        self.assertEqual(history["migrations"][-1]["from_version"], 2)
        self.assertEqual(history["migrations"][-1]["details"]["kind"], "transactional_upgrade")

    def test_schema_v3_adds_durable_claim_git_head_column(self):
        other = Path(self.tmp.name) / "legacy-v3"
        runtime = other / ".runtime"
        runtime.mkdir(parents=True)
        db_path = runtime / "orch.sqlite3"
        db = sqlite3.connect(str(db_path))
        try:
            db.execute(
                "CREATE TABLE runs ("
                "run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                "attempt INTEGER NOT NULL, worker_id TEXT NOT NULL, "
                "state TEXT NOT NULL, lease_token TEXT NOT NULL, "
                "started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, "
                "submitted_at TEXT, snapshot_id TEXT, receipt_json TEXT, "
                "verify_status TEXT, review_status TEXT, feedback_json TEXT, "
                "completed_at TEXT, error TEXT)"
            )
            db.execute("PRAGMA user_version=3")
            db.commit()
        finally:
            db.close()

        migrated = Orchestrator(other)
        self.assertEqual(check_state(migrated)["schema_version"], 4)
        with migrated.connect() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(runs)").fetchall()
            }
        self.assertIn("claim_git_head", columns)
        history = migration_history(migrated)["migrations"]
        self.assertEqual(history[-1]["version"], 4)
        self.assertEqual(history[-1]["from_version"], 3)
        self.assertTrue(history[-1]["details"]["claim_git_head_added"])

    def test_failed_schema_upgrade_rolls_back_all_task_ddl(self):
        other = Path(self.tmp.name) / "broken-upgrade"
        runtime = other / ".runtime"
        runtime.mkdir(parents=True)
        workspace = Path(self.tmp.name) / "broken-workspace"
        workspace.mkdir()
        db_path = runtime / "orch.sqlite3"
        db = sqlite3.connect(str(db_path))
        try:
            db.execute(
                "CREATE TABLE tasks ("
                "task_id TEXT PRIMARY KEY, plan_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,"
                "status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            payload = {
                "id": "BROKEN-1", "goal": "broken", "workspace": str(workspace),
                "writer_key": "invalid writer key!", "dependencies": [],
                "allowed_paths": ["out.json"], "protected_paths": {}, "checks": [],
                "publication": {"kind": "none"},
            }
            db.execute(
                "INSERT INTO tasks(task_id,plan_revision,ordinal,status,payload_json,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                ("BROKEN-1", "legacy-v2", 0, "PLANNED", json.dumps(payload), "2026-01-01T00:00:00Z"),
            )
            db.execute("PRAGMA user_version=2")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "invalid_writer_key"):
            Orchestrator(other)
        db = sqlite3.connect(str(db_path))
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            self.assertNotIn("project_id", columns)
            self.assertNotIn("writer_key", columns)
            self.assertNotIn("queue_seq", columns)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 0)
        finally:
            db.close()

    def _terminal_retention_fixture(self, task_id="RETENTION-RACE"):
        workspace = Path(self.tmp.name) / (task_id.lower() + "-ws")
        workspace.mkdir()
        plan = {
            "schema_version": 1,
            "plan_revision": task_id.lower() + "-plan",
            "tasks": [{
                "id": task_id,
                "goal": "retention race fixture",
                "workspace": str(workspace),
                "dependencies": [],
                "allowed_paths": ["out.json"],
                "protected_paths": {},
                "checks": [],
                "publication": {"kind": "none"},
                "max_attempts": 2,
            }],
        }
        path = self.root / (task_id.lower() + "-plan.json")
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.orch.load_plan(path)
        claim = self.orch.claim("retention-worker")
        self.orch.abort(claim["run_id"], "terminal fixture", retry=False)
        logs = self.orch.runtime / "logs"
        receipts = self.orch.runtime / "worker_receipts"
        exports = self.orch.runtime / "review_exports"
        log = logs / f"{claim['run_id']}-scope.json"
        receipt = receipts / f"{claim['run_id']}.json"
        export = exports / claim["run_id"]
        log.write_text('{"old":true}\n', encoding="utf-8")
        receipt.write_text('{"old":true}\n', encoding="utf-8")
        export.mkdir(parents=True)
        (export / "review.json").write_text(
            '{"old":true}\n', encoding="utf-8"
        )
        return {
            "claim": claim,
            "log": log,
            "receipt": receipt,
            "export": export,
        }

    def test_retention_prunes_terminal_evidence_but_preserves_active_run(self):
        ws1 = Path(self.tmp.name) / "retention-ws1"
        ws2 = Path(self.tmp.name) / "retention-ws2"
        ws1.mkdir(); ws2.mkdir()
        plan = {
            "schema_version": 1,
            "plan_revision": "retention-v1",
            "tasks": [
                {
                    "id": "OLD-1", "goal": "old", "workspace": str(ws1),
                    "dependencies": [], "allowed_paths": ["out.json"], "protected_paths": {},
                    "checks": [], "publication": {"kind": "none"}, "max_attempts": 2,
                },
                {
                    "id": "ACTIVE-2", "goal": "active", "workspace": str(ws2),
                    "dependencies": [], "allowed_paths": ["out.json"], "protected_paths": {},
                    "checks": [], "publication": {"kind": "none"}, "max_attempts": 2,
                },
            ],
        }
        path = self.root / "retention-plan.json"
        path.write_text(json.dumps(plan))
        self.orch.load_plan(path)
        old = self.orch.claim("worker-old")
        self.orch.abort(old["run_id"], "terminal fixture", retry=False)
        active = self.orch.claim("worker-active")
        logs = self.orch.runtime / "logs"
        receipts = self.orch.runtime / "worker_receipts"
        exports = self.orch.runtime / "review_exports"
        (logs / f"{old['run_id']}-scope.json").write_text('{"old":true}\n')
        (receipts / f"{old['run_id']}.json").write_text('{"old":true}\n')
        old_export = exports / old["run_id"]; old_export.mkdir(parents=True)
        (old_export / "review.json").write_text('{"old":true}\n')
        (logs / f"{active['run_id']}-scope.json").write_text('{"active":true}\n')
        (receipts / f"{active['run_id']}.json").write_text('{"active":true}\n')
        active_export = exports / active["run_id"]; active_export.mkdir(parents=True)
        (active_export / "review.json").write_text('{"active":true}\n')

        result = prune_retention(
            self.orch, older_than_days=0, max_evidence_bytes=1024 * 1024,
            keep_recent_runs=20, keep_backups=5, max_backup_bytes=1024 * 1024,
        )
        self.assertEqual(result["pruned"]["run_count"], 1)
        self.assertEqual(result["pruned"]["runs"][0]["run_id"], old["run_id"])
        self.assertFalse((logs / f"{old['run_id']}-scope.json").exists())
        self.assertFalse((receipts / f"{old['run_id']}.json").exists())
        self.assertFalse(old_export.exists())
        self.assertTrue((logs / f"{active['run_id']}-scope.json").exists())
        self.assertTrue((receipts / f"{active['run_id']}.json").exists())
        self.assertTrue(active_export.exists())
        status = retention_status(self.orch)
        self.assertIn(active["run_id"], status["evidence"]["protected_runs"])

    def test_retention_reports_unknown_artifacts_without_deleting_them(self):
        rogue = self.orch.runtime / "logs" / "manual-note.json"
        rogue.write_text('{"owner":"keep"}\n')
        result = prune_retention(
            self.orch, older_than_days=0, max_evidence_bytes=0,
            keep_recent_runs=0, keep_backups=1, max_backup_bytes=0,
        )
        self.assertTrue(rogue.exists())
        self.assertFalse(result["bounded"])
        self.assertEqual(result["evidence"]["unmanaged"][0]["reason"], "unknown_run")

    def test_retention_reports_dangling_unmanaged_log_symlink(self):
        link = self.orch.logs / "manual-link.json"
        link.symlink_to(self.orch.logs / "missing-log.json")

        status = retention_status(self.orch)

        self.assertEqual(status["status"], "ATTENTION")
        self.assertEqual(status["evidence"]["unmanaged"], [{
            "path": str(link), "reason": "non_regular_or_symlink",
        }])
        pruned = prune_retention(
            self.orch, older_than_days=0, max_evidence_bytes=0,
            keep_recent_runs=0, keep_backups=1, max_backup_bytes=0,
        )
        self.assertFalse(pruned["bounded"])
        self.assertTrue(link.is_symlink())

    def test_retention_reports_dangling_unmanaged_backup_symlink(self):
        backups = self.orch.root / "backups"
        backups.mkdir()
        link = backups / "owner-copy.zip"
        link.symlink_to(backups / "missing-backup.zip")

        status = retention_status(self.orch)

        self.assertEqual(status["status"], "ATTENTION")
        self.assertEqual(status["backups"]["unmanaged"], [{
            "path": str(link), "reason": "unmanaged_backup_artifact",
        }])
        self.assertTrue(link.is_symlink())

    def test_retention_prunes_only_managed_backup_files(self):
        backups = self.root / "backups"
        backups.mkdir()
        managed = []
        for index in range(3):
            item = backups / f"orch-state-2026010{index + 1}.zip"
            item.write_bytes(bytes([index + 1]) * 10)
            os.utime(item, (100 + index, 100 + index))
            managed.append(item)
        owner = backups / "owner-copy.zip"
        owner.write_bytes(b"keep")
        result = prune_retention(
            self.orch, older_than_days=30, max_evidence_bytes=1024,
            keep_recent_runs=20, keep_backups=2, max_backup_bytes=1024,
        )
        self.assertFalse(managed[0].exists())
        self.assertTrue(managed[1].exists())
        self.assertTrue(managed[2].exists())
        self.assertTrue(owner.exists())
        self.assertEqual(result["pruned"]["backup_count"], 1)
        self.assertEqual(Path(result["backups"]["unmanaged"][0]["path"]).resolve(), owner.resolve())

    def test_retention_preserves_replaced_run_artifact_after_inventory(self):
        fixture = self._terminal_retention_fixture("RETENTION-REPLACED")
        original_inventory = state_module._retention_inventory
        raced = False

        def inventory_then_replace(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                replacement = fixture["log"].with_name("owner-replacement.tmp")
                replacement.write_text(
                    '{"owner":"preserve"}\n', encoding="utf-8"
                )
                os.replace(replacement, fixture["log"])
                raced = True
            return inventory

        with mock.patch.object(
            state_module,
            "_retention_inventory",
            side_effect=inventory_then_replace,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=0,
                max_evidence_bytes=1024 * 1024,
                keep_recent_runs=20,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertEqual(result["status"], "ATTENTION")
        self.assertFalse(result["bounded"])
        self.assertEqual(result["pruned"]["run_count"], 0)
        self.assertEqual(result["pruned"]["skipped_changed_count"], 1)
        self.assertEqual(
            fixture["log"].read_text(encoding="utf-8"),
            '{"owner":"preserve"}\n',
        )
        self.assertTrue(fixture["receipt"].exists())
        self.assertTrue(fixture["export"].exists())

    def test_retention_preserves_review_tree_mutated_after_inventory(self):
        fixture = self._terminal_retention_fixture("RETENTION-TREE-RACE")
        original_inventory = state_module._retention_inventory
        raced = False

        def inventory_then_mutate_tree(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                (fixture["export"] / "owner-note.txt").write_text(
                    "owner preserve\n", encoding="utf-8"
                )
                raced = True
            return inventory

        with mock.patch.object(
            state_module,
            "_retention_inventory",
            side_effect=inventory_then_mutate_tree,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=0,
                max_evidence_bytes=1024 * 1024,
                keep_recent_runs=20,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertEqual(result["status"], "ATTENTION")
        self.assertEqual(result["pruned"]["run_count"], 0)
        self.assertEqual(result["pruned"]["skipped_changed_count"], 1)
        self.assertEqual(
            (fixture["export"] / "owner-note.txt").read_text(
                encoding="utf-8"
            ),
            "owner preserve\n",
        )
        self.assertTrue(fixture["log"].exists())
        self.assertTrue(fixture["receipt"].exists())

    def test_retention_rejects_symlinked_managed_anchor_after_inventory(self):
        fixture = self._terminal_retention_fixture("RETENTION-ANCHOR-RACE")
        original_inventory = state_module._retention_inventory
        logs = self.orch.runtime / "logs"
        moved_logs = self.orch.runtime / "logs-before-race"
        external = Path(self.tmp.name) / "external-logs"
        external.mkdir()
        external_log = external / fixture["log"].name
        external_log.write_text(
            '{"owner":"outside"}\n', encoding="utf-8"
        )
        raced = False

        def inventory_then_swap_anchor(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                logs.rename(moved_logs)
                logs.symlink_to(external, target_is_directory=True)
                raced = True
            return inventory

        try:
            with mock.patch.object(
                state_module,
                "_retention_inventory",
                side_effect=inventory_then_swap_anchor,
            ):
                result = prune_retention(
                    self.orch,
                    older_than_days=0,
                    max_evidence_bytes=1024 * 1024,
                    keep_recent_runs=20,
                    keep_backups=5,
                    max_backup_bytes=1024 * 1024,
                )
            self.assertEqual(result["status"], "ATTENTION")
            self.assertEqual(result["pruned"]["run_count"], 0)
            self.assertGreaterEqual(
                result["pruned"]["skipped_changed_count"], 1
            )
            self.assertEqual(
                external_log.read_text(encoding="utf-8"),
                '{"owner":"outside"}\n',
            )
        finally:
            if logs.is_symlink():
                logs.unlink()
            if moved_logs.exists():
                moved_logs.rename(logs)

    def test_retention_reconciles_missing_backup_without_deleting_next(self):
        backups = self.root / "backups"
        backups.mkdir()
        oldest = backups / "orch-state-20260101.zip"
        middle = backups / "orch-state-20260102.zip"
        newest = backups / "orch-state-20260103.zip"
        oldest.write_bytes(b"a" * 100)
        middle.write_bytes(b"b" * 10)
        newest.write_bytes(b"c" * 10)
        for index, item in enumerate((oldest, middle, newest)):
            os.utime(item, (100 + index, 100 + index))
        original_inventory = state_module._retention_inventory
        raced = False

        def inventory_then_remove_oldest(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                oldest.unlink()
                raced = True
            return inventory

        with mock.patch.object(
            state_module,
            "_retention_inventory",
            side_effect=inventory_then_remove_oldest,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=30,
                max_evidence_bytes=1024,
                keep_recent_runs=20,
                keep_backups=2,
                max_backup_bytes=20,
            )
        self.assertTrue(middle.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(result["pruned"]["backup_count"], 0)
        self.assertEqual(result["pruned"]["already_missing_count"], 1)
        self.assertTrue(result["bounded"])

    def test_retention_preserves_changed_backup_and_stops_backup_prune(self):
        backups = self.root / "backups"
        backups.mkdir()
        items = []
        for index in range(3):
            item = backups / f"orch-state-2026020{index + 1}.zip"
            item.write_bytes(bytes([index + 1]) * 10)
            os.utime(item, (200 + index, 200 + index))
            items.append(item)
        original_inventory = state_module._retention_inventory
        raced = False

        def inventory_then_replace_oldest(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                replacement = backups / "owner-backup.tmp"
                replacement.write_bytes(b"owner-data")
                os.replace(replacement, items[0])
                raced = True
            return inventory

        with mock.patch.object(
            state_module,
            "_retention_inventory",
            side_effect=inventory_then_replace_oldest,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=30,
                max_evidence_bytes=1024,
                keep_recent_runs=20,
                keep_backups=2,
                max_backup_bytes=1024,
            )
        self.assertEqual(result["status"], "ATTENTION")
        self.assertFalse(result["bounded"])
        self.assertEqual(result["pruned"]["backup_count"], 0)
        self.assertEqual(result["pruned"]["skipped_changed_count"], 1)
        self.assertEqual(items[0].read_bytes(), b"owner-data")
        self.assertTrue(items[1].exists())
        self.assertTrue(items[2].exists())

    def test_retention_reconciles_missing_newest_before_backup_delete(self):
        backups = self.root / "backups"
        backups.mkdir()
        items = []
        for index in range(3):
            item = backups / f"orch-state-2026021{index + 1}.zip"
            item.write_bytes(bytes([index + 1]) * 10)
            os.utime(item, (250 + index, 250 + index))
            items.append(item)
        original_inventory = state_module._retention_inventory
        raced = False

        def inventory_then_remove_newest(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                items[2].unlink()
                raced = True
            return inventory

        with mock.patch.object(
            state_module,
            "_retention_inventory",
            side_effect=inventory_then_remove_newest,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=30,
                max_evidence_bytes=1024,
                keep_recent_runs=20,
                keep_backups=2,
                max_backup_bytes=1024,
            )
        self.assertTrue(items[0].exists())
        self.assertTrue(items[1].exists())
        self.assertFalse(items[2].exists())
        self.assertEqual(result["pruned"]["backup_count"], 0)
        self.assertEqual(result["pruned"]["already_missing_count"], 1)
        self.assertTrue(result["bounded"])

    def test_retention_never_deletes_last_bound_backup_after_race(self):
        backups = self.root / "backups"
        backups.mkdir()
        items = []
        for index in range(3):
            item = backups / f"orch-state-2026030{index + 1}.zip"
            item.write_bytes(bytes([index + 1]) * 10)
            os.utime(item, (300 + index, 300 + index))
            items.append(item)
        original_quarantine_name = state_module._quarantine_name_at
        raced = False

        def remove_other_backups(parent_fd):
            nonlocal raced
            if not raced:
                items[1].unlink()
                items[2].unlink()
                raced = True
            return original_quarantine_name(parent_fd)

        with mock.patch.object(
            state_module,
            "_quarantine_name_at",
            side_effect=remove_other_backups,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=30,
                max_evidence_bytes=1024,
                keep_recent_runs=20,
                keep_backups=2,
                max_backup_bytes=1024,
            )
        self.assertTrue(items[0].exists())
        self.assertFalse(items[1].exists())
        self.assertFalse(items[2].exists())
        self.assertEqual(result["status"], "ATTENTION")
        self.assertFalse(result["bounded"])
        self.assertEqual(result["pruned"]["backup_count"], 0)
        self.assertEqual(result["pruned"]["skipped_changed_count"], 1)
        self.assertEqual(
            result["pruned"]["skipped_changed"][0]["reason"],
            "last_backup_guard",
        )

    def test_retention_prunes_nested_review_export_tree(self):
        fixture = self._terminal_retention_fixture("RETENTION-NESTED")
        nested = fixture["export"] / "nested" / "deeper"
        nested.mkdir(parents=True)
        (nested / "evidence.json").write_text(
            '{"nested":true}\n', encoding="utf-8"
        )

        result = prune_retention(
            self.orch,
            older_than_days=0,
            max_evidence_bytes=1024 * 1024,
            keep_recent_runs=20,
            keep_backups=5,
            max_backup_bytes=1024 * 1024,
        )
        self.assertEqual(result["pruned"]["run_count"], 1)
        self.assertEqual(result["pruned"]["failed_count"], 0)
        self.assertEqual(result["pruned"]["skipped_changed_count"], 0)
        self.assertFalse(fixture["export"].exists())
        self.assertFalse(fixture["log"].exists())
        self.assertFalse(fixture["receipt"].exists())

    def test_retention_quarantine_detects_swap_before_rename(self):
        fixture = self._terminal_retention_fixture("RETENTION-QUARANTINE")
        inventory = state_module._retention_inventory(self.orch)
        run_id = fixture["claim"]["run_id"]
        artifact = next(
            item for item in inventory["runs"][run_id]["artifacts"]
            if item["kind"] == "log"
        )
        logs = self.orch.runtime / "logs"
        replacement = logs / ".race-owner"
        replacement.write_text(
            '{"owner":"quarantine-race"}\n', encoding="utf-8"
        )
        saved_original = ".race-original"
        original_rename = os.rename
        raced = False

        def swap_before_quarantine(src, dst, *args, **kwargs):
            nonlocal raced
            if (
                not raced
                and src == fixture["log"].name
                and isinstance(dst, str)
                and dst.startswith(".orch-prune-")
            ):
                source_fd = kwargs.get("src_dir_fd")
                target_fd = kwargs.get("dst_dir_fd")
                original_rename(
                    src, saved_original,
                    src_dir_fd=source_fd, dst_dir_fd=target_fd,
                )
                original_rename(
                    replacement.name, src,
                    src_dir_fd=source_fd, dst_dir_fd=target_fd,
                )
                raced = True
            return original_rename(src, dst, *args, **kwargs)

        with mock.patch.object(
            state_module.os, "rename", side_effect=swap_before_quarantine
        ):
            result = state_module._delete_bound_artifact(
                self.orch, artifact
            )
        self.assertEqual(result["status"], "CHANGED")
        self.assertEqual(
            result["reason"], "identity_changed_during_quarantine"
        )
        self.assertTrue(result["restored"])
        self.assertEqual(
            fixture["log"].read_text(encoding="utf-8"),
            '{"owner":"quarantine-race"}\n',
        )
        self.assertTrue((logs / saved_original).exists())

    def test_retention_reports_partial_delete_failure_accounting(self):
        fixture = self._terminal_retention_fixture("RETENTION-PARTIAL")
        log_bytes = fixture["log"].stat().st_size
        original_delete = state_module._delete_bound_artifact
        calls = 0

        def fail_second_delete(orch, artifact):
            nonlocal calls
            calls += 1
            if calls == 2:
                return {
                    "status": "FAILED",
                    "reason": "synthetic_delete_failure",
                }
            return original_delete(orch, artifact)

        with mock.patch.object(
            state_module,
            "_delete_bound_artifact",
            side_effect=fail_second_delete,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=0,
                max_evidence_bytes=1024 * 1024,
                keep_recent_runs=20,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertEqual(result["status"], "ATTENTION")
        self.assertFalse(result["bounded"])
        self.assertEqual(result["pruned"]["run_count"], 1)
        self.assertEqual(result["pruned"]["evidence_bytes"], log_bytes)
        self.assertEqual(result["pruned"]["failed_count"], 1)
        self.assertFalse(fixture["log"].exists())
        self.assertTrue(fixture["receipt"].exists())
        self.assertTrue(fixture["export"].exists())

    def test_retention_accounts_partial_failure_inside_review_tree(self):
        fixture = self._terminal_retention_fixture("RETENTION-TREE-PARTIAL")
        nested = fixture["export"] / "nested"
        nested.mkdir()
        first = nested / "a.json"
        second = nested / "b.json"
        first.write_bytes(b"a" * 11)
        second.write_bytes(b"b" * 13)
        inventory = state_module._retention_inventory(self.orch)
        run_id = fixture["claim"]["run_id"]
        inventoried_bytes = inventory["runs"][run_id]["bytes"]
        base_deleted = (
            fixture["log"].stat().st_size + fixture["receipt"].stat().st_size
        )
        original_unlink = state_module.os.unlink
        tree_calls = 0

        def fail_second_tree_file(name, *args, **kwargs):
            nonlocal tree_calls
            if name in {"a.json", "b.json"}:
                tree_calls += 1
                if tree_calls == 2:
                    raise OSError("synthetic tree unlink failure")
            return original_unlink(name, *args, **kwargs)

        with mock.patch.object(
            state_module.os, "unlink", side_effect=fail_second_tree_file
        ):
            result = prune_retention(
                self.orch,
                older_than_days=0,
                max_evidence_bytes=1024 * 1024,
                keep_recent_runs=20,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertEqual(result["status"], "ATTENTION")
        self.assertFalse(result["bounded"])
        self.assertEqual(result["pruned"]["failed_count"], 1)
        self.assertEqual(result["pruned"]["run_count"], 1)
        self.assertGreater(
            result["pruned"]["evidence_bytes"], base_deleted
        )
        self.assertLess(
            result["pruned"]["evidence_bytes"], inventoried_bytes
        )
        failure = result["pruned"]["failed"][0]
        self.assertGreater(failure["deleted_bytes"], 0)
        self.assertTrue(failure["deleted_paths"])
        self.assertTrue(Path(failure["quarantine_path"]).exists())

    def test_retention_rechecks_budget_after_missing_artifacts(self):
        fixture = self._terminal_retention_fixture("RETENTION-BUDGET-RECHECK")
        fixture["log"].write_bytes(b"x" * 100)
        original_inventory = state_module._retention_inventory
        raced = False

        def inventory_then_remove_large_artifacts(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                fixture["log"].unlink()
                import shutil as shutil_module
                shutil_module.rmtree(fixture["export"])
                raced = True
            return inventory

        with mock.patch.object(
            state_module,
            "_retention_inventory",
            side_effect=inventory_then_remove_large_artifacts,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=3650,
                max_evidence_bytes=20,
                keep_recent_runs=0,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertTrue(fixture["receipt"].exists())
        self.assertEqual(result["pruned"]["run_count"], 0)
        self.assertEqual(result["pruned"]["already_missing_count"], 2)
        self.assertTrue(result["bounded"])

    def test_retention_rechecks_budget_after_delete_boundary_missing(self):
        fixture = self._terminal_retention_fixture(
            "RETENTION-LATE-MISSING"
        )
        fixture["log"].write_bytes(b"x" * 100)
        original_delete = state_module._delete_bound_artifact
        removed = False

        def disappear_at_delete(orch, artifact, **kwargs):
            nonlocal removed
            if not removed and artifact["kind"] == "log":
                Path(artifact["path"]).unlink()
                removed = True
                return {
                    "status": "MISSING",
                    "name": Path(artifact["path"]).name,
                }
            return original_delete(orch, artifact, **kwargs)

        with mock.patch.object(
            state_module,
            "_delete_bound_artifact",
            side_effect=disappear_at_delete,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=3650,
                max_evidence_bytes=30,
                keep_recent_runs=0,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertFalse(fixture["log"].exists())
        self.assertTrue(fixture["receipt"].exists())
        self.assertTrue(fixture["export"].exists())
        self.assertEqual(result["pruned"]["run_count"], 0)
        self.assertEqual(result["pruned"]["already_missing_count"], 1)
        self.assertTrue(result["bounded"])

    def test_retention_late_missing_counts_bytes_already_removed(self):
        fixture = self._terminal_retention_fixture(
            "RETENTION-LATE-MISSING-AFTER-DELETE"
        )
        fixture["log"].write_bytes(b"l" * 10)
        fixture["receipt"].write_bytes(b"r" * 100)
        original_delete = state_module._delete_bound_artifact
        removed_receipt = False

        def receipt_disappears_after_log_delete(orch, artifact, **kwargs):
            nonlocal removed_receipt
            if (
                not removed_receipt
                and artifact["kind"] == "worker_receipt"
            ):
                Path(artifact["path"]).unlink()
                removed_receipt = True
                return {
                    "status": "MISSING",
                    "name": Path(artifact["path"]).name,
                }
            return original_delete(orch, artifact, **kwargs)

        with mock.patch.object(
            state_module,
            "_delete_bound_artifact",
            side_effect=receipt_disappears_after_log_delete,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=3650,
                max_evidence_bytes=20,
                keep_recent_runs=0,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertFalse(fixture["log"].exists())
        self.assertFalse(fixture["receipt"].exists())
        self.assertTrue(fixture["export"].exists())
        self.assertEqual(result["pruned"]["run_count"], 1)
        self.assertEqual(result["pruned"]["evidence_bytes"], 10)
        self.assertEqual(result["pruned"]["already_missing_count"], 1)
        self.assertTrue(result["bounded"])

    def test_retention_fifo_substitution_fails_closed_without_blocking(self):
        fixture = self._terminal_retention_fixture("RETENTION-FIFO")
        original_inventory = state_module._retention_inventory
        raced = False

        def inventory_then_fifo(orch):
            nonlocal raced
            inventory = original_inventory(orch)
            if not raced:
                fixture["log"].unlink()
                os.mkfifo(fixture["log"])
                raced = True
            return inventory

        with mock.patch.object(
            state_module,
            "_retention_inventory",
            side_effect=inventory_then_fifo,
        ):
            result = prune_retention(
                self.orch,
                older_than_days=0,
                max_evidence_bytes=1024 * 1024,
                keep_recent_runs=20,
                keep_backups=5,
                max_backup_bytes=1024 * 1024,
            )
        self.assertEqual(result["status"], "ATTENTION")
        self.assertFalse(result["bounded"])
        self.assertEqual(result["pruned"]["run_count"], 0)
        self.assertEqual(result["pruned"]["skipped_changed_count"], 1)
        self.assertTrue(fixture["log"].exists())

    def test_retention_content_hash_detects_same_inode_mutation(self):
        fixture = self._terminal_retention_fixture("RETENTION-CONTENT-RACE")
        inventory = state_module._retention_inventory(self.orch)
        run_id = fixture["claim"]["run_id"]
        artifact = next(
            item for item in inventory["runs"][run_id]["artifacts"]
            if item["kind"] == "log"
        )
        original_stat = fixture["log"].stat()
        original_quarantine_name = state_module._quarantine_name_at
        mutated = False

        def mutate_before_rename(parent_fd):
            nonlocal mutated
            if not mutated:
                fixture["log"].write_text(
                    '{"new":true}\n', encoding="utf-8"
                )
                os.utime(
                    fixture["log"],
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                mutated = True
            return original_quarantine_name(parent_fd)

        with mock.patch.object(
            state_module,
            "_quarantine_name_at",
            side_effect=mutate_before_rename,
        ):
            result = state_module._delete_bound_artifact(
                self.orch, artifact
            )
        self.assertEqual(result["status"], "CHANGED")
        self.assertEqual(
            result["reason"], "identity_changed_during_quarantine"
        )
        self.assertTrue(result["restored"])
        self.assertEqual(
            fixture["log"].read_text(encoding="utf-8"),
            '{"new":true}\n',
        )

    def test_future_state_schema_is_rejected(self):
        other = Path(self.tmp.name) / "future"; runtime = other / ".runtime"
        runtime.mkdir(parents=True)
        db = sqlite3.connect(str(runtime / "orch.sqlite3"))
        try:
            db.execute("PRAGMA user_version=99")
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "unsupported_state_schema:99"):
            Orchestrator(other)


if __name__ == "__main__":
    unittest.main()
