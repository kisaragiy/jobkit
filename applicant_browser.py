"""Dedicated visible Edge session preparation; no cookies or platform messages."""
import json
import base64
import math
import hashlib
import re
import os
from pathlib import Path
import socket
import stat
import subprocess
import time
import websocket
import urllib.error
import urllib.request
from urllib.parse import urlparse, quote

import store
import applicant_salary_ocr
import applicant_inbox
import applicant_messages
from sandbox import KernelError, current_execution, require_maintenance

PORT = 9222
JOBS_URL = 'https://www.zhipin.com/web/geek/jobs'
CHAT_URL = 'https://www.zhipin.com/web/geek/chat'


def _edge_executable():
    for base, suffix in [('PROGRAMFILES(X86)', 'Microsoft/Edge/Application/msedge.exe'),
                         ('PROGRAMFILES', 'Microsoft/Edge/Application/msedge.exe'),
                         ('LOCALAPPDATA', 'Microsoft/Edge/Application/msedge.exe')]:
        value = os.environ.get(base)
        if value and (Path(value) / suffix).is_file():
            return Path(value) / suffix
    raise KernelError('EDGE_NOT_FOUND', '未找到本机 Edge，请先安装或核对维护环境。')


def _port_busy():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=0.2):
            return True
    except OSError:
        return False


def _directory(path):
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or os.path.normcase(str(path.resolve())) != os.path.normcase(str(path))):
        raise KernelError('UNSAFE_BROWSER_PROFILE', '求职浏览器资料目录不能使用链接或重解析点。')


def open_session(scope_id):
    require_maintenance()
    context = current_execution('runtime.applicant.browser.open')
    if scope_id != context['scope_id']:
        raise KernelError('UNSAFE_BROWSER_PROFILE', '求职浏览器必须使用当前工作区的独立资料目录。')
    if _port_busy():
        raise KernelError('BROWSER_PORT_IN_USE', '求职浏览器端口已被占用，请先检查现有连接；不会关闭其他浏览器。')
    executable = _edge_executable()
    root = Path(store.DB_PATH).resolve().parent / '.jobkit-browsers'
    _directory(root)
    profile = root / scope_id
    _directory(profile)
    arguments = [str(executable), '--user-data-dir=' + str(profile), '--remote-debugging-port=' + str(PORT),
                 '--remote-debugging-address=127.0.0.1', '--no-first-run', '--new-window', JOBS_URL]
    child_env = {key: value for key, value in os.environ.items()
                 if key not in {'JOBKIT_LLM_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'}}
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, shell=False, env=child_env)
    return {'browser': 'edge', 'launcher_pid': process.pid, 'profile_directory': str(profile),
            'login_required': True, 'account_verified': False, 'sending_enabled': False,
            'cookies_imported': False, 'existing_browsers_closed': False}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise KernelError('BROWSER_CONNECTION_INVALID', '本地浏览器诊断不接受重定向。')


def _read_json(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open('http://127.0.0.1:' + str(PORT) + path, timeout=1) as response:
        raw = response.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器连接返回的数据超过诊断容量。')
    return json.loads(raw.decode('utf-8'))


def _browser_endpoint():
    metadata = _read_json('/json/version')
    endpoint = metadata.get('webSocketDebuggerUrl') if isinstance(metadata, dict) else None
    if not isinstance(endpoint, str):
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器会话标识无法读取。')
    try:
        target = urlparse(endpoint)
        valid = (target.scheme == 'ws' and target.hostname in {'127.0.0.1', 'localhost'} and target.port == PORT
                 and not target.username and not target.password and not target.query and not target.fragment
                 and re.fullmatch(r'/devtools/browser/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', target.path))
    except ValueError:
        valid = False
    if not valid:
        raise KernelError('BROWSER_CONNECTION_INVALID', '只接受固定回环端口的浏览器会话标识。')
    return endpoint, hashlib.sha256(target.path.rsplit('/', 1)[1].encode('ascii')).hexdigest()


def _fixed_reply(connection, sequence, method, params, *, side_effect_possible=False):
    connection.send(json.dumps({'id': sequence, 'method': method, 'params': params}))
    deadline, size = time.monotonic() + 3, 0
    for _ in range(50):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection.settimeout(remaining)
        try:
            raw = connection.recv()
        except (websocket.WebSocketTimeoutException, TimeoutError):
            break
        size += len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
        if size > 512 * 1024:
            raise KernelError('BROWSER_CONNECTION_INVALID', '会话读取响应超过容量。',outcome_unknown=side_effect_possible)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError('invalid response')
            if result.get('id') != sequence:
                continue
            if 'error' in result or not isinstance(result.get('result'), dict) or result['result'].get('exceptionDetails'):
                raise ValueError('invalid response')
            return result['result']
        except (TypeError, ValueError):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '会话读取响应无法核对。',outcome_unknown=side_effect_possible) from None
    raise KernelError('BROWSER_READ_TIMEOUT', '会话读取超时，请重新检查页面。',outcome_unknown=side_effect_possible)


def _require_browser_owner(context):
    with store.runtime_db() as c:
        owner = c.execute("SELECT 1 FROM runtime_tasks WHERE scope_id=? AND tool='runtime.applicant.browser.open' AND status='completed' LIMIT 1", (context['scope_id'],)).fetchone()
    if owner is None:
        raise KernelError('APPLICANT_BROWSER_REQUIRED', '请先在当前工作区准备专用浏览器。')


def open_chat():
    require_maintenance()
    context = current_execution('runtime.applicant.chat.open')
    _require_browser_owner(context)
    tabs = _read_json('/json/list')
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    chats = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面记录无法核对。')
        url = urlparse(tab['url'])
        if url.scheme == 'https' and url.hostname == 'www.zhipin.com' and url.path == '/web/geek/chat':
            chats.append(tab)
    if chats:
        return {'status': 'already_open' if len(chats) == 1 else 'page_selection_required', 'sending_enabled': False}
    endpoint, fingerprint = _browser_endpoint()
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True, http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        reply = _fixed_reply(connection, 1, 'Target.createTarget', {'url': CHAT_URL, 'background': True},side_effect_possible=True)
        if not isinstance(reply.get('targetId'), str) or not re.fullmatch(r'[A-Za-z0-9_\-]{1,128}', reply['targetId']):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '沟通页面打开结果无法核对。',outcome_unknown=True)
        if _browser_endpoint()[1] != fingerprint:
            raise KernelError('BROWSER_SESSION_CHANGED', '浏览器会话已变化，页面打开结果需重新核对。',outcome_unknown=True)
        return {'status': 'chat_page_opened', 'browser_session_fingerprint': fingerprint, 'sending_enabled': False}
    finally:
        connection.close()


def inspect_chat():
    return read_chat()


