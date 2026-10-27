"""G3 三场景受控验证：正常回显 / 提交后无回显 / 提交后页面失联。

本地回环服务 + 本机 Edge 专用临时 profile，**不访问 zhipin 或任何平台**。
每个场景走真实内核路径：登记一次性授权 → 执行一次（填入选提交）→ 只读观察 → 六态。

故障场景通过受控页的固定 query（脚本内硬编码，不是外部输入）打开，
生产路径的 controlled_open 永远只开无参数地址。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import applicant_browser
import main
import store
from sandbox import maintenance_access

PORT = 18770
BASE = 'http://127.0.0.1:%d/controlled-chat' % PORT
# (场景, 固定地址, 期望六态, 执行后己方消息应有条数)
SCENARIOS = [
    ('normal', BASE, 'observed_reply', 1),
    ('no-echo', BASE + '?fail=submit', 'submitted_unverified', 0),
    ('crash', BASE + '?fail=crash', 'outcome_unknown', 0),
    ('history', BASE + '?preset=mine', 'observed_reply', 2),
    ('wrong-target', BASE + '?job=other-job&company=%E5%85%B6%E4%BB%96%E5%85%AC%E5%8F%B8',
     'reject:CHANNEL_TARGET_CHANGED', None),
]
TEXT = '受控验证：您好，我是受控页面上的测试消息。'
RESULTS = []


def note(step, payload):
    RESULTS.append({'step': step, 'payload': payload})
    print('[%s] %s' % (step, json.dumps(payload, ensure_ascii=False)))


def wait_for(url, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def controlled_tabs():
    with urllib.request.urlopen('http://127.0.0.1:9222/json/list', timeout=3) as resp:
        tabs = json.loads(resp.read().decode('utf-8'))
    return [t for t in tabs if isinstance(t, dict) and '/controlled-chat' in str(t.get('url', ''))]


def close_controlled_tabs():
    for tab in controlled_tabs():
        try:
            urllib.request.urlopen('http://127.0.0.1:9222/json/close/' + tab['id'], timeout=3).read()
        except Exception:
            pass
    time.sleep(0.5)


def open_fixed(url):
    """用固定回环地址建一个受控页标签（脚本内硬编码的三种场景地址之一）。"""
    import websocket
    endpoint, _ = applicant_browser._browser_endpoint()
    connection = websocket.create_connection(endpoint, timeout=3, suppress_origin=True,
                                             http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        reply = applicant_browser._fixed_reply(connection, 1, 'Target.createTarget',
                                               {'url': url, 'background': True},
                                               side_effect_possible=True)
        return reply.get('targetId')
    finally:
        connection.close()


def run_scenario(name, url, expected, expected_count=None):
    close_controlled_tabs()
    target = open_fixed(url)
    time.sleep(1.2)
    with store.runtime_db() as c:
        context = store.runtime_context(c)
    token = context['context_token']
    # 每个场景一份新授权（一次性，用完即废）
    with maintenance_access():
        main.dispatch('runtime.applicant.action.authorize', {
            'action': 'greeting.submit', 'account_evidence': 'controlled:local',
            'job_id': 'controlled-job-1', 'company': '受控公司', 'text': TEXT}, token)
        with mock.patch.object(applicant_browser, '_require_browser_owner', lambda *a, **k: None):
            try:
                result = main.dispatch('runtime.applicant.action.execute', {
                    'action': 'greeting.submit', 'text': TEXT, 'confirmed': True}, token)
            except Exception as exc:  # noqa: BLE001 - 要把真实异常报出来
                code = getattr(exc, 'code', None)
                reject_ok = str(expected).startswith('reject:') and str(expected).split(':', 1)[1] == code
                note('scenario', {'name': name, 'expected': expected, 'rejected_with': code,
                                  'ok': reject_ok, 'target': target})
                return {'scenario': name, 'expected': expected, 'state': 'rejected:' + str(code),
                        'ok': reject_ok}
        view = main.dispatch('runtime.applicant.action.get', {'include_authorization': True}, token)
    data = result.get('data') if isinstance(result, dict) else None
    state = (data or {}).get('state')
    ok = state == expected
    note('scenario', {'name': name, 'expected': expected, 'state': state, 'ok': ok,
                      'target': target, 'delivery_confirmed': (data or {}).get('delivery_confirmed'),
                      'retryable': (data or {}).get('retryable'),
                      'needs_human': (data or {}).get('needs_human'),
                      'authorization_status': ((view.get('data') or {}).get('authorization') or {}).get('status')})
    return {'scenario': name, 'expected': expected, 'state': state, 'ok': ok}


def main_run():
    service = edge = None
    try:
        service = subprocess.Popen([sys.executable, 'main.py', 'serve', '--host', '127.0.0.1',
                                    '--port', str(PORT)],
                                   cwd=str(Path(__file__).resolve().parent.parent),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        if not wait_for('http://127.0.0.1:%d/health' % PORT):
            note('service', {'ok': False})
            return 1
        note('service', {'ok': True})

        profile = Path(store.DB_PATH).resolve().parent / '.jobkit-browsers' / 'controlled-verify'
        profile.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if k not in {'JOBKIT_LLM_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'}}
        edge = subprocess.Popen([str(applicant_browser._edge_executable()),
                                 '--user-data-dir=' + str(profile),
                                 '--remote-debugging-port=9222',
                                 '--remote-debugging-address=127.0.0.1',
                                 '--no-first-run', '--no-default-browser-check',
                                 '--new-window', 'about:blank'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env)
        if not wait_for('http://127.0.0.1:9222/json/version'):
            note('edge', {'ok': False})
            return 2
        note('edge', {'ok': True})

        outcomes = [run_scenario(name, url, expected, count)
                    for name, url, expected, count in SCENARIOS]
        summary = {'all_ok': all(o['ok'] for o in outcomes), 'outcomes': outcomes}
        note('summary', summary)
        return 0 if summary['all_ok'] else 4
    finally:
        for proc in (edge, service):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        Path(__file__).with_name('controlled_scenarios_result.json').write_text(
            json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main_run())
