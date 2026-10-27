"""End-to-end applicant loop: anchor, draft, reserve, hand off, self-report, readback, attribute.

This file is the acceptance scenario for the whole chain. Each existing unit test proves one
link; this one proves the links still fit together after eight batches of change.
"""
from copy import deepcopy
import unittest
from unittest import mock

import applicant_send
import app
import main
import runtime_tools
import store
import test_applicant_messages as fixture
from sandbox import KernelError, maintenance_access


def message(identity='mid-123', text='请问何时到岗？', **changes):
    return {**fixture.snapshot()['messages'][0], 'message_id': identity, 'text': text, **changes}


class ApplicantLoopTest(unittest.TestCase):
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
        self.task = self.h.call('runtime.task.execute', {'task_id': self.intent['draft_task_id']})
        return self.intent

    def confirm(self):
        return self.dispatch('runtime.applicant.send.recipient.confirm', {
            'intent_id': self.intent['intent_id'], 'expected_manual_revision': 0,
            'expected_profile_revision': 1, 'expected_binding_revision': 1,
            'expected_window_revision': self.window, 'confirmed': True})['data']['recipient']

    def reserve(self):
        recipient = self.confirm()
        return self.dispatch('runtime.applicant.send.reserve', {
            'intent_id': self.intent['intent_id'], 'recipient_binding_id': recipient['recipient_fingerprint'],
            'expected_manual_revision': 0, 'expected_profile_revision': 1, 'expected_binding_revision': 1,
            'expected_window_revision': self.window, 'confirmed': True})['data']

    def delivered_snapshot(self, text):
        return {**fixture.snapshot(), 'messages': [*self.messages, message('own-sent', text, direction='outgoing')]}

    def test_the_whole_loop_yields_one_attributable_delivery_evidence(self):
        reserved = self.reserve()
        attempt_id = reserved['attempt']['attempt_id']
        self.assertEqual(reserved['budget']['committed_attempts'], 1)
        card = self.dispatch('runtime.applicant.send.handoff',
                             {'attempt_id': attempt_id, 'confirmed': True})['data']
        self.assertEqual(card['text'], self.task['result']['text'])
        self.assertIs(card['manual_handoff_only'], True)

        claim = self.dispatch('runtime.applicant.send.manual.claim',
                              {'attempt_id': attempt_id, 'confirmed': True})['data']
        self.assertIs(claim['claim']['self_reported'], True)
        self.assertIs(claim['delivery_confirmed'], False)

        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat',
                               return_value=self.delivered_snapshot(self.task['result']['text'])):
            committed = self.dispatch('runtime.applicant.send.readback.commit', {'attempt_id': attempt_id})['data']
        self.assertTrue(committed['readback']['observed'])
        self.assertEqual(committed['readback']['message_id'], 'own-sent')
        self.assertIs(committed['delivery_confirmed'], False)

        stats = self.dispatch('runtime.applicant.send.stats', {})['data']
        self.assertEqual(stats['totals']['attempts'], 1)
        self.assertEqual((stats['totals']['handoffs'], stats['totals']['claims'], stats['totals']['readback_hits']),
                         (1, 1, 1))
        self.assertEqual(stats['totals']['handoff_to_readback_rate'], 1.0)
        self.assertEqual(stats['items'][0]['attempt_id'], attempt_id)
        self.assertIs(stats['limits']['not_platform_reply_rate'], True)
        self.assertIsNotNone(stats['limits']['sample_warning'])
        self.assertIs(stats['delivery_confirmed'], False)

    def test_reserving_the_same_intent_twice_never_double_counts(self):
        first = self.reserve()
        second = self.reserve()
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(second['attempt']['attempt_id'], first['attempt']['attempt_id'])
        self.assertEqual(second['budget']['committed_attempts'], 1)

    def test_daily_limit_stops_the_second_reservation_and_release_restores_it(self):
        with mock.patch.object(applicant_send, 'daily_limit', return_value=1):
            first = self.reserve()
            self.question('mid-456', '请问何时到岗？')
            self.confirm()
            with self.assertRaises(KernelError) as caught:
                self.reserve()
            self.assertEqual(caught.exception.code, 'SEND_DAILY_LIMIT_REACHED')
            released = self.dispatch('runtime.applicant.send.release', {
                'attempt_id': first['attempt']['attempt_id'], 'reason': 'user_cancelled', 'confirmed': True})['data']
            self.assertEqual(released['budget']['remaining'], 1)
            second = self.reserve()
            self.assertTrue(second['created'])
            self.assertNotEqual(second['attempt']['attempt_id'], first['attempt']['attempt_id'])

    def test_the_loop_blocks_forgetting_the_conversation_it_relies_on(self):
        self.reserve()
        with maintenance_access():
            with self.assertRaises(KernelError) as caught:
                self.dispatch('runtime.applicant.reply.forget',
                              {'intent_id': self.intent['intent_id'], 'confirmed': True})
        self.assertEqual(caught.exception.code, 'FORGET_BLOCKED_BY_EVIDENCE')
        with store.runtime_db() as c:
            self.assertTrue(c.execute('SELECT 1 FROM runtime_documents WHERE kind=?',
                                      ('applicant_reply_intent:' + self.intent['intent_id'],)).fetchone())


if __name__ == '__main__':
    unittest.main()
