"""Immutable reply provenance over the existing SQLite store; no execution or sending."""
import hashlib
import json
import re
import store
import applicant_messages
from sandbox import KernelError

BINDING_FIELDS=('scope_id','context_token','party','messages','profile_revision','binding_revision',
                'browser_session_fingerprint','block_boundary_observed','draft_backend')


def fingerprint(binding):
    return hashlib.sha256(json.dumps(binding,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()


def message_key(message_id):
    return 'applicant_reply_message:'+hashlib.sha256(message_id.encode('utf-8')).hexdigest()


def block(snapshot):
    applicant_messages.checked(snapshot)
    if snapshot['status']!='observed_messages' or not snapshot['messages']:
        raise KernelError('MESSAGE_BLOCK_REQUIRED','请先读取当前选中会话的正文。')
    pending=[]
    for message in reversed(snapshot['messages']):
        if message['direction']!='incoming':
            break
        pending.insert(0,message)
    preceding=len(snapshot['messages'])-len(pending)-1
    if preceding>=0 and snapshot['messages'][preceding]['direction']=='unknown':
        raise KernelError('MESSAGE_BLOCK_REVIEW_REQUIRED','问题块前存在方向未核对的消息，请人工确认完整问题后处理。')
    if (not pending or len(pending)>10 or any(message['kind']!='text' or not message['full_text_available']
            or message['text_truncated'] or not message['message_id']
            or message['message_id'].lower() in {'undefined','null','nan','0','-1'}
            or message['message_id_source']!='dom_data_mid' for message in pending)):
        raise KernelError('MESSAGE_BLOCK_REVIEW_REQUIRED','末尾消息不是可完整读取且有标识的对方纯文本，请人工核对。')
    text='\n\n'.join(message['text'] for message in pending)
    if len(text)>8000:
        raise KernelError('REPLY_BLOCK_TOO_LARGE','连续问题超过本次草稿容量，请人工核对，不会截断或拆成重复回复。')
    return pending,text,len(pending)<len(snapshot['messages'])


def reference(c,scope_id,message_id):
    try:
        document=store.runtime_document(c,scope_id,message_key(message_id))
    except (TypeError,ValueError,RecursionError):
        raise KernelError('INVALID_REPLY_INTENT_STATE','消息关联记录无法完整读取，未重复准备。') from None
    if not document['revision']:
        return None
    data=document['content']
    if (type(document['revision']) is not int or document['revision']!=1 or not isinstance(data,dict)
            or set(data)!={'intent_id','message_id'} or data['message_id']!=message_id
            or not isinstance(data['intent_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',data['intent_id'])):
        raise KernelError('INVALID_REPLY_INTENT_STATE','消息与回复意图的关联无法核对，未重复准备。')
    return data['intent_id']


def read(c,scope_id,intent_id):
    try:
        document=store.runtime_document(c,scope_id,'applicant_reply_intent:'+intent_id)
        data=document['content']
        binding={key:data[key] for key in BINDING_FIELDS}
        if (type(document['revision']) is not int or document['revision']!=1 or set(data)!=set(BINDING_FIELDS)|{
            'intent_id','draft_task_id','source_observation_task_id','fingerprint','state','recipient_verified',
            'recency_verified','automation_ready','sent','sending_enabled','quota_consumed'}
            or data['scope_id']!=scope_id or data['intent_id']!=intent_id or data['fingerprint']!=fingerprint(binding)
            or data['state']!='prepared_unsent' or data['draft_backend']!='rules'
            or any(data[k] is not False for k in ('recipient_verified','recency_verified','automation_ready','sent','sending_enabled','quota_consumed'))
            or type(data['block_boundary_observed']) is not bool
            or any(type(data[k]) is not int or data[k]<1 for k in ('profile_revision','binding_revision'))
            or any(not isinstance(data[k],str) or not re.fullmatch(r'[0-9a-f]{32}',data[k]) for k in ('intent_id','draft_task_id','source_observation_task_id','context_token'))
            or not isinstance(data['browser_session_fingerprint'],str) or not re.fullmatch(r'[0-9a-f]{64}',data['browser_session_fingerprint'])):
            raise ValueError('invalid provenance')
        snapshot={'status':'observed_messages','party':data['party'],'messages':data['messages'],'messages_truncated':False,
                  'history_complete':False,'recipient_verified':False,'automation_ready':False}
        messages,text,_=block(snapshot)
        source=c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',(data['source_observation_task_id'],scope_id)).fetchone()
        original=json.loads(source['result']) if source is not None else None
        if (source is None or source['tool']!='runtime.applicant.messages.read' or source['status']!='completed'
                or source['context_token']!=data['context_token'] or not isinstance(original,dict)
                or original.get('source_type')!='boss_selected_message_dom' or original.get('binding_revision')!=data['binding_revision']):
            raise ValueError('missing original observation')
        original_snapshot={key:original[key] for key in ('status','party','messages','messages_truncated','history_complete','recipient_verified','automation_ready')}
        original_messages,_,boundary=block(original_snapshot)
        if original_messages!=messages or original_snapshot['party']!=data['party'] or boundary!=data['block_boundary_observed']:
            raise ValueError('original observation mismatch')
        task=c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',(data['draft_task_id'],scope_id)).fetchone()
        payload=json.loads(task['payload']) if task is not None else None
        if (task is None or task['tool']!='draft.reply' or task['kind']!='work' or task['effect']!='none'
                or task['context_token']!=data['context_token'] or task['conversation_note']!=data['party']['display_name']
                or payload!={'message':text.strip(),'document_kind':'profile','document_revision':data['profile_revision']}
                or any(reference(c,scope_id,m['message_id'])!=intent_id for m in messages)):
            raise ValueError('invalid task or mapping')
        return data,task
    except (TypeError,ValueError,KeyError,AttributeError,RecursionError,KernelError):
        raise KernelError('INVALID_REPLY_INTENT_STATE','回复意图、消息依据或草稿关联无法完整核对。') from None
