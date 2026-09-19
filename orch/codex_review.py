from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import stat as statmod
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional

from .config import (atomic_write_bytes, atomic_write_json,
                     ensure_private_dir, read_bounded_regular_file)
from .core import (VERIFICATION_EVIDENCE_MAX_BYTES, Orchestrator,
                   safe_workspace_path, sha256_bytes, utc_now)

CODEX_BIN = Path('/Applications/ChatGPT.app/Contents/Resources/codex')


def safe_env() -> Dict[str, str]:
    return {key: os.environ[key] for key in ('HOME','USER','PATH','TMPDIR','LANG','LC_ALL') if key in os.environ}


def subscription_preflight(root: Path) -> Dict[str, Any]:
    if not CODEX_BIN.is_file():
        return {'status':'BLOCKED','reason':'codex_binary_missing'}
    forbidden = [name for name in ('OPENAI_API_KEY','OPENAI_BASE_URL','DEEPSEEK_API_KEY','ANTHROPIC_API_KEY') if os.environ.get(name)]
    if forbidden:
        return {'status':'BLOCKED','reason':'forbidden_provider_env','present':forbidden}
    env=safe_env()
    proc=subprocess.Popen([str(CODEX_BIN),'--disable','hooks','app-server','--stdio'],cwd=str(root),env=env,
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    def send(obj: Dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj,separators=(',',':'))+'\n'); proc.stdin.flush()
    send({'method':'initialize','id':0,'params':{'clientInfo':{'name':'orch','title':'ORCH subscription preflight','version':'0.1.0'}}})
    send({'method':'initialized','params':{}})
    send({'method':'account/read','id':1,'params':{'refreshToken':False}})
    send({'method':'account/rateLimits/read','id':2})
    responses={}; deadline=time.time()+12
    assert proc.stdout is not None
    while time.time()<deadline and len(responses)<3:
        ready,_,_=select.select([proc.stdout],[],[],0.5)
        if not ready: continue
        line=proc.stdout.readline()
        if not line: break
        try: msg=json.loads(line)
        except Exception: continue
        if msg.get('id') in {0,1,2}: responses[msg['id']]=msg
    proc.terminate()
    try: proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.wait()
    account_result=responses.get(1,{}).get('result',{})
    account=account_result.get('account') or {}
    rates=responses.get(2,{}).get('result',{})
    primary=(rates.get('rateLimits') or {}).get('primary') or {}
    buckets=rates.get('rateLimitsByLimitId') or {}
    credits=(buckets.get('codex') or {}).get('credits') or {}
    summary={'status':'PASS','checked_at_utc':utc_now(),'account_type':account.get('type'),'plan_type':account.get('planType'),
             'requires_openai_auth':account_result.get('requiresOpenaiAuth'),'rate_limit_reached_type':(rates.get('rateLimits') or {}).get('rateLimitReachedType'),
             'used_percent':primary.get('usedPercent'),'resets_at':primary.get('resetsAt'),'purchased_credits':bool(credits.get('hasCredits')),
             'credit_balance':credits.get('balance'),'errors':{str(k):v.get('error') for k,v in responses.items() if v.get('error')}}
    if len(responses)<3 or summary['errors'] or summary['account_type']!='chatgpt' or summary['rate_limit_reached_type'] is not None or summary['purchased_credits']:
        summary['status']='BLOCKED'
    return summary


