"""Forgetting reply intents: bodies removed, evidence chain protected; nothing is sent."""
from copy import deepcopy
import json
import unittest
from unittest import mock

import applicant_reply
import main
import runtime_tools
import store
import test_applicant_messages as fixture
from sandbox import KernelError, maintenance_access


def message(identity='mid-123', text='请问何时到岗？', **changes):
    return {**fixture.snapshot()['messages'][0], 'message_id': identity, 'text': text, **changes}


class ForgetReplyTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SelectedMessagesTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.messages = [message('own-old', '早前回复', direction='outgoing')]
        self.source = self.observe()
        self.window = self.dispatch('runtime.applicant.reply.window.start', {
            'message_observation_task_id': self.source, 'expected_revision': 0,
            'expected_profile_revision': 1, 'expected_binding_revision': 1})['data']['revision']
        self.question('mid-123', '请问何时到岗？')
        self.h.call('runtime.task.execute', {'task_id': self.intent['draft_task_id']})

    def dispatch(self, tool, payload=None):
        return main.dispatch(tool, payload, self.h.context['context_token'])

    def observe(self):
        value = {**fixture.snapshot(), 'messages': deepcopy(self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', return_value=value):
            return self.dispatch('runtime.applicant.messages.read', {})['trace_id']

    def question(self, identity, text):
        if self.messages[-1]['direction'] == 'incoming':
            self.messages = [*self.messages, message('own-' + identity, '已回复', direction='outgoing')]
        self.messages = [*self.messages, message(identity, text)]
        self.source = self.observe()
        self.window = self.dispatch('runtime.applicant.reply.window.observe', {
            'message_observation_task_id': self.source, 'expected_revision': self.window,
            'expected_profile_revision': 1, 'expected_binding_revision': 1})['data']['revision']
        saved = self.dispatch('runtime.applicant.reply.prepare', {
            'message_observation_task_id': self.source, 'message_ids': [identity],
            'expected_profile_revision': 1, 'expected_binding_revision': 1})['data']
        self.intent = {'intent_id': saved['intent']['intent_id'], 'draft_task_id': saved['task']['task_id']}
        return self.intent

    def plan(self, **payload):
        with maintenance_access():
            return self.dispatch('runtime.applicant.reply.forget.plan', payload)['data']

    def forget(self, **payload):
        with maintenance_access():
            return self.dispatch('runtime.applicant.reply.forget', payload)['data']

    def reserve(self):
        recipient = self.dispatch('runtime.applicant.send.recipient.confirm', {
            'intent_id': self.intent['intent_id'], 'expected_manual_revision': 0,
            'expected_profile_revision': 1, 'expected_binding_revision': 1,
            'expected_window_revision': self.window, 'confirmed': True})['data']['recipient']
        return self.dispatch('runtime.applicant.send.reserve', {
            'intent_id': self.intent['intent_id'], 'recipient_binding_id': recipient['recipient_fingerprint'],
            'expected_manual_revision': 0, 'expected_profile_revision': 1, 'expected_binding_revision': 1,
            'expected_window_revision': self.window, 'confirmed': True})['data']

    def party_name(self):
        return fixture.snapshot()['party']['display_name']

    def documents(self):
        with store.runtime_db() as c:
            return c.execute('SELECT count(*) FROM runtime_documents').fetchone()[0]

    def intent_document(self):
        with store.runtime_db() as c:
            return c.execute('SELECT revision FROM runtime_documents WHERE kind=?',
                             ('applicant_reply_intent:' + self.intent['intent_id'],)).fetchone()

    def message_document(self, message_id):
        with store.runtime_db() as c:
            return c.execute('SELECT revision FROM runtime_documents WHERE kind=?',
                             (applicant_reply.message_key(message_id),)).fetchone()

    def task_row(self):
        with store.runtime_db() as c:
            return dict(c.execute('SELECT payload,result,conversation_note FROM runtime_tasks WHERE task_id=?',
                                  (self.intent['draft_task_id'],)).fetchone())

    def test_plan_lists_what_would_be_removed_and_changes_nothing(self):
        before = self.documents()
        plan = self.plan(intent_id=self.intent['intent_id'])
        self.assertEqual(plan['intents'], [self.intent['intent_id']])
        self.assertEqual(plan['documents_to_delete'], 2)
        self.assertEqual(plan['tasks_to_redact'], 1)
        self.assertEqual(plan['blocked'], {})
        self.assertTrue(plan['irreversible'])
        self.assertFalse(plan['applied'])
        self.assertEqual(self.documents(), before)

    def test_forget_removes_the_intent_and_mapping_and_blanks_the_draft_body(self):
        result = self.forget(intent_id=self.intent['intent_id'], confirmed=True)
        self.assertTrue(result['applied'])
        self.assertEqual(result['deleted_documents'], 2)
        self.assertEqual(result['redacted_tasks'], 1)
        self.assertIsNone(self.intent_document())
        self.assertIsNone(self.message_document('mid-123'))
        task = self.task_row()
        self.assertEqual(json.loads(task['payload'])['message'], '')
        self.assertEqual(json.loads(task['result'])['text'], '')
        self.assertEqual(task['conversation_note'], '已删除')

    def test_forgetting_the_same_intent_twice_reports_it_is_gone(self):
        self.forget(intent_id=self.intent['intent_id'], confirmed=True)
        with self.assertRaises(KernelError) as caught:
            self.forget(intent_id=self.intent['intent_id'], confirmed=True)
        self.assertEqual(caught.exception.code, 'REPLY_INTENT_NOT_FOUND')

    def test_a_send_attempt_blocks_forgetting_the_whole_intent(self):
        self.reserve()
        with self.assertRaises(KernelError) as caught:
            self.forget(intent_id=self.intent['intent_id'], confirmed=True)
        self.assertEqual(caught.exception.code, 'FORGET_BLOCKED_BY_EVIDENCE')
        self.assertIsNotNone(self.intent_document())
        self.assertEqual(json.loads(self.task_row()['payload'])['message'], '请问何时到岗？')

    def test_a_handoff_card_blocks_forgetting_even_without_an_attempt_record(self):
        saved = self.reserve()
        attempt_id = saved['attempt']['attempt_id']
        self.dispatch('runtime.applicant.send.handoff', {'attempt_id': attempt_id, 'confirmed': True})
        plan = self.plan(intent_id=self.intent['intent_id'])
        self.assertIn(self.intent['intent_id'], plan['blocked'])
        with self.assertRaises(KernelError) as caught:
            self.forget(intent_id=self.intent['intent_id'], confirmed=True)
        self.assertEqual(caught.exception.code, 'FORGET_BLOCKED_BY_EVIDENCE')

    def test_forgetting_by_party_covers_every_intent_of_that_conversation(self):
        self.question('mid-456', '还招人吗？')
        plan = self.plan(party_display_name=self.party_name())
        self.assertEqual(len(plan['intents']), 2)
        before = self.documents()
        result = self.forget(party_display_name=self.party_name(), confirmed=True)
        self.assertEqual(len(result['intents']), 2)
        self.assertEqual(before - self.documents(), result['deleted_documents'])
        self.assertIsNone(self.message_document('mid-123'))

    def test_confirmation_and_selectors_are_enforced(self):
        with self.assertRaises(KernelError) as caught:
            self.forget(intent_id=self.intent['intent_id'])
        self.assertEqual(caught.exception.code, 'FORGET_CONFIRMATION_REQUIRED')
        with self.assertRaises(KernelError) as caught:
            self.plan(intent_id=self.intent['intent_id'], party_display_name=self.party_name())
        self.assertEqual(caught.exception.code, 'INVALID_FORGET_SELECTOR')
        with self.assertRaises(KernelError) as caught:
            self.plan(intent_id='not-an-id')
        self.assertEqual(caught.exception.code, 'INVALID_FORGET_SELECTOR')
        with self.assertRaises(KernelError) as caught:
            self.plan(intent_id='f' * 32)
        self.assertEqual(caught.exception.code, 'REPLY_INTENT_NOT_FOUND')

    def test_forget_is_refused_outside_local_maintenance(self):
        with self.assertRaises(KernelError) as caught:
            self.dispatch('runtime.applicant.reply.forget', {'intent_id': self.intent['intent_id'], 'confirmed': True})
        self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')


if __name__ == '__main__':
    unittest.main()
