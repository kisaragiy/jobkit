"""Explicit salary confirmation binds real read traces; fixtures never contact BOSS."""
from dataclasses import replace
import json
import unittest
from unittest import mock
from fastapi.testclient import TestClient

import app
import main
import runtime_tools
import store
import test_applicant_delivery as fixture
from sandbox import KernelError


class SalaryConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApplicantDeliveryTest();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h=self.f.h;self.f.save()
        self.job=fixture.job(salary='\ue032-\ue033K')
        self.evidence={'job_id':'example','raw_text':self.job['salary'],'status':'needs_confirmation',
            'recognized_text':'8-12K','confidence':0.999,'monthly_salary':[8000,12000],
            'confirmed':False,'image_sha256':'a'*64}
        self.jobs=self.observe_jobs()
        self.salary=self.observe_salary()

    def dispatch(self,tool,payload=None):
        return main.dispatch(tool,payload,self.h.context['context_token'])

    def observe_jobs(self):
        with mock.patch.object(runtime_tools.applicant_browser,'read_current_jobs',return_value={'status':'observed','jobs':[self.job],
            'account_verified':False,'sending_enabled':False}):
            return self.dispatch('runtime.applicant.jobs.read',{})

    def observe_salary(self, evidence=None):
        value={'status':'observed','salaries':[evidence or self.evidence],
               'account_verified':False,'sending_enabled':False,'matching_updated':False,'source_type':'rendered_salary_ocr_unconfirmed'}
        with mock.patch.object(runtime_tools.applicant_browser,'read_current_jobs',return_value=value):
            return self.dispatch('runtime.applicant.salary.read',{})

    def payload(self,**changes):
        return {'job_observation_task_id':self.jobs['trace_id'],'salary_observation_task_id':self.salary['trace_id'],
                'job_id':'example','expected_profile_revision':1,'expected_plan_revision':1,'expected_salary_text':'8-12K','confirmed':True,**changes}

    def confirm(self,**changes):return self.dispatch('runtime.applicant.salary.confirm',self.payload(**changes))

    def preview(self,ids,**changes):
        return self.dispatch('runtime.applicant.jobs.confirmed.preview',{'job_observation_task_id':self.jobs['trace_id'],
            'confirmation_ids':ids,'expected_profile_revision':1,'expected_plan_revision':1,**changes})

    def prepare(self,preview):
        return self.dispatch('runtime.applicant.greeting.prepare',{'observation_task_id':preview['trace_id'],'job_id':'example',
            'expected_profile_revision':1,'expected_plan_revision':1})

    def test_confirmed_salary_can_match_and_prepare_without_sending(self):
        self.assertEqual(self.jobs['data']['counts']['needs_review'],1)
        confirmed=self.confirm()['data']
        preview=self.preview([confirmed['confirmation_id']])
        self.assertEqual(preview['data']['counts']['eligible'],1)
        candidate=preview['data']['candidates'][0]
        self.assertEqual(candidate['salary_source'],'explicitly_confirmed_ocr')
        self.assertEqual(candidate['salary_confirmation_id'],confirmed['confirmation_id'])
        intent=self.prepare(preview)['data']
        self.assertFalse(intent['sent']);self.assertFalse(intent['quota_consumed'])
        self.assertFalse(preview['data']['ready_for_platform_execution'])
        self.assertEqual(intent['job']['salary'],'8-12K')

    def test_repeated_confirmation_reuses_one_immutable_record(self):
        first=self.confirm()['data'];second=self.confirm()['data']
        self.assertEqual(first,second)
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_documents WHERE kind LIKE 'applicant_salary:%'").fetchone()[0],1)

    def test_confirmation_cannot_invent_or_change_ocr_amount(self):
        with self.assertRaises(KernelError) as caught:self.confirm(expected_salary_text='80-120K')
        self.assertEqual(caught.exception.code,'SALARY_TEXT_CHANGED')

    def test_explicit_confirmation_and_typed_revisions_are_required(self):
        for changes in ({'confirmed':False},{'confirmed':1},{'expected_profile_revision':True},{'extra':'unsupported'}):
            with self.subTest(changes=changes),self.assertRaises(KernelError) as caught:self.confirm(**changes)
            self.assertEqual(caught.exception.code,'INVALID_SALARY_REVIEW')

    def test_stale_underlying_read_cannot_be_hidden_by_new_preview(self):
        confirmation=self.confirm()['data'];preview=self.preview([confirmation['confirmation_id']])
        with store.runtime_db() as c:
            c.execute('UPDATE runtime_tasks SET updated_at=updated_at-301 WHERE task_id=?',(self.salary['trace_id'],))
        with self.assertRaises(KernelError) as caught:self.prepare(preview)
        self.assertEqual(caught.exception.code,'STALE_OBSERVATION')

    def test_foreign_scope_reads_are_rejected(self):
        self.h.switch(role='applicant',organization='other',account='other')
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'OBSERVATION_REQUIRED')

    def test_switching_back_does_not_reuse_old_context(self):
        self.h.switch(role='applicant',organization='delivery-fictional',account='two')
        self.h.switch(role='applicant',organization='delivery-fictional',account='one')
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'OBSERVATION_REQUIRED')

    def test_profile_change_invalidates_confirmation_and_preview(self):
        confirmed=self.confirm()['data']
        self.h.save({**fixture.PROFILE,'skills':'new confirmed facts'},revision=1)
        with self.assertRaises(KernelError) as caught:self.preview([confirmed['confirmation_id']],expected_profile_revision=2)
        self.assertEqual(caught.exception.code,'STALE_SALARY_FACTS')

    def test_unrelated_raw_text_job_or_invalid_ocr_is_rejected(self):
        changes=[{'raw_text':'different'},{'job_id':'other'},{'confidence':True},
                 {'confidence':0.5},{'monthly_salary':[80000,120000]},
                 {'image_sha256':'not-an-image-hash'},{'status':'needs_review'},{'confirmed':True}]
        for change in changes:
            self.salary=self.observe_salary({**self.evidence,**change})
            with self.subTest(change=change),self.assertRaises(KernelError) as caught:self.confirm()
            self.assertEqual(caught.exception.code,'SALARY_EVIDENCE_MISMATCH')

    def test_nonfinite_ocr_result_is_rejected_by_kernel_before_storage(self):
        with self.assertRaises(KernelError) as caught:self.observe_salary({**self.evidence,'confidence':float('nan')})
        self.assertEqual(caught.exception.code,'INVALID_TOOL_RESULT')

    def test_provided_job_preview_is_not_real_observation(self):
        fake=self.dispatch('runtime.applicant.jobs.preview',{'jobs':[self.job]})
        with self.assertRaises(KernelError) as caught:self.confirm(job_observation_task_id=fake['trace_id'])
        self.assertEqual(caught.exception.code,'OBSERVATION_REQUIRED')

    def test_confirmation_cannot_be_reused_for_a_different_job_read(self):
        confirmed=self.confirm()['data'];self.jobs=self.observe_jobs()
        with self.assertRaises(KernelError) as caught:self.preview([confirmed['confirmation_id']])
        self.assertEqual(caught.exception.code,'SALARY_EVIDENCE_MISMATCH')

    def test_rollback_preserves_no_confirmation_without_receipt(self):
        with mock.patch.object(runtime_tools,'record_commit',side_effect=KernelError('FIXTURE','fixture')):
            with self.assertRaises(KernelError):self.confirm()
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_documents WHERE kind LIKE 'applicant_salary:%'").fetchone()[0],0)

    def test_recovery_uses_receipt_without_replaying_or_requiring_fresh_sources(self):
        saved=self.confirm()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?",(saved['trace_id'],))
            c.execute('UPDATE runtime_tasks SET updated_at=updated_at-301 WHERE task_id=?',(self.salary['trace_id'],))
        tool=main._router._tools['runtime.applicant.salary.confirm']
        with mock.patch.dict(main._router._tools,{tool.name:replace(tool,handler=mock.Mock(side_effect=AssertionError('must not replay')))}):
            recovered=self.h.call('runtime.task.recover',{'task_id':saved['trace_id']})
        self.assertEqual(recovered['result'],saved['data']);self.assertEqual(recovered['status'],'completed')

    def test_corrupt_confirmation_cannot_affect_matching(self):
        confirmed=self.confirm()['data']
        with store.runtime_db() as c:
            c.execute('UPDATE runtime_documents SET content=? WHERE kind=?',(json.dumps({**confirmed,'salary_text':'80-120K'}),
                      'applicant_salary:'+confirmed['confirmation_id']))
        with self.assertRaises(KernelError) as caught:self.preview([confirmed['confirmation_id']])
        self.assertEqual(caught.exception.code,'INVALID_SALARY_CONFIRMATION')

    def test_rejected_low_salary_stays_rejected_even_when_confirmed(self):
        self.evidence={**self.evidence,'recognized_text':'4-5K','monthly_salary':[4000,5000]}
        self.salary=self.observe_salary()
        confirmed=self.confirm(expected_salary_text='4-5K')['data'];preview=self.preview([confirmed['confirmation_id']])
        self.assertEqual(preview['data']['counts']['rejected'],1)
        with self.assertRaises(KernelError) as caught:self.prepare(preview)
        self.assertEqual(caught.exception.code,'JOB_NOT_ELIGIBLE')

    def test_http_routes_require_local_session_and_current_context(self):
        with TestClient(app.app,base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post('/api/runtime/applicant/salaries/confirm',json=self.payload()).status_code,401)
            client.get('/workspace');headers={'X-Jobkit-Context':self.h.context['context_token']}
            response=client.post('/api/runtime/applicant/salaries/confirm',json=self.payload(),headers=headers)
            self.assertEqual(response.status_code,200)
            self.h.switch(role='recruiter')
            self.assertEqual(client.post('/api/runtime/applicant/salaries/confirm',json=self.payload(),headers=headers).status_code,409)


if __name__=='__main__':unittest.main()
