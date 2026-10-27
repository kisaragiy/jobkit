"""B 方案（真实会话严格授权）测试：目标指纹、凭证 TTL、逐字段核对、一次性。"""
import unittest

import applicant_conversation as C

TARGET = {'name': '示例招聘方', 'company': '示例科技有限公司',
          'position': 'AI应用开发助理', 'job_id': None}
TEXT = '您好，看到贵司在招 AI 应用开发，我做过 Agent 后端，方便聊两句吗？'
BY = 'user'


def confirm(target=TARGET, now=1000.0, used=False):
    record = C.confirmation_required(target, by=BY, now=now)
    record['used'] = used
    return record


class SnapshotTest(unittest.TestCase):
    def test_snapshot_requires_name_and_company(self):
        with self.assertRaises(ValueError):
            C.target_snapshot(name='', company='某公司')
        with self.assertRaises(ValueError):
            C.target_snapshot(name='示例招聘方', company=' ')

    def test_position_and_job_are_optional(self):
        snap = C.target_snapshot(name='示例招聘方', company='某公司')
        self.assertIsNone(snap['position'])
        self.assertIsNone(snap['job_id'])

    def test_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            C.target_snapshot(name='王' * (C.MAX_NAME + 1), company='某公司')

    def test_fingerprint_changes_with_every_identity_field(self):
        base = C.fingerprint(TARGET)
        for key, value in (('name', '李先生'), ('company', '别的公司'),
                           ('position', '后端工程师'), ('job_id', 'abc')):
            changed = dict(TARGET, **{key: value})
            self.assertNotEqual(base, C.fingerprint(changed), key)

    def test_fingerprint_is_stable(self):
        self.assertEqual(C.fingerprint(TARGET), C.fingerprint(dict(TARGET)))


class ConfirmationTest(unittest.TestCase):
    def test_valid_confirmation_passes(self):
        self.assertIsNone(C.check_confirmation(confirm(), target=TARGET, now=1010.0))

    def test_expired_confirmation_is_refused(self):
        record = confirm(now=1000.0)
        self.assertEqual(C.check_confirmation(record, target=TARGET,
                                              now=1000.0 + C.CONFIRMATION_TTL_SECONDS + 1),
                         'CONVERSATION_CONFIRMATION_INVALID')

    def test_reused_confirmation_is_refused(self):
        self.assertEqual(C.check_confirmation(confirm(used=True), target=TARGET, now=1010.0),
                         'CONVERSATION_CONFIRMATION_TAKEN')

    def test_confirmation_bound_to_target(self):
        other = dict(TARGET, name='李先生')
        self.assertEqual(C.check_confirmation(confirm(), target=other, now=1010.0),
                         'CONVERSATION_TARGET_CHANGED')

    def test_missing_by_is_refused(self):
        with self.assertRaises(ValueError):
            C.confirmation_required(TARGET, by='  ')


