"""HTTP contract for the applicant send loop: routes, status codes, response schema, CAS and idempotency.

G1 (docs/handoff/hermes-implementation-plan-2026-10-27.md) requires the contract to be checked
beyond "the tool name exists": every documented route is exercised through the real ASGI app and
its response is validated field by field.  Nothing here talks to a browser or a platform.
"""
import hashlib
import json
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import app
import applicant_browser
import applicant_send
import runtime_tools
import store
import test_applicant_send as harness

TOKEN_PATTERN = re.compile(r'[0-9a-f]{32}')
SHA256_PATTERN = re.compile(r'[0-9a-f]{64}')


class SendLoopHttpContractTest(unittest.TestCase):
    """Every case reuses the real fixture chain: profile, binding, observation window and intent."""

    def setUp(self):
        # Composition, not inheritance: the fixture's own test methods must not run twice here.
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.intent, self.window, self.source = self.f.intent, self.f.window, self.f.source

    def confirm(self, **changes):
        return self.f.confirm(**changes)

    def reserve(self, recipient=None, **changes):
        return self.f.reserve(recipient, **changes)

    def question(self, identity, text):
        saved = self.f.question(identity, text)
        self.intent, self.window, self.source = self.f.intent, self.f.window, self.f.source
        return saved

    def live(self, extra=()):
        """What the browser would show on a read-only check: the fixture messages plus any new ones."""
        return {**harness.fixture.snapshot(), 'messages': [*self.f.messages, *extra]}

    def http(self, method, path, body=None, token=None):
        client = TestClient(app.app, base_url='http://127.0.0.1:18770')
        try:
            client.get('/workspace')
            headers = {'X-Jobkit-Context': token if token is not None else self.h.context['context_token']}
            return client.request(method, '/api/runtime/' + path, headers=headers, json=body)
        finally:
            client.close()

    def ok(self, method, path, body=None, token=None):
        response = self.http(method, path, body, token)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload['ok'], payload)
        return payload['data']

    def failed(self, method, path, body=None, token=None, status=409, code=None):
        response = self.http(method, path, body, token)
        self.assertEqual(response.status_code, status, response.text)
        payload = response.json()
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['code'], code)
        self.assertIsInstance(payload['error'], str)
        self.assertTrue(payload['error'].strip())
        return payload

    def reserve_ready(self):
        self.confirm()
        return self.reserve()

    # ------------------------------------------------------------------ 观察窗口

    def test_window_get_and_update_schema(self):
        read = self.ok('GET', 'applicant/reply-window')
        self.assertIsInstance(read['revision'], int)
        self.assertIs(read['automatic_polling'], False)
        self.assertIs(read['sending_enabled'], False)
        self.assertIn('observation_gap_ok', read)
        self.assertIsInstance(read['content'], dict)
        self.assertEqual(set(read['content']),
                         {'scope_id', 'context_token', 'profile_revision', 'binding_revision',
                          'browser_session_fingerprint', 'party', 'started_at', 'last_observed_at',
                          'start_observation_task_id', 'last_observation_task_id', 'tail_message_id',
                          'seen', 'sending_enabled', 'automatic_polling'})
        self.assertIs(read['content']['sending_enabled'], False)
        self.assertIs(read['content']['automatic_polling'], False)

        stale = self.failed('POST', 'applicant/reply-window/start',
                            {'message_observation_task_id': self.source, 'expected_revision': read['revision'] + 1,
                             'expected_profile_revision': 1, 'expected_binding_revision': 1},
                            code='STALE_REPLY_WINDOW')
        self.assertTrue(stale['error'])

    def test_window_start_rejects_wrong_shapes_as_400(self):
        for body in ({}, {'message_observation_task_id': 'not-hex', 'expected_revision': 0,
                          'expected_profile_revision': 1, 'expected_binding_revision': 1},
                     {'message_observation_task_id': self.source, 'expected_revision': '0',
                      'expected_profile_revision': 1, 'expected_binding_revision': 1},
                     {'message_observation_task_id': self.source, 'expected_revision': 0,
                      'expected_profile_revision': 1, 'expected_binding_revision': 1, 'extra': 1}):
            with self.subTest(body=body):
                self.failed('POST', 'applicant/reply-window/start', body, status=400, code='INVALID_REPLY_WINDOW')

    # ------------------------------------------------------------------ 预检

    def test_preflight_schema_and_blocker_reporting(self):
        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', return_value=self.live()):
            data = self.ok('POST', 'applicant/replies/' + self.intent['intent_id'] + '/preflight',
                           {'expected_manual_revision': 0})
        self.assertEqual(set(data),
                         {'intent_id', 'draft_task_id', 'manual_revision', 'draft_text_sha256', 'profile_revision',
                          'binding_revision', 'window_revision', 'live_message_ids', 'draft_ready',
                          'recipient_fence_passed', 'recency_fence_passed', 'recency_basis', 'checked_at',
                          'expires_at', 'blockers', 'preflight_passed', 'platform_account_id_verified',
                          'recipient_verified', 'sending_enabled', 'quota_consumed', 'automatic_polling'})
        self.assertEqual(data['intent_id'], self.intent['intent_id'])
        self.assertTrue(SHA256_PATTERN.fullmatch(data['draft_text_sha256']))
        self.assertEqual(data['recency_basis'], 'first_observed_after_window_anchor')
        self.assertEqual(data['expires_at'] - data['checked_at'], 10)
        self.assertIsInstance(data['blockers'], list)
        self.assertEqual(data['preflight_passed'], not data['blockers'])
        self.assertIs(data['platform_account_id_verified'], False)
        self.assertIs(data['recipient_verified'], False)
        self.assertIs(data['sending_enabled'], False)
        self.assertIs(data['quota_consumed'], False)

    def test_preflight_rejects_unknown_fields_and_stale_draft_revision(self):
        path = 'applicant/replies/' + self.intent['intent_id'] + '/preflight'
        self.failed('POST', path, {'expected_manual_revision': 0, 'confirmed': True},
                    status=400, code='INVALID_REPLY_PREFLIGHT')
        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', return_value=self.live()):
            self.failed('POST', path, {'expected_manual_revision': 1}, code='STALE_MANUAL_DRAFT')

    # ------------------------------------------------------------------ 对象绑定 / 预占

    def test_recipient_confirm_schema_idempotency_and_change_refusal(self):
        data = self.confirm()
        self.assertEqual(set(data), {'recipient', 'revision', 'created', 'message_age_seconds', 'sending_enabled'})
        self.assertIs(data['created'], True)
        self.assertEqual(data['revision'], 1)
        self.assertIs(data['sending_enabled'], False)
        recipient = data['recipient']
        self.assertEqual(set(recipient),
                         {'scope_id', 'intent_id', 'draft_task_id', 'context_token', 'party', 'anchor_message_id',
                          'message_ids', 'profile_revision', 'binding_revision', 'browser_session_fingerprint',
                          'recipient_fingerprint', 'created_at', 'platform_account_id_verified', 'sending_enabled'})
        self.assertTrue(SHA256_PATTERN.fullmatch(recipient['recipient_fingerprint']))
        self.assertEqual(recipient['recipient_fingerprint'], applicant_send.fingerprint(recipient))
        self.assertIs(recipient['platform_account_id_verified'], False)
        self.assertLessEqual(data['message_age_seconds'], 120)

        again = self.ok('POST', 'applicant/send/recipient/confirm',
                        {'intent_id': self.intent['intent_id'], 'expected_manual_revision': 0,
                         'expected_profile_revision': 1, 'expected_binding_revision': 1,
                         'expected_window_revision': self.window, 'confirmed': True})
        self.assertIs(again['created'], False)
        self.assertEqual(again['recipient'], recipient)

        self.failed('POST', 'applicant/send/recipient/confirm',
                    {'intent_id': self.intent['intent_id'], 'expected_manual_revision': 0,
                     'expected_profile_revision': 1, 'expected_binding_revision': 1,
                     'expected_window_revision': self.window}, status=400, code='INVALID_SEND_RECIPIENT')
        read = self.ok('GET', 'applicant/send/recipient/' + self.intent['intent_id'])
        self.assertEqual(set(read), {'recipient', 'revision', 'profile_stale', 'binding_stale', 'sending_enabled'})
        self.assertEqual(read['recipient'], recipient)
        self.assertIs(read['profile_stale'], False)
        self.assertIs(read['binding_stale'], False)

    def test_reserve_schema_idempotency_budget_and_missing_recipient(self):
        self.failed('POST', 'applicant/send/reserve',
                    {'intent_id': self.intent['intent_id'], 'recipient_binding_id': '0' * 64,
                     'expected_manual_revision': 0, 'expected_profile_revision': 1,
                     'expected_binding_revision': 1, 'expected_window_revision': self.window, 'confirmed': True},
                    code='SEND_RECIPIENT_REQUIRED')

        reserved = self.reserve_ready()
        self.assertEqual(set(reserved), {'attempt', 'budget', 'created', 'message_age_seconds', 'sending_enabled'})
        self.assertIs(reserved['created'], True)
        attempt = reserved['attempt']
        self.assertEqual(set(attempt),
                         {'attempt_id', 'intent_id', 'draft_task_id', 'recipient_binding_id', 'budget_day', 'state',
                          'message_sha256', 'created_at', 'updated_at', 'sent', 'sending_enabled',
                          'delivery_confirmed'})
        self.assertTrue(TOKEN_PATTERN.fullmatch(attempt['attempt_id']))
        self.assertEqual(attempt['state'], 'reserved')
        self.assertIs(attempt['sent'], False)
        self.assertIs(attempt['delivery_confirmed'], False)
        budget = reserved['budget']
        self.assertEqual(set(budget), {'budget_day', 'daily_limit', 'committed_attempts', 'released_attempts',
                                       'not_sent_attempts', 'remaining', 'quota_source'})
        self.assertEqual(budget['quota_source'], 'local_workspace_not_platform')
        self.assertEqual(budget['committed_attempts'], 1)

        repeat = self.reserve()
        self.assertIs(repeat['created'], False)
        self.assertEqual(repeat['attempt']['attempt_id'], attempt['attempt_id'])
        self.assertEqual(repeat['budget']['committed_attempts'], 1)

        read = self.ok('GET', 'applicant/send/' + attempt['attempt_id'])
        self.assertEqual(set(read), {'attempt', 'budget', 'sending_enabled'})
        self.assertEqual(read['attempt'], attempt)

    def test_release_schema_state_machine_and_used_evidence(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        released = self.ok('POST', 'applicant/send/release',
                           {'attempt_id': attempt_id, 'reason': 'user_cancelled', 'confirmed': True})
        self.assertEqual(set(released), {'attempt', 'budget', 'reason', 'sending_enabled'})
        self.assertEqual(released['attempt']['state'], 'released')
        self.assertEqual(released['reason'], 'user_cancelled')
        self.assertEqual(released['budget']['released_attempts'], 1)
        self.assertEqual(released['budget']['committed_attempts'], 0)
        self.failed('POST', 'applicant/send/release',
                    {'attempt_id': attempt_id, 'reason': 'user_cancelled', 'confirmed': True},
                    code='INVALID_SEND_ATTEMPT_STATE')
        self.failed('POST', 'applicant/send/release',
                    {'attempt_id': attempt_id, 'reason': 'because', 'confirmed': True},
                    status=400, code='INVALID_SEND_RELEASE')

    def test_claimed_attempt_cannot_release_its_budget(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        self.ok('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True})
        self.ok('POST', 'applicant/send/manual/claim', {'attempt_id': attempt_id, 'confirmed': True})
        self.failed('POST', 'applicant/send/release',
                    {'attempt_id': attempt_id, 'reason': 'user_cancelled', 'confirmed': True},
                    code='SEND_ALREADY_USED')
        read = self.ok('GET', 'applicant/send/' + attempt_id)
        self.assertEqual(read['attempt']['state'], 'reserved')
        self.assertEqual(read['budget']['committed_attempts'], 1)

    # ------------------------------------------------------------------ 交接

    def test_handoff_schema_never_leaks_body_into_receipt(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        data = self.ok('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True})
        self.assertEqual(set(data), {'handoff', 'text', 'created', 'text_expires_at', 'expires_in_seconds',
                                     'manual_handoff_only', 'delivery_confirmed', 'sending_enabled'})
        self.assertIs(data['created'], True)
        self.assertIs(data['manual_handoff_only'], True)
        self.assertIs(data['delivery_confirmed'], False)
        self.assertIs(data['sending_enabled'], False)
        self.assertTrue(data['text'].strip())
        self.assertGreater(data['expires_in_seconds'], 0)
        handoff = data['handoff']
        self.assertEqual(set(handoff),
                         {'scope_id', 'attempt_id', 'intent_id', 'draft_task_id', 'recipient_fingerprint',
                          'party_display_name', 'anchor_message_id', 'message_ids', 'draft_text_sha256',
                          'text_expires_at', 'created_at', 'manual_handoff_only', 'sending_enabled'})
        self.assertNotIn('text', handoff)

        proof = self.ok('GET', 'applicant/send/handoff/' + attempt_id)
        self.assertEqual(set(proof), {'attempt', 'handoff', 'claim', 'expired', 'delivery_confirmed', 'sending_enabled'})
        self.assertEqual(proof['handoff'], handoff)
        self.assertIsNone(proof['claim'])
        self.assertIs(proof['expired'], False)
        self.assertIs(proof['delivery_confirmed'], False)

        self.failed('POST', 'applicant/send/handoff', {'attempt_id': attempt_id}, status=400,
                    code='INVALID_SEND_HANDOFF')

    def test_handoff_requires_reservation(self):
        self.failed('POST', 'applicant/send/handoff', {'attempt_id': 'f' * 32, 'confirmed': True},
                    code='SEND_ATTEMPT_NOT_FOUND')

    # ------------------------------------------------------------------ 队列

    def test_queue_view_and_batch_handoff_schema(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        queue = self.ok('GET', 'applicant/send/queue')
        self.assertEqual(set(queue), {'items', 'count', 'queue_limit', 'sending_enabled', 'delivery_confirmed'})
        self.assertEqual(queue['queue_limit'], 10)
        self.assertIs(queue['sending_enabled'], False)
        self.assertEqual(queue['count'], 1)
        item = queue['items'][0]
        self.assertEqual(item['attempt_id'], attempt_id)
        self.assertIs(item['ready'], True)
        self.assertIsNone(item['reason'])
        self.assertIs(item['has_handoff'], False)
        self.assertIs(item['claimed'], False)
        self.assertGreater(item['expires_in_seconds'], 0)
        self.assertIs(item['expired'], False)

        batch = self.ok('POST', 'applicant/send/queue/handoff',
                        {'attempt_ids': [attempt_id, attempt_id], 'confirmed': True})
        self.assertEqual(batch['ok_count'], 1)
        self.assertEqual(batch['skipped_count'], 0)
        self.assertIs(batch['partial'], False)
        self.assertEqual(batch['created_count'], 1)
        self.assertEqual(batch['queue_limit'], 10)
        self.assertIs(batch['manual_handoff_only'], True)
        self.assertIs(batch['delivery_confirmed'], False)
        self.assertEqual(len(batch['cards']), 1)
        self.assertTrue(batch['cards'][0]['text'].strip())
        self.assertIn('note', batch)

        self.failed('POST', 'applicant/send/queue/handoff',
                    {'attempt_ids': [attempt_id] * 11, 'confirmed': True}, status=400, code='INVALID_SEND_QUEUE')
        self.failed('POST', 'applicant/send/queue/handoff',
                    {'attempt_ids': ['nope'], 'confirmed': True}, status=400, code='INVALID_SEND_QUEUE')

        after = self.ok('GET', 'applicant/send/queue')
        self.assertIs(after['items'][0]['has_handoff'], True)

    def test_queue_reports_blocked_items_instead_of_hiding_them(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        with store.runtime_db() as c:
            c.execute('UPDATE runtime_send_attempts SET window_start_task_id=? WHERE attempt_id=?',
                      ('0' * 32, attempt_id))
        queue = self.ok('GET', 'applicant/send/queue')
        self.assertEqual(queue['count'], 1)
        item = queue['items'][0]
        self.assertIs(item['ready'], False)
        self.assertEqual(item['reason'], 'WINDOW_REBUILT')
        self.assertTrue(item['detail'].strip())
        self.failed('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True},
                    code='WINDOW_REBUILT')

    # ------------------------------------------------------------------ 自报与回读

    def test_manual_claim_is_self_report_not_delivery(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        self.failed('POST', 'applicant/send/manual/claim', {'attempt_id': attempt_id, 'confirmed': True},
                    code='SEND_HANDOFF_REQUIRED')
        card = self.ok('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True})
        claim = self.ok('POST', 'applicant/send/manual/claim', {'attempt_id': attempt_id, 'confirmed': True})
        self.assertEqual(set(claim), {'attempt', 'claim', 'created', 'delivery_confirmed', 'sending_enabled', 'note'})
        self.assertIs(claim['created'], True)
        self.assertIs(claim['delivery_confirmed'], False)
        self.assertIs(claim['sending_enabled'], False)
        self.assertEqual(set(claim['claim']), {'scope_id', 'attempt_id', 'handoff_revision', 'claimed_at',
                                               'self_reported', 'delivery_confirmed', 'sending_enabled'})
        self.assertIs(claim['claim']['self_reported'], True)
        self.assertIs(claim['claim']['delivery_confirmed'], False)
        self.assertIn('不是平台确认', claim['note'])

        again = self.ok('POST', 'applicant/send/manual/claim', {'attempt_id': attempt_id, 'confirmed': True})
        self.assertIs(again['created'], False)
        self.assertEqual(again['claim'], claim['claim'])
        self.assertTrue(card['text'])

    def readback(self, extra_messages=()):
        return mock.patch.object(applicant_browser, 'read_chat', return_value=self.live(extra_messages))

    def test_readback_and_commit_schema(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        card = self.ok('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True})
        text = card['text']
        outgoing = {'message_id': 'mid-own-new', 'message_id_source': 'dom_data_mid',
                    'direction': 'outgoing', 'kind': 'text', 'text': text, 'text_truncated': False,
                    'full_text_available': True, 'timestamp_label': '今天 10:31'}
        with self.readback([outgoing]):
            probe = self.ok('POST', 'applicant/send/readback', {'attempt_id': attempt_id})
            self.assertEqual(set(probe), {'attempt_id', 'intent_id', 'party_match', 'readback_observed',
                                          'evidence', 'delivered_confirmed', 'sending_enabled', 'note'})
            self.assertIs(probe['party_match'], True)
            self.assertIs(probe['readback_observed'], True)
            self.assertIs(probe['delivered_confirmed'], False)
            self.assertIs(probe['sending_enabled'], False)
            self.assertEqual(set(probe['evidence']), {'observed', 'ambiguous', 'reason', 'message_id',
                                                      'message_sha256', 'match_count'})
            committed = self.ok('POST', 'applicant/send/readback/commit', {'attempt_id': attempt_id})
        self.assertEqual(set(committed), {'readback', 'created', 'party_match', 'delivery_confirmed',
                                          'sending_enabled'})
        self.assertIs(committed['created'], True)
        self.assertIs(committed['delivery_confirmed'], False)
        record = committed['readback']
        self.assertEqual(set(record), {'scope_id', 'attempt_id', 'observed', 'ambiguous', 'reason', 'match_count',
                                       'message_id', 'message_sha256', 'observed_at', 'party_match',
                                       'delivery_confirmed', 'sending_enabled'})
        self.assertIs(record['observed'], True)
        self.assertIs(record['delivery_confirmed'], False)

        with self.readback([outgoing]):
            repeat = self.ok('POST', 'applicant/send/readback/commit', {'attempt_id': attempt_id})
        self.assertIs(repeat['created'], False)
        self.assertEqual(repeat['readback'], record)

    def test_readback_keeps_ambiguity_instead_of_inflating_hits(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        card = self.ok('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True})
        text = card['text']
        duplicated = [{'message_id': 'mid-own-a', 'message_id_source': 'dom_data_mid', 'direction': 'outgoing',
                       'kind': 'text', 'text': text, 'text_truncated': False, 'full_text_available': True,
                       'timestamp_label': None},
                      {'message_id': 'mid-own-b', 'message_id_source': 'dom_data_mid', 'direction': 'outgoing',
                       'kind': 'text', 'text': text, 'text_truncated': False, 'full_text_available': True,
                       'timestamp_label': None}]
        with self.readback(duplicated):
            probe = self.ok('POST', 'applicant/send/readback', {'attempt_id': attempt_id})
        # Two identical outgoing copies stay ambiguous: the hit is recorded but never upgraded to certain.
        self.assertIs(probe['readback_observed'], True)
        self.assertIs(probe['evidence']['ambiguous'], True)
        self.assertEqual(probe['evidence']['match_count'], 2)

    # ------------------------------------------------------------------ 统计与错误面

    def test_stats_schema_and_platform_disclaimer(self):
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        self.ok('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True})
        self.ok('POST', 'applicant/send/manual/claim', {'attempt_id': attempt_id, 'confirmed': True})
        stats = self.ok('GET', 'applicant/send/stats')
        self.assertEqual(set(stats), {'totals', 'by_day', 'by_draft', 'items', 'truncated', 'invalid_documents',
                                      'limits', 'delivery_confirmed', 'sending_enabled'})
        self.assertIs(stats['delivery_confirmed'], False)
        self.assertIs(stats['sending_enabled'], False)
        self.assertEqual(set(stats['totals']), {'attempts', 'by_state', 'handoffs', 'claims', 'readback_hits',
                                                'readback_ambiguous', 'handoff_to_readback_rate',
                                                'claim_to_readback_rate'})
        self.assertEqual(set(stats['limits']), {'readback_rate_basis', 'not_platform_reply_rate',
                                                'delivery_not_confirmed', 'sample_warning'})
        self.assertIs(stats['limits']['not_platform_reply_rate'], True)
        self.assertEqual(stats['totals']['attempts'], 1)
        self.assertEqual(stats['totals']['handoffs'], 1)
        self.assertEqual(stats['totals']['claims'], 1)
        self.assertEqual(stats['totals']['by_state'], {'reserved': 1})
        self.assertIsInstance(stats['by_day'], list)
        self.assertIsInstance(stats['by_draft'], list)
        self.assertIsInstance(stats['items'], list)


    def test_stale_context_and_unknown_route_are_refused(self):
        self.failed('GET', 'applicant/send/queue', None, token='0' * 32, code='STALE_CONTEXT')
        response = self.http('GET', 'applicant/send/queue', None, token='')
        self.assertIn(response.status_code, (400, 409))
        self.assertFalse(response.json()['ok'])

    def test_no_platform_write_claim_anywhere(self):
        """Every documented response that could be mistaken for delivery states it explicitly."""
        reserved = self.reserve_ready()
        attempt_id = reserved['attempt']['attempt_id']
        responses = [
            self.ok('POST', 'applicant/send/handoff', {'attempt_id': attempt_id, 'confirmed': True}),
            self.ok('POST', 'applicant/send/manual/claim', {'attempt_id': attempt_id, 'confirmed': True}),
            self.ok('GET', 'applicant/send/handoff/' + attempt_id),
            self.ok('GET', 'applicant/send/queue'),
            self.ok('GET', 'applicant/send/stats'),
        ]
        for payload in responses:
            self.assertIs(payload.get('sending_enabled', False), False)
            if 'delivery_confirmed' in payload:
                self.assertIs(payload['delivery_confirmed'], False)
            self.assertNotIn('sent', {key for key, value in payload.items() if value is True})


if __name__ == '__main__':
    unittest.main()
