"""发送结果闸门的契约测试。

针对 2026-10-27 20:29 的真实事故：一次提交读超时（outcome_unknown），
17 秒后又对同一对象提交了一次。本测试锁死三件事：

1. 点过发送就必须留下「未核对」记录，不能当作没发生；
2. 未核对期间，真实通道的任何外部写入都拒绝（沙箱层拦，不只 handler）；
3. 解除只能走人工核对，且核对结论决定这个对象以后还能不能发。
"""
import json
import unittest
from unittest import mock

import applicant_browser as A
import applicant_send_ledger as ledger
import store
import test_applicant_send as harness
from sandbox import KernelError, maintenance_access

TEXT = '您好，我做过 Agent 后端，方便聊两句吗？'
POINT = {'x': 640, 'y': 480, 'w': 40, 'h': 20}
LIVE = {'status': 'target_read', 'name': '示例招聘方', 'company': '示例科技有限公司',
        'position': 'AI应用开发助理', 'job_id': 'job-AI-001',
        'sending_enabled': False, 'delivery_confirmed': False}
OTHER = dict(LIVE, name='李先生', company='另一家公司', position='后端开发')
SUBMIT_OK = {'status': 'submit_requested', 'sending_enabled': False, 'delivery_confirmed': False}


def panel(marks=34):
    return {'status': 'observed', 'composer_ready': True, 'can_send': True,
            'send_disabled': False, 'empty': True, 'delivered_marks': marks,
            'read_marks': 0, 'panel_text': '', 'account_verified': False,
            'sending_enabled': False, 'delivery_confirmed': False}


