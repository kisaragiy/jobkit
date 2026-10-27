"""真实会话发送的严格授权（B 方案）。

与受控页那套（`applicant_action`）的区别，全部是"发错人"这个不可撤回错误的防线：

| 项 | 受控页 | 真实会话 |
| --- | --- | --- |
| 目标 | 硬编码 `controlled-job-1` | **身份快照**（名字 + 公司 + 职位）带指纹 |
| 有效期 | 900s | **120s** |
| 用完即弃 | max_uses=1 | max_uses=1 |
| 人工确认 | `account_evidence` 字符串 | **独立凭证**（`confirmation`），自带 TTL 且绑定目标指纹 |
| 执行前核对 | 读受控页 data 属性 | **重读 `chat.target` 与快照逐一比对** |

**诚实的边界**：调用方如果有终端权限，它能自己生成凭证。因此本层提供的是
"增加步骤、留下痕迹、无法批量"——不是密码学意义上的强制。真正的硬边界是
「不实现自动连发」：一次授权只对应一次点击，且每次都要重新读真实目标。
"""
import hashlib
import json
import time

AUTHORIZATION_KIND = 'conversation_authorization'
CONFIRMATION_KIND = 'conversation_confirmation'
MAX_USES = 1
AUTHORIZATION_TTL_SECONDS = 120
CONFIRMATION_TTL_SECONDS = 90
MAX_NAME = 40
MAX_COMPANY = 60
MAX_POSITION = 60
BODY_MAX = 500

CODES = {
    'CONVERSATION_NOT_AUTHORIZED': '还没有为这个对话登记发送授权。',
    'CONVERSATION_AUTHORIZATION_EXHAUSTED': '这份授权已经用过了，需要重新登记。',
    'CONVERSATION_AUTHORIZATION_EXPIRED': '授权已过期，请重新登记并确认。',
    'CONVERSATION_TARGET_CHANGED': '当前打开的对话不是授权里那个，已停止。',
    'CONVERSATION_TARGET_UNREADABLE': '读不到当前对话身份，无法确认发给谁。',
    'CONVERSATION_BODY_CHANGED': '正文与登记时不一致，请重新登记。',
    'CONVERSATION_CONFIRMATION_INVALID': '确认凭证无效或已过期。',
    'CONVERSATION_CONFIRMATION_TAKEN': '确认凭证已被使用。',
    'CONVERSATION_PLAN_CHANGED': '资料或求职方案已变化，请重新核对。',
}


def _clean(value, limit, *, required=True):
    if value is None:
        if required:
            raise ValueError('FIELD_REQUIRED')
        return None
    if not isinstance(value, str):
        raise ValueError('FIELD_TYPE')
    text = value.strip()
    if not text:
        if required:
            raise ValueError('FIELD_REQUIRED')
        return None
    if len(text) > limit:
        raise ValueError('FIELD_TOO_LONG')
    return text


def target_snapshot(*, name, company, position=None, job_id=None):
    """身份快照。名字和公司必须存在——缺一个就没法说清发给谁。"""
    snapshot = {'name': _clean(name, MAX_NAME), 'company': _clean(company, MAX_COMPANY),
                'position': _clean(position, MAX_POSITION, required=False),
                'job_id': _clean(job_id, 80, required=False)}
    return snapshot


