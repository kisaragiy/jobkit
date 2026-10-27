"""Pure message snapshot checks. A DOM message ID is not verified recipient authority."""
import re
from sandbox import KernelError


def checked(value):
    def invalid():raise KernelError('BROWSER_SCHEMA_CHANGED','当前会话消息无法完整核对，未用于回复。')
    if (not isinstance(value,dict) or set(value)!={'status','party','messages','messages_truncated','history_complete','recipient_verified','automation_ready'}
            or value['status'] not in {'conversation_selection_required','identity_unavailable','restricted','auth_required','session_changed','schema_changed',
                 'empty_or_loading','pane_not_ready','recipient_reference_mismatch','observed_messages'}
            or not isinstance(value['messages'],list) or len(value['messages'])>100
            or ((value['status']=='observed_messages')!=bool(value['messages']))
            or type(value['messages_truncated']) is not bool
            or any(value[k] is not False for k in ('history_complete','recipient_verified','automation_ready'))):invalid()
    party=value['party']
    if party is not None:
        if (not isinstance(party,dict) or set(party)!={'display_name','party_summary','header_display_name','platform_conversation_id','identity_source'}
                or any(not isinstance(party.get(k),str) or not 1<=len(party[k])<=limit for k,limit in [('display_name',80),('header_display_name',80),('party_summary',200)])
                or party['display_name']!=party['header_display_name']):invalid()
        identity=party['platform_conversation_id']
        if identity is None:
            if party['identity_source']!='unavailable':invalid()
        elif not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}',identity) or party['identity_source']!='dom_data_conversation_id':invalid()
    if value['messages'] and party is None:invalid()
    seen=set()
    for message in value['messages']:
        if (not isinstance(message,dict) or set(message)!={'message_id','message_id_source','direction','kind','text','text_truncated','full_text_available','timestamp_label'}
                or message['direction'] not in {'incoming','outgoing','unknown'} or message['kind'] not in {'text','media','card','unknown'}
                or not isinstance(message['text'],str) or len(message['text'])>4000 or not isinstance(message['timestamp_label'],str) or len(message['timestamp_label'])>80
                or type(message['text_truncated']) is not bool or type(message['full_text_available']) is not bool
                or message['full_text_available']!=(message['kind']=='text' and bool(message['text']) and not message['text_truncated'])):invalid()
        identity=message['message_id']
        if identity is None:
            if message['message_id_source']!='unavailable':invalid()
        else:
            if (not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_~.\-]{1,128}',identity)
                    or identity in seen or message['message_id_source']!='dom_data_mid'):invalid()
            seen.add(identity)
    return value
