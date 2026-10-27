"""Persisted cancellation must stop socket waits across processes, not retry."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import app
import llm_transport
import main
import store
import test_llm_transport as fixture
from sandbox import KernelError
from task_lease import task_lease


class ModelCancellationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LlmTransportTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '5'
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool.shutdown, wait=True)
        self.task = self.f.h.submit('工作地点工资？', key='cancel-source')
        self.token = self.f.h.context['context_token']

    def start(self, scenario=None):
        self.f.server.scenario = scenario or {'delay_headers': 3}
        def run():
            try:
                return {'response': main.dispatch('runtime.task.execute', {'task_id': self.task['task_id']}, self.token)}
            except KernelError as exc:
                return {'error': exc.code}
        return self.pool.submit(run)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(predicate())

    def cancel(self):
        return main.dispatch('runtime.task.cancel', {'task_id': self.task['task_id']}, self.token)

    def report(self):
        return self.f.h.call('runtime.model.usage.get')

    def result(self, future):
        outcome = future.result(timeout=2)
        self.assertNotIn('error', outcome)
        task = outcome['response']['data']
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(task['error_code'], 'CANCELLED')
        self.assertIsNone(task['result'])
        return outcome['response']

    def test_cancel_during_headers_stops_wait_and_keeps_unknown_charge(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        start = time.monotonic()
        self.assertEqual(self.cancel()['data']['status'], 'cancelled')
        response = self.result(future)
        self.assertLess(time.monotonic() - start, 1.5)
        row = self.report()['items'][0]
        self.assertEqual((row['state'], row['error_code']), ('outcome_unknown', 'CANCELLED'))
        self.assertGreater(row['reserved_units'], 0)
        self.assertFalse(row['usage_known'])
        self.assertEqual(self.f.factory.call_count, 1)
        with store.runtime_db() as c:
            receipt = json.loads(c.execute('SELECT commit_receipt FROM runtime_tasks WHERE task_id=?', (response['trace_id'],)).fetchone()[0])
        self.assertEqual(receipt['result']['status'], 'cancelled')

    def test_cancel_slow_body_with_connection_close(self):
        reading_body = threading.Event()
        factory = self.f.factory.side_effect
        def tracked(timeout):
            connection = factory(timeout)
            getresponse = connection.getresponse
            def response():
                result = getresponse()
                read = result.read1
                def read1(*args):
                    reading_body.set()
                    return read(*args)
                result.read1 = read1
                return result
            connection.getresponse = response
            return connection
        self.f.factory.side_effect = tracked
        future = self.start({'trickle_body': True})
        self.assertTrue(reading_body.wait(2))
        self.cancel()
        self.result(future)
        row = self.report()['items'][0]
        self.assertEqual(row['http_status'], 200)
        self.assertEqual(row['state'], 'outcome_unknown')
        self.assertIsNone(row['total_tokens'])

    def test_cancel_before_connect_does_not_send_or_refund_reservation(self):
        factory = self.f.factory.side_effect
        def stop_before_connect(timeout):
            self.cancel()
            return factory(timeout)
        self.f.factory.side_effect = stop_before_connect
        self.result(self.start())
        self.assertEqual(self.f.server.requests, [])
        row = self.report()['items'][0]
        self.assertEqual(row['state'], 'not_sent')
        self.assertGreater(row['reserved_units'], 0)

    def test_blocked_connect_does_not_release_lease_or_slot_on_logical_cancel(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class BlockedConnection:
            sock = None
            def connect(inner):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError()
            def request(inner, *args, **kwargs):
                raise AssertionError('cancelled connection must not send')
            def close(inner):
                pass
        self.f.factory.side_effect = lambda timeout: BlockedConnection()
        future = self.start()
        self.assertTrue(entered.wait(2))
        self.cancel()
        time.sleep(.1)
        self.assertFalse(future.done())
        with self.assertRaises(KernelError) as busy:
            with task_lease(self.task['task_id']):
                pass
        self.assertEqual(busy.exception.code, 'TASK_BUSY')
        attempt = self.report()['items'][0]
        with self.assertRaises(KernelError) as review_busy:
            self.f.h.call('runtime.model.review', {'attempt_id': attempt['attempt_id'], 'expected_state': 'reserved',
                'note': '等待原执行线程结束。', 'confirmed': True})
        self.assertEqual(review_busy.exception.code, 'TASK_BUSY')
        with task_lease('model-concurrency-slot-1'):
            with self.assertRaises(KernelError) as full:
                with llm_transport.request_slot():
                    pass
        self.assertEqual(full.exception.code, 'LLM_CONCURRENCY_LIMIT')
        release.set()
        self.result(future)
        self.assertEqual(self.report()['items'][0]['state'], 'not_sent')

    def test_cross_process_cancel_interrupts_the_child_socket(self):
        self.f.server.scenario = {'delay_headers': 3}
        script = '''import http.client,json,sys
import main,llm_transport
llm_transport._connection=lambda timeout:http.client.HTTPConnection('127.0.0.1',int(sys.argv[3]),timeout=timeout)
result=main.dispatch('runtime.task.execute',{'task_id':sys.argv[1]},sys.argv[2])
print(json.dumps({'status':result['data']['status'],'code':result['data']['error_code']}))
'''
        env = {**os.environ, 'JOBKIT_DB': str(store.DB_PATH)}
        process = subprocess.Popen([sys.executable, '-c', script, self.task['task_id'], self.token,
            str(self.f.server.server_address[1])], env=env, cwd=Path(__file__).parent,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: bool(self.f.server.requests))
            self.cancel()
            stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout), {'status': 'cancelled', 'code': 'CANCELLED'})
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
        row = self.report()['items'][0]
        self.assertEqual((row['state'], row['error_code']), ('outcome_unknown', 'CANCELLED'))
        self.assertEqual(len(self.f.server.requests), 1)

    def test_context_switch_interrupts_without_exposing_old_result(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        self.f.h.switch(role='applicant', account='other')
        self.assertEqual(future.result(timeout=2), {'error': 'STALE_CONTEXT'})
        self.assertEqual(self.report()['items'], [])
        self.f.h.switch()
        row = self.report()['items'][0]
        self.assertEqual(row['state'], 'outcome_unknown')
        self.assertEqual(row['error_code'], 'STALE_CONTEXT')
        self.assertEqual(self.f.factory.call_count, 1)

    def test_late_cancel_keeps_received_response_usage_but_discards_draft(self):
        finish = store.runtime_model_finish
        def cancel_after_response(context, attempt, **kwargs):
            result = finish(context, attempt, **kwargs)
            if kwargs['state'] == 'response_received':
                self.cancel()
            return result
        body = fixture.completion(usage={'prompt_tokens': 16, 'completion_tokens': 10, 'total_tokens': 26})
        with mock.patch.object(store, 'runtime_model_finish', side_effect=cancel_after_response):
            self.result(self.start({'body': body}))
        row = self.report()['items'][0]
        self.assertEqual(row['state'], 'response_received')
        self.assertEqual(row['total_tokens'], 26)
        self.assertTrue(row['usage_known'])

    def test_cancel_does_not_hide_ledger_failure(self):
        def fail_after_cancel(*args, **kwargs):
            self.cancel()
            raise KernelError('INVALID_MODEL_CALL_STATE', 'private failure')
        with mock.patch.object(store, 'runtime_model_finish', side_effect=fail_after_cancel):
            outcome = self.start({'body': fixture.completion()}).result(timeout=2)
        self.assertEqual(outcome, {'error': 'INVALID_MODEL_CALL_STATE'})
        task = self.f.h.call('runtime.task.get', {'task_id': self.task['task_id']})
        self.assertEqual(task['status'], 'cancelled')
        events = self.f.h.call('runtime.events')
        self.assertTrue(any(e['task_id'] == self.task['task_id'] and e['status'] == 'cancellation_error'
                            and e['code'] == 'INVALID_MODEL_CALL_STATE' for e in events))
        self.assertNotIn('private failure', str(events))
        self.assertEqual(self.report()['items'][0]['state'], 'sending')

    def test_monitor_failure_stops_socket_and_is_explicit(self):
        failed = threading.Event()
        probe = store.runtime_model_interrupt
        def fail_probe(*args, **kwargs):
            if failed.is_set():
                raise OSError('private database path')
            return probe(*args, **kwargs)
        with mock.patch.object(store, 'runtime_model_interrupt', side_effect=fail_probe):
            future = self.start()
            self.wait_for(lambda: bool(self.f.server.requests))
            failed.set()
            self.assertEqual(future.result(timeout=2), {'error': 'MODEL_MONITOR_FAILED'})
        row = self.report()['items'][0]
        self.assertEqual((row['state'], row['error_code']), ('outcome_unknown', 'MODEL_MONITOR_FAILED'))
        self.assertNotIn('private database', str(self.f.h.call('runtime.events')))

    def test_missing_probe_database_is_not_recreated(self):
        missing = Path(self.f.h.tmp.name) / 'missing.db'
        context = {**self.f.h.context, 'task_id': self.task['task_id']}
        self.assertEqual(store.runtime_model_interrupt(context, missing), 'MODEL_MONITOR_FAILED')
        self.assertFalse(missing.exists())

    def test_queued_cancel_never_reserves_or_opens_connection(self):
        self.cancel()
        self.result(self.start())
        self.assertEqual(self.report()['items'], [])
        self.f.factory.assert_not_called()

    def test_completed_task_cannot_be_retroactively_cancelled(self):
        result = self.start({'body': fixture.completion()}).result(timeout=2)['response']['data']
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.cancel()['data'], result)
        self.assertEqual(self.f.factory.call_count, 1)

    def test_http_cancel_requires_session_context_and_records_receipt(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        path = '/api/runtime/tasks/' + self.task['task_id'] + '/cancel'
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post(path).status_code, 401)
            client.get('/workspace')
            self.assertEqual(client.post(path, headers={'X-Jobkit-Context': '0'*32}).status_code, 409)
            self.assertFalse(future.done())
            response = client.post(path, headers={'X-Jobkit-Context': self.token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['data']['status'], 'cancelled')
            self.assertTrue(response.json()['trace_id'])
        self.result(future)

    def test_facts_update_does_not_resurrect_cancelled_task(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        self.cancel()
        self.f.h.save({'location': 'new confirmed place', 'source': 'confirmed'}, revision=1)
        self.result(future)
        self.assertEqual(self.f.h.call('runtime.task.get', {'task_id': self.task['task_id']})['status'], 'cancelled')

    def test_duplicate_cancel_never_repeats_model_or_refunds(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        first, second = self.cancel(), self.cancel()
        self.result(future)
        self.assertEqual(first['data']['status'], second['data']['status'])
        self.assertEqual(self.f.factory.call_count, 1)
        self.assertEqual(self.report()['scope_daily']['attempts'], 1)
        self.assertGreater(self.report()['scope_daily']['reserved_units'], 0)
        self.assertEqual(self.f.h.call('runtime.task.execute', {'task_id': self.task['task_id']})['status'], 'cancelled')

    def test_cancellation_cleanup_releases_both_slots_and_watcher(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        self.cancel()
        self.result(future)
        row = self.report()['items'][0]
        with task_lease('model-concurrency-slot-0'), task_lease('model-concurrency-slot-1'), task_lease(self.task['task_id']):
            pass
        self.assertFalse(any(t.name == 'jobkit-model-watch-' + row['attempt_id'][:8] for t in threading.enumerate()))

    def test_cancel_control_recovers_without_cancelling_or_sending_again(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        cancelled = self.cancel()
        self.result(future)
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL,error_code=NULL WHERE task_id=?", (cancelled['trace_id'],))
        tool = main._router._tools['runtime.task.cancel']
        main._router._tools[tool.name] = replace(tool, handler=mock.Mock(side_effect=AssertionError('no cancel replay')))
        try:
            recovered = self.f.h.call('runtime.task.recover', {'task_id': cancelled['trace_id']})
        finally:
            main._router._tools[tool.name] = tool
        self.assertEqual(recovered['error_code'], 'RECOVERED_COMMIT')
        self.assertEqual(recovered['result'], cancelled['data'])
        self.assertEqual(self.f.factory.call_count, 1)

    def test_unknown_call_can_be_reviewed_only_after_worker_cleanup(self):
        future = self.start()
        self.wait_for(lambda: bool(self.f.server.requests))
        self.cancel()
        self.result(future)
        row = self.report()['items'][0]
        review = self.f.h.call('runtime.model.review', {'attempt_id': row['attempt_id'], 'expected_state': 'outcome_unknown',
            'note': '已取消本地等待，上游结果仍未知；保留额度。', 'confirmed': True})
        self.assertFalse(review['network_outcome_verified'])
        self.assertFalse(review['reservation_refunded'])
        self.assertEqual(self.f.factory.call_count, 1)


if __name__ == '__main__':
    unittest.main()
