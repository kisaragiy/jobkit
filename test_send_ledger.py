"""发送台账纯逻辑测试。

覆盖的核心是那条真实事故：点过发送但没核对，系统又允许同一对象再发一次。
"""
import json
import time
import unittest

import applicant_send_ledger as ledger

T1 = 'a' * 64
T2 = 'b' * 64
B1 = 'c' * 64
B2 = 'd' * 64
SCOPE = 'scope-applicant'
TS = 1793104188.0


def entry(target=T1, body=B1, ts=TS, scope=SCOPE):
    return ledger.new_entry(scope_id=scope, target_sha256=target, body_sha256=body,
                            action='greeting', ts=ts)


def target(n):
    """第 n 个不同对象（64 位十六进制指纹）。"""
    return '%064x' % n


def not_delivered(target_sha=T1, body=B1, ts=TS, scope=SCOPE):
    """人工确认没发出去的流水：唯一可被容量淘汰的状态。"""
    e = entry(target=target_sha, body=body, ts=ts, scope=scope)
    return ledger.resolve(ledger.close(e, ledger.STATE_UNKNOWN, ts=ts + 1),
                          'not_delivered', by='user', ts=ts + 2)


def make_entry(state, n, scope=SCOPE):
    """按目标状态造一条台账记录，n 只用来区分对象与时间。"""
    ts = TS + n
    e = entry(target=target(n), ts=ts, scope=scope)
    if state == ledger.STATE_SUBMITTING:
        return e
    if state == ledger.STATE_DELIVERED:
        return ledger.resolve(ledger.close(e, ledger.STATE_UNKNOWN, ts=ts + 1),
                              'delivered', by='user', ts=ts + 2)
    if state == ledger.STATE_NOT_DELIVERED:
        return ledger.resolve(ledger.close(e, ledger.STATE_UNKNOWN, ts=ts + 1),
                              'not_delivered', by='user', ts=ts + 2)
    return ledger.close(e, state, ts=ts + 1)


def mixed_600():
    """600 条混合台账：450 条保护集 + 150 条可淘汰流水。"""
    plan = [(ledger.STATE_SUBMITTING, 50), (ledger.STATE_SUBMITTED, 40),
            (ledger.STATE_OBSERVED, 150), (ledger.STATE_UNKNOWN, 120),
            (ledger.STATE_CONFLICT, 40), (ledger.STATE_DELIVERED, 50),
            (ledger.STATE_NOT_DELIVERED, 150)]
    items, n = [], 0
    for state, count in plan:
        for _ in range(count):
            n += 1
            items.append(make_entry(state, n))
    return items


class NewEntryTests(unittest.TestCase):
    def test_new_entry_is_submitting_and_blocking(self):
        e = entry()
        self.assertEqual(e['state'], ledger.STATE_SUBMITTING)
        self.assertTrue(ledger.is_blocking(e))
        self.assertFalse(ledger.is_contacted(e))

    def test_day_uses_shanghai_boundary(self):
        # 直接构造上海日界两侧：23:59:50 与次日 00:01:10
        midnight = (TS + 8 * 3600) // 86400 * 86400 - 8 * 3600
        e1 = entry(ts=midnight + 86390)
        e2 = entry(ts=midnight + 86410)
        self.assertEqual(ledger.day_key(e1['ts']), '2026-10-27')
        self.assertNotEqual(e1['day'], e2['day'])

    def test_bad_input_rejected(self):
        with self.assertRaises(ValueError):
            ledger.new_entry(scope_id='', target_sha256=T1, body_sha256=B1, action='greeting')
        with self.assertRaises(ValueError):
            ledger.new_entry(scope_id=SCOPE, target_sha256='short', body_sha256=B1, action='greeting')
        with self.assertRaises(ValueError):
            ledger.new_entry(scope_id=SCOPE, target_sha256=T1, body_sha256=B1, action='send')


class BlockingTests(unittest.TestCase):
    def test_all_unclosed_states_block(self):
        for state in ledger.BLOCKING_STATES:
            e = dict(entry(), state=state)
            self.assertTrue(ledger.is_blocking(e), state)

    def test_closed_states_do_not_block(self):
        for state in (ledger.STATE_OBSERVED, ledger.STATE_NOT_DELIVERED, ledger.STATE_DELIVERED):
            e = dict(entry(), state=state)
            self.assertFalse(ledger.is_blocking(e), state)

    def test_blocking_is_not_relieved_by_next_day(self):
        """昨天的 outcome_unknown 今天照样拦 —— 方案要求跨日不自动放行。"""
        yesterday = entry(ts=TS)
        unknown = ledger.close(yesterday, ledger.STATE_UNKNOWN, ts=TS + 60)
        today = TS + 24 * 3600
        self.assertNotEqual(unknown['day'], ledger.day_key(today))
        self.assertEqual(len(ledger.blocking_items([unknown])), 1)

    def test_blocking_items_filters_scope(self):
        mine = ledger.close(entry(), ledger.STATE_UNKNOWN, ts=TS + 1)
        other = ledger.close(entry(scope='other'), ledger.STATE_UNKNOWN, ts=TS + 1)
        self.assertEqual(len(ledger.blocking_items([mine, other], scope_id=SCOPE)), 1)