def read_chat(*, binding=None, navigation=None, messages=False, preflight=False):
    if preflight and not messages:
        raise KernelError('INVALID_BROWSER_REQUEST','发送前检查只允许固定的消息读取。',400)
    if messages and (binding is None or navigation is not None):
        raise KernelError('INVALID_BROWSER_REQUEST','消息读取必须使用当前已绑定的会话。',400)
    if binding is None or navigation is not None:
        require_maintenance()
    context = current_execution('runtime.applicant.reply.preflight' if preflight else
                                'runtime.applicant.messages.read' if messages else
                                'runtime.applicant.conversation.open' if navigation is not None else
                                'runtime.applicant.chat.inspect' if binding is None else 'runtime.applicant.conversations.read')
    _require_browser_owner(context)
    if binding is not None and _browser_endpoint()[1] != binding['browser_session_fingerprint']:
        raise KernelError('BROWSER_BINDING_MISMATCH', '浏览器会话已变化，请重新核对绑定。')
    tabs = _read_json('/json/list')
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    choices = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面记录无法核对。')
        page = urlparse(tab['url'])
        if page.scheme == 'https' and page.hostname == 'www.zhipin.com' and page.path == '/web/geek/chat':
            choices.append(tab)
    if len(choices) != 1:
        return {'status': 'chat_page_required' if not choices else 'page_selection_required',
                **({'messages':[],'party':None,'messages_truncated':False,'history_complete':False,'recipient_verified':False,'automation_ready':False}
                   if messages else {'rows': [], 'automation_ready': False, 'list_truncated': False} if binding is not None else {})}
    endpoint = choices[0].get('webSocketDebuggerUrl')
    if not isinstance(endpoint, str):
        raise KernelError('BROWSER_CONNECTION_INVALID', '沟通页面连接无法核对。')
    parsed = urlparse(endpoint)
    if (parsed.scheme != 'ws' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.port != PORT
            or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.startswith('/devtools/page/')):
        raise KernelError('BROWSER_CONNECTION_INVALID', '沟通页面必须使用固定回环端口连接。')
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True, http_no_proxy=['127.0.0.1', 'localhost'])
    local_action_started=False
    try:
        script = Path(__file__).with_name('applicant_chat_inspect.js' if binding is None else 'applicant_inbox_read.js').read_text(encoding='utf-8')
        if binding is not None:
            if messages:
                message_script=Path(__file__).with_name('applicant_messages_read.js').read_text(encoding='utf-8')
                script=message_script+'('+json.dumps(binding['display_name'],ensure_ascii=False)+','+script+')'
            elif navigation is None:
                script = script + '(' + json.dumps(binding['display_name'], ensure_ascii=False) + ')'
            else:
                navigation_script=Path(__file__).with_name('applicant_conversation_open.js').read_text(encoding='utf-8')
                args=[binding['display_name'],navigation['snapshot'],navigation['row_index']]
                script=navigation_script+'('+','.join(json.dumps(arg,ensure_ascii=False,allow_nan=False) for arg in args)+','+script+')'
        sequence=1
        window_restored=False
        if navigation is not None:
            window=_fixed_reply(connection,sequence,'Browser.getWindowForTarget',{})
            sequence+=1
            if (type(window.get('windowId')) is not int or window['windowId']<1 or not isinstance(window.get('bounds'),dict)
                    or window['bounds'].get('windowState') not in {'normal','minimized','maximized','fullscreen'}):
                raise KernelError('BROWSER_SCHEMA_CHANGED','专用沟通窗口无法核对，未继续打开会话。')
            if window['bounds']['windowState']=='minimized':
                local_action_started=True
                _fixed_reply(connection,sequence,'Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'windowState':'normal'}})
                sequence+=1
                window_restored=True
            local_action_started=True
            _fixed_reply(connection,sequence,'Page.bringToFront',{})
            sequence+=1
        value = _fixed_reply(connection, sequence, 'Runtime.evaluate', {'expression': script, 'returnByValue': True}).get('result', {}).get('value')
        if messages:
            repeated=_fixed_reply(connection,sequence+1,'Runtime.evaluate',{'expression':script,'returnByValue':True}).get('result',{}).get('value')
            if repeated!=value:
                raise KernelError('CONVERSATION_CHANGED','读取期间会话或消息发生变化，请重新读取。')
        if binding is not None:
            if _browser_endpoint()[1] != binding['browser_session_fingerprint']:
                raise KernelError('BROWSER_SESSION_CHANGED', '读取期间浏览器会话发生变化，未采用预览。')
            if messages:
                return applicant_messages.checked(value)
            if navigation is None:
                return applicant_inbox.checked(value, binding['display_name'])
            if (not isinstance(value,dict) or set(value)!={'status','navigation_requested','recipient_verified','sending_enabled'}
                    or value['status'] not in {'conversation_changed','identity_unavailable','restricted','auth_required','session_changed',
                        'schema_changed','empty_or_loading','conversation_reference_incomplete','conversation_reference_ambiguous','navigation_requested'}
                    or type(value['navigation_requested']) is not bool or value['navigation_requested']!=(value['status']=='navigation_requested')
                    or value['recipient_verified'] is not False or value['sending_enabled'] is not False):
                raise KernelError('BROWSER_SCHEMA_CHANGED','会话打开结果无法核对。')
            return {**value,'window_restored':window_restored}
        if not isinstance(value, dict) or set(value) != {'page_path', 'viewport', 'notices', 'rows', 'messages', 'classes'}:
            raise KernelError('BROWSER_SCHEMA_CHANGED', '沟通页面结构无法核对。')
        metadata=_read_json('/json/version')
        product=metadata.get('Browser','') if isinstance(metadata,dict) else ''
        return {**value,'browser_product':product[:100] if isinstance(product,str) else ''}
    except KernelError as error:
        if local_action_started:
            error.outcome_unknown=True
        raise
    finally:
        connection.close()


def check_connection():
    result = {'browser_reachable': False, 'boss_page_count': 0, 'page_types': [],
              'account_verified': False, 'sending_enabled': False, 'cookies_read': False}
    try:
        tabs = _read_json('/json/list')
    except KernelError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        return {**result, 'status': 'unavailable'}
    except (ValueError, UnicodeError, RecursionError):
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器连接数据无法解析。') from None
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    types = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面信息不完整。')
        url = urlparse(tab['url'])
        if url.scheme != 'https' or url.hostname not in {'www.zhipin.com', 'zhipin.com'}:
            continue
        if url.path.startswith('/web/geek/jobs'):
            types.append('jobs')
        elif url.path.startswith('/web/geek/chat'):
            types.append('chat')
        elif url.path.startswith('/job_detail/'):
            types.append('job_detail')
    return {**result, 'browser_reachable': True, 'boss_page_count': len(types),
            'page_types': types, 'status': 'page_found_unverified' if types else 'no_applicant_page'}


def read_current_jobs(*, diagnostics=False, identity=False, salary=False, reset_viewport=False, binding_check=False):
    if diagnostics or reset_viewport:
        require_maintenance()
    if sum((diagnostics, identity, salary, reset_viewport, binding_check)) > 1:
        raise KernelError('INVALID_BROWSER_REQUEST', '一次只能执行一种固定页面读取。', 400)
    context = current_execution('runtime.applicant.browser.inspect' if diagnostics else
                                'runtime.applicant.browser.binding.check' if binding_check else
                                'runtime.applicant.browser.viewport.reset' if reset_viewport else
                                'runtime.applicant.identity.read' if identity else
                                'runtime.applicant.salary.read' if salary else 'runtime.applicant.jobs.read')
    with store.runtime_db() as c:
        owner = c.execute("SELECT 1 FROM runtime_tasks WHERE scope_id=? AND tool='runtime.applicant.browser.open' AND status='completed' LIMIT 1", (context['scope_id'],)).fetchone()
    if owner is None:
        raise KernelError('APPLICANT_BROWSER_REQUIRED', '请先在此求职者工作区准备专用浏览器。')
    identity_mode = identity or binding_check
    session = None
    try:
        if identity_mode:
            session = _browser_endpoint()[1]
        tabs = _read_json('/json/list')
    except (urllib.error.URLError, TimeoutError, OSError):
        return {'status': 'unavailable', 'jobs': [], 'account_verified': False, 'sending_enabled': False}
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    choices = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面信息不完整。')
        page = urlparse(tab['url'])
        if page.scheme == 'https' and page.hostname == 'www.zhipin.com' and page.path in {'/web/geek/jobs', '/web/geek/job'}:
            choices.append(tab)
    if len(choices) != 1:
        return {'status': 'page_selection_required', 'jobs': [], 'account_verified': False, 'sending_enabled': False}
    endpoint = choices[0].get('webSocketDebuggerUrl')
    if not isinstance(endpoint, str):
        raise KernelError('BROWSER_CONNECTION_INVALID', '求职页面缺少可核对的本地连接。')
    target = urlparse(endpoint)
    if (target.scheme != 'ws' or target.hostname not in {'127.0.0.1', 'localhost'} or target.port != PORT
            or target.username or target.password or target.query or target.fragment or not target.path.startswith('/devtools/page/')):
        raise KernelError('BROWSER_CONNECTION_INVALID', '仅允许固定回环端口的求职页面连接。')
    script = Path(__file__).with_name('applicant_salary_regions.js' if salary else
                                    'applicant_identity_read.js' if identity_mode else 'applicant_dom_read.js').read_text(encoding='utf-8')
    if reset_viewport:
        script = "(() => ({width:innerWidth,height:innerHeight,outer_width:outerWidth,outer_height:outerHeight,visibility:document.visibilityState}))()"
    if diagnostics:
        script = """(() => {const counts={};for(const node of document.querySelectorAll('[class]')){const name=typeof node.className==='string'?node.className:'';if(/job.*(?:card|list)|(?:card|list).*job|nav-figure|user-nav|user-name|label-text|geek-name/i.test(name))counts[name]=(counts[name]||0)+1;}return {page_path:location.pathname,page_title:document.title.slice(0,120),viewport:{width:innerWidth,height:innerHeight,outer_width:outerWidth,outer_height:outerHeight,pixel_ratio:devicePixelRatio,scroll_y:scrollY,visibility:document.visibilityState},salary_rects:[...document.querySelectorAll(".job-salary,.salary,.salary-text")].slice(0,3).map(n=>{const r=n.getBoundingClientRect();return {x:r.left,y:r.top,width:r.width,height:r.height};}),classes:Object.entries(counts).slice(0,40),job_links:[...document.querySelectorAll('a[href*="/job_detail/"]')].slice(0,3).map(node=>{const parents=[];for(let p=node,i=0;p&&i<6;p=p.parentElement,i++)parents.push({tag:p.tagName,classes:typeof p.className==='string'?p.className:''});const card=node.closest("li.job-card-box");return {parents,path:new URL(node.getAttribute("href"),location.href).pathname,fields:card?[...card.querySelectorAll("[class]")].slice(0,35).map(n=>({classes:typeof n.className==='string'?n.className:'',text:(n.innerText||'').trim().slice(0,100)})):[]};})};})()"""
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True, http_no_proxy=['127.0.0.1', 'localhost'])
    deadline, received = time.monotonic() + 3, 0
    try:
        connection.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {'expression': script, 'returnByValue': True}}))
        for _ in range(50):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KernelError('BROWSER_READ_TIMEOUT', '求职页面读取超时，未采用不完整结果。')
            connection.settimeout(remaining)
            raw = connection.recv()
            received += len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
            if received > 1024 * 1024:
                raise KernelError('BROWSER_CONNECTION_INVALID', '页面响应超过读取容量。')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise KernelError('BROWSER_SCHEMA_CHANGED', '页面响应格式无法核对。')
            if data.get('id') != 1:
                continue
            if 'error' in data or data.get('result', {}).get('exceptionDetails'):
                raise KernelError('BROWSER_SCHEMA_CHANGED', '页面读取规则未通过，请人工核对。')
            value = data.get('result', {}).get('result', {}).get('value')
            if reset_viewport:
                return _reset_collapsed_viewport(connection, value, script)
            if salary:
                return _salary_evidence(connection, value, script)
            if diagnostics:
                if not isinstance(value, dict) or set(value) != {'page_path', 'page_title', 'viewport', 'salary_rects', 'classes', 'job_links'}:
                    raise KernelError('BROWSER_SCHEMA_CHANGED', '页面诊断结果无法核对。')
                return value
            if identity_mode:
                if (not isinstance(value, dict) or set(value) != {'status', 'display_name', 'role_evidence'}
                        or value['status'] not in {'identity_unavailable', 'restricted', 'auth_required', 'display_name_observed'}
                        or not isinstance(value['display_name'], str) or len(value['display_name']) > 40
                        or value['role_evidence'] != 'applicant_page'
                        or (value['status'] == 'display_name_observed') != bool(value['display_name'])):
                    raise KernelError('BROWSER_SCHEMA_CHANGED', '账号显示资料无法核对。')
                try:
                    unchanged = _browser_endpoint()[1] == session
                except (urllib.error.URLError, TimeoutError, OSError):
                    unchanged = False
                if not unchanged:
                    raise KernelError('BROWSER_SESSION_CHANGED', '读取过程中浏览器会话已变化，请重新核对。')
                return {**value, 'browser_session_fingerprint': session, 'account_verified': False, 'sending_enabled': False}
            if (not isinstance(value, dict) or set(value) != {'status', 'jobs'}
                    or value['status'] not in {'observed', 'empty', 'auth_required', 'restricted', 'schema_changed'}
                    or not isinstance(value['jobs'], list) or (value['status'] != 'observed' and value['jobs'])
                    or (value['status'] == 'observed' and not value['jobs'])):
                raise KernelError('BROWSER_SCHEMA_CHANGED', '页面结果无法完整核对。')
            return {**value, 'account_verified': False, 'sending_enabled': False}
        raise KernelError('BROWSER_READ_TIMEOUT', '未取得对应页面读取结果。')
    finally:
        connection.close()


