"""Immutable reply provenance over the existing SQLite store; no execution or sending.

Every reply intent belongs to one conversation key. The key is recorded on the intent document and
on the draft task row, and it seeds the per-session message index, so two sessions can answer the
same message without either draft overwriting the other. Records written before this build carry no
key: they are read as the default (legacy) session, never migrated in place.
"""
import hashlib
import json
import re
import store
import applicant_messages
import applicant_reply_windows
from sandbox import KernelError

BINDING_FIELDS=('scope_id','context_token','party','messages','profile_revision','binding_revision',
                'browser_session_fingerprint','block_boundary_observed','draft_backend')
DEFAULT_CONVERSATION_KEY=applicant_reply_windows.DEFAULT_KEY
INTENT_FIELDS=set(BINDING_FIELDS)|{'intent_id','draft_task_id','source_observation_task_id','fingerprint','state',
                                   'recipient_verified','recency_verified','automation_ready','sent','sending_enabled','quota_consumed'}
SUMMARY_LIMIT=50


def fingerprint(binding):
    return hashlib.sha256(json.dumps(binding,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()


def task_key(row):
    """Draft rows written before this build carry an empty key: read them as the legacy session."""
    try:value=row['conversation_key']
    except (IndexError,KeyError):return DEFAULT_CONVERSATION_KEY
    return value or DEFAULT_CONVERSATION_KEY


def message_key(message_id,conversation_key=DEFAULT_CONVERSATION_KEY):
    """Per-session message index.

    The legacy key keeps the exact digest older builds wrote, so an upgrade still finds the old
    mapping; every other key gets its own row, so one message can be answered in two sessions
    without either draft overwriting the other.
    """
    seed=message_id if conversation_key==DEFAULT_CONVERSATION_KEY else message_id+'\x1f'+conversation_key
    return 'applicant_reply_message:'+hashlib.sha256(seed.encode('utf-8')).hexdigest()


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


def reference(c,scope_id,message_id,conversation_key=DEFAULT_CONVERSATION_KEY):
    try:
        document=store.runtime_document(c,scope_id,message_key(message_id,conversation_key))
    except (TypeError,ValueError,RecursionError):
        raise KernelError('INVALID_REPLY_INTENT_STATE','消息关联记录无法完整读取，未重复准备。') from None
    if not document['revision']:
        return None
    data=document['content']
    if (type(document['revision']) is not int or document['revision']!=1 or not isinstance(data,dict)
            or set(data) not in ({'intent_id','message_id'},{'intent_id','message_id','conversation_key'})
            or data['message_id']!=message_id
            or data.get('conversation_key',DEFAULT_CONVERSATION_KEY)!=conversation_key
            or not isinstance(data['intent_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',data['intent_id'])):
        raise KernelError('INVALID_REPLY_INTENT_STATE','消息与回复意图的关联无法核对，未重复准备。')
    return data['intent_id']


def read(c,scope_id,intent_id):
    try:
        document=store.runtime_document(c,scope_id,'applicant_reply_intent:'+intent_id)
        data=document['content']
        binding={key:data[key] for key in BINDING_FIELDS}
        conversation_key=data.get('conversation_key',DEFAULT_CONVERSATION_KEY)
        if (type(document['revision']) is not int or document['revision']!=1
                or set(data) not in (INTENT_FIELDS,INTENT_FIELDS|{'conversation_key'})
                or not applicant_reply_windows.valid_key(conversation_key)
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
                or task_key(task)!=conversation_key
                or payload!={'message':text.strip(),'document_kind':'profile','document_revision':data['profile_revision']}
                or any(reference(c,scope_id,m['message_id'],conversation_key)!=intent_id for m in messages)):
            raise ValueError('invalid task or mapping')
        return {**data,'conversation_key':conversation_key},task
    except (TypeError,ValueError,KeyError,AttributeError,RecursionError,KernelError):
        raise KernelError('INVALID_REPLY_INTENT_STATE','回复意图、消息依据或草稿关联无法完整核对。') from None


def summary(c,scope_id,conversation_key=DEFAULT_CONVERSATION_KEY,limit=SUMMARY_LIMIT):
    """Read-only digest of the drafts and intents pinned to one conversation key.

    Nothing here decides anything: the authoritative reads stay ``read``/``reply.get``. Rows written
    before this build (empty task key, no intent field) are reported under the legacy key.
    """
    if not applicant_reply_windows.valid_key(conversation_key) or type(limit) is not int or limit<1:
        raise KernelError('INVALID_CONVERSATION_KEY','会话键必须是 1..128 个不含空白的可见字符，且不能是显示名。',400)
    rows=c.execute("SELECT task_id,status,review_status,manual_revision,created_at FROM runtime_tasks "
                   "WHERE scope_id=? AND kind='work' AND tool='draft.reply' AND coalesce(nullif(conversation_key,''),?)=? "
                   "ORDER BY created_at DESC,task_id LIMIT ?",(scope_id,DEFAULT_CONVERSATION_KEY,conversation_key,limit+1)).fetchall()
    drafts=[{'task_id':row['task_id'],'status':row['status'],'review_status':row['review_status'],
             'manual_revision':row['manual_revision'],'conversation_key':conversation_key,'created_at':row['created_at']}
            for row in rows[:limit]]
    intents=[];unreadable=0
    for row in c.execute("SELECT kind,content FROM runtime_documents WHERE scope_id=? AND kind LIKE 'applicant_reply_intent:%' ORDER BY kind",(scope_id,)).fetchall():
        try:
            data=json.loads(row['content'])
            key=data.get('conversation_key',DEFAULT_CONVERSATION_KEY)
            identity,draft_id=data['intent_id'],data['draft_task_id']
        except (TypeError,ValueError,KeyError):
            unreadable+=1;continue
        if key!=conversation_key:
            continue
        intents.append({'intent_id':identity,'draft_task_id':draft_id,'state':data.get('state'),
                        'conversation_key':key,'message_ids':[m.get('message_id') for m in data.get('messages') or ()]})
    return {'drafts':drafts,'drafts_truncated':len(rows)>limit,'intents':intents[:limit],
            'intents_truncated':len(intents)>limit,'intents_unreadable':unreadable}
