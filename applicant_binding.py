"""Pure validation of local browser-session confirmation, never platform authority."""
import re
from sandbox import KernelError

REQUIRED = {'scope_id', 'display_name', 'browser_session_fingerprint', 'identity_observation_task_id',
            'confirmed', 'platform_account_id_verified', 'sending_enabled'}
# 方案第 159 行：绑定要能带上岗位与招呼模板；两者可为 null（会话级绑定本来没有岗位）。
# 旧记录没有这两个字段，读取时补 None，不当作记录损坏。
OPTIONAL = {'job_id': 128, 'greeting_template_id': 64}


def _optional_ok(name, value, width):
    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    if name == 'job_id':
        return re.fullmatch(r'[A-Za-z0-9_~\-]{1,%d}' % width, value) is not None
    return len(value) <= width and re.fullmatch(r'[A-Za-z0-9_~:\-]{1,%d}' % width, value) is not None


def checked(document, scope_id):
    value = document['content']
    if (type(document['revision']) is not int or document['revision'] < 1 or not isinstance(value, dict)
            or not REQUIRED <= set(value) or set(value) - REQUIRED - set(OPTIONAL)
            or value.get('scope_id') != scope_id or value.get('confirmed') is not True
            or value.get('platform_account_id_verified') is not False or value.get('sending_enabled') is not False
            or not isinstance(value.get('display_name'), str) or not 1 <= len(value['display_name'].strip()) <= 40
            or not isinstance(value.get('browser_session_fingerprint'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['browser_session_fingerprint'])
            or not isinstance(value.get('identity_observation_task_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', value['identity_observation_task_id'])
            or any(not _optional_ok(name, value.get(name), width) for name, width in OPTIONAL.items())):
        raise KernelError('INVALID_BROWSER_BINDING', '专用浏览器确认记录无法核对，请重新检查。')
    # 归一化：旧记录缺字段读成 None（未知），新调用拿到的形状始终一致。
    return {**value, **{name: value.get(name) for name in OPTIONAL}}
