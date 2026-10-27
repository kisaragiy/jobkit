"""真实对话页只读通道的契约测试：选择器、鼠标序列、schema 严格性。"""
import unittest
from unittest import mock

import applicant_browser as A
from dom import boss_selectors
from sandbox import maintenance_access


class FakeConnection:
    def __init__(self, replies, fail_after=None):
        self.replies = list(replies)
        self.sent = []
        self.fail_after = fail_after
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)
        if self.fail_after is not None and len(self.sent) > self.fail_after:
            raise A.websocket.WebSocketException('connection reset')

    def recv(self):
        return self.replies.pop(0)

    def settimeout(self, _value):
        return None

    def close(self):
        self.closed = True


def reply(value, rid=1):
    import json
    return json.dumps({'id': rid, 'result': {'result': {'value': value}}})


CHAT_TAB = {'url': 'https://www.zhipin.com/web/geek/chat',
            'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/AAA'}
LIST_TAB = {'url': 'https://www.zhipin.com/web/geek/jobs',
            'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/BBB'}


class TabsTest(unittest.TestCase):
    def test_only_chat_page_is_accepted(self):
        with mock.patch.object(A, '_read_json', return_value=[CHAT_TAB, LIST_TAB]):
            self.assertEqual([t['url'] for t in A._chat_page_tabs()],
                             ['https://www.zhipin.com/web/geek/chat'])

    def test_browser_down_returns_none(self):
        with mock.patch.object(A, '_read_json', side_effect=OSError):
            self.assertIsNone(A._chat_page_tabs())

    def test_schema_drift_is_refused(self):
        with mock.patch.object(A, '_read_json', return_value=[{'url': 123}]):
            with self.assertRaises(A.KernelError) as caught:
                A._chat_page_tabs()
        self.assertEqual(caught.exception.code, 'BROWSER_CONNECTION_INVALID')


class OpenConversationTest(unittest.TestCase):
    def test_requires_maintenance(self):
        with self.assertRaises(A.KernelError):
            A.real_open_conversation()

    def test_no_chat_tab_is_reported_not_guessed(self):
        with mock.patch.object(A, '_read_json', return_value=[LIST_TAB]), \
                mock.patch.object(A, 'current_execution'), maintenance_access():
            self.assertEqual(A.real_open_conversation()['status'], 'chat_page_required')

    def test_two_chat_tabs_require_selection(self):
        second = dict(CHAT_TAB, webSocketDebuggerUrl='ws://127.0.0.1:9222/devtools/page/CCC')
        with mock.patch.object(A, '_read_json', return_value=[CHAT_TAB, second]), \
                mock.patch.object(A, 'current_execution'), maintenance_access():
            self.assertEqual(A.real_open_conversation()['status'], 'page_selection_required')

    def test_empty_session_list_is_explicit(self):
        conn = FakeConnection([reply({'ok': False, 'code': 'SESSION_LIST_EMPTY'})])
        with mock.patch.object(A, '_read_json', return_value=[CHAT_TAB]), \
                mock.patch.object(A, 'current_execution'), \
                mock.patch.object(A.websocket, 'create_connection', return_value=conn), \
                maintenance_access():
            result = A.real_open_conversation()
        self.assertEqual(result['status'], 'no_conversation')
        self.assertEqual(result['code'], 'SESSION_LIST_EMPTY')

    def test_full_mouse_sequence_is_sent(self):
        ready = {'ok': True, 'code': 'SESSION_READY', 'x': 649, 'y': 285, 'count': 44,
                 'name': '示例招聘方', 'company': '示例科技有限公司', 'preview': '面试结果…'}
        # _fixed_reply 按 sequence 匹配响应 id：evaluate=1，鼠标事件=2/3/4
        conn = FakeConnection([reply(ready, 1), reply({'ok': True}, 2),
                               reply({'ok': True}, 3), reply({'ok': True}, 4)])
        with mock.patch.object(A, '_read_json', return_value=[CHAT_TAB]), \
                mock.patch.object(A, 'current_execution'), \
                mock.patch.object(A.websocket, 'create_connection', return_value=conn), \
                maintenance_access():
            result = A.real_open_conversation()
        self.assertEqual(result['status'], 'open_requested')
        self.assertEqual(result['name'], '示例招聘方')
        self.assertEqual(result['sessions'], 44)
        import json as _json
        events = [_json.loads(payload) for payload in conn.sent if 'Input.' in payload]
        self.assertEqual([event['params']['type'] for event in events],
                         ['mouseMoved', 'mousePressed', 'mouseReleased'])
        self.assertEqual(events[0]['params']['x'], 649)
        self.assertNotIn('button', events[0]['params'])
        self.assertEqual(events[1]['params']['buttons'], 1)
        self.assertEqual(events[2]['params']['buttons'], 0)

    def test_connection_reset_after_click_is_not_failure(self):
        ready = {'ok': True, 'code': 'SESSION_READY', 'x': 10, 'y': 20, 'count': 3,
                 'name': 'A', 'company': 'B', 'preview': 'c'}
        conn = FakeConnection([reply(ready, 1)], fail_after=1)   # evaluate 成功，点击时断开
        with mock.patch.object(A, '_read_json', return_value=[CHAT_TAB]), \
                mock.patch.object(A, 'current_execution'), \
                mock.patch.object(A.websocket, 'create_connection', return_value=conn), \
                maintenance_access():
            result = A.real_open_conversation()
        self.assertEqual(result['status'], 'open_requested')
        self.assertFalse(result['delivery_confirmed'])


