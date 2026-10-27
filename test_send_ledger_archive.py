"""发送台账「归档出口」的契约测试。

背景：保护集（阻塞态 + 已联系证据）永不因容量淘汰，超 `MAX_PROTECTED_KEPT` 只报警。
用满之后原来没有出路。归档出口把**已联系证据**搬进同库的另一份文档
（`applicant_send_ledger_archive`），活跃台账因此腾出额度。

这个改动最危险的方向不是「搬不走」，而是「搬走了就查不到」——那等于归档一次就把
「这个对象联系过」擦掉，同一对象立刻能再发一次，比不归档更糟。所以本文件锁死：

1. 归档后已联系证据**仍然拦得住**（活跃 ∪ 归档都读，含跨 scope）；
2. 未闭环（`submitting` / `submitted_unverified` / `outcome_unknown` / `conflict`）
   和流水（`resolved_not_delivered`）一律不许归档，且按状态给不同错误码；
3. 同一 `ledger_id` / 同一 `target_sha256` 反复归档不产生第二条（幂等）；
4. 归档前后 `blocking_items` 数量与条目完全一致（归档不解除任何阻塞）；
5. 归档动作本身是带提交回执的控件：崩溃恢复时从两个文档重读核对，不重放；
6. 回执被篡改时恢复必须报 `INVALID_COMMIT_RECEIPT`，不能「能解析就信」。
"""
import json
import time
import unittest
from unittest import mock

import applicant_browser as A
import applicant_send_ledger as ledger
import store
import test_applicant_send as harness
from sandbox import KernelError

TEXT = '您好，我做过 Agent 后端，方便聊两句吗？'
POINT = {'x': 640, 'y': 480, 'w': 40, 'h': 20}
LIVE = {'status': 'target_read', 'name': '示例招聘方', 'company': '示例科技有限公司',
        'position': 'AI应用开发助理', 'job_id': 'job-AI-001',
        'sending_enabled': False, 'delivery_confirmed': False}
TS = 1793104188.0
SCOPE = 'scope-applicant'


def target(n):
    return '%064x' % n


def make_entry(state, n, scope=SCOPE):
    """按目标状态造一条台账记录（n 只用来区分对象与时间）。"""
    stamp = TS + n
    entry = ledger.new_entry(scope_id=scope, target_sha256=target(n),
                             body_sha256='b' * 64, action='greeting', ts=stamp)
    if state == ledger.STATE_SUBMITTING:
        return entry
    if state in (ledger.STATE_OBSERVED, ledger.STATE_SUBMITTED, ledger.STATE_CONFLICT):
        return ledger.close(entry, state, ts=stamp + 1)
    if state in (ledger.STATE_UNKNOWN, ledger.STATE_DELIVERED, ledger.STATE_NOT_DELIVERED):
        closed = ledger.close(entry, ledger.STATE_UNKNOWN, ts=stamp + 1)
        if state == ledger.STATE_UNKNOWN:
            return closed
        resolution = ('delivered' if state == ledger.STATE_DELIVERED else 'not_delivered')
        return ledger.resolve(closed, resolution, by='user', ts=stamp + 2)
    raise AssertionError('unsupported fixture state: %s' % state)