def _reset_collapsed_viewport(connection, value, script):
    if (not isinstance(value, dict) or set(value) != {'width', 'height', 'outer_width', 'outer_height', 'visibility'}
            or any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or not 0 <= value[k] <= 20000
                   for k in ('width', 'height', 'outer_width', 'outer_height'))):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '浏览器显示尺寸无法核对。')
    base = {'sending_enabled': False, 'account_verified': False}
    if value['width'] >= 600 and value['height'] >= 300:
        return {**base, 'status': 'reset_not_needed', 'viewport': value}
    if value['visibility'] != 'visible' or value['outer_width'] < 900 or value['outer_height'] < 600:
        return {**base, 'status': 'manual_restore_required', 'viewport': value}
    def call(sequence, method, params):
        connection.send(json.dumps({'id': sequence, 'method': method, 'params': params}))
        deadline = time.monotonic() + 3
        size = 0
        for _ in range(50):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection.settimeout(remaining)
            try:
                raw = connection.recv()
            except (websocket.WebSocketTimeoutException, TimeoutError):
                raise KernelError('VIEWPORT_RESET_UNKNOWN', '浏览器显示恢复结果尚未核对，请重新检查。',outcome_unknown=True) from None
            size += len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
            if size > 128 * 1024:
                raise KernelError('BROWSER_CONNECTION_INVALID', '显示恢复响应超过容量。',outcome_unknown=True)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise KernelError('BROWSER_SCHEMA_CHANGED', '显示恢复响应无法核对。',outcome_unknown=True)
            if data.get('id') != sequence:
                continue
            if 'error' in data or not isinstance(data.get('result'), dict) or data['result'].get('exceptionDetails'):
                raise KernelError('VIEWPORT_RESET_UNKNOWN', '浏览器显示恢复结果尚未核对，请重新检查。',outcome_unknown=True)
            return data['result']
        raise KernelError('VIEWPORT_RESET_UNKNOWN', '浏览器显示恢复结果尚未核对，请重新检查。',outcome_unknown=True)
    call(2, 'Emulation.clearDeviceMetricsOverride', {})
    call(3, 'Emulation.setVisibleSize', {'width': min(2560, int(value['outer_width'])),
                                      'height': min(1440, max(600, int(value['outer_height']) - 160))})
    after = call(4, 'Runtime.evaluate', {'expression': script, 'returnByValue': True}).get('result', {}).get('value')
    if (not isinstance(after, dict) or set(after) != set(value) or after['visibility'] != 'visible'
            or any(type(after[k]) not in (int, float) or not math.isfinite(after[k]) or not 0 <= after[k] <= 20000
                   for k in ('width', 'height', 'outer_width', 'outer_height'))):
        raise KernelError('VIEWPORT_RESET_UNKNOWN', '浏览器恢复后的显示尺寸无法核对。',outcome_unknown=True)
    return {**base, 'status': 'reset_completed' if after['width'] >= 600 and after['height'] >= 300 else 'manual_restore_required', 'viewport': after}


