"""Bounded job-search session logic; no browser, network or storage.

One session = an ordered list of (city, keyword) combinations taken from the
saved plan, walked page by page. Everything is bounded: job count, pages per
combination, total runtime and idle pages. Failures are never folded into
"no jobs": every page lands in an explicit status and failed pages are counted
separately from empty ones.
"""

from sandbox import KernelError

MAX_JOBS = 60              # first acceptance batch; 150 is a daily greeting cap, not a crawl target
MAX_PAGES_PER_COMBO = 3
MAX_COMBOS = 4
MAX_SECONDS = 600
MAX_IDLE_PAGES = 2         # consecutive pages with zero new job ids end the combination
JOBS_PER_PAGE = 50

OBSERVED = 'observed'
EMPTY = 'empty'
RESTRICTED = 'restricted'
AUTH_REQUIRED = 'auth_required'
UNAVAILABLE = 'unavailable'
SCHEMA_CHANGED = 'schema_changed'

STOP_REASONS = frozenset({
    'COMBOS_DONE', 'LIMIT_JOBS', 'LIMIT_SECONDS', 'ACCOUNT_RISK',
    'AUTH_REQUIRED', 'BROWSER_UNAVAILABLE', 'USER_STOPPED',
})
JOB_FIELDS = ('job_id', 'title', 'company', 'city', 'salary', 'education', 'experience')
JOB_LIMITS = {'job_id': 128, 'title': 200, 'company': 160, 'city': 40,
              'salary': 80, 'education': 40, 'experience': 40}
# 方案第 119 行：每条岗位带来源与观察时间；旧会话缺这两个字段时按 unknown 补齐，不报错。
JOB_EVIDENCE = {'jd': 4000, 'source': 300, 'observed_at': 64}


def evidence_defaults(stored):
    """Fill evidence fields for records written before this contract existed."""
    stored.setdefault('jd', '')
    if not isinstance(stored.get('source'), str) or not stored['source'].strip():
        stored['source'] = 'unknown'
    stored.setdefault('observed_at', None)
    return stored


def _job_ok(job):
    return (isinstance(job, dict) and set(JOB_FIELDS) <= set(job)
            and not set(job) - set(JOB_FIELDS) - set(JOB_EVIDENCE)
            and all(isinstance(job[key], str) and len(job[key]) <= width
                    for key, width in JOB_LIMITS.items())
            and all(isinstance(job[key], str) and len(job[key]) <= width
                    for key, width in JOB_EVIDENCE.items() if key in job and key != 'observed_at')
            and (job.get('observed_at') is None or isinstance(job['observed_at'], (int, float))))


def _invalid(detail):
    raise KernelError('INVALID_JOB_SEARCH', detail, 400)


def combos(plan):
    """Ordered city×keyword pairs: primary cities first, keywords in plan order."""
    cities = list(plan['primary_cities']) + list(plan['secondary_cities'])
    pairs = []
    for city in cities:
        for keyword in plan['keywords']:
            pairs.append([city.strip(), keyword.strip()])
            if len(pairs) == MAX_COMBOS:
                return pairs
    return pairs


def new(plan, plan_revision, now):
    if type(plan_revision) is not int or plan_revision < 1:
        _invalid('请先确认保存求职方案。')
    pairs = combos(plan)
    return {
        'plan_revision': plan_revision,
        'combos': [{'city': city, 'keyword': keyword, 'pages': 0, 'new_jobs': 0,
                    'idle': 0, 'status': 'pending'} for city, keyword in pairs],
        'cursor': 0,
        'jobs': {},
        'observed_at': {},
        'status': 'running',
        'stop_reason': None,
        'pages_read': 0,
        'failed_pages': 0,
        'navigated': None,
        'started_at': now,
        'updated_at': now,
        '_order': [],
    }