class ArchiveLogicTests(unittest.TestCase):
    """纯逻辑层：归档资格、挑选、合并、快照。"""

    def test_archivable_set_is_exactly_contacted_evidence(self):
        # 谁把阻塞态加进可归档集，就等于关掉闸门 —— 这条断言要求先改这里再改代码
        self.assertEqual(ledger.ARCHIVABLE_STATES, ledger.CONTACTED_STATES)

    def test_only_contacted_evidence_is_archivable(self):
        for state in ledger.ARCHIVABLE_STATES:
            self.assertIsNone(ledger.archive_refusal(make_entry(state, 1)), state)
        for state in ledger.BLOCKING_STATES + ledger.EVICTABLE_STATES:
            self.assertIsNotNone(ledger.archive_refusal(make_entry(state, 2)), state)

    def test_refusal_codes_distinguish_what_to_do_next(self):
        self.assertEqual(ledger.archive_refusal(make_entry(ledger.STATE_SUBMITTING, 1)),
                         'SEND_LEDGER_IN_FLIGHT')
        for state in (ledger.STATE_SUBMITTED, ledger.STATE_UNKNOWN, ledger.STATE_CONFLICT):
            self.assertEqual(ledger.archive_refusal(make_entry(state, 2)),
                             'SEND_LEDGER_UNRESOLVED', state)
        self.assertEqual(ledger.archive_refusal(make_entry(ledger.STATE_NOT_DELIVERED, 3)),
                         'SEND_LEDGER_NOT_ARCHIVABLE')

    def test_refusal_is_fail_closed_on_missing_or_unknown_state(self):
        for entry in ({}, {'state': 'some_future_state'}, {'state': None}, 'not-a-dict'):
            self.assertIsNotNone(ledger.archive_refusal(entry), entry)

    def test_bulk_plan_moves_only_contacted_and_leaves_blockers_in_place(self):
        items = [make_entry(ledger.STATE_UNKNOWN, 1), make_entry(ledger.STATE_OBSERVED, 2),
                 make_entry(ledger.STATE_SUBMITTING, 3), make_entry(ledger.STATE_DELIVERED, 4),
                 make_entry(ledger.STATE_NOT_DELIVERED, 5)]
        moved, kept = ledger.plan_archive(items)
        self.assertEqual([i['ledger_id'] for i in moved],
                         [items[1]['ledger_id'], items[3]['ledger_id']])
        self.assertEqual([i['ledger_id'] for i in kept],
                         [items[0]['ledger_id'], items[2]['ledger_id'], items[4]['ledger_id']])
        # 归档前后阻塞集合必须一模一样
        self.assertEqual([i['ledger_id'] for i in ledger.blocking_items(kept)],
                         [i['ledger_id'] for i in ledger.blocking_items(items)])

    def test_named_plan_refuses_instead_of_silently_skipping(self):
        items = [make_entry(ledger.STATE_UNKNOWN, 1), make_entry(ledger.STATE_OBSERVED, 2)]
        with self.assertRaises(ValueError) as caught:
            ledger.plan_archive(items, ledger_ids=[items[0]['ledger_id']])
        self.assertEqual(str(caught.exception), 'SEND_LEDGER_UNRESOLVED')
        with self.assertRaises(ValueError) as caught:
            ledger.plan_archive(items, target_sha256=items[0]['target_sha256'])
        self.assertEqual(str(caught.exception), 'SEND_LEDGER_UNRESOLVED')
        # 合格的点名归档正常返回
        moved, kept = ledger.plan_archive(items, ledger_ids=[items[1]['ledger_id']])
        self.assertEqual([i['ledger_id'] for i in moved], [items[1]['ledger_id']])
        self.assertEqual(len(kept), 1)

    def test_plan_does_not_mutate_input(self):
        items = [make_entry(ledger.STATE_OBSERVED, 1)]
        snapshot = json.dumps(items, sort_keys=True)
        ledger.plan_archive(items)
        self.assertEqual(json.dumps(items, sort_keys=True), snapshot)

    def test_plan_never_moves_dirty_or_cross_scope_rows(self):
        dirty = ['not-a-dict']
        other = make_entry(ledger.STATE_OBSERVED, 4, scope='scope-other')
        moved, kept = ledger.plan_archive(dirty + [other], scope_id=SCOPE)
        self.assertEqual(moved, [])
        self.assertEqual(len(kept), 2)

    def test_merge_archive_is_idempotent_by_ledger_id(self):
        entry = make_entry(ledger.STATE_OBSERVED, 1)
        first = ledger.merge_archive([], [entry])
        second = ledger.merge_archive(first, [entry])
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['ledger_id'], entry['ledger_id'])
        # 同 id 的新版本原地替换，不是追加
        updated = dict(entry, note='later')
        third = ledger.merge_archive(second, [updated])
        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]['note'], 'later')

    def test_archive_report_counts_targets_and_flags_blockers(self):
        items = [make_entry(ledger.STATE_OBSERVED, 1), make_entry(ledger.STATE_OBSERVED, 1),
                 make_entry(ledger.STATE_DELIVERED, 2), make_entry(ledger.STATE_UNKNOWN, 3)]
        report = ledger.archive_report(items + ['dirty'])
        self.assertEqual(report['archived'], 4)
        self.assertEqual(report['contacted'], 3)
        # 对象 1 有两条（同 target 不同正文的极端情形），对象 2、3 各一条
        self.assertEqual(report['targets'], 3)
        # 归档里出现阻塞态就是数据坏了 —— 报告必须看得见（沙箱也会照样拦）
        self.assertEqual(report['blocking'], 1)

    def test_archive_document_shape_supports_the_union_reader(self):
        """活跃 ∪ 归档的并集读法：搬走的条目靠归档文档仍然参与 target_evidence。"""
        entry = make_entry(ledger.STATE_OBSERVED, 7)
        kind, found = ledger.target_evidence([], entry['target_sha256'])
        self.assertEqual(kind, None)
        kind, found = ledger.target_evidence([entry], entry['target_sha256'])
        self.assertEqual(kind, 'contacted')
        self.assertEqual(found['ledger_id'], entry['ledger_id'])