def _salary_evidence(connection, value, script):
    """Fixed read/screenshot protocol; images remain in memory, never in task results."""
    if (not isinstance(value, dict) or set(value) != ({'status', 'regions', 'viewport'} if value.get('status') == 'observed' else {'status', 'regions'})
            or value['status'] not in {'observed', 'restricted', 'auth_required', 'schema_changed', 'no_visible_salary', 'fonts_loading', 'browser_not_visible'}
            or not isinstance(value['regions'], list) or len(value['regions']) > 8
            or (value['status'] == 'observed') != bool(value['regions'])):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '薪资区域无法完整核对。')
    result = {'status': value['status'], 'salaries': [], 'account_verified': False, 'sending_enabled': False,
              'matching_updated': False, 'source_type': 'rendered_salary_ocr_unconfirmed'}
    if value['status'] != 'observed':
        return result
    viewport = value['viewport']
    if (not isinstance(viewport, dict) or set(viewport) != {'width', 'height'}
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in viewport.values())
            or not 600 <= viewport['width'] <= 4096 or not 300 <= viewport['height'] <= 2160):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '当前可视区尺寸不适用于薪资截图。')
    import re
    seen = set()
    for region in value['regions']:
        if (not isinstance(region, dict) or set(region) != {'job_id', 'raw_text', 'rect'}
                or not isinstance(region['job_id'], str) or not re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', region['job_id'])
                or region['job_id'] in seen or not isinstance(region['raw_text'], str) or len(region['raw_text']) > 80
                or not isinstance(region['rect'], dict) or set(region['rect']) != {'x', 'y', 'width', 'height'}):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '薪资岗位或区域标识无法核对。')
        seen.add(region['job_id'])
        rect = region['rect']
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in rect.values())
                or not (0 <= rect['x'] <= 20000 and 0 <= rect['y'] <= 20000
                        and 4 <= rect['width'] <= 600 and 4 <= rect['height'] <= 120)):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '薪资截图区域超出允许范围。')
        if rect['x'] + rect['width'] > viewport['width'] or rect['y'] + rect['height'] > viewport['height']:
            raise KernelError('BROWSER_SCHEMA_CHANGED', '薪资区域不在当前可视区内。')
    sequence = 1
    batch_deadline = time.monotonic() + 30
    def rpc(method, params):
        nonlocal sequence
        sequence += 1
        connection.send(json.dumps({'id': sequence, 'method': method, 'params': params}))
        deadline, size = min(batch_deadline, time.monotonic() + (10 if method == 'Page.captureScreenshot' else 3)), 0
        for _ in range(50):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection.settimeout(remaining)
            try:
                raw = connection.recv()
            except (websocket.WebSocketTimeoutException, TimeoutError):
                raise KernelError('BROWSER_READ_TIMEOUT', '薪资截图读取超时，未采用不完整结果。') from None
            size += len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
            if size > 4 * 1024 * 1024:
                raise KernelError('BROWSER_CONNECTION_INVALID', '薪资截图响应超过容量。')
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise KernelError('BROWSER_SCHEMA_CHANGED', '薪资截图响应无法核对。')
            if response.get('id') != sequence:
                continue
            if 'error' in response or not isinstance(response.get('result'), dict) or response['result'].get('exceptionDetails'):
                raise KernelError('BROWSER_SCHEMA_CHANGED', '薪资截图读取未完成。')
            return response['result']
        raise KernelError('BROWSER_READ_TIMEOUT', '薪资截图读取超时，未采用不完整结果。')
    # One viewport capture without clip/scale avoids Chromium changing the page's
    # visible size during clipped screenshot capture. Crop locally after attribution.
    screenshot = rpc('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
    try:
        encoded = screenshot['data']
        if not isinstance(encoded, str) or len(encoded) > 2800000:
            raise ValueError('oversized screenshot')
        png = base64.b64decode(encoded, validate=True)
        if len(png) > 2 * 1024 * 1024:
            raise ValueError('oversized screenshot')
    except (ValueError, KeyError):
        raise KernelError('INVALID_SALARY_IMAGE', '薪资截图数据无法核对。') from None
    # Refuse attribution if a list update or scroll changed any salary location.
    after = rpc('Runtime.evaluate', {'expression': script, 'returnByValue': True}).get('result', {}).get('value')
    if after != value:
        raise KernelError('SALARY_PAGE_CHANGED', '岗位页面已变化，请重新读取薪资，未采用旧截图。')
    images = _crop_salary_regions(png, value)
    for region, png in zip(value['regions'], images):
        evidence = applicant_salary_ocr.recognize(png)
        result['salaries'].append({'job_id': region['job_id'], 'raw_text': region['raw_text'], **evidence})
    return result


def _crop_salary_regions(png, observation):
    from io import BytesIO
    from PIL import Image
    try:
        with Image.open(BytesIO(png)) as source:
            if source.format != 'PNG' or not 1 <= source.width <= 4096 or not 1 <= source.height <= 2160:
                raise ValueError('invalid viewport image')
            xscale = source.width / observation['viewport']['width']
            yscale = source.height / observation['viewport']['height']
            if not 0.5 <= xscale <= 2 or not 0.5 <= yscale <= 2 or abs(xscale - yscale) > 0.02:
                raise ValueError('inconsistent screenshot scale')
            source.load()
            images = []
            for region in observation['regions']:
                rect = region['rect']
                box = (math.floor(rect['x'] * xscale), math.floor(rect['y'] * yscale),
                       math.ceil((rect['x'] + rect['width']) * xscale), math.ceil((rect['y'] + rect['height']) *yscale))
                crop = source.crop(box)
                if crop.width > 1200 or crop.height > 240:
                    raise ValueError('oversized salary crop')
                stream = BytesIO()
                crop.save(stream, format='PNG')
                images.append(stream.getvalue())
            return images
    except (OSError, ValueError, Image.DecompressionBombError):
        raise KernelError('INVALID_SALARY_IMAGE', '可视区截图尺寸或缩放比例无法核对。') from None

LIST_PAGE_PATHS = {'/web/geek/jobs', '/web/geek/job'}


def _jobs_page_tabs():
    try:
        tabs = _read_json('/json/list')
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    choices = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面信息不完整。')
        page = urlparse(tab['url'])
        if page.scheme == 'https' and page.hostname == 'www.zhipin.com' and page.path in LIST_PAGE_PATHS:
            choices.append(tab)
    return choices


def _loopback_endpoint(endpoint):
    target = urlparse(endpoint) if isinstance(endpoint, str) else None
    if (target is None or target.scheme != 'ws' or target.hostname not in {'127.0.0.1', 'localhost'}
            or target.port != PORT or target.username or target.password or target.query
            or target.fragment or not target.path.startswith('/devtools/page/')):
        raise KernelError('BROWSER_CONNECTION_INVALID', '仅允许固定回环端口的求职页面连接。')
    return endpoint


def search_open(keyword, city):
    """Navigate the dedicated jobs tab to the fixed search URL. Maintenance only.

    地址由 dom.boss_selectors 的实测模板与城市码拼出，不接受外部传入的地址或脚本。
    """
    from dom import boss_selectors
    require_maintenance()
    current_execution('runtime.applicant.jobs.search.open')
    if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 40:
        raise KernelError('INVALID_JOB_SEARCH', '请提供 1-40 字的岗位关键词。', 400)
    if not isinstance(city, str) or not 1 <= len(city.strip()) <= 20:
        raise KernelError('INVALID_JOB_SEARCH', '请提供有效的求职城市。', 400)
    code = boss_selectors.city_code(city.strip())
    if not code:
        raise KernelError('CITY_NOT_SUPPORTED',
                          '该城市暂时没有可用的搜索编码，请改用广州、深圳、东莞等已支持城市。', 400)
    url = boss_selectors.LIST_URL.format(query=quote(keyword.strip(), safe=''), city=code)
    tabs = _jobs_page_tabs()
    if tabs is None:
        return {'status': 'unavailable', 'account_verified': False, 'sending_enabled': False}
    if len(tabs) != 1:
        return {'status': 'jobs_page_required' if not tabs else 'page_selection_required',
                'account_verified': False, 'sending_enabled': False}
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        reply = _fixed_reply(connection, 1, 'Page.navigate', {'url': url}, side_effect_possible=True)
    finally:
        connection.close()
    if not isinstance(reply, dict) or not isinstance(reply.get('frameId'), str):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '搜索页面导航未返回可核对的结果。')
    return {'status': 'navigation_requested', 'keyword': keyword.strip(), 'city': city.strip(),
            'city_code': code, 'account_verified': False, 'sending_enabled': False}