def _copy_bound_review_file(
    source: Path,
    target: Path,
    *,
    relative: str,
    expected_sha256: str,
    expected_bytes: Optional[int],
    error_prefix: str,
) -> Dict[str, Any]:
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise ValueError(error_prefix + "_binding_invalid:" + relative)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif source.is_symlink():
        raise ValueError(error_prefix + ":" + relative)
    try:
        fd = os.open(str(source), flags)
    except OSError as exc:
        raise ValueError(error_prefix + ":" + relative) from exc
    temp: Optional[Path] = None
    out_fd = -1
    try:
        before = os.fstat(fd)
        if not statmod.S_ISREG(before.st_mode):
            raise ValueError(error_prefix + ":" + relative)
        if (
            expected_bytes is not None
            and before.st_size != expected_bytes
        ):
            raise ValueError(error_prefix + ":" + relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        out_fd, temp_name = tempfile.mkstemp(
            prefix=target.name + ".tmp-",
            dir=str(target.parent),
        )
        temp = Path(temp_name)
        os.fchmod(out_fd, 0o600)
        digest = hashlib.sha256()
        written = 0
        with os.fdopen(fd, "rb", closefd=True) as src:
            fd = -1
            with os.fdopen(out_fd, "wb", closefd=True) as dst:
                out_fd = -1
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    written += len(block)
                    dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
            after = os.fstat(src.fileno())
        before_identity = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if (
            before_identity != after_identity
            or written != before.st_size
            or (
                expected_bytes is not None
                and written != expected_bytes
            )
            or digest.hexdigest() != expected_sha256
        ):
            raise ValueError(error_prefix + ":" + relative)
        os.replace(str(temp), str(target))
        temp = None
        return {
            "sha256": expected_sha256,
            "bytes": written,
        }
    finally:
        if fd >= 0:
            os.close(fd)
        if out_fd >= 0:
            os.close(out_fd)
        if temp is not None:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def _copy_bound_evidence(
    source: Path,
    target: Path,
    *,
    record: Dict[str, Any],
    evidence_id: str,
) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("review_evidence_binding_invalid:" + evidence_id)
    expected_sha = record.get("sha256")
    expected_bytes = record.get("bytes")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha)
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
    ):
        raise ValueError("review_evidence_binding_invalid:" + evidence_id)
    try:
        raw, meta = read_bounded_regular_file(
            source,
            max_bytes=VERIFICATION_EVIDENCE_MAX_BYTES,
            unsafe_error="review_evidence_unsafe",
            too_large_error="review_evidence_too_large",
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "review_evidence_missing:" + evidence_id
        ) from exc
    except ValueError as exc:
        raise ValueError(
            str(exc) + ":" + evidence_id
        ) from exc
    if (
        meta["bytes"] != expected_bytes
        or sha256_bytes(raw) != expected_sha
    ):
        raise ValueError("review_evidence_stale:" + evidence_id)
    atomic_write_bytes(
        target,
        raw,
        mode=0o600,
        unsafe_error="review_export_target_unsafe",
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "review_evidence_invalid_json:" + evidence_id
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("review_evidence_invalid_json:" + evidence_id)
    return data


def prepare_review(orch: Orchestrator, run_id: str) -> Dict[str, Any]:
    decision = orch.review_decision(run_id)
    if not decision.get("required"):
        raise ValueError("review_not_required")
    if decision.get("reviewer") != "codex":
        raise ValueError("reviewer_not_codex")
    with orch.connect() as conn:
        run = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not run or run["state"] != "REVIEWING":
            raise ValueError("run_not_reviewing")
        task = conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)
        ).fetchone()
        if not task:
            raise ValueError("review_task_missing")
        payload = json.loads(task["payload_json"])
        snap = conn.execute(
            "SELECT manifest_json FROM snapshots WHERE snapshot_id=?",
            (run["snapshot_id"],),
        ).fetchone()
        if not snap:
            raise ValueError("snapshot_missing")
        manifest = json.loads(snap["manifest_json"])

    orch._assert_manifest_current(payload, manifest)
    evidence_binding = manifest.get("verification_evidence")
    if not isinstance(evidence_binding, dict):
        raise ValueError("review_evidence_binding_missing")

    export = orch.runtime / "review_exports" / run_id
    if export.is_symlink() or (export.exists() and not export.is_dir()):
        raise ValueError("review_export_unsafe")
    if export.exists():
        shutil.rmtree(export)
    ensure_private_dir(export)
    work = ensure_private_dir(export / "workspace")
    workspace = Path(payload["workspace"]).resolve()

    try:
        copied: Dict[str, Dict[str, Any]] = {}
        deleted_files = []
        for rel, meta in sorted(manifest.get("files", {}).items()):
            if not isinstance(meta, dict):
                raise ValueError("review_snapshot_binding_invalid:" + rel)
            if meta.get("deleted"):
                deleted_files.append(rel)
                continue
            expected_sha = meta.get("sha256")
            expected_bytes = meta.get("bytes")
            if (
                not isinstance(expected_bytes, int)
                or isinstance(expected_bytes, bool)
                or expected_bytes < 0
            ):
                raise ValueError(
                    "review_snapshot_binding_invalid:" + rel
                )
            src = safe_workspace_path(
                workspace, rel, must_exist=True
            )
            dst = safe_workspace_path(
                work, rel, must_exist=False
            )
            copied[rel] = _copy_bound_review_file(
                src,
                dst,
                relative=rel,
                expected_sha256=expected_sha,
                expected_bytes=expected_bytes,
                error_prefix="review_snapshot_stale",
            )

        support_bindings: Dict[str, str] = {}
        for check in payload.get("checks", []):
            authority = check.get("authority_files", [])
            if not isinstance(authority, list):
                raise ValueError("review_support_binding_invalid")
            for item in authority:
                if not isinstance(item, dict):
                    raise ValueError("review_support_binding_invalid")
                if item.get("expected") != "file":
                    continue
                rel = item.get("path")
                expected_sha = item.get("sha256")
                if not isinstance(rel, str) or not isinstance(
                    expected_sha, str
                ):
                    raise ValueError("review_support_binding_invalid")
                prior = support_bindings.get(rel)
                if prior is not None and prior != expected_sha:
                    raise ValueError(
                        "review_support_binding_conflict:" + rel
                    )
                support_bindings[rel] = expected_sha

        support_files: Dict[str, Dict[str, Any]] = {}
        for rel, expected_sha in sorted(support_bindings.items()):
            if rel in copied:
                if copied[rel]["sha256"] != expected_sha:
                    raise ValueError("review_support_stale:" + rel)
                copied_support = copied[rel]
            else:
                src = safe_workspace_path(
                    workspace, rel, must_exist=True
                )
                dst = safe_workspace_path(
                    work, rel, must_exist=False
                )
                copied_support = _copy_bound_review_file(
                    src,
                    dst,
                    relative=rel,
                    expected_sha256=expected_sha,
                    expected_bytes=None,
                    error_prefix="review_support_stale",
                )
                copied[rel] = copied_support
            support_files[rel] = {
                "sha256": expected_sha,
                "bytes": copied_support["bytes"],
                "purpose": "approved_check_support",
            }

        scope_record = evidence_binding.get("scope")
        authority_record = evidence_binding.get("check_authority")
        check_records = evidence_binding.get("checks")
        if (
            not isinstance(scope_record, dict)
            or not isinstance(authority_record, dict)
            or not isinstance(check_records, list)
        ):
            raise ValueError("review_evidence_binding_invalid")
        expected_scope_name = f"{run_id}-scope.json"
        expected_authority_name = f"{run_id}-check-authority.json"
        if scope_record.get("file") != expected_scope_name:
            raise ValueError("review_evidence_binding_invalid:scope")
        if authority_record.get("file") != expected_authority_name:
            raise ValueError(
                "review_evidence_binding_invalid:check_authority"
            )
        checks = payload.get("checks", [])
        if len(check_records) != len(checks):
            raise ValueError("review_evidence_binding_invalid:checks")

        evidence_dir = ensure_private_dir(
            work / "verification_evidence"
        )
        scope_target = evidence_dir / "scope.json"
        scope_data = _copy_bound_evidence(
            orch.logs / expected_scope_name,
            scope_target,
            record=scope_record,
            evidence_id="scope",
        )
        scope_evidence = {
            **scope_data,
            "evidence_file": str(scope_target.relative_to(work)),
            "sha256": scope_record["sha256"],
            "bytes": scope_record["bytes"],
        }

        authority_target = evidence_dir / "check-authority.json"
        authority_data = _copy_bound_evidence(
            orch.logs / expected_authority_name,
            authority_target,
            record=authority_record,
            evidence_id="check_authority",
        )
        check_authority_evidence = {
            **authority_data,
            "evidence_file": str(authority_target.relative_to(work)),
            "sha256": authority_record["sha256"],
            "bytes": authority_record["bytes"],
        }

        verification = []
        for index, check in enumerate(checks):
            check_id = str(check.get("id", "check"))
            record = check_records[index]
            if (
                not isinstance(record, dict)
                or record.get("id") != check_id
            ):
                raise ValueError(
                    "review_evidence_binding_invalid:check:" + check_id
                )
            filename = record.get("file")
            if (
                not isinstance(filename, str)
                or Path(filename).name != filename
                or not filename.startswith(run_id + "-")
                or not filename.endswith(".json")
            ):
                raise ValueError(
                    "review_evidence_binding_invalid:check:" + check_id
                )
            exported_name = filename[len(run_id) + 1:]
            target = evidence_dir / exported_name
            data = _copy_bound_evidence(
                orch.logs / filename,
                target,
                record=record,
                evidence_id="check:" + check_id,
            )
            verification.append({
                "id": data.get("id"),
                "exit_code": data.get("exit_code"),
                "timed_out": data.get("timed_out"),
                "stdout": data.get("stdout", "")[-2000:],
                "stderr": data.get("stderr", "")[-2000:],
                "evidence_file": str(target.relative_to(work)),
                "sha256": record["sha256"],
                "bytes": record["bytes"],
            })

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "run_id", "snapshot_id", "verdict",
                "findings", "uncertainty",
            ],
            "properties": {
                "run_id": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "verdict": {
                    "type": "string",
                    "enum": ["PASS", "NEEDS_FIX", "BLOCKED"],
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "severity", "path", "evidence", "impact",
                        ],
                        "properties": {
                            "severity": {"type": "string"},
                            "path": {"type": "string"},
                            "evidence": {"type": "string"},
                            "impact": {"type": "string"},
                        },
                    },
                },
                "uncertainty": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }
        schema_path = export / "review_schema.json"
        atomic_write_json(schema_path, schema, mode=0o600)

        prompt = {
            "role": "reviewer_only",
            "run_id": run_id,
            "snapshot_id": run["snapshot_id"],
            "goal": payload.get("goal"),
            "non_goals": payload.get("non_goals", []),
            "review_decision": decision,
            "files": sorted(manifest.get("files", {})),
            "deleted_files": deleted_files,
            "file_manifest": manifest.get("files", {}),
            "support_files": support_files,
            "checks": checks,
            "verification_evidence": verification,
            "scope_evidence": scope_evidence,
            "check_authority_evidence": check_authority_evidence,
            "instructions": [
                "Read only the exported workspace.",
                "Do not edit files or run project hooks.",
                "Verifier checks already ran against the source workspace; "
                "inspect supplied evidence and frozen support files.",
                "Only rerun an approved check when necessary; never expand "
                "beyond the exported workspace.",
                "Report only concrete findings.",
                "PASS only when the stated goal/checklist is met.",
            ],
        }
        prompt_path = export / "review_prompt.json"
        atomic_write_json(prompt_path, prompt, mode=0o600)
        return {
            "export": str(export),
            "workspace": str(work),
            "schema": str(schema_path),
            "prompt": str(prompt_path),
            "report": str(export / "review.json"),
            "snapshot_id": run["snapshot_id"],
        }
    except Exception:
        if export.exists() and export.is_dir() and not export.is_symlink():
            shutil.rmtree(export)
        raise


