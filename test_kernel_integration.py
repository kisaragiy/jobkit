"""Kernel integration: real dispatch → router → engine → sandbox → handler → browser.

只替换最底层的 websocket 与 CDP HTTP 元数据（`_read_json`），业务函数
（search_open / read_current_jobs / detail_open / real_job_detail）全部真跑。
这样「能力名对不对得上、effect 与回执归属、页面状态解析」这些只在内核集成层
才暴露的问题，才会被真正执行到——把业务函数整个 mock 掉时它们永远跑不到
（2026-10-27 live 验收：4 个 100% 门禁全绿却必崩的 bug 都出在这一层）。

桩 CDP 会话按真实 Chrome 的语义行事：`Page.navigate` 会真的把那个 tab 的地址
换掉，`/json/list` 读到的是导航之后的页面地址。所以「导航之后还能不能找到列表页」
这种只有真浏览器才会问的问题，这里能被问出来。

不连 9222、不开浏览器、不碰 jobkit.db（临时库）、所有数据均为虚构。
"""
import contextlib
import json
import unittest
from pathlib import Path
from unittest import mock

import applicant_browser
import main
import runtime_tools
import store
import test_applicant_observation as fixture
from sandbox import KernelError, maintenance_access

JOBS_URL = 'https://www.zhipin.com/web/geek/jobs'
DETAIL_URL = 'https://www.zhipin.com/job_detail/%s.html'
PAGE_WS = 'ws://127.0.0.1:9222/devtools/page/kernel-integration'
BROWSER_WS = 'ws://127.0.0.1:9222/devtools/browser/11111111-2222-3333-4444-555555555555'
KEYWORD, CITY = 'Agent', '广州'

SCRIPTS = {name: Path(applicant_browser.__file__).with_name(name).read_text(encoding='utf-8')
           for name in ('applicant_dom_read.js', 'applicant_jobs_scroll.js',
                        'applicant_job_detail_read.js')}


def card(job_id):
    """列表卡：岗位记录字段与 applicant_job_search.JOB_FIELDS 一致（不含 JD）。"""
    return {'job_id': job_id, 'title': 'AI 应用开发', 'company': '虚构公司', 'city': CITY,
            'salary': '8-12K', 'education': '本科', 'experience': '1-3年'}


def detail_payload(job_id):
    return {'status': 'observed', 'job_id': job_id, 'truncated': False,
            'title': 'AI 应用开发', 'company': '虚构公司',
            'jd': '岗位职责（虚构）：%s 的正文。' % job_id}


class FakeBrowser:
    """一个足够诚实的 Edge 会话：导航会改变 tab 地址，页面按地址返回固定桩值。"""

    def __init__(self, jobs_tabs=1):
        self.tabs = [{'url': JOBS_URL, 'webSocketDebuggerUrl': PAGE_WS} for _ in range(jobs_tabs)]
        self.landing = {}            # 请求地址 → 实际落地地址（模拟服务端重定向到别的岗位）
        self.dom = {'status': 'observed', 'jobs': []}
        self.dom_queue = []          # 依次返回的列表页桩值（模拟刚导航完还没渲染）
        self.detail = None           # dict 或 callable(job_id)：详情页桩值
        self.scroll = {'status': 'scrolled', 'before': {'height': 1200, 'cards': 2, 'y': 0}}
        self.requests = []           # 所有 CDP 请求（跨连接）
        self.connections = []

    # ---- CDP HTTP 元数据（/json/version、/json/list）----
    def read_json(self, path):
        if path == '/json/version':
            return {'Browser': 'Edge/999.0.0.0', 'webSocketDebuggerUrl': BROWSER_WS}
        if path == '/json/list':
            return [dict(tab) for tab in self.tabs]
        raise AssertionError('业务层读了没有桩的元数据地址：%r' % path)

    # ---- websocket ----
    def open(self, endpoint, **kwargs):
        if endpoint != PAGE_WS:
            raise AssertionError('业务层连了没有桩的 endpoint：%r' % endpoint)
        connection = Connection(self, endpoint)
        self.connections.append(connection)
        return connection

    def reply(self, endpoint, request):
        self.requests.append(request)
        method = request['method']
        if method == 'Page.navigate':
            requested = request['params']['url']
            landing = self.landing.get(requested, requested)
            for tab in self.tabs:
                if tab['webSocketDebuggerUrl'] == endpoint:
                    tab['url'] = landing
            return {'frameId': 'stub-frame-1'}
        if method == 'Runtime.evaluate':
            return {'result': {'value': self.render(request['params']['expression'], endpoint)}}
        raise AssertionError('业务层发了没有桩的 CDP 方法：%r' % method)

    # ---- 页面固定桩值 ----
    def render(self, expression, endpoint):
        if expression == SCRIPTS['applicant_jobs_scroll.js']:
            return self.scroll
        if expression == SCRIPTS['applicant_dom_read.js']:
            return self.dom_queue.pop(0) if self.dom_queue else self.dom
        if expression == SCRIPTS['applicant_job_detail_read.js']:
            job_id = self.page_job_id(endpoint)
            if callable(self.detail):
                return self.detail(job_id)
            return dict(self.detail) if self.detail is not None else detail_payload(job_id)
        raise AssertionError('业务层执行了非固定脚本：%r' % expression[:80])

    def page_job_id(self, endpoint):
        for tab in self.tabs:
            if tab['webSocketDebuggerUrl'] == endpoint and tab['url'].startswith(
                    'https://www.zhipin.com/job_detail/'):
                return tab['url'].split('/job_detail/')[1].split('.html')[0]
        raise AssertionError('详情脚本读到了一个不是详情页的 tab')

    def urls(self):
        return [tab['url'] for tab in self.tabs]

    def methods(self):
        return [request['method'] for request in self.requests]

    def expressions(self):
        return [request['params']['expression'] for request in self.requests
                if request['method'] == 'Runtime.evaluate']


