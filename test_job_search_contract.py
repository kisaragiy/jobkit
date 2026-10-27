"""HTTP/CLI contract for the bounded job search: no browser, no sending."""

import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import app
import applicant_browser
import applicant_job_search as S
import main
import test_applicant_send as harness
from sandbox import KernelError, maintenance_access

KEYWORD = 'Agent'
CITY = '广州'

OBSERVED = {'status': 'observed', 'jobs': [
    {'job_id': 'gz1', 'title': 'AI 应用开发', 'company': '甲公司', 'city': '广州',
     'salary': '面议', 'education': '本科', 'experience': '1-3年'},
    {'job_id': 'gz2', 'title': 'Python 后端', 'company': '乙公司', 'city': '广州',
     'salary': '面议', 'education': '', 'experience': ''},
]}
NAV = {'status': 'navigation_requested', 'keyword': KEYWORD, 'city': CITY,
       'city_code': '101280100', 'account_verified': False, 'sending_enabled': False}


class JobSearchContractTest(unittest.TestCase):
    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.dispatch = self.f.dispatch
        self.token = self.f.h.context['context_token']

    # ---- helpers -------------------------------------------------------
    def open(self, keyword=KEYWORD, city=CITY, nav=NAV):
        with mock.patch.object(applicant_browser, 'search_open', return_value=nav):
            with maintenance_access():
                return self.dispatch('runtime.applicant.jobs.search.open',
                                     {'keyword': keyword, 'city': city})['data']

    def step(self, pages, observation=OBSERVED, scroll=None):
        scroll = scroll or {'status': 'scrolled', 'before': {'height': 100, 'cards': 2, 'y': 0}}
        with mock.patch.object(applicant_browser, 'read_current_jobs', return_value=observation), \
                mock.patch.object(applicant_browser, 'scroll_jobs', return_value=scroll), \
                mock.patch('time.sleep', return_value=None):
            with maintenance_access():
                return self.dispatch('runtime.applicant.jobs.search.step', {})['data']

    def get(self, **payload):
        return self.dispatch('runtime.applicant.jobs.search.get', payload)['data']

    def failed(self, tool, payload, code, maintenance=True):
        with self.assertRaises(KernelError) as caught:
            if maintenance:
                with maintenance_access():
                    self.dispatch(tool, payload)
            else:
                self.dispatch(tool, payload)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    # ---- reading before anything starts --------------------------------
    def test_get_before_start_reports_not_started(self):
        result = self.get()
        self.assertEqual(result['status'], 'not_started')
        self.assertIsNone(result['state'])
        self.assertEqual(result['candidates'], [])
        self.assertFalse(result['sending_enabled'])

    def test_get_has_http_route_but_maintenance_tools_do_not(self):
        paths = {getattr(route, 'path', '') for route in app.app.routes}
        self.assertIn('/api/runtime/applicant/jobs/search/get', paths)
        self.assertNotIn('/api/runtime/applicant/jobs/search/open', paths)
        self.assertNotIn('/api/runtime/applicant/jobs/search/step', paths)
        self.assertNotIn('/api/runtime/applicant/jobs/search/stop', paths)
        for name in ('runtime.applicant.jobs.search.open', 'runtime.applicant.jobs.search.step',
                     'runtime.applicant.jobs.search.stop'):
            self.assertTrue(main._router._tools[name].maintenance_only)

    def test_open_outside_saved_plan_is_refused(self):
        self.failed('runtime.applicant.jobs.search.open', {'keyword': 'Java 开发', 'city': CITY},
                    'JOB_SEARCH_OUT_OF_PLAN')
        self.failed('runtime.applicant.jobs.search.open', {'keyword': KEYWORD, 'city': '拉萨'},
                    'JOB_SEARCH_OUT_OF_PLAN')

    def test_open_rejects_extra_fields(self):
        with self.assertRaises(KernelError) as caught:
            with maintenance_access():
                self.dispatch('runtime.applicant.jobs.search.open',
                              {'keyword': KEYWORD, 'city': CITY, 'url': 'https://evil.example'})
        self.assertEqual(caught.exception.code, 'INVALID_JOB_SEARCH')

    def test_open_requires_browser_navigation_to_be_acknowledged(self):
        result = self.open(nav={'status': 'page_selection_required',
                                'account_verified': False, 'sending_enabled': False})
        self.assertEqual(result['status'], 'page_selection_required')
        state = result['state']
        # 导航未成功：组合仍在原地，且必须重新导航后才允许读页
        self.assertEqual(state['next'], {'city': CITY, 'keyword': KEYWORD})
        self.assertTrue(state['navigation_required'])
        self.assertEqual(state['status'], 'running')

    # ---- navigation gating --------------------------------------------
    def test_open_marks_the_current_combination(self):
        result = self.open()
        self.assertEqual(result['navigation']['status'], 'navigation_requested')
        state = result['state']
        self.assertEqual(state['next'], {'city': CITY, 'keyword': KEYWORD})
        self.assertFalse(state['navigation_required'])

    def test_step_requires_navigation_after_the_combination_moves_on(self):
        self.open()
        for _ in range(S.MAX_PAGES_PER_COMBO):
            self.step(1)
        result = self.step(1)
        self.assertEqual(result['status'], 'navigation_required')
        self.assertIsNotNone(result['next'])
        self.assertTrue(result['state']['navigation_required'])

    def test_out_of_order_open_is_refused_without_navigating(self):
        self.open()
        with mock.patch.object(applicant_browser, 'search_open') as patched:
            with maintenance_access():
                result = self.dispatch('runtime.applicant.jobs.search.open',
                                       {'keyword': 'Agentic', 'city': CITY})['data']
        self.assertEqual(result['status'], 'out_of_order')
        self.assertEqual(result['expected'], {'city': CITY, 'keyword': KEYWORD})
        patched.assert_not_called()

    # ---- page folding --------------------------------------------------
    def test_step_merges_and_deduplicates(self):
        self.open()
        first = self.step(1)
        self.assertEqual(first['event']['added'], 2)
        second = self.step(1)  # 同一页再读一次：没有新增
        self.assertEqual(second['event']['added'], 0)
        self.assertEqual(second['state']['counts']['jobs'], 2)

    def test_step_uses_scroll_only_for_later_pages(self):
        self.open()
        with mock.patch.object(applicant_browser, 'scroll_jobs') as scrolled:
            scrolled.return_value = {'status': 'scrolled', 'before': {'height': 10, 'cards': 2, 'y': 0}}
            with mock.patch.object(applicant_browser, 'read_current_jobs', return_value=OBSERVED):
                with maintenance_access():
                    self.dispatch('runtime.applicant.jobs.search.step', {})
                self.assertEqual(scrolled.call_count, 0)
                with maintenance_access():
                    self.dispatch('runtime.applicant.jobs.search.step', {})
                self.assertEqual(scrolled.call_count, 1)

    def test_step_restricted_stops_the_session_and_says_so(self):
        self.open()
        result = self.step(1, observation={'status': 'restricted', 'jobs': []})
        self.assertEqual(result['state']['status'], 'stopped')
        self.assertEqual(result['state']['stop_reason'], 'ACCOUNT_RISK')
        self.assertEqual(result['state']['counts']['failed_pages'], 1)
        self.assertIn('未按', result['event']['note'])

    def test_step_after_finish_reports_finished(self):
        self.open()
        self.step(1, observation={'status': 'restricted', 'jobs': []})
        result = self.step(1)
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(result['state']['stop_reason'], 'ACCOUNT_RISK')

    def test_step_rejects_any_payload(self):
        self.open()
        with self.assertRaises(KernelError) as caught:
            with maintenance_access():
                self.dispatch('runtime.applicant.jobs.search.step', {'script': 'alert(1)'})
        self.assertEqual(caught.exception.code, 'INVALID_BROWSER_REQUEST')

    def test_step_without_a_session_is_refused(self):
        with self.assertRaises(KernelError) as caught:
            with maintenance_access():
                self.dispatch('runtime.applicant.jobs.search.step', {})
        self.assertEqual(caught.exception.code, 'JOB_SEARCH_REQUIRED')

    # ---- results -------------------------------------------------------
    def test_get_returns_explained_candidates_from_collected_pages(self):
        self.open()
        self.step(1)
        result = self.get(include_candidates=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['source_type'], 'bounded_browser_search')
        self.assertEqual(len(result['candidates']), 2)
        for candidate in result['candidates']:
            self.assertFalse(candidate['sent'])
            # 方案第 119 行：每条都要有来源与观察时间（不能只有会话级聚合值）
            self.assertTrue(candidate['source'].startswith('boss_search:'), candidate['source'])
            self.assertIn(CITY, candidate['source'])
            self.assertIn(KEYWORD, candidate['source'])
            self.assertRegex(candidate['observed_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$')
            self.assertIsInstance(candidate['observed_at_epoch'], (int, float))
            self.assertFalse(candidate['platform_identity_verified'])
            self.assertIn(candidate['decision'], {'eligible', 'needs_review', 'rejected'})
        self.assertTrue(all(item['needs'] or item['reason_codes'] for item in result['candidates']
                            if item['decision'] != 'eligible'))
        self.assertFalse(result['automatic_execution'])

    def test_get_can_skip_candidates(self):
        self.open()
        self.step(1)
        result = self.get(include_candidates=False)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['state']['counts']['jobs'], 2)

    def test_get_rejects_unknown_payload(self):
        with self.assertRaises(KernelError) as caught:
            self.dispatch('runtime.applicant.jobs.search.get', {'url': 'x'})
        self.assertEqual(caught.exception.code, 'INVALID_JOB_SEARCH')

    def test_stop_requires_explicit_confirmation(self):
        self.open()
        self.failed('runtime.applicant.jobs.search.stop', {}, 'INVALID_JOB_SEARCH')
        with maintenance_access():
            result = self.dispatch('runtime.applicant.jobs.search.stop', {'confirmed': True})['data']
        self.assertEqual(result['state']['status'], 'stopped')
        self.assertEqual(result['state']['stop_reason'], 'USER_STOPPED')

    def test_stop_without_session_is_refused(self):
        self.failed('runtime.applicant.jobs.search.stop', {'confirmed': True}, 'JOB_SEARCH_REQUIRED')

    # ---- regression guards for defects found by static audit -------------
    def test_step_reports_page_read_status(self):
        self.open()
        result = self.step(1)
        self.assertEqual(result['status'], 'page_read')
        self.assertEqual(result['event']['added'], 2)

    def test_get_handles_more_than_fifty_collected_jobs(self):
        self.open()
        first = {'status': 'observed', 'jobs': [
            {'job_id': f'j{i}', 'title': 'AI 应用开发', 'company': '甲', 'city': '广州',
             'salary': '面议', 'education': '本科', 'experience': '1-3年'} for i in range(50)]}
        second = {'status': 'observed', 'jobs': [
            {'job_id': f'k{i}', 'title': 'AI 应用开发', 'company': '乙', 'city': '广州',
             'salary': '面议', 'education': '本科', 'experience': '1-3年'} for i in range(5)]}
        self.step(1, observation=first)
        self.step(1, observation=second)
        result = self.get(include_candidates=True)
        self.assertEqual(len(result['candidates']), 55)
        self.assertEqual(sum(result['decision_counts'].values()), 55)
        self.assertEqual(result['source_type'], 'bounded_browser_search')

    def test_reset_clears_the_session_so_it_can_start_again(self):
        self.open()
        self.step(1)
        with maintenance_access():
            cleared = self.dispatch('runtime.applicant.jobs.search.reset', {'confirmed': True})['data']
        self.assertEqual(cleared['status'], 'reset')
        self.assertEqual(self.get()['status'], 'not_started')
        again = self.open()
        self.assertEqual(again['navigation']['status'], 'navigation_requested')
        self.assertEqual(again['state']['counts']['jobs'], 0)

    def test_reset_requires_confirmation_and_an_existing_session(self):
        with self.assertRaises(KernelError) as caught:
            with maintenance_access():
                self.dispatch('runtime.applicant.jobs.search.reset', {})
        self.assertEqual(caught.exception.code, 'INVALID_JOB_SEARCH')
        self.failed('runtime.applicant.jobs.search.reset', {'confirmed': True}, 'JOB_SEARCH_REQUIRED')
        self.assertTrue(main._router._tools['runtime.applicant.jobs.search.reset'].maintenance_only)


if __name__ == '__main__':
    unittest.main()