def scroll_jobs():
    """Scroll the jobs tab to the bottom so more cards load. No click, no input."""
    require_maintenance()
    current_execution('runtime.applicant.jobs.search.step')
    tabs = _jobs_page_tabs()
    if tabs is None:
        return {'status': 'unavailable'}
    if len(tabs) != 1:
        return {'status': 'jobs_page_required' if not tabs else 'page_selection_required'}
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    script = Path(__file__).with_name('applicant_jobs_scroll.js').read_text(encoding='utf-8')
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        value = _fixed_reply(connection, 1, 'Runtime.evaluate',
                             {'expression': script, 'returnByValue': True}).get('result', {}).get('value')
    finally:
        connection.close()
    if (not isinstance(value, dict) or set(value) != {'status', 'before'}
            or value['status'] != 'scrolled' or not isinstance(value['before'], dict)
            or set(value['before']) != {'height', 'cards', 'y'}
            or any(type(item) is not int or item < 0 for item in value['before'].values())):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '页面滚动结果无法核对。')
    return {'status': 'scrolled', 'before': value['before']}

# ---------------------------------------------------------------- 岗位详情页 JD（G2）
# 详情页是只读观察：固定脚本 applicant_job_detail_read.js，只允许 zhipin.com 的
# /job_detail/<id>.html；导航由 detail_open 做，读由 real_job_detail 做，两步都走内核。
DETAIL_PATH_PREFIX = '/job_detail/'
DETAIL_URL_TEMPLATE = 'https://www.zhipin.com/job_detail/{job_id}.html'
JD_MAX_CHARS = 4000


def job_id_ok(value):
    """job_id 必须能原样拼进 /job_detail/<id>.html，否则拒绝（防注入路径/查询串）。"""
    return (isinstance(value, str) and 1 <= len(value) <= 128
            and all(ch.isalnum() or ch in '_-~' for ch in value))


def _detail_page_tabs():
    try:
        tabs = _read_json('/json/list')
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    choices = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面信息不完整。')
        page = urlparse(tab['url'])
        if (page.scheme == 'https' and page.hostname == 'www.zhipin.com'
                and page.path.startswith(DETAIL_PATH_PREFIX) and page.path.endswith('.html')):
            choices.append(tab)
    return choices


def _detail_job_id(tab):
    page = urlparse(tab['url'])
    name = page.path[len(DETAIL_PATH_PREFIX):-len('.html')]
    return name if job_id_ok(name) else None


def detail_open(job_id):
    """Navigate the jobs tab to a fixed job-detail URL. Maintenance only.

    URL 由 job_id 拼出，不接受外部传入地址；导航本身有副作用（页面状态变化），
    因此走 require_maintenance + 执行名登记，不是 effect=none。
    """
    require_maintenance()
    current_execution('runtime.applicant.jobs.detail')
    if not job_id_ok(job_id):
        raise KernelError('INVALID_JOB_DETAIL', '岗位标识不合法，无法打开详情页。', 400)
    tabs = _jobs_page_tabs()
    if tabs is None:
        return {'status': 'unavailable', 'account_verified': False, 'sending_enabled': False}
    if len(tabs) != 1:
        return {'status': 'jobs_page_required' if not tabs else 'page_selection_required',
                'account_verified': False, 'sending_enabled': False}
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        reply = _fixed_reply(connection, 1, 'Page.navigate',
                             {'url': DETAIL_URL_TEMPLATE.format(job_id=job_id)},
                             side_effect_possible=True)
    finally:
        connection.close()
    if not isinstance(reply, dict) or not isinstance(reply.get('frameId'), str):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页导航未返回可核对的结果。')
    return {'status': 'navigation_requested', 'job_id': job_id,
            'account_verified': False, 'sending_enabled': False,
            'delivery_confirmed': False}


def real_job_detail(expected_job_id=None):
    """Read JD text from the single open job-detail tab. Read-only, no clicks.

    双重核对：先看 tab URL 的 job_id，再核对脚本返回的 job_id 与之一致；
    调用方给了 expected_job_id 时还要三者相同，否则拒绝（读错对象的正文等于假证据）。
    """
    tabs = _detail_page_tabs()
    if tabs is None:
        return {'status': 'unavailable', 'sending_enabled': False}
    if len(tabs) != 1:
        return {'status': 'detail_page_required' if not tabs else 'page_selection_required',
                'sending_enabled': False}
    tab_job_id = _detail_job_id(tabs[0])
    if tab_job_id is None:
        raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页地址里的岗位标识无法核对。')
    if expected_job_id is not None and tab_job_id != expected_job_id:
        raise KernelError('DETAIL_TARGET_MISMATCH', '当前详情页不是该岗位，未读取职责描述。')
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    script = Path(__file__).with_name('applicant_job_detail_read.js').read_text(encoding='utf-8')
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        value = _fixed_reply(connection, 1, 'Runtime.evaluate',
                             {'expression': script, 'returnByValue': True}
                             ).get('result', {}).get('value')
    finally:
        connection.close()
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页读取结果无法核对。')
    status = value['status']
    if status in {'restricted', 'auth_required', 'detail_page_required'}:
        return {'status': status, 'sending_enabled': False}
    if status == 'jd_missing':
        # 结构可能整体变了也可能是这个岗位真没写职责：两种都不能当「读到了空正文」。
        if value.get('job_id') != tab_job_id:
            raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页岗位标识与地址不一致。')
        return {'status': 'jd_missing', 'job_id': tab_job_id,
                'title': value.get('title') if isinstance(value.get('title'), str) else '',
                'company': value.get('company') if isinstance(value.get('company'), str) else '',
                'sending_enabled': False}
    if status != 'observed':
        raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页读取返回了未知状态。')
    if value.get('job_id') != tab_job_id:
        raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页岗位标识与地址不一致。')
    jd = value.get('jd')
    if not isinstance(jd, str) or not jd.strip() or len(jd) > JD_MAX_CHARS:
        raise KernelError('BROWSER_SCHEMA_CHANGED', '职责描述正文不完整或超出长度。')
    for key in ('title', 'company'):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页字段类型无法核对。')
        if isinstance(value.get(key), str) and len(value[key]) > 200:
            raise KernelError('BROWSER_SCHEMA_CHANGED', '详情页字段超出允许长度。')
    return {'status': 'observed', 'job_id': tab_job_id, 'jd': jd,
            'truncated': bool(value.get('truncated')),
            'title': value.get('title') or '', 'company': value.get('company') or '',
            'sending_enabled': False, 'delivery_confirmed': False,
            'account_verified': False}


# ---------------------------------------------------------------- 受控沟通页（G3）
# 受控页是本机回环页面，仅用于验证「填入 → 提交 → 观察」的执行协议。
# 它不是平台页面：选择器由 web/controlled_chat.html 固定，不能当作 BOSS 选择器证据。
CONTROLLED_CHAT_URL = 'http://127.0.0.1:18770/controlled-chat'
CONTROLLED_CHAT_PATH = '/controlled-chat'
MAX_ACTION_TEXT = 500

_FILL_SCRIPT = """(() => {
  const box = document.getElementById('compose-textarea');
  if (!box) { return {ok: false, code: 'COMPOSER_UNKNOWN'}; }
  box.focus();
  const text = %s;
  box.value = text;
  box.dispatchEvent(new Event('input', {bubbles: true}));
  if (box.value !== text) { return {ok: false, code: 'FILL_FAILED'}; }
  return {ok: true, code: 'FILLED', focused: document.activeElement === box};
})()"""

_SUBMIT_SCRIPT = """(() => {
  const btn = document.getElementById('send-button');
  const box = document.getElementById('compose-textarea');
  if (!btn) { return {ok: false, code: 'SEND_BUTTON_UNKNOWN'}; }
  btn.click();
  return {ok: true, code: 'CLICKED', cleared: box ? box.value === '' : null};
})()"""

