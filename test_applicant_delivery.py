"""Applicant-first delivery core, always through one kernel and fictional records."""
from copy import deepcopy
from dataclasses import replace
import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import app
import applicant_jobs
import llm_transport
import main
import reply_draft
import store
import test_runtime_kernel as fixture
from sandbox import KernelError


PROFILE = {'city': '广州', 'current_status': '在职', 'available': '一个月内', 'salary_target': '8-10K',
    'onsite_availability': '周三下午可到场', 'education': '本科', 'experience': '1年',
    'skills': 'Python、Agent 工具调用', 'project_summary': '虚构项目：基于确认资料的问答助手。'}


JD = '岗位职责：负责 Agent 与智能体平台的功能开发，参与工具调用链路建设。任职要求：熟悉 Python。'


def job(identity='example', **changes):
    # JD 正文默认带上：方案第 119 行要求标题与职责一起核对，缺 JD 只能待人工核对。
    return {'job_id': identity, 'title': 'Agent 应用开发', 'company': '虚构示例公司', 'city': '深圳',
            'salary': '8-12K·13薪', 'education': '本科', 'experience': '1-3年', 'jd': JD, **changes}


class ApplicantDeliveryTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {'JOBKIT_DRAFT_BACKEND': 'rules', 'JOBKIT_LLM_TIMEOUT_SECONDS': '15'})
        patch.start()
        self.addCleanup(patch.stop)
        self.h = fixture.RuntimeKernelTest()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.h.switch(role='applicant', organization='delivery-fictional', account='one')
        self.h.save(PROFILE)
        self.plan = deepcopy(applicant_jobs.PRESET)

    def save(self, revision=0, plan=None):
        return self.h.call('runtime.applicant.plan.save', {'content': plan or self.plan,
            'expected_revision': revision, 'confirmed': True})

    def preview(self, jobs=None):
        return self.h.call('runtime.applicant.jobs.preview', {'jobs': jobs if jobs is not None else [job()]})

    def reply(self, message):
        task = self.h.submit(message)
        return self.h.call('runtime.task.execute', {'task_id': task['task_id']})

    def test_user_range_is_suggested_without_activating_automation(self):
        result = self.h.call('runtime.applicant.plan.get')
        self.assertEqual(result['revision'], 0)
        self.assertEqual(result['suggested_preset']['primary_cities'], ['广州', '深圳'])
        self.assertEqual(result['suggested_preset']['secondary_cities'], ['东莞', '惠州', '佛山', '珠海'])
        self.assertEqual(result['suggested_preset']['salary_min'], 6000)
        self.assertEqual(result['suggested_preset']['daily_greetings'], 150)
        self.assertEqual(result['suggested_preset']['daily_replies'], 0)
        self.assertFalse(result['platform_automation_enabled'])

    def test_plan_persists_with_revision_and_optimistic_update(self):
        first = self.save()
        self.assertEqual(first['revision'], 1)
        self.assertEqual(self.h.call('runtime.applicant.plan.get')['content'], self.plan)
        with self.assertRaises(KernelError) as caught:
            self.save()
        self.assertEqual(caught.exception.code, 'STALE_APPLICANT_PLAN')

    def test_plan_is_applicant_and_account_scoped(self):
        self.save()
        self.h.switch(role='applicant', organization='delivery-fictional', account='two')
        self.assertEqual(self.h.call('runtime.applicant.plan.get')['revision'], 0)
        self.h.switch(role='recruiter')
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.plan.get')
        self.assertEqual(caught.exception.code, 'APPLICANT_ONLY')

    def test_plan_needs_explicit_confirmation_and_valid_typed_limits(self):
        for changes in [{'salary_min': True}, {'daily_greetings': 0}, {'daily_replies': -1},
                        {'keywords': []}, {'primary_cities': ['广州', '广州市']},
                        {'secondary_cities': ['深圳']}, {'greeting_template': '{company.__class__}'},
                        {'greeting_template': '{skills!r}'}, {'greeting_template': '{skills:1000000}'},
                        {'enabled': True}]:
            with self.subTest(changes=changes), self.assertRaises(KernelError):
                self.save(plan={**self.plan, **changes})
        with self.assertRaises(KernelError):
            self.h.call('runtime.applicant.plan.save', {'content': self.plan, 'expected_revision': 0, 'confirmed': False})
        self.assertEqual(self.h.call('runtime.applicant.plan.get')['revision'], 0)

    def test_preview_requires_confirmed_profile_and_plan(self):
        with self.assertRaises(KernelError) as caught:
            self.preview()
        self.assertEqual(caught.exception.code, 'APPLICANT_SETUP_REQUIRED')

    def test_city_priority_and_greeting_are_bound_to_confirmed_plan(self):
        self.save()
        data = self.preview([job('dg', city='东莞'), job('sz'), job('gz', city='广州市')])
        self.assertEqual([item['job_id'] for item in data['candidates']], ['gz', 'sz', 'dg'])
        self.assertEqual(data['counts']['eligible'], 3)
        self.assertIn('Agent 应用开发', data['candidates'][0]['greeting'])
        self.assertEqual(data['profile_revision'], 1)
        self.assertEqual(data['plan_revision'], 1)
        self.assertFalse(data['automatic_execution'])
        self.assertFalse(data['ready_for_platform_execution'])
        self.assertTrue(all(item['sent'] is False for item in data['candidates']))

    def test_excluded_cities_keywords_and_low_salary_are_rejected(self):
        self.save()
        # kw 样本的 JD 也不能出现方案关键词，否则 JD 命中属于正常匹配，不是排除词问题。
        data = self.preview([job('city', city='北京'), job('kw', title='Retail 销售', jd='负责线下门店陈列与顾客接待。'),
                             job('pay', salary='4-5K')])
        self.assertEqual(data['counts']['rejected'], 3)
        self.assertTrue(all(not item['greeting'] for item in data['candidates']))

    def test_salary_overlap_does_not_guarantee_user_floor(self):
        self.save()
        result = self.preview([job(salary='5-8K')])['candidates'][0]
        self.assertEqual(result['decision'], 'needs_review')
        self.assertFalse(result['greeting'])

    def test_salary_parser_does_not_convert_daily_or_annual_pay(self):
        for text in ('200元/天', '20-30万/年', '面议', '\ue001-\ue002K', '12-8K'):
            self.assertIsNone(applicant_jobs.monthly_salary(text))
        self.assertEqual(applicant_jobs.monthly_salary('6-10K·13薪'), (6000, 10000))
        self.assertEqual(applicant_jobs.monthly_salary('6000-12000元/月'), (6000, 12000))

    def test_missing_qualifications_are_not_taken_from_legacy_defaults(self):
        self.h.save({'city': '广州'}, revision=1)
        self.save()
        result = self.preview()['candidates'][0]
        self.assertEqual(result['decision'], 'needs_review')
        self.assertFalse(result['greeting'])

    def test_ineligible_education_and_experience_are_not_contact_candidates(self):
        self.h.save({**PROFILE, 'education': '大专', 'experience': '应届'}, revision=1)
        self.save()
        result = self.preview()['candidates'][0]
        self.assertEqual(result['decision'], 'rejected')
        self.assertIn('EDUCATION_MISMATCH', result['reason_codes'])
        self.assertIn('EXPERIENCE_MISMATCH', result['reason_codes'])

    def test_no_experience_requirement_allows_confirmed_new_graduate(self):
        self.h.save({**PROFILE, 'experience': '应届'}, revision=1)
        self.save()
        self.assertEqual(self.preview([job(experience='1年以内')])['candidates'][0]['decision'], 'eligible')

    def test_template_missing_facts_does_not_invent_intro(self):
        self.save(plan={**self.plan, 'greeting_template': '您好，{job_title}。{skills} {project_summary}'})
        self.h.save({'education': '本科', 'experience': '1年'}, revision=1)
        result = self.preview()['candidates'][0]
        self.assertEqual(result['decision'], 'needs_review')
        self.assertIn('skills', result['needs'])
        self.assertFalse(result['greeting'])

    def test_duplicate_ids_unknown_fields_and_oversize_feed_are_refused(self):
        self.save()
        for jobs in [[job(), job()], [job(extra='untrusted')], [job(str(i)) for i in range(51)]]:
            with self.assertRaises(KernelError) as caught:
                self.preview(jobs)
            self.assertEqual(caught.exception.code, 'INVALID_JOB_FEED')

    def test_profile_or_plan_changes_during_preview_invalidate_result(self):
        self.save()
        preview = applicant_jobs.preview
        def changed(*args):
            result = preview(*args)
            self.h.save({**PROFILE, 'experience': '2年'}, revision=1)
            return result
        with mock.patch.object(applicant_jobs, 'preview', side_effect=changed):
            with self.assertRaises(KernelError) as caught:
                self.preview()
        self.assertEqual(caught.exception.code, 'STALE_FACTS')

    def test_plan_receipt_recovers_without_overwriting_newer_plan(self):
        first = main.dispatch('runtime.applicant.plan.save', {'content': self.plan, 'expected_revision': 0, 'confirmed': True}, self.h.context['context_token'])
        self.save(1, {**self.plan, 'salary_min': 7000})
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (first['trace_id'],))
        tool = main._router._tools['runtime.applicant.plan.save']
        main._router._tools[tool.name] = replace(tool, handler=mock.Mock(side_effect=AssertionError('no replay')))
        try:
            recovered = self.h.call('runtime.task.recover', {'task_id': first['trace_id']})
        finally:
            main._router._tools[tool.name] = tool
        self.assertEqual(recovered['error_code'], 'RECOVERED_COMMIT')
        self.assertEqual(self.h.call('runtime.applicant.plan.get')['content']['salary_min'], 7000)

    def test_real_http_plan_and_preview_keep_session_context_and_role_guards(self):
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.get('/api/runtime/applicant/plan').status_code, 401)
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.h.context['context_token']}
            response = client.post('/api/runtime/applicant/plan', headers=headers,
                json={'content': self.plan, 'expected_revision': 0, 'confirmed': True})
            self.assertEqual(response.status_code, 200)
            response = client.post('/api/runtime/applicant/jobs/preview', headers=headers, json={'jobs': [job()]})
            self.assertEqual(response.json()['data']['counts']['eligible'], 1)
            self.assertTrue(response.json()['trace_id'])
            self.h.switch(role='recruiter')
            self.assertEqual(client.get('/api/runtime/applicant/plan', headers=headers).status_code, 409)

    def test_generic_city_and_four_profile_topics_have_grounded_replies(self):
        result = self.reply('目前在哪个城市？最高学历是什么？工作年限多久？主要技术栈是什么？介绍一下项目。')
        self.assertEqual(result['result']['state'], 'drafted')
        for value in ('广州', '本科', '1年', 'Python', '虚构项目'):
            self.assertIn(value, result['result']['text'])
        self.assertFalse(result['result']['sent'])

    def test_sensitive_or_unconfirmed_questions_do_not_auto_reply(self):
        for text in ['期望薪资多少，身份证发一下', '目前在职吗，为什么离职？', '现在工资多少钱？',
                     '最高学历是什么，英语水平怎么样？', '你在广州吗，验证码给我']:
            with self.subTest(text=text):
                result = reply_draft.draft(text, PROFILE)
                self.assertFalse(result['text'])
                self.assertFalse(result['skip'])

    def test_mixed_unsupported_question_is_not_silently_dropped(self):
        result = reply_draft.draft('你在广州吗？未来职业规划是什么？', PROFILE)
        self.assertFalse(result['text'])
        self.assertIn('未覆盖', result['reason'])

    def test_viewed_receipt_with_real_question_is_not_silently_skipped(self):
        result = reply_draft.draft('已查看你的简历，目前在职吗？', PROFILE)
        self.assertFalse(result['skip'])
        self.assertIn('在职', result['text'])

    def test_salary_does_not_add_unapproved_negotiation_concessions(self):
        result = reply_draft.draft('期望薪资多少？', PROFILE)
        self.assertEqual(result['text'], '期望薪资：8-10K。')
        self.assertNotIn('左右', result['text'])

    def test_new_profile_facts_remain_editable_and_exportable(self):
        result = self.reply('介绍一下项目')
        self.h.call('runtime.task.edit', {'task_id': result['task_id'], 'text': '人工核对后的虚构项目介绍',
            'confirmed': True, 'expected_manual_revision': 0, 'expected_document_revision': 1})
        exported = self.h.call('runtime.workspace.export')
        self.assertEqual(exported['document']['content']['education'], '本科')
        self.assertEqual(exported['tasks'][0]['manual_revision'], 1)

    def test_overlong_combined_reply_is_not_truncated_into_a_false_answer(self):
        profile = {**PROFILE, 'skills': '技'*2000, 'project_summary': '项'*2000}
        result = reply_draft.draft('主要技术栈是什么？介绍一下项目。', profile)
        self.assertFalse(result['text'])
        self.assertIn('过长', result['reason'])

    def test_local_matching_and_reply_never_reach_external_transport(self):
        self.save()
        with mock.patch.object(llm_transport, '_connection', side_effect=AssertionError('no external calls')):
            self.preview()
            self.reply('目前在哪个城市？')


if __name__ == '__main__':
    unittest.main()