class TargetEvidenceTests(unittest.TestCase):
    def test_no_evidence(self):
        self.assertEqual(ledger.target_evidence([entry()], T2), (None, None))

    def test_unresolved_evidence(self):
        e = ledger.close(entry(), ledger.STATE_UNKNOWN, ts=TS + 1)
        kind, found = ledger.target_evidence([e], T1)
        self.assertEqual(kind, 'unresolved')
        self.assertEqual(found['ledger_id'], e['ledger_id'])

    def test_contacted_beats_unresolved(self):
        old_unknown = ledger.close(entry(body=B1, ts=TS), ledger.STATE_UNKNOWN, ts=TS + 1)
        observed = ledger.close(entry(body=B2, ts=TS + 10), ledger.STATE_OBSERVED, ts=TS + 20)
        kind, found = ledger.target_evidence([old_unknown, observed], T1)
        self.assertEqual(kind, 'contacted')
        self.assertEqual(found['ledger_id'], observed['ledger_id'])

    def test_identity_is_target_not_body(self):
        """同一对象换了正文，仍然算同一个对象 —— 重复招呼不能靠改文案绕过。"""
        sent = ledger.close(entry(body=B1), ledger.STATE_OBSERVED, ts=TS + 1)
        kind, _ = ledger.target_evidence([sent], T1)
        self.assertEqual(kind, 'contacted')


class ObservedStateTests(unittest.TestCase):
    def test_new_mark_observed(self):
        state, why = ledger.observed_state(before_marks=34, after_marks=36)
        self.assertEqual((state, why), (ledger.STATE_OBSERVED, 'new_mark_observed'))

    def test_no_new_mark_stays_unverified(self):
        state, why = ledger.observed_state(before_marks=34, after_marks=34)
        self.assertEqual((state, why), (ledger.STATE_SUBMITTED, 'no_new_mark'))

    def test_read_error_is_unknown_not_success(self):
        state, why = ledger.observed_state(error=True)
        self.assertEqual((state, why), (ledger.STATE_UNKNOWN, 'observation_failed'))

    def test_missing_baseline_is_not_upgraded(self):
        state, why = ledger.observed_state(before_marks=None, after_marks=40)
        self.assertEqual((state, why), (ledger.STATE_SUBMITTED, 'no_baseline'))

    def test_backwards_marks_is_conflict(self):
        state, why = ledger.observed_state(before_marks=40, after_marks=34)
        self.assertEqual((state, why), (ledger.STATE_CONFLICT, 'marks_went_backwards'))


class CloseTests(unittest.TestCase):
    def test_close_requires_by_for_human_resolution(self):
        with self.assertRaises(ValueError):
            ledger.close(entry(), ledger.STATE_DELIVERED, by='')
        with self.assertRaises(ValueError):
            ledger.close(entry(), ledger.STATE_NOT_DELIVERED)

    def test_close_automatic_state_needs_no_by(self):
        e = ledger.close(entry(), ledger.STATE_UNKNOWN, ts=TS + 1)
        self.assertEqual(e['state'], ledger.STATE_UNKNOWN)
        self.assertIsNone(e['resolved_by'])

    def test_close_refuses_already_closed(self):
        closed = ledger.close(entry(), ledger.STATE_OBSERVED, ts=TS + 1)
        with self.assertRaises(ValueError):
            ledger.close(closed, ledger.STATE_UNKNOWN, ts=TS + 2)

    def test_close_refuses_back_to_submitting(self):
        with self.assertRaises(ValueError):
            ledger.close(entry(), ledger.STATE_SUBMITTING)

    def test_close_does_not_mutate_input(self):
        e = entry()
        ledger.close(e, ledger.STATE_UNKNOWN, ts=TS + 1)
        self.assertEqual(e['state'], ledger.STATE_SUBMITTING)


