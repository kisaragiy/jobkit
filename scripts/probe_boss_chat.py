"""打开一条会话，抓真实 composer 结构（只读：不输入、不点击发送）。

安全设计：
- 只点击「会话列表项」，且该项文本里不能出现「沟通/投递/发送/发送简历」等动作词；
- 全程不写任何正文，不碰发送按钮。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

import websocket

CDP = 'http://127.0.0.1:9222'
DANGER = ('沟通', '投递', '发送', '举报', '删除')

LIST_JS = r"""
(() => {
  const box = document.querySelector('.user-list-content');
  if (!box) return {found: false};
  const items = [...box.querySelectorAll('li')].filter(li => li.getBoundingClientRect().height > 30);
  return {found: true, count: items.length,
    items: items.slice(0, 6).map((li, i) => ({
      i, cls: (typeof li.className === 'string' ? li.className : '').slice(0, 70),
      text: (li.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 70),
      box: (r => ({x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}))(li.getBoundingClientRect())
    }))};
})()
"""

CLICK_JS = r"""
(() => {
  const box = document.querySelector('.user-list-content');
  if (!box) return {clicked: false, reason: 'no list'};
  const items = [...box.querySelectorAll('li')].filter(li => li.getBoundingClientRect().height > 30);
  if (!items.length) return {clicked: false, reason: 'empty'};
  const target = items[0];
  const text = (target.innerText || '').slice(0, 100);
  // 会话项本身只是选中对话（只读）；但如果项内嵌了可点按钮，就有误触风险，一律不点。
  const actionable = target.querySelector('a,button,[class*="btn"],[class*="send"]');
  if (actionable) return {clicked: false, reason: 'has actionable child: ' +
    (typeof actionable.className === 'string' ? actionable.className : actionable.tagName)};
  target.click();
  return {clicked: true, text: text.replace(/\s+/g, ' ').trim().slice(0, 60)};
})()
"""

COMPOSER_JS = r"""
(() => {
  const pick = (el) => ({tag: el.tagName.toLowerCase(),
    cls: (typeof el.className === 'string' ? el.className : '').slice(0, 90),
    ph: el.getAttribute('placeholder'),
    editable: el.isContentEditable === true,
    box: (r => ({x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}))(el.getBoundingClientRect())});
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 20 && r.height > 10; };
  const editors = [...document.querySelectorAll('textarea,[contenteditable="true"],div[class*="input"]')]
    .filter(visible).map(pick);
  const sendish = [...document.querySelectorAll('[class*="send"],[class*="submit"],div[class*="btn"],button,span[class*="send"]')]
    .filter(visible).slice(0, 20).map((el) => Object.assign(pick(el), {text: (el.innerText || '').trim().slice(0, 20)}));
  const bubbles = [...document.querySelectorAll('[class*="message"],[class*="chat-item"],[class*="msg"]')]
    .filter(visible).slice(0, 8).map(pick);
  const header = document.querySelector('[class*="chat-header"],[class*="conversation-title"],[class*="job-name"]');
  return {url: location.href,
    header: header ? {cls: String(header.className).slice(0, 70), text: (header.innerText || '').slice(0, 60)} : null,
    editors, sendish, bubbles};
})()
"""


def pages():
    with urllib.request.urlopen(CDP + '/json/list', timeout=5) as resp:
        return [t for t in json.loads(resp.read().decode()) if t.get('type') == 'page']


def ev(ws, expr, rid):
    ws.send(json.dumps({'id': rid, 'method': 'Runtime.evaluate',
                        'params': {'expression': expr, 'returnByValue': True}}))
    deadline = time.time() + 20
    while time.time() < deadline:
        msg = json.loads(ws.recv())
        if msg.get('id') == rid:
            if 'error' in msg:
                raise RuntimeError(msg['error'])
            return msg['result']['result'].get('value')
    raise TimeoutError('eval timeout')


def main():
    targets = [t for t in pages() if '/web/geek/chat' in t['url']]
    if not targets:
        url = 'https://www.zhipin.com/web/geek/chat'
        req = urllib.request.Request(CDP + '/json/new?' + urllib.parse.quote(url, safe=''),
                                     method='PUT')
        with urllib.request.urlopen(req, timeout=10) as resp:
            fresh = json.loads(resp.read().decode())
        time.sleep(8)
        targets = [t for t in pages() if t.get('id') == fresh['id']]
    if not targets:
        print('FAIL 拿不到 chat 页面')
        return 2
    ws = websocket.create_connection(targets[0]['webSocketDebuggerUrl'], timeout=30,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({'id': 0, 'method': 'Runtime.enable'}))
        listing = ev(ws, LIST_JS, 1)
        print('会话列表:', json.dumps(listing, ensure_ascii=False)[:600])
        if not listing.get('found') or not listing.get('count'):
            print('没有会话可打开')
            return 2
        clicked = ev(ws, CLICK_JS, 2)
        print('点击:', clicked)
        if not clicked.get('clicked'):
            return 2
    finally:
        ws.close()
    # 点击会让 SPA 路由/重载，原 CDP target 会断（10054）→ 重连后再读 composer
    time.sleep(3.0)
    targets = [t for t in pages() if '/web/geek/chat' in t['url']]
    if not targets:
        print('FAIL 点击后找不到 chat 页面')
        return 2
    ws = websocket.create_connection(targets[0]['webSocketDebuggerUrl'], timeout=30,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({'id': 0, 'method': 'Runtime.enable'}))
        composer = ev(ws, COMPOSER_JS, 3)
        with open('tmp/dom-snapshots/boss-chat-composer.json', 'w', encoding='utf-8') as fh:
            json.dump(composer, fh, ensure_ascii=False, indent=2)
        print('--- header ---')
        print(' ', composer.get('header'))
        print('--- editors ---')
        for e in composer.get('editors') or []:
            print(' ', e)
        print('--- send-ish ---')
        for e in composer.get('sendish') or []:
            print(' ', e)
        print('--- bubbles (前 3) ---')
        for e in (composer.get('bubbles') or [])[:3]:
            print(' ', e)
    finally:
        ws.close()
    return 0


if __name__ == '__main__':
    import os
    os.makedirs('tmp/dom-snapshots', exist_ok=True)
    sys.exit(main())
