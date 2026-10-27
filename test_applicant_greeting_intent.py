"""Durable preparation from observed jobs, no real platform sending."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest import mock

import main
import runtime_tools
import store
import app
from fastapi.testclient import TestClient
import test_applicant_delivery as fixture
from sandbox import KernelError


class GreetingIntentTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ApplicantDeliveryTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.f.save()
        self.observation = self.observe()

    def observe(self, jobs=None):
        value = {'status': 'observed', 'jobs': jobs if jobs is not None else [fixture.job()],
                 'account_verified': False, 'sending_enabled': False}
        with mock.patch.object(runtime_tools.applicant_browser, 'read_current_jobs', return_value=value):
            return main.dispatch('runtime.applicant.jobs.read', {}, self.h.context['context_token'])

    def payload(self, **changes):
        return {'observation_task_id': self.observation['trace_id'], 'job_id': 'example',
                'expected_profile_revision': 1, 'expected_plan_revision': 1, **changes}

    def prepare(self, **changes):
        return main.dispatch('runtime.applicant.greeting.prepare', self.payload(**changes), self.h.context['context_token'])

    def test_prepared_record_freezes_evidence_and_cannot_send(self):
        result = self.prepare()['data']
        self.assertEqual(result['state'], 'awaiting_channel_verification')
        self.assertEqual(result['job']['job_id'], 'example')
        self.assertEqual(result['observation_task_id'], self.observation['trace_id'])
        self.assertIn('Agent 应用开发', result['text'])
        for key in ('sent', 'sending_enabled', 'account_verified', 'quota_consumed'):
            self.assertIs(result[key], False)
        self.assertEqual(self.h.call('runtime.applicant.greeting.get', {'job_id': 'example'})['intent'], result)

    def test_repeated_request_and_new_observation_reuse_one_intent(self):
        first = self.prepare()['data']
        self.assertEqual(first, self.prepare()['data'])
        self.observation = self.observe()
        self.assertEqual(first, self.prepare()['data'])
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_documents WHERE kind LIKE 'applicant_greeting:%'").fetchone()[0], 1)

    def test_changed_job_does_not_overwrite_or_repeat_preparation(self):
        self.prepare()
        self.observation = self.observe([fixture.job(company='different fictional company')])
        with self.assertRaises(KernelError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'GREETING_INTENT_CONFLICT')

    def test_concurrent_preparation_creates_one_durable_intent(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.prepare()['data'], range(3)))
        self.assertEqual(len({r['intent_id'] for r in results}), 1)

    def test_plan_change_requires_new_observation(self):
        self.f.save(revision=1, plan={**self.f.plan, 'salary_min': 7000})
        with self.assertRaises(KernelError) as caught:
            self.prepare(expected_plan_revision=2)
        self.assertEqual(caught.exception.code, 'STALE_GREETING_FACTS')

    def test_only_real_read_tool_is_accepted_as_source(self):
        preview = main.dispatch('runtime.applicant.jobs.preview', {'jobs': [fixture.job()]}, self.h.context['context_token'])
        with self.assertRaises(KernelError) as caught:
            self.prepare(observation_task_id=preview['trace_id'])
        self.assertEqual(caught.exception.code, 'OBSERVED_JOBS_REQUIRED')

    def test_corrupt_record_cannot_claim_a_send_or_be_replaced(self):
        saved = self.prepare()['data']
        with store.runtime_db() as c:
            c.execute('UPDATE runtime_documents SET content=? WHERE kind=?',
                      (json.dumps({**saved, 'sent': True}), 'applicant_greeting:example'))
        with self.assertRaises(KernelError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'INVALID_GREETING_INTENT_STATE')
        with self.assertRaises(KernelError):
            self.h.call('runtime.applicant.greeting.get', {'job_id': 'example'})

    def test_http_requires_session_and_current_context(self):
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post('/api/runtime/applicant/greetings/prepare', json=self.payload()).status_code, 401)
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.h.context['context_token']}
            response = client.post('/api/runtime/applicant/greetings/prepare', headers=headers, json=self.payload())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['data']['sent'])
            self.assertTrue(client.get('/api/runtime/applicant/greetings/example', headers=headers).json()['data']['found'])
            self.h.switch(role='recruiter')
            self.assertEqual(client.get('/api/runtime/applicant/greetings/example', headers=headers).status_code, 409)

    def test_stale_and_future_observation_stop(self):
        for delta in (-301, 60):
            with store.runtime_db() as c:
                c.execute('UPDATE runtime_tasks SET updated_at=strftime(\'%s\',\'now\')+? WHERE task_id=?', (delta, self.observation['trace_id']))
            with self.assertRaises(KernelError) as caught:
                self.prepare()
            self.assertEqual(caught.exception.code, 'STALE_JOB_OBSERVATION')

    def test_profile_change_requires_new_observation(self):
        self.h.save({**fixture.PROFILE, 'skills': 'new confirmed skills'}, revision=1)
        with self.assertRaises(KernelError) as caught:
            self.prepare(expected_profile_revision=2)
        self.assertEqual(caught.exception.code, 'STALE_GREETING_FACTS')

    def test_missing_or_ineligible_job_cannot_prepare(self):
        with self.assertRaises(KernelError) as caught:
            self.prepare(job_id='missing')
        self.assertEqual(caught.exception.code, 'OBSERVED_JOB_NOT_FOUND')
        self.observation = self.observe([fixture.job(salary='opaque salary')])
        with self.assertRaises(KernelError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'JOB_NOT_ELIGIBLE')

    def test_no_client_text_or_arbitrary_payload(self):
        for changes in ({'text': 'unverified statement'}, {'expected_profile_revision': True}, {'job_id': '../unsafe'}):
            with self.assertRaises(KernelError) as caught:
                self.prepare(**changes)
            self.assertEqual(caught.exception.code, 'INVALID_GREETING_INTENT')

    def test_foreign_scope_cannot_read_or_prepare(self):
        self.prepare()
        self.h.switch(role='applicant', organization='fictional other', account='other')
        self.assertFalse(self.h.call('runtime.applicant.greeting.get', {'job_id': 'example'})['found'])
        with self.assertRaises(KernelError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'OBSERVED_JOBS_REQUIRED')

    def test_switching_back_still_invalidates_old_observation(self):
        self.h.switch(role='applicant', organization='delivery-fictional', account='two')
        self.h.switch(role='applicant', organization='delivery-fictional', account='one')
        with self.assertRaises(KernelError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'OBSERVED_JOBS_REQUIRED')

    def test_write_and_receipt_roll_back_together(self):
        with mock.patch.object(runtime_tools, 'record_commit', side_effect=KernelError('FIXTURE', 'fixture')):
            with self.assertRaises(KernelError):
                self.prepare()
        self.assertFalse(self.h.call('runtime.applicant.greeting.get', {'job_id': 'example'})['found'])

    def test_recover_commit_does_not_replay_preparation(self):
        saved = self.prepare()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (saved['trace_id'],))
        tool = main._router._tools['runtime.applicant.greeting.prepare']
        with mock.patch.dict(main._router._tools, {tool.name: replace(tool, handler=mock.Mock(side_effect=AssertionError('must not replay')))}):
            recovered = self.h.call('runtime.task.recover', {'task_id': saved['trace_id']})
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(recovered['result'], saved['data'])


if __name__ == '__main__':
    unittest.main()
