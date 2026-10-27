"""G4 纯逻辑层测试：额度、去重、跨日、时区、平台限制（不触网、不发内容）。"""
import unittest

import applicant_action as action
import applicant_greeting as G

ACCOUNT = 'acct-1'
DAY = '2026-10-27'


def rec(job_id, state, day=DAY, account=ACCOUNT, ts=None):
    return {'account_id': account, 'job_id': job_id, 'company': '某公司',
            'state': state, 'day': day, 'ts': ts or 0.0}


def cand(job_id, **over):
    base = {'job_id': job_id, 'company': '某公司', 'job_open': True, 'page_restricted': False}
    base.update(over)
    return base


class TimezoneTest(unittest.TestCase):
    def test_day_starts_at_utc_16(self):
        # UTC 2026-10-26 15:59 = 上海 10-26 23:59
        self.assertEqual(G.day_key(1793030340 - 60), '2026-10-26')
        # UTC 2026-10-26 16:00 = 上海 10-27 00:00
        self.assertEqual(G.day_key(1793030400), '2026-10-27')

    def test_day_key_is_stable(self):
        self.assertEqual(G.day_key(1793100000), G.day_key(1793100000))


class AttemptTest(unittest.TestCase):
    def test_attempt_rejects_unknown_state(self):
        with self.assertRaises(ValueError):
            G.attempt(account_id=ACCOUNT, job_id='j1', company='x', state='sent_ok')

    def test_attempt_requires_account_and_job(self):
        with self.assertRaises(ValueError):
            G.attempt(account_id='', job_id='j1', company='x', state='observed_reply')
        with self.assertRaises(ValueError):
            G.attempt(account_id=ACCOUNT, job_id=' ', company='x', state='observed_reply')

    def test_attempt_carries_shanghai_day(self):
        a = G.attempt(account_id=ACCOUNT, job_id='j1', company='x', state='submitted_unverified',
                      ts=1793030400)
        self.assertEqual(a['day'], '2026-10-27')


class UsageTest(unittest.TestCase):
    def test_only_budget_states_count(self):
        records = [rec('j1', 'not_executed'), rec('j2', 'submitted_unverified'),
                   rec('j3', 'observed_reply'), rec('j4', 'outcome_unknown')]
        self.assertEqual(G.usage(records, day=DAY, account_id=ACCOUNT), {'used': 3, 'unresolved': 2})

    def test_other_account_and_day_are_ignored(self):
        records = [rec('j1', 'observed_reply', account='acct-2'),
                   rec('j2', 'observed_reply', day='2026-10-26')]
        self.assertEqual(G.usage(records, day=DAY, account_id=ACCOUNT)['used'], 0)

    def test_conflict_counts_as_unresolved(self):
        self.assertEqual(G.usage([rec('j1', 'conflict')], day=DAY, account_id=ACCOUNT),
                         {'used': 1, 'unresolved': 1})


class LimitTest(unittest.TestCase):
    def test_platform_limit_wins_when_stricter(self):
        self.assertEqual(G.effective_limit(150, 20), 20)
        self.assertEqual(G.effective_limit(20, 150), 20)

    def test_missing_platform_limit_uses_configured(self):
        self.assertEqual(G.effective_limit(150, None), 150)

    def test_invalid_values_fall_back_safely(self):
        self.assertEqual(G.effective_limit(0, None), 0)
        self.assertEqual(G.effective_limit(-5, 'x'), 0)


class ReservedQuotaTest(unittest.TestCase):
    """执行前占位：并发下必须靠占位而不是「读一次 used」来限额。"""

    def test_reserved_counts_toward_budget(self):
        records = [rec('j1', G.RESERVED_STATE)]
        self.assertEqual(G.usage(records, day=DAY, account_id=ACCOUNT),
                         {'used': 1, 'unresolved': 0})

    def test_reserved_blocks_same_job(self):
        records = [rec('j1', G.RESERVED_STATE)]
        self.assertEqual(G.decide(cand('j1'), records=records, day=DAY,
                                  account_id=ACCOUNT)['reason'], 'UNRESOLVED_ATTEMPT')

    def test_reserved_consumes_remaining(self):
        records = [rec('a'.join(['j', str(i)]), G.RESERVED_STATE) for i in range(2)]
        result = G.plan([cand('x')], records=records, day=DAY, account_id=ACCOUNT,
                        configured=3)
        self.assertEqual(result['proceed'], ['x'])
        self.assertEqual(result['remaining'], 0)


