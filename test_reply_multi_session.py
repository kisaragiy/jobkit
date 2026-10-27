"""多会话草稿隔离：同一个消息在两个会话键下各得一份草稿，谁也不能冒充谁。

Only local, fictional records: no platform access, no sending, temporary database.
"""
import json
from copy import deepcopy
import time
import unittest
from unittest import mock
import main
import store
import applicant_reply
import runtime_tools
import test_applicant_messages as fixture
from sandbox import KernelError

MISSING = object()


def message(identity='mid-123', text='请问何时到岗？', **changes):
    return {**fixture.snapshot()['messages'][0], 'message_id': identity, 'text': text, **changes}


class MultiSessionDraftTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SelectedMessagesTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups); self.h = self.f.h
        self.messages = [message()]
        self.source = self.observe()

    def dispatch(self, tool, payload=None):
        return main.dispatch(tool, payload, self.h.context['context_token'])

    def observe(self, messages=None):
        value = {**fixture.snapshot(), 'messages': deepcopy(messages if messages is not None else self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', return_value=value):
            return self.dispatch('runtime.applicant.messages.read', {})['trace_id']

    def start(self, key, expected=0):
        """以一条刚读取的消息为这个会话键建立观察起点。"""
        payload = {'message_observation_task_id': self.observe(), 'expected_revision': expected,
                   'expected_profile_revision': 1, 'expected_binding_revision': 1, 'conversation_key': key}
        return self.dispatch('runtime.applicant.reply.window.start', payload)['data']

    def prepare(self, key=MISSING, source=None, **changes):
        payload = {'message_observation_task_id': source or self.source,
                   'message_ids': [m['message_id'] for m in self.messages],
                   'expected_profile_revision': 1, 'expected_binding_revision': 1, **changes}
        if key is not MISSING:
            payload['conversation_key'] = key
        return self.dispatch('runtime.applicant.reply.prepare', payload)['data']

    def window_get(self, key=MISSING):
        payload = {} if key is MISSING else {'conversation_key': key}
        return self.dispatch('runtime.applicant.reply.window.get', payload)['data']

    def preflight(self, intent_id, key=MISSING, manual=0):
        payload = {'intent_id': intent_id, 'expected_manual_revision': manual}
        if key is not MISSING:
            payload['conversation_key'] = key
        value = {**fixture.snapshot(), 'messages': deepcopy(self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', return_value=value):
            return self.dispatch('runtime.applicant.reply.preflight', payload)['data']

    def counts(self):
        with store.runtime_db() as c:
            return (c.execute("SELECT count(*) FROM runtime_tasks WHERE kind='work' AND tool='draft.reply'").fetchone()[0],
                    c.execute("SELECT count(*) FROM runtime_documents WHERE kind LIKE 'applicant_reply_intent:%'").fetchone()[0])

    def draft_keys(self):
        with store.runtime_db() as c:
            return sorted(row['conversation_key'] or applicant_reply.DEFAULT_CONVERSATION_KEY
                          for row in c.execute("SELECT conversation_key FROM runtime_tasks WHERE kind='work' AND tool='draft.reply'"))

    def stored_window(self, key):
        with store.runtime_db() as c:
            row = c.execute("SELECT content FROM runtime_documents WHERE kind='applicant_reply_window'").fetchone()
        return next(item for item in json.loads(row['content'])['items'] if item['key'] == key)

    def rewrite_window(self, key, **changes):
        """直接改写库里的窗口记录：用来构造「过期」和「同名键换了会话」这两种现实里迟早会出现的状态。"""
        with store.runtime_db() as c:
            row = c.execute("SELECT content FROM runtime_documents WHERE kind='applicant_reply_window'").fetchone()
            container = json.loads(row['content'])
            for item in container['items']:
                if item['key'] == key:
                    item.update({k: (deepcopy(v) if isinstance(v, dict) else v) for k, v in changes.items()})
            c.execute("UPDATE runtime_documents SET content=? WHERE kind='applicant_reply_window'",
                      (json.dumps(container, ensure_ascii=False),))

    def test_two_keys_prepare_the_same_message_without_overwriting_each_other(self):
        self.start('conv-a'); self.start('conv-b')
        first = self.prepare('conv-a'); second = self.prepare('conv-b')
        self.assertNotEqual(first['task']['task_id'], second['task']['task_id'])
        self.assertNotEqual(first['intent']['intent_id'], second['intent']['intent_id'])
        self.assertEqual(first['intent']['conversation_key'], 'conv-a')
        self.assertEqual(second['intent']['conversation_key'], 'conv-b')
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.draft_keys(), ['conv-a', 'conv-b'])
        for saved, key in ((first, 'conv-a'), (second, 'conv-b')):
            current = self.h.call('runtime.applicant.reply.get', {'intent_id': saved['intent']['intent_id']})
            self.assertEqual(current['task']['task_id'], saved['task']['task_id'])
            self.assertEqual(current['intent']['conversation_key'], key)

    def test_repeated_prepare_in_the_same_key_is_idempotent(self):
        self.start('conv-a')
        first = self.prepare('conv-a')
        again = self.prepare('conv-a', source=self.observe())      # 重新读一次消息再准备
        self.assertEqual(first['intent']['intent_id'], again['intent']['intent_id'])
        self.assertEqual(first['task']['task_id'], again['task']['task_id'])
        self.assertNotEqual(first['fresh_observation_task_id'], again['fresh_observation_task_id'])
        self.assertEqual(self.counts(), (1, 1))

    def test_prepare_without_a_key_keeps_the_legacy_behaviour(self):
        # 没有窗口也能准备：升级前的页面只认默认会话，多要求一步会把它们全部拒掉。
        saved = self.prepare()
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.draft_keys(), [applicant_reply.DEFAULT_CONVERSATION_KEY])
        self.assertEqual(self.window_get()['drafts'][0]['task_id'], saved['task']['task_id'])
        self.assertEqual(self.window_get('conv-a')['drafts'], [])

    def test_expired_window_is_refused_before_any_draft(self):
        self.start('conv-a')
        now = time.time()
        self.rewrite_window('conv-a', first_seen_at=now - 7200, expires_at=now - 3600)
        with self.assertRaises(KernelError) as caught:
            self.prepare('conv-a')
        self.assertEqual(caught.exception.code, 'WINDOW_EXPIRED')
        self.assertEqual(self.counts(), (0, 0))

    def test_named_key_without_a_window_is_refused(self):
        with self.assertRaises(KernelError) as caught:
            self.prepare('conv-a')
        self.assertEqual(caught.exception.code, 'REPLY_WINDOW_REQUIRED')
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.window_get('conv-a')['windows'], [])

    def test_invalid_key_shape_is_refused_with_400(self):
        for key in ('', '  padded', 'has space', 'a' * 129, '张三（招聘）', 'line\nbreak'):
            with self.subTest(key=key):
                with self.assertRaises(KernelError) as caught:
                    self.prepare(key)
                self.assertEqual(caught.exception.code, 'INVALID_CONVERSATION_KEY')
                self.assertEqual(caught.exception.status, 400)
                with self.assertRaises(KernelError) as caught:
                    self.window_get(key)
                self.assertEqual(caught.exception.code, 'INVALID_CONVERSATION_KEY')
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.window_get()['windows'], [])

    def test_window_of_another_party_is_refused(self):
        self.start('conv-a')
        original = self.stored_window('conv-a')['window']['party']
        self.rewrite_window('conv-a', window={**self.stored_window('conv-a')['window'],
                                              'party': {'display_name': '另一个HR', 'party_summary': '另一个HR | 另一家公司'}})
        with self.assertRaises(KernelError) as caught:
            self.prepare('conv-a')
        self.assertEqual(caught.exception.code, 'WINDOW_PARTY_MISMATCH')
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.stored_window('conv-a')['window']['party']['display_name'], '另一个HR')
        self.assertNotEqual(original['display_name'], '另一个HR')

    def test_cross_key_edit_and_review_are_refused(self):
        self.start('conv-a'); self.start('conv-b')
        first = self.prepare('conv-a')['task']['task_id']
        second = self.prepare('conv-b')['task']['task_id']
        self.assertNotEqual(first, second)
        for tool, payload in (('runtime.task.review', {'task_id': first, 'expected_review_revision': 0, 'handled': True}),
                              ('runtime.task.edit', {'task_id': first, 'text': '改过的稿子', 'confirmed': True,
                                                     'expected_manual_revision': 0, 'expected_document_revision': 1})):
            with self.subTest(tool=tool):
                with self.assertRaises(KernelError) as caught:
                    self.dispatch(tool, {**payload, 'conversation_key': 'conv-b'})
                self.assertEqual(caught.exception.code, 'CONVERSATION_KEY_MISMATCH')
        self.h.call('runtime.task.execute', {'task_id': first})
        reviewed = self.dispatch('runtime.task.review',
                                 {'task_id': first, 'expected_review_revision': 0, 'handled': True,
                                  'conversation_key': 'conv-a'})['data']
        self.assertEqual(reviewed['review_status'], 'handled')      # 自己的键照常通过

    def test_cross_key_copy_and_edits_are_refused(self):
        self.start('conv-a'); self.start('conv-b')
        first = self.prepare('conv-a')['task']['task_id']
        self.prepare('conv-b')
        self.h.call('runtime.task.execute', {'task_id': first})
        for tool in ('runtime.task.copy.get', 'runtime.task.edits.get'):
            with self.subTest(tool=tool):
                with self.assertRaises(KernelError) as caught:
                    self.dispatch(tool, {'task_id': first, 'conversation_key': 'conv-b'})
                self.assertEqual(caught.exception.code, 'CONVERSATION_KEY_MISMATCH')
                self.assertTrue(self.dispatch(tool, {'task_id': first, 'conversation_key': 'conv-a'})['data'])
                self.assertTrue(self.dispatch(tool, {'task_id': first})['data'])   # 不传键：与升级前一致

    def test_cross_key_preflight_is_refused(self):
        self.start('conv-a'); self.start('conv-b')
        intent_id = self.prepare('conv-a')['intent']['intent_id']
        with self.assertRaises(KernelError) as caught:
            self.preflight(intent_id, 'conv-b')
        self.assertEqual(caught.exception.code, 'CONVERSATION_KEY_MISMATCH')
        # 自己的键能读到底层窗口：草稿还没执行，blockers 里是 DRAFT_NOT_READY，不是串会话。
        checked = self.preflight(intent_id, 'conv-a')
        self.assertIn('DRAFT_NOT_READY', checked['blockers'])
        self.assertNotIn('CONVERSATION_KEY_MISMATCH', checked['blockers'])
        self.assertFalse(checked['preflight_passed'])

    def test_preflight_reads_the_intents_own_window(self):
        self.start('conv-a')
        intent_id = self.prepare('conv-a')['intent']['intent_id']
        legacy = self.window_get()
        self.assertEqual([item['key'] for item in legacy['windows']], ['conv-a'])
        self.assertEqual(legacy['revision'], 0)          # 默认会话自己没有窗口
        checked = self.preflight(intent_id)              # 不传键：也要按意图自己的键去读窗口
        self.assertNotIn('REPLY_WINDOW_REQUIRED', checked['blockers'])
        self.assertIn('DRAFT_NOT_READY', checked['blockers'])

    def test_replacement_inherits_the_source_tasks_key(self):
        self.start('conv-a'); self.start('conv-b')
        source = self.prepare('conv-a')['task']['task_id']
        self.prepare('conv-b')
        self.h.call('runtime.task.execute', {'task_id': source})
        with self.assertRaises(KernelError) as caught:
            self.dispatch('runtime.task.replace', {'task_id': source, 'expected_review_revision': 0,
                                                   'expected_document_revision': 1, 'conversation_key': 'conv-b'})
        self.assertEqual(caught.exception.code, 'CONVERSATION_KEY_MISMATCH')
        replaced = self.dispatch('runtime.task.replace', {'task_id': source, 'expected_review_revision': 0,
                                                          'expected_document_revision': 1,
                                                          'conversation_key': 'conv-a'})['data']
        with store.runtime_db() as c:
            row = c.execute('SELECT * FROM runtime_tasks WHERE task_id=?', (replaced['task_id'],)).fetchone()
        self.assertEqual(row['conversation_key'], 'conv-a')          # 重建没有把 A 会话变成默认会话
        self.assertEqual(applicant_reply.task_key(row), 'conv-a')
        self.assertEqual(self.draft_keys(), ['conv-a', 'conv-a', 'conv-b'])

    def test_window_get_returns_each_keys_own_summary(self):
        self.start('conv-a'); self.start('conv-b')
        first = self.prepare('conv-a'); second = self.prepare('conv-b')
        a, b = self.window_get('conv-a'), self.window_get('conv-b')
        self.assertEqual(a['conversation_key'], 'conv-a')
        self.assertEqual([draft['task_id'] for draft in a['drafts']], [first['task']['task_id']])
        self.assertEqual([intent['intent_id'] for intent in a['intents']], [first['intent']['intent_id']])
        self.assertEqual([draft['task_id'] for draft in b['drafts']], [second['task']['task_id']])
        self.assertEqual([intent['intent_id'] for intent in b['intents']], [second['intent']['intent_id']])
        self.assertFalse(a['drafts_truncated'])
        # 两个键的窗口都在同一份文档里，但摘要只报自己的草稿
        self.assertEqual(sorted(item['key'] for item in a['windows']), ['conv-a', 'conv-b'])
        self.assertEqual(self.window_get()['drafts'], [])
        self.assertEqual(self.window_get()['intents'], [])

    def test_rows_written_before_keys_still_read_as_the_default_session(self):
        saved = self.prepare()
        with store.runtime_db() as c:  # 升级前的记录：任务上没有键，意图与消息关联里也没有这个字段
            c.execute("UPDATE runtime_tasks SET conversation_key='' WHERE kind='work' AND tool='draft.reply'")
            for row in c.execute("SELECT kind,content FROM runtime_documents WHERE kind LIKE 'applicant_reply_%'").fetchall():
                data = json.loads(row['content'])
                if isinstance(data, dict) and 'conversation_key' in data:
                    data.pop('conversation_key')
                    c.execute('UPDATE runtime_documents SET content=? WHERE kind=?',
                              (json.dumps(data, ensure_ascii=False), row['kind']))
        current = self.h.call('runtime.applicant.reply.get', {'intent_id': saved['intent']['intent_id']})
        self.assertEqual(current['intent']['conversation_key'], applicant_reply.DEFAULT_CONVERSATION_KEY)
        self.assertEqual(current['task']['task_id'], saved['task']['task_id'])
        legacy = self.window_get()
        self.assertEqual([draft['task_id'] for draft in legacy['drafts']], [saved['task']['task_id']])
        self.assertEqual([intent['intent_id'] for intent in legacy['intents']], [saved['intent']['intent_id']])
        again = self.prepare()                                        # 旧记录照旧幂等复用
        self.assertEqual(again['intent']['intent_id'], saved['intent']['intent_id'])
        self.assertEqual(self.counts(), (1, 1))


if __name__ == '__main__':
    unittest.main()
