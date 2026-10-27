"""G4 纯逻辑层：招呼配额、去重与跨日核对。

本模块不连浏览器、不产生任何外发内容。它只回答：**这一批候选里，哪些可以进入执行协议**，
以及为什么其余的不能。

底线（都是可证伪的不变量）：
- 额度按**账号**计，不按 scope / workspace 计 —— 换 workspace 不能刷新额度。
- 日界线固定 UTC+8（Asia/Shanghai，无夏令时），不跟服务器时区漂移。
- 保守占额：填写未提交、已提交待核对、观察到回显、结果未知、证据冲突都**计入**用掉的额度；
  只有明确没发生的 `not_executed` 不占。
- 已联系是**永久证据**：只要存在 `observed_reply`，跨多少天都跳过。
- 未核对的尝试当天阻断重试，且**不会因为跨天就自动放行**重新联系同一岗位（要人工核对）。
- 平台明确显示更严格的限制时取两者较小值；额度用完就正常停止，不换入口继续。
"""
import time

DAILY_LIMIT = 150
TIMEZONE_OFFSET_HOURS = 8

# 执行前先占位：并发下两个进程各自读到 used=N 会双双放行，所以占用要写进同一事务。
RESERVED_STATE = 'reserved'
# 计入当日额度的结果（保守：未知也算用掉；占位也算，进程崩了就当用掉）
BUDGET_STATES = ('filled_not_submitted', 'submitted_unverified', 'observed_reply',
                 'outcome_unknown', 'conflict', RESERVED_STATE)
# 有"已经联系过"的证据
CONTACTED_STATES = ('observed_reply',)
# 结果尚未核对清楚
UNRESOLVED_STATES = ('filled_not_submitted', 'submitted_unverified', 'outcome_unknown', 'conflict')

REASONS = ('PROCEED', 'ALREADY_CONTACTED', 'UNRESOLVED_ATTEMPT', 'JOB_CLOSED',
           'PAGE_RESTRICTED', 'DAILY_LIMIT', 'DUPLICATE_IN_BATCH', 'PLAN_CHANGED')

MESSAGES = {
    'PROCEED': '可以进入单次执行协议。',
    'ALREADY_CONTACTED': '这个岗位已经有联系记录，不再重复招呼。',
    'UNRESOLVED_ATTEMPT': '这个岗位上次招呼结果还没核对清楚，先人工核对再决定。',
    'JOB_CLOSED': '岗位已关闭或已下线，跳过。',
    'PAGE_RESTRICTED': '页面显示限制，本轮停止，交给人工处理。',
    'DAILY_LIMIT': '今天的招呼额度已用完，正常停止。',
    'DUPLICATE_IN_BATCH': '这一批里同一个岗位出现了多次，只保留第一次。',
    'PLAN_CHANGED': '求职方案或本人资料已变化，这一条需要重新核对。',
}


def day_key(ts=None):
    """Asia/Shanghai 的日期键（固定 +8，不依赖系统时区）。"""
    ts = time.time() if ts is None else float(ts)
    return time.strftime('%Y-%m-%d', time.gmtime(ts + TIMEZONE_OFFSET_HOURS * 3600))


def attempt(*, account_id, job_id, company, state, ts=None):
    """一条尝试记录。state 必须是六态之一，否则拒绝——避免把新状态偷偷塞进配额核算。"""
    from applicant_action import OUTCOME_STATES
    # reserved 是执行前的占位（内部状态），不属于六态结果，但占额度；其余必须严格是六态。
    if state not in tuple(OUTCOME_STATES) + (RESERVED_STATE,):
        raise ValueError('INVALID_OUTCOME_STATE')
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError('ACCOUNT_REQUIRED')
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError('JOB_REQUIRED')
    ts = time.time() if ts is None else float(ts)
    return {'account_id': account_id.strip(), 'job_id': job_id.strip(),
            'company': (company or '').strip(), 'state': state,
            'ts': ts, 'day': day_key(ts)}


def usage(records, *, day, account_id=None):
    """当日用掉多少额度（保守口径）。

    `account_id=None` 表示跨账号汇总：额度按人计，换工作区不能刷新上限
    （方案验收矩阵「已用/未知不退占；跨日/跨 scope 不绕过」）。
    """
    used = 0
    unknown = 0
    for record in records or ():
        if record.get('day') != day:
            continue
        if account_id is not None and record.get('account_id') != account_id:
            continue
        if record.get('state') in BUDGET_STATES:
            used += 1
            if record.get('state') in UNRESOLVED_STATES:
                unknown += 1
    return {'used': used, 'unresolved': unknown}


def effective_limit(configured=DAILY_LIMIT, platform_limit=None, platform_expires_at=None,
                    now=None):
    """用户配置与平台限制取较小值；平台限制过期后自动失效（当天填的值不能明天还压着）。"""
    values = []
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        values.append(configured)
    fresh = True
    if platform_expires_at is not None:
        stamp = now if now is not None else time.time()
        try:
            fresh = float(platform_expires_at) > float(stamp)
        except (TypeError, ValueError):
            fresh = False
    if fresh and isinstance(platform_limit, int) and not isinstance(platform_limit, bool) \
            and platform_limit > 0:
        values.append(platform_limit)
    return min(values) if values else 0