class ResolveTests(unittest.TestCase):
    def test_resolutions_map_to_states(self):
        base = ledger.close(entry(), ledger.STATE_UNKNOWN, ts=TS + 1)
        cases = {'delivered': ledger.STATE_DELIVERED,
                 'not_delivered': ledger.STATE_NOT_DELIVERED,
                 'duplicate_confirmed': ledger.STATE_DELIVERED}
        for resolution, expected in cases.items():
            e = dict(base, state=ledger.STATE_UNKNOWN, resolved_at=None)
            out = ledger.resolve(e, resolution, by='user', ts=TS + 30)
            self.assertEqual(out['state'], expected, resolution)
            self.assertEqual(out['resolved_by'], 'user')
            self.assertIn(resolution, out['resolution'])

    def test_resolve_rejects_unknown_resolution(self):
        base = ledger.close(entry(), ledger.STATE_UNKNOWN, ts=TS + 1)
        with self.assertRaises(ValueError):
            ledger.resolve(base, 'probably_fine', by='user')

    def test_resolve_requires_operator(self):
        base = ledger.close(entry(), ledger.STATE_UNKNOWN, ts=TS + 1)
        with self.assertRaises(ValueError):
            ledger.resolve(base, 'not_delivered', by='   ')

    def test_not_delivered_unlocks_the_target(self):
        """人工核对确认没发出去，这个对象必须重新可用，否则系统永久锁死。"""
        e = ledger.close(entry(), ledger.STATE_UNKNOWN, ts=TS + 1)
        resolved = ledger.resolve(e, 'not_delivered', by='user', ts=TS + 30)
        self.assertFalse(ledger.is_blocking(resolved))
        self.assertEqual(ledger.blocking_items([resolved]), [])
        self.assertEqual(ledger.target_evidence([resolved], T1), (None, None))


class MergeTests(unittest.TestCase):
    def test_merge_replaces_same_id(self):
        e = entry()
        closed = ledger.close(e, ledger.STATE_UNKNOWN, ts=TS + 1)
        merged = ledger.merge([e], closed)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['state'], ledger.STATE_UNKNOWN)

    def test_merge_appends_new(self):
        a = entry(ts=TS)
        b = entry(ts=TS + 5)
        merged = ledger.merge([a], b)
        self.assertEqual([i['ledger_id'] for i in merged], [a['ledger_id'], b['ledger_id']])

    def test_merge_caps_length(self):
        """容量上限只作用于可淘汰的流水；旧实现连阻塞证据一起截断，等于静默放行。"""
        items = [make_entry(ledger.STATE_NOT_DELIVERED, i) for i in range(1, 6)]
        merged = ledger.merge(items, make_entry(ledger.STATE_NOT_DELIVERED, 99), limit=3)
        self.assertEqual(len(merged), 3)
        self.assertTrue(all(not ledger.is_protected(i) for i in merged))
        # 留下的是最新的三条（99 最新，其次是 5、4）
        self.assertEqual([i['ts'] for i in merged], [TS + 4, TS + 5, TS + 99])
        self.assertEqual(merged.evicted_count, 3)

    def test_merge_does_not_mutate_input(self):
        e = entry()
        closed = ledger.close(e, ledger.STATE_UNKNOWN, ts=TS + 1)
        items = [e]
        ledger.merge(items, closed)
        self.assertEqual(items[0]['state'], ledger.STATE_SUBMITTING)


class RealIncidentTests(unittest.TestCase):
    """复现 2026-10-27 20:29 那次：outcome_unknown 后 17 秒又发了一次同一对象。"""

    def test_second_send_is_refused_after_unknown(self):
        first = ledger.new_entry(scope_id=SCOPE, target_sha256=T1, body_sha256=B1,
                                 action='greeting', ts=1793104171.87)
        items = ledger.merge([], first)
        # 页面读取超时 → engine 记 outcome_unknown
        items = ledger.merge(items, ledger.close(first, ledger.STATE_UNKNOWN, ts=1793104171.88))
        # 17 秒后同一对象再来：必须被拦
        kind, found = ledger.target_evidence(items, T1)
        self.assertEqual(kind, 'unresolved')
        self.assertEqual(found['state'], ledger.STATE_UNKNOWN)
        self.assertEqual(len(ledger.blocking_items(items, scope_id=SCOPE)), 1)

    def test_after_human_check_same_target_still_refused_if_delivered(self):
        first = ledger.new_entry(scope_id=SCOPE, target_sha256=T1, body_sha256=B1,
                                 action='greeting', ts=1793104171.87)
        unknown = ledger.close(first, ledger.STATE_UNKNOWN, ts=1793104172.0)
        resolved = ledger.resolve(unknown, 'delivered', by='user',
                                  note='对方确认收到', ts=1793104300.0)
        items = ledger.merge([], resolved)
        kind, _ = ledger.target_evidence(items, T1)
        self.assertEqual(kind, 'contacted')
        self.assertEqual(ledger.blocking_items(items), [])