class OutcomeGateTest(unittest.TestCase):
    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.context = self.f.h.context
        self.dispatch = self.f.dispatch
        self.target = dict(LIVE)
        self.marks = 34
        self.submit_effect = None
        for name, fn in (('real_target', lambda: dict(self.target)),
                         ('real_read_composer', lambda: {'status': 'read', 'text': TEXT,
                                                         'composer_ready': True})):
            p = mock.patch.object(A, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(A, 'real_observe', side_effect=lambda *a, **k: panel(self.marks))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(A, 'real_submit', side_effect=self._submit)
        p.start()
        self.addCleanup(p.stop)

    def _submit(self, point):
        if self.submit_effect is not None:
            raise self.submit_effect
        return dict(SUBMIT_OK)

    # ---- helpers -------------------------------------------------------
    def authorize(self, *, action='greeting', text=TEXT, by='user'):
        payload = {'text': text, 'confirmed_by': by}
        if action:
            payload['action'] = action
        return self.dispatch('runtime.applicant.chat.authorize', payload)['data']

    def submit(self):
        return self.dispatch('runtime.applicant.chat.submit', {'point': dict(POINT)})['data']

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

    def greeting_attempts(self):
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.context['scope_id'],
                                         'applicant_greeting_attempts')
        content = doc['content'] if isinstance(doc['content'], dict) else {}
        return [i for i in (content.get('items') or []) if isinstance(i, dict)]

    # ---- 登记 ----------------------------------------------------------
    def test_authorize_defaults_to_reply(self):
        result = self.authorize(action=None)
        self.assertEqual(result['action'], 'reply')
        self.assertEqual(result['status'], 'authorized')

    def test_authorize_rejects_unknown_action(self):
        self.failed('runtime.applicant.chat.authorize',
                    {'text': TEXT, 'confirmed_by': 'user', 'action': 'blast'}, 'INVALID_ACTION_REQUEST')

    def test_authorize_keeps_action_in_document(self):
        self.authorize(action='greeting')
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.context['scope_id'], 'conversation_authorization')
        self.assertEqual(doc['content']['action'], 'greeting')

    # ---- 提交留痕 ------------------------------------------------------
    def test_submit_leaves_unverified_ledger_entry(self):
        self.authorize()
        result = self.submit()
        self.assertEqual(result['ledger_state'], ledger.STATE_SUBMITTED)
        self.assertIs(result['needs_outcome_check'], True)
        items = self.ledger_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['state'], ledger.STATE_SUBMITTED)
        self.assertEqual(items[0]['marks_before'], 34)
        self.assertIsNone(items[0]['resolved_at'])

    def test_submit_exception_records_unknown_then_reraises(self):
        self.authorize()
        self.submit_effect = TimeoutError('browser read timeout')
        # 引擎会把 external_write 的异常包成"人工核对"错误，但台账必须先记成未知
        with self.assertRaises(KernelError):
            self.submit()
        items = self.ledger_items()
        self.assertEqual(items[0]['state'], ledger.STATE_UNKNOWN)

    # ---- 拦截 ----------------------------------------------------------
    def test_second_authorize_same_target_is_refused(self):
        self.authorize()
        self.submit()
        self.failed('runtime.applicant.chat.authorize',
                    {'text': TEXT, 'confirmed_by': 'user', 'action': 'greeting'},
                    'SEND_TARGET_UNRESOLVED')

    def test_sandbox_blocks_other_target_while_unresolved(self):
        """换一个对象也发不了 —— 未知结果未核对前，整个 scope 停下。"""
        self.authorize()
        self.submit()
        self.target = dict(OTHER)
        self.authorize()      # 登记本身允许（换人了）
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_OUTCOME_UNRESOLVED')

    def test_outcome_get_lists_blocker(self):
        self.authorize()
        self.submit()
        result = self.dispatch('runtime.applicant.chat.outcome.get', {})['data']
        self.assertEqual(result['blockers_count'], 1)
        self.assertEqual(result['blocking'][0]['state'], ledger.STATE_SUBMITTED)
        self.assertIs(result['sending_enabled'], False)

    # ---- 只读核对 ------------------------------------------------------
    def test_verify_without_new_mark_stays_unverified(self):
        self.authorize()
        self.submit()
        result = self.dispatch('runtime.applicant.chat.outcome.verify', {})['data']
        self.assertEqual(result['state'], ledger.STATE_SUBMITTED)
        self.assertEqual(result['reason'], 'no_new_mark')
        self.assertIs(result['needs_human'], True)

    def test_verify_with_new_mark_counts_as_contacted(self):
        self.authorize()
        self.submit()
        self.marks = 36
        result = self.dispatch('runtime.applicant.chat.outcome.verify', {})['data']
        self.assertEqual(result['state'], ledger.STATE_OBSERVED)
        self.assertEqual(result['reason'], 'new_mark_observed')
        self.assertIs(result['needs_human'], False)
        # 有回显后同对象不再发第二次
        self.failed('runtime.applicant.chat.authorize',
                    {'text': TEXT, 'confirmed_by': 'user', 'action': 'greeting'},
                    'SEND_TARGET_ALREADY_CONTACTED')

    def test_verify_without_baseline_does_not_upgrade(self):
        self.authorize()
        with mock.patch.object(A, 'real_observe', side_effect=RuntimeError('down')):
            self.submit()
        self.assertEqual(self.ledger_items()[0]['marks_before'], None)
        self.marks = 36
        result = self.dispatch('runtime.applicant.chat.outcome.verify', {})['data']
        self.assertEqual(result['state'], ledger.STATE_SUBMITTED)
        self.assertEqual(result['reason'], 'no_baseline')

    # ---- 人工核对解除 --------------------------------------------------
    def test_resolve_requires_operator_and_known_resolution(self):
        self.authorize()
        self.submit()
        entry_id = self.ledger_items()[0]['ledger_id']
        self.failed('runtime.applicant.chat.outcome.resolve',
                    {'ledger_id': entry_id, 'resolution': 'delivered'}, 'INVALID_ACTION_REQUEST')
        self.failed('runtime.applicant.chat.outcome.resolve',
                    {'ledger_id': entry_id, 'resolution': 'probably', 'confirmed_by': 'user'},
                    'SEND_RESOLUTION_INVALID')

    def test_resolve_not_delivered_unlocks_same_target(self):
        self.authorize()
        self.submit()
        entry_id = self.ledger_items()[0]['ledger_id']
        result = self.dispatch('runtime.applicant.chat.outcome.resolve',
                              {'ledger_id': entry_id, 'resolution': 'not_delivered',
                               'confirmed_by': 'user'})['data']
        self.assertEqual(result['state'], ledger.STATE_NOT_DELIVERED)
        self.assertEqual(result['resolved_by'], 'user')
        # 解锁后同对象可以重新登记
        self.authorize()

    def test_resolve_delivered_keeps_target_contacted(self):
        self.authorize()
        self.submit()
        entry_id = self.ledger_items()[0]['ledger_id']
        self.dispatch('runtime.applicant.chat.outcome.resolve',
                      {'ledger_id': entry_id, 'resolution': 'delivered', 'confirmed_by': 'user'})
        self.failed('runtime.applicant.chat.authorize',
                    {'text': TEXT, 'confirmed_by': 'user', 'action': 'greeting'},
                    'SEND_TARGET_ALREADY_CONTACTED')
        self.assertEqual(self.dispatch('runtime.applicant.chat.outcome.get', {})['data']
                         ['blockers_count'], 0)

    def test_resolve_unknown_entry_is_404(self):
        self.failed('runtime.applicant.chat.outcome.resolve',
                    {'ledger_id': 'nope', 'resolution': 'delivered', 'confirmed_by': 'user'},
                    'SEND_LEDGER_ENTRY_NOT_FOUND')

    # ---- 额度接线 ------------------------------------------------------
    def test_greeting_reserves_quota(self):
        self.authorize(action='greeting')
        self.submit()
        attempts = self.greeting_attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['state'], 'submitted_unverified')

    def test_reply_does_not_touch_greeting_quota(self):
        self.authorize(action='reply')
        self.submit()
        self.assertEqual(self.greeting_attempts(), [])

    def test_greeting_daily_limit_is_enforced_on_real_channel(self):
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            doc = store.runtime_document(c, self.context['scope_id'],
                                         'applicant_greeting_settings')
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                      "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                      "content=excluded.content",
                      (self.context['scope_id'], 'applicant_greeting_settings',
                       doc['revision'] + 1, json.dumps({'configured': 1})))
        self.authorize(action='greeting')
        self.submit()
        entry_id = self.ledger_items()[0]['ledger_id']
        self.dispatch('runtime.applicant.chat.outcome.resolve',
                      {'ledger_id': entry_id, 'resolution': 'delivered', 'confirmed_by': 'user'})
        self.target = dict(OTHER)
        self.authorize(action='greeting')
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)}, 'DAILY_LIMIT_REACHED')

    # ---- 工具注册 ------------------------------------------------------
    # ---- 审计回归（独立只读审计发现的绕过路径，2026-10-27）------------
    def _write_ledger(self, items):
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                      "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                      "content=excluded.content",
                      (self.context['scope_id'], ledger.LEDGER_KIND, 1,
                       json.dumps({'items': items}, ensure_ascii=False)))

    def _ready_channel_authorization(self):
        """造一份受控通道的 ready 授权（这类授权以前能借道放行真实发送）。"""
        doc = {'kind': 'channel_authorization', 'action': 'greeting.submit',
               'status': 'ready', 'used': 0, 'max_uses': 1,
               'expires_at': __import__('time').time() + 900}
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                      "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                      "content=excluded.content",
                      (self.context['scope_id'], 'channel_authorization', 1,
                       json.dumps(doc, ensure_ascii=False)))
        # 前提断言：这份授权必须真的被认成 ready，否则下面的测试会因为别的原因通过（假绿）
        import sandbox
        self.assertIsNotNone(sandbox.ready_authorization(self.context['scope_id']))

    def _rearm_conversation_authorization(self):
        """把会话授权重新置为可用：真实事故里第二次提交就是重新登记的一份新授权。"""
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            doc = store.runtime_document(c, self.context['scope_id'],
                                         'conversation_authorization')
            content = dict(doc['content'])
            content['status'] = 'ready'
            content['used'] = 0
            c.execute("UPDATE runtime_documents SET revision=revision+1, content=? "
                      "WHERE scope_id=? AND kind='conversation_authorization'",
                      (json.dumps(content, ensure_ascii=False), self.context['scope_id']))

    def test_channel_authorization_cannot_bypass_ledger(self):
        """有受控通道授权也不能借道发真实消息：台账检查必须在渠道分支之前。

        复现真实事故：第一次提交留下未核对记录，之后**重新登记**一份可用授权，
        并且工作区里还有一份受控通道的 ready 授权。旧代码在渠道分支直接 return，
        于是第二轮照样点了发送。
        """
        self.authorize()
        self.submit()
        self._rearm_conversation_authorization()
        self._ready_channel_authorization()
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_OUTCOME_UNRESOLVED')

    # ---- 跨工作区不绕过（方案验收矩阵：跨日 / 跨 scope 不绕过）--------
    def _other_scope(self):
        return store.runtime_scope_id('applicant', '别的企业', '另一个账号')

    def test_unresolved_blocker_crosses_scope(self):
        """换一个工作区等于换一份空台账 —— 但磁盘上只有一份库，未核对就不许再发。"""
        self.authorize()
        self.submit()
        import sandbox
        self.assertIsNotNone(sandbox.unresolved_send_blocker(self._other_scope()))

    def test_already_contacted_evidence_crosses_scope(self):
        """已联系证据跨工作区仍生效：换 scope 不能把同一真人变回「没联系过」。"""
        self.authorize()
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.context['scope_id'],
                                         'conversation_authorization')
        content = doc['content']
        entry = ledger.new_entry(scope_id=self._other_scope(),
                                 target_sha256=content['target_sha256'],
                                 body_sha256=content['body_sha256'], action='greeting')
        entry['state'] = ledger.STATE_OBSERVED
        self._write_ledger([entry])
        # 当前 scope 里没有任何台账，但证据在别的 scope 上
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_TARGET_ALREADY_CONTACTED')

    def test_daily_quota_does_not_reset_with_scope(self):
        """换工作区不能刷新 150 上限：额度按人汇总，不按 workspace 计。"""
        import applicant_greeting
        other = self._other_scope()
        day = applicant_greeting.day_key()
        records = [{'job_id': 'j%d' % i, 'company': '某公司', 'state': 'observed_reply',
                    'ts': __import__('time').time(), 'day': day, 'account_id': other}
                   for i in range(applicant_greeting.DAILY_LIMIT)]
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                      "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                      "content=excluded.content",
                      (other, 'applicant_greeting_attempts', 1,
                       json.dumps({'items': records}, ensure_ascii=False)))
        self.authorize()
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'DAILY_LIMIT_REACHED')

    def test_submit_requires_stable_target_evidence(self):
        """方案第131行：没有平台会话 ID / 岗位 ID 时只出草稿，不自动提交。"""
        self.target = dict(LIVE, job_id=None)
        self.authorize()
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_STABLE_TARGET_REQUIRED')
        self.assertEqual(self.ledger_items(), [])

    def test_sandbox_layer_blocks_before_channel_branch(self):
        """直接锁沙箱层：渠道授权 ready 时，真实通道工具也必须先被台账拦下。"""
        self.authorize()
        self.submit()
        self._ready_channel_authorization()
        import sandbox
        with self.assertRaises(sandbox.KernelError) as caught:
            sandbox.require_channel_authorization(self.context, 'runtime.applicant.chat.submit')
        self.assertEqual(caught.exception.code, 'SEND_OUTCOME_UNRESOLVED')

    def test_verify_asks_for_its_own_capability(self):
        """核对必须用自己的工具名登记，否则生产上必抛 KERNEL_REQUIRED（测试 mock 会掩盖）。"""
        self.authorize()
        self.submit()
        seen = []

        def observe(*args, **kwargs):
            seen.append(args[0] if args else kwargs.get('tool_name'))
            return panel(34)

        with mock.patch.object(A, 'real_observe', side_effect=observe):
            self.dispatch('runtime.applicant.chat.outcome.verify', {})
        self.assertEqual(seen, ['runtime.applicant.chat.outcome.verify'])

    def test_verify_refuses_when_panel_is_another_target(self):
        """面板停的不是这条记录的对象时不许核对：否则别人的回显会被记成本条已送达。"""
        self.authorize()
        self.submit()
        self.target = dict(OTHER)
        self.failed('runtime.applicant.chat.outcome.verify', {}, 'SEND_VERIFY_TARGET_MISMATCH')

    def test_resolve_refuses_in_flight_entry(self):
        """正在点击那一次不能提前给结论，否则阻塞被解除、另一条路立刻再发一次。"""
        entry = ledger.new_entry(scope_id=self.context['scope_id'],
                                 target_sha256='a' * 64, body_sha256='b' * 64,
                                 action='greeting')
        self._write_ledger([entry])
        self.failed('runtime.applicant.chat.outcome.resolve',
                    {'ledger_id': entry['ledger_id'], 'resolution': 'not_delivered',
                     'confirmed_by': 'user'}, 'SEND_LEDGER_IN_FLIGHT')

    def test_submit_blocks_already_contacted_target(self):
        """绕过登记层直接写授权，也发不到已联系过的对象（纵深防御）。"""
        self.authorize()
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.context['scope_id'],
                                         'conversation_authorization')
        content = doc['content']
        entry = ledger.new_entry(scope_id=self.context['scope_id'],
                                 target_sha256=content['target_sha256'],
                                 body_sha256=content['body_sha256'], action='greeting')
        entry['state'] = ledger.STATE_OBSERVED
        self._write_ledger([entry])
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_TARGET_ALREADY_CONTACTED')

    def test_submit_without_click_is_not_marked_submitted(self):
        """页面没连上时一次点击都没发生，不能记成「已提交未核对」去假阻塞整个 scope。"""
        self.authorize()
        with mock.patch.object(A, 'real_submit', return_value={'status': 'unavailable'}):
            result = self.submit()
        self.assertEqual(result['ledger_state'], ledger.STATE_NOT_DELIVERED)
        self.assertIs(result['needs_outcome_check'], False)
        self.assertEqual(self.ledger_items()[0]['state'], ledger.STATE_NOT_DELIVERED)

    def test_tools_registered_with_expected_effects(self):
        import main
        self.assertEqual(main._router.resolve('runtime.applicant.chat.outcome.get').effect, 'none')
        self.assertEqual(main._router.resolve('runtime.applicant.chat.outcome.verify').effect, 'none')
        self.assertEqual(main._router.resolve('runtime.applicant.chat.outcome.resolve').effect, 'state')
        self.assertEqual(main._router.resolve('runtime.applicant.chat.submit').effect, 'external_write')


if __name__ == '__main__':
    unittest.main()
