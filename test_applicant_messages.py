"""Selected message reads and snapshot-checked navigation, fictional CDP only."""
from dataclasses import replace
import json
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import store
import runtime_tools
import applicant_browser
import applicant_messages
import test_applicant_chat as fixture
from test_applicant_binding import preview
from test_applicant_observation import VERSION
from sandbox import KernelError,maintenance_access


def snapshot():
    return {'status':'observed_messages','party':{'display_name':'虚构HR','party_summary':'虚构HR | 虚构公司',
        'header_display_name':'虚构HR','platform_conversation_id':None,'identity_source':'unavailable'},
        'messages':[{'message_id':'mid-123','message_id_source':'dom_data_mid','direction':'incoming','kind':'text',
            'text':'请问何时到岗？','text_truncated':False,'full_text_available':True,'timestamp_label':'今天 10:30'}],
        'messages_truncated':False,'history_complete':False,'recipient_verified':False,'automation_ready':False}


class Socket:
    def __init__(self, first=None, second=None, fail_navigation=False):
        self.first=first or snapshot();self.second=second if second is not None else self.first
        self.sent=[];self.closed=False;self.evaluations=0;self.fail_navigation=fail_navigation
    def send(self,value):self.sent.append(json.loads(value))
    def settimeout(self,value):pass
    def recv(self):
        request=self.sent[-1];method=request['method']
        if self.fail_navigation and method=='Runtime.evaluate':
            raise applicant_browser.websocket.WebSocketTimeoutException('fixture')
        if method=='Browser.getWindowForTarget':result={'windowId':1,'bounds':{'windowState':'normal'}}
        elif method=='Runtime.evaluate':
            self.evaluations+=1;result={'result':{'value':self.first if self.evaluations==1 else self.second}}
        else:result={}
        return json.dumps({'id':request['id'],'result':result})
    def close(self):self.closed=True


class SelectedMessagesTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BoundChatProtocolTest();self.f.setUp();self.addCleanup(self.f.doCleanups);self.h=self.f.h

    def read(self,first=None,second=None):
        socket=Socket(first,second)
        with mock.patch.object(applicant_browser,'_read_json',side_effect=lambda path:VERSION if path=='/json/version' else [self.f.chat]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=socket):
            result=main.dispatch('runtime.applicant.messages.read',{},self.h.context['context_token'])
        self.assertTrue(socket.closed)
        return result,socket

    def seed_inbox(self):
        with mock.patch.object(applicant_browser,'read_chat',return_value=preview()):
            return main.dispatch('runtime.applicant.conversations.read',{},self.h.context['context_token'])

    def test_real_body_and_observed_dom_id_are_not_recipient_authority(self):
        result,socket=self.read();data=result['data']
        self.assertEqual(data['source_type'],'boss_selected_message_dom')
        self.assertTrue(data['messages'][0]['full_text_available'])
        self.assertEqual(data['messages'][0]['message_id'],'mid-123')
        self.assertFalse(data['recipient_verified']);self.assertFalse(data['automation_ready']);self.assertFalse(data['sending_enabled'])
        self.assertEqual([r['method'] for r in socket.sent],['Runtime.evaluate','Runtime.evaluate'])

    def test_changing_message_snapshot_is_discarded(self):
        changed=snapshot();changed['messages'][0]['text']='new incoming message'
        with self.assertRaises(KernelError) as caught:self.read(second=changed)
        self.assertEqual(caught.exception.code,'CONVERSATION_CHANGED')

    def test_missing_selected_conversation_is_not_empty_message_success(self):
        value={**snapshot(),'status':'conversation_selection_required','party':None,'messages':[]}
        result,_=self.read(value)
        self.assertEqual(result['data']['status'],'conversation_selection_required')

    def test_duplicate_message_ids_and_false_full_text_claims_are_rejected(self):
        value=snapshot();value['messages']*=2
        with self.assertRaises(KernelError):self.read(value)
        for changes in ({'text_truncated':True},{'kind':'media'},{'direction':'assumed_incoming'},{'message_id_source':'text_hash'}):
            value=snapshot();value['messages'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises(KernelError):self.read(value)

    def test_missing_message_id_remains_unavailable(self):
        value=snapshot();value['messages'][0].update(message_id=None,message_id_source='unavailable')
        result,_=self.read(value)
        self.assertIsNone(result['data']['messages'][0]['message_id']);self.assertFalse(result['data']['automation_ready'])

    def test_card_media_unknown_direction_and_truncation_do_not_claim_full_text(self):
        for kind in ('card','media','unknown'):
            value=snapshot();value['messages'][0].update(kind=kind,direction='unknown',full_text_available=False)
            result,_=self.read(value);self.assertFalse(result['data']['messages'][0]['full_text_available'])

    def test_header_party_mismatch_and_complete_history_claim_are_rejected(self):
        value=snapshot();value['party']['header_display_name']='different HR'
        with self.assertRaises(KernelError):self.read(value)
        with self.assertRaises(KernelError):self.read({**snapshot(),'history_complete':True})

    def test_oversized_message_window_and_text_are_rejected(self):
        value=snapshot();value['messages'][0]['text']='x'*4001
        with self.assertRaises(KernelError):applicant_messages.checked(value)
        value=snapshot();value['messages']*=101
        with self.assertRaises(KernelError):applicant_messages.checked(value)

    def test_messages_require_binding_and_refuse_client_code(self):
        with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.messages.read',{'script':'arbitrary'})
        self.assertEqual(caught.exception.code,'INVALID_BROWSER_REQUEST')
        with store.runtime_db() as c:c.execute("DELETE FROM runtime_documents WHERE kind='applicant_browser_binding'")
        with mock.patch.object(applicant_browser,'read_chat') as read:
            data=self.h.call('runtime.applicant.messages.read')
        self.assertEqual(data['status'],'binding_required');self.assertEqual(data['messages'],[]);read.assert_not_called()

    def test_navigation_uses_fresh_snapshot_and_only_fixed_ui_actions(self):
        source=self.seed_inbox();value={'status':'navigation_requested','navigation_requested':True,'recipient_verified':False,'sending_enabled':False}
        socket=Socket(value)
        with mock.patch.object(applicant_browser,'_read_json',side_effect=lambda path:VERSION if path=='/json/version' else [self.f.chat]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=socket),maintenance_access():
            result=self.h.call('runtime.applicant.conversation.open',{'inbox_observation_task_id':source['trace_id'],'row_index':0,'confirmed':True})
        self.assertTrue(result['navigation_requested']);self.assertFalse(result['recipient_verified']);self.assertTrue(socket.closed)
        self.assertEqual([r['method'] for r in socket.sent],['Browser.getWindowForTarget','Page.bringToFront','Runtime.evaluate'])
        self.assertNotIn('btn-send',socket.sent[-1]['params']['expression'])

    def test_expired_or_cross_scope_navigation_reference_never_touches_browser(self):
        source=self.seed_inbox()
        with store.runtime_db() as c:c.execute('UPDATE runtime_tasks SET updated_at=updated_at-31 WHERE task_id=?',(source['trace_id'],))
        with maintenance_access(),mock.patch.object(applicant_browser,'read_chat') as read:
            with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.conversation.open',{'inbox_observation_task_id':source['trace_id'],'row_index':0,'confirmed':True})
        self.assertEqual(caught.exception.code,'FRESH_INBOX_REQUIRED');read.assert_not_called()

    def test_navigation_timeout_records_unknown_and_recovery_never_replays(self):
        source=self.seed_inbox();socket=Socket(fail_navigation=True)
        with mock.patch.object(applicant_browser,'_read_json',side_effect=lambda path:VERSION if path=='/json/version' else [self.f.chat]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=socket),maintenance_access():
            with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.conversation.open',{'inbox_observation_task_id':source['trace_id'],'row_index':0,'confirmed':True})
        task_id=caught.exception.trace_id
        with store.runtime_db() as c:row=c.execute('SELECT status FROM runtime_tasks WHERE task_id=?',(task_id,)).fetchone()
        self.assertEqual(row['status'],'outcome_unknown')
        tool=main._router._tools['runtime.applicant.conversation.open']
        with mock.patch.dict(main._router._tools,{tool.name:replace(tool,handler=mock.Mock(side_effect=AssertionError('no replay')))}):
            recovered=self.h.call('runtime.task.recover',{'task_id':task_id})
        self.assertEqual(recovered['status'],'outcome_unknown')

    def test_navigation_is_not_available_as_an_implicit_public_action(self):
        source=self.seed_inbox()
        with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.conversation.open',{'inbox_observation_task_id':source['trace_id'],'row_index':0,'confirmed':True})
        self.assertEqual(caught.exception.code,'MAINTENANCE_REQUIRED')
        with maintenance_access(),self.assertRaises(KernelError):self.h.call('runtime.applicant.conversation.open',{'inbox_observation_task_id':source['trace_id'],'row_index':True,'confirmed':True})

    def test_real_http_message_read_requires_session_and_keeps_flags(self):
        socket=Socket()
        with TestClient(app.app,base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post('/api/runtime/applicant/messages/read',json={}).status_code,401)
            client.get('/workspace');headers={'X-Jobkit-Context':self.h.context['context_token']}
            with mock.patch.object(applicant_browser,'_read_json',side_effect=lambda path:VERSION if path=='/json/version' else [self.f.chat]), \
                 mock.patch.object(applicant_browser.websocket,'create_connection',return_value=socket):
                response=client.post('/api/runtime/applicant/messages/read',json={},headers=headers)
            self.assertEqual(response.status_code,200);self.assertFalse(response.json()['data']['automation_ready'])


if __name__=='__main__':unittest.main()
