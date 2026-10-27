"""G3 单次受限动作的契约测试：授权门、单次性、六态结果、无第二入口。

全程不连平台、不开浏览器：受控页面通道用 mock 代替，只验证协议本身。
"""
import json
import unittest
from unittest import mock

import app
import applicant_action
import applicant_browser
import test_applicant_send as harness
from sandbox import KernelError, maintenance_access

TEXT = '您好，我做过 Agent 后端，方便聊两句吗？'
FILLED = {'status': 'filled', 'focused': True, 'length': len(TEXT), 'sending_enabled': False}
SUBMITTED = {'status': 'submitted', 'cleared': True, 'sending_enabled': False,
             'delivery_confirmed': False}


def seen(text=TEXT, count=1, crashed=False):
    return {'status': 'observed', 'count': count, 'last': text if count else None,
            'crashed': crashed}


def observe_sequence(*values):
    """按顺序返回观察结果，用完后重复最后一个（有界轮询会调多次）。"""
    state = {'n': 0}

    def _fn(*_a, **_k):
        index = min(state['n'], len(values) - 1)
        state['n'] += 1
        return values[index]

    return _fn


class ActionContractTest(unittest.TestCase):
    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.dispatch = self.f.dispatch

    # ---- helpers -------------------------------------------------------
    def authorize(self, **over):
        payload = {'action': 'greeting.submit', 'account_evidence': 'display:张三|mid:abc',
                   'job_id': 'controlled-job-1', 'company': '受控公司', 'text': TEXT}
        payload.update(over)
        with maintenance_access():
            return self.dispatch('runtime.applicant.action.authorize', payload)['data']

    def execute(self, **over):
        payload = {'action': 'greeting.submit', 'text': TEXT, 'confirmed': True}
        payload.update(over)
        with maintenance_access():
            return self.dispatch('runtime.applicant.action.execute', payload)['data']

    def run_with(self, fill=None, submit=None, observes=None):
        fill = fill if fill is not None else FILLED
        submit = submit if submit is not None else SUBMITTED
        observes = observes or (seen(count=0), seen())
        patches = [
            mock.patch.object(applicant_browser, 'controlled_open', return_value={'status': 'already_open'}),
            mock.patch.object(applicant_browser, 'controlled_target',
                              return_value={'job_id': 'controlled-job-1', 'company': '受控公司'}),
            mock.patch.object(applicant_browser, 'controlled_fill', return_value=fill),
            mock.patch.object(applicant_browser, 'controlled_submit', return_value=submit),
            mock.patch.object(applicant_browser, 'controlled_observe',
                              side_effect=observe_sequence(*observes)),
            mock.patch('time.sleep', return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.execute()

    def failed(self, tool, payload, code, maintenance=True):
        with self.assertRaises(KernelError) as caught:
            if maintenance:
                with maintenance_access():
                    self.dispatch(tool, payload)
            else:
                self.dispatch(tool, payload)
        self.assertEqual(caught.exception.code, code)

    # ---- authorization gate -------------------------------------------
    def test_execute_without_authorization_is_refused(self):
        self.failed('runtime.applicant.action.execute',
                    {'action': 'greeting.submit', 'text': TEXT, 'confirmed': True},
                    'CHANNEL_NOT_AUTHORIZED')

    def test_authorization_requires_maintenance(self):
        self.failed('runtime.applicant.action.authorize',
                    {'action': 'greeting.submit', 'account_evidence': 'x', 'job_id': 'j',
                     'company': 'c', 'text': TEXT}, 'MAINTENANCE_REQUIRED', maintenance=False)

    def test_authorization_requires_explicit_confirmation_to_execute(self):
        self.authorize()
        self.failed('runtime.applicant.action.execute',
                    {'action': 'greeting.submit', 'text': TEXT, 'confirmed': False},
                    'INVALID_ACTION_REQUEST')

    def test_extra_fields_are_refused(self):
        self.authorize()
        self.failed('runtime.applicant.action.execute',
                    {'action': 'greeting.submit', 'text': TEXT, 'confirmed': True, 'target': 'x'},
                    'INVALID_ACTION_REQUEST')

    def test_body_must_match_the_registered_digest(self):
        self.authorize()
        self.failed('runtime.applicant.action.execute',
                    {'action': 'greeting.submit', 'text': '换了一句话', 'confirmed': True},
                    'ACTION_BODY_CHANGED')

    def test_authorization_is_single_use(self):
        self.authorize()
        first = self.run_with()
        self.assertIn(first['state'], applicant_action.OUTCOME_STATES)
        self.failed('runtime.applicant.action.execute',
                    {'action': 'greeting.submit', 'text': TEXT, 'confirmed': True},
                    'CHANNEL_AUTHORIZATION_EXHAUSTED')

    def test_registered_authorization_hides_body_text(self):
        view = self.authorize()
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(TEXT, blob)
        self.assertEqual(view['authorization']['max_uses'], 1)
        self.assertFalse(view['sending_enabled'])
        self.assertNotIn('display:张三', blob)

    def test_target_outside_the_controlled_page_is_refused(self):
        # 授权绑定的目标与受控页面不一致时，不允许执行
        doc_target = applicant_action.target_key(job_id='other-job', company='受控公司')
        with mock.patch.object(applicant_action, 'target_key', return_value=doc_target):
            self.authorize()
        self.failed('runtime.applicant.action.execute',
                    {'action': 'greeting.submit', 'text': TEXT, 'confirmed': True},
                    'CHANNEL_TARGET_CHANGED')

    # ---- six-state outcomes -------------------------------------------
    def test_echo_is_observed_but_not_delivery(self):
        self.authorize()
        result = self.run_with()
        self.assertEqual(result['state'], 'observed_reply')
        self.assertTrue(result['consumes_budget'])
        self.assertFalse(result['delivery_confirmed'])
        self.assertFalse(result['read_by_recipient'])
        self.assertFalse(result['sending_enabled'])

    def test_no_echo_stays_unverified(self):
        self.authorize()
        result = self.run_with(observes=(seen(count=0),))
        self.assertEqual(result['state'], 'submitted_unverified')
        self.assertFalse(result['delivery_confirmed'])

    def test_late_echo_is_caught_by_bounded_polling(self):
        self.authorize()
        result = self.run_with(observes=(seen(count=0), seen(count=0), seen(count=0), seen()))
        self.assertEqual(result['state'], 'observed_reply')
        self.assertFalse(result['delivery_confirmed'])

    def test_page_target_mismatch_is_refused_without_consuming(self):
        self.authorize()
        with mock.patch.object(applicant_browser, 'controlled_target',
                               return_value={'job_id': 'other-job', 'company': '其他公司'}):
            self.failed('runtime.applicant.action.execute',
                        {'action': 'greeting.submit', 'text': TEXT, 'confirmed': True},
                        'CHANNEL_TARGET_CHANGED')
        # 目标不符时授权没有被消耗，换回正确目标后仍可用
        result = self.run_with()
        self.assertEqual(result['state'], 'observed_reply')

    def test_fill_failure_means_not_executed(self):
        self.authorize()
        with mock.patch.object(applicant_browser, 'controlled_open', return_value={'status': 'already_open'}), \
                mock.patch.object(applicant_browser, 'controlled_target',
                                  return_value={'job_id': 'controlled-job-1', 'company': '受控公司'}), \
                mock.patch.object(applicant_browser, 'controlled_fill',
                                  side_effect=KernelError('FILL_FAILED', '写入失败')), \
                mock.patch.object(applicant_browser, 'controlled_submit', return_value=SUBMITTED), \
                mock.patch.object(applicant_browser, 'controlled_observe', return_value=seen()), \
                mock.patch('time.sleep', return_value=None):
            with self.assertRaises(KernelError):
                self.execute()

    def test_unknown_result_is_not_retryable(self):
        self.authorize()
        with mock.patch.object(applicant_browser, 'controlled_open', return_value={'status': 'already_open'}), \
                mock.patch.object(applicant_browser, 'controlled_target',
                                  return_value={'job_id': 'controlled-job-1', 'company': '受控公司'}), \
                mock.patch.object(applicant_browser, 'controlled_fill', return_value=FILLED), \
                mock.patch.object(applicant_browser, 'controlled_submit', return_value=SUBMITTED), \
                mock.patch.object(applicant_browser, 'controlled_observe',
                                  side_effect=KernelError('BROWSER_READ_TIMEOUT', '超时',
                                                          outcome_unknown=True)), \
                mock.patch('time.sleep', return_value=None):
            result = self.execute()
        self.assertEqual(result['state'], 'outcome_unknown')
        self.assertFalse(result['retryable'])
        self.assertTrue(result['needs_human'])
        self.assertTrue(result['consumes_budget'])

    def test_concurrent_claim_is_refused_by_optimistic_lock(self):
        """同一份授权被两个进程同时领走时，只有一个能成功。"""
        import runtime_tools
        import store
        self.authorize()
        with store.runtime_db() as c:
            context = store.runtime_context(c)
            context = dict(context, task_id='concurrency-probe')
            doc = store.runtime_document(c, context['scope_id'], 'channel_authorization')
        stale_revision = doc['revision']
        payload = dict(doc['content'], used=1, status='exhausted')
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            first = runtime_tools._action_claim(c, context, stale_revision, payload)
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            second = runtime_tools._action_claim(c, context, stale_revision, payload)
        self.assertTrue(first)
        self.assertFalse(second, '过期 revision 不应再抢到同一份授权')

    def test_result_is_persisted_and_readable(self):
        self.authorize()
        self.run_with()
        with maintenance_access():
            view = self.dispatch('runtime.applicant.action.get',
                                 {'include_authorization': True})['data']
        self.assertEqual(view['status'], 'reported')
        self.assertEqual(view['result']['state'], 'observed_reply')
        self.assertEqual(view['channel'], 'controlled')
        self.assertFalse(view['delivery_confirmed'])
        self.assertEqual(view['authorization']['status'], 'exhausted')

    def test_get_before_any_action(self):
        with maintenance_access():
            view = self.dispatch('runtime.applicant.action.get', {})['data']
        self.assertEqual(view['status'], 'not_started')
        self.assertIsNone(view['result'])

    # ---- routing invariants -------------------------------------------
    def test_only_get_has_http_route(self):
        paths = {getattr(r, 'path', '') for r in app.app.routes}
        self.assertIn('/api/runtime/applicant/action/get', paths)
        for tool in ('authorize', 'execute', 'submit'):
            self.assertNotIn(f'/api/runtime/applicant/action/{tool}', paths)

    def test_execute_is_registered_as_external_write(self):
        import main
        tool = main._router.resolve('runtime.applicant.action.execute')
        self.assertEqual(tool.effect, 'external_write')
        self.assertTrue(tool.maintenance_only)


if __name__ == '__main__':
    unittest.main()
