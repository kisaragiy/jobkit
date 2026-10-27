"""用 CDP 真实鼠标事件打开一条会话，抓 composer（只读：不输入、不发送）。

JS 的 el.click() 触发不到 Vue 的会话选中逻辑（右侧一直是 chat-no-data），
必须走 Input.dispatchMouseEvent 模拟真实按下/抬起。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

import websocket

CDP = 'http://127.0.0.1:9222'

RECT_JS = r"""
(() => {
  const box = document.querySelector('.user-list-content');
  if (!box) return null;
  const items = [...box.querySelectorAll('li')].filter(li => li.getBoundingClientRect().height > 30);
  const li = items[1] || items[0];              // 优先第二条，第一条常是「已发送简历」系统会话
  if (!li) return null;
  // 实测：点 li 中心/avatar 区不响应；必须点 .friend-content 的水平 35% 处
  const fc = li.querySelector('.friend-content') || li;
  const r = fc.getBoundingClientRect();
  return {x: Math.round(r.x + r.width * 0.35), y: Math.round(r.y + r.height / 2),
          cls: (typeof fc.className === 'string' ? fc.className : '').slice(0, 60),
          text: (li.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60)};
})()
"""

COMPOSER_JS = r"""
(() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 40 && r.height > 10; };
  const info = el => ({tag: el.tagName.toLowerCase(),
    cls: (typeof el.className === 'string' ? el.className : '').slice(0, 90),
    ph: el.getAttribute('placeholder'),
    editable: el.isContentEditable === true,
    box: (r => ({x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}))(el.getBoundingClientRect())});
  const panel = document.querySelector('.chat-conversation');
  const empty = document.querySelector('.chat-no-data');
  const editors = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .filter(vis).map(info);
  const senders = [...document.querySelectorAll('[class*="send"],[class*="submit"]')]
    .filter(vis).slice(0, 12).map(el => Object.assign(info(el), {text: (el.innerText || '').trim().slice(0, 16)}));
  const msgs = [...document.querySelectorAll('.message-status')].slice(0, 4)
    .map(el => ({cls: (typeof el.className === 'string' ? el.className : ''), text: (el.innerText || '').trim()}));
  const head = document.querySelector('[class*="chat-title"],[class*="chat-header"],[class*="conversation-title"]');
  return {panelText: panel ? (panel.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120) : null,
          stillEmpty: !!empty,
          header: head ? {cls: String(head.className).slice(0, 70), text: (head.innerText || '').slice(0, 50)} : null,
          editors, senders, statuses: msgs};
})()
"""


def pages():
    with urllib.request.urlopen(CDP + '/json/list', timeout=5) as resp:
        return [t for t in json.loads(resp.read().decode()) if t.get('type') == 'page']


def ev(ws, expr, rid, timeout=20):
    ws.send(json.dumps({'id': rid, 'method': 'Runtime.evaluate',
                        'params': {'expression': expr, 'returnByValue': True}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(ws.recv())
        if msg.get('id') == rid:
            if 'error' in msg:
                raise RuntimeError(msg['error'])
            return msg['result']['result'].get('value')
    raise TimeoutError('eval timeout')


def ensure_tab():
    targets = [t for t in pages() if '/web/geek/chat' in t['url']]
    if targets:
        return targets[0]
    url = 'https://www.zhipin.com/web/geek/chat'
    req = urllib.request.Request(CDP + '/json/new?' + urllib.parse.quote(url, safe=''), method='PUT')
    with urllib.request.urlopen(req, timeout=10) as resp:
        fresh = json.loads(resp.read().decode())
    time.sleep(9)
    found = [t for t in pages() if t.get('id') == fresh['id']]
    return found[0] if found else None


def main():
    target = ensure_tab()
    if not target:
        print('FAIL 没有 chat 页面')
        return 2
    ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=30,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({'id': 0, 'method': 'Runtime.enable'}))
        rect = ev(ws, RECT_JS, 1)
        if not rect:
            print('FAIL 没有会话项')
            return 2
        print('目标会话:', rect['text'], '中心坐标', rect['x'], rect['y'])
        for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            params = {'type': kind, 'x': rect['x'], 'y': rect['y']}
            if kind != 'mouseMoved':
                params.update({'button': 'left', 'clickCount': 1,
                               'buttons': 1 if kind == 'mousePressed' else 0})
            ws.send(json.dumps({'id': 90, 'method': 'Input.dispatchMouseEvent',
                                'params': params}))
            time.sleep(0.25)
        time.sleep(0.8)               # 点击会触发 SPA 导航，原 target 必然断开
    finally:
        try:
            ws.close()
        except Exception:
            pass
    time.sleep(4.5)
    # 只认 /web/geek/chat 这个路径，且必须真的拿到 editors。
    # 踩过的坑：按 URL 长度排序时 /web/geek/jobs 排前面，而它 stillEmpty=false，
    # 于是被误判成「命中」直接 break —— 读到的是岗位列表页，不是对话。
    candidates = [t for t in pages() if '/web/geek/chat' in t['url']]
    if not candidates:
        print('候选 tab:', [t['url'][:60] for t in pages() if 'zhipin' in t['url']])
    composer = None
    for target in candidates:
        try:
            ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=20,
                                             suppress_origin=True)
        except Exception:
            continue
        try:
            ws.send(json.dumps({'id': 0, 'method': 'Runtime.enable'}))
            probe = ev(ws, COMPOSER_JS, 2)
        except Exception:
            probe = None
        finally:
            try:
                ws.close()
            except Exception:
                pass
        print('tab %-58s empty=%s editors=%d' % (target['url'][:56],
              (probe or {}).get('stillEmpty'), len((probe or {}).get('editors') or [])))
        if probe and probe.get('editors'):
            composer = probe
            print('>>> 命中:', target['url'][:80])
            break
    if composer is None:
        print('FAIL 没有可用 tab')
        return 2
    with open('tmp/dom-snapshots/boss-chat-composer.json', 'w', encoding='utf-8') as fh:
        json.dump(composer, fh, ensure_ascii=False, indent=2)
    print('stillEmpty:', composer['stillEmpty'])
    print('panelText:', composer['panelText'])
    print('header:', composer['header'])
    print('--- editors ---')
    for e in composer['editors']:
        print(' ', e)
    print('--- senders ---')
    for e in composer['senders']:
        print(' ', e)
    print('--- statuses ---')
    for e in composer['statuses']:
        print(' ', e)
    return 0


if __name__ == '__main__':
    import os
    os.makedirs('tmp/dom-snapshots', exist_ok=True)
    sys.exit(main())