_TARGET_SCRIPT = """(() => {
  const el = document.getElementById('target');
  if (!el) { return {ok: false, code: 'TARGET_UNKNOWN'}; }
  return {ok: true, code: 'TARGET',
           job_id: el.getAttribute('data-job-id'),
           company: el.getAttribute('data-company')};
})()"""

_OBSERVE_SCRIPT = """(() => {
  const list = document.getElementById('message-list');
  if (!list) { return {ok: false, code: 'MESSAGE_LIST_UNKNOWN'}; }
  const mine = Array.prototype.slice.call(list.querySelectorAll('[data-from="me"]'))
    .map(function (n) { return n.textContent; });
  return {ok: true, code: 'OBSERVED', count: mine.length,
          last: mine.length ? mine[mine.length - 1] : null,
          crashed: document.body.getAttribute('data-crashed') === '1'};
})()"""


def _controlled_tab():
    tabs = _read_json('/json/list')
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    choices = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面信息不完整。')
        page = urlparse(tab['url'])
        if (page.scheme == 'http' and page.hostname in {'127.0.0.1', 'localhost'}
                and page.port == 18770 and page.path == CONTROLLED_CHAT_PATH):
            choices.append(tab)
    if not choices:
        return None
    if len(choices) > 1:
        raise KernelError('CONTROLLED_PAGE_AMBIGUOUS', '受控页面打开了多个，请只保留一个。')
    return choices[0]


def controlled_open():
    """在专用浏览器里打开本机受控沟通页（只读准备，不涉及平台）。"""
    require_maintenance()
    current_execution('runtime.applicant.action.execute')
    _require_browser_owner(current_execution('runtime.applicant.action.execute'))
    tab = _controlled_tab()
    if tab is not None:
        return {'status': 'already_open', 'url': CONTROLLED_CHAT_URL, 'sending_enabled': False}
    endpoint, fingerprint = _browser_endpoint()
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        reply = _fixed_reply(connection, 1, 'Target.createTarget',
                             {'url': CONTROLLED_CHAT_URL, 'background': True},
                             side_effect_possible=True)
        if not isinstance(reply.get('targetId'), str) or not re.fullmatch(r'[A-Za-z0-9_\-]{1,128}', reply['targetId']):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '受控页面打开结果无法核对。', outcome_unknown=True)
        return {'status': 'controlled_page_opened', 'url': CONTROLLED_CHAT_URL,
                'browser_session_fingerprint': fingerprint, 'sending_enabled': False}
    finally:
        connection.close()


def _controlled_call(script, *, side_effect_possible):
    tab = _controlled_tab()
    if tab is None:
        raise KernelError('CONTROLLED_PAGE_REQUIRED', '请先打开受控沟通页。')
    endpoint = _loopback_endpoint(tab.get('webSocketDebuggerUrl'))
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        reply = _fixed_reply(connection, 1, 'Runtime.evaluate', {
            'expression': script, 'returnByValue': True, 'awaitPromise': False,
            'userGesture': False,
        }, side_effect_possible=side_effect_possible)
        value = (reply.get('result') or {}).get('value')
        if not isinstance(value, dict) or not isinstance(value.get('ok'), bool):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '受控页面返回无法核对。',
                              outcome_unknown=side_effect_possible)
        return value
    finally:
        connection.close()


def controlled_fill(text):
    """把待发正文写入受控输入框。写输入框本身也算副作用，失败一律按未确认处理。"""
    require_maintenance()
    current_execution('runtime.applicant.action.execute')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_ACTION_TEXT:
        raise KernelError('INVALID_ACTION_TEXT', '待发正文需为 1-500 字。', 400)
    if any(ch in text for ch in ('\x00', '\r')):
        raise KernelError('INVALID_ACTION_TEXT', '待发正文含不可发送字符。', 400)
    value = _controlled_call(_FILL_SCRIPT % json.dumps(text, ensure_ascii=False),
                             side_effect_possible=True)
    if not value['ok']:
        raise KernelError(value['code'], '受控输入框写入失败，未提交。',
                          outcome_unknown=value['code'] == 'FILL_FAILED')
    return {'status': 'filled', 'focused': value.get('focused') is True,
            'length': len(text), 'sending_enabled': False}


def controlled_submit():
    """点击受控发送按钮一次。这是唯一真实写动作，之后必须只读观察。"""
    require_maintenance()
    current_execution('runtime.applicant.action.execute')
    value = _controlled_call(_SUBMIT_SCRIPT, side_effect_possible=True)
    if not value['ok']:
        raise KernelError(value['code'], '受控发送按钮不可用，未提交。', outcome_unknown=True)
    return {'status': 'submitted', 'cleared': value.get('cleared') is True,
            'sending_enabled': False, 'delivery_confirmed': False}


def controlled_observe():
    """只读观察受控页面的己方消息回显。"""
    require_maintenance()
    current_execution('runtime.applicant.action.execute')
    value = _controlled_call(_OBSERVE_SCRIPT, side_effect_possible=False)
    if not value['ok']:
        raise KernelError(value['code'], '受控页面消息区域无法核对。')
    return {'status': 'observed', 'count': int(value.get('count') or 0),
            'last': value.get('last'), 'crashed': value.get('crashed') is True}


def controlled_target():
    """只读：受控页面上当前展示的目标。提交前必须与授权一致。"""
    require_maintenance()
    current_execution('runtime.applicant.action.execute')
    value = _controlled_call(_TARGET_SCRIPT, side_effect_possible=False)
    if not value['ok']:
        raise KernelError(value['code'], '受控页面目标无法核对。')
    return {'job_id': value.get('job_id'), 'company': value.get('company')}


# ---------------------------------------------------------------- 真实对话页（只读）
# 2026-10-27 真机实测（登录态为求职者）。以下选择器全部来自实测 DOM，不是推断：
#   div.chat-input    —— contenteditable 输入框（不是 textarea），752x79
#   button.btn-send   —— 发送按钮，class 尾缀 disabled 表示空内容不可发
#   .user-list-content li / .friend-content —— 会话列表与可点区域（点水平 35% 处）
#   i.message-status.status-delivery / status-read —— [送达] / [已读]
#   .chat-conversation / .chat-no-data —— 面板容器与未选中占位
# 打开对话必须走真实鼠标序列（mouseMoved → mousePressed → mouseReleased）：
#   el.click()、Vue handleOpenChat()、缺 mouseMoved 的 press/release 都已实测无效。
REAL_CHAT_PATHS = {'/web/geek/chat'}
REAL_INPUT_SELECTOR = 'div.chat-input'
REAL_SEND_SELECTOR = 'button.btn-send'
REAL_PANEL_SELECTOR = '.chat-conversation'
REAL_EMPTY_SELECTOR = '.chat-no-data'
REAL_STATUS_DELIVERY = 'i.message-status.status-delivery'
REAL_STATUS_READ = 'i.message-status.status-read'
MAX_OBSERVED_TEXT = 400

REAL_SESSION_SCRIPT = """(() => {
  const list = document.querySelector('.user-list-content');
  if (!list) { return {ok: false, code: 'SESSION_LIST_MISSING'}; }
  const items = [...list.querySelectorAll('li')].filter(li => li.getBoundingClientRect().height > 30);
  if (!items.length) { return {ok: false, code: 'SESSION_LIST_EMPTY'}; }
  const index = items.length > 1 ? 1 : 0;
  const li = items[index];
  const area = li.querySelector('.friend-content') || li;
  const rect = area.getBoundingClientRect();
  if (!(rect.width > 60 && rect.height > 20)) { return {ok: false, code: 'SESSION_RECT_INVALID'}; }
  return {ok: true, code: 'SESSION_READY',
          x: Math.round(rect.x + rect.width * 0.35),
          y: Math.round(rect.y + rect.height / 2),
          count: items.length,
          name: (li.querySelector('.name-text') || {}).innerText || null,
          company: (li.querySelector('.title-box span:nth-child(2)') || {}).innerText || null,
          preview: (li.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80)};
})()"""

