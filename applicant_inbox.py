"""Validate visible previews without pretending they are complete addressable messages."""
import re
from sandbox import KernelError


def checked(value, expected_name):
    valid=False
    if isinstance(value,dict) and set(value)=={'status','display_name','rows','automation_ready','list_truncated'}:
        valid=(value['status'] in {'identity_unavailable','restricted','auth_required','session_changed','schema_changed','empty_or_loading','observed_previews'}
               and isinstance(value['display_name'],str) and len(value['display_name'])<=40
               and isinstance(value['rows'],list) and len(value['rows'])<=30 and value['automation_ready'] is False
               and type(value['list_truncated']) is bool
               and ((value['status']=='observed_previews')==bool(value['rows'])))
    if not valid:
        raise KernelError('BROWSER_SCHEMA_CHANGED','会话预览结果无法完整核对。')
    if value['status']=='observed_previews' and value['display_name']!=expected_name:
        raise KernelError('BROWSER_BINDING_MISMATCH','会话页显示账号与已确认的专用会话不一致。')
    seen=set()
    for row in value['rows']:
        valid=(isinstance(row,dict) and set(row)=={'display_name','party_summary','last_message_preview','preview_truncated','unread_marker',
               'platform_conversation_id','identity_source','full_message_available','message_id_available'}
               and all(isinstance(row.get(k),str) and len(row[k])<=limit for k,limit in [('display_name',80),('party_summary',200),('last_message_preview',500)])
               and type(row.get('preview_truncated')) is bool and type(row.get('unread_marker')) is bool
               and row.get('full_message_available') is False and row.get('message_id_available') is False)
        if not valid:
            raise KernelError('BROWSER_SCHEMA_CHANGED','会话预览字段无法核对。')
        identity=row['platform_conversation_id']
        if identity is None:
            valid=row['identity_source']=='unavailable'
        else:
            valid=(isinstance(identity,str) and re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}',identity)
                   and identity not in seen and row['identity_source']=='dom_data_conversation_id')
            seen.add(identity if isinstance(identity,str) else '')
        if not valid:
            raise KernelError('BROWSER_SCHEMA_CHANGED','会话标识无法核对，未使用姓名或序号替代。')
    return value