class AuthorizationTest(unittest.TestCase):
    def make(self, **over):
        kwargs = dict(scope_id='acct-1', target=TARGET, confirmation=confirm(now=1000.0),
                      text=TEXT, profile_revision=1, plan_revision=1, now=1000.0)
        kwargs.update(over)
        return C.authorization(**kwargs)

    def test_authorization_uses_confirmation(self):
        doc = self.make()
        self.assertEqual(doc['kind'], C.AUTHORIZATION_KIND)
        self.assertEqual(doc['max_uses'], 1)
        self.assertEqual(doc['used'], 0)
        self.assertEqual(doc['confirmation_by'], BY)
        self.assertFalse(doc['sending_enabled'])
        self.assertFalse(doc['delivery_confirmed'])

    def test_authorization_ttl_is_short(self):
        doc = self.make()
        self.assertEqual(doc['expires_at'] - doc['created_at'], C.AUTHORIZATION_TTL_SECONDS)
        self.assertLessEqual(C.AUTHORIZATION_TTL_SECONDS, 120)

    def test_body_is_not_stored_in_clear(self):
        doc = self.make()
        self.assertNotIn(TEXT, str(doc))
        self.assertEqual(len(doc['body_sha256']), 64)

    def test_expired_confirmation_blocks_authorization(self):
        with self.assertRaises(ValueError) as caught:
            self.make(confirmation=confirm(now=1000.0), now=1000.0 + C.CONFIRMATION_TTL_SECONDS + 1)
        self.assertIn('CONVERSATION_CONFIRMATION_INVALID', str(caught.exception))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.doc = C.authorization(scope_id='acct-1', target=TARGET, confirmation=confirm(now=1000.0),
                                  text=TEXT, profile_revision=1, plan_revision=1, now=1000.0)

    def run_check(self, **over):
        kwargs = dict(target=TARGET, text=TEXT, profile_revision=1, plan_revision=1, now=1010.0)
        kwargs.update(over)
        return C.check(self.doc, **kwargs)

    def test_happy_path(self):
        self.assertIsNone(self.run_check())

    def test_expired_authorization(self):
        self.assertEqual(self.run_check(now=1000.0 + C.AUTHORIZATION_TTL_SECONDS + 1),
                         'CONVERSATION_AUTHORIZATION_EXPIRED')

    def test_used_authorization(self):
        used = C.consume(self.doc)
        self.assertEqual(C.check(used, target=TARGET, text=TEXT, profile_revision=1,
                                 plan_revision=1, now=1010.0),
                         'CONVERSATION_AUTHORIZATION_EXHAUSTED')

    def test_target_name_changed(self):
        self.assertEqual(self.run_check(target=dict(TARGET, name='李先生')),
                         'CONVERSATION_TARGET_CHANGED')

    def test_target_company_changed(self):
        self.assertEqual(self.run_check(target=dict(TARGET, company='别的公司')),
                         'CONVERSATION_TARGET_CHANGED')

    def test_target_position_changed(self):
        self.assertEqual(self.run_check(target=dict(TARGET, position='后端工程师')),
                         'CONVERSATION_TARGET_CHANGED')

    def test_unreadable_target_changes_every_field(self):
        blank = {'name': None, 'company': None, 'position': None, 'job_id': None}
        self.assertEqual(self.run_check(target=blank), 'CONVERSATION_TARGET_CHANGED')

    def test_body_changed(self):
        self.assertEqual(self.run_check(text='另一句话'), 'CONVERSATION_BODY_CHANGED')

    def test_plan_changed(self):
        self.assertEqual(self.run_check(plan_revision=2), 'CONVERSATION_PLAN_CHANGED')
        self.assertEqual(self.run_check(profile_revision=2), 'CONVERSATION_PLAN_CHANGED')

    def test_unknown_document(self):
        self.assertEqual(C.check({}, target=TARGET, text=TEXT,
                                 profile_revision=1, plan_revision=1, now=1010.0),
                         'CONVERSATION_NOT_AUTHORIZED')

    def test_consume_is_pure(self):
        updated = C.consume(self.doc)
        self.assertEqual(self.doc['used'], 0)
        self.assertEqual(updated['used'], 1)
        self.assertEqual(updated['status'], 'used')

    def test_every_code_has_message(self):
        for code in ('CONVERSATION_NOT_AUTHORIZED', 'CONVERSATION_AUTHORIZATION_EXHAUSTED',
                     'CONVERSATION_AUTHORIZATION_EXPIRED', 'CONVERSATION_TARGET_CHANGED',
                     'CONVERSATION_TARGET_UNREADABLE', 'CONVERSATION_BODY_CHANGED',
                     'CONVERSATION_CONFIRMATION_INVALID', 'CONVERSATION_CONFIRMATION_TAKEN',
                     'CONVERSATION_PLAN_CHANGED'):
            self.assertIn(code, C.CODES)


class ObserveTargetTest(unittest.TestCase):
    def test_match_reports_no_diffs(self):
        doc = C.authorization(scope_id='a', target=TARGET, confirmation=confirm(now=1000.0),
                              text=TEXT, profile_revision=1, plan_revision=1, now=1000.0)
        result = C.observe_target(TARGET, document=doc)
        self.assertTrue(result['match'])
        self.assertEqual(result['diff_fields'], [])

    def test_diff_lists_changed_fields(self):
        doc = C.authorization(scope_id='a', target=TARGET, confirmation=confirm(now=1000.0),
                              text=TEXT, profile_revision=1, plan_revision=1, now=1000.0)
        result = C.observe_target(dict(TARGET, name='李先生'), document=doc)
        self.assertFalse(result['match'])
        self.assertEqual(result['diff_fields'], ['name'])


if __name__ == '__main__':
    unittest.main()