REAL_OBSERVE_SCRIPT = """(() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 30 && r.height > 8; };
  const panel = document.querySelector('.chat-conversation');
  const editors = [...document.querySelectorAll('div.chat-input')].filter(visible);
  const sends = [...document.querySelectorAll('button.btn-send')].filter(visible);
  // 只统计右侧消息区：左侧会话列表的每条预览也带 .message-status（实测 44 条里 39 个），
  // 全局统计会把列表噪声算进来，得到的数字不是「本会话的消息状态」。
  const scope = panel ? panel.querySelector('.message-content') : null;
  const marks = scope
    ? [...scope.querySelectorAll('.message-status')].map(el => (el.innerText || '').trim())
    : [];
  const box = panel || document.body;
  return {
    composer: editors.length,
    can_send: sends.some(el => !/disabled/.test(String(el.className))),
    send_disabled: sends.length > 0 && sends.every(el => /disabled/.test(String(el.className))),
    empty: !!document.querySelector('.chat-no-data'),
    delivered: marks.filter(t => t.indexOf('送达') >= 0).length,
    read: marks.filter(t => t.indexOf('已读') >= 0).length,
    panel_text: (box.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400)
  };
})()"""


def _chat_page_tabs():
    """只认 /web/geek/chat，且必须恰好一个标签页可用。"""
    try:
        tabs = _read_json('/json/list')
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if not isinstance(tabs, list) or len(tabs) > 100:
        raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面列表无法核对。')
    choices = []
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get('url'), str):
            raise KernelError('BROWSER_CONNECTION_INVALID', '浏览器页面信息不完整。')
        page = urlparse(tab['url'])
        if page.scheme == 'https' and page.hostname == 'www.zhipin.com' and page.path in REAL_CHAT_PATHS:
            choices.append(tab)
    return choices


# 实测身份结构（2026-10-27）：
#   div.top-info-content
#   ├── div.user-info-wrap > div.user-info
#   │   ├── span.name-text      → 对方名
#   │   ├── span（无名）         → 公司
#   │   └── span.base-title     → 招聘者/HRBP
#   └── div.chat-position-content > div.position-main
#       ├── span.position-name → 职位
#       ├── span.salary        → 5-8K
#       └── span.city          → 深圳
# 注意：会话列表项的 class 点击前后不变（选中态不用 class 标记），
# 所以「当前对话是谁」只能从面板读，不能从列表推断。
REAL_TARGET_SCRIPT = """(() => {
  const root = document.querySelector('.top-info-content');
  if (!root) { return {ok: false, code: 'TARGET_PANEL_MISSING'}; }
  const text = (el) => (el && el.innerText ? el.innerText.trim() : null);
  const name = text(root.querySelector('.name-text'));
  const role = text(root.querySelector('.base-title'));
  const position = text(root.querySelector('.position-name'));
  const info = root.querySelector('.user-info');
  let company = null;
  if (info) {
    for (const span of info.querySelectorAll('span')) {
      const cls = String(span.className || '');
      if (cls.indexOf('name-text') >= 0 || cls.indexOf('base-title') >= 0) continue;
      const value = text(span);
      if (value && value !== name) { company = value; break; }
    }
  }
  return {ok: true, name: name, company: company, role: role, position: position,
          salary: text(root.querySelector('.salary')), city: text(root.querySelector('.city'))};
})()"""


def real_target():
    """读当前打开对话的身份（对方名/公司/职位）。只读，不点击、不输入。

    身份取不到时返回 TARGET_UNREADABLE，**绝不拿面板文本硬切**——
    名字含空格、公司含空格时切分会裂，裂了还拿去核对等于假核对。
    """
    # 归属由外层 handler（chat.target / chat.submit）决定，这里不做执行名校验，
    # 否则 submit 内部复用本函数会撞 require_registered。
    tabs = _chat_page_tabs()
    if tabs is None:
        return {'status': 'unavailable', 'sending_enabled': False}
    if len(tabs) != 1:
        return {'status': 'chat_page_required' if not tabs else 'page_selection_required',
                'sending_enabled': False}
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        value = _fixed_reply(connection, 1, 'Runtime.evaluate',
                             {'expression': REAL_TARGET_SCRIPT, 'returnByValue': True}
                             ).get('result', {}).get('value')
    finally:
        connection.close()
    if not isinstance(value, dict) or not isinstance(value.get('ok'), bool):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '对话身份读取结果无法核对。')
    if not value['ok']:
        return {'status': 'target_unreadable', 'code': value.get('code'),
                'sending_enabled': False}
    for key in ('name', 'company', 'role', 'position', 'salary', 'city'):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '对话身份字段类型无法核对。')
        if isinstance(value.get(key), str) and len(value[key]) > 60:
            raise KernelError('BROWSER_SCHEMA_CHANGED', '对话身份字段超出允许长度。')
    if not value.get('name') or not value.get('company'):
        return {'status': 'target_unreadable', 'code': 'NAME_OR_COMPANY_MISSING',
                'sending_enabled': False}
    return {'status': 'target_read', 'name': value['name'], 'company': value['company'],
            'role': value.get('role'), 'position': value.get('position'),
            'salary': value.get('salary'), 'city': value.get('city'),
            'sending_enabled': False, 'delivery_confirmed': False,
            'account_verified': False}


def real_open_conversation():
    """在对话页选中第一条真实会话。只读：不输入正文、不碰发送按钮。"""
    require_maintenance()
    current_execution('runtime.applicant.chat.open')
    tabs = _chat_page_tabs()
    if tabs is None:
        return {'status': 'unavailable', 'sending_enabled': False}
    if len(tabs) != 1:
        return {'status': 'chat_page_required' if not tabs else 'page_selection_required',
                'sending_enabled': False}
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        value = _fixed_reply(connection, 1, 'Runtime.evaluate',
                             {'expression': REAL_SESSION_SCRIPT, 'returnByValue': True}
                             ).get('result', {}).get('value')
    finally:
        connection.close()
    if not isinstance(value, dict) or not isinstance(value.get('ok'), bool):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '会话列表读取结果无法核对。')
    if not value['ok']:
        return {'status': 'no_conversation', 'code': value.get('code'), 'sending_enabled': False}
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        for sequence, kind in enumerate(('mouseMoved', 'mousePressed', 'mouseReleased'), start=2):
            params = {'type': kind, 'x': value['x'], 'y': value['y']}
            if kind != 'mouseMoved':
                params.update({'button': 'left', 'clickCount': 1,
                               'buttons': 1 if kind == 'mousePressed' else 0})
            _fixed_reply(connection, sequence, 'Input.dispatchMouseEvent', params,
                         side_effect_possible=True)
    except (websocket.WebSocketException, OSError):
        # 点击会让 SPA 导航，连接被对端关闭是预期行为，不是失败
        pass
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return {'status': 'open_requested', 'sessions': value['count'],
            'name': value.get('name'), 'company': value.get('company'),
            'preview': value.get('preview'), 'sending_enabled': False,
            'delivery_confirmed': False}


