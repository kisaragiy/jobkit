"""单次受限动作：渠道授权、原子占位与六态结果协议（纯逻辑）。

本模块不含浏览器调用，也不读写平台。它只回答三个问题：
1. 这次动作有没有可用授权（账号/角色/动作类型/目标/资料版本/有效期/数量）；
2. 授权能不能被消费（只能用一次，不能续期，不能换目标）；
3. 一次执行之后，结果落在哪一态（未执行/已填写未提交/已提交待核对/新回显/结果未知/冲突）。

设计底线：
- 数量上限硬编码为 1，不存在"批量授权"这条路；要第二次就必须重新登记并重新核对目标。
- 授权自带版本与目标，动作发生前必须与当前事实一致；不一致就是拒绝，不是"提醒"。
- outcome_unknown 不可重试，必须保守占额度：未知不等于没发生。
- 新回显（observed)不等于平台送达，送达也不等于对方已读：三件事分开表达，不合并成一个成功。
"""
import hashlib
import time

AUTHORIZATION_KIND = 'channel_authorization'
MAX_USES = 1
AUTHORIZATION_TTL_SECONDS = 900
ALLOWED_ACTIONS = ('greeting.submit',)

# 一次动作可能落到六态之一；顺序即严重度（后者更不确定）
OUTCOME_STATES = (
    'not_executed',          # 明确没有发生
    'filled_not_submitted',  # 已填入但未提交，保守处理为可能已占用额度
    'submitted_unverified',  # 已提交，尚无回显证据
    'observed_reply',        # 观察到新回显（不等于送达，更不等于已读）
    'outcome_unknown',       # 无法确定是否发生，禁止自动重试
    'conflict',              # 证据自相矛盾，交人工
)
UNSAFE_STATES = ('outcome_unknown', 'conflict')
BUDGET_CONSUMING_STATES = ('filled_not_submitted', 'submitted_unverified', 'observed_reply',
                           'outcome_unknown', 'conflict')


def _now(now=None):
    return time.time() if now is None else now


def target_key(*, job_id, company, conversation_key=None):
    """目标指纹：岗位号 + 公司 + 会话键；缺一个就不算可核对的目标。"""
    parts = [job_id, company, conversation_key or '']
    if not all(isinstance(p, str) and p.strip() for p in parts[:2]):
        raise ValueError('目标必须包含岗位与公司')
    return hashlib.sha256('\x00'.join(p.strip() for p in parts).encode('utf-8')).hexdigest()


def authorization(*, scope_id, action, account_evidence, target, profile_revision,
                  plan_revision, ttl_seconds=AUTHORIZATION_TTL_SECONDS, now=None):
    """登记一份只能用一次的渠道授权。"""
    if action not in ALLOWED_ACTIONS:
        raise ValueError('INVALID_ACTION')
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError('INVALID_SCOPE')
    if not isinstance(account_evidence, str) or not account_evidence.strip():
        raise ValueError('ACCOUNT_EVIDENCE_REQUIRED')
    ttl = int(ttl_seconds)
    if ttl <= 0 or ttl > AUTHORIZATION_TTL_SECONDS:
        raise ValueError('INVALID_TTL')
    created = _now(now)
    return {
        'kind': AUTHORIZATION_KIND,
        'scope_id': scope_id,
        'action': action,
        'account_evidence': account_evidence.strip(),
        'target': target,
        'profile_revision': int(profile_revision),
        'plan_revision': int(plan_revision),
        'created_at': created,
        'expires_at': created + ttl,
        'max_uses': MAX_USES,
        'used': 0,
        'status': 'ready',
        'sending_enabled': False,
    }


def check(doc, *, action, target, profile_revision, plan_revision, now=None):
    """返回错误码字符串；可用则返回 None。"""
    if not isinstance(doc, dict) or doc.get('kind') != AUTHORIZATION_KIND:
        return 'CHANNEL_NOT_AUTHORIZED'
    if doc.get('scope_id') == '' or not isinstance(doc.get('scope_id'), str):
        return 'CHANNEL_NOT_AUTHORIZED'
    if doc.get('action') != action:
        return 'CHANNEL_NOT_AUTHORIZED'
    if doc.get('status') == 'exhausted' or int(doc.get('used', 0)) >= int(doc.get('max_uses', 0)):
        return 'CHANNEL_AUTHORIZATION_EXHAUSTED'
    if doc.get('status') != 'ready':
        return 'CHANNEL_NOT_AUTHORIZED'
    if _now(now) >= float(doc.get('expires_at', 0)):
        return 'CHANNEL_AUTHORIZATION_EXPIRED'
    if doc.get('target') != target:
        return 'CHANNEL_TARGET_CHANGED'
    if int(doc.get('profile_revision', -1)) != int(profile_revision):
        return 'CHANNEL_PROFILE_CHANGED'
    if int(doc.get('plan_revision', -1)) != int(plan_revision):
        return 'CHANNEL_PLAN_CHANGED'
    return None


def consume(doc, *, now=None):
    """消费一次授权；用完即 exhausted，不可续期。"""
    used = int(doc.get('used', 0)) + 1
    updated = dict(doc)
    updated['used'] = used
    updated['status'] = 'exhausted' if used >= int(doc.get('max_uses', MAX_USES)) else 'ready'
    updated['consumed_at'] = _now(now)
    return updated


def outcome(*, attempted, filled, submitted, observed_change, expected_text=None,
            observed_text=None):
    """把执行过程的四个观测折叠成六态之一。

    attempted   是否进入了动作步骤
    filled      输入框是否被写入
    submitted   提交动作是否发出（不等于平台收到）
    observed    观察窗口内是否出现新回显
    """
    if observed_change and submitted:
        if expected_text is not None and observed_text is not None and expected_text != observed_text:
            return _pack('conflict', 'observed_text_mismatch')
        return _pack('observed_reply', 'new_echo_observed')
    if observed_change and not submitted:
        return _pack('conflict', 'echo_without_submit')
    if submitted:
        return _pack('submitted_unverified', 'no_echo_in_window')
    if filled:
        return _pack('filled_not_submitted', 'composer_written_only')
    if attempted:
        return _pack('not_executed', 'no_write_detected')
    return _pack('not_executed', 'never_started')


def _pack(state, reason):
    return {
        'state': state,
        'reason': reason,
        'retryable': state == 'not_executed',
        'consumes_budget': state in BUDGET_CONSUMING_STATES,
        'needs_human': state in UNSAFE_STATES,
        'delivery_confirmed': False,
        'read_by_recipient': False,
        'sending_enabled': False,
    }


def public(doc):
    """对外只暴露授权轮廓，不外泄账号证据原文。"""
    if not isinstance(doc, dict):
        return None
    evidence = doc.get('account_evidence') or ''
    return {
        'action': doc.get('action'),
        'status': doc.get('status'),
        'used': int(doc.get('used', 0)),
        'max_uses': int(doc.get('max_uses', 0)),
        'expires_at': doc.get('expires_at'),
        'profile_revision': doc.get('profile_revision'),
        'plan_revision': doc.get('plan_revision'),
        'account_evidence_fingerprint': hashlib.sha256(evidence.encode('utf-8')).hexdigest()[:16],
        'sending_enabled': False,
        'delivery_confirmed': False,
    }
