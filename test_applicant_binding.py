"""Local browser-session binding and reference-only inbox; fictional data only."""
from dataclasses import replace
import json
import unittest
from unittest import mock
from fastapi.testclient import TestClient

import app
import main
import runtime_tools
import store
import applicant_inbox
import test_applicant_delivery as fixture
from sandbox import KernelError


class BrowserBindingTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApplicantDeliveryTest();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h=self.f.h
        self.observed={'status':'display_name_observed','display_name':'虚构姓名','role_evidence':'applicant_page',
            'browser_session_fingerprint':'a'*64,'account_verified':False,'sending_enabled':False}
        self.identity=self.observe()

    def observe(self, **changes):
        with mock.patch.object(runtime_tools.applicant_browser,'read_current_jobs',return_value={**self.observed,**changes}):
            return main.dispatch('runtime.applicant.identity.read',{},self.h.context['context_token'])

    def confirm(self, **changes):
        return main.dispatch('runtime.applicant.browser.binding.confirm',{
            'identity_observation_task_id':self.identity['trace_id'],'expected_display_name':self.observed['display_name'],
            'expected_browser_session_fingerprint':self.observed['browser_session_fingerprint'],
            'expected_revision':0,'confirmed':True,**changes},self.h.context['context_token'])

    def check(self, **changes):
        with mock.patch.object(runtime_tools.applicant_browser,'read_current_jobs',return_value={**self.observed,**changes}) as read:
            result=self.h.call('runtime.applicant.browser.binding.check')
        return result,read

    def test_binding_is_scoped_and_does_not_claim_platform_account_verification(self):
        result=self.confirm()['data']
        self.assertEqual(result['revision'],1)
        self.assertFalse(result['content']['platform_account_id_verified'])
        saved=self.h.call('runtime.applicant.browser.binding.get')
        self.assertEqual(saved['content'],result['content']);self.assertFalse(saved['live_session_checked'])
        self.assertFalse(saved['account_verified']);self.assertFalse(saved['sending_enabled'])

    def test_no_binding_never_reads_live_identity(self):
        result,read=self.check()
        self.assertEqual(result['status'],'binding_required');read.assert_not_called()

    def test_live_match_requires_both_browser_session_and_name(self):
        self.confirm();result,read=self.check()
        self.assertTrue(result['session_matches_confirmation'])
        read.assert_called_once_with(binding_check=True)
        for changes in ({'display_name':'另一虚构账号'},{'browser_session_fingerprint':'b'*64}):
            result,_=self.check(**changes)
            self.assertEqual(result['status'],'session_changed')
            self.assertFalse(result['session_matches_confirmation'])

    def test_login_or_connection_failure_does_not_match_binding(self):
        self.confirm()
        for status in ('auth_required','unavailable','restricted','identity_unavailable'):
            result,_=self.check(status=status)
            self.assertEqual(result['status'],status);self.assertFalse(result['session_matches_confirmation'])

    def test_confirm_requires_typed_revision_explicit_intent_and_exact_observation(self):
        for changes in ({'confirmed':False},{'expected_revision':True},{'expected_browser_session_fingerprint':'not-a-fingerprint'}):
            with self.subTest(changes=changes),self.assertRaises(KernelError) as caught:self.confirm(**changes)
            self.assertEqual(caught.exception.code,'INVALID_BROWSER_BINDING')
        with self.assertRaises(KernelError) as caught:self.confirm(expected_display_name='different')
        self.assertEqual(caught.exception.code,'BROWSER_BINDING_MISMATCH')

    def test_old_or_future_observation_cannot_bind(self):
        for offset in (-121,30):
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET updated_at=strftime('%s','now')+? WHERE task_id=?",(offset,self.identity['trace_id']))
            with self.assertRaises(KernelError) as caught:self.confirm()
            self.assertEqual(caught.exception.code,'FRESH_IDENTITY_REQUIRED')

    def test_other_workspace_and_old_context_cannot_reuse_observation(self):
        self.h.switch(role='applicant',organization='other',account='other')
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'FRESH_IDENTITY_REQUIRED')
        self.h.switch(role='applicant',organization='delivery-fictional',account='one')
        with self.assertRaises(KernelError):self.confirm()

    def test_one_browser_session_cannot_bind_two_workspaces(self):
        self.confirm();self.h.switch(role='applicant',organization='other',account='other');self.identity=self.observe()
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'BROWSER_SESSION_ALREADY_BOUND')

    def test_revision_fence_and_atomic_receipt(self):
        self.confirm()
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'STALE_BROWSER_BINDING')
        with mock.patch.object(runtime_tools,'record_commit',side_effect=KernelError('FIXTURE','fixture')):
            with self.assertRaises(KernelError):self.confirm(expected_revision=1)
        self.assertEqual(self.h.call('runtime.applicant.browser.binding.get')['revision'],1)

    def test_old_commit_recovery_does_not_overwrite_newer_binding(self):
        first=self.confirm()
        self.observed={**self.observed,'browser_session_fingerprint':'b'*64};self.identity=self.observe()
        second=self.confirm(expected_revision=1)
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?",(first['trace_id'],))
        tool=main._router._tools['runtime.applicant.browser.binding.confirm']
        with mock.patch.dict(main._router._tools,{tool.name:replace(tool,handler=mock.Mock(side_effect=AssertionError('no replay')))}):
            recovered=self.h.call('runtime.task.recover',{'task_id':first['trace_id']})
        self.assertEqual(recovered['result'],first['data'])
        self.assertEqual(self.h.call('runtime.applicant.browser.binding.get')['content'],second['data']['content'])

    def test_changed_binding_during_check_discards_result(self):
        self.confirm()
        def change(**kwargs):
            self.confirm(expected_revision=1)
            return self.observed
        with mock.patch.object(runtime_tools.applicant_browser,'read_current_jobs',side_effect=change):
            with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.browser.binding.check')
        self.assertEqual(caught.exception.code,'STALE_BROWSER_BINDING')

    def test_corrupt_binding_cannot_be_used_as_a_live_match(self):
        self.confirm()
        with store.runtime_db() as c:c.execute("UPDATE runtime_documents SET content='{}' WHERE kind='applicant_browser_binding'")
        with self.assertRaises(KernelError) as caught:self.check()
        self.assertEqual(caught.exception.code,'INVALID_BROWSER_BINDING')

    def test_http_local_session_and_context_guards(self):
        with TestClient(app.app,base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.get('/api/runtime/applicant/browser/binding').status_code,401)
            client.get('/workspace');headers={'X-Jobkit-Context':self.h.context['context_token']}
            self.assertEqual(client.get('/api/runtime/applicant/browser/binding',headers=headers).status_code,200)
            self.h.switch(role='recruiter')
            self.assertEqual(client.post('/api/runtime/applicant/browser/binding/check',headers=headers,json={}).status_code,409)


