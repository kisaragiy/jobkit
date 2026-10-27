"""Recipient anchoring, message-age fence, local send budget and readback evidence.

No send call exists here: a reservation is a local bookkeeping record, not a delivered message.
"""
import hashlib
import json
import math
import os
import re
import time
import store
import applicant_messages
import applicant_reply_window
from sandbox import KernelError

RECIPIENT_PREFIX='applicant_send_recipient:'
HANDOFF_PREFIX='applicant_send_handoff:'
CLAIM_PREFIX='applicant_send_claim:'
READBACK_PREFIX='applicant_send_readback:'
DEFAULT_DAILY_LIMIT=20
MAX_MESSAGE_AGE_SECONDS=120
READBACK_FIELDS=('scope_id','attempt_id','observed','ambiguous','reason','match_count','message_id',
                 'message_sha256','observed_at','party_match','delivery_confirmed','sending_enabled')
READBACK_REASONS=('live_snapshot_unavailable','draft_text_missing','no_matching_outgoing_message',
                  'matching_outgoing_message','conversation_changed','anchor_not_in_snapshot')
HANDOFF_FIELDS=('scope_id','attempt_id','intent_id','draft_task_id','recipient_fingerprint','party_display_name',
                'anchor_message_id','message_ids','draft_text_sha256','text_expires_at','created_at',
                'manual_handoff_only','sending_enabled')
CLAIM_FIELDS=('scope_id','attempt_id','handoff_revision','claimed_at','self_reported','delivery_confirmed','sending_enabled')
RECIPIENT_FIELDS=('scope_id','intent_id','draft_task_id','context_token','party','anchor_message_id','message_ids',
                  'profile_revision','binding_revision','browser_session_fingerprint','recipient_fingerprint',
                  'created_at','platform_account_id_verified','sending_enabled')
STAMP_FIELDS=('intent_id','draft_task_id','context_token','party','anchor_message_id','message_ids',
              'profile_revision','binding_revision','browser_session_fingerprint')


def daily_limit():
    raw=os.environ.get('JOBKIT_SEND_DAILY_LIMIT')
    try:value=int(raw) if raw not in (None,'') else DEFAULT_DAILY_LIMIT
    except (TypeError,ValueError):value=DEFAULT_DAILY_LIMIT
    return value if 1<=value<=200 else DEFAULT_DAILY_LIMIT


def budget_day(now=None):
    return time.strftime('%Y-%m-%d',time.localtime(time.time() if now is None else now))


def recipient_kind(intent_id):
    return RECIPIENT_PREFIX+intent_id


def fingerprint(data):
    """Identity of one send target: same intent, same party, same anchored messages, same facts."""
    fields={key:data[key] for key in STAMP_FIELDS}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def usable_id(value):
    return (isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_~.\-]{1,128}',value)
            and value.lower() not in {'undefined','null','nan','0','-1'})


def checked_recipient(document,scope_id,intent_id):
    try:
        data=document['content']
        if (type(document['revision']) is not int or document['revision']!=1 or not isinstance(data,dict)
                or set(data)!=set(RECIPIENT_FIELDS) or data['scope_id']!=scope_id or data['intent_id']!=intent_id
                or data['recipient_fingerprint']!=fingerprint(data)
                or any(data[k] is not False for k in ('platform_account_id_verified','sending_enabled'))
                or not isinstance(data['message_ids'],list) or not 1<=len(data['message_ids'])<=10
                or any(not usable_id(value) for value in data['message_ids'])
                or len(set(data['message_ids']))!=len(data['message_ids'])
                or data['anchor_message_id']!=data['message_ids'][-1]
                or any(type(data[k]) is not int or data[k]<1 for k in ('profile_revision','binding_revision'))
                or type(data['created_at']) not in (int,float) or not math.isfinite(data['created_at'])
                or not isinstance(data['browser_session_fingerprint'],str) or not re.fullmatch(r'[0-9a-f]{64}',data['browser_session_fingerprint'])
                or any(not isinstance(data[k],str) or not re.fullmatch(r'[0-9a-f]{32}',data[k]) for k in ('intent_id','draft_task_id','context_token'))):
            raise ValueError('invalid recipient')
        applicant_messages.checked({'status':'empty_or_loading','party':data['party'],'messages':[],'messages_truncated':False,
                                    'history_complete':False,'recipient_verified':False,'automation_ready':False})
        return data
    except (TypeError,KeyError,ValueError,RecursionError):
        raise KernelError('INVALID_SEND_RECIPIENT','发送对象绑定无法完整核对，未用于发送。') from None


def same_recipient(left,right):
    """Idempotent re-confirmation ignores only the creation stamp, never the identity fields."""
    return all(left[key]==right[key] for key in RECIPIENT_FIELDS if key!='created_at')


