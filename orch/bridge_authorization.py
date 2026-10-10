"""Local-only run/session authorization preflight for a future OrchKit Bridge.

No file or process permission is granted here. Every session check re-reads
existing ledger and capability authority; a session ID is only a routing key.
This is not a daemon, transport, OS fence or trust boundary against other
processes executing as the same local user.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from .config import read_bounded_json_object
from .core import Orchestrator, WRITER_LOCK_RUN_STATES
from .project import (
    PROJECT_CONFIG_MAX_BYTES, ProjectRegistry, inspect_project,
    project_root_identity_matches, validate_project_schema,
)
from .worker_operations import WorkerRunBinding


class LocalRunSessionAuthority:
    """Volatile session routing with mandatory fresh ledger/capability checks.

    The caller constructing this object must be trusted local OrchKit code.
    Neither route evidence nor a saved RDC marker participates in the decision.
    No worker operation is executed or approved by this object.
    """

    def __init__(self, orchestrator: Orchestrator):
        self._orch = orchestrator
        self._sessions: dict[str, WorkerRunBinding] = {}

    def open_session(self, binding: WorkerRunBinding, capability_file: Path) -> str:
        self._require_live_run(binding, capability_file)
        if binding in self._sessions.values():
            raise ValueError("bridge_run_already_bound_locally")
        session_id = secrets.token_urlsafe(24)
        if session_id in self._sessions:
            raise ValueError("bridge_session_id_collision")
        self._sessions[session_id] = binding
        return session_id

    def require_session(
        self, session_id: str, binding: WorkerRunBinding, capability_file: Path
    ) -> None:
        """Preflight identity ONLY; no authorization for any file/process I/O."""
        if (not isinstance(session_id, str)
                or self._sessions.get(session_id) != binding
                or not isinstance(binding, WorkerRunBinding)):
            raise ValueError("bridge_session_mismatch")
        self._require_live_run(binding, capability_file)

    def close_session(self, session_id: str) -> None:
        if not isinstance(session_id, str) or session_id not in self._sessions:
            raise ValueError("bridge_session_unknown")
        del self._sessions[session_id]

    def _require_live_run(
        self, binding: WorkerRunBinding, capability_file: Path
    ) -> None:
        if not isinstance(binding, WorkerRunBinding):
            raise ValueError("bridge_invalid_run_binding")
        # Use the core's bounded, no-follow, mode-checked capability reader.
        # Never retain, log or return the lease token.
        try:
            lease = self._orch.lease_from_capability(
                binding.run_id, capability_file
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("bridge_capability_denied") from exc

        with self._orch.connect() as conn:
            row = conn.execute(
                "SELECT r.task_id, r.attempt, r.state, r.lease_token, "
                "r.claim_git_head, t.project_id, t.writer_key, "
                "t.status AS task_state, t.payload_json "
                "FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                "WHERE r.run_id=?", (binding.run_id,)
            ).fetchone()
            if row is None:
                raise ValueError("bridge_run_unknown")
            expected = WorkerRunBinding(
                project_id=row["project_id"], writer_key=row["writer_key"],
                task_id=row["task_id"], run_id=binding.run_id,
                attempt=row["attempt"],
            )
            if expected != binding:
                raise ValueError("bridge_run_scope_mismatch")
            if row["state"] != "RUNNING" or row["task_state"] != "IN_PROGRESS":
                raise ValueError("bridge_run_not_active")
            if not secrets.compare_digest(row["lease_token"], lease):
                raise ValueError("bridge_capability_denied")
            latest = conn.execute(
                "SELECT MAX(attempt) AS latest FROM runs WHERE task_id=?",
                (binding.task_id,),
            ).fetchone()
            if latest["latest"] != binding.attempt:
                raise ValueError("bridge_stale_attempt")
            placeholders = ",".join("?" for _ in WRITER_LOCK_RUN_STATES)
            writers = conn.execute(
                "SELECT r.run_id FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                "WHERE t.writer_key=? AND r.state IN (" + placeholders + ")",
                (binding.writer_key, *sorted(WRITER_LOCK_RUN_STATES)),
            ).fetchall()
            if len(writers) != 1 or writers[0]["run_id"] != binding.run_id:
                raise ValueError("bridge_writer_not_exclusive")
            payload = json.loads(row["payload_json"])
            claim_head = row["claim_git_head"]

        # Registry and current on-disk Git identity are separate from a
        # caller-provided binding. Dirty in-scope worker files are expected.
        try:
            registry = ProjectRegistry.open_readonly(self._orch.root)
            project, meta = read_bounded_json_object(
                registry.projects_dir / (binding.project_id + ".json"),
                max_bytes=PROJECT_CONFIG_MAX_BYTES,
                unsafe_error="project_config_unsafe",
                too_large_error="project_config_too_large",
                invalid_error="project_config_invalid_json",
            )
            validate_project_schema(project)
            if meta["mode"] != 0o600:
                raise ValueError("project_config_mode_unsafe")
            workspace = Path(payload["workspace"])
            if (not workspace.is_absolute() or workspace.is_symlink()
                    or not workspace.is_dir()):
                raise ValueError("unsafe_workspace")
            root = workspace.resolve()
            if (str(root) != project["root"]
                    or not project_root_identity_matches(binding.project_id, root)
                    or project["project_id"] != binding.project_id
                    or project["writer_key"] != binding.writer_key
                    or payload.get("project_id") != binding.project_id
                    or payload.get("writer_key") != binding.writer_key):
                raise ValueError("workspace_binding_mismatch")
            current = inspect_project(root)
            if (current["root"] != str(root)
                    or current["writer_key"] != binding.writer_key):
                raise ValueError("git_writer_mismatch")
            expected_branch = project.get("git", {}).get("branch")
            publication_branch = payload.get("publication", {}).get("branch")
            if (not expected_branch or current["branch"] != expected_branch
                    or (publication_branch
                        and current["branch"] != publication_branch)):
                raise ValueError("git_branch_mismatch")
            if claim_head is not None and current["head"] != claim_head:
                raise ValueError("git_head_mismatch")
        except (OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError("bridge_project_binding_denied") from exc
