from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import time
from typing import Any, Dict

from .core import Orchestrator, canonical_json, safe_workspace_path, sha256_file, utc_now

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


def prepare_review(orch: Orchestrator, run_id: str) -> Dict[str, Any]:
    decision = orch.review_decision(run_id)
    if not decision.get('required'):
        raise ValueError('review_not_required')
    if decision.get('reviewer') != 'codex':
        raise ValueError('reviewer_not_codex')
    with orch.connect() as conn:
        run=conn.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
        if not run or run['state']!='REVIEWING': raise ValueError('run_not_reviewing')
        task=conn.execute('SELECT * FROM tasks WHERE task_id=?',(run['task_id'],)).fetchone()
        payload=json.loads(task['payload_json'])
        snap=conn.execute('SELECT manifest_json FROM snapshots WHERE snapshot_id=?',(run['snapshot_id'],)).fetchone()
        if not snap: raise ValueError('snapshot_missing')
        manifest=json.loads(snap['manifest_json'])
    export=orch.runtime/'review_exports'/run_id
    if export.exists(): shutil.rmtree(export)
    work=export/'workspace'; work.mkdir(parents=True)
    workspace=Path(payload['workspace']).resolve()
    copied=set(); deleted_files=[]
    for rel, meta in sorted(manifest.get('files',{}).items()):
        if meta.get('deleted'):
            deleted_files.append(rel)
            continue
        src=safe_workspace_path(workspace,rel,must_exist=True)
        dst=work/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst); copied.add(rel)
    support_files={}
    for check in payload.get('checks',[]):
        for arg in check.get('argv',[])[1:]:
            if not isinstance(arg,str) or not arg or arg.startswith('-') or Path(arg).is_absolute():
                continue
            try:
                src=safe_workspace_path(workspace,arg,must_exist=True)
            except (ValueError,OSError):
                continue
            if not src.is_file():
                continue
            rel=Path(arg).as_posix()
            if rel not in copied:
                dst=work/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst); copied.add(rel)
            support_files[rel]={'sha256':sha256_file(src),'purpose':'approved_check_support'}
    verification=[]
    evidence_dir=work/'verification_evidence'; evidence_dir.mkdir()
    scope_evidence=None
    scope_log=orch.logs/f"{run_id}-scope.json"
    if scope_log.is_file():
        scope_data=json.loads(scope_log.read_text(encoding='utf-8'))
        scope_target=evidence_dir/'scope.json'; shutil.copy2(scope_log,scope_target)
        scope_evidence={**scope_data,'evidence_file':str(scope_target.relative_to(work)),'sha256':sha256_file(scope_target)}
    for check in payload.get('checks',[]):
        safe_id=str(check.get('id','check')).replace('/','_')
        log=orch.logs/f"{run_id}-{safe_id}.json"
        if not log.is_file():
            continue
        data=json.loads(log.read_text(encoding='utf-8'))
        target=evidence_dir/f"{safe_id}.json"; shutil.copy2(log,target)
        verification.append({'id':data.get('id'),'exit_code':data.get('exit_code'),'timed_out':data.get('timed_out'),
                             'stdout':data.get('stdout','')[-2000:],'stderr':data.get('stderr','')[-2000:],
                             'evidence_file':str(target.relative_to(work)),'sha256':sha256_file(target)})
    schema={'type':'object','additionalProperties':False,'required':['run_id','snapshot_id','verdict','findings','uncertainty'],
            'properties':{'run_id':{'type':'string'},'snapshot_id':{'type':'string'},'verdict':{'type':'string','enum':['PASS','NEEDS_FIX','BLOCKED']},
                          'findings':{'type':'array','items':{'type':'object','additionalProperties':False,'required':['severity','path','evidence','impact'],
                                      'properties':{'severity':{'type':'string'},'path':{'type':'string'},'evidence':{'type':'string'},'impact':{'type':'string'}}}},
                          'uncertainty':{'type':'array','items':{'type':'string'}}}}
    schema_path=export/'review_schema.json'; schema_path.write_text(json.dumps(schema,indent=2)+'\n',encoding='utf-8')
    prompt={'role':'reviewer_only','run_id':run_id,'snapshot_id':run['snapshot_id'],'goal':payload.get('goal'),'non_goals':payload.get('non_goals',[]),
            'review_decision':decision,
            'files':sorted(manifest.get('files',{})),'deleted_files':deleted_files,
            'file_manifest':manifest.get('files',{}),'support_files':support_files,'checks':payload.get('checks',[]),
            'verification_evidence':verification,'scope_evidence':scope_evidence,
            'instructions':['Read only the exported workspace.','Do not edit files or run project hooks.',
                            'Verifier checks already ran against the source workspace; inspect supplied evidence and frozen support files.',
                            'Only rerun an approved check when necessary; never expand beyond the exported workspace.',
                            'Report only concrete findings.','PASS only when the stated goal/checklist is met.']}
    prompt_path=export/'review_prompt.json'; prompt_path.write_text(json.dumps(prompt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'export':str(export),'workspace':str(work),'schema':str(schema_path),'prompt':str(prompt_path),'snapshot_id':run['snapshot_id']}


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
