"""Keyed observation windows: pure registry rules plus the tool-layer document migration."""
from copy import deepcopy
import json
import time
import unittest
from unittest import mock
import applicant_reply_window
import applicant_reply_windows as windows
import main
import runtime_tools
import store
import test_applicant_messages as fixture
from sandbox import KernelError


def window(start='t1', **changes):
    return {'start_observation_task_id': start, 'started_at': 1000.0, 'last_observed_at': 1000.0,
            'seen': {}, 'party': {'display_name': '同名HR'}, **changes}


def seeded(keys, *, now=1000.0, ttl=windows.WINDOW_TTL_SECONDS):
    container = windows.empty()
    for index, key in enumerate(keys):
        container = windows.upsert(container, key, window('t%d' % index), now=now, ttl=ttl)
    return container


class KeyedWindowRulesTest(unittest.TestCase):
    """Pure logic only: no database, no clock reads, no platform."""

    def test_conversation_key_must_be_a_stable_machine_identifier(self):
        for value in ('conv-1', 'boss:123456', 'a' * 128, windows.DEFAULT_KEY):
            with self.subTest(value=value):
                self.assertEqual(windows.checked_key(value), value)
        for value in ('', '   ', '  padded', 'has space', 'line\nbreak', 'a' * 129, None, 17,
                      '张三（招聘）'):
            with self.subTest(value=value):
                with self.assertRaises(windows.InvalidKeyError):
                    windows.checked_key(value)
        # 显示名不是键：带空白的名字直接拒绝，同名 HR 就不会被并成一份窗口
        self.assertFalse(windows.valid_key('张三 （招聘）'))

    def test_empty_container_has_no_windows(self):
        container = windows.empty()
        self.assertEqual(windows.keys(container), [])
        self.assertIsNone(windows.get(container, 'conv-1'))

    def test_first_write_creates_one_record_with_a_fixed_expiry(self):
        container = windows.upsert(windows.empty(), 'conv-1', window('t1'), now=1000.0, ttl=120)
        record = windows.get(container, 'conv-1')
        self.assertEqual(windows.keys(container), ['conv-1'])
        self.assertEqual((record['start_task_id'], record['revision']), ('t1', 1))
        self.assertEqual((record['first_seen_at'], record['expires_at']), (1000.0, 1120.0))
        self.assertFalse(windows.expired(record, 1119.0))

    def test_repeated_write_keeps_one_record_and_advances_the_same_key(self):
        container = windows.upsert(windows.empty(), 'conv-1', window('t1'), now=1000.0, ttl=120)
        again = windows.upsert(container, 'conv-1', window('t1', last_observed_at=1030.0), now=1030.0, ttl=120)
        record = windows.get(again, 'conv-1')
        self.assertEqual(windows.keys(again), ['conv-1'])          # 同键幂等：不会多出一份窗口
        self.assertEqual(record['revision'], 2)
        self.assertEqual(record['start_task_id'], 't1')            # 推进不改起点
        self.assertEqual(record['first_seen_at'], 1000.0)
        self.assertEqual(record['window']['last_observed_at'], 1030.0)

    def test_advancing_never_renews_the_expiry(self):
        container = windows.upsert(windows.empty(), 'conv-1', window('t1'), now=1000.0, ttl=120)
        for moment in (1060.0, 1100.0):
            container = windows.upsert(container, 'conv-1', window('t1', last_observed_at=moment), now=moment, ttl=120)
            self.assertEqual(windows.get(container, 'conv-1')['expires_at'], 1120.0)
        with self.assertRaises(windows.ExpiredError):
            windows.upsert(container, 'conv-1', window('t1', last_observed_at=1125.0), now=1125.0, ttl=120)

    def test_writing_a_new_anchor_requires_an_explicit_rebuild(self):
        container = windows.upsert(windows.empty(), 'conv-1', window('t1'), now=1000.0)
        with self.assertRaises(windows.AnchorMismatch):
            windows.upsert(container, 'conv-1', window('t2'), now=1010.0)
        with self.assertRaises(windows.WindowStoreError):
            windows.advance(container, 'missing-key', window('t1'), now=1010.0)

    def test_rebuild_moves_the_start_and_invalidates_old_candidates(self):
        container = windows.upsert(windows.empty(), 'conv-1', window('t1'), now=1000.0, ttl=120)
        old = windows.get(container, 'conv-1')
        rebuilt = windows.rebuild(container, 'conv-1', window('t2', started_at=1010.0), now=1010.0, ttl=120)
        new = windows.get(rebuilt, 'conv-1')
        self.assertEqual((new['start_task_id'], new['revision']), ('t2', 2))
        self.assertEqual((new['first_seen_at'], new['expires_at']), (1010.0, 1130.0))  # 重建才换新到期时间
        self.assertFalse(windows.is_current(new, old['start_task_id']))                  # A05：旧候选失效
        self.assertTrue(windows.is_current(new, 't2'))

    def test_advance_keeps_candidates_alive(self):
        container = windows.upsert(windows.empty(), 'conv-1', window('t1'), now=1000.0, ttl=600)
        advanced = windows.advance(container, 'conv-1', window('t1', last_observed_at=1040.0), now=1040.0)
        record = windows.get(advanced, 'conv-1')
        self.assertEqual(record['revision'], 2)
        self.assertTrue(windows.is_current(record, 't1'))       # 推进不等于重建：预占照常可用

    def test_expired_window_can_only_be_rebuilt(self):
        container = windows.upsert(windows.empty(), 'conv-1', window('t1'), now=1000.0, ttl=120)
        with self.assertRaises(windows.ExpiredError):
            windows.advance(container, 'conv-1', window('t1'), now=1200.0)
        rebuilt = windows.rebuild(container, 'conv-1', window('t2'), now=1200.0, ttl=120)
        self.assertEqual(windows.get(rebuilt, 'conv-1')['expires_at'], 1320.0)

    def test_capacity_is_bounded_and_live_windows_are_never_dropped(self):
        container = seeded(['conv-%d' % i for i in range(windows.MAX_KEYS)])
        self.assertEqual(len(windows.keys(container)), windows.MAX_KEYS)
        with self.assertRaises(windows.CapacityError):
            windows.upsert(container, 'conv-extra', window('t9'), now=1001.0)
        self.assertEqual(len(windows.keys(container)), windows.MAX_KEYS)

    def test_capacity_evicts_only_expired_windows(self):
        # 19 份过期窗口 + 1 份活的：新键写入时只淘汰过期的那 19 份
        container = seeded(['old-%d' % i for i in range(windows.MAX_KEYS - 1)], now=1000.0, ttl=100)
        container = windows.upsert(container, 'live', window('t-live'), now=1101.0, ttl=3600)
        self.assertEqual(windows.keys(container), ['live'])
        for index in range(windows.MAX_KEYS - 1):
            container = windows.upsert(container, 'later-%d' % index, window('t%d' % index), now=1101.0, ttl=3600)
        self.assertEqual(len(windows.keys(container)), windows.MAX_KEYS)
        with self.assertRaises(windows.CapacityError):
            windows.upsert(container, 'one-more', window('t0'), now=1102.0, ttl=3600)
        self.assertIn('live', windows.keys(container))   # 未过期的会话不会被静默丢弃

    def test_same_display_name_different_keys_do_not_bleed(self):
        container = seeded(['conv-a', 'conv-b'])
        both = windows.get(container, 'conv-a')['window']['party']['display_name']
        self.assertEqual(both, windows.get(container, 'conv-b')['window']['party']['display_name'])
        advanced = windows.advance(container, 'conv-a', window('t0', last_observed_at=1005.0), now=1005.0)
        self.assertEqual(windows.get(advanced, 'conv-a')['revision'], 2)
        untouched = windows.get(advanced, 'conv-b')
        self.assertEqual((untouched['revision'], untouched['start_task_id']), (1, 't1'))
        self.assertEqual(untouched['window']['last_observed_at'], 1000.0)

    def test_legacy_flat_document_migrates_to_one_default_key_window(self):
        legacy = {**window('t-legacy'), 'scope_id': 'scope-1', 'seen': {'mid-1': {'digest': '0' * 64}}}
        container = windows.load(legacy, revision=5)
        record = windows.get(container, windows.DEFAULT_KEY)
        self.assertEqual(record['start_task_id'], 't-legacy')
        self.assertEqual(record['revision'], 5)                  # 不丢旧 revision
        self.assertEqual(record['window'], legacy)               # 不丢旧正文
        self.assertEqual(record['expires_at'], 1000.0 + windows.WINDOW_TTL_SECONDS)
        self.assertEqual(windows.load(container), container)     # 新形状再读一次是幂等的

    def test_public_projection_hides_the_window_payload(self):
        record = windows.get(seeded(['conv-a']), 'conv-a')
        projection = windows.public(record, 1001.0)
        self.assertEqual(set(projection),
                         {'key', 'start_task_id', 'first_seen_at', 'revision', 'expires_at', 'expired'})
        self.assertNotIn('window', projection)
        self.assertNotIn('seen', projection)
        self.assertIs(projection['expired'], False)
        self.assertIs(windows.public(record, 99999.0)['expired'], True)

    def test_corrupt_records_are_refused_instead_of_silently_dropped(self):
        good = windows.get(seeded(['conv-a']), 'conv-a')
        for broken in ({key: value for key, value in good.items() if key != 'revision'},
                       {**good, 'revision': 0}, {**good, 'key': 'has space'},
                       {**good, 'window': {}}, 'not-a-record'):
            with self.subTest(broken=broken):
                with self.assertRaises(windows.WindowStoreError):
                    windows.load({'items': [broken]})
        for content in ('flat', {'items': 'nope'}, {'items': []} | {'extra': 1}):
            with self.subTest(content=content):
                if content == {'items': []} | {'extra': 1}:
                    self.assertEqual(windows.load(content)['items'], [])
                else:
                    with self.assertRaises(windows.WindowStoreError):
                        windows.load(content)


