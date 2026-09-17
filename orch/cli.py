from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import Orchestrator
from .codex_review import run_review, subscription_preflight


def root_from_args(args: argparse.Namespace) -> Path:
    value = getattr(args, "root", None)
    return Path(value).expanduser().resolve() if value else Path(__file__).resolve().parent.parent


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch", description="ChatGPT-first Variant B workflow orchestrator")
    parser.add_argument("--root", help="orchestrator root; defaults to package parent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    load = sub.add_parser("load-plan")
    load.add_argument("plan")
    claim = sub.add_parser("claim")
    claim.add_argument("--worker", required=True)
    context = sub.add_parser("context")
    context.add_argument("--run-id", required=True)
    heartbeat = sub.add_parser("heartbeat")
    heartbeat.add_argument("--run-id", required=True)
    heartbeat.add_argument("--lease")
    heartbeat.add_argument("--cap")
    submit = sub.add_parser("submit")
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--lease")
    submit.add_argument("--cap")
    submit.add_argument("--receipt", required=True)
    quiesce = sub.add_parser("quiesce")
    quiesce.add_argument("--run-id", required=True)
    quiesce.add_argument("--lease")
    quiesce.add_argument("--cap")
    verify = sub.add_parser("verify")
    verify.add_argument("--run-id", required=True)
    review = sub.add_parser("review-import")
    review.add_argument("--run-id", required=True)
    review.add_argument("--report", required=True)
    complete = sub.add_parser("complete")
    complete.add_argument("--run-id", required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--note", default="owner approved")
    publish = sub.add_parser("publish")
    publish.add_argument("--run-id", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    sub.add_parser("next")
    pause = sub.add_parser("pause"); pause.add_argument("--reason", required=True)
    sub.add_parser("resume")
    abort = sub.add_parser("abort"); abort.add_argument("--run-id", required=True); abort.add_argument("--reason", required=True); abort.add_argument("--retry", action="store_true")
    sub.add_parser("codex-preflight")
    codex_review = sub.add_parser("codex-review")
    codex_review.add_argument("--run-id", required=True)
    codex_review.add_argument("--execute", action="store_true")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = root_from_args(args)
    orch = Orchestrator(root)
    try:
        if args.command == "init":
            result = {"status": "OK", "root": str(root), "db": str(orch.db_path)}
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
        elif args.command == "review-import":
            result = orch.import_review(args.run_id, Path(args.report).expanduser().resolve())
        elif args.command == "complete":
            result = orch.complete(args.run_id)
        elif args.command == "approve":
            result = orch.approve(args.run_id, args.note)
        elif args.command == "publish":
            result = orch.publish(args.run_id)
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
