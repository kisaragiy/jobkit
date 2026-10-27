"""G4 规划工具的内核契约：候选 → 配额/去重闸门 → 可执行前缀（不触网）。"""
import json
import unittest
from unittest import mock

import app
import applicant_browser
import main
import store
import test_applicant_send as harness
from sandbox import KernelError, maintenance_access

TEXT = '您好，我做过 Agent 后端，方便聊两句吗？'
FILLED = {'status': 'filled', 'focused': True, 'length': len(TEXT), 'sending_enabled': False}
SUBMITTED = {'status': 'submitted', 'cleared': True, 'sending_enabled': False,
             'delivery_confirmed': False}
TARGET = {'job_id': 'controlled-job-1', 'company': '受控公司'}


class GreetingPlanContractTest(unittest.TestCase):
    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.dispatch = self.f.dispatch
        with store.runtime_db() as c:
            self.context = store.runtime_context(c)
        self.scope = self.context['scope_id']

    # ---- helpers -------------------------------------------------------
    def seed_search(self, jobs):
        content = {'jobs': jobs, 'profile_revision': 1, 'plan_revision': 1,
                   'status': 'finished', 'sending_enabled': False}
        with store.runtime_db() as c:
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
                      "DO UPDATE SET revision=revision+1, content=excluded.content",
                      (self.scope, 'applicant_job_search', 1,
                       json.dumps(content, ensure_ascii=False)))

    def plan(self, **payload):
        with maintenance_access():
            return self.dispatch('runtime.applicant.greeting.plan', payload)['data']

    def attempts(self):
        with store.runtime_db() as c:
            doc = store.runtime_document(c, self.scope, 'applicant_greeting_attempts')
        content = doc['content'] if isinstance(doc['content'], dict) else {}
        return content.get('items') or []

    def run_action(self, observes):
        with maintenance_access():
            self.dispatch('runtime.applicant.action.authorize', {
                'action': 'greeting.submit', 'account_evidence': 'controlled:local',
                'job_id': TARGET['job_id'], 'company': TARGET['company'], 'text': TEXT})
            with mock.patch.object(applicant_browser, 'controlled_open',
                                   return_value={'status': 'already_open'}), \
                    mock.patch.object(applicant_browser, 'controlled_target',
                                      return_value=TARGET), \
                    mock.patch.object(applicant_browser, 'controlled_fill', return_value=FILLED), \
                    mock.patch.object(applicant_browser, 'controlled_submit',
                                      return_value=SUBMITTED), \
                    mock.patch.object(applicant_browser, 'controlled_observe',
                                      side_effect=observes), \
                    mock.patch('time.sleep', return_value=None):
                return self.dispatch('runtime.applicant.action.execute', {
                    'action': 'greeting.submit', 'text': TEXT, 'confirmed': True})['data']

    def failed(self, payload, code):
        with self.assertRaises(KernelError) as caught:
            with maintenance_access():
                self.dispatch('runtime.applicant.greeting.plan', payload)
        self.assertEqual(caught.exception.code, code)

    # ---- cases ---------------------------------------------------------
    def test_no_candidates_reports_source(self):
        result = self.plan()
        self.assertEqual(result['source'], 'no_candidates')
        self.assertEqual(result['proceed'], [])
        self.assertEqual(result['candidates_total'], 0)
        self.assertFalse(result['sending_enabled'])

    def test_candidates_become_proceed_prefix(self):
        self.seed_search([{'job_id': 'j1', 'company': '甲'}, {'job_id': 'j2', 'company': '乙'}])
        result = self.plan()
        self.assertEqual(result['source'], 'bounded_browser_search')
        self.assertEqual(result['proceed'], ['j1', 'j2'])
        self.assertEqual(result['limit'], 150)
        self.assertIsNone(result['stop_reason'])

    def test_limit_truncates_batch(self):
        self.seed_search([{'job_id': 'j%d' % i, 'company': '甲'} for i in range(6)])
        result = self.plan(configured_limit=2)
        self.assertEqual(result['proceed'], ['j0', 'j1'])
        self.assertEqual(result['stop_reason'], 'DAILY_LIMIT')

    def test_platform_limit_is_stricter(self):
        self.seed_search([{'job_id': 'j%d' % i, 'company': '甲'} for i in range(6)])
        result = self.plan(configured_limit=150, platform_limit=1)
        self.assertEqual(result['proceed'], ['j0'])
        self.assertEqual(result['limit'], 1)

    def test_closed_job_is_skipped(self):
        self.seed_search([{'job_id': 'j1', 'company': '甲', 'job_open': False},
                          {'job_id': 'j2', 'company': '乙'}])
        result = self.plan()
        self.assertEqual(result['proceed'], ['j2'])
        self.assertEqual(result['decisions'][0]['reason'], 'JOB_CLOSED')

    def test_action_records_an_attempt_and_blocks_repeat(self):
        self.seed_search([{'job_id': 'j1', 'company': '甲'}])
        first = self.run_action([
            {'status': 'observed', 'count': 0, 'last': None, 'crashed': False},
            {'status': 'observed', 'count': 1, 'last': TEXT, 'crashed': False},
        ])
        self.assertEqual(first['state'], 'observed_reply')
        items = self.attempts()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['job_id'], TARGET['job_id'])
        self.assertEqual(items[0]['state'], 'observed_reply')

        self.seed_search([{'job_id': TARGET['job_id'], 'company': TARGET['company']}])
        result = self.plan()
        self.assertEqual(result['decisions'][0]['reason'], 'ALREADY_CONTACTED')
        self.assertEqual(result['proceed'], [])
        self.assertEqual(result['used'], 1)

    def test_unknown_outcome_consumes_quota_and_blocks(self):
        with maintenance_access():
            self.dispatch('runtime.applicant.action.authorize', {
                'action': 'greeting.submit', 'account_evidence': 'controlled:local',
                'job_id': TARGET['job_id'], 'company': TARGET['company'], 'text': TEXT})
            with mock.patch.object(applicant_browser, 'controlled_open',
                                   return_value={'status': 'already_open'}), \
                    mock.patch.object(applicant_browser, 'controlled_target',
                                      return_value=TARGET), \
                    mock.patch.object(applicant_browser, 'controlled_fill', return_value=FILLED), \
                    mock.patch.object(applicant_browser, 'controlled_submit',
                                      return_value=SUBMITTED), \
                    mock.patch.object(applicant_browser, 'controlled_observe',
                                      side_effect=KernelError('BROWSER_READ_TIMEOUT', '超时',
                                                              outcome_unknown=True)), \
                    mock.patch('time.sleep', return_value=None):
                self.dispatch('runtime.applicant.action.execute', {
                    'action': 'greeting.submit', 'text': TEXT, 'confirmed': True})
        self.seed_search([{'job_id': TARGET['job_id'], 'company': TARGET['company']}])
        result = self.plan()
        self.assertEqual(result['used'], 1)
        self.assertEqual(result['unresolved'], 1)
        self.assertEqual(result['decisions'][0]['reason'], 'UNRESOLVED_ATTEMPT')

    def test_quota_blocks_second_action_without_consuming_authorization(self):
        self.seed_search([{'job_id': 'j1', 'company': '甲'}])
        self.plan(configured_limit=1)          # 落 settings: limit=1
        first = self.run_action([
            {'status': 'observed', 'count': 0, 'last': None, 'crashed': False},
            {'status': 'observed', 'count': 1, 'last': TEXT, 'crashed': False},
        ])
        self.assertEqual(first['state'], 'observed_reply')
        # 额度已满：再授权 + 执行必须被额度挡住（挡在 claim 之前，授权不消耗）
        with maintenance_access():
            self.dispatch('runtime.applicant.action.authorize', {
                'action': 'greeting.submit', 'account_evidence': 'controlled:local',
                'job_id': TARGET['job_id'], 'company': TARGET['company'], 'text': TEXT})
            with mock.patch.object(applicant_browser, 'controlled_open',
                                   return_value={'status': 'already_open'}), \
                    mock.patch.object(applicant_browser, 'controlled_target',
                                      return_value=TARGET), \
                    mock.patch.object(applicant_browser, 'controlled_fill', return_value=FILLED), \
                    mock.patch.object(applicant_browser, 'controlled_submit',
                                      return_value=SUBMITTED), \
                    mock.patch.object(applicant_browser, 'controlled_observe',
                                      return_value={'status': 'observed', 'count': 1,
                                                    'last': TEXT, 'crashed': False}):
                with self.assertRaises(KernelError) as caught:
                    self.dispatch('runtime.applicant.action.execute', {
                        'action': 'greeting.submit', 'text': TEXT, 'confirmed': True})
        self.assertEqual(caught.exception.code, 'DAILY_LIMIT_REACHED')
        # 授权未被消耗：文档里的用量仍是 0（额度拦在 claim 之前）
        import runtime_tools
        with store.runtime_db() as c:
            doc = store.runtime_document(c, self.scope, runtime_tools.ACTION_AUTH_KIND)
        self.assertEqual(doc['content']['used'], 0)
        self.assertEqual(doc['content']['status'], 'ready')

    def test_settings_persist_platform_limit(self):
        self.plan(configured_limit=150, platform_limit=20)
        with store.runtime_db() as c:
            doc = store.runtime_document(c, self.scope, 'applicant_greeting_settings')
        self.assertEqual(doc['content']['platform_limit'], 20)
        self.assertEqual(doc['content']['platform_limit_source'], 'manual')
        self.assertIsNotNone(doc['content']['platform_limit_expires_at'])

    def test_invalid_limit_is_refused(self):
        self.failed({'configured_limit': -1}, 'INVALID_GREETING_PLAN')

    def test_extra_fields_are_refused(self):
        self.failed({'limit': 10}, 'INVALID_GREETING_PLAN')

    def test_plan_has_no_http_route(self):
        paths = {getattr(route, 'path', '') for route in app.app.routes}
        self.assertNotIn('/api/runtime/applicant/greetings/plan', paths)
        self.assertNotIn('/api/runtime/applicant/greeting/plan', paths)

    def test_plan_tool_is_maintenance_only_state(self):
        tool = main._router.resolve('runtime.applicant.greeting.plan')
        self.assertTrue(tool.maintenance_only)
        self.assertEqual(tool.effect, 'state')


if __name__ == '__main__':
    unittest.main()
