"""HTTP contract for the applicant send loop: routes, status codes, response schema, CAS and idempotency.

G1 (docs/handoff/hermes-implementation-plan-2026-10-27.md) requires the contract to be checked
beyond "the tool name exists": every documented route is exercised through the real ASGI app and
its response is validated field by field.  Nothing here talks to a browser or a platform.
"""
import hashlib
import json
import re
import unittest
from copy import deepcopy
from unittest import mock

from fastapi.testclient import TestClient

import app
import applicant_browser
import applicant_send
import applicant_send_ledger as ledger
import main
import runtime_tools
import store
import test_applicant_messages as messages_fixture
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


class OutcomeArchiveHttpContractTest(unittest.TestCase):
    """归档出口的 HTTP 契约（docs/api/applicant-send-loop.md 第十二节）。

    工作台要能把「需要人工归档」报警变成一个动作，所以这条路由必须真的能打：身份/来源校验
    一条都不放宽、payload 坏形状 400、未闭环拒绝码能区分「先核对」和「正在点」。
    """

    ROUTE = 'applicant/chat/outcome/archive'

    def setUp(self):
        self.f = harness.SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.scope = self.h.context['scope_id']

    # ---- helpers -------------------------------------------------------
    def http(self, body, token=None):
        client = TestClient(app.app, base_url='http://127.0.0.1:18770')
        try:
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.h.context['context_token'] if token is None else token}
            return client.request('POST', '/api/runtime/' + self.ROUTE, headers=headers, json=body)
        finally:
            client.close()

    def ok(self, body):
        response = self.http(body)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload['ok'], payload)
        return payload['data']

    def refused(self, body, *, status, code, token=None):
        response = self.http(body, token)
        self.assertEqual(response.status_code, status, response.text)
        payload = response.json()
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['code'], code, payload)
        self.assertTrue(payload['error'].strip())
        return payload

    def seeded(self, state, n):
        """造一条指定状态的台账记录（n 只用来区分对象与时间）。"""
        stamp = 1793104188.0 + n
        entry = ledger.new_entry(scope_id=self.scope, target_sha256='%064x' % n,
                                 body_sha256='b' * 64, action='greeting', ts=stamp)
        if state == ledger.STATE_SUBMITTING:
            return entry
        if state == ledger.STATE_OBSERVED:
            return ledger.close(entry, ledger.STATE_OBSERVED, ts=stamp + 1)
        closed = ledger.close(entry, ledger.STATE_UNKNOWN, ts=stamp + 1)
        if state == ledger.STATE_UNKNOWN:
            return closed
        if state == ledger.STATE_DELIVERED:
            return ledger.resolve(closed, 'delivered', by='user', ts=stamp + 2)
        if state == ledger.STATE_NOT_DELIVERED:
            return ledger.resolve(closed, 'not_delivered', by='user', ts=stamp + 2)
        raise AssertionError('unsupported fixture state: %s' % state)

    def write_ledger(self, items):
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                      "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                      "content=excluded.content",
                      (self.scope, ledger.LEDGER_KIND, 1,
                       json.dumps({'items': items}, ensure_ascii=False)))

    def ledger_items(self, kind=ledger.LEDGER_KIND):
        with store.runtime_db() as c:
            c.execute('BEGIN')
            doc = store.runtime_document(c, self.scope, kind)
        content = doc['content'] if isinstance(doc['content'], dict) else {}
        return [item for item in (content.get('items') or []) if isinstance(item, dict)]

    def outcome(self):
        client = TestClient(app.app, base_url='http://127.0.0.1:18770')
        try:
            client.get('/workspace')
            response = client.get('/api/runtime/applicant/chat/outcome',
                                  headers={'X-Jobkit-Context': self.h.context['context_token']})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()['data']
        finally:
            client.close()

    # ---- 身份与来源 ----------------------------------------------------
    def test_missing_or_foreign_context_is_refused_without_touching_the_ledger(self):
        entry = self.seeded(ledger.STATE_OBSERVED, 1)
        self.write_ledger([entry])
        for token in ('', '0' * 32):
            with self.subTest(token=token):
                self.refused({'confirmed_by': 'user'}, status=409, code='STALE_CONTEXT', token=token)
        self.assertEqual([item['ledger_id'] for item in self.ledger_items()], [entry['ledger_id']])

    # ---- payload -------------------------------------------------------
    def test_bad_payload_shapes_are_400_and_move_nothing(self):
        entry = self.seeded(ledger.STATE_OBSERVED, 1)
        self.write_ledger([entry])
        for body in ({}, {'confirmed_by': ''}, {'confirmed_by': '   '},
                     {'confirmed_by': 'user', 'ledger_ids': []},
                     {'confirmed_by': 'user', 'ledger_ids': 'abc'},
                     {'confirmed_by': 'user', 'target_sha256': 'abc'},
                     {'confirmed_by': 'user', 'extra': 1}):
            with self.subTest(body=body):
                self.refused(body, status=400, code='INVALID_ACTION_REQUEST')
        self.assertEqual(len(self.ledger_items()), 1)
        self.assertEqual(self.ledger_items(ledger.ARCHIVE_KIND), [])

    # ---- 正常路径 ------------------------------------------------------
    def test_archive_moves_contacted_evidence_and_reports_counts(self):
        observed = self.seeded(ledger.STATE_OBSERVED, 1)
        delivered = self.seeded(ledger.STATE_DELIVERED, 2)
        unresolved = self.seeded(ledger.STATE_UNKNOWN, 3)
        self.write_ledger([observed, delivered, unresolved])

        done = self.ok({'confirmed_by': 'user'})
        self.assertEqual(set(done), {'status', 'archived', 'archived_count', 'already_archived',
                                     'active_count', 'archive_total', 'capacity', 'archive',
                                     'archived_by', 'sending_enabled', 'delivery_confirmed'})
        self.assertEqual(done['status'], 'archived')
        self.assertEqual(sorted(done['archived']),
                         sorted([observed['ledger_id'], delivered['ledger_id']]))
        self.assertEqual(done['archived_count'], 2)
        self.assertEqual(done['already_archived'], [])
        self.assertEqual(done['active_count'], 1)
        self.assertEqual(done['archive_total'], 2)
        self.assertEqual(done['archived_by'], 'user')
        self.assertIs(done['sending_enabled'], False)
        self.assertIs(done['delivery_confirmed'], False)
        # 归档文档没有套上限，只报告条数；活跃台账只剩未核对那条
        self.assertEqual(done['archive']['archived'], 2)
        self.assertEqual([item['ledger_id'] for item in self.ledger_items()], [unresolved['ledger_id']])
        self.assertEqual(sorted(item['ledger_id'] for item in self.ledger_items(ledger.ARCHIVE_KIND)),
                         sorted([observed['ledger_id'], delivered['ledger_id']]))

        # outcome 面板重拉后仍然看得到归档条数与剩下的未核对记录
        view = self.outcome()
        self.assertEqual(view['archive']['archived'], 2)
        self.assertEqual([item['ledger_id'] for item in view['blocking']], [unresolved['ledger_id']])
        self.assertIs(view['capacity']['overflow'], False)

        # 重复归档幂等：点名同一条不产生第二条，报 already_archived
        repeat = self.ok({'confirmed_by': 'user', 'ledger_ids': [observed['ledger_id']]})
        self.assertEqual(repeat['status'], 'already_archived')
        self.assertEqual(repeat['archived_count'], 0)
        self.assertEqual(repeat['already_archived'], [observed['ledger_id']])
        self.assertEqual(repeat['archive_total'], 2)

    # ---- 拒绝码：先说「先核对」而不是「归档失败」 -----------------------
    def test_unfinished_entries_are_refused_with_distinguishable_codes(self):
        submitting = self.seeded(ledger.STATE_SUBMITTING, 1)
        unresolved = self.seeded(ledger.STATE_UNKNOWN, 2)
        skipped = self.seeded(ledger.STATE_NOT_DELIVERED, 3)
        self.write_ledger([submitting, unresolved, skipped])

        flight = self.refused({'confirmed_by': 'user', 'ledger_ids': [submitting['ledger_id']]},
                              status=409, code='SEND_LEDGER_IN_FLIGHT')
        self.assertIn('执行中', flight['error'])
        pending = self.refused({'confirmed_by': 'user', 'ledger_ids': [unresolved['ledger_id']]},
                               status=409, code='SEND_LEDGER_UNRESOLVED')
        self.assertIn('核对', pending['error'])
        self.refused({'confirmed_by': 'user', 'ledger_ids': [skipped['ledger_id']]},
                     status=409, code='SEND_LEDGER_NOT_ARCHIVABLE')
        # 整批拒绝，不做部分成功
        self.assertEqual(len(self.ledger_items()), 3)
        self.assertEqual(self.ledger_items(ledger.ARCHIVE_KIND), [])

    def test_unknown_ledger_id_is_404(self):
        self.write_ledger([self.seeded(ledger.STATE_OBSERVED, 1)])
        self.refused({'confirmed_by': 'user', 'ledger_ids': ['0' * 20]},
                     status=404, code='SEND_LEDGER_ENTRY_NOT_FOUND')