def pending_details(state, limit=10):
    """会话里还没有 JD 的岗位（按发现顺序），供「逐条读详情」编排使用。

    只读，不改变状态。搜索已结束时仍可返回剩余缺口——补读详情不会让旧候选复活。
    """
    missing = []
    for job_id in state['_order']:
        job = state['jobs'].get(job_id)
        if job is None or job.get('jd'):
            continue
        missing.append(job_id)
        if len(missing) >= limit:
            break
    return missing


def pending_count(state):
    """还有多少条岗位缺 JD（只读计数，供推进提示用；不触发任何读取）。"""
    return len(pending_details(state, limit=max(len(state['jobs']), 1)))


def enrich_jd(state, job_id, jd, now, *, truncated=False):
    """把详情页读到的职责描述写回会话里的对应岗位。纯逻辑，无 IO。

    - 岗位不在会话里 → 拒绝（不能凭 job_id 往任意岗位写证据）
    - 正文过短/超长/非字符串 → 拒绝（截断由读取层负责，这里只做边界校验）
    - 已有相同正文 → unchanged（幂等，不刷新观察时间）
    - 已有不同正文 → updated，同时刷新该岗位的观察时间（岗位内容变了是事实，不掩盖）
    """
    if not isinstance(job_id, str) or not job_id:
        _invalid('岗位标识不合法，无法写入职责描述。')
    job = state['jobs'].get(job_id)
    if job is None:
        raise KernelError('OBSERVED_JOB_NOT_FOUND', '该岗位不在本次读取记录中。', 404)
    limit = JOB_EVIDENCE['jd']
    if not isinstance(jd, str) or not jd.strip() or len(jd) > limit:
        _invalid('职责描述正文为空或超出长度，未写入。')
    previous = job.get('jd') or ''
    if previous == jd:
        return {'status': 'unchanged', 'job_id': job_id, 'jd_chars': len(jd),
                'truncated': bool(truncated)}
    job['jd'] = jd
    state['observed_at'][job_id] = now
    job['observed_at'] = now
    state['updated_at'] = now
    return {'status': 'updated' if previous else 'stored', 'job_id': job_id,
            'jd_chars': len(jd), 'truncated': bool(truncated),
            'replaced': bool(previous)}


def mark_navigated(state, now):
    """Record that the browser was navigated to the current combination's search page."""
    combo = current(state)
    if combo is None:
        _invalid('本次搜索已结束，请重新开始。')
    state['navigated'] = state['cursor']
    state['updated_at'] = now
    return {'city': combo['city'], 'keyword': combo['keyword'], 'index': state['cursor']}


def needs_navigation(state):
    return state['status'] == 'running' and state['navigated'] != state['cursor']


def current(state):
    """The combination to read next, or None when the session cannot continue."""
    if state['status'] != 'running':
        return None
    while state['cursor'] < len(state['combos']):
        combo = state['combos'][state['cursor']]
        if combo['status'] == 'pending':
            return combo
        state['cursor'] += 1
    state['status'] = 'finished'
    state['stop_reason'] = 'COMBOS_DONE'
    return None


def _finish_combo(state, status):
    state['combos'][state['cursor']]['status'] = status
    state['cursor'] += 1


def _settle(state):
    """Every finished combination advances the cursor; the last one ends the session."""
    if state['status'] == 'running' and state['cursor'] >= len(state['combos']):
        state['status'] = 'finished'
        state['stop_reason'] = 'COMBOS_DONE'


def _stop(state, reason):
    state['status'] = 'stopped' if reason in {'ACCOUNT_RISK', 'AUTH_REQUIRED',
                                              'BROWSER_UNAVAILABLE', 'USER_STOPPED'} else 'finished'
    state['stop_reason'] = reason