def real_observe(tool_name='runtime.applicant.chat.observe'):
    """读当前对话面板：输入框是否就绪、发送是否可用、送达/已读标记、面板文本。

    纯只读（不点、不写、不导航），因此**不要求 maintenance 权限**——
    要求维护权限会让「观察」变成需要提权的动作，反而鼓励把读写混在一起。

    `tool_name` 由调用方给：submit 需要在点击前读一次基线，此时能力上下文是它自己的。
    """
    current_execution(tool_name)
    tabs = _chat_page_tabs()
    if tabs is None:
        return {'status': 'unavailable', 'sending_enabled': False}
    if len(tabs) != 1:
        return {'status': 'chat_page_required' if not tabs else 'page_selection_required',
                'sending_enabled': False}
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        value = _fixed_reply(connection, 1, 'Runtime.evaluate',
                             {'expression': REAL_OBSERVE_SCRIPT, 'returnByValue': True}
                             ).get('result', {}).get('value')
    finally:
        connection.close()
    if not isinstance(value, dict) or set(value) != {
            'composer', 'can_send', 'send_disabled', 'empty', 'delivered', 'read', 'panel_text'}:
        raise KernelError('BROWSER_SCHEMA_CHANGED', '对话面板读取结果无法核对。')
    if any(type(value[key]) is not int or value[key] < 0
           for key in ('composer', 'delivered', 'read')):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '对话面板计数无法核对。')
    if not isinstance(value['panel_text'], str) or len(value['panel_text']) > MAX_OBSERVED_TEXT:
        raise KernelError('BROWSER_SCHEMA_CHANGED', '对话面板文本超出允许长度。')
    return {'status': 'observed', 'composer_ready': value['composer'] > 0,
            'can_send': value['can_send'] is True,
            'send_disabled': value['send_disabled'] is True,
            'empty': value['empty'] is True,
            'delivered_marks': value['delivered'], 'read_marks': value['read'],
            'panel_text': value['panel_text'],
            'account_verified': False, 'sending_enabled': False,
            'delivery_confirmed': False}


# ---------------------------------------------------------------- 真实写入（不可逆，谨慎）
# 输入框是 contenteditable 的 div，不是 textarea：
#   - 直接改 innerText 不会触发框架的 v-model，按钮不会解禁；
#   - 必须 focus 后用 Input.insertText 走真实输入事件。
REAL_FOCUS_SCRIPT = """(() => {
  const box = document.querySelector('div.chat-input');
  if (!box) { return {ok: false, code: 'COMPOSER_MISSING'}; }
  box.focus();
  // 显式清空再填：不依赖选区状态（新进程/失焦窗口里 selectNodeContents 不一定生效，
  // 实测会出现「旧文本 + 新文本 = 100 字」）。清空后补 input 事件让框架同步。
  box.innerHTML = '';
  box.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true,
                                             inputType: 'deleteContentBackward'}));
  return {ok: true, focused: box === document.activeElement || box.contains(document.activeElement)};
})()"""

# insertText 只改 DOM 文本，不触发框架响应式 → 发送按钮一直 disabled。
# 必须补发 input 事件，让 v-model 收到变化。
REAL_NOTIFY_SCRIPT = """(() => {
  const box = document.querySelector('div.chat-input');
  if (!box) { return {ok: false, code: 'COMPOSER_MISSING'}; }
  box.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true,
                                             inputType: 'insertText'}));
  box.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true};
})()"""

REAL_READ_SCRIPT = """(() => {
  const box = document.querySelector('div.chat-input');
  const send = document.querySelector('button.btn-send');
  const rect = send ? send.getBoundingClientRect() : null;
  return {
    text: box ? (box.innerText || '') : null,
    send_present: !!send,
    send_disabled: send ? /disabled/.test(String(send.className)) : null,
    point: rect ? {x: Math.round(rect.x + rect.width / 2),
                   y: Math.round(rect.y + rect.height / 2),
                   w: Math.round(rect.width), h: Math.round(rect.height)} : null
  };
})()"""


def _chat_connection():
    """连唯一的 chat 页，返回 (connection, None) 或 (None, status_dict)。"""
    tabs = _chat_page_tabs()
    if tabs is None:
        return None, {'status': 'unavailable', 'sending_enabled': False}
    if len(tabs) != 1:
        return None, {'status': 'chat_page_required' if not tabs else 'page_selection_required',
                      'sending_enabled': False}
    endpoint = _loopback_endpoint(tabs[0].get('webSocketDebuggerUrl'))
    return websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                       http_no_proxy=['127.0.0.1', 'localhost']), None


def real_read_composer():
    """读输入框当前内容与发送按钮状态（含按钮中心坐标）。归属同 real_target。"""
    connection, failure = _chat_connection()
    if connection is None:
        return failure
    try:
        value = _fixed_reply(connection, 1, 'Runtime.evaluate',
                             {'expression': REAL_READ_SCRIPT, 'returnByValue': True}
                             ).get('result', {}).get('value')
    finally:
        connection.close()
    if not isinstance(value, dict) or set(value) != {'text', 'send_present', 'send_disabled', 'point'}:
        raise KernelError('BROWSER_SCHEMA_CHANGED', '输入框读取结果无法核对。')
    return {'status': 'read', 'text': value['text'], 'send_present': value['send_present'],
            'send_disabled': value['send_disabled'], 'point': value['point'],
            'sending_enabled': False}


def real_fill(text):
    """把正文填进真实输入框并用真实输入事件触发。不发送。"""
    current_execution('runtime.applicant.chat.fill')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_ACTION_TEXT:
        raise KernelError('INVALID_ACTION_TEXT', '待发正文需为 1-500 字。', 400)
    connection, failure = _chat_connection()
    if connection is None:
        return failure
    try:
        focus = _fixed_reply(connection, 1, 'Runtime.evaluate',
                             {'expression': REAL_FOCUS_SCRIPT, 'returnByValue': True}
                             ).get('result', {}).get('value')
        if not isinstance(focus, dict) or not isinstance(focus.get('ok'), bool):
            raise KernelError('BROWSER_SCHEMA_CHANGED', '输入框聚焦结果无法核对。')
        if not focus['ok']:
            return {'status': 'composer_unavailable', 'code': focus.get('code'),
                    'sending_enabled': False}
        if focus.get('focused') is not True:
            return {'status': 'composer_unavailable', 'code': 'FOCUS_FAILED',
                    'sending_enabled': False}
        _fixed_reply(connection, 2, 'Input.insertText', {'text': text.strip()},
                     side_effect_possible=True)
        _fixed_reply(connection, 3, 'Runtime.evaluate',
                     {'expression': REAL_NOTIFY_SCRIPT, 'returnByValue': True},
                     side_effect_possible=True)
        value = _fixed_reply(connection, 4, 'Runtime.evaluate',
                             {'expression': REAL_READ_SCRIPT, 'returnByValue': True}
                             ).get('result', {}).get('value')
    finally:
        connection.close()
    if not isinstance(value, dict) or not isinstance(value.get('text'), str):
        raise KernelError('BROWSER_SCHEMA_CHANGED', '填入后读取结果无法核对。')
    typed = value['text'].strip()
    return {'status': 'filled', 'typed': type(typed) is str, 'length': len(typed),
            'matched': typed == text.strip(),
            'send_disabled': value.get('send_disabled'), 'point': value.get('point'),
            'sending_enabled': False}


def real_submit(point):
    """点一次真实发送按钮。只接受已知坐标，不接受外部传入的脚本。"""
    current_execution('runtime.applicant.chat.submit')
    if (not isinstance(point, dict) or set(point) != {'x', 'y', 'w', 'h'}
            or any(type(point[key]) is not int for key in ('x', 'y', 'w', 'h'))
            or not (0 < point['x'] < 20000 and 0 < point['y'] < 20000)
            or not (4 <= point['w'] <= 400 and 4 <= point['h'] <= 120)):
        raise KernelError('INVALID_BROWSER_REQUEST', '发送按钮位置无法核对。', 400)
    connection, failure = _chat_connection()
    if connection is None:
        return failure
    # 鼠标事件不等响应：点击会让页面开始处理，等响应反而会超时误判成失败。
    # 发完就返回，由调用方用 observe 核对真实结果（这就是六态里 outcome_unknown 的用法）。
    try:
        for sequence, kind in enumerate(('mouseMoved', 'mousePressed', 'mouseReleased'), start=1):
            params = {'type': kind, 'x': point['x'], 'y': point['y']}
            if kind != 'mouseMoved':
                params.update({'button': 'left', 'clickCount': 1,
                               'buttons': 1 if kind == 'mousePressed' else 0})
            connection.send(json.dumps({'id': sequence, 'method': 'Input.dispatchMouseEvent',
                                       'params': params}))
            time.sleep(0.25)
    except (websocket.WebSocketException, OSError):
        pass
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return {'status': 'submit_requested', 'sending_enabled': False,
            'delivery_confirmed': False}
