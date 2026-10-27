"""The applicant workbench must actually wire the documented loop — no dead buttons, no invented routes.

This is a static contract check on ``web/workspace.html``: it proves every element the script touches
exists, every endpoint it calls is registered in the real ASGI app, and the panel cannot be mistaken
for an enabled sender.  It does not replace a browser run; it stops the wiring from silently rotting.
"""
import re
import pathlib
import unittest
from pathlib import Path

import app

PAGE = Path(__file__).with_name('web') / 'workspace.html'

LOOP_IDS = ('sendLoopPanel', 'windowStatus', 'refreshWindow', 'startWindow', 'advanceWindow',
            'loopDraftStatus', 'loopConfirmed', 'loopSteps', 'loopCard', 'loopStatus', 'loopDev',
            'refreshQueue', 'handoffQueue', 'queueStatus', 'queueItems', 'queueCards',
            'refreshStats', 'statsStatus', 'statsBody')

LOOP_ENDPOINTS = ('applicant/reply-window', 'applicant/replies/', 'applicant/send/recipient/confirm',
                  'applicant/send/reserve', 'applicant/send/release', 'applicant/send/handoff',
                  'applicant/send/manual/claim', 'applicant/send/readback', 'applicant/send/queue',
                  'applicant/send/queue/handoff', 'applicant/send/stats')


class WorkspaceUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = PAGE.read_text(encoding='utf-8')
        blocks = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', cls.page, re.S | re.I)
        cls.script = '\n'.join(blocks)

    def routes(self):
        live = set()
        for route in app.app.routes:
            path = getattr(route, 'path', '')
            if path.startswith('/api/runtime/'):
                live.add(re.sub(r'\{[^}]+\}', '{}', path))
        return live

    def test_loop_panel_and_controls_exist(self):
        for element in LOOP_IDS:
            with self.subTest(element=element):
                self.assertEqual(self.page.count('id="%s"' % element), 1)

    def test_every_element_the_script_touches_exists_in_the_page(self):
        ids = set(re.findall(r'id="([^"]+)"', self.page))
        used = set(re.findall(r"\$\('([^']+)'\)", self.script))
        self.assertEqual(sorted(used - ids), [])

    def test_every_called_endpoint_is_registered(self):
        live = self.routes()
        called = sorted(set(re.findall(r"api\('([^']+)'", self.script)))
        self.assertTrue(called)
        for path in called:
            full = '/api/runtime/' + path
            with self.subTest(path=full):
                if path.endswith('/'):
                    self.assertTrue(any(route.startswith(full) for route in live), full)
                else:
                    self.assertIn(full, live)

    def test_loop_endpoints_are_all_wired(self):
        called = set(re.findall(r"api\('([^']+)'", self.script))
        for path in LOOP_ENDPOINTS:
            with self.subTest(path=path):
                self.assertIn(path, called)

    def test_panel_is_hidden_until_the_applicant_workspace_loads(self):
        self.assertIn('<details id="sendLoopPanel" hidden>', self.page)
        self.assertIn("$('sendLoopPanel').hidden=false", self.script)

    def test_no_wording_claims_platform_delivery(self):
        for phrase in ('已送达', '送达成功', '发送成功', '平台已读'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.page)
        # The only "sent" states a person can record are explicit self-reports.
        self.assertIn('已自报', self.page)
        self.assertIn('不等于平台送达', self.page)

    def test_queue_renders_skipped_items_not_only_successes(self):
        self.assertIn('data.skipped', self.script)
        self.assertIn('未取到卡', self.script)

    def test_preflight_blockers_are_shown_in_plain_language(self):
        self.assertIn('blockerText', self.script)
        for code in ('STALE_REPLY_PROFILE', 'LIVE_MESSAGE_ANCHOR_CHANGED', 'MESSAGE_NOT_NEW_AFTER_WINDOW'):
            with self.subTest(code=code):
                self.assertIn(code, self.script)

    def test_job_search_dom_ids_are_present_and_unique(self):
        for element in ('jobSearchPanel', 'jobSearchStatus', 'jobSearchRefresh',
                        'jobSearchCounts', 'jobSearchCombos', 'jobSearchCandidates',
                        'jobSearchEmpty', 'jobSearchNavigation', 'jobSearchCommand',
                        'jobSearchCopy', 'jobSearchDev', 'jobSearchDebug'):
            with self.subTest(element=element):
                self.assertEqual(self.page.count('id="%s"' % element), 1)

    def test_job_search_is_read_only_and_scoped(self):
        self.assertIn('<details id="jobSearchPanel" hidden>', self.page)
        self.assertIn("initSendLoop();initJobSearch();", self.script)
        self.assertIn('clearLoop();clearJobSearch();', self.script)
        self.assertIn("api('applicant/jobs/search/get',{include_candidates:true},run.token)", self.script)
        for action in ('open', 'step', 'stop'):
            self.assertNotIn('applicant/jobs/search/' + action, self.page)
        self.assertIn('不等于已打招呼', self.page)
        self.assertIn('未验证平台账号身份', self.page)
        self.assertIn('需要在命令行重新导航到下一个组合', self.page)
        self.assertNotIn('已完成搜索所以没有岗位', self.page)

    def test_job_search_explains_stop_and_filter_codes(self):
        for code in ('COMBOS_DONE', 'LIMIT_JOBS', 'LIMIT_SECONDS', 'ACCOUNT_RISK',
                     'AUTH_REQUIRED', 'BROWSER_UNAVAILABLE', 'USER_STOPPED',
                     'SALARY_TOO_LOW', 'KEYWORD_MISMATCH', 'CITY_MISMATCH',
                     'EXCLUDED_TERM', 'EDUCATION_MISMATCH', 'EXPERIENCE_MISMATCH'):
            with self.subTest(code=code):
                self.assertIn(code, self.script)
        self.assertIn('读取失败不代表没有岗位', self.script)