class ObserveTest(unittest.TestCase):
    GOOD = {'composer': 1, 'can_send': False, 'send_disabled': True, 'empty': False,
            'delivered': 3, 'read': 1, 'panel_text': '示例招聘方 示例科技有限公司'}

    def run_observe(self, value):
        conn = FakeConnection([reply(value)])
        with mock.patch.object(A, '_read_json', return_value=[CHAT_TAB]), \
                mock.patch.object(A, 'current_execution'), \
                mock.patch.object(A.websocket, 'create_connection', return_value=conn), \
                maintenance_access():
            return A.real_observe()

    def test_normal_observation(self):
        result = self.run_observe(self.GOOD)
        self.assertEqual(result['status'], 'observed')
        self.assertTrue(result['composer_ready'])
        self.assertFalse(result['can_send'])
        self.assertEqual(result['delivered_marks'], 3)
        self.assertEqual(result['read_marks'], 1)
        self.assertFalse(result['delivery_confirmed'])

    def test_extra_field_is_refused(self):
        with self.assertRaises(A.KernelError) as caught:
            self.run_observe(dict(self.GOOD, extra=1))
        self.assertEqual(caught.exception.code, 'BROWSER_SCHEMA_CHANGED')

    def test_missing_field_is_refused(self):
        bad = dict(self.GOOD)
        bad.pop('read')
        with self.assertRaises(A.KernelError):
            self.run_observe(bad)

    def test_negative_count_is_refused(self):
        with self.assertRaises(A.KernelError):
            self.run_observe(dict(self.GOOD, delivered=-1))

    def test_oversized_text_is_refused(self):
        with self.assertRaises(A.KernelError):
            self.run_observe(dict(self.GOOD, panel_text='x' * 500))


