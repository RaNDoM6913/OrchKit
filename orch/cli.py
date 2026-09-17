from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .codex_review import run_review, subscription_preflight
from .config import configure_home, default_home
from .core import Orchestrator
from .doctor import run_doctor
from .dispatcher import bootstrap_prompt, read_rdc, record_rdc, render_dispatcher
from .git_policy import evaluate_project_git_policy
from .plan import build_single_task_plan, write_plan
from .project import PROFILE_DEFAULTS, ProjectRegistry
from .review_policy import MODES, REVIEWERS
from .state import backup_state, check_state, prune_capabilities


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
    make_plan = project_sub.add_parser("make-plan")
    make_plan.add_argument("project_id")
    make_plan.add_argument("--task-id", required=True)
    make_plan.add_argument("--goal", required=True)
    make_plan.add_argument("--allowed-path", action="append", required=True)
    make_plan.add_argument("--risk-tag", action="append", default=[])
    make_plan.add_argument("--owner-approval", action="store_true")
    make_plan.add_argument("--plan-revision")
    make_plan.add_argument("--output", required=True)
    remove = project_sub.add_parser("remove"); remove.add_argument("project_id")

    git = sub.add_parser("git", help="read-only Git inspection through a registered project")
    git_sub = git.add_subparsers(dest="git_command", required=True)
    git_inspect = git_sub.add_parser("inspect"); git_inspect.add_argument("project_id")
    git_policy = git_sub.add_parser("policy"); git_policy.add_argument("project_id")

    sub.add_parser("init")
    state = sub.add_parser("state", help="inspect and maintain durable ORCH state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_sub.add_parser("check")
    state_backup = state_sub.add_parser("backup"); state_backup.add_argument("--output")
    state_sub.add_parser("prune-capabilities")
    load = sub.add_parser("load-plan"); load.add_argument("plan")
    claim = sub.add_parser("claim"); claim.add_argument("--worker", required=True)
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
    sub.add_parser("status"); sub.add_parser("reconcile"); sub.add_parser("next")
    pause = sub.add_parser("pause"); pause.add_argument("--reason", required=True)
    sub.add_parser("resume")
    abort = sub.add_parser("abort"); abort.add_argument("--run-id", required=True); abort.add_argument("--reason", required=True); abort.add_argument("--retry", action="store_true")
    sub.add_parser("codex-preflight")
    codex_review = sub.add_parser("codex-review"); codex_review.add_argument("--run-id", required=True); codex_review.add_argument("--execute", action="store_true")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = root_from_args(args)
    orch = Orchestrator(root)
    try:
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
            result = render_dispatcher(root, output=output)
        elif args.command == "project":
            registry = ProjectRegistry(root)
            if args.project_command == "add":
                result = registry.add(
                    Path(args.path), name=args.name, profile=args.profile,
                    review_mode=args.review_mode, reviewer=args.reviewer,
                    allow_commit=args.commit, allow_push=args.push, replace=args.replace,
                )
            elif args.project_command == "list":
                result = {"status": "OK", "projects": registry.list()}
            elif args.project_command == "show":
                result = {"status": "OK", "project": registry.get(args.project_id)}
            elif args.project_command == "inspect":
                result = {"status": "OK", **registry.inspect(args.project_id)}
            elif args.project_command == "make-plan":
                plan = build_single_task_plan(
                    registry.get(args.project_id), task_id=args.task_id, goal=args.goal,
                    allowed_paths=args.allowed_path, risk_tags=args.risk_tag,
                    owner_acceptance=args.owner_approval, plan_revision=args.plan_revision,
                )
                result = write_plan(Path(args.output).expanduser(), plan)
                result["plan"] = plan
            elif args.project_command == "remove":
                result = registry.remove(args.project_id)
            else:
                raise ValueError("unknown_project_command")
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
        elif args.command == "state":
            if args.state_command == "check":
                result = check_state(orch)
            elif args.state_command == "backup":
                output = Path(args.output).expanduser().resolve() if args.output else None
                result = backup_state(orch, output)
            elif args.state_command == "prune-capabilities":
                result = prune_capabilities(orch)
            else:
                raise ValueError("unknown_state_command")
        elif args.command == "load-plan":
            result = orch.load_plan(Path(args.plan).expanduser().resolve())
        elif args.command == "claim":
            result = orch.claim(args.worker)
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
            result = orch.next_work()
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