class PlatformLimitExpiryTest(unittest.TestCase):
    def test_expired_platform_limit_is_ignored(self):
        self.assertEqual(G.effective_limit(150, 20, platform_expires_at=100, now=200), 150)

    def test_fresh_platform_limit_applies(self):
        self.assertEqual(G.effective_limit(150, 20, platform_expires_at=300, now=200), 20)

    def test_broken_expiry_falls_back_to_configured(self):
        self.assertEqual(G.effective_limit(150, 20, platform_expires_at='soon', now=200), 150)


class NeedsReviewTest(unittest.TestCase):
    def test_unresolved_items_are_listed_for_humans(self):
        records = [rec('j1', 'outcome_unknown'), rec('j2', 'observed_reply')]
        result = G.plan([cand('j3')], records=records, day=DAY, account_id=ACCOUNT)
        ids = [item['job_id'] for item in result['needs_review']]
        self.assertEqual(ids, ['j1'])
        self.assertEqual(result['needs_review'][0]['state'], 'outcome_unknown')

    def test_clean_history_has_empty_review_list(self):
        result = G.plan([cand('j3')], records=[], day=DAY, account_id=ACCOUNT)
        self.assertEqual(result['needs_review'], [])


class DecideTest(unittest.TestCase):
    def test_fresh_candidate_proceeds(self):
        self.assertEqual(G.decide(cand('j1'), records=[], day=DAY, account_id=ACCOUNT)['reason'],
                         'PROCEED')

    def test_contacted_is_permanent_across_days(self):
        records = [rec('j1', 'observed_reply', day='2026-01-01')]
        self.assertEqual(G.decide(cand('j1'), records=records, day=DAY, account_id=ACCOUNT)['reason'],
                         'ALREADY_CONTACTED')

    def test_unresolved_today_blocks_retry(self):
        records = [rec('j1', 'outcome_unknown')]
        self.assertEqual(G.decide(cand('j1'), records=records, day=DAY, account_id=ACCOUNT)['reason'],
                         'UNRESOLVED_ATTEMPT')

    def test_unresolved_yesterday_still_blocks(self):
        records = [rec('j1', 'outcome_unknown', day='2026-10-26')]
        self.assertEqual(G.decide(cand('j1'), records=records, day=DAY, account_id=ACCOUNT)['reason'],
                         'UNRESOLVED_ATTEMPT')

    def test_closed_job_is_skipped(self):
        self.assertEqual(G.decide(cand('j1', job_open=False), records=[], day=DAY,
                                  account_id=ACCOUNT)['reason'], 'JOB_CLOSED')

    def test_page_restriction_stops_everything(self):
        self.assertEqual(G.decide(cand('j1', page_restricted=True), records=[], day=DAY,
                                  account_id=ACCOUNT)['reason'], 'PAGE_RESTRICTED')

    def test_plan_change_is_flagged(self):
        self.assertEqual(G.decide(cand('j1', plan_revision=1), records=[], day=DAY,
                                  account_id=ACCOUNT, plan_revision=2)['reason'], 'PLAN_CHANGED')

    def test_daily_limit_blocks(self):
        records = [rec('j%d' % i, 'observed_reply') for i in range(3)]
        self.assertEqual(G.decide(cand('new'), records=records, day=DAY, account_id=ACCOUNT,
                                  configured=3)['reason'], 'DAILY_LIMIT')

    def test_duplicate_in_batch(self):
        taken = {'j1'}
        self.assertEqual(G.decide(cand('j1'), records=[], day=DAY, account_id=ACCOUNT,
                                  taken=taken)['reason'], 'DUPLICATE_IN_BATCH')

    def test_missing_job_id_is_not_proceed(self):
        self.assertNotEqual(G.decide({}, records=[], day=DAY, account_id=ACCOUNT)['reason'],
                            'PROCEED')


