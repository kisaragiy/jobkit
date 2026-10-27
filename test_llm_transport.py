"""Actual loopback HTTP protocol checks; production destination never contacted."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ssl
import threading
import time
import unittest
from unittest import mock

import llm_transport
import test_runtime_kernel as fixture
from sandbox import KernelError


def completion(content='{"selection":["location","salary"]}', **overrides):
    value = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}]}
    value.update(overrides)
    return json.dumps(value).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        scenario = self.server.scenario.copy()
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.requests.append({'path': self.path, 'headers': self.headers, 'body': json.loads(body)})
        try:
            time.sleep(scenario.get('delay_headers', 0))
            if scenario.get('trickle_headers'):
                for chunk in (b'HTTP/1.1 200 OK\r\n', b'Content-Type: application/json\r\n', b'Connection: close\r\n', b'\r\n'):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(.08)
            else:
                self.send_response(scenario.get('status', 200))
                self.send_header('Content-Type', scenario.get('content_type', 'application/json; charset=utf-8'))
                self.send_header('Connection', 'close')
                if not scenario.get('no_length'):
                    self.send_header('Content-Length', str(scenario.get('length', len(scenario.get('body', completion())))))
                for key, value in scenario.get('headers', []):
                    self.send_header(key, value)
                self.end_headers()
            payload = scenario.get('body', completion())
            if scenario.get('trickle_body'):
                for index in range(0, len(payload), 4):
                    self.wfile.write(payload[index:index + 4])
                    self.wfile.flush()
                    time.sleep(.04)
            else:
                self.wfile.write(payload)
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            self.close_connection = True


class LlmTransportTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {'JOBKIT_DRAFT_BACKEND': 'llm',
            'JOBKIT_LLM_TIMEOUT_SECONDS': '2', 'JOBKIT_LLM_PROVIDER': 'deepseek',
            'JOBKIT_LLM_MODEL': 'deepseek-flash', 'JOBKIT_LLM_API_KEY': 'fictional-test-key-0000',
            'JOBKIT_LLM_EXTERNAL_ALLOWED': 'yes', 'JOBKIT_LLM_DAILY_REQUEST_LIMIT': '100',
            'JOBKIT_LLM_DAILY_RESERVATION_LIMIT': '1000000'})
        env.start()
        self.addCleanup(env.stop)
        self.h = fixture.RuntimeKernelTest()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.h.switch()
        self.h.save({'location': '虚构园区', 'salary': '8-10K', 'source': '虚构来源'})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.requests = []
        self.server.scenario = {}
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=1)
        self.addCleanup(cleanup)
        self.original_factory = llm_transport._connection
        patch = mock.patch.object(llm_transport, '_connection', side_effect=lambda timeout: http.client.HTTPConnection(
            '127.0.0.1', self.server.server_address[1], timeout=timeout))
        self.factory = patch.start()
        self.addCleanup(patch.stop)
        self.counter = 0

    def execute(self):
        self.counter += 1
        task = self.h.submit('工作地点工资？', key=f'transport-{self.counter}')
        try:
            return self.h.call('runtime.task.execute', {'task_id': task['task_id']})
        except KernelError as error:
            result = self.h.call('runtime.task.get', {'task_id': task['task_id']})
            self.assertEqual(error.code, result['error_code'])
            return result

    def assert_code(self, expected):
        result = self.execute()
        self.assertEqual(result['error_code'], expected)
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['result'])
        return result

    def test_real_http_exchange_uses_minimal_request_and_local_facts(self):
        result = self.execute()
        self.assertEqual(result['result']['text'], '虚构园区\n8-10K')
        self.assertFalse(result['result']['sent'])
        self.assertEqual(len(self.server.requests), 1)
        sent = self.server.requests[0]
        self.assertEqual(sent['path'], '/chat/completions')
        self.assertEqual(sent['headers']['Authorization'], 'Bearer fictional-test-key-0000')
        body = sent['body']
        self.assertEqual(body['model'], 'deepseek-flash')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['max_tokens'], 256)
        self.assertFalse(body['stream'])
        self.assertNotIn('tools', body)
        self.assertNotIn('虚构来源', str(body))
        self.assertNotIn('fictional-test-key', str(body))

    def test_external_permission_is_required_before_socket_creation(self):
        os.environ['JOBKIT_LLM_EXTERNAL_ALLOWED'] = 'no'
        self.assert_code('LLM_EXTERNAL_NOT_ALLOWED')
        self.factory.assert_not_called()

    def test_invalid_provider_model_and_secret_never_create_socket(self):
        for key, value in [('JOBKIT_LLM_PROVIDER', 'other'), ('JOBKIT_LLM_MODEL', 'unknown'),
                           ('JOBKIT_LLM_API_KEY', ''), ('JOBKIT_LLM_API_KEY', 'bad\r\nInjected: header'),
                           ('JOBKIT_LLM_API_KEY', 'a' * 513)]:
            with self.subTest(key=key, value=value), mock.patch.dict(os.environ, {key: value}):
                self.assert_code('LLM_INVALID_CONFIG')
        self.factory.assert_not_called()

    def test_fixed_destination_uses_tls_verification_and_ignores_proxy_environment(self):
        with mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:1', 'JOBKIT_LLM_BASE_URL': 'http://127.0.0.1:1'}):
            with mock.patch.object(http.client, 'HTTPSConnection') as constructor:
                self.original_factory(3)
                self.assertEqual(constructor.call_args.args, ('api.deepseek.com',))
                self.assertEqual(constructor.call_args.kwargs['port'], 443)
                self.assertEqual(constructor.call_args.kwargs['timeout'], 3)
                context = constructor.call_args.kwargs['context']
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(llm_transport.HOST, 'api.deepseek.com')
        self.assertEqual(llm_transport.PATH, '/chat/completions')

    def test_redirects_are_not_followed_or_retried(self):
        self.server.scenario = {'status': 302, 'headers': [('Location', 'https://example.invalid/steal')]}
        self.assert_code('LLM_PROVIDER_FAILED')
        self.assertEqual(len(self.server.requests), 1)

    def test_auth_rate_and_server_errors_are_sanitized_without_retry(self):
        for status, code in [(401, 'LLM_AUTH_FAILED'), (403, 'LLM_AUTH_FAILED'),
                             (429, 'LLM_RATE_LIMITED'), (503, 'LLM_PROVIDER_FAILED')]:
            self.server.scenario = {'status': status, 'body': b'private vendor error and key'}
            self.assert_code(code)
        self.assertEqual(len(self.server.requests), 4)
        self.assertNotIn('private vendor', str(self.h.call('runtime.events')))

    def test_declared_oversize_is_refused(self):
        self.server.scenario = {'length': 65537}
        self.assert_code('LLM_RESPONSE_TOO_LARGE')

    def test_streamed_oversize_without_length_is_refused(self):
        self.server.scenario = {'no_length': True, 'body': b'x' * 65537}
        self.assert_code('LLM_RESPONSE_TOO_LARGE')

    def test_incomplete_response_is_refused(self):
        self.server.scenario = {'length': len(completion()) + 10}
        self.assert_code('LLM_INVALID_OUTPUT')

    def test_duplicate_or_conflicting_framing_is_refused(self):
        for headers in [[('Content-Length', '1')], [('Transfer-Encoding', 'chunked')]]:
            self.server.scenario = {'headers': headers}
            self.assert_code('LLM_INVALID_OUTPUT')

    def test_compression_and_non_json_are_refused(self):
        for scenario in [{'headers': [('Content-Encoding', 'gzip')]}, {'content_type': 'text/html'}]:
            self.server.scenario = scenario
            self.assert_code('LLM_INVALID_OUTPUT')

    def test_malformed_utf8_and_duplicate_envelope_are_refused(self):
        for body in [b'\xff', b'{}', b'null', b'{"choices":[],"choices":[]}', b'{"choices":NaN}']:
            self.server.scenario = {'body': body}
            self.assert_code('LLM_INVALID_OUTPUT')

    def test_truncated_completion_tool_calls_and_empty_text_are_refused(self):
        for choice in [
            {'finish_reason': 'length', 'message': {'role': 'assistant', 'content': '{}'}},
            {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}', 'tool_calls': [{}]}},
            {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': ''}},
            {'finish_reason': 'stop', 'message': {'role': 'user', 'content': '{}'}},
        ]:
            self.server.scenario = {'body': json.dumps({'choices': [choice]}).encode()}
            self.assert_code('LLM_INVALID_OUTPUT')

    def test_invalid_selection_is_refused_after_valid_http_envelope(self):
        self.server.scenario = {'body': completion('{"selection":["made_up"]}')}
        self.assert_code('LLM_INVALID_OUTPUT')

    def test_delayed_headers_are_interrupted(self):
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '.15'
        self.server.scenario = {'delay_headers': .6}
        start = time.monotonic()
        self.assert_code('LLM_TIMEOUT')
        self.assertLess(time.monotonic() - start, .9)

    def test_header_trickle_cannot_extend_deadline(self):
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '.15'
        self.server.scenario = {'trickle_headers': True}
        self.assert_code('LLM_TIMEOUT')

    def test_body_trickle_cannot_extend_deadline_when_connection_is_closing(self):
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '.15'
        self.server.scenario = {'trickle_body': True}
        start = time.monotonic()
        self.assert_code('LLM_TIMEOUT')
        self.assertLess(time.monotonic() - start, .9)

    def test_connection_error_does_not_expose_details(self):
        self.factory.side_effect = OSError('private key and internal endpoint')
        self.assert_code('LLM_PROVIDER_FAILED')
        self.assertNotIn('private key', str(self.h.call('runtime.events')))

    def test_public_binding_changes_for_model_and_permission_but_excludes_secret(self):
        original = llm_transport.binding()
        with mock.patch.dict(os.environ, {'JOBKIT_LLM_API_KEY': 'rotated-test-key-0000'}):
            self.assertEqual(original, llm_transport.binding())
        with mock.patch.dict(os.environ, {'JOBKIT_LLM_MODEL': 'deepseek-v4-pro'}):
            self.assertNotEqual(original, llm_transport.binding())
        self.assertNotIn('fictional-test-key', repr(llm_transport.settings()))

    def test_queue_cannot_send_to_changed_model(self):
        task = self.h.submit()
        os.environ['JOBKIT_LLM_MODEL'] = 'deepseek-v4-pro'
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.task.execute', {'task_id': task['task_id']})
        self.assertEqual(caught.exception.code, 'LLM_CONFIG_CHANGED')
        self.factory.assert_not_called()

    def test_legacy_model_task_without_destination_binding_cannot_send(self):
        import store
        task = self.h.submit()
        with store.runtime_db() as c:
            payload = json.loads(c.execute('SELECT payload FROM runtime_tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0])
            payload.pop('llm_binding')
            c.execute('UPDATE runtime_tasks SET payload=? WHERE task_id=?', (json.dumps(payload), task['task_id']))
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.task.execute', {'task_id': task['task_id']})
        self.assertEqual(caught.exception.code, 'LLM_CONFIG_CHANGED')
        self.factory.assert_not_called()

    def test_request_limit_prevents_socket_creation(self):
        with mock.patch.object(llm_transport, 'MAX_REQUEST_BYTES', 1):
            self.assert_code('LLM_REQUEST_TOO_LARGE')
        self.factory.assert_not_called()

    def test_direct_transport_requires_registered_capability(self):
        with self.assertRaises(KernelError) as caught:
            llm_transport.complete({}, timeout_seconds=1)
        self.assertEqual(caught.exception.code, 'KERNEL_REQUIRED')
        self.factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