class DraftConversationKeyHttpContractTest(unittest.TestCase):
    """copy/edits 两个 GET 路由能吃可选的 conversation_key（docs/api/runtime-task-drafts.md）。

    升级前的页面只认 task_id，所以不带键必须与旧行为逐字段一致；带了键就必须真的核对：
    非法形状 400，跨会话 409。宁可 400 也不把坏键静默降级成「旧行为」——那样界面会以为
    自己锁定了会话，实际却没有。
    """

    ROUTES = ('copy', 'edits')
    INVALID_KEYS = ('', ' padded', 'has space', 'a' * 129, 'line\nbreak', '张三（招聘）')

    def setUp(self):
        # 复用多会话草稿的既有夹具链：虚构消息、本机临时库，不碰浏览器也不碰真实 jobkit.db。
        self.f = messages_fixture.SelectedMessagesTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.messages = [deepcopy(messages_fixture.snapshot()['messages'][0])]
        self.source = self.observe()
        self.start('conv-a')
        self.start('conv-b')
        self.task_id = self.prepare('conv-a')['task']['task_id']
        self.dispatch('runtime.task.execute', {'task_id': self.task_id})

    # ---- helpers -------------------------------------------------------
    def dispatch(self, tool, payload=None):
        return main.dispatch(tool, payload, self.h.context['context_token'])

    def observe(self):
        value = {**messages_fixture.snapshot(), 'messages': deepcopy(self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', return_value=value):
            return self.dispatch('runtime.applicant.messages.read', {})['trace_id']

    def start(self, key):
        return self.dispatch('runtime.applicant.reply.window.start',
                             {'message_observation_task_id': self.observe(), 'expected_revision': 0,
                              'expected_profile_revision': 1, 'expected_binding_revision': 1,
                              'conversation_key': key})['data']

    def prepare(self, key):
        return self.dispatch('runtime.applicant.reply.prepare',
                             {'message_observation_task_id': self.source,
                              'message_ids': [message['message_id'] for message in self.messages],
                              'expected_profile_revision': 1, 'expected_binding_revision': 1,
                              'conversation_key': key})['data']

    def http(self, route, key=None, token=None):
        client = TestClient(app.app, base_url='http://127.0.0.1:18770')
        try:
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.h.context['context_token'] if token is None else token}
            params = {} if key is None else {'conversation_key': key}
            return client.request('GET', '/api/runtime/tasks/%s/%s' % (self.task_id, route),
                                  headers=headers, params=params)
        finally:
            client.close()

    def ok(self, route, key=None):
        response = self.http(route, key)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload['ok'], payload)
        return payload['data']

    def refused(self, route, key, status, code):
        response = self.http(route, key)
        self.assertEqual(response.status_code, status, response.text)
        payload = response.json()
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['code'], code, payload)
        self.assertTrue(payload['error'].strip())
        return payload

    # ---- 不带键：与升级前一致 ------------------------------------------
    def test_without_the_key_the_http_response_matches_the_tool_exactly(self):
        for route, tool, keys in (('copy', 'runtime.task.copy.get', {'task_id', 'text', 'manual_revision',
                                                                    'document_revision'}),
                                  ('edits', 'runtime.task.edits.get', {'task_id', 'total', 'edits'})):
            with self.subTest(route=route):
                direct = self.dispatch(tool, {'task_id': self.task_id})['data']
                over_http = self.ok(route)
                self.assertEqual(set(over_http), keys)
                self.assertEqual(over_http, direct)
                self.assertEqual(over_http['task_id'], self.task_id)

    def test_own_key_returns_the_same_payload_as_the_legacy_call(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.ok(route, 'conv-a'), self.ok(route))

    # ---- 传了键：非法形状 400，跨会话 409 -------------------------------
    def test_invalid_key_shapes_are_400_instead_of_falling_back_to_legacy(self):
        for key in self.INVALID_KEYS:
            for route in self.ROUTES:
                with self.subTest(route=route, key=key):
                    self.refused(route, key, 400, 'INVALID_CONVERSATION_KEY')

    def test_a_key_from_another_conversation_is_refused(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                self.refused(route, 'conv-b', 409, 'CONVERSATION_KEY_MISMATCH')
                # 被拒之后自己的键照常可读：拒绝的是串会话，不是把草稿锁死。
                self.assertEqual(self.ok(route, 'conv-a'), self.ok(route))

    def test_a_foreign_context_token_still_cannot_read_the_draft(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                response = self.http(route, 'conv-a', token='0' * 32)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['code'], 'STALE_CONTEXT')


if __name__ == '__main__':
    unittest.main()
