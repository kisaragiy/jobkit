"""Send-target anchoring, message-age fence, local budget reservation and readback; nothing is sent."""
from copy import deepcopy
import hashlib
import json
import os
import time
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import applicant_reply_window
import applicant_send
import app
import main
import runtime_tools
import store
import test_applicant_messages as fixture
from sandbox import KernelError


def message(identity='mid-123',text='请问何时到岗？',**changes):
    return {**fixture.snapshot()['messages'][0],'message_id':identity,'text':text,**changes}


class SendTargetTest(unittest.TestCase):
    """Anchor first, message second: a baseline message is never treated as newly arrived."""

    def setUp(self):
        self.f=fixture.SelectedMessagesTest();self.f.setUp();self.addCleanup(self.f.doCleanups);self.h=self.f.h
        self.messages=[message('own-old','早前回复',direction='outgoing')]
        self.source=self.observe()
        self.window=self.dispatch('runtime.applicant.reply.window.start',{'message_observation_task_id':self.source,
            'expected_revision':0,'expected_profile_revision':1,'expected_binding_revision':1})['data']['revision']
        self.question('mid-123','请问何时到岗？')
        self.task=self.h.call('runtime.task.execute',{'task_id':self.intent['draft_task_id']})

    def dispatch(self,tool,payload=None):return main.dispatch(tool,payload,self.h.context['context_token'])

    def observe(self):
        value={**fixture.snapshot(),'messages':deepcopy(self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=value):
            return self.dispatch('runtime.applicant.messages.read',{})['trace_id']

    def question(self,identity,text):
        if self.messages[-1]['direction']=='incoming':
            self.messages=[*self.messages,message('own-'+identity,'已回复',direction='outgoing')]
        self.messages=[*self.messages,message(identity,text)]
        self.source=self.observe()
        self.window=self.dispatch('runtime.applicant.reply.window.observe',{'message_observation_task_id':self.source,
            'expected_revision':self.window,'expected_profile_revision':1,'expected_binding_revision':1})['data']['revision']
        saved=self.dispatch('runtime.applicant.reply.prepare',{'message_observation_task_id':self.source,
            'message_ids':[identity],'expected_profile_revision':1,'expected_binding_revision':1})['data']
        self.intent={'intent_id':saved['intent']['intent_id'],'draft_task_id':saved['task']['task_id']}
        return self.intent

    def confirm(self,**changes):
        payload={'intent_id':self.intent['intent_id'],'expected_manual_revision':0,'expected_profile_revision':1,
                 'expected_binding_revision':1,'expected_window_revision':self.window,'confirmed':True}
        payload.update(changes)
        return self.dispatch('runtime.applicant.send.recipient.confirm',payload)['data']

    def reserve(self,recipient=None,**changes):
        recipient=recipient or self.confirm()['recipient']
        payload={'intent_id':self.intent['intent_id'],'recipient_binding_id':recipient['recipient_fingerprint'],
                 'expected_manual_revision':0,'expected_profile_revision':1,'expected_binding_revision':1,
                 'expected_window_revision':self.window,'confirmed':True}
        payload.update(changes)
        return self.dispatch('runtime.applicant.send.reserve',payload)['data']

    def attempts(self):
        with store.runtime_db() as c:return c.execute('SELECT * FROM runtime_send_attempts').fetchall()

    def test_recipient_binding_pins_party_anchor_and_messages_without_claiming_identity(self):
        saved=self.confirm();recipient=saved['recipient']
        self.assertTrue(saved['created']);self.assertEqual(saved['revision'],1)
        self.assertEqual(recipient['anchor_message_id'],'mid-123');self.assertEqual(recipient['message_ids'],['mid-123'])
        self.assertEqual(recipient['party']['display_name'],'虚构HR')
        self.assertEqual(len(recipient['recipient_fingerprint']),64)
        self.assertEqual(recipient['recipient_fingerprint'],applicant_send.fingerprint(recipient))
        self.assertIs(recipient['platform_account_id_verified'],False);self.assertIs(recipient['sending_enabled'],False)
        self.assertIs(saved['sending_enabled'],False);self.assertLessEqual(saved['message_age_seconds'],120)
        current=self.dispatch('runtime.applicant.send.recipient.get',{'intent_id':self.intent['intent_id']})['data']
        self.assertEqual(current['recipient'],recipient);self.assertFalse(current['profile_stale'])

    def test_repeated_confirmation_reuses_one_immutable_binding(self):
        first=self.confirm();second=self.confirm()
        self.assertFalse(second['created']);self.assertEqual(second['recipient'],first['recipient'])
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_documents WHERE kind LIKE 'applicant_send_recipient:%'").fetchone()[0],1)

    def test_observation_window_is_required_and_fenced(self):
        with self.assertRaises(KernelError) as caught:self.confirm(expected_window_revision=self.window+1)
        self.assertEqual(caught.exception.code,'STALE_REPLY_WINDOW')
        with store.runtime_db() as c:c.execute("DELETE FROM runtime_documents WHERE kind='applicant_reply_window'")
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'STALE_REPLY_WINDOW')

    def test_window_gap_after_two_minutes_blocks_the_binding(self):
        past=time.time()-121
        with store.runtime_db() as c:
            c.execute('UPDATE runtime_tasks SET updated_at=? WHERE task_id=?',(past,self.source))
            row=c.execute("SELECT content FROM runtime_documents WHERE kind='applicant_reply_window'").fetchone()
            content=json.loads(row['content']);content['last_observed_at']=past;content['started_at']=past-10
            for entry in content['seen'].values():entry['first_seen_at']=past-5
            c.execute("UPDATE runtime_documents SET content=? WHERE kind='applicant_reply_window'",(json.dumps(content,ensure_ascii=False),))
            c.execute('UPDATE runtime_tasks SET updated_at=? WHERE task_id=?',(past-10,content['start_observation_task_id']))
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'WINDOW_GAP_EXCEEDED')

    def test_stale_profile_or_browser_binding_is_refused(self):
        with self.assertRaises(KernelError) as caught:self.confirm(expected_profile_revision=2)
        self.assertEqual(caught.exception.code,'STALE_SEND_FACTS')
        with self.assertRaises(KernelError) as caught:self.confirm(expected_binding_revision=2)
        self.assertEqual(caught.exception.code,'STALE_SEND_FACTS')
        self.assertEqual(self.attempts(),[])

    def test_unfinished_draft_cannot_be_bound(self):
        self.question('mid-9','能来面试吗？')
        with self.assertRaises(KernelError) as caught:self.confirm()
        self.assertEqual(caught.exception.code,'DRAFT_NOT_READY')

    def test_message_age_new_after_anchor_and_digest_are_enforced(self):
        digest=applicant_reply_window.message_digest(self.messages[-1])
        stale={'seen':{'mid-123':{'digest':digest,'first_seen_at':time.time()-300,'new_after_start':True}}}
        with self.assertRaises(KernelError) as caught:applicant_send.message_age(stale,[self.messages[-1]])
        self.assertEqual(caught.exception.code,'MESSAGE_AGE_EXCEEDED')
        baseline={'seen':{'mid-123':{'digest':digest,'first_seen_at':time.time(),'new_after_start':False}}}
        with self.assertRaises(KernelError) as caught:applicant_send.message_age(baseline,[self.messages[-1]])
        self.assertEqual(caught.exception.code,'MESSAGE_NOT_NEW_AFTER_WINDOW')
        changed={'seen':{'mid-123':{'digest':'0'*64,'first_seen_at':time.time(),'new_after_start':True}}}
        with self.assertRaises(KernelError):applicant_send.message_age(changed,[self.messages[-1]])
        with self.assertRaises(KernelError) as caught:applicant_send.window_age({'last_observed_at':time.time()-121})
        self.assertEqual(caught.exception.code,'WINDOW_GAP_EXCEEDED')

    def test_corrupt_recipient_document_is_refused(self):
        self.confirm()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_documents SET content=? WHERE kind LIKE 'applicant_send_recipient:%'",('{}',))
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.recipient.get',{'intent_id':self.intent['intent_id']})
        self.assertEqual(caught.exception.code,'INVALID_SEND_RECIPIENT')

    def test_reservation_requires_a_confirmed_recipient(self):
        with self.assertRaises(KernelError) as caught:self.reserve(recipient={'recipient_fingerprint':'f'*64})
        self.assertEqual(caught.exception.code,'SEND_RECIPIENT_REQUIRED')
        self.assertEqual(self.attempts(),[])

    def test_reservation_records_local_budget_without_sending(self):
        saved=self.reserve();attempt=saved['attempt']
        self.assertTrue(saved['created']);self.assertEqual(attempt['state'],'reserved')
        self.assertIs(attempt['sent'],False);self.assertIs(attempt['delivery_confirmed'],False)
        self.assertEqual(attempt['message_sha256'],hashlib.sha256(self.task['result']['text'].encode()).hexdigest())
        self.assertEqual(saved['budget']['committed_attempts'],1)
        self.assertEqual(saved['budget']['remaining'],applicant_send.daily_limit()-1)
        self.assertEqual(saved['budget']['quota_source'],'local_workspace_not_platform')
        self.assertEqual(len(self.attempts()),1)

    def test_repeated_reservation_reuses_the_same_attempt(self):
        recipient=self.confirm()['recipient']
        first=self.reserve(recipient);second=self.reserve(recipient)
        self.assertFalse(second['created']);self.assertEqual(first['attempt']['attempt_id'],second['attempt']['attempt_id'])
        self.assertEqual(len(self.attempts()),1)

    def test_different_recipient_binding_for_same_intent_is_refused(self):
        recipient=self.confirm()['recipient'];self.reserve(recipient)
        with self.assertRaises(KernelError) as caught:self.reserve(recipient,recipient_binding_id='a'*64)
        self.assertEqual(caught.exception.code,'SEND_RECIPIENT_CHANGED')
        self.assertEqual(len(self.attempts()),1)

    def test_daily_limit_refuses_further_reservations(self):
        self.reserve()
        self.question('mid-2','请问何时到岗？')
        self.h.call('runtime.task.execute',{'task_id':self.intent['draft_task_id']})
        with mock.patch.dict(os.environ,{'JOBKIT_SEND_DAILY_LIMIT':'1'}):
            with self.assertRaises(KernelError) as caught:self.reserve()
        self.assertEqual(caught.exception.code,'SEND_DAILY_LIMIT_REACHED')
        self.assertEqual(len(self.attempts()),1)

    def test_release_returns_budget_once_and_never_reopens_a_used_attempt(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        released=self.dispatch('runtime.applicant.send.release',{'attempt_id':attempt_id,'reason':'user_cancelled','confirmed':True})['data']
        self.assertEqual(released['attempt']['state'],'released');self.assertEqual(released['budget']['remaining'],applicant_send.daily_limit())
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.release',{'attempt_id':attempt_id,'reason':'user_cancelled','confirmed':True})
        self.assertEqual(caught.exception.code,'INVALID_SEND_ATTEMPT_STATE')

    def test_readback_reports_evidence_for_a_matching_outgoing_message(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        live={**fixture.snapshot(),'messages':[*self.messages,message('own-1',self.task['result']['text'],direction='outgoing')]}
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=live):
            result=self.dispatch('runtime.applicant.send.readback',{'attempt_id':attempt_id})['data']
        self.assertTrue(result['party_match']);self.assertTrue(result['readback_observed'])
        self.assertEqual(result['evidence']['message_id'],'own-1');self.assertFalse(result['evidence']['ambiguous'])
        self.assertIs(result['delivered_confirmed'],False);self.assertIs(result['sending_enabled'],False)

    def test_readback_without_a_matching_message_is_not_delivery(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        live={**fixture.snapshot(),'messages':deepcopy(self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=live):
            result=self.dispatch('runtime.applicant.send.readback',{'attempt_id':attempt_id})['data']
        self.assertFalse(result['readback_observed']);self.assertEqual(result['evidence']['reason'],'no_matching_outgoing_message')
        self.assertIs(result['delivered_confirmed'],False)

    def test_readback_refuses_a_different_conversation(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        live={**fixture.snapshot(),'party':{**fixture.snapshot()['party'],'display_name':'另一个HR','header_display_name':'另一个HR'},
              'messages':deepcopy(self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=live):
            result=self.dispatch('runtime.applicant.send.readback',{'attempt_id':attempt_id})['data']
        self.assertFalse(result['party_match']);self.assertFalse(result['readback_observed'])
        self.assertEqual(result['evidence']['reason'],'conversation_changed')

    def test_readback_after_draft_change_is_refused(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET result=json_set(result,'$.text',?) WHERE task_id=?",('改过的答复',self.intent['draft_task_id']))
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.readback',{'attempt_id':attempt_id})
        self.assertEqual(caught.exception.code,'DRAFT_CHANGED_AFTER_RESERVATION')

    def test_unknown_and_corrupt_attempts_are_refused(self):
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.get',{'attempt_id':'f'*32})
        self.assertEqual(caught.exception.code,'SEND_ATTEMPT_NOT_FOUND')
        self.reserve()
        with store.runtime_db() as c:c.execute("UPDATE runtime_send_attempts SET message_sha256='broken'")
        with self.assertRaises(KernelError):self.dispatch('runtime.applicant.send.get',{'attempt_id':self.attempts()[0]['attempt_id']})

    def handoff(self,attempt_id):
        return self.dispatch('runtime.applicant.send.handoff',{'attempt_id':attempt_id,'confirmed':True})['data']

    def test_handoff_card_is_copy_ready_and_kept_out_of_the_receipt(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        card=self.handoff(attempt_id);text=self.task['result']['text']
        self.assertEqual(card['text'],text);self.assertTrue(card['created'])
        self.assertEqual(card['handoff']['draft_text_sha256'],saved['attempt']['message_sha256'])
        self.assertLessEqual(card['expires_in_seconds'],120)
        self.assertIs(card['manual_handoff_only'],True);self.assertIs(card['delivery_confirmed'],False)
        self.assertIs(card['sending_enabled'],False)
        with store.runtime_db() as c:
            receipt=c.execute("SELECT commit_receipt FROM runtime_tasks WHERE tool='runtime.applicant.send.handoff'").fetchone()
        self.assertIsNotNone(receipt);self.assertNotIn(text,receipt['commit_receipt'])

    def test_handoff_is_idempotent_and_requires_a_reserved_attempt(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        first=self.handoff(attempt_id);second=self.handoff(attempt_id)
        self.assertFalse(second['created']);self.assertEqual(first['handoff'],second['handoff'])
        self.dispatch('runtime.applicant.send.release',{'attempt_id':attempt_id,'reason':'user_cancelled','confirmed':True})
        with self.assertRaises(KernelError) as caught:self.handoff(attempt_id)
        self.assertEqual(caught.exception.code,'INVALID_SEND_ATTEMPT_STATE')

    def test_manual_claim_requires_a_handoff_and_stays_a_self_report(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.manual.claim',{'attempt_id':attempt_id,'confirmed':True})
        self.assertEqual(caught.exception.code,'SEND_HANDOFF_REQUIRED')
        self.handoff(attempt_id)
        claim=self.dispatch('runtime.applicant.send.manual.claim',{'attempt_id':attempt_id,'confirmed':True})['data']
        self.assertIs(claim['claim']['self_reported'],True);self.assertIs(claim['claim']['delivery_confirmed'],False)
        self.assertIs(claim['delivery_confirmed'],False)
        again=self.dispatch('runtime.applicant.send.manual.claim',{'attempt_id':attempt_id,'confirmed':True})['data']
        self.assertFalse(again['created']);self.assertEqual(again['claim'],claim['claim'])

    def test_expired_handoff_cannot_be_claimed(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id'];self.handoff(attempt_id)
        kind='applicant_send_handoff:'+attempt_id
        with store.runtime_db() as c:
            row=c.execute('SELECT content FROM runtime_documents WHERE kind=?',(kind,)).fetchone()
            content=json.loads(row['content']);content['text_expires_at']=time.time()-1;content['created_at']=time.time()-200
            c.execute('UPDATE runtime_documents SET content=? WHERE kind=?',(json.dumps(content,ensure_ascii=False),kind))
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.manual.claim',{'attempt_id':attempt_id,'confirmed':True})
        self.assertEqual(caught.exception.code,'HANDOFF_EXPIRED')
        state=self.dispatch('runtime.applicant.send.handoff.get',{'attempt_id':attempt_id})['data']
        self.assertTrue(state['expired']);self.assertIsNone(state['claim'])

    def test_handoff_get_and_corrupt_handoff_are_fenced(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        empty=self.dispatch('runtime.applicant.send.handoff.get',{'attempt_id':attempt_id})['data']
        self.assertIsNone(empty['handoff']);self.assertIsNone(empty['claim']);self.assertFalse(empty['expired'])
        self.handoff(attempt_id)
        with store.runtime_db() as c:c.execute("UPDATE runtime_documents SET content='{}' WHERE kind=?",('applicant_send_handoff:'+attempt_id,))
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.handoff.get',{'attempt_id':attempt_id})
        self.assertEqual(caught.exception.code,'INVALID_SEND_HANDOFF')

    def test_text_expiry_is_fixed_by_first_observation(self):
        messages=self.messages[-1:];stamp=time.time()-30
        window={'seen':{'mid-123':{'digest':applicant_reply_window.message_digest(messages[0]),
                                   'first_seen_at':stamp,'new_after_start':True}}}
        self.assertEqual(applicant_send.text_expiry(window,messages),stamp+applicant_send.MAX_MESSAGE_AGE_SECONDS)

    def hit_snapshot(self):
        return {**fixture.snapshot(),'messages':[*self.messages,message('own-1',self.task['result']['text'],direction='outgoing')]}

    def test_readback_commit_persists_and_is_idempotent_for_the_same_observation(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=self.hit_snapshot()):
            first=self.dispatch('runtime.applicant.send.readback.commit',{'attempt_id':attempt_id})['data']
            second=self.dispatch('runtime.applicant.send.readback.commit',{'attempt_id':attempt_id})['data']
        self.assertTrue(first['created']);self.assertFalse(second['created'])
        self.assertEqual(first['readback'],second['readback'])
        self.assertTrue(first['readback']['observed']);self.assertEqual(first['readback']['message_id'],'own-1')
        self.assertIs(first['delivery_confirmed'],False);self.assertIs(first['sending_enabled'],False)

    def test_readback_commit_upgrades_a_miss_to_a_hit(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value={**fixture.snapshot(),'messages':deepcopy(self.messages)}):
            first=self.dispatch('runtime.applicant.send.readback.commit',{'attempt_id':attempt_id})['data']
        self.assertFalse(first['readback']['observed']);self.assertEqual(first['readback']['match_count'],0)
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=self.hit_snapshot()):
            second=self.dispatch('runtime.applicant.send.readback.commit',{'attempt_id':attempt_id})['data']
        self.assertTrue(second['created']);self.assertTrue(second['readback']['observed'])
        self.assertEqual(second['readback']['message_id'],'own-1')

    def test_readback_commit_refuses_a_hit_later_reported_as_a_miss(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=self.hit_snapshot()):
            self.dispatch('runtime.applicant.send.readback.commit',{'attempt_id':attempt_id})
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value={**fixture.snapshot(),'messages':deepcopy(self.messages)}):
            with self.assertRaises(KernelError) as caught:
                self.dispatch('runtime.applicant.send.readback.commit',{'attempt_id':attempt_id})
        self.assertEqual(caught.exception.code,'READBACK_REGRESSION')

    def test_stats_aggregates_handoff_claim_and_readback_by_draft(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id']
        self.handoff(attempt_id)
        self.dispatch('runtime.applicant.send.manual.claim',{'attempt_id':attempt_id,'confirmed':True})
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=self.hit_snapshot()):
            self.dispatch('runtime.applicant.send.readback.commit',{'attempt_id':attempt_id})
        stats=self.dispatch('runtime.applicant.send.stats',{})['data']
        totals=stats['totals']
        self.assertEqual(totals['attempts'],1);self.assertEqual(totals['by_state'],{'reserved':1})
        self.assertEqual((totals['handoffs'],totals['claims'],totals['readback_hits']),(1,1,1))
        self.assertEqual(totals['handoff_to_readback_rate'],1.0);self.assertEqual(totals['claim_to_readback_rate'],1.0)
        self.assertEqual(stats['by_draft'][0]['draft_text_sha256'],saved['attempt']['message_sha256'])
        self.assertEqual(stats['by_draft'][0]['hits'],1);self.assertEqual(stats['invalid_documents'],[])
        self.assertIsNotNone(stats['limits']['sample_warning'])
        self.assertIs(stats['limits']['not_platform_reply_rate'],True)
        self.assertIs(stats['delivery_confirmed'],False);self.assertIs(stats['sending_enabled'],False)

    def test_stats_counts_an_empty_workspace_without_inventing_rates(self):
        stats=self.dispatch('runtime.applicant.send.stats',{})['data']
        self.assertEqual(stats['totals']['attempts'],0)
        self.assertIsNone(stats['totals']['handoff_to_readback_rate'])
        self.assertEqual(stats['by_draft'],[]);self.assertEqual(stats['by_day'],[])
        with self.assertRaises(KernelError) as caught:self.dispatch('runtime.applicant.send.stats',{'attempt_id':'f'*32})
        self.assertEqual(caught.exception.code,'INVALID_SEND_STATS')

    def test_stats_reports_corrupt_documents_instead_of_hiding_them(self):
        saved=self.reserve();attempt_id=saved['attempt']['attempt_id'];self.handoff(attempt_id)
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_documents SET content='{}' WHERE kind=?",('applicant_send_handoff:'+attempt_id,))
        stats=self.dispatch('runtime.applicant.send.stats',{})['data']
        self.assertEqual(stats['totals']['attempts'],0)
        self.assertIn('attempt:'+attempt_id,stats['invalid_documents'])

    def test_http_routes_require_session_and_current_workspace(self):
        with TestClient(app.app,base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post('/api/runtime/applicant/send/reserve',json={}).status_code,401)
            client.get('/workspace');headers={'X-Jobkit-Context':self.h.context['context_token']}
            recipient=self.confirm()['recipient']
            payload={'intent_id':self.intent['intent_id'],'recipient_binding_id':recipient['recipient_fingerprint'],
                     'expected_manual_revision':0,'expected_profile_revision':1,'expected_binding_revision':1,
                     'expected_window_revision':self.window,'confirmed':True}
            response=client.post('/api/runtime/applicant/send/reserve',json=payload,headers=headers)
            self.assertEqual(response.status_code,200)
            attempt_id=response.json()['data']['attempt']['attempt_id']
            self.assertEqual(client.get('/api/runtime/applicant/send/'+attempt_id,headers=headers).status_code,200)
            self.assertEqual(client.post('/api/runtime/applicant/send/handoff',json={'attempt_id':attempt_id,'confirmed':True},headers=headers).status_code,200)
            self.assertEqual(client.get('/api/runtime/applicant/send/handoff/'+attempt_id,headers=headers).status_code,200)
            self.assertEqual(client.post('/api/runtime/applicant/send/manual/claim',json={'attempt_id':attempt_id,'confirmed':True},headers=headers).status_code,200)
            self.assertEqual(client.get('/api/runtime/applicant/send/stats',headers=headers).status_code,200)
            self.assertEqual(client.get('/api/runtime/applicant/send/stats').status_code,409)
            self.h.switch(role='recruiter')
            self.assertEqual(client.get('/api/runtime/applicant/send/'+attempt_id,headers=headers).status_code,409)


if __name__=='__main__':unittest.main()