class JobSearchPanelContractTest(unittest.TestCase):
    """有界搜索面板：只读结果，不提供采集按钮，不允许送达类文案。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (pathlib.Path(__file__).with_name('web') / 'workspace.html').read_text(encoding='utf-8')
        start = cls.html.index('<details id="jobSearchPanel"')
        end = cls.html.index('<details id="batchPanel"')
        cls.panel = cls.html[start:end]

    def test_panel_elements_exist_once(self):
        for name in ('jobSearchPanel', 'jobSearchRefresh', 'jobSearchStatus', 'jobSearchCounts',
                     'jobSearchCombos', 'jobSearchCandidates', 'jobSearchNavigation',
                     'jobSearchCommand', 'jobSearchCopy', 'jobSearchEmpty', 'jobSearchDebug'):
            self.assertEqual(self.html.count('id="%s"' % name), 1, name)

    def test_panel_is_hidden_until_loaded(self):
        self.assertRegex(self.html, r'<details id="jobSearchPanel" hidden>')

    def test_refresh_uses_the_read_only_route(self):
        self.assertIn("api('applicant/jobs/search/get'", self.html)

    def test_panel_offers_no_collection_or_send_button(self):
        for endpoint in ("jobs/search/open", "jobs/search/step", "jobs/search/stop",
                         "jobs/confirmed-preview", "greetings/prepare", "send/reserve"):
            self.assertNotIn(endpoint, self.panel, endpoint)

    def test_stop_reasons_are_explained_in_plain_words(self):
        for code, words in (('ACCOUNT_RISK', '安全验证'), ('AUTH_REQUIRED', '登录'),
                            ('BROWSER_UNAVAILABLE', '浏览器不可用'), ('USER_STOPPED', '手动停止'),
                            ('LIMIT_SECONDS', '时长上限')):
            self.assertIn(code, self.html)
            self.assertIn(words, self.html)

    def test_no_delivery_claim_on_the_search_panel(self):
        for word in ('已送达', '送达成功', '发送成功', '平台已读'):
            self.assertNotIn(word, self.panel, word)
        self.assertIn('采集结果不等于已打招呼', self.panel)
        self.assertIn('采集候选（非发送记录）', self.panel)


class CapacityAlarmContractTest(unittest.TestCase):
    """台账容量报警：保护集超限必须在待核对区域顶部可见，且说的是「需人工归档」不是「发送失败」。

    背景（docs/reviews/hermes-p8-2026-10-27.md 第 4 节）：后端早就有 capacity.overflow，
    工作台没展示 = 等于没报警。这里锁的是「展示 + 文案口径 + 只读路由」三件事。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (pathlib.Path(__file__).with_name('web') / 'workspace.html').read_text(encoding='utf-8')
        blocks = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', cls.html, re.S | re.I)
        cls.script = '\n'.join(blocks)
        start = cls.html.index('<section class="panel"><h3>⑤')
        end = cls.html.index('<section class="panel"><h3>⑥')
        cls.panel = cls.html[start:end]
        fn_start = cls.script.index('function paintCapacity')
        cls.capacity_copy = cls.script[fn_start:cls.script.index('function paintOutcome')]

    def test_alarm_is_the_first_thing_in_the_pending_panel(self):
        self.assertIn('<div id="capacityAlarm" role="alert" hidden></div>', self.panel)
        self.assertLess(self.panel.index('id="capacityAlarm"'),
                        self.panel.index('id="outcomeStatus"'))
        self.assertEqual(self.html.count('id="capacityAlarm"'), 1)

    def test_capacity_comes_from_the_read_only_outcome_route(self):
        self.assertIn("api('applicant/chat/outcome')", self.script)     # 不是 verify/resolve
        self.assertIn('paintCapacity(data&&data.capacity)', self.script)
        for field in ('capacity', 'overflow', 'overflow_count', 'protected', 'protected_limit'):
            with self.subTest(field=field):
                self.assertIn(field, self.script)

    def test_alarm_is_hidden_unless_overflow_is_true(self):
        self.assertIn("$('capacityAlarm').hidden=true", self.script)
        self.assertIn('data.overflow!==true', self.script)
        self.assertIn('clearCapacityAlarm();', self.script)             # 换工作区要收起来

    def test_wording_means_archive_not_send_failure(self):
        for word in ('容量上限', '人工归档', '保护集'):
            with self.subTest(word=word):
                self.assertIn(word, self.capacity_copy)
        # 报警文案不能说成「发送失败」：那会让人去重发，而这批记录恰恰不许重发
        for phrase in ('发送失败', '发送出错', '发送异常'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.capacity_copy)
        self.assertNotIn('发送失败', self.html)
        for claim in ('已送达', '送达成功', '发送成功', '平台已读'):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, self.capacity_copy)
                self.assertNotIn(claim, self.panel)

    def test_alarm_does_not_pretend_the_ledger_evicts_evidence(self):
        self.assertIn('不会自动删除', self.capacity_copy)
        # 容量报警不能变成「可以重发」的信号
        self.assertIn('拦住重复发送', self.capacity_copy)


