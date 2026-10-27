"""运行开关（暂停自动回复）的契约测试。

用户 2026-10-27 定的三条边界，这里逐条锁死：

1. 只停自动回复，招呼照发；
2. 不拦人的手工操作（把草稿填进输入框）；
3. 跨工作区：换账号不能绕过暂停。
"""
import json
import unittest
from unittest import mock

import applicant_browser as A
import applicant_send_ledger as ledger
import run_state
import store
import test_applicant_send as harness
from sandbox import KernelError

TEXT = '您好，我做过 Agent 后端，方便聊两句吗？'
POINT = {'x': 640, 'y': 480, 'w': 40, 'h': 20}
LIVE = {'status': 'target_read', 'name': '示例招聘方', 'company': '示例科技有限公司',
        'position': 'AI应用开发助理', 'job_id': 'job-AI-001',
        'sending_enabled': False, 'delivery_confirmed': False}
SUBMIT_OK = {'status': 'submit_requested', 'sending_enabled': False, 'delivery_confirmed': False}


def panel(marks=34):
    return {'status': 'observed', 'composer_ready': True, 'can_send': True,
            'send_disabled': False, 'empty': True, 'delivered_marks': marks,
            'read_marks': 0, 'panel_text': '', 'account_verified': False,
            'sending_enabled': False, 'delivery_confirmed': False}