def message(identity='mid-123', text='请问何时到岗？', **changes):
    return {**fixture.snapshot()['messages'][0], 'message_id': identity, 'text': text, **changes}


class KeyedWindowToolTest(unittest.TestCase):
    """The same rules through the real tools, on one runtime_documents row and the same kind."""

    def setUp(self):
        self.f = fixture.SelectedMessagesTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups); self.h = self.f.h
        self.messages = [message('own-old', '早前回复', direction='outgoing')]
        self.scope = self.h.context['scope_id']

    def dispatch(self, tool, payload=None):
        return main.dispatch(tool, payload, self.h.context['context_token'])

    def observe(self):
        value = {**fixture.snapshot(), 'messages': deepcopy(self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', return_value=value):
            return self.dispatch('runtime.applicant.messages.read', {})['trace_id']

    def start(self, source, key=None, expected=0):
        payload = {'message_observation_task_id': source, 'expected_revision': expected,
                   'expected_profile_revision': 1, 'expected_binding_revision': 1}
        if key is not None:
            payload['conversation_key'] = key
        return self.dispatch('runtime.applicant.reply.window.start', payload)['data']

    def advance(self, source, expected, key=None):
        payload = {'message_observation_task_id': source, 'expected_revision': expected,
                   'expected_profile_revision': 1, 'expected_binding_revision': 1}
        if key is not None:
            payload['conversation_key'] = key
        return self.dispatch('runtime.applicant.reply.window.observe', payload)['data']

    def get(self, key=None):
        payload = {'conversation_key': key} if key is not None else {}
        return self.dispatch('runtime.applicant.reply.window.get', payload)['data']

    def stored(self):
        with store.runtime_db() as c:
            row = c.execute("SELECT content FROM runtime_documents WHERE kind='applicant_reply_window'").fetchone()
        return json.loads(row['content'])

    def test_legacy_document_is_migrated_on_read_and_rewritten_in_the_keyed_shape(self):
        first = self.start(self.observe())
        self.assertEqual(first['revision'], 1)
        self.assertIn('items', self.stored())
        legacy_window = self.stored()['items'][0]['window']
        with store.runtime_db() as c:  # 升级前的形状：一份全局窗口，没有 items
            c.execute("UPDATE runtime_documents SET revision=5, content=? WHERE kind='applicant_reply_window'",
                      (json.dumps(legacy_window, ensure_ascii=False),))
        read = self.get()
        self.assertEqual(read['revision'], 5)                        # 旧 revision 不丢
        self.assertEqual(read['conversation_key'], applicant_reply_window.DEFAULT_CONVERSATION_KEY)
        self.assertEqual(read['content']['start_observation_task_id'],
                         legacy_window['start_observation_task_id'])  # 旧起点不丢
        self.assertEqual([item['key'] for item in read['windows']],
                         [applicant_reply_window.DEFAULT_CONVERSATION_KEY])
        self.assertEqual(read['windows'][0]['revision'], 5)
        migrated = self.advance(self.observe(), 5)
        self.assertEqual(migrated['revision'], 6)
        self.assertEqual(migrated['content']['start_observation_task_id'],
                         legacy_window['start_observation_task_id'])
        stored = self.stored()
        self.assertIn('items', stored)
        self.assertEqual([(item['key'], item['revision']) for item in stored['items']],
                         [(applicant_reply_window.DEFAULT_CONVERSATION_KEY, 6)])

    def test_two_conversation_keys_coexist_on_one_document(self):
        source_a = self.observe()
        first = self.start(source_a, 'conv-a')
        self.assertEqual(first['revision'], 1)
        source_b = self.observe()
        second = self.start(source_b, 'conv-b', expected=0)          # 新键不受另一会话的 revision 影响
        self.assertEqual(second['revision'], 1)
        # 默认键仍是空的：没有串。契约层会把空 dict 规范化成 null，所以判 falsy 而不是 == {}
        self.assertEqual(self.get()['revision'], 0)
        a, b = self.get('conv-a'), self.get('conv-b')
        self.assertEqual(a['content']['start_observation_task_id'], source_a)
        self.assertEqual(b['content']['start_observation_task_id'], source_b)
        self.assertEqual(a['document_revision'], b['document_revision'])
        self.assertEqual(a['content']['party'], b['content']['party'])   # 同名 HR，不同键
        self.assertEqual([item['key'] for item in a['windows']], ['conv-a', 'conv-b'])
        advanced = self.advance(self.observe(), 1, 'conv-a')
        self.assertEqual(advanced['revision'], 2)
        self.assertEqual(self.get('conv-a')['content']['start_observation_task_id'], source_a)
        untouched = self.get('conv-b')
        self.assertEqual((untouched['revision'], untouched['content']['start_observation_task_id']),
                         (1, source_b))
        self.assertEqual(sorted(item['key'] for item in self.stored()['items']), ['conv-a', 'conv-b'])

    def test_conversation_key_shape_is_fenced_with_400(self):
        source = self.observe()
        for key in ('', '   ', 'has space', 'a' * 129):
            with self.subTest(key=key):
                with self.assertRaises(KernelError) as caught:
                    self.start(source, key)
                self.assertEqual(caught.exception.code, 'INVALID_CONVERSATION_KEY')
                self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(KernelError) as caught:
            self.dispatch('runtime.applicant.reply.window.start',
                          {'message_observation_task_id': source, 'expected_revision': 0,
                           'expected_profile_revision': 1, 'expected_binding_revision': 1, 'extra': 1})
        self.assertEqual(caught.exception.code, 'INVALID_REPLY_WINDOW')
        self.assertEqual(self.get()['windows'], [])   # 被拒绝的请求没有留下任何窗口


if __name__ == '__main__':
    unittest.main()
