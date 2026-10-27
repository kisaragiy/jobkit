"""Batch handoff queue: several cards at once, one failure never blocks the rest."""
from copy import deepcopy
import unittest
from unittest import mock

import applicant_reply_window
import applicant_send
import main
import runtime_tools
import store
import test_applicant_messages as fixture
from sandbox import KernelError


def message(identity='mid-123', text='请问何时到岗？', **changes):
    return {**fixture.snapshot()['messages'][0], 'message_id': identity, 'text': text, **changes}


class SendQueueTest(unittest.TestCase):
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

    def queue(self):
        return self.dispatch('runtime.applicant.send.queue.get', {})['data']

    def handoff_all(self, attempt_ids):
        return self.dispatch('runtime.applicant.send.queue.handoff',
                             {'attempt_ids': attempt_ids, 'confirmed': True})['data']

    def test_queue_lists_reservations_with_readiness_and_expiry(self):
        first = self.reserve()
        attempt_id = first['attempt']['attempt_id']
        queue = self.queue()
        self.assertEqual(queue['count'], 1)
        self.assertEqual(queue['items'][0]['attempt_id'], attempt_id)
        self.assertTrue(queue['items'][0]['ready'])
        self.assertFalse(queue['items'][0]['expired'])
        self.assertFalse(queue['items'][0]['has_handoff'])
        self.assertLessEqual(queue['items'][0]['expires_in_seconds'], 120)
        self.assertIs(queue['sending_enabled'], False)

    def test_batch_handoff_produces_one_card_per_reservation(self):
        first = self.reserve()
        first_text = self.task['result']['text']
        self.question('mid-456', '请问何时到岗？')
        second = self.reserve()
        result = self.handoff_all([first['attempt']['attempt_id'], second['attempt']['attempt_id']])
        self.assertEqual(result['ok_count'], 2)
        self.assertEqual(result['skipped'], [])
        self.assertFalse(result['partial'])
        self.assertEqual(result['created_count'], 2)
        self.assertEqual(result['cards'][0]['text'], first_text)
        self.assertTrue(all(card['manual_handoff_only'] for card in result['cards']))
        self.assertIs(result['delivery_confirmed'], False)
        self.assertEqual(self.queue_count(has_handoff=True), 2)

    def test_one_failing_card_never_blocks_the_others(self):
        first = self.reserve()
        self.question('mid-456', '请问何时到岗？')
        second = self.reserve()
        self.dispatch('runtime.applicant.send.release', {
            'attempt_id': second['attempt']['attempt_id'], 'reason': 'user_cancelled', 'confirmed': True})
        result = self.handoff_all([first['attempt']['attempt_id'], second['attempt']['attempt_id']])
        self.assertEqual(result['ok_count'], 1)
        self.assertEqual(result['skipped_count'], 1)
        self.assertTrue(result['partial'])
        self.assertEqual(result['skipped'][0]['attempt_id'], second['attempt']['attempt_id'])
        self.assertEqual(result['skipped'][0]['code'], 'INVALID_SEND_ATTEMPT_STATE')

    def test_a_batch_where_everything_fails_reports_the_first_reason(self):
        first = self.reserve()
        self.dispatch('runtime.applicant.send.release', {
            'attempt_id': first['attempt']['attempt_id'], 'reason': 'user_cancelled', 'confirmed': True})
        with self.assertRaises(KernelError) as caught:
            self.handoff_all([first['attempt']['attempt_id']])
        self.assertEqual(caught.exception.code, 'INVALID_SEND_ATTEMPT_STATE')

    def test_batch_handoff_is_idempotent_and_deduplicates_ids(self):
        first = self.reserve()
        attempt_id = first['attempt']['attempt_id']
        once = self.handoff_all([attempt_id, attempt_id])
        self.assertEqual(once['ok_count'], 1)
        again = self.handoff_all([attempt_id])
        self.assertEqual(again['created_count'], 0)
        self.assertFalse(again['cards'][0]['created'])
        self.assertEqual(again['cards'][0]['handoff'], once['cards'][0]['handoff'])

    def test_batch_size_and_payload_shape_are_bounded(self):
        first = self.reserve()
        attempt_id = first['attempt']['attempt_id']
        with self.assertRaises(KernelError) as caught:
            self.handoff_all([attempt_id] * 11)
        self.assertEqual(caught.exception.code, 'INVALID_SEND_QUEUE')
        with self.assertRaises(KernelError) as caught:
            self.handoff_all([])
        self.assertEqual(caught.exception.code, 'INVALID_SEND_QUEUE')
        with self.assertRaises(KernelError) as caught:
            self.dispatch('runtime.applicant.send.queue.handoff', {'attempt_ids': [attempt_id]})
        self.assertEqual(caught.exception.code, 'INVALID_SEND_QUEUE')
        with self.assertRaises(KernelError) as caught:
            self.dispatch('runtime.applicant.send.queue.handoff',
                          {'attempt_ids': ['nope'], 'confirmed': True})
        self.assertEqual(caught.exception.code, 'INVALID_SEND_QUEUE')

    def test_queue_view_marks_a_stale_draft_instead_of_hiding_it(self):
        first = self.reserve()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET result=json_set(result,'$.text',?) WHERE task_id=?",
                      ('改过的答复', self.intent['draft_task_id']))
        queue = self.queue()
        self.assertEqual(queue['count'], 1)
        self.assertFalse(queue['items'][0]['ready'])
        self.assertEqual(queue['items'][0]['reason'], 'DRAFT_CHANGED_AFTER_RESERVATION')
        with self.assertRaises(KernelError) as caught:
            self.handoff_all([first['attempt']['attempt_id']])
        self.assertEqual(caught.exception.code, 'DRAFT_CHANGED_AFTER_RESERVATION')

    def queue_count(self, has_handoff=None):
        items = self.queue()['items']
        if has_handoff is None:
            return len(items)
        return sum(1 for item in items if item['has_handoff'] is has_handoff)


if __name__ == '__main__':
    unittest.main()
