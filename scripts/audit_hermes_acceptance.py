"""Offline acceptance probes for the 2026-10-27 Hermes handoff.

These assert the desired behavior and deliberately expose gaps in the phase-40
baseline. They use temporary databases and fictional browser responses only.
Run from the project root: python scripts/audit_hermes_acceptance.py
Keep separate from the existing passing gate until the findings are fixed.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import applicant_send
import runtime_tools
import store
from sandbox import KernelError, maintenance_access
from test_applicant_send import SendTargetTest, message
from test_applicant_messages import snapshot


class HermesAcceptanceAudit(unittest.TestCase):
    def setUp(self):
        self.f = SendTargetTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def dispatch(self, name, payload=None):
        return self.f.dispatch(name, payload)

    def reserve(self):
        return self.f.reserve()['attempt']['attempt_id']

    def handoff(self, attempt_id):
        return self.dispatch('runtime.applicant.send.handoff',
                             {'attempt_id': attempt_id, 'confirmed': True})['data']

    def test_A01_new_control_receipt_recovers_without_replay(self):
        self.f.confirm()
        with store.runtime_db() as c:
            row = c.execute("SELECT task_id FROM runtime_tasks WHERE tool=? ORDER BY created_at DESC",
                            ('runtime.applicant.send.recipient.confirm',)).fetchone()
            task_id = row['task_id']
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL,error_code=NULL WHERE task_id=?",
                      (task_id,))
        recovered = self.dispatch('runtime.task.recover', {'task_id': task_id})['data']
        self.assertEqual((recovered['status'], recovered['error_code']),
                         ('completed', 'RECOVERED_COMMIT'),
                         'A01: valid committed recipient confirmation must reconcile from its receipt')

    def test_A02_historical_same_text_is_not_this_attempt_readback(self):
        text = self.f.task['result']['text']
        live = {**snapshot(), 'messages': [message('historical-own', text, direction='outgoing'),
                                          *deepcopy(self.f.messages)]}
        evidence = applicant_send.readback_evidence(live, text, ['mid-123'])
        self.assertFalse(evidence['observed'],
                         'A02: an outgoing message before the incoming anchor is not a new send')

    def test_A03_claimed_attempt_cannot_release_budget(self):
        attempt_id = self.reserve()
        self.handoff(attempt_id)
        self.dispatch('runtime.applicant.send.manual.claim', {'attempt_id': attempt_id, 'confirmed': True})
        with self.assertRaises(KernelError, msg='A03: self-reported send must not refund its budget'):
            self.dispatch('runtime.applicant.send.release',
                          {'attempt_id': attempt_id, 'reason': 'user_cancelled', 'confirmed': True})

    def test_A04_profile_change_invalidates_copy_ready_card(self):
        attempt_id = self.reserve()
        with store.runtime_db() as c:
            profile = store.runtime_document(c, self.f.h.context['scope_id'], 'profile')
        self.dispatch('runtime.document.save', {'content': {**profile['content'], 'available': '一个月后'},
                                               'expected_revision': profile['revision'], 'confirmed': True})
        with self.assertRaises(KernelError, msg='A04: old profile facts cannot remain copy-ready'):
            self.handoff(attempt_id)

    def test_A05_restarting_window_cannot_requalify_old_intent(self):
        attempt_id = self.reserve()
        source = self.f.observe()
        self.dispatch('runtime.applicant.reply.window.start', {
            'message_observation_task_id': source, 'expected_revision': self.f.window,
            'expected_profile_revision': 1, 'expected_binding_revision': 1})
        with self.assertRaises(KernelError, msg='A05: a new baseline must not renew an old intent'):
            self.handoff(attempt_id)

    def test_A06_forget_removes_original_observation_body(self):
        source = self.f.source
        with maintenance_access():
            self.dispatch('runtime.applicant.reply.forget',
                          {'intent_id': self.f.intent['intent_id'], 'confirmed': True})
        with store.runtime_db() as c:
            row = c.execute('SELECT result FROM runtime_tasks WHERE task_id=?', (source,)).fetchone()
            original = json.loads(row['result'])
        self.assertFalse(any(m.get('text') for m in original.get('messages', [])),
                         'A06: original message observation still contains supposedly forgotten body')

    def test_A07_queue_readiness_matches_recipient_validation(self):
        self.reserve()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_documents SET content='{}' WHERE kind=? AND scope_id=?",
                      (applicant_send.recipient_kind(self.f.intent['intent_id']), self.f.h.context['scope_id']))
        entry = self.dispatch('runtime.applicant.send.queue.get', {})['data']['items'][0]
        self.assertFalse(entry['ready'],
                         'A07: queue cannot show ready when the immutable recipient record is invalid')

    def test_A08_readback_commit_rechecks_draft_after_browser_read(self):
        attempt_id = self.reserve()
        text = self.f.task['result']['text']

        def concurrent_edit(**kwargs):
            with store.runtime_db() as c:
                row = c.execute('SELECT result FROM runtime_tasks WHERE task_id=?',
                                (self.f.intent['draft_task_id'],)).fetchone()
                result = json.loads(row['result'])
                result['text'] = '另一个并发修改后的答复'
                c.execute('UPDATE runtime_tasks SET result=? WHERE task_id=?',
                          (json.dumps(result, ensure_ascii=False), self.f.intent['draft_task_id']))
            return {**snapshot(), 'messages': [*deepcopy(self.f.messages),
                                              message('new-own', text, direction='outgoing')]}

        with mock.patch.object(runtime_tools.applicant_browser, 'read_chat', side_effect=concurrent_edit):
            with self.assertRaises(KernelError, msg='A08: stale evidence must not commit after a draft race'):
                self.dispatch('runtime.applicant.send.readback.commit', {'attempt_id': attempt_id})

    def test_A09_claimed_attempt_is_not_copy_ready_again(self):
        attempt_id = self.reserve()
        self.handoff(attempt_id)
        self.dispatch('runtime.applicant.send.manual.claim', {'attempt_id': attempt_id, 'confirmed': True})
        entry = self.dispatch('runtime.applicant.send.queue.get', {})['data']['items'][0]
        self.assertFalse(entry['ready'], 'A09: already claimed card must not appear actionable again')


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HermesAcceptanceAudit)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    destination = Path(__file__).resolve().parents[1] / 'tmp' / 'commercial-audit' / 'hermes-adversarial-2026-10-27.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({
        'date': '2026-10-27', 'tests': result.testsRun,
        'failures': [test._testMethodName for test, _ in result.failures],
        'errors': [test._testMethodName for test, _ in result.errors],
        'success': result.wasSuccessful(), 'temporary_databases_only': True,
        'real_platform_reads': 0, 'real_platform_writes': 0,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)