class Connection:
    def __init__(self, browser, endpoint):
        self.browser, self.endpoint = browser, endpoint
        self.sent, self.closed, self.pending = [], False, None

    def send(self, payload):
        self.pending = json.loads(payload)
        self.sent.append(self.pending)

    def settimeout(self, value):
        pass

    def recv(self):
        request = self.pending
        return json.dumps({'id': request['id'],
                           'result': self.browser.reply(self.endpoint, request)})

    def close(self):
        self.closed = True


class KernelIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ApplicantObservationTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        # 专用户已打开（fixture 走真内核开浏览器）。search.open 要求方案在库；
        # 上游 fixture 有时已存过方案，所以按 revision 决定是否补存，补完再核对锚点。
        with store.runtime_db() as c:
            plan = store.runtime_document(c, self.h.context['scope_id'], 'applicant_plan')
        if plan['revision'] == 0:
            self.f.f.save()          # fixture 内层 = test_applicant_delivery 的方案助手
            with store.runtime_db() as c:
                plan = store.runtime_document(c, self.h.context['scope_id'], 'applicant_plan')
        self.assertIn(KEYWORD, plan['content']['keywords'])
        self.assertIn(CITY, plan['content']['primary_cities'])
        self.browser = FakeBrowser()

    # ---- 只在这一层打桩：CDP 元数据 + websocket ----
    @contextlib.contextmanager
    def stub(self):
        with mock.patch.object(applicant_browser, '_read_json', side_effect=self.browser.read_json), \
                mock.patch.object(applicant_browser.websocket, 'create_connection',
                                  side_effect=self.browser.open), \
                mock.patch('time.sleep', return_value=None), \
                maintenance_access():
            yield

    def call(self, tool, payload=None):
        """返回完整信封（要 trace_id 查回执），业务失败抛 KernelError。"""
        with self.stub():
            return main.dispatch(tool, payload, self.h.context['context_token'])

    def session(self):
        with store.runtime_db() as c:
            document = store.runtime_document(c, self.h.context['scope_id'],
                                              runtime_tools.JOB_SEARCH_KIND)
        content = document['content']
        return content if isinstance(content, dict) else json.loads(content)

    def commit_receipt(self, task_id):
        with store.runtime_db() as c:
            row = c.execute('SELECT commit_receipt FROM runtime_tasks WHERE task_id=?',
                            (task_id,)).fetchone()
        return row['commit_receipt']

    def collect(self, *job_ids):
        """真实走 open + step 建会话：列表卡不带 JD，所以每条都是缺 JD 的缺口。"""
        self.browser.dom = {'status': 'observed', 'jobs': [card(job_id) for job_id in job_ids]}
        opened = self.call('runtime.applicant.jobs.search.open',
                           {'keyword': KEYWORD, 'city': CITY})
        self.assertEqual(opened['data']['navigation']['status'], 'navigation_requested')
        return self.call('runtime.applicant.jobs.search.step', {})

    # ---- a. 搜索页导航 ----
    def test_search_open_navigates_the_single_jobs_tab(self):
        result = self.call('runtime.applicant.jobs.search.open',
                           {'keyword': KEYWORD, 'city': CITY})['data']
        self.assertEqual(result['navigation']['status'], 'navigation_requested')
        self.assertEqual(result['navigated'], {'city': CITY, 'keyword': KEYWORD, 'index': 0})
        # state 里已经标记「已导航」：不标记就会让 step 永远回 navigation_required
        self.assertFalse(result['state']['navigation_required'])
        self.assertEqual(self.browser.methods(), ['Page.navigate'])
        self.assertEqual(self.browser.urls(), [
            'https://www.zhipin.com/web/geek/jobs?query=Agent&city=101280100'])
        self.assertTrue(self.browser.connections[0].closed)

    # ---- b. 读一页列表（bug 1：能力名要对得上，否则 KERNEL_REQUIRED）----
    def test_search_step_reads_page_and_stores_both_jobs(self):
        step = self.collect('gz1', 'gz2')
        data = step['data']
        self.assertEqual(data['status'], 'page_read')
        self.assertEqual(data['event']['status'], 'observed')
        self.assertEqual(data['event']['added'], 2)
        self.assertEqual(data['state']['counts'], {'jobs': 2, 'pages_read': 1, 'failed_pages': 0})
        self.assertEqual(data['pending_details'], 2)
        jobs = self.session()['jobs']
        self.assertEqual(sorted(jobs), ['gz1', 'gz2'])
        self.assertEqual({job['jd'] for job in jobs.values()}, {''})
        self.assertEqual({job['source'] for job in jobs.values()}, {'boss_search:广州/Agent'})
        # 读的是固定 DOM 脚本，不是任意注入
        self.assertIn(SCRIPTS['applicant_dom_read.js'], self.browser.expressions())
        # effect=state 的控制操作必须有提交回执，否则 recover 会把已完成的搜索判成没开始
        self.assertIsNotNone(self.commit_receipt(step['trace_id']))

    # ---- h. 翻页（BUG-6：浏览器 I/O 曾在写事务内自锁）----
    def test_second_page_scroll_does_not_lock_the_database(self):
        """第二次 step 会走 scroll 分支。

        BUG-6（2026-10-28 实测）：scroll_jobs 曾在 `with store.runtime_db()` 块内调用，
        内部 current_execution → require_registered → 再开连接 → _migrate() 的
        BEGIN IMMEDIATE，外层写事务未提交时自己锁自己，5 秒后 database is locked。
        这里跑真实 dispatch 链复现；仍在事务内的话这条会抛 TOOL_FAILED/OperationalError。
        """
        self.collect('gz1', 'gz2')
        second = self.call('runtime.applicant.jobs.search.step', {})['data']
        self.assertEqual(second['status'], 'page_read')
        self.assertIsNotNone(second['scroll'], '第二页必须走滚动分支才是真的覆盖到 BUG-6')
        self.assertEqual(second['scroll']['status'], 'scrolled')
        self.assertEqual(second['state']['counts']['pages_read'], 2)
        # 滚动脚本被真实执行过（不是被 mock 掉的）
        self.assertIn(SCRIPTS['applicant_jobs_scroll.js'], self.browser.expressions())

    # ---- c. 单条详情成功路径（bug 2：local_ui 不能去要 state 的提交凭据）----
    def test_job_detail_success_writes_jd_and_keeps_task_completed(self):
        self.collect('gz1', 'gz2')
        result = self.call('runtime.applicant.jobs.detail', {'job_id': 'gz1'})['data']
        self.assertEqual(result['status'], 'stored')
        self.assertEqual(result['job_id'], 'gz1')
        self.assertTrue(result['read_only'])
        self.assertFalse(result['delivery_confirmed'])
        self.assertEqual(self.browser.methods(), ['Page.navigate', 'Runtime.evaluate',
                                                  'Page.navigate', 'Runtime.evaluate'])
        self.assertEqual(self.browser.urls(), [DETAIL_URL % 'gz1'])
        jobs = self.session()['jobs']
        self.assertEqual(jobs['gz1']['jd'], detail_payload('gz1')['jd'])
        self.assertEqual(jobs['gz2']['jd'], '')
        self.assertTrue(jobs['gz1']['observed_at'])

    # ---- d. 详情对象不符：拒绝且不写库 ----
    def test_job_detail_for_another_job_is_refused_and_not_written(self):
        self.collect('gz1', 'gz2')
        # 导航去 gz2，落地却是 gz1（合并/下架岗位重定向）：正文不是请求对象的
        self.browser.landing[DETAIL_URL % 'gz2'] = DETAIL_URL % 'gz1'
        with self.assertRaises(KernelError) as caught:
            self.call('runtime.applicant.jobs.detail', {'job_id': 'gz2'})
        self.assertEqual(caught.exception.code, 'DETAIL_TARGET_MISMATCH')
        self.assertNotIn(SCRIPTS['applicant_job_detail_read.js'], self.browser.expressions())
        jobs = self.session()['jobs']
        self.assertEqual(jobs['gz2']['jd'], '')
        self.assertEqual(jobs['gz1']['jd'], '')

    def test_detail_script_reporting_another_job_is_schema_changed(self):
        self.collect('gz1')
        self.browser.detail = {'status': 'observed', 'job_id': 'swapped', 'truncated': False,
                               'title': '', 'company': '', 'jd': '别人的正文。'}
        with self.assertRaises(KernelError) as caught:
            self.call('runtime.applicant.jobs.detail', {'job_id': 'gz1'})
        self.assertEqual(caught.exception.code, 'BROWSER_SCHEMA_CHANGED')
        self.assertEqual(self.session()['jobs']['gz1']['jd'], '')

    # ---- e. 多个搜索页：不自己挑 ----
    def test_multiple_jobs_tabs_require_page_selection(self):
        self.browser = FakeBrowser(jobs_tabs=2)
        result = self.call('runtime.applicant.jobs.search.open',
                           {'keyword': KEYWORD, 'city': CITY})['data']
        self.assertEqual(result['status'], 'page_selection_required')
        self.assertEqual(self.browser.connections, [])

    # ---- g. 刚导航完页面还没渲染：有界重试，不能误判结构变化 ----
    def test_step_retries_when_the_freshly_navigated_page_is_not_rendered_yet(self):
        self.browser.dom = {'status': 'observed', 'jobs': [card('gz1'), card('gz2')]}
        self.call('runtime.applicant.jobs.search.open', {'keyword': KEYWORD, 'city': CITY})
        # 第一次读：卡片还没渲染出来（DOM 脚本只能报 schema_changed）
        self.browser.dom_queue = [{'status': 'schema_changed', 'jobs': []},
                                  {'status': 'observed', 'jobs': [card('gz1'), card('gz2')]}]
        data = self.call('runtime.applicant.jobs.search.step', {})['data']
        self.assertEqual(data['status'], 'page_read')
        self.assertEqual(data['event']['added'], 2)
        self.assertEqual(data['state']['counts']['failed_pages'], 0)
        self.assertEqual(len([e for e in self.browser.expressions()
                              if e == SCRIPTS['applicant_dom_read.js']]), 2)

    # ---- f. 批量补读：limit 有界、只补缺口、返回剩余 ----
    @unittest.expectedFailure
    def test_details_batch_fills_only_the_limit_and_reports_remaining(self):
        """规格：3 个缺 JD 的岗位 + limit=2 → 补 2 条、remaining=1。

        现在必挂，是真 bug（本层发现，代码未改）：
        - 现象：filled=1、failed=1、remaining=2，status 仍是 'batch_done'、stopped=None；
          第 2 条 results 是 {'status': 'jobs_page_required', 'reason': 'navigation_refused'}。
        - 最小复现：打开一个 jobs 页 → search.open → step 收 3 条 → search.details {'limit': 2}。
        - 根因猜测：detail_open 用 Page.navigate 把**唯一的 jobs 列表 tab** 导航成了
          /job_detail/<id>.html，于是下一次 _jobs_page_tabs() 找不到列表页（0 个 tab），
          detail_open 直接返回 jobs_page_required。整批只能补到第 1 条，且失败被记成
          单条 navigation_refused 后仍回 'batch_done'，调用方看不出这是「列表页已丢」。
          同一个根因也会让「读完详情后再 search.open / step」返回 jobs_page_required。
        解除这个标记 = 该 bug 修好后的验收条件（改业务代码的人删标记，不要改断言）。
        """
        self.collect('gz1', 'gz2', 'gz3')
        result = self.call('runtime.applicant.jobs.search.details', {'limit': 2})['data']
        self.assertEqual(result['filled'], 2)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(result['pending_total'], 3)
        self.assertEqual([item['job_id'] for item in result['results']], ['gz1', 'gz2'])
        self.assertEqual([item['status'] for item in result['results']], ['stored', 'stored'])
        self.assertIsNone(result['stopped'])
        jobs = self.session()['jobs']
        self.assertEqual(jobs['gz3']['jd'], '')
        self.assertEqual(jobs['gz1']['jd'], detail_payload('gz1')['jd'])


if __name__ == '__main__':
    unittest.main()
