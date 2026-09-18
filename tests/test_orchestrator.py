import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from orch.core import Orchestrator
from orch.codex_review import prepare_review


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'orch'; self.root.mkdir()
        self.ws=Path(self.tmp.name)/'ws'; self.ws.mkdir()
        self.orch=Orchestrator(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self,tasks,revision='p1'):
        path=self.root/'plan.json'
        path.write_text(json.dumps({'schema_version':1,'plan_revision':revision,'tasks':tasks}),encoding='utf-8')
        return self.orch.load_plan(path)

    def task(self,tid='T1',deps=None,checks=None,review=False,protected=None,max_attempts=2):
        return {'id':tid,'goal':'test','workspace':str(self.ws),'dependencies':deps or [],'allowed_paths':[f'{tid}.json'],
                'protected_paths':protected or {},'checks':checks or [],'required_review':review,'owner_acceptance':False,
                'publication':{'kind':'none'},'max_attempts':max_attempts}

    def write_result(self,claim,tid='T1',value=1):
        (self.ws/f'{tid}.json').write_text(json.dumps({'value':value})+'\n',encoding='utf-8')
        receipt=self.root/f'{tid}-receipt.json'
        receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':tid,'changed_paths':[f'{tid}.json']})+'\n',encoding='utf-8')
        cap=Path(claim['capability_file'])
        lease=self.orch.lease_from_capability(claim['run_id'],cap)
        self.orch.submit(claim['run_id'],lease,receipt)
        self.orch.quiesce(claim['run_id'],lease)
        return self.orch.verify(claim['run_id'])

    def test_dependency_sequence_and_no_work(self):
        self.load([self.task('T1'),self.task('T2',deps=['T1'])])
        c1=self.orch.claim('w1'); self.assertEqual(c1['task_id'],'T1')
        self.write_result(c1,'T1'); self.orch.complete(c1['run_id'])
        c2=self.orch.claim('w2'); self.assertEqual(c2['task_id'],'T2')
        self.write_result(c2,'T2'); self.orch.complete(c2['run_id'])
        self.assertEqual(self.orch.claim('w3')['status'],'NO_WORK')

    def test_single_writer_busy(self):
        self.load([self.task('T1'),self.task('T2')])
        first=self.orch.claim('w1')
        second=self.orch.claim('w2')
        self.assertEqual(first['status'],'CLAIMED'); self.assertEqual(second['status'],'BUSY')
        self.assertEqual(second['active']['run_id'],first['run_id'])

    def test_fifo_order_is_preserved_across_separately_loaded_plans(self):
        self.load([self.task("Z-FIRST")], revision="fifo-p1")
        path = self.root / "fifo-p2.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "fifo-p2",
            "tasks": [self.task("A-SECOND")],
        }) + "\n")
        self.orch.load_plan(path)
        first = self.orch.claim("w1")
        self.assertEqual(first["task_id"], "Z-FIRST")
        status = self.orch.status()
        queued = {item["task_id"]: item["queue_seq"] for item in status["tasks"]}
        self.assertLess(queued["Z-FIRST"], queued["A-SECOND"])

    def test_independent_workspaces_can_be_claimed_concurrently(self):
        ws2 = Path(self.tmp.name) / "ws2"
        ws2.mkdir()
        first = self.task("P1-T1")
        first["project_id"] = "project-one"
        second = self.task("P2-T1")
        second["project_id"] = "project-two"
        second["workspace"] = str(ws2)
        self.load([first, second], revision="multi-project")
        c1 = self.orch.claim("w1")
        c2 = self.orch.claim("w2")
        self.assertEqual(c1["task_id"], "P1-T1")
        self.assertEqual(c2["task_id"], "P2-T1")
        self.assertNotEqual(c1["context"]["writer_key"], c2["context"]["writer_key"])
        self.assertEqual(len(self.orch.reconcile()["active_runs"]), 2)

    def test_shared_writer_key_blocks_concurrent_claim_across_workspaces(self):
        ws2 = Path(self.tmp.name) / "ws2"
        ws2.mkdir()
        first = self.task("SHARED-1")
        second = self.task("SHARED-2")
        second["workspace"] = str(ws2)
        first["writer_key"] = "git:shared-fixture"
        second["writer_key"] = "git:shared-fixture"
        self.load([first, second], revision="shared-writer")
        c1 = self.orch.claim("w1")
        c2 = self.orch.claim("w2")
        self.assertEqual(c1["status"], "CLAIMED")
        self.assertEqual(c2["status"], "BUSY")
        self.assertEqual(c2["active"]["run_id"], c1["run_id"])

    def test_project_scoped_claim_can_skip_earlier_other_project(self):
        ws2 = Path(self.tmp.name) / "ws2"
        ws2.mkdir()
        first = self.task("P1-FIRST")
        first["project_id"] = "project-one"
        second = self.task("P2-ONLY")
        second["project_id"] = "project-two"
        second["workspace"] = str(ws2)
        self.load([first, second], revision="scoped-claim")
        selected = self.orch.claim("w2", project_id="project-two")
        self.assertEqual(selected["task_id"], "P2-ONLY")
        self.assertEqual(selected["project_id"], "project-two")
        next_one = self.orch.next_work(project_id="project-one")
        self.assertEqual(next_one["status"], "READY")
        self.assertEqual(next_one["task_id"], "P1-FIRST")

    def test_scope_escape_rejected(self):
        self.load([self.task('T1')]); c=self.orch.claim('w')
        receipt=self.root/'bad.json'; receipt.write_text(json.dumps({'run_id':c['run_id'],'task_id':'T1','changed_paths':['../escape']})+'\n')
        lease=self.orch.lease_from_capability(c['run_id'],Path(c['capability_file']))
        with self.assertRaisesRegex(ValueError,'path_not_allowed'):
            self.orch.submit(c['run_id'],lease,receipt)

    def test_failed_check_feedback_moves_to_new_chat_attempt(self):
        check={'id':'fail','argv':[sys.executable,'-c','import sys; sys.exit(7)'],'cwd':'.','timeout_sec':5}
        self.load([self.task('T1',checks=[check])])
        c1=self.orch.claim('w1'); result=self.write_result(c1,'T1')
        self.assertEqual(result['status'],'NEEDS_FIX')
        c2=self.orch.claim('w2')
        self.assertEqual(c2['attempt'],2)
        self.assertEqual(c2['context']['feedback']['kind'],'verification_failure')
        self.assertEqual(c2['context']['previous_snapshot_id'],result['snapshot_id'])

    def test_protected_change_blocks_verification_without_stuck_writer(self):
        sentinel=self.ws/'owner.txt'; sentinel.write_text('keep\n')
        digest=hashlib.sha256(sentinel.read_bytes()).hexdigest()
        self.load([self.task('T1',protected={'owner.txt':digest})])
        c=self.orch.claim('w'); (self.ws/'T1.json').write_text('{"value":1}\n'); sentinel.write_text('changed\n')
        receipt=self.root/'r.json'; receipt.write_text(json.dumps({'run_id':c['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        cap=Path(c['capability_file']); lease=self.orch.lease_from_capability(c['run_id'],cap)
        self.orch.submit(c['run_id'],lease,receipt); quiesced=self.orch.quiesce(c['run_id'],lease)
        self.assertTrue(quiesced['capability_revoked']); self.assertFalse(cap.exists())
        blocked=self.orch.verify(c['run_id'])
        self.assertEqual(blocked['status'],'BLOCKED')
        self.assertIn('protected_path_changed',blocked['reason'])
        self.assertEqual(self.orch.reconcile()['status'],'CLEAN')
        self.assertEqual(self.orch.next_work()['status'],'NO_WORK')

    def test_review_is_snapshot_bound_and_feedback_reappears(self):
        self.load([self.task('T1',review=True)])
        c1=self.orch.claim('w1'); verified=self.write_result(c1,'T1')
        self.assertEqual(verified['status'],'REVIEWING')
        wrong=self.root/'wrong.json'; wrong.write_text(json.dumps({'run_id':c1['run_id'],'snapshot_id':'sha256:wrong','verdict':'PASS','findings':[],'uncertainty':[]})+'\n')
        with self.assertRaisesRegex(ValueError,'stale_review'):
            self.orch.import_review(c1['run_id'],wrong)
        report=self.root/'review.json'; report.write_text(json.dumps({'run_id':c1['run_id'],'snapshot_id':verified['snapshot_id'],'verdict':'NEEDS_FIX','findings':[{'severity':'important','path':'T1.json','evidence':'fixture','impact':'repair'}],'uncertainty':[]})+'\n')
        self.orch.import_review(c1['run_id'],report)
        c2=self.orch.claim('w2')
        self.assertEqual(c2['context']['feedback']['kind'],'review')
        self.assertEqual(c2['context']['feedback']['findings'][0]['path'],'T1.json')

    def test_owner_approval_is_snapshot_bound(self):
        task=self.task('T1'); task['owner_acceptance']=True
        self.load([task])
        c=self.orch.claim('w'); self.write_result(c,'T1')
        self.assertEqual(self.orch.complete(c['run_id'])['status'],'WAITING_OWNER')
        approved=self.orch.approve(c['run_id'],'fixture approval')
        self.assertEqual(approved['status'],'APPROVED')
        self.assertEqual(self.orch.complete(c['run_id'])['status'],'COMPLETE')

    def test_pause_blocks_dispatch_until_resume(self):
        self.load([self.task('T1')])
        paused=self.orch.pause('fixture maintenance')
        self.assertEqual(paused['status'],'PAUSED')
        self.assertEqual(self.orch.claim('w')['status'],'PAUSED')
        self.assertEqual(self.orch.next_work()['status'],'PAUSED')
        self.assertEqual(self.orch.resume()['status'],'RESUMED')
        self.assertEqual(self.orch.claim('w')['status'],'CLAIMED')

    def test_abort_retry_releases_writer_and_creates_new_attempt(self):
        self.load([self.task('T1')])
        first=self.orch.claim('w1')
        aborted=self.orch.abort(first['run_id'],'synthetic crash recovery',retry=True)
        self.assertEqual(aborted['task_status'],'NEEDS_FIX')
        second=self.orch.claim('w2')
        self.assertEqual(second['status'],'CLAIMED')
        self.assertEqual(second['attempt'],2)
        self.assertNotEqual(second['run_id'],first['run_id'])

    def test_review_export_contains_check_support_and_verifier_evidence(self):
        checks_dir=self.ws/'checks'; checks_dir.mkdir()
        check_file=checks_dir/'check.py'
        check_file.write_text("print('support-ok')\n",encoding='utf-8')
        check={'id':'support','argv':[sys.executable,'checks/check.py'],'cwd':'.','timeout_sec':5}
        self.load([self.task('T1',checks=[check],review=True)])
        claim=self.orch.claim('w')
        verified=self.write_result(claim,'T1')
        self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id'])
        workspace=Path(prepared['workspace'])
        self.assertTrue((workspace/'checks/check.py').is_file())
        self.assertTrue((workspace/'verification_evidence/support.json').is_file())
        self.assertTrue((workspace/'verification_evidence/scope.json').is_file())
        prompt=json.loads(Path(prepared['prompt']).read_text())
        self.assertIn('checks/check.py',prompt['support_files'])
        self.assertEqual(prompt['verification_evidence'][0]['exit_code'],0)
        self.assertEqual(prompt['scope_evidence']['status'],'NON_GIT_UNAVAILABLE')
        self.assertEqual(prompt['scope_evidence']['evidence_file'],'verification_evidence/scope.json')

    def test_dependency_cycle_is_rejected_at_plan_load(self):
        one=self.task('T1',deps=['T2']); two=self.task('T2',deps=['T1'])
        with self.assertRaisesRegex(ValueError,'dependency_cycle'):
            self.load([one,two])

    def test_task_id_conflict_across_plan_revisions_is_rejected(self):
        self.load([self.task('T1')],revision='p1')
        path=self.root/'p2.json'
        path.write_text(json.dumps({'schema_version':1,'plan_revision':'p2','tasks':[self.task('T1')]})+'\n')
        with self.assertRaisesRegex(ValueError,'task_id_conflict:T1'):
            self.orch.load_plan(path)

    def test_capability_is_revoked_on_quiesce_and_abort(self):
        self.load([self.task('T1'),self.task('T2')])
        c1=self.orch.claim('w1'); cap1=Path(c1['capability_file']); lease1=self.orch.lease_from_capability(c1['run_id'],cap1)
        (self.ws/'T1.json').write_text('{"value":1}\n')
        receipt=self.root/'cap-r.json'; receipt.write_text(json.dumps({'run_id':c1['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        self.orch.submit(c1['run_id'],lease1,receipt); self.orch.quiesce(c1['run_id'],lease1)
        self.assertFalse(cap1.exists())
        self.orch.verify(c1['run_id']); self.orch.complete(c1['run_id'])
        c2=self.orch.claim('w2'); cap2=Path(c2['capability_file'])
        self.assertTrue(cap2.exists()); self.orch.abort(c2['run_id'],'stop',retry=False)
        self.assertFalse(cap2.exists())

    def test_invalid_check_and_protected_hash_are_rejected_at_plan_load(self):
        bad_check=self.task('T1',checks=[{'id':'bad','argv':[],'cwd':'.'}])
        with self.assertRaisesRegex(ValueError,'invalid_check_argv'):
            self.load([bad_check],revision='bad-check')
        bad_hash=self.task('T2',protected={'owner.txt':'not-a-sha'})
        with self.assertRaisesRegex(ValueError,'invalid_protected_hash'):
            self.load([bad_hash],revision='bad-hash')

    def test_review_export_records_deleted_files_without_recreating_them(self):
        task=self.task('T1',review=True); self.load([task])
        target=self.ws/'T1.json'; target.write_text('{"old":true}\n',encoding='utf-8')
        claim=self.orch.claim('w'); target.unlink()
        receipt=self.root/'delete-review-receipt.json'
        receipt.write_text(json.dumps({'run_id':claim['run_id'],'task_id':'T1','changed_paths':['T1.json']})+'\n')
        lease=self.orch.lease_from_capability(claim['run_id'],Path(claim['capability_file']))
        self.orch.submit(claim['run_id'],lease,receipt); self.orch.quiesce(claim['run_id'],lease)
        verified=self.orch.verify(claim['run_id']); self.assertEqual(verified['status'],'REVIEWING')
        prepared=prepare_review(self.orch,claim['run_id']); prompt=json.loads(Path(prepared['prompt']).read_text())
        self.assertEqual(prompt['deleted_files'],['T1.json'])
        self.assertTrue(prompt['file_manifest']['T1.json']['deleted'])
        self.assertFalse((Path(prepared['workspace'])/'T1.json').exists())

    def test_plan_revision_digest_conflict(self):
        self.load([self.task('T1')],revision='same')
        path=self.root/'other.json'; path.write_text(json.dumps({'schema_version':1,'plan_revision':'same','tasks':[self.task('T2')]})+'\n')
        with self.assertRaisesRegex(ValueError,'plan_revision_digest_conflict'):
            self.orch.load_plan(path)

if __name__=='__main__':
    unittest.main()
