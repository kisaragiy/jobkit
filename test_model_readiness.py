"""Configuration inspection must never probe a network or reserve usage."""
import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import app
import llm_transport
import store
import test_runtime_kernel as fixture
from sandbox import KernelError


class ModelReadinessTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {'JOBKIT_DRAFT_BACKEND': 'rules', 'JOBKIT_LLM_TIMEOUT_SECONDS': '15',
            'JOBKIT_LLM_PROVIDER': '', 'JOBKIT_LLM_MODEL': '', 'JOBKIT_LLM_API_KEY': '',
            'JOBKIT_LLM_EXTERNAL_ALLOWED': 'no', 'JOBKIT_LLM_DAILY_REQUEST_LIMIT': '100',
            'JOBKIT_LLM_DAILY_RESERVATION_LIMIT': '1000000'})
        patch.start()
        self.addCleanup(patch.stop)
        self.h = fixture.RuntimeKernelTest()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        patch = mock.patch.object(llm_transport, '_connection', side_effect=AssertionError('must not connect'))
        self.connection = patch.start()
        self.addCleanup(patch.stop)

    def ready(self, payload=None):
        report = self.h.call('runtime.model.readiness.get', payload)
        self.connection.assert_not_called()
        with store.runtime_db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM runtime_model_calls').fetchone()[0], 0)
        return report

    def configure(self):
        os.environ.update({'JOBKIT_DRAFT_BACKEND': 'llm', 'JOBKIT_LLM_PROVIDER': 'deepseek',
            'JOBKIT_LLM_MODEL': 'deepseek-flash', 'JOBKIT_LLM_API_KEY': 'fictional-readiness-key',
            'JOBKIT_LLM_EXTERNAL_ALLOWED': 'yes'})

    def test_default_rules_is_not_reported_as_ready_for_cloud(self):
        report = self.ready()
        self.assertFalse(report['configuration_ready'])
        self.assertEqual(report['backend'], 'rules')
        self.assertIn('RULES_MODE_SELECTED', report['issues'])
        self.assertIn('LLM_NOT_CONFIGURED', report['issues'])
        self.assertFalse(report['credential_configured'])

    def test_configured_is_configuration_only_not_network_verification(self):
        self.configure()
        report = self.ready()
        self.assertTrue(report['configuration_ready'])
        for key in ('network_checked', 'account_verified', 'quota_checked', 'concurrency_checked', 'automatic_execution'):
            self.assertFalse(report[key])
        text = json.dumps(report)
        self.assertNotIn('fictional-readiness-key', text)
        self.assertNotIn('api_key', text)
        with store.runtime_db() as c:
            values = ' '.join(row[0] for row in c.execute("SELECT coalesce(result,'') FROM runtime_tasks"))
        self.assertNotIn('fictional-readiness-key', values)

    def test_invalid_provider_and_model_values_are_not_echoed(self):
        self.configure()
        os.environ.update({'JOBKIT_LLM_PROVIDER': 'private-untrusted-name', 'JOBKIT_LLM_MODEL': 'private-model-value'})
        report = self.ready()
        self.assertFalse(report['configuration_ready'])
        self.assertIsNone(report['provider'])
        self.assertIsNone(report['model'])
        self.assertNotIn('private-', json.dumps(report))

    def test_permission_is_separate_from_valid_credentials(self):
        self.configure()
        os.environ['JOBKIT_LLM_EXTERNAL_ALLOWED'] = 'no'
        report = self.ready()
        self.assertFalse(report['configuration_ready'])
        self.assertTrue(report['credential_configured'])
        self.assertIn('LLM_EXTERNAL_NOT_ALLOWED', report['issues'])

    def test_bad_timeout_and_budget_have_stable_nonsecret_issues(self):
        self.configure()
        for name in ('JOBKIT_LLM_TIMEOUT_SECONDS', 'JOBKIT_LLM_DAILY_REQUEST_LIMIT', 'JOBKIT_LLM_DAILY_RESERVATION_LIMIT'):
            with mock.patch.dict(os.environ, {name: 'private-invalid-value'}):
                report = self.ready()
                self.assertFalse(report['configuration_ready'])
                self.assertIn('LLM_INVALID_CONFIG', report['issues'])
                self.assertNotIn('private-invalid-value', json.dumps(report))

    def test_readiness_rejects_override_inputs_through_kernel(self):
        with self.assertRaises(KernelError) as caught:
            self.ready({'api_key': 'fake-overridden-key'})
        self.assertEqual(caught.exception.code, 'INVALID_MODEL_READINESS')
        self.assertTrue(caught.exception.trace_id)
        self.connection.assert_not_called()

    def test_http_requires_session_and_current_context(self):
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.get('/api/runtime/model/readiness').status_code, 401)
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.h.context['context_token']}
            response = client.get('/api/runtime/model/readiness', headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['trace_id'])
            self.h.switch()
            self.assertEqual(client.get('/api/runtime/model/readiness', headers=headers).status_code, 409)
        self.connection.assert_not_called()

    def test_report_uses_one_configuration_snapshot(self):
        self.configure()
        settings = llm_transport.settings
        def changed(snapshot):
            result = settings(snapshot)
            os.environ['JOBKIT_LLM_MODEL'] = 'private-new-model'
            os.environ['JOBKIT_LLM_API_KEY'] = ''
            return result
        with mock.patch.object(llm_transport, 'settings', side_effect=changed):
            report = self.ready()
        self.assertTrue(report['configuration_ready'])
        self.assertEqual(report['model'], 'deepseek-flash')
        self.assertTrue(report['credential_configured'])


if __name__ == '__main__':
    unittest.main()
