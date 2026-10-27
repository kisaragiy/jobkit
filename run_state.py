"""运行开关：暂停「自动回复」，不影响打招呼，也不碰人的手工操作。

设计边界（用户 2026-10-27 定）：

- **只停自动回复**：暂停集默认只有 `reply`；招呼（`greeting`）照发。
- **只拦外部写入**：把草稿填进输入框（`local_ui`）是人的接管动作，暂停期间照样允许。
- **跨工作区**：暂停看全库，换账号不能绕过（与发送台账同一口径）。

读不到或读坏了按「已暂停自动回复」处理（fail-closed）：这是刹车，宁可多停一次，
也不能因为库瞬时读不到就当成没暂停继续外发。这一点与台账 blocker 的 fail-open 相反，
是刻意的不对称。
"""
import time

KIND = 'run_state'

STATE_RUNNING = 'running'
STATE_PAUSED = 'paused'
STATES = (STATE_RUNNING, STATE_PAUSED)

DEFAULT_ACTIONS = ('reply',)
PAUSABLE_ACTIONS = ('reply', 'greeting')

CODES = {
    'RUN_PAUSED': '当前已暂停自动回复，先恢复再继续；暂停不影响核对发送结果。',
    'RUN_STATE_INVALID': '运行开关记录无法核对，已按暂停处理。',
    'RUN_STATE_UNREADABLE': '读不到运行开关状态，已按暂停处理，请稍后重试。',
    'RUN_ACTION_INVALID': '暂停范围只能是 reply 或 greeting。',
    'RUN_PAUSED_BY_REQUIRED': '暂停/恢复必须留下操作人。',
    'RUN_STALE_GENERATION': '这条记录已被别的操作改过，请重新读取再操作。',
}


def blank():
    """未暂停的默认记录；revision 0 天然表示「从没动过」。"""
    return {'state': STATE_RUNNING, 'actions': [], 'paused_by': None,
            'paused_at': None, 'reason': None, 'generation': 0}


def normalize_actions(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError('RUN_ACTION_INVALID')
    out = []
    for item in value:
        if item not in PAUSABLE_ACTIONS:
            raise ValueError('RUN_ACTION_INVALID')
        if item not in out:
            out.append(item)
    if not out:
        raise ValueError('RUN_ACTION_INVALID')
    return out


def checked(document, scope_id):
    """严格校验磁盘上的记录；任何不合规都抛 RUN_STATE_INVALID（调用方按暂停处理）。"""
    if not isinstance(document, dict):
        raise ValueError('RUN_STATE_INVALID')
    state = document.get('state')
    if state not in STATES:
        raise ValueError('RUN_STATE_INVALID')
    actions = document.get('actions')
    if not isinstance(actions, list) or any(item not in PAUSABLE_ACTIONS for item in actions):
        raise ValueError('RUN_STATE_INVALID')
    if state == STATE_RUNNING:
        if actions:
            raise ValueError('RUN_STATE_INVALID')
    else:
        if not actions:
            raise ValueError('RUN_STATE_INVALID')
        if not isinstance(document.get('paused_by'), str) or not document['paused_by'].strip():
            raise ValueError('RUN_STATE_INVALID')
        at = document.get('paused_at')
        if not isinstance(at, (int, float)) or isinstance(at, bool) or at <= 0:
            raise ValueError('RUN_STATE_INVALID')
    generation = document.get('generation')
    if type(generation) is not int or generation < 0:
        raise ValueError('RUN_STATE_INVALID')
    return document


def paused_actions(*documents):
    """把多份记录（跨工作区）合起来：任一工作区说某动作暂停，就算暂停。

    任何一份读不懂都按「自动回复已暂停」处理（fail-closed）。
    """
    out = []
    for document in documents:
        try:
            checked(document, None)
        except ValueError:
            # 读坏了按最保守处理：自动回复算暂停
            if 'reply' not in out:
                out.append('reply')
            continue
        for action in document.get('actions') or ():
            if action not in out:
                out.append(action)
    return out


def is_paused(document, action):
    return action in paused_actions(document)


def pause(document, *, by, reason=None, actions=DEFAULT_ACTIONS, ts=None):
    """返回新记录（纯函数，不写库）。generation 每次变动 +1，用作 CAS 与陈旧判定。"""
    if not isinstance(by, str) or not by.strip():
        raise ValueError('RUN_PAUSED_BY_REQUIRED')
    merged = list(actions or DEFAULT_ACTIONS)
    current = []
    if isinstance(document, dict):
        try:
            current = list(checked(document, None).get('actions') or [])
        except ValueError:
            current = []
    for action in current:
        if action not in merged:
            merged.append(action)
    stamp = time.time() if ts is None else float(ts)
    return {'state': STATE_PAUSED, 'actions': normalize_actions(merged),
            'paused_by': by.strip(), 'paused_at': stamp,
            'reason': reason.strip() if isinstance(reason, str) and reason.strip() else None,
            'generation': (document.get('generation', 0) if isinstance(document, dict) else 0) + 1}


def resume(document, *, by, action=None, ts=None, expected_generation=None):
    """恢复：action 为空表示全部恢复；否则只放开这一种。"""
    if not isinstance(by, str) or not by.strip():
        raise ValueError('RUN_PAUSED_BY_REQUIRED')
    base = blank()
    if isinstance(document, dict):
        try:
            base = dict(checked(document, None))
        except ValueError:
            base = dict(blank())
    if expected_generation is not None and base.get('generation') != expected_generation:
        raise ValueError('RUN_STALE_GENERATION')
    remaining = list(base.get('actions') or [])
    if action is None:
        remaining = []
    else:
        if action not in PAUSABLE_ACTIONS:
            raise ValueError('RUN_ACTION_INVALID')
        remaining = [item for item in remaining if item != action]
    stamp = time.time() if ts is None else float(ts)
    return {'state': STATE_PAUSED if remaining else STATE_RUNNING,
            'actions': remaining,
            'paused_by': by.strip() if remaining else None,
            'paused_at': (base.get('paused_at') or stamp) if remaining else None,
            'reason': base.get('reason') if remaining else None,
            'generation': base.get('generation', 0) + 1}
