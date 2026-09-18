from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .codex_review import run_review, subscription_preflight
from .config import configure_home, default_home, ensure_private_dir
from .core import Orchestrator
from .doctor import run_doctor
from .dispatcher import bootstrap_prompt, read_rdc, record_rdc, render_dispatcher
from .git_policy import evaluate_project_git_policy
from .plan import (build_batch_plan, build_single_task_plan,
                   existing_plan_initial_base_binding,
                   read_batch_manifest, write_plan)
from .project import PROFILE_DEFAULTS, ProjectRegistry
from .readiness import audit_project
from .review_policy import MODES, REVIEWERS
from .state import (backup_state, check_state, inspect_home_replacement,
                    migration_history, prune_capabilities, prune_retention,
                    recovery_inspect, reconcile_home_replacement,
                    replace_home_from_backup, restore_backup_archive,
                    retention_status, verify_backup_archive)


def root_from_args(args: argparse.Namespace) -> Path:
    value = getattr(args, "root", None)
    return Path(value).expanduser().resolve() if value else default_home()


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _add_tristate(parser: argparse.ArgumentParser, name: str, help_text: str) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--allow-{name}", dest=name.replace("-", "_"), action="store_true", help=help_text)
    group.add_argument(f"--deny-{name}", dest=name.replace("-", "_"), action="store_false")
    parser.set_defaults(**{name.replace("-", "_"): None})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch", description="ChatGPT-first durable workflow orchestrator")
    parser.add_argument("--root", help="ORCH state home; defaults to $ORCH_HOME or ~/.orch")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="initialize the ORCH home without touching a project")
    setup.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default="safe")
    doctor = sub.add_parser("doctor", help="check local prerequisites and optional reviewer availability")
    doctor.add_argument("--skip-codex", action="store_true")

    rdc = sub.add_parser("rdc", help="record or inspect ChatGPT-to-RDC device binding")
    rdc_sub = rdc.add_subparsers(dest="rdc_command", required=True)
    rdc_record = rdc_sub.add_parser("record")
    rdc_record.add_argument("--device-id", required=True)
    rdc_record.add_argument("--device-name", required=True)
    rdc_sub.add_parser("show")
    rdc_sub.add_parser("bootstrap-prompt")

    dispatcher = sub.add_parser("dispatcher", help="render the reusable Scheduled ChatGPT prompt")
    dispatcher_sub = dispatcher.add_subparsers(dest="dispatcher_command", required=True)
    dispatcher_render = dispatcher_sub.add_parser("render")
    dispatcher_render.add_argument("--output")
    dispatcher_render.add_argument("--project")

    project = sub.add_parser("project", help="register and inspect project folders")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    add = project_sub.add_parser("add")
    add.add_argument("path")
    add.add_argument("--name")
    add.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS))
    add.add_argument("--review-mode", choices=sorted(MODES))
    add.add_argument("--reviewer", choices=sorted(REVIEWERS))
    add.add_argument("--replace", action="store_true")
    _add_tristate(add, "commit", "allow normal commits after all gates")
    _add_tristate(add, "push", "allow normal non-force pushes after all gates")
    project_sub.add_parser("list")
    show = project_sub.add_parser("show"); show.add_argument("project_id")
    inspect = project_sub.add_parser("inspect"); inspect.add_argument("project_id")
    audit = project_sub.add_parser("audit"); audit.add_argument("project_id"); audit.add_argument("--require-dispatcher", action="store_true")
    make_plan = project_sub.add_parser("make-plan")
    make_plan.add_argument("project_id")
    make_plan.add_argument("--task-id", required=True)
    make_plan.add_argument("--goal", required=True)
    make_plan.add_argument("--allowed-path", action="append", required=True)
    make_plan.add_argument("--risk-tag", action="append", default=[])
    make_plan.add_argument("--owner-approval", action="store_true")
    make_plan.add_argument("--depends", action="append", default=[])
    make_plan.add_argument("--max-attempts", type=int, default=2)
    make_plan.add_argument("--plan-revision")
    make_plan.add_argument("--output", required=True)
    remove = project_sub.add_parser("remove"); remove.add_argument("project_id")

    queue = sub.add_parser("queue", help="inspect or manage the durable task queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_list = queue_sub.add_parser("list")
    queue_list.add_argument("--project")
    queue_list.add_argument("--limit", type=int, default=100)
    queue_cancel = queue_sub.add_parser("cancel")
    queue_cancel.add_argument("task_id")
    queue_cancel.add_argument("--reason", required=True)
    queue_pause_project = queue_sub.add_parser("pause-project")
    queue_pause_project.add_argument("project_id")
    queue_pause_project.add_argument("--reason", required=True)
    queue_resume_project = queue_sub.add_parser("resume-project")
    queue_resume_project.add_argument("project_id")
    queue_enqueue = queue_sub.add_parser("enqueue")
    queue_enqueue.add_argument("project_id")
    queue_enqueue.add_argument("--task-id", required=True)
    queue_enqueue.add_argument("--goal", required=True)
    queue_enqueue.add_argument("--allowed-path", action="append", required=True)
    queue_enqueue.add_argument("--depends", action="append", default=[])
    queue_enqueue.add_argument("--risk-tag", action="append", default=[])
    queue_enqueue.add_argument("--owner-approval", action="store_true")
    queue_enqueue.add_argument("--max-attempts", type=int, default=2)
    queue_enqueue.add_argument("--plan-revision")
    queue_batch = queue_sub.add_parser(
        "enqueue-batch",
        help="atomically compile and enqueue a bounded JSON task manifest",
    )
    queue_batch.add_argument("project_id")
    queue_batch.add_argument("manifest")

    git = sub.add_parser("git", help="read-only Git inspection through a registered project")
    git_sub = git.add_subparsers(dest="git_command", required=True)
    git_inspect = git_sub.add_parser("inspect"); git_inspect.add_argument("project_id")
    git_policy = git_sub.add_parser("policy"); git_policy.add_argument("project_id")

    sub.add_parser("init")
    recovery = sub.add_parser("recovery", help="inspect durable restart/recovery state")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_inspect_cmd = recovery_sub.add_parser("inspect")
    recovery_inspect_cmd.add_argument("--run-id")
    recovery_inspect_cmd.add_argument("--project")
    recovery_inspect_cmd.add_argument("--replacement-home")
    state = sub.add_parser("state", help="inspect and maintain durable ORCH state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_sub.add_parser("check")
    state_sub.add_parser("migrations")
    state_backup = state_sub.add_parser("backup"); state_backup.add_argument("--output")
    state_verify_backup = state_sub.add_parser("verify-backup")
    state_verify_backup.add_argument("path")
    state_restore_backup = state_sub.add_parser("restore-backup")
    state_restore_backup.add_argument("path")
    state_restore_backup.add_argument("--destination", required=True)
    state_replace_backup = state_sub.add_parser("replace-backup")
    state_replace_backup.add_argument("path")
    state_replace_backup.add_argument("--destination", required=True)
    state_replace_reconcile = state_sub.add_parser("replace-reconcile")
    state_replace_reconcile.add_argument("--destination", required=True)
    replace_action = state_replace_reconcile.add_mutually_exclusive_group()
    replace_action.add_argument("--resume", action="store_true")
    replace_action.add_argument("--rollback", action="store_true")
    replace_action.add_argument("--finalize", action="store_true")
    state_sub.add_parser("prune-capabilities")
    retention = state_sub.add_parser("retention")
    retention.add_argument("--max-evidence-mb", type=int, default=256)
    retention.add_argument("--max-backup-mb", type=int, default=512)
    retention.add_argument("--keep-backups", type=int, default=5)
    prune_ret = state_sub.add_parser("prune-retention")
    prune_ret.add_argument("--older-than-days", type=int, default=30)
    prune_ret.add_argument("--max-evidence-mb", type=int, default=256)
    prune_ret.add_argument("--keep-recent-runs", type=int, default=20)
    prune_ret.add_argument("--keep-backups", type=int, default=5)
    prune_ret.add_argument("--max-backup-mb", type=int, default=512)
    load = sub.add_parser("load-plan"); load.add_argument("plan")
    claim = sub.add_parser("claim"); claim.add_argument("--worker", required=True); claim.add_argument("--project")
    context = sub.add_parser("context"); context.add_argument("--run-id", required=True)
    heartbeat = sub.add_parser("heartbeat"); heartbeat.add_argument("--run-id", required=True); heartbeat.add_argument("--lease"); heartbeat.add_argument("--cap")
    submit = sub.add_parser("submit"); submit.add_argument("--run-id", required=True); submit.add_argument("--lease"); submit.add_argument("--cap"); submit.add_argument("--receipt", required=True)
    quiesce = sub.add_parser("quiesce"); quiesce.add_argument("--run-id", required=True); quiesce.add_argument("--lease"); quiesce.add_argument("--cap")
    verify = sub.add_parser("verify"); verify.add_argument("--run-id", required=True)
    decision = sub.add_parser("review-decision"); decision.add_argument("--run-id", required=True)
    review = sub.add_parser("review-import"); review.add_argument("--run-id", required=True); review.add_argument("--report", required=True)
    complete = sub.add_parser("complete"); complete.add_argument("--run-id", required=True)
    approve = sub.add_parser("approve"); approve.add_argument("--run-id", required=True); approve.add_argument("--note", default="owner approved")
    publish = sub.add_parser("publish"); publish.add_argument("--run-id", required=True)
    publish_reconcile = sub.add_parser("publish-reconcile")
    publish_reconcile.add_argument("--run-id", required=True)
    publish_reconcile.add_argument("--resume", action="store_true")
    sub.add_parser("status"); sub.add_parser("reconcile")
    next_cmd = sub.add_parser("next"); next_cmd.add_argument("--project")
    pause = sub.add_parser("pause"); pause.add_argument("--reason", required=True)
    sub.add_parser("resume")
    abort = sub.add_parser("abort"); abort.add_argument("--run-id", required=True); abort.add_argument("--reason", required=True); abort.add_argument("--retry", action="store_true")
    sub.add_parser("codex-preflight")
    codex_review = sub.add_parser("codex-review"); codex_review.add_argument("--run-id", required=True); codex_review.add_argument("--execute", action="store_true")
    return parser




def _command_requires_existing_ledger(args: argparse.Namespace) -> bool:
    if args.command in {
        "setup", "doctor", "rdc", "dispatcher", "git",
        "codex-preflight", "project", "init",
    }:
        return False
    if (
        args.command == "state"
        and args.state_command in {
            "verify-backup", "restore-backup",
            "replace-backup", "replace-reconcile",
        }
    ):
        return False
    if (
        args.command == "recovery"
        and args.recovery_command == "inspect"
        and args.replacement_home is not None
        and args.run_id is None
        and args.project is None
    ):
        return False
    return True


def _open_existing_orchestrator(root: Path) -> Orchestrator:
    runtime = root / ".runtime"
    db = runtime / "orch.sqlite3"
    if (
        runtime.is_symlink()
        or not runtime.is_dir()
        or db.is_symlink()
        or not db.is_file()
    ):
        raise ValueError("state_ledger_missing_or_unsafe")
    return Orchestrator(root)


def _initialize_fresh_or_existing_orchestrator(root: Path) -> Orchestrator:
    runtime = root / ".runtime"
    db = runtime / "orch.sqlite3"
    if runtime.exists():
        if runtime.is_symlink() or not runtime.is_dir():
            raise ValueError("state_ledger_missing_or_unsafe")
        if db.exists():
            if db.is_symlink() or not db.is_file():
                raise ValueError("state_ledger_missing_or_unsafe")
            return Orchestrator(root)
        if any(runtime.iterdir()):
            raise ValueError("state_ledger_missing_or_unsafe")

    projects = root / "projects"
    if projects.exists():
        if projects.is_symlink() or not projects.is_dir():
            raise ValueError("project_registry_missing_or_unsafe")
        if any(projects.iterdir()):
            raise ValueError("registered_project_authority_without_ledger")

    return Orchestrator(root)

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = root_from_args(args)
    orch = None
    try:
        if args.command == "init":
            orch = _initialize_fresh_or_existing_orchestrator(root)
        elif _command_requires_existing_ledger(args):
            orch = _open_existing_orchestrator(root)

        if args.command == "setup":
            result = configure_home(root, profile=args.profile)
            result["status"] = "READY_FOR_PROJECT_REGISTRATION"
            result["profile"] = args.profile
        elif args.command == "doctor":
            result = run_doctor(root, check_codex=not args.skip_codex)
        elif args.command == "rdc":
            if args.rdc_command == "record":
                result = record_rdc(root, device_id=args.device_id, device_name=args.device_name)
            elif args.rdc_command == "show":
                result = read_rdc(root)
            elif args.rdc_command == "bootstrap-prompt":
                result = {"status": "READY", "prompt": bootstrap_prompt(root)}
            else:
                raise ValueError("unknown_rdc_command")
        elif args.command == "dispatcher":
            if args.dispatcher_command != "render":
                raise ValueError("unknown_dispatcher_command")
            output = Path(args.output).expanduser().resolve() if args.output else None
            result = render_dispatcher(root, output=output, project_id=args.project)
        elif args.command == "project":
            if args.project_command == "audit":
                result = audit_project(
                    root, args.project_id,
                    require_dispatcher=args.require_dispatcher,
                )
                registry = None
            else:
                registry = ProjectRegistry(root)
            if args.project_command == "add":
                ledger = _initialize_fresh_or_existing_orchestrator(root)
                result = registry.add(
                    Path(args.path), name=args.name, profile=args.profile,
                    review_mode=args.review_mode, reviewer=args.reviewer,
                    allow_commit=args.commit, allow_push=args.push, replace=args.replace,
                )
                result["ledger"] = {
                    "status": "READY",
                    "db": str(ledger.db_path),
                }
            elif args.project_command == "list":
                result = {"status": "OK", "projects": registry.list()}
            elif args.project_command == "show":
                result = {"status": "OK", "project": registry.get(args.project_id)}
            elif args.project_command == "inspect":
                result = {"status": "OK", **registry.inspect(args.project_id)}
            elif args.project_command == "audit":
                pass
            elif args.project_command == "make-plan":
                readiness = audit_project(root, args.project_id)
                if readiness["status"] == "BLOCKED":
                    result = {
                        "status": "BLOCKED",
                        "reason": "project_readiness_blocked",
                        "project_id": args.project_id,
                        "audit": readiness,
                    }
                else:
                    project_orch = _open_existing_orchestrator(root)
                    prior = project_orch.project_unresolved_tasks(args.project_id)
                    plan = build_single_task_plan(
                        registry.get(args.project_id), task_id=args.task_id, goal=args.goal,
                        allowed_paths=args.allowed_path, risk_tags=args.risk_tag,
                        owner_acceptance=args.owner_approval, plan_revision=args.plan_revision,
                        dependencies=args.depends, max_attempts=args.max_attempts,
                        bind_expected_base=not prior["has_unresolved"],
                    )
                    result = write_plan(Path(args.output).expanduser(), plan)
                    result["base_binding"] = (
                        "dynamic_at_verify" if prior["has_unresolved"]
                        else "admission_head"
                    )
                    result["plan"] = plan
                    result["audit_status"] = readiness["status"]
            elif args.project_command == "remove":
                registry.get(args.project_id)
                db_path = root / ".runtime" / "orch.sqlite3"
                if (
                    db_path.is_symlink()
                    or not db_path.is_file()
                ):
                    result = {
                        "status": "BLOCKED",
                        "project_id": args.project_id,
                        "reason": "project_state_ledger_missing_or_unsafe",
                        "db": str(db_path),
                        "safe_next_steps": [
                            "Restore or reconcile the authoritative ORCH state before deregistration.",
                            "Do not initialize an empty replacement ledger to bypass this guard.",
                        ],
                    }
                else:
                    project_orch = Orchestrator(root)
                    guard = project_orch.project_removal_guard(args.project_id)
                    if guard["status"] != "SAFE":
                        result = guard
                    else:
                        removed = registry.remove(args.project_id)
                        recorded = project_orch.record_project_removed(args.project_id)
                        result = {
                            **removed,
                            "queue_guard": guard,
                            "ledger": recorded,
                        }
            else:
                raise ValueError("unknown_project_command")
        elif args.command == "queue":
            if args.queue_command == "list":
                result = orch.queue_view(project_id=args.project, limit=args.limit)
            elif args.queue_command == "cancel":
                result = orch.cancel_task(args.task_id, args.reason)
            elif args.queue_command == "pause-project":
                result = orch.pause_project(args.project_id, args.reason)
            elif args.queue_command == "resume-project":
                result = orch.resume_project(args.project_id)
            elif args.queue_command == "enqueue-batch":
                readiness = audit_project(root, args.project_id)
                if readiness["status"] == "BLOCKED":
                    result = {
                        "status": "BLOCKED",
                        "reason": "project_readiness_blocked",
                        "project_id": args.project_id,
                        "audit": readiness,
                    }
                else:
                    registry = ProjectRegistry(root)
                    manifest_path = Path(args.manifest).expanduser()
                    manifest, source_digest = read_batch_manifest(manifest_path)
                    prior = orch.project_unresolved_tasks(args.project_id)
                    plans_dir = ensure_private_dir(root / "plans")
                    manifest_revision = manifest.get("plan_revision")
                    existing_binding = None
                    if isinstance(manifest_revision, str):
                        existing_binding = existing_plan_initial_base_binding(
                            plans_dir / f"{manifest_revision}.json"
                        )
                    bind_initial_base = (
                        existing_binding
                        if existing_binding is not None
                        else not prior["has_unresolved"]
                    )
                    plan = build_batch_plan(
                        registry.get(args.project_id),
                        manifest,
                        source_digest=source_digest,
                        bind_initial_base=bind_initial_base,
                    )
                    plan_path = plans_dir / f"{plan['plan_revision']}.json"
                    existed = plan_path.exists()
                    written = write_plan(plan_path, plan, replace=False)
                    try:
                        loaded = orch.load_plan(plan_path)
                    except Exception:
                        if (
                            not existed
                            and written["status"] == "CREATED"
                            and plan_path.is_file()
                            and not plan_path.is_symlink()
                        ):
                            plan_path.unlink()
                        raise
                    result = {
                        "status": "ENQUEUED",
                        "project_id": args.project_id,
                        "task_ids": [item["id"] for item in plan["tasks"]],
                        "task_count": len(plan["tasks"]),
                        "plan_revision": plan["plan_revision"],
                        "plan_path": written["path"],
                        "plan_artifact_status": written["status"],
                        "source_manifest": str(manifest_path.resolve()),
                        "source_sha256": source_digest,
                        "audit_status": readiness["status"],
                        "base_binding": (
                            "dynamic_at_verify"
                            if prior["has_unresolved"]
                            else "first_task_admission_head"
                        ),
                        "load": loaded,
                    }
            elif args.queue_command == "enqueue":
                readiness = audit_project(root, args.project_id)
                if readiness["status"] == "BLOCKED":
                    result = {
                        "status": "BLOCKED",
                        "reason": "project_readiness_blocked",
                        "project_id": args.project_id,
                        "audit": readiness,
                    }
                else:
                    registry = ProjectRegistry(root)
                    prior = orch.project_unresolved_tasks(args.project_id)
                    plans_dir = ensure_private_dir(root / "plans")
                    existing_binding = None
                    if args.plan_revision:
                        existing_binding = existing_plan_initial_base_binding(
                            plans_dir / f"{args.plan_revision}.json"
                        )
                    bind_expected_base = (
                        existing_binding
                        if existing_binding is not None
                        else not prior["has_unresolved"]
                    )
                    plan = build_single_task_plan(
                        registry.get(args.project_id),
                        task_id=args.task_id,
                        goal=args.goal,
                        allowed_paths=args.allowed_path,
                        risk_tags=args.risk_tag,
                        owner_acceptance=args.owner_approval,
                        plan_revision=args.plan_revision,
                        dependencies=args.depends,
                        max_attempts=args.max_attempts,
                        bind_expected_base=bind_expected_base,
                    )
                    plan_path = plans_dir / f"{plan['plan_revision']}.json"
                    existed = plan_path.exists()
                    written = write_plan(plan_path, plan, replace=False)
                    try:
                        loaded = orch.load_plan(plan_path)
                    except Exception:
                        if not existed and written["status"] == "CREATED" and plan_path.is_file() and not plan_path.is_symlink():
                            plan_path.unlink()
                        raise
                    result = {
                        "status": "ENQUEUED",
                        "task_id": args.task_id,
                        "project_id": args.project_id,
                        "plan_revision": plan["plan_revision"],
                        "plan_path": written["path"],
                        "plan_artifact_status": written["status"],
                        "audit_status": readiness["status"],
                        "base_binding": (
                            "dynamic_at_verify"
                            if prior["has_unresolved"]
                            else "admission_head"
                        ),
                        "load": loaded,
                    }
            else:
                raise ValueError("unknown_queue_command")
        elif args.command == "git":
            registry = ProjectRegistry(root)
            if args.git_command == "inspect":
                result = {"status": "OK", **registry.inspect(args.project_id)}
            elif args.git_command == "policy":
                result = evaluate_project_git_policy(registry.get(args.project_id))
            else:
                raise ValueError("unknown_git_command")
        elif args.command == "init":
            result = {"status": "OK", "root": str(root), "db": str(orch.db_path)}
        elif args.command == "recovery":
            if args.recovery_command == "inspect":
                replacement = (
                    inspect_home_replacement(
                        Path(args.replacement_home).expanduser()
                    )
                    if args.replacement_home else None
                )
                if orch is None:
                    result = {
                        "status": replacement["status"],
                        "automatic_expiry": False,
                        "items": [],
                        "replacement": replacement,
                    }
                else:
                    result = recovery_inspect(
                        orch, run_id=args.run_id, project_id=args.project
                    )
                    if replacement is not None:
                        result["replacement"] = replacement
                        if replacement["status"] == "BLOCKED":
                            result["status"] = "BLOCKED"
                        elif (
                            replacement["status"] == "ATTENTION"
                            and result["status"] == "CLEAN"
                        ):
                            result["status"] = "ATTENTION"
            else:
                raise ValueError("unknown_recovery_command")
        elif args.command == "state":
            if args.state_command == "check":
                result = check_state(orch)
            elif args.state_command == "migrations":
                result = migration_history(orch)
            elif args.state_command == "backup":
                output = Path(args.output).expanduser().resolve() if args.output else None
                result = backup_state(orch, output)
            elif args.state_command == "verify-backup":
                result = verify_backup_archive(Path(args.path).expanduser())
            elif args.state_command == "restore-backup":
                result = restore_backup_archive(
                    Path(args.path).expanduser(),
                    Path(args.destination).expanduser(),
                )
            elif args.state_command == "replace-backup":
                result = replace_home_from_backup(
                    Path(args.path).expanduser(),
                    Path(args.destination).expanduser(),
                )
            elif args.state_command == "replace-reconcile":
                result = reconcile_home_replacement(
                    Path(args.destination).expanduser(),
                    resume=args.resume,
                    finalize=args.finalize,
                    rollback=args.rollback,
                )
            elif args.state_command == "prune-capabilities":
                result = prune_capabilities(orch)
            elif args.state_command == "retention":
                result = retention_status(
                    orch,
                    max_evidence_bytes=args.max_evidence_mb * 1024 * 1024,
                    max_backup_bytes=args.max_backup_mb * 1024 * 1024,
                    keep_backups=args.keep_backups,
                )
            elif args.state_command == "prune-retention":
                result = prune_retention(
                    orch,
                    older_than_days=args.older_than_days,
                    max_evidence_bytes=args.max_evidence_mb * 1024 * 1024,
                    keep_recent_runs=args.keep_recent_runs,
                    keep_backups=args.keep_backups,
                    max_backup_bytes=args.max_backup_mb * 1024 * 1024,
                )
            else:
                raise ValueError("unknown_state_command")
        elif args.command == "load-plan":
            result = orch.load_plan(Path(args.plan).expanduser().resolve())
        elif args.command == "claim":
            result = orch.claim(args.worker, project_id=args.project)
        elif args.command == "context":
            result = orch.context(args.run_id)
        elif args.command == "heartbeat":
            lease = args.lease or orch.lease_from_capability(args.run_id, Path(args.cap).expanduser())
            result = orch.heartbeat(args.run_id, lease)
        elif args.command == "submit":
            lease = args.lease or orch.lease_from_capability(args.run_id, Path(args.cap).expanduser())
            result = orch.submit(args.run_id, lease, Path(args.receipt).expanduser().resolve())
        elif args.command == "quiesce":
            lease = args.lease or orch.lease_from_capability(args.run_id, Path(args.cap).expanduser())
            result = orch.quiesce(args.run_id, lease)
        elif args.command == "verify":
            result = orch.verify(args.run_id)
        elif args.command == "review-decision":
            result = orch.review_decision(args.run_id)
        elif args.command == "review-import":
            result = orch.import_review(args.run_id, Path(args.report).expanduser().resolve())
        elif args.command == "complete":
            result = orch.complete(args.run_id)
        elif args.command == "approve":
            result = orch.approve(args.run_id, args.note)
        elif args.command == "publish":
            result = orch.publish(args.run_id)
        elif args.command == "publish-reconcile":
            result = orch.reconcile_publication(args.run_id, resume=args.resume)
        elif args.command == "status":
            result = orch.status()
        elif args.command == "reconcile":
            result = orch.reconcile()
        elif args.command == "next":
            result = orch.next_work(project_id=args.project)
        elif args.command == "pause":
            result = orch.pause(args.reason)
        elif args.command == "resume":
            result = orch.resume()
        elif args.command == "abort":
            result = orch.abort(args.run_id, args.reason, args.retry)
        elif args.command == "codex-preflight":
            result = subscription_preflight(root)
        elif args.command == "codex-review":
            result = run_review(orch, args.run_id, execute=args.execute)
        else:
            parser.error("unknown command")
            return 2
        emit(result)
        return 0
    except Exception as exc:
        emit({"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
