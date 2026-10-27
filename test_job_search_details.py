"""Bounded batch JD fill inside a search session: limits, partial failure, staleness.

No platform reads: CDP/detail navigation is mocked, every payload is fictional,
the runtime DB is a temporary file.
"""
import json
import unittest
from unittest import mock

import applicant_browser
import applicant_job_search
import main
import runtime_tools
import store
import test_applicant_send as harness
from sandbox import KernelError, maintenance_access

TOOL = 'runtime.applicant.jobs.search.details'
KEYWORD = 'Agent'
CITY = '广州'
NAV = {'status': 'navigation_requested', 'keyword': KEYWORD, 'city': CITY,
       'city_code': '101280100', 'account_verified': False, 'sending_enabled': False}


def card(job_id):
    return {'job_id': job_id, 'title': 'AI 应用开发', 'company': '虚构公司', 'city': CITY,
            'salary': '8-12K', 'education': '本科', 'experience': '1-3年'}


def observed(*job_ids):
    return {'status': 'observed', 'jobs': [card(job_id) for job_id in job_ids]}


def detail(job_id, jd=None):
    return {'status': 'observed', 'job_id': job_id,
            'jd': jd if jd is not None else '岗位职责（虚构）：%s 的正文。' % job_id,
            'truncated': False, 'title': 'AI 应用开发', 'company': '虚构公司'}


class FakeClock:
    """time.monotonic 每被读一次就往前走，模拟「每条详情都要花时间」。"""

    def __init__(self, tick=0.0):
        self.now, self.tick = 1000.0, tick

    def monotonic(self):
        value = self.now
        self.now += self.tick
        return value

    def sleep(self, seconds):
        self.now += seconds

    def time(self):
        return 1761560400.0 + self.now


