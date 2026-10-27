"""只读 DOM 探针：连本机 Edge 的 CDP，打开 BOSS 页面并抓结构快照。

红线：**只做 Runtime.evaluate 读 DOM**，不 click、不填表、不提交、不导航到会触发动作的地址。
"""
import json
import sys
import time
import urllib.request

import websocket

CDP = 'http://127.0.0.1:9222'

PROBE_JS = r"""
(() => {
  const box = (el) => { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const pick = (el) => ({tag: el.tagName.toLowerCase(), id: el.id || null,
    cls: (typeof el.className === 'string' ? el.className : '').slice(0, 160),
    name: el.getAttribute('name'), type: el.getAttribute('type'),
    placeholder: el.getAttribute('placeholder'), editable: el.isContentEditable === true,
    text: (el.innerText || el.value || '').trim().slice(0, 100), box: box(el)});
  const containers = [...document.querySelectorAll('ul,ol,div,section')]
    .filter((el) => el.children.length > 3)
    .slice(0, 30)
    .map((el) => ({tag: el.tagName.toLowerCase(), id: el.id || null,
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 140),
      children: el.children.length, box: box(el)}));
  return {
    url: location.href, title: document.title,
    inputs: [...document.querySelectorAll('input,textarea,[contenteditable="true"]')].map(pick),
    buttons: [...document.querySelectorAll('button,[class*="send"],[class*="btn"]')].slice(0, 50).map(pick),
    containers: containers,
    bodyChars: document.body ? document.body.innerText.length : 0,
    loginWall: /扫码登录|登录后查看|请登录/.test((document.body ? document.body.innerText : '').slice(0, 600))
  };
})()
"""


def cdp_targets():
    with urllib.request.urlopen(CDP + '/json/list', timeout=5) as resp:
        return json.loads(resp.read().decode())


def open_page(url):
    req = urllib.request.Request(CDP + '/json/new?' + urllib.parse.quote(url, safe=''), method='PUT')
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def evaluate(ws_url, expression, timeout=30):
    # Edge 148+ 会拒绝带 Origin 的 CDP 连接（403），压掉 Origin 即可，无需重启浏览器
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.enable'}))
        ws.send(json.dumps({'id': 2, 'method': 'Runtime.evaluate',
                            'params': {'expression': expression, 'returnByValue': True,
                                       'awaitPromise': True}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(ws.recv())
            if message.get('id') == 2:
                if 'error' in message:
                    raise RuntimeError(message['error'])
                result = message['result']['result']
                return result.get('value')
        raise TimeoutError('evaluate timeout')
    finally:
        ws.close()


def snapshot(url, wait=6.0):
    target = open_page(url)
    time.sleep(wait)
    targets = [t for t in cdp_targets() if t.get('id') == target['id']]
    ws_url = targets[0]['webSocketDebuggerUrl'] if targets else target['webSocketDebuggerUrl']
    return evaluate(ws_url, PROBE_JS)


def main():
    urls = sys.argv[1:] or ['https://www.zhipin.com/web/geek/chat']
    out = {}
    for url in urls:
        try:
            data = snapshot(url)
            out[url] = {'ok': True, 'data': data}
            print('OK   %s  title=%r inputs=%d buttons=%d containers=%d loginWall=%s'
                  % (url, (data or {}).get('title'), len((data or {}).get('inputs') or []),
                     len((data or {}).get('buttons') or []),
                     len((data or {}).get('containers') or []), (data or {}).get('loginWall')))
        except Exception as exc:  # noqa: BLE001 - 探针要把失败如实带出来
            out[url] = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
            print('FAIL %s  %s: %s' % (url, type(exc).__name__, exc))
    with open('tmp/dom-snapshots/boss-probe.json', 'w', encoding='utf-8') as handle:
        json.dump(out, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == '__main__':
    import os
    import urllib.parse  # noqa: F401 - used by open_page
    os.makedirs('tmp/dom-snapshots', exist_ok=True)
    sys.exit(main())
