"""Forget reply intents and the message bodies behind them; irreversible, evidence-chain protected.

Deletion removes the intent record, the per-message mappings, and the draft body inside the
draft task. Task skeletons, send attempts and交接 records are evidence: when any of them exist
for an intent, the whole request is refused rather than partially applied.
"""
import json
import re

import applicant_reply
import store
from sandbox import KernelError

INTENT_PREFIX = 'applicant_reply_intent:'
GUARD_PREFIXES = ('applicant_send_handoff:', 'applicant_send_claim:', 'applicant_send_readback:')
TASK_BODY_KEYS = ('text', 'draft', 'message')
REDACTED_NOTE = '已删除'


def _selector(payload):
    if not isinstance(payload, dict):
        raise KernelError('INVALID_FORGET_SELECTOR', '请指定要删除的回复意图或会话。', 400)
    keys = set(payload) & {'intent_id', 'party_display_name'}
    if len(keys) != 1:
        raise KernelError('INVALID_FORGET_SELECTOR', '请只指定一个回复意图或一个会话名称。', 400)
    key = keys.pop()
    value = payload[key]
    if key == 'intent_id':
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
            raise KernelError('INVALID_FORGET_SELECTOR', '请提供有效的回复意图标识。', 400)
    elif not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise KernelError('INVALID_FORGET_SELECTOR', '请提供要忘记的会话名称。', 400)
    return key, (value if key == 'intent_id' else value.strip())


def _intents(c, scope_id):
    rows = c.execute("SELECT kind,content FROM runtime_documents WHERE scope_id=? AND kind LIKE ?",
                     (scope_id, INTENT_PREFIX + '%')).fetchall()
    result = []
    for kind, raw in rows:
        try:
            data = json.loads(raw)
        except ValueError:
            raise KernelError('INVALID_REPLY_INTENT_STATE', '存在无法解析的回复意图记录，未执行任何删除。') from None
        if not isinstance(data, dict):
            raise KernelError('INVALID_REPLY_INTENT_STATE', '存在结构异常的回复意图记录，未执行任何删除。')
        result.append({'intent_id': kind[len(INTENT_PREFIX):], 'data': data})
    return result


def _selected(c, scope_id, payload):
    key, value = _selector(payload)
    intents = _intents(c, scope_id)
    if key == 'intent_id':
        chosen = [intent for intent in intents if intent['intent_id'] == value]
    else:
        chosen = [intent for intent in intents
                  if isinstance(intent['data'].get('party'), dict)
                  and intent['data']['party'].get('display_name') == value]
    if not chosen:
        raise KernelError('REPLY_INTENT_NOT_FOUND', '没有找到可删除的回复意图记录。', 404)
    return chosen


def _blocked(c, scope_id, intent_id):
    """Evidence that already points at this intent must not be broken by a delete."""
    reasons = []
    if c.execute('SELECT 1 FROM runtime_send_attempts WHERE scope_id=? AND intent_id=?',
                 (scope_id, intent_id)).fetchone():
        reasons.append('send_attempt_exists')
    for prefix in GUARD_PREFIXES:
        if c.execute('SELECT 1 FROM runtime_documents WHERE scope_id=? AND kind=?',
                     (scope_id, prefix + intent_id)).fetchone():
            reasons.append(prefix.rstrip(':') + '_exists')
    return reasons


def _message_keys(data):
    keys, seen = [], set()
    messages = data.get('messages')
    if not isinstance(messages, list):
        return keys
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get('message_id'), str):
            key = applicant_reply.message_key(message['message_id'])
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _shape(data):
    draft = data.get('draft_task_id')
    if not isinstance(draft, str) or not re.fullmatch(r'[0-9a-f]{32}', draft):
        raise KernelError('INVALID_REPLY_INTENT_STATE', '回复意图缺少有效的草稿关联，未执行任何删除。')
    return [INTENT_PREFIX + '', *( _message_keys(data))], draft


def review(c, scope_id, payload):
    """Read-only: what would be removed, and what blocks it."""
    chosen = _selected(c, scope_id, payload)
    blocked, documents, tasks = {}, 0, []
    for intent in chosen:
        reasons = _blocked(c, scope_id, intent['intent_id'])
        if reasons:
            blocked[intent['intent_id']] = reasons
            continue
        keys, draft = _shape(intent['data'])
        documents += 1 + len(keys[1:])
        tasks.append(draft)
    return {'selector': _selector(payload)[0], 'intents': [intent['intent_id'] for intent in chosen],
            'documents_to_delete': documents, 'tasks_to_redact': len(tasks), 'blocked': blocked,
            'irreversible': True, 'applied': False,
            'kept': '任务骨架、提交凭据与运行轨迹保留；正文与对方显示名抹除。'}