class ArchiveEntryContractTest(unittest.TestCase):
    """归档出口：报警必须能落到一个动作上，且这个动作读起来是「人工归档」而不是「发送失败」。

    后端早就有 `runtime.applicant.chat.outcome.archive`，工作台没入口 = 报警只说不做。
    这里锁四件事：按钮唯一且按条件显示、只打归档路由、操作人必填、拒绝码翻译成下一步动作。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (pathlib.Path(__file__).with_name('web') / 'workspace.html').read_text(encoding='utf-8')
        blocks = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', cls.html, re.S | re.I)
        cls.script = '\n'.join(blocks)
        start = cls.html.index('<section class="panel"><h3>⑤')
        end = cls.html.index('<section class="panel"><h3>⑥')
        cls.panel = cls.html[start:end]
        fn = cls.script.index('async function archiveLedgerEvidence')
        cls.archive_fn = cls.script[fn:cls.script.index("$('archiveLedger').onclick")]
        copy = cls.script.index('const archiveText')
        cls.archive_copy = cls.script[copy:cls.script.index('function archivePhrase')]

    def test_button_exists_once_and_sits_with_the_alarm(self):
        self.assertEqual(self.html.count('id="archiveLedger"'), 1)
        self.assertIn('<button id="archiveLedger" type="button" hidden>归档已联系证据</button>', self.panel)
        self.assertLess(self.panel.index('id="capacityAlarm"'), self.panel.index('id="archiveLedger"'))
        self.assertLess(self.panel.index('id="archiveLedger"'), self.panel.index('id="outcomeList"'))

    def test_button_is_shown_only_when_there_is_something_to_archive(self):
        self.assertIn('capacity.overflow===true', self.script)
        self.assertIn('archive.archived', self.script)
        self.assertIn("$('archiveLedger').hidden=!(overflow||total>0);", self.script)
        # 换工作区要收起来，不能带着上一个工作区的状态留在屏幕上
        self.assertIn('clearCapacityAlarm();clearArchiveEntry();', self.script)
        self.assertIn("$('archiveLedger').hidden=true", self.script)

    def test_only_the_outcome_archive_route_is_called(self):
        called = re.findall(r"api\('([^']+)'", self.archive_fn)
        self.assertEqual(called, ['applicant/chat/outcome/archive'])
        for forbidden in ('resolve', 'chat/submit', 'chat/authorize', 'send/reserve'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.archive_fn)
        paths = {getattr(route, 'path', '') for route in app.app.routes}
        self.assertIn('/api/runtime/applicant/chat/outcome/archive', paths)

    def test_operator_is_required_before_any_request(self):
        self.assertIn('prompt(', self.archive_fn)
        self.assertIn('if(!by){', self.archive_fn)
        self.assertLess(self.archive_fn.index('prompt('), self.archive_fn.index("api('applicant/chat/outcome/archive'"))
        self.assertLess(self.archive_fn.index('if(!by)'), self.archive_fn.index("api('applicant/chat/outcome/archive'"))
        self.assertIn('confirm(', self.archive_fn)
        # 确认文案必须说清「搬走证据」与「不解除未核对」两件事
        self.assertIn('已联系证据', self.archive_fn)
        self.assertIn('不会解除未核对记录', self.archive_fn)
        self.assertIn('仍会拦住重复发送', self.archive_fn)

    def test_refusal_codes_say_what_to_do_instead_of_failing_flat(self):
        for code in ('SEND_LEDGER_UNRESOLVED', 'SEND_LEDGER_IN_FLIGHT'):
            with self.subTest(code=code):
                self.assertIn(code, self.archive_copy)
        self.assertIn('先核对那条记录再归档', self.archive_copy)
        # 失败要落到文案上，不能静默
        self.assertIn('archivePhrase(e.code)', self.archive_fn)
        self.assertNotIn('归档失败', self.archive_copy)

    def test_result_shows_archived_and_total_counts(self):
        for field in ('archived_count', 'archive_total', 'active_count', 'already_archived'):
            with self.subTest(field=field):
                self.assertIn(field, self.archive_fn)
        self.assertIn('await loadOutcome();', self.archive_fn)          # 成功后重拉面板
        self.assertIn('textContent=', self.archive_fn)
        self.assertNotIn('innerHTML', self.archive_fn)

    def test_archive_copy_never_claims_delivery_or_send_failure(self):
        for phrase in ('发送失败', '发送出错', '发送异常', '已送达', '送达成功', '发送成功', '平台已读'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.archive_copy)
                self.assertNotIn(phrase, self.panel)


class NamedArchiveSelectionContractTest(unittest.TestCase):
    """点名归档：勾选具体记录再搬，不靠整区一把梭。

    后端 `runtime.applicant.chat.outcome.archive` 早就吃 `ledger_ids`
    （docs/api/applicant-send-loop.md 第十二节），工作台却只有整区归档 = 选择权没交到人手上。
    这里锁五件事：复选框 value 就是那条 `ledger_id`、没选中不响应、请求体真带 `ledger_ids`、
    四个拒绝码都有下一步动作、文案不许出现「发送失败 / 已送达」。
    """

    REFUSALS = ('SEND_LEDGER_IN_FLIGHT', 'SEND_LEDGER_UNRESOLVED',
                'SEND_LEDGER_NOT_ARCHIVABLE', 'SEND_LEDGER_ENTRY_NOT_FOUND')

    @classmethod
    def setUpClass(cls):
        cls.html = (pathlib.Path(__file__).with_name('web') / 'workspace.html').read_text(encoding='utf-8')
        blocks = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', cls.html, re.S | re.I)
        cls.script = '\n'.join(blocks)
        start = cls.html.index('<section class="panel"><h3>⑤')
        end = cls.html.index('<section class="panel"><h3>⑥')
        cls.panel = cls.html[start:end]
        fn = cls.script.index('async function archiveSelectedEvidence')
        cls.select_fn = cls.script[fn:cls.script.index("$('archiveSelected').onclick")]
        copy = cls.script.index('const archiveSelectedText')
        cls.select_copy = cls.script[copy:cls.script.index('function refreshArchiveSelection')]
        paint = cls.script.index('function paintOutcome(')
        cls.paint_fn = cls.script[paint:cls.script.index('/* 归档出口')]

    def test_each_row_gets_one_checkbox_valued_with_its_ledger_id(self):
        # value 必须是稳定标识（ledger_id），不能用序号/位置：重拉面板后序号会变，点名校验就会点错条
        self.assertIn('cbox.value=item.ledger_id', self.paint_fn)
        self.assertIn("if(typeof item.ledger_id==='string'&&item.ledger_id){", self.paint_fn)
        self.assertIn('cbox.type=checkbox', self.paint_fn.replace("'", ''))
        self.assertIn('archiveCandidates.set(item.ledger_id,cbox)', self.paint_fn)
        self.assertIn('cbox.checked=archiveSelection.has(item.ledger_id)', self.paint_fn)
        # 每行一个：复选框在 forEach 里建，不是复用一个
        self.assertIn('items.forEach(', self.paint_fn)

    def test_selection_button_exists_once_next_to_whole_archive(self):
        self.assertEqual(self.html.count('id="archiveSelected"'), 1)
        self.assertEqual(self.html.count('id="archiveSelectedStatus"'), 1)
        self.assertIn('<button id="archiveSelected" type="button" hidden disabled>仅归档选中</button>', self.panel)
        self.assertLess(self.panel.index('id="archiveLedger"'), self.panel.index('id="archiveSelected"'))
        self.assertLess(self.panel.index('id="archiveSelected"'), self.panel.index('id="outcomeList"'))

    def test_button_is_enabled_only_when_something_is_selected(self):
        self.assertIn("button.disabled=!!archiveSelectedRun||!archiveSelection.size;", self.script)
        self.assertIn("button.hidden=!archiveCandidates.size;", self.script)
        # 空选也得有话说，不能点了没反应
        self.assertIn("if(!ids.length){", self.select_fn)
        self.assertIn('没有选中任何记录', self.select_fn)
        self.assertLess(self.select_fn.index('if(!ids.length){'), self.select_fn.index('prompt('))
        # 换工作区/换列表要收干净，不能把上个工作区的勾选带到新工作区
        self.assertIn('archiveSelection.clear();archiveCandidates.clear();', self.script)

    def test_request_carries_ledger_ids_to_the_same_archive_route(self):
        called = re.findall(r"api\('([^']+)'", self.select_fn)
        self.assertEqual(called, ['applicant/chat/outcome/archive'])
        self.assertIn('const ids=[...archiveSelection];', self.select_fn)
        self.assertIn('{ledger_ids:ids,confirmed_by:by}', self.select_fn)
        # 点名归档不该顺手带上整区/指纹参数
        self.assertNotIn('target_sha256', self.select_fn)
        # 没有改成新路由：整区归档那条调用原样保留
        self.assertIn("api('applicant/chat/outcome/archive',{confirmed_by:by})", self.script)
        paths = {getattr(route, 'path', '') for route in app.app.routes}
        self.assertIn('/api/runtime/applicant/chat/outcome/archive', paths)

    def test_operator_and_confirm_come_before_the_request(self):
        api_at = self.select_fn.index("api('applicant/chat/outcome/archive'")
        self.assertLess(self.select_fn.index('prompt('), api_at)
        self.assertLess(self.select_fn.index('confirm('), api_at)
        self.assertLess(self.select_fn.index('if(!by){'), api_at)
        confirm_line = self.select_fn[self.select_fn.index('confirm('):api_at]
        self.assertIn('仍会拦住重复发送', confirm_line)
        self.assertIn('不会解除未核对记录', confirm_line)
        self.assertIn('已联系证据', confirm_line)

    def test_each_refusal_code_says_the_next_step(self):
        for code in self.REFUSALS:
            with self.subTest(code=code):
                self.assertIn(code, self.select_copy)
                hit = re.search(r"%s:'([^']+)'" % code, self.select_copy)
                self.assertIsNotNone(hit, code)
                self.assertTrue(hit.group(1).strip(), code)
        # 整批拒绝要落地成一句话，不能静默失败
        self.assertIn('archiveSelectedPhrase(e.code)', self.select_fn)
        self.assertIn('不会部分成功', self.select_copy)

    def test_result_reports_counts_and_reloads_the_panel(self):
        for field in ('archived_count', 'already_archived', 'archive_total', 'active_count'):
            with self.subTest(field=field):
                self.assertIn(field, self.select_fn)
        self.assertIn("已归档 '+moved+' 条", self.select_fn)
        self.assertIn('此前已归档', self.select_fn)
        self.assertIn('await loadOutcome();', self.select_fn)

    def test_only_text_content_and_no_wording_claims_delivery(self):
        self.assertNotIn('innerHTML', self.select_fn)
        self.assertNotIn('innerHTML', self.paint_fn)
        self.assertIn('textContent=', self.select_fn)
        self.assertIn('textContent=', self.paint_fn)
        for phrase in ('发送失败', '发送出错', '发送异常', '已送达', '送达成功', '发送成功', '平台已读'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.select_copy)
                self.assertNotIn(phrase, self.panel)
        self.assertNotIn('发送失败', self.html)
        self.assertNotIn('已送达', self.html)


class ArchivableEvidencePanelContractTest(unittest.TestCase):
    """可归档证据区：复选框必须挂在**搬得走**的对象上。

    `outcome.get` 的 `blocking` 是未核对记录，按规则**本来就不许归档**
    （后端报 `SEND_LEDGER_UNRESOLVED`）。复选框挂在那批行上 = 每个勾都会失败。
    所以这里锁五件事：新区域只在 `archivable` 非空时显示、复选框 value 来自
    `ledger_id`、`blocking` 行已经没有复选框、请求体照旧带 `ledger_ids`、
    上限截断要说清「只列最近 N 条」。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (pathlib.Path(__file__).with_name('web') / 'workspace.html').read_text(encoding='utf-8')
        blocks = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', cls.html, re.S | re.I)
        cls.script = '\n'.join(blocks)
        start = cls.html.index('<section class="panel"><h3>⑤')
        end = cls.html.index('<section class="panel"><h3>⑥')
        cls.panel = cls.html[start:end]
        paint = cls.script.index('function paintOutcome(')
        cls.blocking_fn = cls.script[paint:cls.script.index('/* 可归档证据')]
        view = cls.script.index('function paintArchivable(')
        cls.archivable_fn = cls.script[view:cls.script.index('/* 归档出口')]
        fn = cls.script.index('async function archiveSelectedEvidence')
        cls.select_fn = cls.script[fn:cls.script.index("$('archiveSelected').onclick")]

    def test_area_exists_once_inside_the_outcome_panel(self):
        self.assertEqual(self.html.count('id="archivableArea"'), 1)
        self.assertEqual(self.html.count('id="archivableList"'), 1)
        self.assertIn('<div id="archivableArea" hidden>', self.panel)
        self.assertLess(self.panel.index('id="archivableArea"'), self.panel.index('id="archivableList"'))
        # 归档出口那几个按钮在这个区域之前：先说清能搬什么，再给动作
        self.assertLess(self.panel.index('id="archiveSelected"'), self.panel.index('id="archivableArea"'))

    def test_area_is_shown_only_when_there_is_archivable_evidence(self):
        self.assertIn("const evidence=(data&&Array.isArray(data.archivable))?data.archivable:[];",
                      self.blocking_fn)
        self.assertIn('paintArchivable(evidence,data&&data.archivable_total,data&&data.archivable_truncated);',
                      self.blocking_fn)
        # 空列表就整块收起来，不留一个「可归档证据」空壳
        self.assertIn('area.hidden=!rows.length;', self.archivable_fn)
        self.assertIn('if(!rows.length)return;', self.archivable_fn)
        # 换工作区要收干净
        self.assertIn("$('archivableArea').hidden=true;$('archivableList').textContent='';", self.script)

    def test_checkbox_value_is_the_ledger_id_of_that_evidence(self):
        self.assertIn('cbox.value=item.ledger_id', self.archivable_fn)
        self.assertIn("if(typeof item.ledger_id==='string'&&item.ledger_id){", self.archivable_fn)
        self.assertIn('cbox.type=checkbox', self.archivable_fn.replace("'", ''))
        self.assertIn('archiveCandidates.set(item.ledger_id,cbox)', self.archivable_fn)
        self.assertIn('cbox.checked=archiveSelection.has(item.ledger_id)', self.archivable_fn)
        # 选中集合仍由 refreshArchiveSelection 按「还在候选里」剪枝
        self.assertIn('refreshArchiveSelection();', self.archivable_fn)
        self.assertIn('rows.forEach(', self.archivable_fn)

    def test_blocking_rows_are_display_only_now(self):
        # 未核对行不许再建勾选框、不许登记成归档候选：勾了必然被 SEND_LEDGER_UNRESOLVED 拒绝
        for gone in ("createElement('input')", 'cbox', 'archiveCandidates.set(',
                     'archiveSelection.has('):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.blocking_fn)
        # 但它们原来那套核对动作一个字没少
        self.assertIn('resolveOutcome(item.ledger_id', self.blocking_fn)
        self.assertIn('items.forEach(', self.blocking_fn)

    def test_request_still_carries_ledger_ids_from_the_selection(self):
        self.assertIn('const ids=[...archiveSelection];', self.select_fn)
        self.assertIn('{ledger_ids:ids,confirmed_by:by}', self.select_fn)
        self.assertIn("api('applicant/chat/outcome/archive'", self.select_fn)
        self.assertIn('if(!ids.length){', self.select_fn)
        # 确认文案两条底线不能丢
        self.assertIn('仍会拦住重复发送', self.select_fn)
        self.assertIn('不会解除未核对记录', self.select_fn)

    def test_truncation_is_stated_instead_of_silently_dropped(self):
        # 总数与截断标记从响应原样传进渲染函数
        self.assertIn('data.archivable_total', self.blocking_fn)
        self.assertIn('data.archivable_truncated', self.blocking_fn)
        self.assertIn('truncated===true', self.archivable_fn)
        self.assertIn('只列最近', self.archivable_fn)
        self.assertIn('可归档共', self.archivable_fn)

    def test_only_text_content_no_markup_and_no_delivery_claims(self):
        self.assertNotIn('innerHTML', self.archivable_fn)
        self.assertNotIn('innerHTML', self.panel)
        self.assertIn('textContent=', self.archivable_fn)
        for phrase in ('发送失败', '已送达', '送达成功', '发送成功', '平台已读'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.panel)


if __name__ == '__main__':
    unittest.main()
