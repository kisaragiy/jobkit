"""Human acknowledgement preserves uncertain transport facts and atomic receipts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import app
import main
import runtime_tools
import store
import test_llm_transport as fixture
from sandbox import KernelError
from task_lease import task_lease


class ModelReviewTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LlmTransportTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '.15'
        self.f.server.scenario = {'delay_headers': .5}
        self.f.assert_code('LLM_TIMEOUT')
        self.attempt = self.report()['items'][0]

    def report(self):
        return self.f.h.call('runtime.model.usage.get')

    def payload(self, **changes):
        return {'attempt_id': self.attempt['attempt_id'], 'expected_state': 'outcome_unknown',
                'note': '已核对，仍不能确认供应商结果；不重放。', 'confirmed': True, **changes}

    def review(self, **changes):
        return self.f.h.call('runtime.model.review', self.payload(**changes))

    def count(self):
        with store.runtime_db() as c:
            return c.execute('SELECT count(*) FROM runtime_model_reviews').fetchone()[0]

    def source(self):
        with store.runtime_db() as c:
            return dict(c.execute('SELECT * FROM runtime_model_calls WHERE attempt_id=?', (self.attempt['attempt_id'],)).fetchone())

    def interrupt(self, control_id):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL,error_code=NULL WHERE task_id=?", (control_id,))

    def recover(self, control_id):
        return self.f.h.call('runtime.task.recover', {'task_id': control_id})

    def test_acknowledgement_preserves_transport_task_and_budget(self):
        before = self.source()
        budget = self.report()['scope_daily']
        result = self.review(note='  已核对但仍未知  ')
        self.assertEqual(result['note'], '已核对但仍未知')
        self.assertFalse(result['network_outcome_verified'])
        self.assertFalse(result['reservation_refunded'])
        self.assertFalse(result['automatic_execution'])
        self.assertEqual(self.source(), before)
        report = self.report()
        self.assertEqual(report['scope_daily'], budget)
        item = report['items'][0]
        self.assertTrue(item['pending_or_unknown'])
        self.assertFalse(item['review_required'])
        self.assertEqual(item['review'], result)
        self.assertFalse(item['usage_known'])
        self.assertEqual(self.f.factory.call_count, 1)
        self.assertEqual(self.f.h.call('runtime.task.get', {'task_id': self.attempt['task_id']})['status'], 'blocked')

    def test_same_ack_is_idempotent_and_different_note_conflicts(self):
        first = self.review()
        self.assertEqual(self.review(), first)
        with self.assertRaises(KernelError) as caught:
            self.review(note='a different statement')
        self.assertEqual(caught.exception.code, 'MODEL_REVIEW_CONFLICT')
        self.assertEqual(self.count(), 1)

    def test_invalid_or_unconfirmed_requests_do_not_write(self):
        for changes in [{'confirmed': False}, {'confirmed': 1}, {'expected_state': 'response_received'},
                        {'expected_state': []}, {'note': ''}, {'note': 'x'*501},
                        {'attempt_id': '../file'}, {'refund': True}, {'provider_result': 'paid'}]:
            with self.subTest(changes=changes), self.assertRaises(KernelError) as caught:
                self.review(**changes)
            self.assertEqual(caught.exception.code, 'INVALID_MODEL_REVIEW')
        self.assertEqual(self.count(), 0)

    def test_stale_observed_state_is_rejected(self):
        with self.assertRaises(KernelError) as caught:
            self.review(expected_state='sending')
        self.assertEqual(caught.exception.code, 'STALE_MODEL_CALL')
        self.assertEqual(self.count(), 0)

    def test_known_response_cannot_be_relabelled_as_unknown(self):
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '2'
        self.f.server.scenario = {}
        self.f.execute()
        known = self.report()['items'][0]
        with self.assertRaises(KernelError) as caught:
            self.review(attempt_id=known['attempt_id'])
        self.assertEqual(caught.exception.code, 'STALE_MODEL_CALL')

    def test_live_source_task_lease_refuses_review(self):
        with task_lease(self.attempt['task_id']):
            with self.assertRaises(KernelError) as caught:
                self.review()
        self.assertEqual(caught.exception.code, 'TASK_BUSY')
        self.assertEqual(self.count(), 0)

    def test_abandoned_source_must_be_recovered_first(self):
        self.interrupt(self.attempt['task_id'])
        with self.assertRaises(KernelError) as caught:
            self.review()
        self.assertEqual(caught.exception.code, 'MODEL_TASK_NOT_TERMINAL')
        self.recover(self.attempt['task_id'])
        self.assertEqual(self.review()['disposition'], 'acknowledged_unknown')
        self.assertEqual(self.f.factory.call_count, 1)

    def test_review_is_scope_isolated(self):
        self.f.h.switch(account='other')
        with self.assertRaises(KernelError) as caught:
            self.review()
        self.assertEqual(caught.exception.code, 'MODEL_CALL_NOT_FOUND')
        self.assertEqual(self.report()['items'], [])

    def test_http_guard_and_duplicate_path_id_have_trace(self):
        url = '/api/runtime/model/calls/' + self.attempt['attempt_id'] + '/review'
        body = self.payload()
        body.pop('attempt_id')
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post(url, json=body).status_code, 401)
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.f.h.context['context_token']}
            response = client.post(url, json={**body, 'attempt_id': '0'*32}, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['code'], 'INVALID_MODEL_REVIEW')
            self.assertTrue(response.json()['trace_id'])
            response = client.post(url, json=body, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.count(), 1)
            self.f.h.switch(account='another')
            self.assertEqual(client.post(url, json=body, headers=headers).status_code, 409)

    def test_committed_control_recovers_receipt_without_running_handler(self):
        result = main.dispatch('runtime.model.review', self.payload(), self.f.h.context['context_token'])
        self.interrupt(result['trace_id'])
        tool = main._router._tools['runtime.model.review']
        main._router._tools[tool.name] = replace(tool, handler=mock.Mock(side_effect=AssertionError('no replay')))
        try:
            recovered = self.recover(result['trace_id'])
        finally:
            main._router._tools[tool.name] = tool
        self.assertEqual(recovered['error_code'], 'RECOVERED_COMMIT')
        self.assertEqual(recovered['result'], result['data'])
        self.assertEqual(self.count(), 1)

    def test_corrupt_receipt_cannot_verify_network_or_refund(self):
        result = main.dispatch('runtime.model.review', self.payload(), self.f.h.context['context_token'])
        with store.runtime_db() as c:
            receipt = json.loads(c.execute('SELECT commit_receipt FROM runtime_tasks WHERE task_id=?', (result['trace_id'],)).fetchone()[0])
            receipt['result']['network_outcome_verified'] = True
            c.execute('UPDATE runtime_tasks SET commit_receipt=? WHERE task_id=?', (json.dumps(receipt), result['trace_id']))
        self.interrupt(result['trace_id'])
        recovered = self.recover(result['trace_id'])
        self.assertEqual(recovered['status'], 'outcome_unknown')
        self.assertEqual(recovered['error_code'], 'INVALID_COMMIT_RECEIPT')
        self.assertFalse(self.report()['items'][0]['review']['network_outcome_verified'])

    def test_missing_ack_row_does_not_replay_receipt(self):
        result = main.dispatch('runtime.model.review', self.payload(), self.f.h.context['context_token'])
        with store.runtime_db() as c:
            c.execute('DELETE FROM runtime_model_reviews')
        self.interrupt(result['trace_id'])
        self.assertEqual(self.recover(result['trace_id'])['status'], 'outcome_unknown')
        self.assertEqual(self.count(), 0)

    def test_corrupt_ack_is_not_silently_presented_as_verified(self):
        self.review()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_model_reviews SET note='' WHERE attempt_id=?", (self.attempt['attempt_id'],))
        with self.assertRaises(KernelError) as caught:
            self.report()
        self.assertEqual(caught.exception.code, 'INVALID_MODEL_REVIEW_STATE')
        self.assertEqual(self.count(), 1)

    def test_corrupt_source_and_control_links_prevent_receipt_recovery(self):
        result = main.dispatch('runtime.model.review', self.payload(), self.f.h.context['context_token'])
        with store.runtime_db() as c:
            owner = c.execute('SELECT control_task_id FROM runtime_model_reviews').fetchone()[0]
        for field, value, original in [('task_id', '0'*32, self.attempt['task_id']), ('control_task_id', '0'*32, owner)]:
            with self.subTest(field=field):
                with store.runtime_db() as c:
                    c.execute('UPDATE runtime_model_reviews SET ' + field + '=?', (value,))
                self.interrupt(result['trace_id'])
                recovered = self.recover(result['trace_id'])
                self.assertEqual(recovered['status'], 'outcome_unknown')
                self.assertEqual(recovered['error_code'], 'INVALID_MODEL_REVIEW_STATE')
                self.assertIsNone(recovered['result'])
                with store.runtime_db() as c:
                    c.execute('UPDATE runtime_model_reviews SET ' + field + '=?', (original,))

    def test_receipt_failure_rolls_back_ack_and_event(self):
        with mock.patch.object(runtime_tools, 'record_commit', side_effect=RuntimeError('private error')):
            with self.assertRaises(KernelError) as caught:
                self.review()
        self.assertEqual(caught.exception.code, 'TOOL_FAILED')
        self.assertEqual(self.count(), 0)
        events = self.f.h.call('runtime.events')
        self.assertFalse(any(e['status'] == 'model_review_acknowledged' for e in events))
        self.assertNotIn('private error', str(events))

    def test_concurrent_ack_has_one_immutable_result(self):
        barrier = threading.Barrier(2)
        def acknowledge(_):
            barrier.wait(timeout=3)
            try:
                return self.review()
            except KernelError as exc:
                self.assertEqual(exc.code, 'TASK_BUSY')
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [item for item in pool.map(acknowledge, range(2)) if item is not None]
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(self.count(), 1)

    def test_real_crash_before_and_after_commit_never_replays(self):
        script = '''import os,sys
import main,runtime_tools
original=runtime_tools.record_commit
def checkpoint(c,context,tool,result):
    value=original(c,context,tool,result)
    if tool=='runtime.model.review':
        if sys.argv[3]=='before': os._exit(31)
        c.commit()
        os._exit(32)
    return value
runtime_tools.record_commit=checkpoint
main.dispatch('runtime.model.review',__import__('json').loads(sys.argv[1]),sys.argv[2])
'''
        source = self.source()
        for mode in ('before', 'after'):
            with self.subTest(mode=mode):
                env = {**os.environ, 'JOBKIT_DB': str(store.DB_PATH)}
                child = subprocess.run([sys.executable, '-c', script, json.dumps(self.payload()),
                    self.f.h.context['context_token'], mode], env=env, cwd=Path(__file__).parent,
                    capture_output=True, timeout=15)
                self.assertEqual(child.returncode, 31 if mode == 'before' else 32)
                with store.runtime_db() as c:
                    control = dict(c.execute("SELECT * FROM runtime_tasks WHERE tool='runtime.model.review' ORDER BY created_at DESC LIMIT 1").fetchone())
                recovered = self.recover(control['task_id'])
                self.assertEqual(self.count(), 0 if mode == 'before' else 1)
                self.assertEqual(recovered['status'], 'outcome_unknown' if mode == 'before' else 'completed')
                if mode == 'after':
                    self.assertEqual(recovered['error_code'], 'RECOVERED_COMMIT')
                self.assertEqual(self.source(), source)
        self.assertEqual(self.f.factory.call_count, 1)


if __name__ == '__main__':
    unittest.main()