def panel(marks=34):
    return {'status': 'observed', 'composer_ready': True, 'can_send': True,
            'send_disabled': False, 'empty': True, 'delivered_marks': marks,
            'read_marks': 0, 'panel_text': '', 'account_verified': False,
            'sending_enabled': False, 'delivery_confirmed': False}


class ArchiveToolTest(unittest.TestCase):
    """走真实执行链（router → engine → sandbox → handler），临时库，不碰真实 jobkit.db。"""

    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.context = self.f.h.context
        self.dispatch = self.f.dispatch
        self.target = dict(LIVE)
        self.marks = 34
        for name, fn in (('real_target', lambda: dict(self.target)),
                         ('real_read_composer', lambda: {'status': 'read', 'text': TEXT,
                                                         'composer_ready': True})):
            p = mock.patch.object(A, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(A, 'real_observe', side_effect=lambda *a, **k: panel(self.marks))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(A, 'real_submit',
                              side_effect=lambda point: {'status': 'submit_requested',
                                                         'sending_enabled': False,
                                                         'delivery_confirmed': False})
        p.start()
        self.addCleanup(p.stop)

    # ---- helpers -------------------------------------------------------
    def authorize(self, *, action='greeting', text=TEXT, by='user'):
        payload = {'text': text, 'confirmed_by': by}
        if action:
            payload['action'] = action
        return self.dispatch('runtime.applicant.chat.authorize', payload)['data']

    def submit(self):
        return self.dispatch('runtime.applicant.chat.submit', {'point': dict(POINT)})['data']

    def archive(self, **payload):
        payload.setdefault('confirmed_by', 'user')
        return self.dispatch('runtime.applicant.chat.outcome.archive', payload)

    def failed(self, tool, payload, code):
        with self.assertRaises(KernelError) as caught:
            self.dispatch(tool, payload)
        self.assertEqual(caught.exception.code, code)

    def document_items(self, kind):
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.context['scope_id'], kind)
        content = doc['content'] if isinstance(doc['content'], dict) else {}
        return [i for i in (content.get('items') or []) if isinstance(i, dict)]

    def ledger_items(self):
        return self.document_items(ledger.LEDGER_KIND)

    def archive_items(self):
        return self.document_items(ledger.ARCHIVE_KIND)

    def write_ledger(self, items, scope=None, kind=ledger.LEDGER_KIND):
        """造一份台账/归档文档。fixture 行统一落到被写的那个 scope（台账按 scope 存）。"""
        scope = scope or self.context['scope_id']
        rows = [dict(i, scope_id=scope) if isinstance(i, dict) else i for i in items]
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                      "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                      "content=excluded.content",
                      (scope, kind, 1, json.dumps({'items': rows}, ensure_ascii=False)))

    def observed_entry(self, **changes):
        """先真发一次、再观察到新增回显 → 一条真实产生的已联系证据。"""
        self.authorize()
        self.submit()
        self.marks = 36
        verified = self.dispatch('runtime.applicant.chat.outcome.verify', {})['data']
        self.assertEqual(verified['state'], ledger.STATE_OBSERVED)
        self.marks = 36
        items = self.ledger_items()
        self.assertEqual(len(items), 1)
        return items[0]

    def rearm_authorization(self):
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            doc = store.runtime_document(c, self.context['scope_id'], 'conversation_authorization')
            content = dict(doc['content'])
            content['status'] = 'ready'
            content['used'] = 0
            c.execute("UPDATE runtime_documents SET revision=revision+1, content=? "
                      "WHERE scope_id=? AND kind='conversation_authorization'",
                      (json.dumps(content, ensure_ascii=False), self.context['scope_id']))
        return content

    def other_scope(self):
        return store.runtime_scope_id('applicant', '别的企业', '另一个账号')

    # ---- 搬走了还在 -----------------------------------------------------
    def test_archive_moves_contacted_evidence_out_of_active_ledger(self):
        entry = self.observed_entry()
        result = self.archive()['data']
        self.assertEqual(result['status'], 'archived')
        self.assertEqual(result['archived'], [entry['ledger_id']])
        self.assertEqual(result['archived_count'], 1)
        self.assertEqual(result['active_count'], 0)
        self.assertEqual(result['archive_total'], 1)
        self.assertEqual(result['archived_by'], 'user')
        self.assertIs(result['sending_enabled'], False)
        self.assertEqual(self.ledger_items(), [])
        archived = self.archive_items()
        self.assertEqual([i['ledger_id'] for i in archived], [entry['ledger_id']])
        self.assertEqual(archived[0]['state'], ledger.STATE_OBSERVED)
        # 条目原样搬：不是摘要，是完整那条证据
        self.assertEqual(archived[0]['target_sha256'], entry['target_sha256'])
        self.assertEqual(archived[0]['scope_id'], entry['scope_id'])

    def test_archived_evidence_still_blocks_the_same_target(self):
        """核心不变量：归档只换文档，不换结论。"""
        entry = self.observed_entry()
        self.archive()
        self.assertEqual(self.ledger_items(), [])
        self.failed('runtime.applicant.chat.authorize',
                    {'text': TEXT, 'confirmed_by': 'user', 'action': 'greeting'},
                    'SEND_TARGET_ALREADY_CONTACTED')
        # 纵深防御那条路（绕过登记层直写授权）也必须拦
        self.rearm_authorization()
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_TARGET_ALREADY_CONTACTED')
        # 证据仍在（活跃 0 条，归档 1 条）
        self.assertEqual(self.archive_items()[0]['target_sha256'], entry['target_sha256'])

    def test_archived_evidence_still_crosses_scope(self):
        """归档在别人 scope 的文档里，也算「这个对象联系过」。"""
        self.authorize()
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.context['scope_id'],
                                         'conversation_authorization')
        content = doc['content']
        entry = ledger.new_entry(scope_id=self.other_scope(),
                                 target_sha256=content['target_sha256'],
                                 body_sha256=content['body_sha256'], action='greeting')
        entry['state'] = ledger.STATE_OBSERVED
        self.write_ledger([entry], scope=self.other_scope(), kind=ledger.ARCHIVE_KIND)
        # 当前 scope 的活跃台账为空，证据只在别的 scope 的归档里
        self.assertEqual(self.ledger_items(), [])
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_TARGET_ALREADY_CONTACTED')

    def test_union_reader_dedupes_same_entry_in_both_documents(self):
        """同一 ledger_id 同时出现在两边（半次迁移）时只算一条，且结论不变。"""
        entry = self.observed_entry()
        self.archive()
        self.write_ledger([dict(i) for i in self.archive_items()])  # 人为造重复
        self.assertEqual(len(self.ledger_items()), 1)
        self.failed('runtime.applicant.chat.authorize',
                    {'text': TEXT, 'confirmed_by': 'user', 'action': 'greeting'},
                    'SEND_TARGET_ALREADY_CONTACTED')

    # ---- 未闭环拒收 -----------------------------------------------------
    def test_in_flight_and_unresolved_are_refused_with_their_own_codes(self):
        entries = {
            'SEND_LEDGER_IN_FLIGHT': make_entry(ledger.STATE_SUBMITTING, 1),
            'SEND_LEDGER_UNRESOLVED': make_entry(ledger.STATE_UNKNOWN, 2),
            'SEND_LEDGER_NOT_ARCHIVABLE': make_entry(ledger.STATE_NOT_DELIVERED, 3),
        }
        self.write_ledger(list(entries.values()) + [make_entry(ledger.STATE_OBSERVED, 4)])
        for code, entry in entries.items():
            self.failed('runtime.applicant.chat.outcome.archive',
                        {'ledger_ids': [entry['ledger_id']], 'confirmed_by': 'user'}, code)
        # 全部被拒：一份文档都没动
        self.assertEqual(self.archive_items(), [])
        self.assertEqual(len(self.ledger_items()), 4)

    def test_conflict_and_submitted_are_also_refused(self):
        items = [make_entry(ledger.STATE_SUBMITTED, 1), make_entry(ledger.STATE_CONFLICT, 2)]
        self.write_ledger(items)
        for entry in items:
            self.failed('runtime.applicant.chat.outcome.archive',
                        {'ledger_ids': [entry['ledger_id']], 'confirmed_by': 'user'},
                        'SEND_LEDGER_UNRESOLVED')

    def test_bulk_archive_skips_unclosed_and_keeps_them_blocking(self):
        items = [make_entry(ledger.STATE_UNKNOWN, 1), make_entry(ledger.STATE_SUBMITTING, 2),
                 make_entry(ledger.STATE_OBSERVED, 3), make_entry(ledger.STATE_NOT_DELIVERED, 4)]
        self.write_ledger(items)
        result = self.archive()['data']
        self.assertEqual(result['archived'], [items[2]['ledger_id']])
        self.assertEqual(result['active_count'], 3)
        self.assertEqual([i['ledger_id'] for i in self.ledger_items()],
                         [items[0]['ledger_id'], items[1]['ledger_id'], items[4 - 1]['ledger_id']])

    # ---- 幂等 ----------------------------------------------------------
    def test_repeated_archive_of_same_target_adds_no_duplicate(self):
        entry = self.observed_entry()
        first = self.archive(target_sha256=entry['target_sha256'])['data']
        self.assertEqual((first['status'], first['archived_count']), ('archived', 1))
        second = self.archive(target_sha256=entry['target_sha256'])['data']
        self.assertEqual(second['status'], 'already_archived')
        self.assertEqual(second['archived_count'], 0)
        self.assertEqual(second['already_archived'], [entry['ledger_id']])
        third = self.archive(ledger_ids=[entry['ledger_id']])['data']
        self.assertEqual(third['status'], 'already_archived')
        self.assertEqual(len(self.archive_items()), 1)

    def test_archive_unknown_entry_is_404(self):
        self.failed('runtime.applicant.chat.outcome.archive',
                    {'ledger_ids': ['nope'], 'confirmed_by': 'user'},
                    'SEND_LEDGER_ENTRY_NOT_FOUND')

    def test_archive_requires_operator_and_valid_filters(self):
        self.failed('runtime.applicant.chat.outcome.archive', {},
                    'INVALID_ACTION_REQUEST')
        self.failed('runtime.applicant.chat.outcome.archive',
                    {'confirmed_by': '   '}, 'INVALID_ACTION_REQUEST')
        self.failed('runtime.applicant.chat.outcome.archive',
                    {'confirmed_by': 'user', 'target_sha256': 'short'},
                    'INVALID_ACTION_REQUEST')
        self.failed('runtime.applicant.chat.outcome.archive',
                    {'confirmed_by': 'user', 'ledger_ids': []}, 'INVALID_ACTION_REQUEST')
        self.failed('runtime.applicant.chat.outcome.archive',
                    {'confirmed_by': 'user', 'confirmed': True}, 'INVALID_ACTION_REQUEST')

    # ---- 归档前后阻塞不变 -----------------------------------------------
    def test_blocking_items_are_identical_before_and_after_archive(self):
        items = [make_entry(ledger.STATE_UNKNOWN, 1), make_entry(ledger.STATE_SUBMITTED, 2),
                 make_entry(ledger.STATE_OBSERVED, 3), make_entry(ledger.STATE_DELIVERED, 4)]
        self.write_ledger(items)
        before = self.dispatch('runtime.applicant.chat.outcome.get', {})['data']
        self.assertEqual(before['blockers_count'], 2)
        self.assertEqual(before['capacity']['protected'], 4)
        self.assertEqual(before['archive']['archived'], 0)

        result = self.archive()['data']
        self.assertEqual(result['archived_count'], 2)
        # 活跃文档腾出的是已联系证据，容量确实降了
        self.assertEqual(result['capacity']['protected'], 2)

        after = self.dispatch('runtime.applicant.chat.outcome.get', {})['data']
        self.assertEqual(after['blockers_count'], before['blockers_count'])
        self.assertEqual([i['ledger_id'] for i in after['blocking']],
                         [i['ledger_id'] for i in before['blocking']])
        self.assertEqual(after['capacity']['protected'], 2)
        self.assertEqual(after['archive']['archived'], 2)
        # 未闭环仍然拦得住整个 scope（沙箱层）
        self.rearm_authorization()
        self.failed('runtime.applicant.chat.submit', {'point': dict(POINT)},
                    'SEND_OUTCOME_UNRESOLVED')

    # ---- 回执与恢复 -----------------------------------------------------
    def row(self, task_id):
        with store.runtime_db() as c:
            return dict(c.execute('SELECT * FROM runtime_tasks WHERE task_id=?',
                                  (task_id,)).fetchone())

    def interrupt(self, task_id):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL,error_code=NULL "
                      "WHERE task_id=?", (task_id,))

    def test_archive_is_receipted_and_recovers_without_replay(self):
        entry = self.observed_entry()
        done = self.archive()
        task_id = done['trace_id']
        self.assertIsNotNone(self.row(task_id)['commit_receipt'])
        self.interrupt(task_id)
        from dataclasses import replace
        tool = __import__('main')._router._tools['runtime.applicant.chat.outcome.archive']
        with mock.patch.dict(__import__('main')._router._tools,
                             {tool.name: replace(tool, handler=mock.Mock(
                                 side_effect=AssertionError('no replay')))}):
            recovered = self.dispatch('runtime.task.recover', {'task_id': task_id})['data']
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(recovered['error_code'], 'RECOVERED_COMMIT')
        self.assertEqual(recovered['result'], done['data'])
        # 恢复不重放、也不重复写：归档里仍然只有那一条
        self.assertEqual([i['ledger_id'] for i in self.archive_items()], [entry['ledger_id']])

    def test_tampered_archive_receipt_is_refused(self):
        """回执 JSON 能解析不等于事实：结果必须与库里的两个文档对得上。"""
        self.observed_entry()
        done = self.archive()
        task_id = done['trace_id']
        row = self.row(task_id)
        receipt = json.loads(row['commit_receipt'])
        receipt['result']['archived'] = ['0' * 20]      # 声称搬走了一条并不存在的记录
        with store.runtime_db() as c:
            c.execute('UPDATE runtime_tasks SET commit_receipt=? WHERE task_id=?',
                      (json.dumps(receipt, ensure_ascii=False), task_id))
        self.interrupt(task_id)
        recovered = self.dispatch('runtime.task.recover', {'task_id': task_id})['data']
        self.assertEqual(recovered['status'], 'outcome_unknown')
        self.assertEqual(recovered['error_code'], 'INVALID_COMMIT_RECEIPT')

    # ---- 工具注册 -------------------------------------------------------
    def test_tool_registered_as_receipted_state_control(self):
        import execution_engine
        import main
        tool = main._router.resolve('runtime.applicant.chat.outcome.archive')
        self.assertEqual((tool.kind, tool.effect), ('control', 'state'))
        self.assertIs(tool.maintenance_only, False)
        self.assertIn('runtime.applicant.chat.outcome.archive',
                      execution_engine.RECEIPTED_CONTROLS)

    def test_outcome_get_reports_archive_separately_from_capacity(self):
        self.observed_entry()
        self.archive()
        result = self.dispatch('runtime.applicant.chat.outcome.get', {})['data']
        # 容量告警只看活跃文档（5000 上限管的就是它）
        self.assertEqual(result['capacity']['total'], 0)
        self.assertEqual(result['capacity']['overflow'], False)
        # 归档条数单独可见，总数仍如实
        self.assertEqual(result['archive'], {'archived': 1, 'contacted': 1,
                                             'targets': 1, 'blocking': 0})
        self.assertEqual(result['summary']['total'], 1)
        self.assertEqual(result['summary']['contacted'], 1)


if __name__ == '__main__':
    unittest.main()
