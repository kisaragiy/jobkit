"""G3 授权与六态协议的单元测试（纯逻辑，不触网、不开浏览器）。"""
import unittest

import applicant_action as action


class AuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.target = action.target_key(job_id='j-1', company='某公司')
        self.base = dict(
            scope_id='scope-a', action='greeting.submit', account_evidence='display:张三|mid:abc',
            target=self.target, profile_revision=3, plan_revision=7, now=1000,
        )

    def test_authorization_is_single_use(self):
        doc = action.authorization(**self.base)
        self.assertEqual(doc['max_uses'], 1)
        self.assertEqual(doc['used'], 0)
        self.assertEqual(doc['status'], 'ready')
        self.assertIsNone(action.check(doc, action='greeting.submit', target=self.target,
                                       profile_revision=3, plan_revision=7, now=1001))
        used = action.consume(doc, now=1002)
        self.assertEqual(used['used'], 1)
        self.assertEqual(used['status'], 'exhausted')
        self.assertEqual(action.check(used, action='greeting.submit', target=self.target,
                                      profile_revision=3, plan_revision=7, now=1003),
                         'CHANNEL_AUTHORIZATION_EXHAUSTED')

    def test_expired_authorization_is_refused(self):
        doc = action.authorization(**self.base)
        self.assertEqual(action.check(doc, action='greeting.submit', target=self.target,
                                      profile_revision=3, plan_revision=7, now=1000 + 900),
                         'CHANNEL_AUTHORIZATION_EXPIRED')

    def test_target_change_is_refused(self):
        doc = action.authorization(**self.base)
        other = action.target_key(job_id='j-2', company='某公司')
        self.assertEqual(action.check(doc, action='greeting.submit', target=other,
                                      profile_revision=3, plan_revision=7, now=1001),
                         'CHANNEL_TARGET_CHANGED')

    def test_profile_and_plan_revision_changes_are_refused(self):
        doc = action.authorization(**self.base)
        self.assertEqual(action.check(doc, action='greeting.submit', target=self.target,
                                      profile_revision=4, plan_revision=7, now=1001),
                         'CHANNEL_PROFILE_CHANGED')
        self.assertEqual(action.check(doc, action='greeting.submit', target=self.target,
                                      profile_revision=3, plan_revision=8, now=1001),
                         'CHANNEL_PLAN_CHANGED')

    def test_other_actions_are_not_covered(self):
        doc = action.authorization(**self.base)
        self.assertEqual(action.check(doc, action='reply.send', target=self.target,
                                      profile_revision=3, plan_revision=7, now=1001),
                         'CHANNEL_NOT_AUTHORIZED')

    def test_unknown_action_cannot_be_registered(self):
        with self.assertRaises(ValueError):
            action.authorization(**dict(self.base, action='reply.send'))

    def test_authorization_requires_account_evidence(self):
        with self.assertRaises(ValueError):
            action.authorization(**dict(self.base, account_evidence='   '))

    def test_ttl_cannot_exceed_the_cap(self):
        with self.assertRaises(ValueError):
            action.authorization(**dict(self.base, ttl_seconds=action.AUTHORIZATION_TTL_SECONDS + 1))

    def test_target_requires_job_and_company(self):
        with self.assertRaises(ValueError):
            action.target_key(job_id='', company='某公司')

    def test_target_key_is_stable_and_company_sensitive(self):
        a = action.target_key(job_id='j-1', company='某公司')
        b = action.target_key(job_id='j-1', company='某公司')
        c = action.target_key(job_id='j-1', company='别家公司')
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_conversation_key_participates(self):
        a = action.target_key(job_id='j-1', company='某公司', conversation_key='c-1')
        b = action.target_key(job_id='j-1', company='某公司', conversation_key='c-2')
        self.assertNotEqual(a, b)

    def test_public_view_hides_evidence_and_never_enables_sending(self):
        doc = action.authorization(**self.base)
        view = action.public(doc)
        self.assertNotIn('account_evidence', view)
        self.assertEqual(len(view['account_evidence_fingerprint']), 16)
        self.assertFalse(view['sending_enabled'])
        self.assertFalse(view['delivery_confirmed'])
        self.assertNotIn('display:张三', str(view))


class OutcomeTest(unittest.TestCase):
    def test_never_started(self):
        r = action.outcome(attempted=False, filled=False, submitted=False, observed_change=False)
        self.assertEqual(r['state'], 'not_executed')
        self.assertTrue(r['retryable'])
        self.assertFalse(r['consumes_budget'])

    def test_attempted_without_write(self):
        r = action.outcome(attempted=True, filled=False, submitted=False, observed_change=False)
        self.assertEqual(r['state'], 'not_executed')
        self.assertEqual(r['reason'], 'no_write_detected')

    def test_filled_but_not_submitted_consumes_budget(self):
        r = action.outcome(attempted=True, filled=True, submitted=False, observed_change=False)
        self.assertEqual(r['state'], 'filled_not_submitted')
        self.assertTrue(r['consumes_budget'])
        self.assertFalse(r['retryable'])

    def test_submitted_without_echo_is_unverified(self):
        r = action.outcome(attempted=True, filled=True, submitted=True, observed_change=False)
        self.assertEqual(r['state'], 'submitted_unverified')
        self.assertTrue(r['consumes_budget'])
        self.assertFalse(r['delivery_confirmed'])

    def test_new_echo_is_not_delivery(self):
        r = action.outcome(attempted=True, filled=True, submitted=True,
                           observed_change=True, expected_text='你好', observed_text='你好')
        self.assertEqual(r['state'], 'observed_reply')
        self.assertFalse(r['delivery_confirmed'])
        self.assertFalse(r['read_by_recipient'])

    def test_echo_text_mismatch_is_conflict(self):
        r = action.outcome(attempted=True, filled=True, submitted=True, observed_change=True,
                           expected_text='你好', observed_text='您好')
        self.assertEqual(r['state'], 'conflict')
        self.assertTrue(r['needs_human'])

    def test_echo_without_submit_is_conflict(self):
        r = action.outcome(attempted=True, filled=True, submitted=False, observed_change=True)
        self.assertEqual(r['state'], 'conflict')

    def test_every_state_is_declared(self):
        states = set()
        for attempted in (False, True):
            for filled in (False, True):
                for submitted in (False, True):
                    for observed in (False, True):
                        states.add(action.outcome(attempted=attempted, filled=filled,
                                                  submitted=submitted,
                                                  observed_change=observed)['state'])
        self.assertTrue(states.issubset(set(action.OUTCOME_STATES)), states)

    def test_unsafe_states_are_never_retryable(self):
        for attempted, filled, submitted, observed in ((True, False, True, True), (True, True, True, False)):
            r = action.outcome(attempted=attempted, filled=filled, submitted=submitted,
                               observed_change=observed)
            if r['state'] in action.UNSAFE_STATES:
                self.assertFalse(r['retryable'])


if __name__ == '__main__':
    unittest.main()