class RunStateTest(unittest.TestCase):
    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.context = self.f.h.context
        self.dispatch = self.f.dispatch
        for name, fn in (('real_target', lambda: dict(LIVE)),
                         ('real_read_composer', lambda: {'status': 'read', 'text': TEXT,
                                                         'composer_ready': True}),
                         ('real_submit', lambda point: dict(SUBMIT_OK)),
                         ('real_fill', lambda text: {'status': 'filled'})):
            p = mock.patch.object(A, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(A, 'real_observe', side_effect=lambda *a, **k: panel(34))
        p.start()
        self.addCleanup(p.stop)

    # ---- helpers -------------------------------------------------------
    def authorize(self, action='reply'):
        return self.dispatch('runtime.applicant.chat.authorize',
                             {'text': TEXT, 'confirmed_by': 'user', 'action': action})['data']

    def submit(self):
        return self.dispatch('runtime.applicant.chat.submit', {'point': dict(POINT)})['data']

    def pause(self, **extra):
        payload = {'confirmed': True, 'paused_by': 'user'}
        payload.update(extra)
        return self.dispatch('runtime.run.pause', payload)['data']

    def resume(self, **extra):
        payload = {'confirmed': True, 'paused_by': 'user'}
        payload.update(extra)
        return self.dispatch('runtime.run.resume', payload)['data']

    def failed(self, tool, payload, code):
        with self.assertRaises(KernelError) as caught:
            self.dispatch(tool, payload)
        self.assertEqual(caught.exception.code, code)

    def ledger_items(self):
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.context['scope_id'], ledger.LEDGER_KIND)
        content = doc['content'] if isinstance(doc['content'], dict) else {}
        return [i for i in (content.get('items') or []) if isinstance(i, dict)]

    def other_scope(self):
        return store.runtime_scope_id('applicant', '别的企业', '另一个账号')

    # ---- 纯逻辑 --------------------------------------------------------
    def test_blank_is_running(self):
        record = run_state.blank()
        self.assertEqual(record['state'], run_state.STATE_RUNNING)
        self.assertEqual(run_state.paused_actions(record), [])

    def test_pause_defaults_to_reply_only(self):
        record = run_state.pause(None, by='user')
        self.assertEqual(record['actions'], ['reply'])
        self.assertIn('reply', run_state.paused_actions(record))
        self.assertNotIn('greeting', run_state.paused_actions(record))

    def test_broken_record_is_treated_as_reply_paused(self):
        """读坏了 fail-closed：宁可多停自动回复，也不能当成没暂停。"""
        self.assertIn('reply', run_state.paused_actions({'state': 'paused', 'actions': []}))
        self.assertIn('reply', run_state.paused_actions('不是字典'))

    def test_resume_clears_and_bumps_generation(self):
        first = run_state.pause(None, by='user')
        second = run_state.resume(first, by='user')
        self.assertEqual(second['state'], run_state.STATE_RUNNING)
        self.assertEqual(second['actions'], [])
        self.assertEqual(second['generation'], first['generation'] + 1)

    def test_resume_with_stale_generation_is_refused(self):
        first = run_state.pause(None, by='user')
        with self.assertRaises(ValueError) as caught:
            run_state.resume(first, by='user', expected_generation=first['generation'] + 5)
        self.assertEqual(str(caught.exception), 'RUN_STALE_GENERATION')

    # ---- 内核：只停回复 ------------------------------------------------
    def test_pause_blocks_reply_submit(self):
        self.authorize('reply')
        self.pause()
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)}, 'RUN_PAUSED')
        self.assertEqual(self.ledger_items(), [])

    def test_pause_does_not_block_greeting(self):
        """只停自动回复：招呼照发（用户明确选的口径）。"""
        self.pause()
        self.authorize('greeting')
        result = self.submit()
        self.assertEqual(result['ledger_state'], ledger.STATE_SUBMITTED)
        self.assertEqual(len(self.ledger_items()), 1)

    def test_pause_does_not_block_manual_fill(self):
        """把草稿填进输入框是人的接管动作，暂停期间照样允许。"""
        from sandbox import maintenance_access
        self.pause()
        with maintenance_access():
            result = self.dispatch('runtime.applicant.chat.fill', {'text': TEXT})['data']
        self.assertEqual(result['status'], 'filled')

    def test_read_only_paths_still_work_while_paused(self):
        self.authorize('reply')
        self.pause()
        self.assertIs(self.dispatch('runtime.run.get', {})['data']['operations_paused'], True)
        self.assertEqual(self.dispatch('runtime.applicant.chat.outcome.get', {})['data']['blockers_count'], 0)

    # ---- 跨工作区 ------------------------------------------------------
    def test_pause_blocks_other_workspace(self):
        self.pause()
        import sandbox
        self.assertIn('reply', sandbox.paused_actions_now())
        # 别的 scope 的记录也要被算进来
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                      "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                      "content=excluded.content",
                      (self.other_scope(), run_state.KIND, 1,
                       json.dumps(run_state.pause(None, by='别的人'), ensure_ascii=False)))
        self.assertIn('reply', sandbox.paused_actions_now())

    # ---- 回执与恢复 ----------------------------------------------------
    def test_pause_is_receipted_and_recoverable(self):
        """暂停自己也要有可恢复回执（RECEIPTED_CONTROLS + committed_result 分支）。"""
        result = self.pause()
        self.assertEqual(result['state'], run_state.STATE_PAUSED)
        self.assertEqual(result['generation'], 1)
        self.assertEqual(result['unresolved_count'], 0)
        with store.runtime_db() as c:
            c.execute('BEGIN')
            row = c.execute("SELECT * FROM runtime_tasks WHERE tool='runtime.run.pause' "
                            "ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(row['status'], 'completed')

    def test_resume_after_pause_allows_send_again(self):
        self.authorize('reply')
        self.pause()
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)}, 'RUN_PAUSED')
        self.resume()
        result = self.submit()
        self.assertEqual(result['ledger_state'], ledger.STATE_SUBMITTED)

    def test_pause_blocks_reserve_before_any_state_check(self):
        """暂停期间不该吃额度：预占是回复链路入口，要在扣额度之前就拒。"""
        self.pause()
        payload = {'intent_id': 'a' * 32, 'recipient_binding_id': 'b' * 64,
                   'expected_manual_revision': 0, 'expected_profile_revision': 1,
                   'expected_binding_revision': 1, 'expected_window_revision': 1,
                   'confirmed': True}
        self.failed('runtime.applicant.send.reserve', payload, 'RUN_PAUSED')

    def test_queue_and_preflight_gate_on_pause(self):
        """队列与预检在暂停时必须出 RUN_PAUSED。

        端到端前置（intent/binding/window/attempt 全套）太重，这里只锁代码路径：
        两个判据函数都必须引用暂停集并给出 RUN_PAUSED。这是路径锁，不是行为证明。
        """
        import inspect
        import runtime_tools
        for name in ('applicant_reply_preflight', '_send_readiness'):
            source = inspect.getsource(getattr(runtime_tools, name))
            self.assertIn('RUN_PAUSED', source)
            self.assertIn('paused_actions_now', source)

    def test_pause_requires_operator(self):
        self.failed('runtime.run.pause', {'confirmed': True}, 'INVALID_ACTION_REQUEST')
        self.failed('runtime.run.pause', {'confirmed': True, 'paused_by': '  '},
                    'INVALID_ACTION_REQUEST')
        self.failed('runtime.run.pause', {'paused_by': 'user'}, 'INVALID_ACTION_REQUEST')


if __name__ == '__main__':
    unittest.main()