class DetailsBatchTest(unittest.TestCase):
    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.dispatch = self.f.dispatch

    # ---- helpers -------------------------------------------------------
    def collect(self, *job_ids):
        """走真实 open/step 建会话；列表卡本身不带 JD（jd 为空缺口）。"""
        with mock.patch.object(applicant_browser, 'search_open', return_value=NAV), \
                mock.patch.object(applicant_browser, 'read_current_jobs',
                                  return_value=observed(*job_ids)), \
                maintenance_access():
            self.dispatch('runtime.applicant.jobs.search.open', {'keyword': KEYWORD, 'city': CITY})
            return self.dispatch('runtime.applicant.jobs.search.step', {})['data']

    def details(self, payload=None, opened=None, reads=None, clock=None):
        opened = opened or (lambda job_id, **kwargs: {'status': 'navigation_requested'})
        reads = reads or {}
        patches = [mock.patch.object(applicant_browser, 'detail_open', side_effect=opened),
                   mock.patch.object(applicant_browser, 'real_job_detail',
                                     side_effect=lambda expected_job_id=None: reads[expected_job_id]()),
                   mock.patch('time.sleep', return_value=None)]
        if clock is not None:
            patches.append(mock.patch.object(runtime_tools, 'time', clock))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with maintenance_access():
            return self.dispatch(TOOL, payload)['data']

    def session(self):
        with store.runtime_db() as c:
            document = store.runtime_document(c, self.f.h.context['scope_id'],
                                              runtime_tools.JOB_SEARCH_KIND)
        content = document['content']
        return content if isinstance(content, dict) else json.loads(content)

    def refused(self, payload, code='INVALID_JOB_SEARCH'):
        with mock.patch.object(applicant_browser, 'detail_open') as opened, maintenance_access():
            with self.assertRaises(KernelError) as caught:
                self.dispatch(TOOL, payload)
        self.assertEqual(caught.exception.code, code)
        opened.assert_not_called()
        return caught.exception

    # ---- boundaries ----------------------------------------------------
    def test_limit_above_five_or_out_of_range_is_refused_before_touching_the_browser(self):
        self.collect('gz1', 'gz2', 'gz3', 'gz4', 'gz5')
        for payload in ({'limit': 6}, {'limit': 0}, {'limit': -1}, {'limit': True},
                        {'limit': '3'}, {'limit': 2.0}, {'limit': 51}):
            with self.subTest(payload=payload):
                self.refused(payload)

    def test_payload_only_accepts_limit(self):
        self.collect('gz1')
        for payload in ({'limit': 1, 'job_id': 'gz1'}, {'limit': 1, 'url': 'https://evil'},
                        {'script': 'alert(1)'}, {'jobs': ['gz1']}):
            with self.subTest(payload=payload):
                self.refused(payload)
        self.refused({'limit': 1, 'include_candidates': True})

    def test_default_limit_is_three(self):
        self.collect('gz1', 'gz2', 'gz3', 'gz4')
        seen = []

        def opened(job_id, **kwargs):
            seen.append(job_id)
            return {'status': 'navigation_requested'}

        result = self.details({}, opened=opened,
                              reads={job_id: (lambda job_id=job_id: detail(job_id))
                                     for job_id in ('gz1', 'gz2', 'gz3', 'gz4')})
        self.assertEqual(result['limit'], runtime_tools.DETAILS_DEFAULT)
        self.assertEqual(seen, ['gz1', 'gz2', 'gz3'])
        self.assertEqual((result['filled'], result['failed'], result['remaining']), (3, 0, 1))
        self.assertEqual(result['pending_total'], 4)
        self.assertEqual(self.session()['jobs']['gz4']['jd'], '')

    def test_maintenance_only_and_session_required(self):
        with self.assertRaises(KernelError) as caught:
            self.dispatch(TOOL, {'limit': 1})
        self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')
        self.assertTrue(main._router._tools[TOOL].maintenance_only)
        self.assertEqual(main._router._tools[TOOL].effect, 'local_ui')
        self.refused({'limit': 1}, code='JOB_SEARCH_REQUIRED')

    def test_no_http_route(self):
        import app
        paths = {getattr(route, 'path', '') for route in app.app.routes}
        self.assertNotIn('/api/runtime/applicant/jobs/search/details', paths)

    # ---- batch behaviour ----------------------------------------------
    def test_fills_pending_jd_in_discovery_order(self):
        self.collect('gz1', 'gz2')
        result = self.details({'limit': 2}, reads={'gz1': lambda: detail('gz1'),
                                                   'gz2': lambda: detail('gz2', 'gz2 的正文。')})
        self.assertEqual(result['status'], 'batch_done')
        self.assertIsNone(result['stopped'])
        self.assertEqual([item['status'] for item in result['results']], ['stored', 'stored'])
        self.assertEqual(result['filled'], 2)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['remaining'], 0)
        self.assertFalse(result['sending_enabled'])
        self.assertFalse(result['automatic_execution'])
        self.assertTrue(result['read_only'])
        state = self.session()
        self.assertEqual(state['jobs']['gz1']['jd'], '岗位职责（虚构）：gz1 的正文。')
        self.assertEqual(state['jobs']['gz2']['jd'], 'gz2 的正文。')

    def test_one_failure_does_not_stop_the_rest(self):
        self.collect('gz1', 'gz2', 'gz3')

        def opened(job_id, **kwargs):
            if job_id == 'gz1':
                return {'status': 'page_selection_required'}
            return {'status': 'navigation_requested'}

        def read():
            raise KernelError('DETAIL_TARGET_MISMATCH', '当前详情页不是该岗位，未读取职责描述。')

        result = self.details({'limit': 3}, opened=opened,
                              reads={'gz1': lambda: detail('gz1'), 'gz2': read,
                                     'gz3': lambda: detail('gz3')})
        self.assertEqual([item['job_id'] for item in result['results']], ['gz1', 'gz2', 'gz3'])
        self.assertEqual(result['results'][0]['status'], 'page_selection_required')
        self.assertEqual(result['results'][0]['reason'], 'navigation_refused')
        self.assertEqual(result['results'][1]['status'], 'error')
        self.assertEqual(result['results'][1]['reason'], 'DETAIL_TARGET_MISMATCH')
        self.assertEqual(result['results'][2]['status'], 'stored')
        self.assertEqual((result['filled'], result['failed']), (1, 2))
        self.assertEqual(result['filled'] + result['failed'], len(result['results']))
        self.assertEqual(result['remaining'], 2)
        state = self.session()
        self.assertEqual(state['jobs']['gz3']['jd'], '岗位职责（虚构）：gz3 的正文。')
        self.assertEqual(state['jobs']['gz1']['jd'], '')      # 失败不写
        self.assertEqual(state['jobs']['gz2']['jd'], '')

    def test_jd_missing_is_recorded_and_not_written_as_empty_body(self):
        self.collect('gz1')
        result = self.details({'limit': 1},
                              reads={'gz1': lambda: {'status': 'jd_missing', 'job_id': 'gz1',
                                                     'title': 'AI 应用开发', 'company': '虚构公司'}})
        self.assertEqual(result['results'][0]['status'], 'jd_missing')
        self.assertEqual(result['results'][0]['reason'], 'jd_missing')
        self.assertEqual(result['filled'], 0)
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(self.session()['jobs']['gz1']['jd'], '')

    def test_unchanged_body_is_idempotent_and_keeps_the_first_observation_time(self):
        self.collect('gz1')
        before = self.session()['observed_at']['gz1']

        def read(expected_job_id=None):
            # 另一个读取器已经把同一段正文写回去了（revision 不变，正文相同）
            with store.runtime_db() as c:
                c.execute('BEGIN IMMEDIATE')
                document = store.runtime_document(c, self.f.h.context['scope_id'],
                                                  runtime_tools.JOB_SEARCH_KIND)
                content = document['content']
                content = content if isinstance(content, dict) else json.loads(content)
                content['jobs']['gz1']['jd'] = '同一段正文。'
                c.execute('UPDATE runtime_documents SET content=? WHERE scope_id=? AND kind=?',
                          (json.dumps(content, ensure_ascii=False), self.f.h.context['scope_id'],
                           runtime_tools.JOB_SEARCH_KIND))
            return detail('gz1', '同一段正文。')

        result = self.details({'limit': 1}, reads={'gz1': read})
        self.assertEqual(result['results'][0]['status'], 'unchanged')
        self.assertIsNone(result['results'][0]['reason'])
        self.assertEqual(result['filled'], 1)
        self.assertEqual(result['failed'], 0)
        state = self.session()
        self.assertEqual(state['jobs']['gz1']['jd'], '同一段正文。')
        self.assertEqual(state['observed_at']['gz1'], before)   # 幂等不刷新观察时间

    def test_empty_gap_returns_zero_filled_and_no_browser_call(self):
        self.collect('gz1')
        first = self.details({'limit': 1}, reads={'gz1': lambda: detail('gz1')})
        self.assertEqual(first['filled'], 1)

        def opened(job_id, **kwargs):
            raise AssertionError('已经没有缺口，不该再打开详情页')

        result = self.details({'limit': 3}, opened=opened)
        self.assertEqual(result['status'], 'batch_done')
        self.assertEqual(result['results'], [])
        self.assertEqual((result['filled'], result['failed'],
                          result['remaining'], result['pending_total']), (0, 0, 0, 0))

    def test_revision_change_stops_the_batch_and_does_not_write_the_read_body(self):
        self.collect('gz1', 'gz2', 'gz3')
        seen = []

        def reader(target):
            def read():
                seen.append(target)
                if target == 'gz2':
                    # 读详情期间会话被改（例如人工停止）：直接 bump revision 并改状态。
                    # 注意 mock 是 `reads[expected_job_id]()` —— 不传参，所以这里必须用闭包拿 job_id，
                    # 首版写成 read(expected_job_id) 时参数恒为 None，这个测试其实什么都没改。
                    with store.runtime_db() as c:
                        c.execute('BEGIN IMMEDIATE')
                        document = store.runtime_document(c, self.f.h.context['scope_id'],
                                                          runtime_tools.JOB_SEARCH_KIND)
                        content = document['content']
                        content = content if isinstance(content, dict) else json.loads(content)
                        content['status'] = 'stopped'
                        c.execute('UPDATE runtime_documents SET revision=revision+1, content=? '
                                  'WHERE scope_id=? AND kind=?',
                                  (json.dumps(content, ensure_ascii=False),
                                   self.f.h.context['scope_id'], runtime_tools.JOB_SEARCH_KIND))
                return detail(target)
            return read

        result = self.details({'limit': 3},
                              reads={job_id: reader(job_id) for job_id in ('gz1', 'gz2', 'gz3')})
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['stopped'], 'STALE_JOB_SEARCH')
        self.assertEqual(seen, ['gz1', 'gz2'])             # 第三条不再读
        self.assertEqual([item['job_id'] for item in result['results']], ['gz1'])
        self.assertEqual(result['filled'], 1)
        self.assertEqual(result['remaining'], 2)
        state = self.session()
        self.assertEqual(state['jobs']['gz1']['jd'], '岗位职责（虚构）：gz1 的正文。')
        self.assertEqual(state['jobs']['gz2']['jd'], '')   # 已读到的正文没有写进去
        self.assertEqual(state['jobs']['gz3']['jd'], '')
        # 不在这里断言 state['status']：停止是外部动作的结果，批量 handler 只负责
        # 「发现漂移就不写」。断言它等于 mock 写进去的值，测的是存库顺序而不是设计语义
        # （首版就是这么写错的）。真正的判据是上面的 stopped 原因与「gz2 正文没落库」。

    def test_single_jobs_detail_success_path_writes_jd(self):
        """jobs.detail 的成功路径必须真的落库。

        p9 首版把它注册成 local_ui 却调 record_commit，而 commit_owner 要求任务
        effect=='state' → 成功路径必抛 INVALID_COMMIT_OWNER；当时 17 项测试全在测拒绝路径，
        所以门禁全绿也没发现。这条就是那个 bug 的护栏。
        """
        self.collect('gz1')
        with mock.patch.object(applicant_browser, 'detail_open',
                               return_value={'status': 'navigation_requested'}), \
                mock.patch.object(applicant_browser, 'real_job_detail',
                                  return_value=detail('gz1')), \
                mock.patch('time.sleep', return_value=None), maintenance_access():
            result = self.dispatch('runtime.applicant.jobs.detail', {'job_id': 'gz1'})['data']
        self.assertEqual(result['status'], 'stored')
        self.assertEqual(result['job_id'], 'gz1')
        self.assertEqual(self.session()['jobs']['gz1']['jd'], '岗位职责（虚构）：gz1 的正文。')

    def test_total_time_cap_stops_and_reports_what_is_left(self):
        self.collect('gz1', 'gz2', 'gz3', 'gz4', 'gz5')
        clock = FakeClock(tick=25.0)     # 每次读表都消耗 25 秒，40 秒预算只够两条
        seen = []

        def opened(job_id, **kwargs):
            seen.append(job_id)
            return {'status': 'navigation_requested'}

        result = self.details({'limit': 5}, opened=opened,
                              reads={job_id: (lambda job_id=job_id: detail(job_id))
                                     for job_id in ('gz1', 'gz2', 'gz3', 'gz4', 'gz5')},
                              clock=clock)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['stopped'], 'LIMIT_SECONDS')
        self.assertLessEqual(len(seen), 3)
        self.assertEqual(result['filled'], len(seen))
        self.assertEqual(result['remaining'], 5 - len(seen))
        self.assertEqual(result['pending_total'], 5)
        self.assertGreaterEqual(result['elapsed_seconds'], 0)

    def test_rerun_picks_up_the_remaining_gap(self):
        self.collect('gz1', 'gz2', 'gz3')
        first = self.details({'limit': 2}, reads={'gz1': lambda: detail('gz1'),
                                                  'gz2': lambda: detail('gz2')})
        self.assertEqual((first['filled'], first['remaining']), (2, 1))
        second = self.details({'limit': 2}, reads={'gz3': lambda: detail('gz3')})
        self.assertEqual(second['filled'], 1)
        self.assertEqual(second['remaining'], 0)
        self.assertEqual(second['pending_total'], 1)

    def test_search_step_reports_pending_details_without_reading_them(self):
        result = self.collect('gz1', 'gz2')
        self.assertEqual(result['pending_details'], 2)
        self.assertEqual(self.session()['jobs']['gz1']['jd'], '')    # step 不自动读详情


if __name__ == '__main__':
    unittest.main()