class ObserveToolContractTest(unittest.TestCase):
    """只读观察接进内核后的契约。"""

    def setUp(self):
        import test_applicant_send as harness
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_tool_is_registered_read_only(self):
        import main
        tool = main._router.resolve('runtime.applicant.chat.observe')
        self.assertEqual(tool.effect, 'none')
        self.assertFalse(tool.maintenance_only)

    def test_payload_is_refused(self):
        from sandbox import maintenance_access
        with self.assertRaises(A.KernelError) as caught:
            with maintenance_access():
                self.f.dispatch('runtime.applicant.chat.observe', {'x': 1})
        self.assertEqual(caught.exception.code, 'INVALID_BROWSER_REQUEST')

    def test_observation_never_claims_delivery(self):
        from sandbox import maintenance_access
        fake = {'status': 'observed', 'composer_ready': True, 'can_send': False,
                'send_disabled': True, 'empty': False, 'delivered_marks': 3,
                'read_marks': 1, 'panel_text': '示例招聘方', 'account_verified': False,
                'sending_enabled': False, 'delivery_confirmed': False}
        with mock.patch.object(A, 'real_observe', return_value=dict(fake)), \
                maintenance_access():
            data = self.f.dispatch('runtime.applicant.chat.observe', {})['data']
        self.assertFalse(data['sending_enabled'])
        self.assertFalse(data['delivery_confirmed'])
        self.assertEqual(data['delivered_marks'], 3)
        self.assertEqual(data['source'], 'real_chat_page')

    def test_missing_chat_page_is_stated(self):
        from sandbox import maintenance_access
        with mock.patch.object(A, '_read_json', return_value=[LIST_TAB]), \
                maintenance_access():
            data = self.f.dispatch('runtime.applicant.chat.observe', {})['data']
        self.assertEqual(data['status'], 'chat_page_required')
        self.assertFalse(data['sending_enabled'])


class TargetIdentityTest(unittest.TestCase):
    """当前对话身份必须从面板结构化读，不能拿面板文本硬切。"""

    GOOD = {'ok': True, 'name': '示例招聘方', 'company': '示例科技有限公司', 'role': '招聘者',
            'position': 'AI应用开发助理', 'salary': '5-8K', 'city': '深圳'}

    def read(self, value):
        conn = FakeConnection([reply(value)])
        with mock.patch.object(A, '_read_json', return_value=[CHAT_TAB]), \
                mock.patch.object(A, 'current_execution'), \
                mock.patch.object(A.websocket, 'create_connection', return_value=conn), \
                maintenance_access():
            return A.real_target()

    def test_identity_is_read_from_panel(self):
        result = self.read(self.GOOD)
        self.assertEqual(result['status'], 'target_read')
        self.assertEqual(result['name'], '示例招聘方')
        self.assertEqual(result['company'], '示例科技有限公司')
        self.assertEqual(result['position'], 'AI应用开发助理')
        self.assertFalse(result['sending_enabled'])
        self.assertFalse(result['delivery_confirmed'])

    def test_missing_panel_is_stated(self):
        result = self.read({'ok': False, 'code': 'TARGET_PANEL_MISSING'})
        self.assertEqual(result['status'], 'target_unreadable')
        self.assertEqual(result['code'], 'TARGET_PANEL_MISSING')

    def test_missing_name_or_company_is_not_guessed(self):
        bad = dict(self.GOOD, company=None)
        result = self.read(bad)
        self.assertEqual(result['status'], 'target_unreadable')
        self.assertEqual(result['code'], 'NAME_OR_COMPANY_MISSING')

    def test_oversized_field_is_refused(self):
        with self.assertRaises(A.KernelError):
            self.read(dict(self.GOOD, name='王' * 61))

    def test_wrong_type_is_refused(self):
        with self.assertRaises(A.KernelError):
            self.read(dict(self.GOOD, position=123))

    def test_tool_registered_read_only(self):
        import main
        tool = main._router.resolve('runtime.applicant.chat.target')
        self.assertEqual(tool.effect, 'none')
        self.assertFalse(tool.maintenance_only)

    def test_payload_refused(self):
        import test_applicant_send as harness
        from sandbox import maintenance_access
        fixture = harness.SendTargetTest()
        fixture.setUp()
        try:
            with self.assertRaises(A.KernelError) as caught:
                with maintenance_access():
                    fixture.dispatch('runtime.applicant.chat.target', {'x': 1})
            self.assertEqual(caught.exception.code, 'INVALID_BROWSER_REQUEST')
        finally:
            fixture.doCleanups()


