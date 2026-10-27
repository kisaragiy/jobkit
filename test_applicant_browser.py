"""Dedicated browser setup is explicit, scoped and never imports login cookies."""
import json
import os
from pathlib import Path
import urllib.error
import unittest
from unittest import mock

import applicant_browser
import store
import test_runtime_kernel as fixture
from sandbox import KernelError, maintenance_access


class ApplicantBrowserTest(unittest.TestCase):
    def setUp(self):
        self.h = fixture.RuntimeKernelTest()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.h.switch(role='applicant', organization='browser-fictional', account='one')

    def open(self):
        with maintenance_access():
            return self.h.call('runtime.applicant.browser.open', {'confirmed': True})

    def test_open_requires_cli_maintenance_and_registered_capability(self):
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.browser.open', {'confirmed': True})
        self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')
        with maintenance_access(), self.assertRaises(KernelError) as direct:
            applicant_browser.open_session(self.h.context['scope_id'])
        self.assertEqual(direct.exception.code, 'KERNEL_REQUIRED')

    def test_explicit_open_uses_own_profile_and_redacts_model_keys_from_child_env(self):
        with mock.patch.object(applicant_browser, '_port_busy', return_value=False), \
             mock.patch.object(applicant_browser, '_edge_executable', return_value=Path('trusted-edge.exe')), \
             mock.patch.object(applicant_browser.subprocess, 'Popen') as launch, \
             mock.patch.dict(os.environ, {'JOBKIT_LLM_API_KEY': 'private-model-key', 'DEEPSEEK_API_KEY': 'old-private-key'}):
            launch.return_value.pid = 123
            result = self.open()
        arguments = launch.call_args.args[0]
        self.assertIn('--remote-debugging-address=127.0.0.1', arguments)
        self.assertIn('--remote-debugging-port=9222', arguments)
        self.assertEqual(arguments[-1], applicant_browser.JOBS_URL)
        self.assertIn(self.h.context['scope_id'], result['profile_directory'])
        self.assertFalse(result['cookies_imported'])
        self.assertFalse(result['sending_enabled'])
        self.assertNotIn('JOBKIT_LLM_API_KEY', launch.call_args.kwargs['env'])
        self.assertNotIn('DEEPSEEK_API_KEY', launch.call_args.kwargs['env'])
        self.assertNotIn('private-model-key', json.dumps(result))
        self.assertFalse(launch.call_args.kwargs['shell'])

    def test_busy_port_refuses_without_closing_any_existing_browser(self):
        with mock.patch.object(applicant_browser, '_port_busy', return_value=True), \
             mock.patch.object(applicant_browser.subprocess, 'Popen') as launch:
            with self.assertRaises(KernelError) as caught:
                self.open()
        self.assertEqual(caught.exception.code, 'BROWSER_PORT_IN_USE')
        launch.assert_not_called()
        self.assertFalse((Path(store.DB_PATH).parent / '.jobkit-browsers').exists())

    def test_open_needs_confirmation_and_applicant_role(self):
        with maintenance_access(), self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.browser.open', {'confirmed': False})
        self.assertEqual(caught.exception.code, 'BROWSER_CONFIRMATION_REQUIRED')
        self.h.switch(role='recruiter')
        with maintenance_access(), self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.browser.open', {'confirmed': True})
        self.assertEqual(caught.exception.code, 'APPLICANT_ONLY')

    def test_diagnostic_filters_hosts_and_omits_urls_and_login_secrets(self):
        tabs = [{'url': 'https://www.zhipin.com/web/geek/jobs?securityId=private-token'},
                {'url': 'https://www.zhipin.com/web/geek/chat'},
                {'url': 'https://www.zhipin.com.evil.invalid/web/geek/chat'},
                {'url': 'https://unrelated.invalid/private-page'}]
        with mock.patch.object(applicant_browser, '_read_json', return_value=tabs):
            result = self.h.call('runtime.applicant.channel.check')
        self.assertEqual(result['boss_page_count'], 2)
        self.assertFalse(result['account_verified'])
        self.assertFalse(result['sending_enabled'])
        self.assertFalse(result['cookies_read'])
        self.assertNotIn('private-', json.dumps(result))

    def test_unavailable_channel_is_not_an_empty_successful_job_search(self):
        with mock.patch.object(applicant_browser, '_read_json', side_effect=urllib.error.URLError('private error')):
            result = self.h.call('runtime.applicant.channel.check')
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['browser_reachable'])
        self.assertNotIn('private error', json.dumps(result))

    def test_bad_browser_data_is_explicit_and_urls_cannot_be_supplied(self):
        with mock.patch.object(applicant_browser, '_read_json', return_value={'tabs': []}):
            with self.assertRaises(KernelError) as caught:
                self.h.call('runtime.applicant.channel.check')
        self.assertEqual(caught.exception.code, 'BROWSER_CONNECTION_INVALID')
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.channel.check', {'endpoint': 'https://arbitrary.invalid'})
        self.assertEqual(caught.exception.code, 'INVALID_BROWSER_REQUEST')


if __name__ == '__main__':
    unittest.main()