def decide(candidate, *, records, day, account_id, configured=DAILY_LIMIT,
           platform_limit=None, used=None, taken=None, profile_revision=None,
           plan_revision=None):
    """对一条候选给出决定。taken 是本批已经被选中的 job_id 集合（会被就地更新）。"""
    job_id = candidate.get('job_id')
    if not isinstance(job_id, str) or not job_id.strip():
        return {'job_id': job_id, 'reason': 'JOB_CLOSED',
                'message': '候选缺少岗位标识，无法核对。'}
    job_id = job_id.strip()
    taken = taken if taken is not None else set()

    if candidate.get('page_restricted') is True:
        return {'job_id': job_id, 'reason': 'PAGE_RESTRICTED', 'message': MESSAGES['PAGE_RESTRICTED']}
    if candidate.get('job_open') is False:
        return {'job_id': job_id, 'reason': 'JOB_CLOSED', 'message': MESSAGES['JOB_CLOSED']}
    if profile_revision is not None and candidate.get('profile_revision') not in (None, profile_revision):
        return {'job_id': job_id, 'reason': 'PLAN_CHANGED', 'message': MESSAGES['PLAN_CHANGED']}
    if plan_revision is not None and candidate.get('plan_revision') not in (None, plan_revision):
        return {'job_id': job_id, 'reason': 'PLAN_CHANGED', 'message': MESSAGES['PLAN_CHANGED']}

    mine = [r for r in (records or ()) if r.get('account_id') == account_id
            and r.get('job_id') == job_id]
    if any(r.get('state') in CONTACTED_STATES for r in mine):
        return {'job_id': job_id, 'reason': 'ALREADY_CONTACTED', 'message': MESSAGES['ALREADY_CONTACTED']}
    if any(r.get('state') in UNRESOLVED_STATES for r in mine):
        return {'job_id': job_id, 'reason': 'UNRESOLVED_ATTEMPT', 'message': MESSAGES['UNRESOLVED_ATTEMPT']}
    if any(r.get('day') == day and r.get('state') in BUDGET_STATES for r in mine):
        return {'job_id': job_id, 'reason': 'UNRESOLVED_ATTEMPT', 'message': MESSAGES['UNRESOLVED_ATTEMPT']}
    if job_id in taken:
        return {'job_id': job_id, 'reason': 'DUPLICATE_IN_BATCH', 'message': MESSAGES['DUPLICATE_IN_BATCH']}

    limit = effective_limit(configured, platform_limit)
    used_count = used if used is not None else usage(records, day=day, account_id=account_id)['used']
    if used_count >= limit:
        return {'job_id': job_id, 'reason': 'DAILY_LIMIT', 'message': MESSAGES['DAILY_LIMIT']}

    taken.add(job_id)
    return {'job_id': job_id, 'reason': 'PROCEED', 'message': MESSAGES['PROCEED']}


def plan(candidates, *, records, day, account_id, configured=DAILY_LIMIT, platform_limit=None,
         profile_revision=None, plan_revision=None):
    """给整批候选做决定，返回可执行前缀与停止原因。"""
    limit = effective_limit(configured, platform_limit)
    counted = usage(records, day=day, account_id=account_id)
    taken = set()
    decisions = []
    remaining = max(0, limit - counted['used'])
    stop_reason = None
    for candidate in candidates or ():
        result = decide(candidate, records=records, day=day, account_id=account_id,
                        configured=configured, platform_limit=platform_limit,
                        used=limit - remaining, taken=taken,
                        profile_revision=profile_revision, plan_revision=plan_revision)
        decisions.append(result)
        if result['reason'] == 'PROCEED':
            remaining -= 1
        elif result['reason'] == 'PAGE_RESTRICTED':
            stop_reason = stop_reason or 'PAGE_RESTRICTED'
            break
    if stop_reason is None and remaining <= 0:
        stop_reason = 'DAILY_LIMIT'
    return {
        'day': day,
        'account_id': account_id,
        'limit': limit,
        'used': counted['used'],
        'unresolved': counted['unresolved'],
        'remaining': max(0, remaining),
        'proceed': [d['job_id'] for d in decisions if d['reason'] == 'PROCEED'],
        'decisions': decisions,
        'needs_review': [{'job_id': r.get('job_id'), 'company': r.get('company'),
                          'state': r.get('state'), 'day': r.get('day'), 'ts': r.get('ts')}
                         for r in (records or ())
                         if r.get('account_id') == account_id
                         and r.get('state') in UNRESOLVED_STATES][-20:],
        'stop_reason': stop_reason,
        'sending_enabled': False,
        'delivery_confirmed': False,
    }
