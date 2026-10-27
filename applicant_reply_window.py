"""Bounded observation checkpoints, not a scheduler or platform timestamp proof."""
import hashlib
import json
import math
import re
import store
import applicant_messages
import applicant_reply_windows
from sandbox import KernelError

KIND='applicant_reply_window'
GAP_SECONDS=120
MAX_SEEN=200
DEFAULT_CONVERSATION_KEY=applicant_reply_windows.DEFAULT_KEY



def usable_id(value):
    return (isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_~.\-]{1,128}',value)
            and value.lower() not in {'undefined','null','nan','0','-1'})


def message_digest(message):
    # Relative timestamp labels may change without changing the actual message.
    fields={key:message[key] for key in ('message_id','message_id_source','direction','kind','text','text_truncated','full_text_available')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def snapshot(value):
    selected={key:value[key] for key in ('status','party','messages','messages_truncated','history_complete','recipient_verified','automation_ready')}
    applicant_messages.checked(selected)
    if selected['status']!='observed_messages' or not selected['messages'] or not usable_id(selected['messages'][-1]['message_id']):
        raise KernelError('WINDOW_MESSAGES_REQUIRED','请先读取有消息标识的当前会话正文。')
    return selected


def checked(document,scope_id):
    try:
        data=document['content']
        required={'scope_id','context_token','profile_revision','binding_revision','browser_session_fingerprint','party',
                  'started_at','last_observed_at','start_observation_task_id','last_observation_task_id','tail_message_id','seen',
                  'sending_enabled','automatic_polling'}
        valid=(type(document['revision']) is int and document['revision']>=1 and isinstance(data,dict) and set(data)==required
               and data['scope_id']==scope_id and all(data[k] is False for k in ('sending_enabled','automatic_polling'))
               and all(type(data[k]) is int and data[k]>0 for k in ('profile_revision','binding_revision'))
               and all(isinstance(data[k],str) and re.fullmatch(r'[0-9a-f]{32}',data[k]) for k in ('context_token','start_observation_task_id','last_observation_task_id'))
               and isinstance(data['browser_session_fingerprint'],str) and re.fullmatch(r'[0-9a-f]{64}',data['browser_session_fingerprint'])
               and all(type(data[k]) in (int,float) and math.isfinite(data[k]) for k in ('started_at','last_observed_at'))
               and 0<data['started_at']<=data['last_observed_at']
               and isinstance(data['seen'],dict) and 1<=len(data['seen'])<=MAX_SEEN
               and usable_id(data['tail_message_id']) and data['tail_message_id'] in data['seen'])
        if not valid:raise ValueError('invalid observation window')
        applicant_messages.checked({'status':'empty_or_loading','party':data['party'],'messages':[],'messages_truncated':False,
                                    'history_complete':False,'recipient_verified':False,'automation_ready':False})
        if data['party'] is None:raise ValueError('missing party')
        for identity,entry in data['seen'].items():
            if (not usable_id(identity) or not isinstance(entry,dict) or set(entry)!={'digest','first_seen_at','new_after_start'}
                    or not isinstance(entry['digest'],str) or not re.fullmatch(r'[0-9a-f]{64}',entry['digest'])
                    or type(entry['new_after_start']) is not bool or type(entry['first_seen_at']) not in (int,float)
                    or not math.isfinite(entry['first_seen_at']) or not data['started_at']<=entry['first_seen_at']<=data['last_observed_at']):
                raise ValueError('invalid seen message')
        return data
    except (TypeError,KeyError,ValueError,KernelError,RecursionError):
        raise KernelError('INVALID_REPLY_WINDOW','消息观察边界记录无法完整核对。') from None


def container_of(document):
    """Storage container for one workspace; migrates the legacy flat window on read."""
    try:
        return applicant_reply_windows.load(document['content'],document['revision'])
    except (TypeError,KeyError,ValueError):
        raise KernelError('INVALID_REPLY_WINDOW','消息观察边界记录无法完整核对。') from None


def view(document,container,conversation_key):
    """Per-key projection: revision is this key's own revision, never another session's."""
    item=applicant_reply_windows.get(container,conversation_key)
    return {'revision':item['revision'] if item else 0,'content':item['window'] if item else {},
            'document_revision':document['revision'],'conversation_key':conversation_key,'container':container},item


def witnessed(c,scope_id,data):
    sources=[]
    for key,time_key in (('start_observation_task_id','started_at'),('last_observation_task_id','last_observed_at')):
        row=c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',(data[key],scope_id)).fetchone()
        observed=json.loads(row['result']) if row is not None else None
        if (row is None or row['tool']!='runtime.applicant.messages.read' or row['status']!='completed'
                or row['context_token']!=data['context_token'] or row['updated_at']!=data[time_key]
                or observed['source_type']!='boss_selected_message_dom' or observed['binding_revision']!=data['binding_revision']):
            raise ValueError('missing observation witness')
        selected=snapshot(observed)
        if selected['party']!=data['party']:raise ValueError('different party')
        sources.append(selected)
    baseline_ids={m['message_id'] for m in sources[0]['messages'] if usable_id(m['message_id'])}
    if any(data['seen'][identity]['new_after_start'] for identity in baseline_ids if identity in data['seen']):
        raise ValueError('baseline message marked new')
    if sources[-1]['messages'][-1]['message_id']!=data['tail_message_id']:raise ValueError('invalid anchor')
    for message in sources[-1]['messages']:
        if usable_id(message['message_id']):
            if data['seen'].get(message['message_id'],{}).get('digest')!=message_digest(message):raise ValueError('invalid message witness')
    return data


def read(c,scope_id,conversation_key=DEFAULT_CONVERSATION_KEY):
    """Read one conversation's window. Defaults to the legacy key, so older callers are unchanged."""
    try:
        key=applicant_reply_windows.checked_key(conversation_key)
        document=store.runtime_document(c,scope_id,KIND)
        if not document['revision']:
            return {'revision':0,'content':{},'document_revision':0,'conversation_key':key,
                    'container':applicant_reply_windows.empty()},None
        container=container_of(document)
        projection,item=view(document,container,key)
        if item is None:return projection,None
        return projection,witnessed(c,scope_id,checked({'revision':item['revision'],'content':item['window']},scope_id))
    except (TypeError,ValueError,KeyError,RecursionError,KernelError):
        raise KernelError('INVALID_REPLY_WINDOW','消息观察边界与原读取记录无法对应。') from None



def advance(previous,current,observed_at):
    if current['party']!=previous['party']:
        raise KernelError('WINDOW_PARTY_CHANGED','选中会话已变化，请重新建立观察起点。')
    messages=current['messages'];ids=[message['message_id'] for message in messages]
    if previous['tail_message_id'] not in ids:
        raise KernelError('WINDOW_ANCHOR_MISSING','无法找到上次消息锚点，请重新核对会话连续性。')
    anchor=ids.index(previous['tail_message_id']);seen={key:dict(value) for key,value in previous['seen'].items()}
    for index,message in enumerate(messages):
        identity=message['message_id']
        if not usable_id(identity):
            if index>anchor:raise KernelError('WINDOW_MESSAGE_ID_REQUIRED','新消息缺少可核对标识，观察已停止。')
            continue
        digest=message_digest(message)
        if identity in seen:
            if seen[identity]['digest']!=digest:raise KernelError('WINDOW_CONTENT_CHANGED','已观察消息的正文或方向变化，请人工核对。')
        else:
            seen[identity]={'digest':digest,'first_seen_at':observed_at,'new_after_start':index>anchor}
    if len(seen)>MAX_SEEN:
        raise KernelError('WINDOW_CAPACITY_REACHED','本次观察已达到容量，请核对后重新建立观察起点；不会遗忘旧消息并误当新消息。')
    return seen