class CapacityPolicyTests(unittest.TestCase):
    """证据 / 流水分离：保护集永不因容量淘汰，超限要显式报警。"""

    def test_state_partition(self):
        for state in (ledger.STATE_SUBMITTING, ledger.STATE_SUBMITTED, ledger.STATE_OBSERVED,
                      ledger.STATE_UNKNOWN, ledger.STATE_CONFLICT, ledger.STATE_DELIVERED):
            self.assertTrue(ledger.is_protected(dict(entry(), state=state)), state)
        self.assertFalse(ledger.is_protected(dict(entry(), state=ledger.STATE_NOT_DELIVERED)))

    def test_unknown_state_is_protected_not_evictable(self):
        """缺 state / 未来新状态 / 脏数据都按保护处理（fail-closed），绝不靠容量删掉。"""
        self.assertTrue(ledger.is_protected({'ledger_id': 'x'}))
        self.assertTrue(ledger.is_protected({'ledger_id': 'x', 'state': 'some_future_state'}))
        self.assertTrue(ledger.is_protected('not-a-dict'))
        plain = [{'ledger_id': 'x', 'state': 'some_future_state'},
                 make_entry(ledger.STATE_NOT_DELIVERED, 1)]
        merged = ledger.merge(plain, make_entry(ledger.STATE_NOT_DELIVERED, 2), limit=0)
        self.assertEqual([i['ledger_id'] for i in merged], ['x'])
        self.assertEqual(merged.evicted_count, 2)

    def test_mixed_600_keeps_every_protected_entry(self):
        items = mixed_600()
        protected = {i['ledger_id'] for i in items if ledger.is_protected(i)}
        evictable = {i['ledger_id'] for i in items if not ledger.is_protected(i)}
        self.assertEqual((len(protected), len(evictable)), (450, 150))
        kept = []
        for item in items:                      # 模拟 runtime 逐条 merge
            kept = ledger.merge(kept, item)
        self.assertIsInstance(kept, list)
        self.assertEqual(len(kept), 600)
        self.assertTrue(protected <= {i['ledger_id'] for i in kept})
        self.assertFalse(kept.overflow)
        self.assertEqual(kept.evicted_count, 0)

    def test_not_delivered_is_bounded_while_evidence_grows(self):
        items = mixed_600()
        protected = {i['ledger_id'] for i in items if ledger.is_protected(i)}
        kept = []
        for item in items:
            kept = ledger.merge(kept, item)
        for n in range(1000, 1201):             # 再灌 200 条流水
            kept = ledger.merge(kept, make_entry(ledger.STATE_NOT_DELIVERED, n), limit=100)
        ids = {i['ledger_id'] for i in kept}
        self.assertEqual(len([i for i in kept if ledger.is_protected(i)]), 450)
        self.assertTrue(protected <= ids)
        self.assertEqual(len([i for i in kept if not ledger.is_protected(i)]), 100)
        self.assertEqual(len(kept), 550)
        self.assertGreater(kept.evicted_count, 0)

    def test_contacted_evidence_survives_four_days_of_150_greetings(self):
        """复现旧缺陷：150 招呼/日，第 4 天把第 1 天的 observed_reply 挤掉 → 闸门失效。"""
        day1 = []
        for n in range(1, 151):
            state = ledger.STATE_OBSERVED if n <= 3 else ledger.STATE_NOT_DELIVERED
            day1.append(make_entry(state, n))
        first_contacted = day1[0]
        kept = []
        for item in day1 + [make_entry(ledger.STATE_NOT_DELIVERED, n) for n in range(151, 601)]:
            kept = ledger.merge(kept, item)     # 默认上限 500，四条 150
        self.assertEqual(len([i for i in kept if not ledger.is_protected(i)]), 500)
        kind, found = ledger.target_evidence(kept, first_contacted['target_sha256'])
        self.assertEqual(kind, 'contacted')
        self.assertEqual(found['ledger_id'], first_contacted['ledger_id'])

    def test_protected_overflow_flags_without_dropping(self):
        items = [make_entry(ledger.STATE_UNKNOWN, n) for n in range(1, 21)]
        kept = items[:15]
        for item in items[15:]:
            kept = ledger.merge(kept, item, protected_limit=15)
        self.assertEqual(len(kept), 20)                     # 一条都没丢
        self.assertTrue(kept.overflow)
        self.assertEqual(kept.overflow_count, 5)
        self.assertEqual({i['ledger_id'] for i in kept}, {i['ledger_id'] for i in items})
        self.assertEqual(len(ledger.blocking_items(kept)), 20)
        report = kept.report()
        self.assertTrue(report['overflow'])
        self.assertEqual(report['overflow_count'], 5)
        self.assertEqual(report['protected'], 20)
        self.assertIsNotNone(report['alarm'])
        json.dumps(report)                                   # 可直接落文档 meta

    def test_overflow_does_not_block_normal_capacity_of_flows(self):
        """保护集没超限时 overflow 必须是 False，别让上游一直报警。"""
        kept = ledger.merge([make_entry(ledger.STATE_UNKNOWN, 1)], make_entry(ledger.STATE_UNKNOWN, 2))
        self.assertFalse(kept.overflow)
        self.assertEqual(kept.overflow_count, 0)

    def test_merge_is_idempotent(self):
        items = mixed_600()
        once = []
        for item in items:
            once = ledger.merge(once, item)
        twice = list(once)
        for item in items:                                   # 同一批重复写入
            twice = ledger.merge(twice, item)
        self.assertEqual(len(twice), 600)
        self.assertEqual([i['ledger_id'] for i in twice], [i['ledger_id'] for i in once])
        self.assertEqual(len({i['ledger_id'] for i in twice}), 600)
        # 重复写同一条不会重复计入 overflow
        overloaded = [make_entry(ledger.STATE_UNKNOWN, n) for n in range(1, 6)]
        merged = overloaded[:4]
        for item in overloaded[3:]:
            merged = ledger.merge(merged, item, protected_limit=2)
        self.assertEqual(merged.overflow_count, 3)
        merged = ledger.merge(merged, overloaded[-1], protected_limit=2)
        self.assertEqual((len(merged), merged.overflow_count), (5, 3))

    def test_blocking_items_complete_after_mass_eviction(self):
        items = mixed_600()
        kept = []
        for item in items:
            kept = ledger.merge(kept, item)
        before = {i['ledger_id'] for i in ledger.blocking_items(kept, scope_id=SCOPE)}
        self.assertEqual(len(before), 250)
        for round_no in range(10):                           # 再发 1000 条流水
            for n in range(round_no * 100, (round_no + 1) * 100):
                kept = ledger.merge(kept, make_entry(ledger.STATE_NOT_DELIVERED, 5000 + n),
                                    limit=50)
        after = {i['ledger_id'] for i in ledger.blocking_items(kept, scope_id=SCOPE)}
        self.assertEqual(after, before)                      # 未闭环项一条不少
        self.assertEqual(len([i for i in kept if not ledger.is_protected(i)]), 50)

    def test_evicted_items_are_returned_for_audit(self):
        items = [make_entry(ledger.STATE_NOT_DELIVERED, n) for n in range(1, 6)]
        merged = ledger.merge(items, make_entry(ledger.STATE_NOT_DELIVERED, 99), limit=2)
        self.assertEqual(merged.evicted_count, 4)
        self.assertEqual({i['ledger_id'] for i in merged.evicted},
                         {i['ledger_id'] for i in items[:4]})
        self.assertTrue(all(not ledger.is_protected(i) for i in merged.evicted))

    def test_capacity_is_read_only_snapshot(self):
        items = mixed_600()
        snapshot = ledger.capacity(items)
        self.assertEqual((snapshot['protected'], snapshot['evictable']), (450, 150))
        self.assertEqual(snapshot['blocking'], 250)
        self.assertEqual(snapshot['contacted'], 200)
        self.assertFalse(snapshot['overflow'])
        alarm = ledger.capacity(items, protected_limit=100)
        self.assertTrue(alarm['overflow'])
        self.assertEqual(alarm['overflow_count'], 350)
        self.assertEqual(len(items), 600)

    def test_merge_result_is_still_a_plain_list(self):
        merged = ledger.merge([], entry())
        self.assertIsInstance(merged, list)
        self.assertEqual(merged, [entry()])
        self.assertEqual(json.dumps(list(merged))[:1], '[')


class MergeResultCompatTests(unittest.TestCase):
    """老调用方只认 list：切片 / 迭代 / 长度语义不能变。"""

    def test_slicing_and_iteration(self):
        merged = ledger.merge([entry(ts=TS)], entry(ts=TS + 1))
        self.assertEqual([i['ts'] for i in merged[-1:]], [TS + 1])
        self.assertEqual(len(list(merged)), 2)
        self.assertEqual(merged[0]['ts'], TS)


if __name__ == '__main__':
    unittest.main()