class PlanTest(unittest.TestCase):
    def test_batch_stops_at_limit(self):
        candidates = [cand('j%d' % i) for i in range(10)]
        result = G.plan(candidates, records=[], day=DAY, account_id=ACCOUNT, configured=4)
        self.assertEqual(result['proceed'], ['j0', 'j1', 'j2', 'j3'])
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(result['stop_reason'], 'DAILY_LIMIT')
        self.assertFalse(result['sending_enabled'])
        self.assertFalse(result['delivery_confirmed'])

    def test_platform_limit_cannot_be_exceeded(self):
        candidates = [cand('j%d' % i) for i in range(30)]
        result = G.plan(candidates, records=[], day=DAY, account_id=ACCOUNT,
                        configured=150, platform_limit=5)
        self.assertEqual(len(result['proceed']), 5)
        self.assertEqual(result['limit'], 5)

    def test_existing_usage_reduces_remaining(self):
        records = [rec('a%d' % i, 'observed_reply') for i in range(148)]
        result = G.plan([cand('j1'), cand('j2'), cand('j3')], records=records, day=DAY,
                        account_id=ACCOUNT)
        self.assertEqual(result['proceed'], ['j1', 'j2'])
        self.assertEqual(result['stop_reason'], 'DAILY_LIMIT')

    def test_restriction_breaks_the_batch(self):
        candidates = [cand('j1'), cand('j2', page_restricted=True), cand('j3')]
        result = G.plan(candidates, records=[], day=DAY, account_id=ACCOUNT)
        self.assertEqual(result['proceed'], ['j1'])
        self.assertEqual(result['stop_reason'], 'PAGE_RESTRICTED')
        self.assertEqual([d['job_id'] for d in result['decisions']], ['j1', 'j2'])

    def test_duplicates_in_batch_keep_first(self):
        result = G.plan([cand('j1'), cand('j1')], records=[], day=DAY, account_id=ACCOUNT)
        self.assertEqual(result['proceed'], ['j1'])
        self.assertEqual(result['decisions'][1]['reason'], 'DUPLICATE_IN_BATCH')

    def test_quota_is_per_account_not_per_scope(self):
        # 同一账号在别的 scope 已经用掉的额度，这里照样扣
        records = [rec('a%d' % i, 'observed_reply') for i in range(149)]
        result = G.plan([cand('j1')], records=records, day=DAY, account_id=ACCOUNT,
                        configured=150)
        self.assertEqual(result['proceed'], ['j1'])
        self.assertEqual(result['remaining'], 0)
        # 另一个账号不受影响
        other = G.plan([cand('j1')], records=records, day=DAY, account_id='acct-2',
                       configured=150)
        self.assertEqual(other['used'], 0)
        self.assertEqual(len(other['proceed']), 1)

    def test_plan_is_deterministic(self):
        a = G.plan([cand('j1'), cand('j2')], records=[], day=DAY, account_id=ACCOUNT)
        b = G.plan([cand('j1'), cand('j2')], records=[], day=DAY, account_id=ACCOUNT)
        self.assertEqual(a['proceed'], b['proceed'])
        self.assertEqual(a['decisions'], b['decisions'])

    def test_every_reason_is_declared(self):
        for reason in ('PROCEED', 'ALREADY_CONTACTED', 'UNRESOLVED_ATTEMPT', 'JOB_CLOSED',
                       'PAGE_RESTRICTED', 'DAILY_LIMIT', 'DUPLICATE_IN_BATCH', 'PLAN_CHANGED'):
            self.assertIn(reason, G.REASONS)
            self.assertIn(reason, G.MESSAGES)


if __name__ == '__main__':
    unittest.main()