def budget_view(c,scope_id,now=None):
    day=budget_day(now);limit=daily_limit()
    rows=c.execute('SELECT state,count(*) FROM runtime_send_attempts WHERE scope_id=? AND budget_day=? GROUP BY state',
                   (scope_id,day)).fetchall()
    counts={state:count for state,count in rows}
    committed=counts.get('reserved',0)+counts.get('outcome_unknown',0)
    return {'budget_day':day,'daily_limit':limit,'committed_attempts':committed,
            'released_attempts':counts.get('released',0),'not_sent_attempts':counts.get('not_sent',0),
            'remaining':max(0,limit-committed),'quota_source':'local_workspace_not_platform'}


def attempt(c,scope_id,attempt_id):
    row=c.execute('SELECT * FROM runtime_send_attempts WHERE attempt_id=? AND scope_id=?',(attempt_id,scope_id)).fetchone()
    if row is None:
        raise KernelError('SEND_ATTEMPT_NOT_FOUND','找不到本次发送预占记录。')
    data=dict(row)
    if (data['state'] not in {'reserved','released','not_sent','outcome_unknown'} or data['reserved_units']!=1
            or not usable_id(data['attempt_id']) or not re.fullmatch(r'[0-9a-f]{32}',data['intent_id'] or '')
            or not re.fullmatch(r'[0-9a-f]{32}',data['draft_task_id'] or '')
            or not re.fullmatch(r'[0-9a-f]{64}',data['recipient_binding_id'] or '')
            or not re.fullmatch(r'[0-9a-f]{64}',data['message_sha256'] or '')
            or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',data['budget_day'] or '')):
        raise KernelError('INVALID_SEND_ATTEMPT','发送预占记录无法完整核对。')
    return data


def public_attempt(data):
    return {'attempt_id':data['attempt_id'],'intent_id':data['intent_id'],'draft_task_id':data['draft_task_id'],
            'recipient_binding_id':data['recipient_binding_id'],'budget_day':data['budget_day'],'state':data['state'],
            'message_sha256':data['message_sha256'],'created_at':data['created_at'],'updated_at':data['updated_at'],
            'sent':False,'sending_enabled':False,'delivery_confirmed':False}


def message_age(window,messages,now=None):
    """Messages must be new after the observation anchor, unchanged, and still inside the age fence."""
    now=time.time() if now is None else now
    ages=[]
    for message in messages:
        entry=window['seen'].get(message['message_id'])
        if (entry is None or entry['digest']!=applicant_reply_window.message_digest(message)
                or entry['new_after_start'] is not True):
            raise KernelError('MESSAGE_NOT_NEW_AFTER_WINDOW','这些消息不是当前观察起点之后新收到的，不能当作刚发生的对话。')
        ages.append(now-entry['first_seen_at'])
    age=max(ages) if ages else math.inf
    if age>MAX_MESSAGE_AGE_SECONDS:
        raise KernelError('MESSAGE_AGE_EXCEEDED','对方消息已超过两分钟没有被重新观察到，请重新读取会话后再核对发送对象。')
    return age


def window_age(window,now=None):
    now=time.time() if now is None else now
    age=now-window['last_observed_at']
    if not 0<=age<=applicant_reply_window.GAP_SECONDS:
        raise KernelError('WINDOW_GAP_EXCEEDED','消息观察已中断超过两分钟，不能把离线期间的消息当作刚收到。')
    return age


def readback_evidence(snapshot,draft_text,intent_message_ids):
    """Only an outgoing copy that appears AFTER the anchored question counts; never claims delivery."""
    if not isinstance(snapshot,dict) or snapshot.get('status')!='observed_messages':
        return {'observed':False,'ambiguous':False,'reason':'live_snapshot_unavailable','message_id':None,'message_sha256':None}
    text=(draft_text or '').strip()
    if not text:
        return {'observed':False,'ambiguous':False,'reason':'draft_text_missing','message_id':None,'message_sha256':None}
    known=set(intent_message_ids)
    messages=snapshot.get('messages') or []
    anchors=[index for index,message in enumerate(messages) if message.get('message_id') in known]
    if not anchors:
        # 锚点不在当前快照里，就无法证明这条消息是本次动作之后发出的。
        return {'observed':False,'ambiguous':False,'reason':'anchor_not_in_snapshot','message_id':None,'message_sha256':None}
    after=max(anchors)
    matches=[]
    for index,message in enumerate(messages):
        if (index>after and message.get('direction')=='outgoing' and message.get('kind')=='text'
                and message.get('full_text_available') is True and usable_id(message.get('message_id'))
                and message['message_id'] not in known and (message.get('text') or '').strip()==text):
            matches.append(message)
    if not matches:
        return {'observed':False,'ambiguous':False,'reason':'no_matching_outgoing_message','message_id':None,'message_sha256':None}
    latest=matches[-1]
    return {'observed':True,'ambiguous':len(matches)>1,'reason':'matching_outgoing_message',
            'message_id':latest['message_id'],'message_sha256':applicant_reply_window.message_digest(latest),
            'match_count':len(matches)}


def handoff_kind(attempt_id):
    return HANDOFF_PREFIX+attempt_id


def claim_kind(attempt_id):
    return CLAIM_PREFIX+attempt_id


def text_expiry(window,messages):
    """Fixed expiry from the first observation of each message; independent of when the card is requested."""
    stamps=[window['seen'][message['message_id']]['first_seen_at']+MAX_MESSAGE_AGE_SECONDS for message in messages]
    return min(stamps) if stamps else 0


def checked_handoff(document,scope_id,attempt_id):
    try:
        data=document['content']
        if (type(document['revision']) is not int or document['revision']!=1 or not isinstance(data,dict)
                or set(data)!=set(HANDOFF_FIELDS) or data['scope_id']!=scope_id or data['attempt_id']!=attempt_id
                or any(data[k] is not True for k in ('manual_handoff_only',))
                or data['sending_enabled'] is not False
                or any(not isinstance(data[k],str) or not re.fullmatch(r'[0-9a-f]{32}',data[k]) for k in ('attempt_id','intent_id','draft_task_id'))
                or not isinstance(data['recipient_fingerprint'],str) or not re.fullmatch(r'[0-9a-f]{64}',data['recipient_fingerprint'])
                or not isinstance(data['draft_text_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',data['draft_text_sha256'])
                or not isinstance(data['party_display_name'],str) or not 1<=len(data['party_display_name'])<=80
                or not isinstance(data['message_ids'],list) or not 1<=len(data['message_ids'])<=10
                or any(not usable_id(value) for value in data['message_ids'])
                or data['anchor_message_id']!=data['message_ids'][-1]
                or any(type(data[k]) not in (int,float) or not math.isfinite(data[k]) or data[k]<=0 for k in ('text_expires_at','created_at'))):
            raise ValueError('invalid handoff')
        return data
    except (TypeError,KeyError,ValueError,RecursionError):
        raise KernelError('INVALID_SEND_HANDOFF','人工交接记录无法完整核对。') from None


def checked_claim(document,scope_id,attempt_id):
    try:
        data=document['content']
        if (type(document['revision']) is not int or document['revision']!=1 or not isinstance(data,dict)
                or set(data)!=set(CLAIM_FIELDS) or data['scope_id']!=scope_id or data['attempt_id']!=attempt_id
                or data['self_reported'] is not True or data['delivery_confirmed'] is not False or data['sending_enabled'] is not False
                or data['handoff_revision']!=1
                or type(data['claimed_at']) not in (int,float) or not math.isfinite(data['claimed_at'])):
            raise ValueError('invalid claim')
        return data
    except (TypeError,KeyError,ValueError,RecursionError):
        raise KernelError('INVALID_SEND_CLAIM','手动发送报告无法完整核对。') from None


def readback_kind(attempt_id):
    return READBACK_PREFIX+attempt_id


def checked_readback(document,scope_id,attempt_id):
    try:
        data=document['content']
        if (type(document['revision']) is not int or document['revision']!=1 or not isinstance(data,dict)
                or set(data)!=set(READBACK_FIELDS) or data['scope_id']!=scope_id or data['attempt_id']!=attempt_id
                or not isinstance(data['observed'],bool) or not isinstance(data['ambiguous'],bool)
                or not isinstance(data['party_match'],bool)
                or data['reason'] not in READBACK_REASONS
                or type(data['match_count']) is not int or not 0<=data['match_count']<=20
                or data['delivery_confirmed'] is not False or data['sending_enabled'] is not False
                or type(data['observed_at']) not in (int,float) or not math.isfinite(data['observed_at'])):
            raise ValueError('invalid readback')
        if data['observed']:
            if (not usable_id(data['message_id']) or not isinstance(data['message_sha256'],str)
                    or not re.fullmatch(r'[0-9a-f]{64}',data['message_sha256'])
                    or data['match_count']<1 or data['reason']!='matching_outgoing_message'):
                raise ValueError('invalid observed readback')
        elif data['message_id'] is not None or data['message_sha256'] is not None or data['match_count']!=0:
            raise ValueError('invalid missed readback')
        return data
    except (TypeError,KeyError,ValueError,RecursionError):
        raise KernelError('INVALID_SEND_READBACK','送达回读记录无法完整核对。') from None


def supersedes(previous,fresh):
    """Evidence only ever strengthens: a hit may not silently fall back to a miss."""
    if previous is None:
        return True
    if previous['observed'] and not fresh['observed']:
        raise KernelError('READBACK_REGRESSION','已记录的送达证据不允许被一次未命中覆盖；请保留证据并重新核对会话。')
    keys=('observed','ambiguous','reason','match_count','message_id','message_sha256','party_match')
    return any(previous[key]!=fresh[key] for key in keys)
