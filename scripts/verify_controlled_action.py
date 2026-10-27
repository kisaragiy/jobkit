"""受控页面真实 CDP 验证（本地，绝不访问平台）。

流程：起本地服务 → 起本机 Edge（专用临时 profile，about:blank）→ 打开受控页
→ 填入 → 提交 → 只读观察 → 断言六态 → 清理进程。

生产路径仍要求 runtime.applicant.browser.open 的完成记录；本脚本只临时跳过那条
owner 检查（受控页不是平台页面），其余授权/单次性/六态全部走真实代码。
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
import sandbox
import store
from sandbox import capability, maintenance_access

PORT = 18770
RESULTS = []


def log(name, payload):
    RESULTS.append({'step': name, 'payload': payload})
    print('[%s] %s' % (name, json.dumps(payload, ensure_ascii=False)))


def wait_http(url, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def wait_cdp(timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def start_service():
    python = sys.executable
    return subprocess.Popen([python, 'main.py', 'serve', '--host', '127.0.0.1', '--port', str(PORT)],
                            cwd=str(Path(__file__).resolve().parent.parent),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def start_edge():
    edge = applicant_browser._edge_executable()
    profile = Path(store.DB_PATH).resolve().parent / '.jobkit-browsers' / 'controlled-verify'
    profile.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in {'JOBKIT_LLM_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'}}
    return subprocess.Popen([str(edge), '--user-data-dir=' + str(profile),
                             '--remote-debugging-port=9222', '--remote-debugging-address=127.0.0.1',
                             '--no-first-run', '--no-default-browser-check', '--new-window',
                             'about:blank'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)


def main_run():
    service = edge = None
    try:
        service = start_service()
        if not wait_http('http://127.0.0.1:%d/health' % PORT):
            log('service', {'ok': False, 'reason': 'health timeout'})
            return 1
        log('service', {'ok': True})

        with urllib.request.urlopen('http://127.0.0.1:%d/controlled-chat' % PORT, timeout=5) as resp:
            page = resp.read().decode('utf-8')
        log('page', {'ok': 'compose-textarea' in page and 'send-button' in page,
                     'bytes': len(page)})

        edge = start_edge()
        if not wait_cdp():
            log('edge', {'ok': False, 'reason': 'cdp timeout'})
            return 2
        log('edge', {'ok': True})

        with store.runtime_db() as c:
            context = store.runtime_context(c)
        tool = main._router.resolve('runtime.applicant.action.execute')
        context = dict(context, task_id='controlled-verify')

        with mock.patch.object(applicant_browser, '_require_browser_owner', lambda *a, **k: None), \
                capability(tool, context), maintenance_access():
            opened = applicant_browser.controlled_open()
            log('open', opened)
            if opened.get('status') not in ('controlled_page_opened', 'already_open'):
                return 3
            time.sleep(1.2)

            text = '受控验证：您好，我是受控页面上的测试消息。'
            filled = applicant_browser.controlled_fill(text)
            log('fill', filled)

            before = applicant_browser.controlled_observe()
            log('observe_before', before)

            submitted = applicant_browser.controlled_submit()
            log('submit', submitted)

            seen = None
            deadline = time.time() + 5
            while time.time() < deadline:
                seen = applicant_browser.controlled_observe()
                if seen.get('count', 0) > before.get('count', 0):
                    break
                time.sleep(0.3)
            log('observe_after', seen)

            ok = (filled.get('status') == 'filled' and submitted.get('status') == 'submitted'
                  and (seen or {}).get('count', 0) == before.get('count', 0) + 1
                  and (seen or {}).get('last') == text)
            log('verdict', {'ok': bool(ok), 'echo_matches': (seen or {}).get('last') == text,
                            'count_delta': (seen or {}).get('count', 0) - before.get('count', 0)})
            return 0 if ok else 4
    finally:
        for proc in (edge, service):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        Path(__file__).with_name('controlled_verify_result.json').write_text(
            json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main_run())
