"""Job-detail JD reading: fixed DOM script, tab identity checks, pure enrich logic.

No platform reads: CDP is mocked and every payload is fictional.
"""
import json
import unittest
from unittest import mock

import applicant_browser
import applicant_job_search
import runtime_tools
from sandbox import KernelError, maintenance_access


class DetailSocket:
    """Returns whatever the fixed detail script would return."""

    def __init__(self, value):
        self.value = value
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def settimeout(self, value):
        pass

    def recv(self):
        request = self.sent[-1]
        return json.dumps({'id': request['id'], 'result': {'result': {'value': self.value}}})

    def close(self):
        self.closed = True


def detail_tab(job_id='fictional~id'):
    return {'url': applicant_browser.DETAIL_URL_TEMPLATE.format(job_id=job_id),
            'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/detail'}


class RealJobDetailTest(unittest.TestCase):
    def read(self, tabs, value, expected='fictional~id'):
        socket = DetailSocket(value)
        with mock.patch.object(applicant_browser, '_read_json', return_value=tabs), \
                mock.patch.object(applicant_browser.websocket, 'create_connection',
                                  return_value=socket):
            return applicant_browser.real_job_detail(expected_job_id=expected), socket

    def test_observed_returns_body_and_identity(self):
        result, socket = self.read([detail_tab()], {'status': 'observed', 'job_id': 'fictional~id',
                                                    'title': 'Agent 开发', 'company': '虚构公司',
                                                    'jd': '负责 Agent 编排。', 'truncated': False})
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['jd'], '负责 Agent 编排。')
        self.assertFalse(result['delivery_confirmed'])
        self.assertEqual(socket.sent[0]['method'], 'Runtime.evaluate')
        self.assertNotIn('Page.navigate', str(socket.sent))
        self.assertTrue(socket.closed)

    def test_tab_identity_mismatch_is_refused_before_reading(self):
        socket = DetailSocket({'status': 'observed'})
        with mock.patch.object(applicant_browser, '_read_json',
                               return_value=[detail_tab('other~id')]), \
                mock.patch.object(applicant_browser.websocket, 'create_connection',
                                  return_value=socket):
            with self.assertRaises(KernelError) as caught:
                applicant_browser.real_job_detail(expected_job_id='fictional~id')
        self.assertEqual(caught.exception.code, 'DETAIL_TARGET_MISMATCH')
        self.assertEqual(socket.sent, [])          # 没读正文就拒绝

    def test_script_job_id_must_match_tab_address(self):
        with self.assertRaises(KernelError) as caught:
            self.read([detail_tab()], {'status': 'observed', 'job_id': 'swapped~id',
                                       'title': '', 'company': '', 'jd': '正文', 'truncated': False})
        self.assertEqual(caught.exception.code, 'BROWSER_SCHEMA_CHANGED')

    def test_missing_jd_is_reported_not_treated_as_empty_body(self):
        result, _ = self.read([detail_tab()], {'status': 'jd_missing', 'job_id': 'fictional~id',
                                               'title': 'Agent 开发', 'company': '虚构公司'})
        self.assertEqual(result['status'], 'jd_missing')
        self.assertNotIn('jd', result)

    def test_empty_or_oversized_body_is_schema_changed(self):
        for value in ({'status': 'observed', 'job_id': 'fictional~id', 'jd': '   ', 'truncated': False},
                      {'status': 'observed', 'job_id': 'fictional~id', 'jd': '甲' * 4001,
                       'truncated': True}):
            with self.assertRaises(KernelError):
                self.read([detail_tab()], value)

    def test_multiple_or_absent_detail_tabs(self):
        result, _ = self.read([detail_tab('a~1'), detail_tab('b~2')],
                              {'status': 'observed'}, expected=None)
        self.assertEqual(result['status'], 'page_selection_required')
        result, _ = self.read([], {'status': 'observed'}, expected=None)
        self.assertEqual(result['status'], 'detail_page_required')

    def test_browser_unavailable(self):
        with mock.patch.object(applicant_browser, '_read_json',
                               side_effect=OSError('down')):
            self.assertEqual(applicant_browser.real_job_detail()['status'], 'unavailable')

    def test_restricted_and_auth_states_pass_through_without_body(self):
        for status in ('restricted', 'auth_required'):
            result, _ = self.read([detail_tab()], {'status': status})
            self.assertEqual(result['status'], status)
            self.assertNotIn('jd', result)