class AuthorizeToolTest(unittest.TestCase):
    """chat.authorize：登记授权但不发送。"""

    LIVE = {'status': 'target_read', 'name': '示例招聘方', 'company': '示例科技有限公司',
            'position': 'AI应用开发助理', 'sending_enabled': False, 'delivery_confirmed': False}

    def setUp(self):
        import test_applicant_send as harness
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def call(self, payload, live=None):
        from sandbox import maintenance_access
        with mock.patch.object(A, 'real_target', return_value=dict(live or self.LIVE)), \
                maintenance_access():
            return self.f.dispatch('runtime.applicant.chat.authorize', payload)

    def test_registered_as_state_control(self):
        import main
        tool = main._router.resolve('runtime.applicant.chat.authorize')
        self.assertEqual(tool.effect, 'state')
        self.assertFalse(tool.maintenance_only)

    def test_authorizes_without_sending(self):
        data = self.call({'text': '测试授权，不发送。', 'confirmed_by': 'user'})['data']
        self.assertEqual(data['status'], 'authorized')
        self.assertEqual(data['target']['name'], '示例招聘方')
        self.assertEqual(data['max_uses'], 1)
        self.assertFalse(data['sending_enabled'])
        self.assertFalse(data['delivery_confirmed'])
        self.assertNotIn('测试授权，不发送。', str(data))   # 正文不落明文

    def test_persists_one_shot_document(self):
        import store
        self.call({'text': '测试授权，不发送。', 'confirmed_by': 'user'})
        with store.runtime_db() as c:
            doc = store.runtime_document(c, self.f.h.context['scope_id'],
                                        'conversation_authorization')
        self.assertEqual(doc['content']['kind'], 'conversation_authorization')
        self.assertEqual(doc['content']['max_uses'], 1)
        self.assertEqual(doc['content']['used'], 0)

    def test_missing_field_is_refused(self):
        with self.assertRaises(A.KernelError) as caught:
            self.call({'text': '只有正文'})
        self.assertEqual(caught.exception.code, 'INVALID_ACTION_REQUEST')

    def test_unreadable_target_blocks_authorization(self):
        bad = {'status': 'target_unreadable', 'code': 'NAME_OR_COMPANY_MISSING'}
        with self.assertRaises(A.KernelError) as caught:
            self.call({'text': '测试', 'confirmed_by': 'user'}, live=bad)
        self.assertEqual(caught.exception.code, 'CONVERSATION_TARGET_UNREADABLE')

    def test_empty_confirmer_is_refused(self):
        with self.assertRaises(A.KernelError):
            self.call({'text': '测试', 'confirmed_by': '  '})


class StatusScopeTest(unittest.TestCase):
    """delivered_marks 必须只统计消息区，不能把左侧会话列表的标记也算进去。"""

    def test_status_count_is_scoped_to_message_area(self):
        script = A.REAL_OBSERVE_SCRIPT
        self.assertIn(".message-content", script)
        # 全局裸查询就是那个 bug：左侧列表每条预览也带 .message-status
        self.assertNotIn("document.querySelectorAll('.message-status')", script)

    def test_scope_missing_yields_zero_not_global(self):
        self.assertIn(": [];", A.REAL_OBSERVE_SCRIPT)


class SelectorRegressionTest(unittest.TestCase):
    def test_job_list_includes_measured_container(self):
        self.assertIn('ul.rec-job-list', boss_selectors.SELECTORS['JOB_LIST'])

    def test_real_chat_constants_match_measurement(self):
        self.assertEqual(A.REAL_INPUT_SELECTOR, 'div.chat-input')
        self.assertEqual(A.REAL_SEND_SELECTOR, 'button.btn-send')
        self.assertEqual(A.REAL_STATUS_DELIVERY, 'i.message-status.status-delivery')
        self.assertEqual(A.REAL_STATUS_READ, 'i.message-status.status-read')


if __name__ == '__main__':
    unittest.main()
