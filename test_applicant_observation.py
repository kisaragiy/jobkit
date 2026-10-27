"""Read-only typed CDP observation, never a generic script or sending bridge."""
import json
from pathlib import Path
import unittest
from unittest import mock

import applicant_browser
import applicant_jobs
import test_applicant_delivery as fixture
from sandbox import KernelError, maintenance_access


class Socket:
    def __init__(self, value):
        self.value, self.sent, self.closed = value, [], False
    def send(self, value):
        self.sent.append(json.loads(value))
    def settimeout(self, value):
        pass
    def recv(self):
        return json.dumps({'id': 1, 'result': {'result': {'value': self.value}}})
    def close(self):
        self.closed = True


VERSION = {'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/browser/11111111-2222-3333-4444-555555555555'}


class ApplicantObservationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ApplicantDeliveryTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # RuntimeKernelTest owns resources via tearDown, as delegated by the fixture.
        self.h = self.f.h
        self.f.save()
        with mock.patch.object(applicant_browser, '_port_busy', return_value=False), \
             mock.patch.object(applicant_browser, '_edge_executable', return_value=Path('test-edge.exe')), \
             mock.patch.object(applicant_browser.subprocess, 'Popen') as launch, maintenance_access():
            launch.return_value.pid = 100
            self.h.call('runtime.applicant.browser.open', {'confirmed': True})
        self.tab = {'url': 'https://www.zhipin.com/web/geek/jobs',
                    'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/test'}

    def read(self, value, tab=None):
        connection = Socket(value)
        with mock.patch.object(applicant_browser, '_read_json', return_value=[tab or self.tab]), \
             mock.patch.object(applicant_browser.websocket, 'create_connection', return_value=connection):
            result = self.h.call('runtime.applicant.jobs.read')
        return result, connection

    def test_observed_cards_are_matched_without_identity_or_sending_claim(self):
        result, connection = self.read({'status': 'observed', 'jobs': [fixture.job()]})
        self.assertEqual(result['source_type'], 'observed_browser_dom')
        self.assertEqual(result['counts']['eligible'], 1)
        self.assertTrue(result['matching_ready'])
        self.assertFalse(result['account_verified'])
        self.assertFalse(result['sending_enabled'])
        self.assertFalse(result['automatic_execution'])
        self.assertEqual([item['method'] for item in connection.sent], ['Runtime.evaluate'])
        self.assertEqual(connection.sent[0]['params']['expression'], Path(applicant_browser.__file__).with_name('applicant_dom_read.js').read_text(encoding='utf-8'))
        self.assertTrue(connection.closed)

    def test_login_restriction_and_schema_failure_are_distinct_from_empty(self):
        for state in ('auth_required', 'restricted', 'schema_changed', 'empty'):
            result, connection = self.read({'status': state, 'jobs': []})
            self.assertEqual(result['status'], state)
            self.assertFalse(result['matching_ready'])
            self.assertFalse(result['sending_enabled'])
            self.assertTrue(connection.closed)

    def test_multiple_pages_are_not_silently_guessed(self):
        with mock.patch.object(applicant_browser, '_read_json', return_value=[self.tab, self.tab]), \
             mock.patch.object(applicant_browser.websocket, 'create_connection') as connect:
            result = self.h.call('runtime.applicant.jobs.read')
        self.assertEqual(result['status'], 'page_selection_required')
        connect.assert_not_called()

    def test_websocket_cannot_redirect_to_other_hosts_or_ports(self):
        for endpoint in ('ws://external.invalid:9222/devtools/page/test', 'ws://127.0.0.1:1234/devtools/page/test',
                         'ws://127.0.0.1:9222/arbitrary', 'ws://name:password@127.0.0.1:9222/devtools/page/test'):
            with self.assertRaises(KernelError) as caught:
                self.read({'status': 'observed', 'jobs': [fixture.job()]}, tab={**self.tab, 'webSocketDebuggerUrl': endpoint})
            self.assertEqual(caught.exception.code, 'BROWSER_CONNECTION_INVALID')

    def test_invalid_or_duplicate_records_are_not_matchable(self):
        with self.assertRaises(KernelError) as caught:
            self.read({'status': 'observed', 'jobs': [fixture.job(), fixture.job()]})
        self.assertEqual(caught.exception.code, 'INVALID_JOB_FEED')
        with self.assertRaises(KernelError):
            self.read({'status': 'observed', 'jobs': []})

    def test_arbitrary_browser_code_cannot_be_supplied(self):
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.jobs.read', {'script': 'send message'})
        self.assertEqual(caught.exception.code, 'INVALID_BROWSER_REQUEST')

    def test_visible_account_name_is_not_an_account_authorization(self):
        connection = Socket({'status': 'display_name_observed', 'display_name': '虚构姓名', 'role_evidence': 'applicant_page'})
        with mock.patch.object(applicant_browser, '_read_json', side_effect=lambda path: VERSION if path == '/json/version' else [self.tab]), \
             mock.patch.object(applicant_browser.websocket, 'create_connection', return_value=connection):
            result = self.h.call('runtime.applicant.identity.read')
        self.assertEqual(result['display_name'], '虚构姓名')
        self.assertFalse(result['account_verified'])
        self.assertFalse(result['sending_enabled'])
        self.assertEqual(connection.sent[0]['params']['expression'], Path(applicant_browser.__file__).with_name('applicant_identity_read.js').read_text(encoding='utf-8'))
        self.assertTrue(connection.closed)

    def test_inconsistent_identity_result_is_rejected(self):
        connection = Socket({'status': 'auth_required', 'display_name': 'stale name', 'role_evidence': 'applicant_page'})
        with mock.patch.object(applicant_browser, '_read_json', side_effect=lambda path: VERSION if path == '/json/version' else [self.tab]), \
             mock.patch.object(applicant_browser.websocket, 'create_connection', return_value=connection):
            with self.assertRaises(KernelError) as caught:
                self.h.call('runtime.applicant.identity.read')
        self.assertEqual(caught.exception.code, 'BROWSER_SCHEMA_CHANGED')
        self.assertTrue(connection.closed)

    def test_identity_read_detects_browser_session_replacement(self):
        connection=Socket({'status':'display_name_observed','display_name':'虚构姓名','role_evidence':'applicant_page'})
        other={'webSocketDebuggerUrl':'ws://127.0.0.1:9222/devtools/browser/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}
        with mock.patch.object(applicant_browser,'_read_json',side_effect=[VERSION,[self.tab],other]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=connection):
            with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.identity.read')
        self.assertEqual(caught.exception.code,'BROWSER_SESSION_CHANGED');self.assertTrue(connection.closed)

    def test_browser_metadata_rejects_remote_hosts_and_wrong_ports(self):
        for url in ('ws://external.invalid:9222/devtools/browser/11111111-2222-3333-4444-555555555555',
                    'ws://127.0.0.1:1234/devtools/browser/11111111-2222-3333-4444-555555555555',
                    'ws://127.0.0.1:9222/devtools/page/test'):
            with mock.patch.object(applicant_browser,'_read_json',return_value={'webSocketDebuggerUrl':url}), \
                 mock.patch.object(applicant_browser.websocket,'create_connection') as connect:
                with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.identity.read')
            self.assertEqual(caught.exception.code,'BROWSER_CONNECTION_INVALID');connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
