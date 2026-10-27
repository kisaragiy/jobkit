"""Atomic observed-message reply preparation and local rules only; fictional records."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import os
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import runtime_tools
import store
import test_applicant_messages as fixture
from sandbox import KernelError


def message(identity='mid-123',text='请问何时到岗？',**changes):
    return {**fixture.snapshot()['messages'][0],'message_id':identity,'text':text,**changes}


class ObservedReplyIntentTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SelectedMessagesTest();self.f.setUp();self.addCleanup(self.f.doCleanups);self.h=self.f.h
        self.messages=[message()];self.source=self.observe()

    def dispatch(self,tool,payload=None):return main.dispatch(tool,payload,self.h.context['context_token'])

    def observe(self,messages=None):
        value={**fixture.snapshot(),'messages':deepcopy(messages if messages is not None else self.messages)}
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value=value):
            return self.dispatch('runtime.applicant.messages.read',{})

    def prepare(self,**changes):
        return self.dispatch('runtime.applicant.reply.prepare',{'message_observation_task_id':self.source['trace_id'],
            'message_ids':[m['message_id'] for m in self.messages],'expected_profile_revision':1,'expected_binding_revision':1,**changes})

    def counts(self):
        with store.runtime_db() as c:
            return (c.execute("SELECT count(*) FROM runtime_tasks WHERE kind='work' AND tool='draft.reply'").fetchone()[0],
                    c.execute("SELECT count(*) FROM runtime_documents WHERE kind LIKE 'applicant_reply_intent:%'").fetchone()[0])

    def test_prepare_queues_one_normal_draft_and_records_unsent_provenance(self):
        saved=self.prepare()['data'];intent=saved['intent']
        self.assertEqual(self.counts(),(1,1));self.assertEqual(saved['task']['status'],'queued')
        self.assertEqual(intent['source_observation_task_id'],self.source['trace_id'])
        self.assertEqual(intent['messages'][0]['message_id'],'mid-123')
        for k in ('recipient_verified','recency_verified','automation_ready','sent','sending_enabled','quota_consumed'):
            self.assertIs(intent[k],False)
        task=self.h.call('runtime.task.execute',{'task_id':saved['task']['task_id']})
        self.assertEqual(task['status'],'completed');self.assertIn('一个月内',task['result']['text'])
        current=self.h.call('runtime.applicant.reply.get',{'intent_id':intent['intent_id']})
        self.assertEqual(current['task']['status'],'completed');self.assertFalse(current['profile_stale'])

    def test_all_trailing_incoming_questions_are_joined_in_one_draft(self):
        self.messages=[message('mid-1','学历是什么？'),message('mid-2','何时到岗？')];self.source=self.observe()
        with self.assertRaises(KernelError) as caught:self.prepare(message_ids=['mid-2'])
        self.assertEqual(caught.exception.code,'REPLY_BLOCK_MISMATCH')
        saved=self.prepare()['data'];self.assertIn('学历是什么？\n\n何时到岗？',saved['task']['message'])
        done=self.h.call('runtime.task.execute',{'task_id':saved['task']['task_id']})
        self.assertIn('本科',done['result']['text']);self.assertIn('一个月内',done['result']['text'])

    def test_repeat_and_fresh_observation_reuse_same_intent_and_task(self):
        first=self.prepare()['data'];self.source=self.observe();second=self.prepare()['data']
        self.assertEqual(first['intent'],second['intent']);self.assertEqual(first['task']['task_id'],second['task']['task_id'])
        self.assertNotEqual(first['fresh_observation_task_id'],second['fresh_observation_task_id'])
        self.assertEqual(self.counts(),(1,1))

    def test_concurrent_preparation_is_atomic_per_message(self):
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda _:self.prepare()['data'],range(3)))
        self.assertEqual(len({r['intent']['intent_id'] for r in results}),1);self.assertEqual(self.counts(),(1,1))

    def test_partially_overlapping_block_does_not_queue_another_reply(self):
        self.prepare();self.messages.append(message('new-mid','最高学历是什么？'));self.source=self.observe()
        with self.assertRaises(KernelError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'REPLY_MESSAGE_OVERLAP');self.assertEqual(self.counts(),(1,1))

    def test_new_question_after_observed_outgoing_boundary_gets_its_own_intent(self):
        first=self.prepare()['data']
        self.source=self.observe([message(),message('own-reply','已回复',direction='outgoing'),message('new-question','最高学历是什么？')])
        second=self.prepare(message_ids=['new-question'])['data']
        self.assertNotEqual(first['intent']['intent_id'],second['intent']['intent_id'])
        self.assertTrue(second['intent']['block_boundary_observed']);self.assertEqual(self.counts(),(2,2))

    def test_changed_message_body_under_same_id_is_conflict_not_new_reply(self):
        self.prepare();self.messages=[message(text='changed question')];self.source=self.observe()
        with self.assertRaises(KernelError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'REPLY_INTENT_CONFLICT');self.assertEqual(self.counts(),(1,1))

    def test_last_outgoing_media_card_or_unknown_direction_is_not_skipped(self):
        for tail in (message('tail',direction='outgoing'),message('tail',kind='media',full_text_available=False),
                     message('tail',kind='card',full_text_available=False),message('tail',direction='unknown')):
            self.source=self.observe([message(),tail])
            with self.subTest(tail=tail),self.assertRaises(KernelError) as caught:self.prepare(message_ids=['mid-123'])
            self.assertEqual(caught.exception.code,'MESSAGE_BLOCK_REVIEW_REQUIRED')
        self.source=self.observe([message('unclear',direction='unknown'),message()])
        with self.assertRaises(KernelError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'MESSAGE_BLOCK_REVIEW_REQUIRED')
        self.assertEqual(self.counts(),(0,0))

    def test_truncated_text_missing_or_placeholder_ids_cannot_prepare(self):
        for changes in ({'text_truncated':True,'full_text_available':False},{'message_id':None,'message_id_source':'unavailable'},
                        {'message_id':'undefined'},{'message_id':'0'}):
            self.source=self.observe([message(**changes)])
            with self.subTest(changes=changes),self.assertRaises(KernelError):self.prepare()
        self.assertEqual(self.counts(),(0,0))

    def test_block_limits_refuse_instead_of_truncating_or_splitting(self):
        self.messages=[message('mid-'+str(i)) for i in range(11)];self.source=self.observe()
        with self.assertRaises(KernelError):self.prepare(message_ids=[m['message_id'] for m in self.messages[:10]])
        self.messages=[message('mid-'+str(i),'x'*3000) for i in range(3)];self.source=self.observe()
        with self.assertRaises(KernelError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'REPLY_BLOCK_TOO_LARGE');self.assertEqual(self.counts(),(0,0))

    def test_expired_future_or_cross_context_message_observation_is_refused(self):
        for offset in (-301,30):
            with store.runtime_db() as c:c.execute("UPDATE runtime_tasks SET updated_at=strftime('%s','now')+? WHERE task_id=?",(offset,self.source['trace_id']))
            with self.assertRaises(KernelError) as caught:self.prepare()
            self.assertEqual(caught.exception.code,'FRESH_MESSAGES_REQUIRED')
        self.source=self.observe();self.h.switch(role='applicant',organization='other',account='other')
        with self.assertRaises(KernelError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'FRESH_MESSAGES_REQUIRED')

    def test_preview_task_and_client_supplied_text_cannot_be_used(self):
        with self.assertRaises(KernelError) as caught:self.prepare(message='forged')
        self.assertEqual(caught.exception.code,'INVALID_OBSERVED_REPLY')
        with mock.patch.object(runtime_tools.applicant_browser,'read_chat',return_value={'status':'empty_or_loading','rows':[],'automation_ready':False,'list_truncated':False}):
            preview=self.dispatch('runtime.applicant.conversations.read',{})
        with self.assertRaises(KernelError) as caught:self.prepare(message_observation_task_id=preview['trace_id'])
        self.assertEqual(caught.exception.code,'FRESH_MESSAGES_REQUIRED')
        with self.assertRaises(KernelError) as caught:self.prepare(message_observation_task_id='f'*32)
        self.assertEqual(caught.exception.code,'FRESH_MESSAGES_REQUIRED')

    def test_profile_and_binding_versions_are_fenced(self):
        with self.assertRaises(KernelError) as caught:self.prepare(expected_binding_revision=2)
        self.assertEqual(caught.exception.code,'STALE_REPLY_FACTS')
        saved=self.prepare()['data']
        self.h.save({'education':'本科','available':'两周内'},revision=1)
        with self.assertRaises(KernelError) as caught:self.prepare(expected_profile_revision=2)
        self.assertEqual(caught.exception.code,'REPLY_INTENT_CONFLICT')
        current=self.h.call('runtime.applicant.reply.get',{'intent_id':saved['intent']['intent_id']})
        self.assertTrue(current['profile_stale'])

    def test_remote_draft_configuration_is_refused_without_model_io(self):
        with mock.patch.dict(os.environ,{'JOBKIT_DRAFT_BACKEND':'llm'}):
            with self.assertRaises(KernelError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'OBSERVED_REPLY_RULES_REQUIRED');self.assertEqual(self.counts(),(0,0))

    def test_queue_provenance_and_all_message_refs_roll_back_with_receipt(self):
        with mock.patch('execution_engine.record_commit',side_effect=KernelError('FIXTURE','fixture')):
            with self.assertRaises(KernelError):self.prepare()
        self.assertEqual(self.counts(),(0,0))
        with store.runtime_db() as c:self.assertEqual(c.execute("SELECT count(*) FROM runtime_documents WHERE kind LIKE 'applicant_reply_message:%'").fetchone()[0],0)

    def test_commit_recovery_does_not_replay_after_draft_was_executed(self):
        saved=self.prepare();self.h.call('runtime.task.execute',{'task_id':saved['data']['task']['task_id']})
        with store.runtime_db() as c:c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?",(saved['trace_id'],))
        tool=main._router._tools['runtime.applicant.reply.prepare']
        with mock.patch.dict(main._router._tools,{tool.name:replace(tool,handler=mock.Mock(side_effect=AssertionError('no replay')))}):
            recovered=self.h.call('runtime.task.recover',{'task_id':saved['trace_id']})
        self.assertEqual(recovered['status'],'completed');self.assertEqual(recovered['result'],saved['data'])
        self.assertEqual(self.counts(),(1,1))

    def test_corrupt_mapping_or_task_source_blocks_intent_read(self):
        saved=self.prepare()['data']
        with store.runtime_db() as c:c.execute("UPDATE runtime_documents SET content='{}' WHERE kind LIKE 'applicant_reply_message:%'")
        with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.reply.get',{'intent_id':saved['intent']['intent_id']})
        self.assertEqual(caught.exception.code,'INVALID_REPLY_INTENT_STATE')
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_documents SET content=? WHERE kind LIKE 'applicant_reply_message:%'",(json.dumps({'intent_id':saved['intent']['intent_id'],'message_id':'mid-123'}),))
            c.execute('UPDATE runtime_tasks SET result=NULL WHERE task_id=?',(self.source['trace_id'],))
        with self.assertRaises(KernelError) as caught:self.h.call('runtime.applicant.reply.get',{'intent_id':saved['intent']['intent_id']})
        self.assertEqual(caught.exception.code,'INVALID_REPLY_INTENT_STATE')

    def test_http_routes_require_session_and_current_workspace(self):
        with TestClient(app.app,base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post('/api/runtime/applicant/replies/prepare',json={}).status_code,401)
            client.get('/workspace');headers={'X-Jobkit-Context':self.h.context['context_token']}
            payload={'message_observation_task_id':self.source['trace_id'],'message_ids':['mid-123'],'expected_profile_revision':1,'expected_binding_revision':1}
            response=client.post('/api/runtime/applicant/replies/prepare',json=payload,headers=headers)
            self.assertEqual(response.status_code,200)
            identity=response.json()['data']['intent']['intent_id']
            self.assertEqual(client.get('/api/runtime/applicant/replies/'+identity,headers=headers).status_code,200)
            self.h.switch(role='recruiter')
            self.assertEqual(client.get('/api/runtime/applicant/replies/'+identity,headers=headers).status_code,409)


if __name__=='__main__':unittest.main()
