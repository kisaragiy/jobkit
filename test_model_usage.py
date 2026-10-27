"""Budget reservations, scoped accounting and cross-process OS concurrency."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

from fastapi.testclient import TestClient
import app
import llm_transport
import main
import store
import test_llm_transport as fixture
from sandbox import KernelError
from task_lease import task_lease


class ModelUsageTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LlmTransportTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def report(self, cursor=None):
        return self.f.h.call('runtime.model.usage.get', {'cursor': cursor} if cursor else {})

    def rows(self):
        with store.runtime_db() as c:
            return [dict(row) for row in c.execute('SELECT * FROM runtime_model_calls ORDER BY created_at')]

    def test_success_correlates_task_provider_usage_and_events_without_secrets(self):
        provider_id = '930c60df-bf64-41c9-a88e-3ec75f81e00e'
        self.f.server.scenario = {'body': fixture.completion(id=provider_id,
            usage={'prompt_tokens': 16, 'completion_tokens': 10, 'total_tokens': 26})}
        task = self.f.execute()
        report = self.report()
        row = report['items'][0]
        self.assertEqual(row['task_id'], task['task_id'])
        self.assertEqual(row['provider_request_id'], provider_id)
        self.assertEqual(row['state'], 'response_received')
        self.assertEqual(row['total_tokens'], 26)
        self.assertTrue(row['usage_known'])
        self.assertEqual(report['scope_daily']['reported_tokens'], 26)
        self.assertGreater(report['scope_daily']['reserved_units'], 26)
        self.assertFalse(report['reported_usage_is_billing_proof'])
        self.assertNotIn('context_token', row)
        self.assertNotIn('fictional-test-key', json.dumps(self.rows()))
        self.assertNotIn('虚构园区', json.dumps(self.rows(), ensure_ascii=False))
        statuses = [e['status'] for e in self.f.h.call('runtime.events') if e['task_id'] == task['task_id']]
        self.assertIn('model_reserved', statuses)
        self.assertIn('model_sending', statuses)
        self.assertIn('model_response_received', statuses)

    def test_missing_usage_is_unknown_never_zero_per_attempt(self):
        self.f.execute()
        report = self.report()
        self.assertIsNone(report['items'][0]['total_tokens'])
        self.assertFalse(report['items'][0]['usage_known'])
        self.assertEqual(report['scope_daily']['usage_unknown_attempts'], 1)
        self.assertEqual(report['scope_daily']['usage_known_attempts'], 0)

    def test_invalid_usage_is_explicitly_unknown(self):
        for usage in [None, {}, {'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2},
                      {'prompt_tokens': -1, 'completion_tokens': 1, 'total_tokens': 0},
                      {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 3},
                      {'prompt_tokens': 1.5, 'completion_tokens': 1, 'total_tokens': 2.5},
                      {'prompt_tokens': 1000000001, 'completion_tokens': 1, 'total_tokens': 1000000002}]:
            self.f.server.scenario = {'body': fixture.completion(usage=usage)}
            self.assertEqual(self.f.execute()['status'], 'completed')
        self.assertTrue(all(row['total_tokens'] is None for row in self.rows()))
        self.assertEqual(self.report()['scope_daily']['usage_unknown_attempts'], 7)

    def test_daily_request_cap_is_checked_before_socket_creation(self):
        os.environ['JOBKIT_LLM_DAILY_REQUEST_LIMIT'] = '1'
        self.f.execute()
        self.f.assert_code('LLM_BUDGET_EXCEEDED')
        self.assertEqual(self.f.factory.call_count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_reservation_cap_is_not_refunded_for_reported_small_usage(self):
        self.f.server.scenario = {'body': fixture.completion(usage={'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2})}
        self.f.execute()
        units = self.rows()[0]['reserved_units']
        os.environ['JOBKIT_LLM_DAILY_RESERVATION_LIMIT'] = str(units)
        self.f.assert_code('LLM_BUDGET_EXCEEDED')
        self.assertEqual(self.report()['scope_daily']['reserved_units'], units)
        self.assertEqual(self.f.factory.call_count, 1)

    def test_unsent_connection_failure_keeps_reservation(self):
        os.environ['JOBKIT_LLM_DAILY_REQUEST_LIMIT'] = '1'
        self.f.factory.side_effect = OSError('private endpoint')
        self.f.assert_code('LLM_PROVIDER_FAILED')
        self.assertEqual(self.rows()[0]['state'], 'not_sent')
        self.f.assert_code('LLM_BUDGET_EXCEEDED')
        self.assertEqual(self.f.factory.call_count, 1)

    def test_timeout_after_send_is_unknown_and_keeps_reservation(self):
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '.15'
        os.environ['JOBKIT_LLM_DAILY_REQUEST_LIMIT'] = '1'
        self.f.server.scenario = {'delay_headers': .5}
        self.f.assert_code('LLM_TIMEOUT')
        row = self.report()['items'][0]
        self.assertEqual(row['state'], 'outcome_unknown')
        self.assertTrue(row['pending_or_unknown'])
        self.f.assert_code('LLM_BUDGET_EXCEEDED')

    def test_http_error_records_status_without_claiming_zero_usage(self):
        self.f.server.scenario = {'status': 429}
        self.f.assert_code('LLM_RATE_LIMITED')
        row = self.report()['items'][0]
        self.assertEqual((row['state'], row['http_status']), ('response_error', 429))
        self.assertFalse(row['usage_known'])

    def test_even_requeued_corrupt_task_cannot_send_a_second_attempt(self):
        task = self.f.execute()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='queued',result=NULL WHERE task_id=?", (task['task_id'],))
        with self.assertRaises(KernelError) as caught:
            self.f.h.call('runtime.task.execute', {'task_id': task['task_id']})
        self.assertEqual(caught.exception.code, 'LLM_ALREADY_ATTEMPTED')
        self.assertEqual(self.f.factory.call_count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_both_os_slots_busy_rejects_without_reservation(self):
        with task_lease('model-concurrency-slot-0'), task_lease('model-concurrency-slot-1'):
            self.f.assert_code('LLM_CONCURRENCY_LIMIT')
        self.assertEqual(self.rows(), [])
        self.f.factory.assert_not_called()

    def test_one_busy_slot_uses_the_other(self):
        with task_lease('model-concurrency-slot-0'):
            self.assertEqual(self.f.execute()['status'], 'completed')

    def test_cross_process_slots_release_on_process_exit(self):
        ready = Path(self.f.h.tmp.name) / 'slot-ready'
        script = '''import time,sys
from pathlib import Path
from task_lease import task_lease
with task_lease('model-concurrency-slot-0'), task_lease('model-concurrency-slot-1'):
    Path(sys.argv[1]).write_text('ready')
    time.sleep(30)
'''
        env = {**os.environ, 'JOBKIT_DB': str(store.DB_PATH)}
        process = subprocess.Popen([sys.executable, '-c', script, str(ready)], env=env, cwd=Path(__file__).parent,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            self.f.assert_code('LLM_CONCURRENCY_LIMIT')
            self.assertEqual(self.rows(), [])
        finally:
            process.terminate()
            process.communicate(timeout=5)
        self.assertEqual(self.f.execute()['status'], 'completed')

    def test_budget_is_global_while_reports_are_scoped(self):
        os.environ['JOBKIT_LLM_DAILY_REQUEST_LIMIT'] = '1'
        self.f.execute()
        original = self.report()['items'][0]
        self.f.h.switch(account='second-account')
        self.f.h.save({'location': 'other', 'salary': 'other', 'source': 'confirmed'})
        self.f.assert_code('LLM_BUDGET_EXCEEDED')
        self.assertEqual(self.report()['scope_daily']['attempts'], 0)
        self.assertEqual(self.report()['items'], [])
        with self.assertRaises(KernelError) as caught:
            self.report(original['attempt_id'])
        self.assertEqual(caught.exception.code, 'INVALID_MODEL_CURSOR')

    def test_keyset_pagination_has_no_duplicates(self):
        context = self.f.h.context
        with store.runtime_db() as c:
            for index in range(51):
                c.execute('INSERT INTO runtime_model_calls(attempt_id,task_id,scope_id,context_token,provider,model,config_binding,budget_day,reserved_units,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, context['scope_id'], context['context_token'], 'deepseek', 'deepseek-flash', '0'*64, store.runtime_model_day(), 1, 'reserved', index, index))
        first = self.report()
        second = self.report(first['next_cursor'])
        ids = [row['attempt_id'] for row in first['items'] + second['items']]
        self.assertEqual(len(set(ids)), 51)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(first['scope_daily']['reserved_units'], 51)

    def test_http_report_requires_session_context_and_valid_cursor(self):
        self.f.execute()
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.get('/api/runtime/model/usage').status_code, 401)
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.f.h.context['context_token']}
            result = client.get('/api/runtime/model/usage', headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json()['trace_id'])
            self.assertNotIn('fictional-test-key', result.text)
            self.assertEqual(client.get('/api/runtime/model/usage?cursor=bad', headers=headers).status_code, 400)
            self.f.h.switch(account='new-scope')
            self.assertEqual(client.get('/api/runtime/model/usage', headers=headers).status_code, 409)

    def test_invalid_limits_fail_before_reservation(self):
        for name in ('JOBKIT_LLM_DAILY_REQUEST_LIMIT', 'JOBKIT_LLM_DAILY_RESERVATION_LIMIT'):
            for value in ('0', '-1', '1.5', 'nan', '10000000000'):
                with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value}):
                    self.f.assert_code('LLM_INVALID_CONFIG')
        self.assertEqual(self.rows(), [])
        self.f.factory.assert_not_called()

    def test_concurrent_reservations_cannot_overspend_daily_cap(self):
        os.environ['JOBKIT_LLM_DAILY_REQUEST_LIMIT'] = '1'
        tasks = [self.f.h.submit('工作地点工资？', key=f'race-{index}') for index in range(3)]
        barrier = threading.Barrier(3)
        def execute(task):
            barrier.wait(timeout=3)
            try:
                return self.f.h.call('runtime.task.execute', {'task_id': task['task_id']})['status']
            except KernelError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(execute, tasks))
        self.assertEqual(results.count('completed'), 1)
        self.assertTrue(all(value in ('completed', 'LLM_BUDGET_EXCEEDED', 'LLM_CONCURRENCY_LIMIT') for value in results))
        self.assertEqual(len(self.f.server.requests), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_real_process_crash_reservations_are_retained_without_replay(self):
        script = '''import os,sys
import main,llm_transport
ctx=main.dispatch('runtime.context')['data']
def call(tool,payload): return main.dispatch(tool,payload,ctx['context_token'])['data']
call('runtime.document.save',{'content':{'location':'fictional','source':'confirmed'},'expected_revision':0,'confirmed':True})
task=call('runtime.task.submit',{'tool':'draft.reply','input':{'message':'工作地点？'},'idempotency_key':'crash'})
class Connection:
    sock=None
    def connect(self): pass
    def request(self,*args,**kwargs): os._exit(47)
    def close(self): pass
def factory(timeout):
    if sys.argv[1]=='reserved': os._exit(47)
    return Connection()
llm_transport._connection=factory
call('runtime.task.execute',{'task_id':task['task_id']})
'''
        for state in ('reserved', 'sending'):
            with self.subTest(state=state):
                path = Path(self.f.h.tmp.name) / (state + '.db')
                env = {**os.environ, 'JOBKIT_DB': str(path)}
                result = subprocess.run([sys.executable, '-c', script, state], env=env, cwd=Path(__file__).parent,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 47)
                with mock.patch.object(store, 'DB_PATH', path):
                    context = main.dispatch('runtime.context')['data']
                    with store.runtime_db() as c:
                        row = dict(c.execute('SELECT * FROM runtime_model_calls').fetchone())
                    self.assertEqual(row['state'], state)
                    self.assertGreater(row['reserved_units'], 0)
                    recovered = main.dispatch('runtime.task.recover', {'task_id': row['task_id']}, context['context_token'])['data']
                    self.assertEqual(recovered['status'], 'failed')
                    main.dispatch('runtime.task.execute', {'task_id': row['task_id']}, context['context_token'])
                    report = main.dispatch('runtime.model.usage.get', {}, context['context_token'])['data']
                    self.assertTrue(report['items'][0]['pending_or_unknown'])
                    self.assertEqual(report['scope_daily']['attempts'], 1)
        self.f.factory.assert_not_called()

    def test_new_utc_day_gets_new_quota_without_erasing_old_records(self):
        os.environ['JOBKIT_LLM_DAILY_REQUEST_LIMIT'] = '1'
        self.f.execute()
        with mock.patch.object(store, 'runtime_model_day', return_value='2099-01-01'):
            self.assertEqual(self.f.execute()['status'], 'completed')
            self.assertEqual(self.report()['scope_daily']['attempts'], 1)
        self.assertEqual(len(self.rows()), 2)

    def test_facts_change_during_connect_prevents_sending(self):
        factory = self.f.factory.side_effect
        def changed(timeout):
            connection = factory(timeout)
            connect = connection.connect
            def connect_and_change():
                connect()
                self.f.h.save({'location': 'new', 'source': 'confirmed'}, revision=1)
            connection.connect = connect_and_change
            return connection
        self.f.factory.side_effect = changed
        self.f.assert_code('STALE_FACTS')
        self.assertEqual(len(self.f.server.requests), 0)
        self.assertEqual(self.rows()[0]['state'], 'not_sent')

    def test_permission_change_during_connect_prevents_sending(self):
        factory = self.f.factory.side_effect
        def changed(timeout):
            connection = factory(timeout)
            connect = connection.connect
            def connect_and_revoke():
                connect()
                os.environ['JOBKIT_LLM_EXTERNAL_ALLOWED'] = 'no'
            connection.connect = connect_and_revoke
            return connection
        self.f.factory.side_effect = changed
        self.f.assert_code('LLM_CONFIG_CHANGED')
        self.assertEqual(len(self.f.server.requests), 0)
        self.assertEqual(self.rows()[0]['state'], 'not_sent')

    def test_registered_rules_task_cannot_implicitly_call_model(self):
        os.environ['JOBKIT_DRAFT_BACKEND'] = 'rules'
        with mock.patch('recruiter_reply.draft', side_effect=lambda *args: llm_transport.complete({}, timeout_seconds=1)):
            self.f.assert_code('LLM_CONFIG_CHANGED')
        self.f.factory.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_provider_cannot_echo_key_into_request_identifier(self):
        secret = 'abcdef0123456789abcdef0123456789'
        os.environ['JOBKIT_LLM_API_KEY'] = secret
        self.f.server.scenario = {'body': fixture.completion(id=secret)}
        self.f.execute()
        self.assertIsNone(self.report()['items'][0]['provider_request_id'])
        self.assertNotIn(secret, json.dumps(self.rows()))

    def test_ledger_finish_failure_does_not_return_reply_or_refund(self):
        os.environ['JOBKIT_LLM_DAILY_REQUEST_LIMIT'] = '1'
        with mock.patch.object(store, 'runtime_model_finish', side_effect=KernelError('INVALID_MODEL_CALL_STATE', 'failure')):
            self.f.assert_code('INVALID_MODEL_CALL_STATE')
        self.assertEqual(self.rows()[0]['state'], 'sending')
        self.assertTrue(self.report()['items'][0]['pending_or_unknown'])
        self.f.assert_code('LLM_BUDGET_EXCEEDED')


if __name__ == '__main__':
    unittest.main()
