"""Fixed chat navigation and bound inbox reads; mocked CDP, no real messages."""
import hashlib
import json
import unittest
from unittest import mock

import applicant_browser
import main
import runtime_tools
import test_applicant_observation as fixture
from test_applicant_binding import preview
from sandbox import KernelError,maintenance_access


class Socket:
    def __init__(self):self.sent=[];self.closed=False
    def send(self,value):self.sent.append(json.loads(value))
    def settimeout(self,value):pass
    def recv(self):
        request=self.sent[-1]
        result={'targetId':'fictional-target'} if request['method']=='Target.createTarget' else {'result':{'value':preview()}}
        return json.dumps({'id':request['id'],'result':result})
    def close(self):self.closed=True


class BoundChatProtocolTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApplicantObservationTest();self.f.setUp();self.addCleanup(self.f.doCleanups);self.h=self.f.h
        self.chat={'url':applicant_browser.CHAT_URL,'webSocketDebuggerUrl':'ws://127.0.0.1:9222/devtools/page/chat'}
        self.fingerprint=hashlib.sha256(b'11111111-2222-3333-4444-555555555555').hexdigest()
        with mock.patch.object(runtime_tools.applicant_browser,'read_current_jobs',return_value={
            'status':'display_name_observed','display_name':'虚构姓名','role_evidence':'applicant_page',
            'browser_session_fingerprint':self.fingerprint,'account_verified':False,'sending_enabled':False}):
            observed=main.dispatch('runtime.applicant.identity.read',{},self.h.context['context_token'])
        self.h.call('runtime.applicant.browser.binding.confirm',{'identity_observation_task_id':observed['trace_id'],
            'expected_display_name':'虚构姓名','expected_browser_session_fingerprint':self.fingerprint,'expected_revision':0,'confirmed':True})

    def test_chat_open_is_fixed_background_navigation_and_maintenance_only(self):
        with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.chat.open',{'confirmed':True})
        self.assertEqual(caught.exception.code,'MAINTENANCE_REQUIRED')
        socket=Socket()
        with mock.patch.object(applicant_browser,'_read_json',side_effect=lambda path:fixture.VERSION if path=='/json/version' else [self.f.tab]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=socket),maintenance_access():
            result=self.h.call('runtime.applicant.chat.open',{'confirmed':True})
        self.assertEqual(result['status'],'chat_page_opened');self.assertTrue(socket.closed)
        self.assertEqual(socket.sent[0]['method'],'Target.createTarget')
        self.assertEqual(socket.sent[0]['params'],{'url':applicant_browser.CHAT_URL,'background':True})
        self.assertFalse(result['sending_enabled'])

    def test_existing_chat_does_not_open_duplicate_tab(self):
        with mock.patch.object(applicant_browser,'_read_json',return_value=[self.chat]), \
             mock.patch.object(applicant_browser.websocket,'create_connection') as connect,maintenance_access():
            result=self.h.call('runtime.applicant.chat.open',{'confirmed':True})
        self.assertEqual(result['status'],'already_open');connect.assert_not_called()

    def test_bound_read_evaluates_only_fixed_preview_reader(self):
        socket=Socket()
        with mock.patch.object(applicant_browser,'_read_json',side_effect=lambda path:fixture.VERSION if path=='/json/version' else [self.chat]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=socket):
            result=self.h.call('runtime.applicant.conversations.read')
        self.assertEqual(result['status'],'observed_previews');self.assertFalse(result['automation_ready'])
        self.assertEqual([r['method'] for r in socket.sent],['Runtime.evaluate']);self.assertTrue(socket.closed)
        self.assertIn('expectedDisplayName',socket.sent[0]['params']['expression'])
        self.assertNotIn('Page.navigate',str(socket.sent));self.assertIsNone(result['rows'][0]['platform_conversation_id'])

    def test_restarted_browser_is_refused_before_private_preview_read(self):
        other={'webSocketDebuggerUrl':'ws://127.0.0.1:9222/devtools/browser/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}
        with mock.patch.object(applicant_browser,'_read_json',return_value=other), \
             mock.patch.object(applicant_browser.websocket,'create_connection') as connect:
            with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.conversations.read')
        self.assertEqual(caught.exception.code,'BROWSER_BINDING_MISMATCH');connect.assert_not_called()

    def test_mid_read_browser_replacement_discards_preview(self):
        other={'webSocketDebuggerUrl':'ws://127.0.0.1:9222/devtools/browser/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}
        socket=Socket()
        with mock.patch.object(applicant_browser,'_read_json',side_effect=[fixture.VERSION,[self.chat],other]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=socket):
            with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.conversations.read')
        self.assertEqual(caught.exception.code,'BROWSER_SESSION_CHANGED');self.assertTrue(socket.closed)

    def test_ambiguous_or_missing_chat_page_is_not_empty_inbox_success(self):
        for tabs,status in (([],'chat_page_required'),([self.chat,self.chat],'page_selection_required')):
            with mock.patch.object(applicant_browser,'_read_json',side_effect=lambda path:fixture.VERSION if path=='/json/version' else tabs), \
                 mock.patch.object(applicant_browser.websocket,'create_connection') as connect:
                result=self.h.call('runtime.applicant.conversations.read')
            self.assertEqual(result['status'],status);self.assertFalse(result['automation_ready']);connect.assert_not_called()


if __name__=='__main__':unittest.main()