class DetailOpenTest(unittest.TestCase):
    def test_rejects_identifiers_that_cannot_build_a_fixed_url(self):
        # current_execution 由 handler 层的内核 capability 提供（工具层测试见 DetailToolPayloadTest），
        # 这里只验证「拼 URL 前的标识校验」，所以把它 patch 掉（与 test_real_chat.py 同口径）。
        for value in ('', 'a/b', '../x', 'a b', 'x?y=1', 123, None):
            with mock.patch.object(applicant_browser, 'current_execution'), maintenance_access():
                with self.assertRaises(KernelError) as caught:
                    applicant_browser.detail_open(value)
            self.assertEqual(caught.exception.code, 'INVALID_JOB_DETAIL')

    def test_navigation_requires_maintenance_and_uses_fixed_url(self):
        socket = DetailSocket({'result': None})
        socket.recv = lambda: json.dumps({'id': socket.sent[-1]['id'], 'result': {'frameId': 'f1'}})
        jobs_tab = {'url': applicant_browser.JOBS_URL,
                    'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/jobs'}
        with self.assertRaises(KernelError) as caught:
            applicant_browser.detail_open('fictional~id')
        self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')
        with mock.patch.object(applicant_browser, 'current_execution'), \
                mock.patch.object(applicant_browser, '_read_json', return_value=[jobs_tab]), \
                mock.patch.object(applicant_browser.websocket, 'create_connection',
                                  return_value=socket), maintenance_access():
            result = applicant_browser.detail_open('fictional~id')
        self.assertEqual(result['status'], 'navigation_requested')
        self.assertEqual(socket.sent[0]['params']['url'],
                         'https://www.zhipin.com/job_detail/fictional~id.html')


def search_state(jobs):
    return {'jobs': {job['job_id']: dict(job) for job in jobs},
            'observed_at': {}, 'updated_at': 0,
            '_order': [job['job_id'] for job in jobs]}


class EnrichJdTest(unittest.TestCase):
    def job(self, job_id='fictional~id', jd=''):
        return {'job_id': job_id, 'title': 'Agent 开发', 'company': '虚构公司', 'city': '广州',
                'salary': '10-15K', 'education': '本科', 'experience': '1年以内', 'jd': jd}

    def test_stores_then_is_idempotent(self):
        state = search_state([self.job()])
        first = applicant_job_search.enrich_jd(state, 'fictional~id', '负责 Agent 编排。', 100)
        self.assertEqual(first['status'], 'stored')
        self.assertEqual(state['jobs']['fictional~id']['jd'], '负责 Agent 编排。')
        again = applicant_job_search.enrich_jd(state, 'fictional~id', '负责 Agent 编排。', 200)
        self.assertEqual(again['status'], 'unchanged')
        self.assertEqual(state['observed_at']['fictional~id'], 100)   # 幂等不刷新观察时间

    def test_changed_jd_replaces_and_refreshes_observation(self):
        state = search_state([self.job(jd='旧职责')])
        result = applicant_job_search.enrich_jd(state, 'fictional~id', '新职责', 300)
        self.assertEqual(result['status'], 'updated')
        self.assertTrue(result['replaced'])
        self.assertEqual(state['observed_at']['fictional~id'], 300)

    def test_rejects_unknown_job_empty_and_oversized(self):
        state = search_state([self.job()])
        with self.assertRaises(KernelError) as caught:
            applicant_job_search.enrich_jd(state, 'other~id', '正文', 1)
        self.assertEqual(caught.exception.code, 'OBSERVED_JOB_NOT_FOUND')
        for bad in ('', '   ', None, '甲' * 4001):
            with self.assertRaises(KernelError) as caught:
                applicant_job_search.enrich_jd(state, 'fictional~id', bad, 1)
            self.assertEqual(caught.exception.code, 'INVALID_JOB_SEARCH')

    def test_pending_details_lists_only_missing_and_respects_limit(self):
        state = search_state([self.job('a~1'), self.job('b~2', jd='已有'), self.job('c~3')])
        self.assertEqual(applicant_job_search.pending_details(state), ['a~1', 'c~3'])
        self.assertEqual(applicant_job_search.pending_details(state, limit=1), ['a~1'])
        self.assertNotIn('jd', applicant_job_search.pending_details(state)[0])


class DetailToolPayloadTest(unittest.TestCase):
    def setUp(self):
        import test_applicant_observation as fixture
        self.f = fixture.ApplicantObservationTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h

    def test_payload_must_be_exactly_job_id(self):
        for payload in ({}, {'job_id': 'a', 'url': 'https://evil'}, {'job': 'a'}, None):
            with maintenance_access():
                with self.assertRaises(KernelError) as caught:
                    self.h.call('runtime.applicant.jobs.detail', payload)
            self.assertEqual(caught.exception.code, 'INVALID_JOB_DETAIL')

    def test_maintenance_only_so_plain_calls_are_refused_first(self):
        # 方案第 145 行：维护类 local_ui 不应被普通 HTTP 调用变成动作权限。
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.jobs.detail', {'job_id': 'fictional~id'})
        self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')

    def test_illegal_job_id_rejected_before_touching_browser(self):
        with mock.patch.object(applicant_browser, 'detail_open') as opened, maintenance_access():
            with self.assertRaises(KernelError) as caught:
                self.h.call('runtime.applicant.jobs.detail', {'job_id': '../../etc/passwd'})
        self.assertEqual(caught.exception.code, 'INVALID_JOB_DETAIL')
        opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