def preview():
    return {'status':'observed_previews','display_name':'虚构姓名','automation_ready':False,'list_truncated':False,'rows':[
        {'display_name':'虚构HR','party_summary':'虚构HR | 虚构公司','last_message_preview':'请问何时到岗？',
         'preview_truncated':False,'unread_marker':True,'platform_conversation_id':None,'identity_source':'unavailable',
         'full_message_available':False,'message_id_available':False}]}


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.f=BrowserBindingTest();self.f.setUp();self.addCleanup(self.f.doCleanups);self.h=self.f.h

    def test_no_binding_does_not_read_inbox(self):
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat') as read:
            result=self.h.call('runtime.applicant.conversations.read')
        self.assertEqual(result['status'],'binding_required');read.assert_not_called()

    def test_inbox_previews_do_not_become_full_messages_or_automatic_replies(self):
        self.f.confirm()
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=preview()):
            result=self.h.call('runtime.applicant.conversations.read')
        self.assertEqual(result['source_type'],'boss_visible_inbox_previews')
        self.assertFalse(result['automation_ready']);self.assertFalse(result['rows'][0]['message_id_available'])
        self.assertIsNone(result['rows'][0]['platform_conversation_id']);self.assertFalse(result['sending_enabled'])

    def test_binding_changes_during_read_discard_previews(self):
        self.f.confirm()
        def change(**kwargs):self.f.confirm(expected_revision=1);return preview()
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',side_effect=change):
            with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.conversations.read')
        self.assertEqual(caught.exception.code,'STALE_BROWSER_BINDING')

    def test_client_script_and_urls_are_refused(self):
        with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.conversations.read',{'url':'https://untrusted.invalid'})
        self.assertEqual(caught.exception.code,'INVALID_BROWSER_REQUEST')

    def test_preview_schema_rejects_identity_fabrication_and_complete_message_claims(self):
        original=preview();applicant_inbox.checked(original,'虚构姓名')
        for changes in ({'platform_conversation_id':'HR name','identity_source':'name_hash'},
                        {'full_message_available':True},{'message_id_available':True},{'preview_truncated':1}):
            value={**original,'rows':[{**original['rows'][0],**changes}]}
            with self.subTest(changes=changes),self.assertRaises(KernelError):applicant_inbox.checked(value,'虚构姓名')
        with self.assertRaises(KernelError):applicant_inbox.checked(original,'different account')


if __name__=='__main__':unittest.main()