def run_review(orch: Orchestrator, run_id: str, *, execute: bool=False) -> Dict[str, Any]:
    prepared=prepare_review(orch,run_id)
    preflight=subscription_preflight(orch.root)
    cmd=[str(CODEX_BIN),'--disable','hooks','exec','--ignore-user-config','--sandbox','read-only','--skip-git-repo-check','--json',
         '--output-schema',prepared['schema'],'-o',str(Path(prepared['export'])/'review.json'),'-C',prepared['workspace'],'-']
    if not execute:
        return {'status':'DRY_RUN','preflight':preflight,'command':cmd,'prepared':prepared}
    if preflight.get('status')!='PASS': return {'status':'BLOCKED','preflight':preflight}
    prompt=Path(prepared['prompt']).read_text(encoding='utf-8')
    events=Path(prepared['export'])/'events.jsonl'
    proc=subprocess.run(cmd,cwd=prepared['workspace'],env=safe_env(),input=prompt,capture_output=True,text=True,timeout=180,check=False)
    events.write_text(proc.stdout,encoding='utf-8')
    report=Path(prepared['export'])/'review.json'
    if proc.returncode!=0 or not report.is_file():
        return {'status':'BLOCKED','preflight':preflight,'exit_code':proc.returncode,'stderr':proc.stderr[-4000:],'events':str(events)}
    imported=orch.import_review(run_id,report)
    return {'status':'COMPLETE','preflight':preflight,'exit_code':proc.returncode,'report':str(report),'imported':imported}