def _merge(state, jobs, now, combo=None):
    added = 0
    source = ('boss_search:%s/%s' % (combo['city'], combo['keyword'])) if combo else 'unknown'
    for job in jobs:
        if len(state['jobs']) >= MAX_JOBS:
            break
        job_id = job['job_id']
        if job_id in state['jobs']:
            continue
        stored = {key: job[key] for key in JOB_FIELDS}
        stored['jd'] = job.get('jd') if isinstance(job.get('jd'), str) else ''
        # 渠道标识只写实际搜索范围，不拼 URL；详情未读时 jd 留空，交给匹配层标 jd_missing。
        stored['source'] = job['source'] if isinstance(job.get('source'), str) and job.get('source', '').strip() else source
        stored['observed_at'] = now
        state['jobs'][job_id] = stored
        state['observed_at'][job_id] = now
        state['_order'].append(job_id)
        added += 1
    return added


def apply_page(state, observation, now):
    """Fold one page observation into the session; returns what happened.

    Structure errors raise;页面上的失败状态只改变会话状态，不伪装成"没有岗位"。
    """
    if state['status'] != 'running':
        _invalid('本次搜索已结束，请重新开始。')
    combo = current(state)
    if combo is None:
        return {'status': 'finished', 'stop_reason': state['stop_reason'], 'added': 0}
    if not isinstance(observation, dict) or observation.get('status') not in {
            OBSERVED, EMPTY, RESTRICTED, AUTH_REQUIRED, UNAVAILABLE, SCHEMA_CHANGED}:
        raise KernelError('BROWSER_SCHEMA_CHANGED', '岗位列表读取结果无法核对。')
    status = observation['status']
    state['pages_read'] += 1
    state['updated_at'] = now
    if status in {RESTRICTED, AUTH_REQUIRED, UNAVAILABLE}:
        state['failed_pages'] += 1
        _stop(state, {'restricted': 'ACCOUNT_RISK', 'auth_required': 'AUTH_REQUIRED',
                      'unavailable': 'BROWSER_UNAVAILABLE'}[status])
        return {'status': status, 'stop_reason': state['stop_reason'], 'added': 0,
                'note': '本次读取失败，未按"没有岗位"处理。'}
    if status in {SCHEMA_CHANGED}:
        state['failed_pages'] += 1
        combo['pages'] += 1
        _finish_combo(state, 'schema_changed')
        _settle(state)
        return {'status': status, 'added': 0, 'note': '页面结构变化，该组合停止；不是没有岗位。'}
    if status == EMPTY:
        combo['pages'] += 1
        _finish_combo(state, 'empty')
        _settle(state)
        return {'status': EMPTY, 'added': 0}
    jobs = observation.get('jobs')
    if not isinstance(jobs, list) or len(jobs) > JOBS_PER_PAGE:
        raise KernelError('INVALID_JOB_FEED', '岗位记录数量或结构无法核对。')
    for job in jobs:
        if not _job_ok(job):
            raise KernelError('INVALID_JOB_FEED', '岗位字段不完整，未生成候选。')
    added = _merge(state, jobs, now, combo)
    combo['pages'] += 1
    combo['new_jobs'] += added
    combo['idle'] = 0 if added else combo['idle'] + 1
    if len(state['jobs']) >= MAX_JOBS:
        _stop(state, 'LIMIT_JOBS')
    elif now - state['started_at'] > MAX_SECONDS:
        _stop(state, 'LIMIT_SECONDS')
    elif combo['idle'] >= MAX_IDLE_PAGES:
        _finish_combo(state, 'duplicate_pages')
    elif combo['pages'] >= MAX_PAGES_PER_COMBO:
        _finish_combo(state, 'done')
    _settle(state)
    return {'status': OBSERVED, 'added': added, 'page_jobs': len(jobs),
            'total_jobs': len(state['jobs']), 'combo': {'city': combo['city'],
                                                       'keyword': combo['keyword'],
                                                       'pages': combo['pages']}}