def fingerprint(snapshot):
    """目标指纹：身份字段全部参与，任何一项变了指纹就变。"""
    parts = [snapshot.get('name') or '', snapshot.get('company') or '',
             snapshot.get('position') or '', snapshot.get('job_id') or '']
    payload = json.dumps(parts, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def body_digest(text):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= BODY_MAX:
        raise ValueError('INVALID_BODY')
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()


def confirmation_required(target, *, by, now=None):
    """登记一次人工确认凭证（独立一步，绑定目标指纹）。"""
    if not isinstance(by, str) or not by.strip():
        raise ValueError('CONFIRMATION_BY_REQUIRED')
    stamp = time.time() if now is None else float(now)
    return {'kind': CONFIRMATION_KIND,
            'target_sha256': fingerprint(target),
            'target': dict(target),
            'by': by.strip(),
            'created_at': stamp,
            'expires_at': stamp + CONFIRMATION_TTL_SECONDS,
            'used': False}


def check_confirmation(record, *, target, now=None):
    """返回 None 表示可用，否则返回错误码。"""
    if not isinstance(record, dict):
        return 'CONVERSATION_CONFIRMATION_INVALID'
    if record.get('kind') != CONFIRMATION_KIND:
        return 'CONVERSATION_CONFIRMATION_INVALID'
    stamp = time.time() if now is None else float(now)
    expires = record.get('expires_at')
    if not isinstance(expires, (int, float)) or stamp > float(expires):
        return 'CONVERSATION_CONFIRMATION_INVALID'
    if record.get('used') is True:
        return 'CONVERSATION_CONFIRMATION_TAKEN'
    if record.get('target_sha256') != fingerprint(target):
        return 'CONVERSATION_TARGET_CHANGED'
    return None


def _now(value):
    return time.time() if value is None else float(value)


def authorization(*, scope_id, target, confirmation, text, profile_revision,
                  plan_revision, now=None):
    """用一份未用过的确认凭证换一份一次性发送授权。"""
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError('SCOPE_REQUIRED')
    code = check_confirmation(confirmation, target=target, now=now)
    if code:
        raise ValueError(code)
    stamp = _now(now)
    return {'kind': AUTHORIZATION_KIND,
            'scope_id': scope_id.strip(),
            'target': dict(target),
            'target_sha256': fingerprint(target),
            'confirmation_by': confirmation.get('by'),
            'body_sha256': body_digest(text),
            'profile_revision': int(profile_revision),
            'plan_revision': int(plan_revision),
            'created_at': stamp,
            'expires_at': stamp + AUTHORIZATION_TTL_SECONDS,
            'max_uses': MAX_USES,
            'used': 0,
            'status': 'ready',
            'sending_enabled': False,
            'delivery_confirmed': False}


def check(document, *, target, text, profile_revision, plan_revision, now=None):
    """执行前核对。返回 None 表示可以发送，否则返回错误码。"""
    if not isinstance(document, dict) or document.get('kind') != AUTHORIZATION_KIND:
        return 'CONVERSATION_NOT_AUTHORIZED'
    if document.get('used', 0) >= document.get('max_uses', MAX_USES):
        return 'CONVERSATION_AUTHORIZATION_EXHAUSTED'
    stamp = _now(now)
    expires = document.get('expires_at')
    if not isinstance(expires, (int, float)) or stamp > float(expires):
        return 'CONVERSATION_AUTHORIZATION_EXPIRED'
    if document.get('profile_revision') != int(profile_revision):
        return 'CONVERSATION_PLAN_CHANGED'
    if document.get('plan_revision') != int(plan_revision):
        return 'CONVERSATION_PLAN_CHANGED'
    # 目标必须逐个字段对得上：只比哈希会让"部分字段读不到"也通过
    authorized = document.get('target') or {}
    for key in ('name', 'company', 'position', 'job_id'):
        if (authorized.get(key) or None) != (target.get(key) or None):
            return 'CONVERSATION_TARGET_CHANGED'
    try:
        digest = body_digest(text)
    except ValueError:
        return 'CONVERSATION_BODY_CHANGED'
    if digest != document.get('body_sha256'):
        return 'CONVERSATION_BODY_CHANGED'
    return None


def consume(document):
    """标记用掉一次，返回新副本（不原地改传入对象）。"""
    if not isinstance(document, dict):
        raise ValueError('INVALID_DOCUMENT')
    updated = dict(document)
    updated['used'] = int(document.get('used', 0)) + 1
    updated['status'] = 'used' if updated['used'] >= document.get('max_uses', MAX_USES) else 'ready'
    return updated


def observe_target(target, *, document):
    """把真实读到的身份与授权目标比对，返回可读结论（供人核对，不做判断以外的动作）。"""
    authorized = (document or {}).get('target') or {}
    diffs = [key for key in ('name', 'company', 'position', 'job_id')
             if (authorized.get(key) or None) != ((target or {}).get(key) or None)]
    return {'match': not diffs, 'diff_fields': diffs,
            'authorized': {key: authorized.get(key) for key in ('name', 'company', 'position')},
            'current': {key: (target or {}).get(key) for key in ('name', 'company', 'position')}}