def _redact_observation(c,task_id):
    """A06: the original messages.read result keeps the full body; forgetting must clear it too."""
    row=c.execute('SELECT result FROM runtime_tasks WHERE task_id=?',(task_id,)).fetchone()
    if row is None or not row['result']:
        return False
    try:
        result=json.loads(row['result'])
    except ValueError:
        raise KernelError('INVALID_REPLY_INTENT_STATE','原始观察结果无法解析，未执行任何删除。') from None
    changed=False
    if isinstance(result,dict) and isinstance(result.get('messages'),list):
        for message in result['messages']:
            if isinstance(message,dict) and message.get('text'):
                message['text']='';changed=True
    if changed:
        c.execute('UPDATE runtime_tasks SET result=?,conversation_note=? WHERE task_id=?',
                  (json.dumps(result,ensure_ascii=False),REDACTED_NOTE,task_id))
    return changed


def _shared_observation(c,scope_id,removing,source_task):
    """Another live intent still depends on the same observation: refuse instead of breaking it."""
    rows=c.execute('SELECT kind,content FROM runtime_documents WHERE scope_id=? AND kind LIKE ?',
                   (scope_id,INTENT_PREFIX+'%')).fetchall()
    for kind,raw in rows:
        if kind[len(INTENT_PREFIX):] in removing:
            continue
        try:
            data=json.loads(raw)
        except ValueError:
            continue
        if isinstance(data,dict) and data.get('source_observation_task_id')==source_task:
            return True
    return False


def _redact(c, task_id):
    row = c.execute('SELECT payload,result FROM runtime_tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row['payload'])
    except (TypeError, ValueError):
        raise KernelError('INVALID_REPLY_INTENT_STATE', '草稿任务无法解析，未执行任何删除。') from None
    if not isinstance(payload, dict):
        raise KernelError('INVALID_REPLY_INTENT_STATE', '草稿任务结构异常，未执行任何删除。')
    if 'message' in payload:
        payload['message'] = ''
    result = None
    if row['result']:
        try:
            result = json.loads(row['result'])
        except ValueError:
            result = None
        if isinstance(result, dict):
            for key in TASK_BODY_KEYS:
                if key in result:
                    result[key] = ''
    c.execute('UPDATE runtime_tasks SET payload=?,result=?,conversation_note=? WHERE task_id=?',
              (json.dumps(payload, ensure_ascii=False), json.dumps(result, ensure_ascii=False) if result is not None else row['result'],
               REDACTED_NOTE, task_id))
    return True


def forget(c, scope_id, payload):
    """Delete the selected intents and their bodies. Caller owns the transaction."""
    chosen = _selected(c, scope_id, payload)
    blocked = {intent['intent_id']: _blocked(c, scope_id, intent['intent_id']) for intent in chosen}
    blocked = {key: value for key, value in blocked.items() if value}
    if blocked:
        raise KernelError('FORGET_BLOCKED_BY_EVIDENCE',
                          '这些回复意图已有发送或交接记录，删除会破坏证据链，已整体拒绝。')
    removing = {intent['intent_id'] for intent in chosen}
    for intent in chosen:
        source = intent['data'].get('source_observation_task_id')
        if isinstance(source, str) and _shared_observation(c, scope_id, removing, source):
            raise KernelError('FORGET_SHARED_OBSERVATION',
                              '原始观察结果仍被其他回复意图引用，删除会破坏他人证据，已整体拒绝。')
    documents = 0
    redacted = 0
    observations = 0
    for intent in chosen:
        keys, draft = _shape(intent['data'])
        kinds = [INTENT_PREFIX + intent['intent_id'], *keys[1:]]
        marks = ','.join('?' * len(kinds))
        documents += c.execute(f'DELETE FROM runtime_documents WHERE scope_id=? AND kind IN ({marks})',
                               (scope_id, *kinds)).rowcount
        redacted += 1 if _redact(c, draft) else 0
        source = intent['data'].get('source_observation_task_id')
        if isinstance(source, str):
            observations += 1 if _redact_observation(c, source) else 0
    return {'intents': [intent['intent_id'] for intent in chosen], 'deleted_documents': documents,
            'redacted_tasks': redacted, 'redacted_observations': observations,
            'irreversible': True, 'applied': True,
            'note': '正文与对方显示名已抹除（含原始消息观察结果）；任务骨架与提交凭据保留。'}