def public(state):
    """Session view for the workspace: no per-job payload, no internals."""
    return {
        'status': state['status'],
        'stop_reason': state['stop_reason'],
        'plan_revision': state['plan_revision'],
        'combos': [{'city': item['city'], 'keyword': item['keyword'], 'pages': item['pages'],
                    'new_jobs': item['new_jobs'], 'status': item['status']}
                   for item in state['combos']],
        'counts': {'jobs': len(state['jobs']), 'pages_read': state['pages_read'],
                   'failed_pages': state['failed_pages']},
        'limits': {'max_jobs': MAX_JOBS, 'max_pages_per_combo': MAX_PAGES_PER_COMBO,
                   'max_combos': MAX_COMBOS, 'max_seconds': MAX_SECONDS,
                   'max_idle_pages': MAX_IDLE_PAGES, 'jobs_per_page': JOBS_PER_PAGE},
        'navigation_required': needs_navigation(state),
        'next': (None if state['status'] != 'running' or state['cursor'] >= len(state['combos'])
                 else {'city': state['combos'][state['cursor']]['city'],
                       'keyword': state['combos'][state['cursor']]['keyword']}),
        'started_at': state['started_at'],
        'updated_at': state['updated_at'],
        'observed_at': dict(state['observed_at']),
        'automatic_execution': False,
        'sending_enabled': False,
        'note': '采集结果不等于已打招呼；每次进入候选仍需人工核对。',
    }


def job_records(state):
    """Collected jobs in first-seen order, ready for applicant_jobs.preview."""
    return [dict(state['jobs'][job_id]) for job_id in state['_order']]


def stop(state, reason, now):
    if reason != 'USER_STOPPED':
        _invalid('未知的停止原因。')
    if state['status'] == 'running':
        state['status'] = 'stopped'
        state['stop_reason'] = 'USER_STOPPED'
    state['updated_at'] = now
    return public(state)


def validate(state):
    """Reject a stored session that no longer has the expected shape."""
    required = {'plan_revision', 'combos', 'cursor', 'jobs', 'observed_at', 'status',
                'stop_reason', 'pages_read', 'failed_pages', 'navigated',
                'started_at', 'updated_at', '_order'}
    if (not isinstance(state, dict) or set(state) != required
            or state['status'] not in {'running', 'finished', 'stopped'}
            or (state['stop_reason'] is not None and state['stop_reason'] not in STOP_REASONS)
            or (state['status'] == 'running' and state['stop_reason'] is not None)
            or not isinstance(state['combos'], list) or not 1 <= len(state['combos']) <= MAX_COMBOS
            or any(not isinstance(item, dict) or set(item) != {'city', 'keyword', 'pages',
                                                              'new_jobs', 'idle', 'status'}
                   or item['status'] not in {'pending', 'done', 'empty', 'duplicate_pages', 'schema_changed'}
                   or any(type(item[key]) is not int or item[key] < 0 for key in ('pages', 'new_jobs', 'idle'))
                   for item in state['combos'])
            or type(state['cursor']) is not int or not 0 <= state['cursor'] <= len(state['combos'])
            or (state['navigated'] is not None
                and (type(state['navigated']) is not int or not 0 <= state['navigated'] < len(state['combos'])))
            or not isinstance(state['jobs'], dict) or len(state['jobs']) > MAX_JOBS
            or not isinstance(state['observed_at'], dict) or set(state['observed_at']) != set(state['jobs'])
            or not isinstance(state['_order'], list) or len(state['_order']) != len(state['jobs'])
            or set(state['_order']) != set(state['jobs'])
            or any(type(state[key]) is not int for key in ('pages_read', 'failed_pages', 'plan_revision'))):
        raise KernelError('INVALID_JOB_SEARCH_STATE', '搜索记录无法核对，请重新开始。')
    for job_id, job in state['jobs'].items():
        # 旧会话的岗位记录没有来源/观察时间：按 unknown 补齐，而不是判定记录损坏。
        if isinstance(job, dict) and set(JOB_FIELDS) <= set(job):
            evidence_defaults(job)
        if not _job_ok(job) or job['job_id'] != job_id:
            raise KernelError('INVALID_JOB_SEARCH_STATE', '搜索记录中的岗位无法核对。')
    return state
