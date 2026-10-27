"""抓「当前选中会话」的身份结构（只读）。

目标三个问题：
1. 会话列表里选中项与未选中项在 class 上的差别是什么（选中态标记）；
2. .chat-conversation 内部的头部容器结构（对方名 / 公司 / 职位在哪一层）；
3. 能不能可靠地取到身份字段（取不到就明说 UNREADABLE，不许拿面板文本硬切）。

红线：只读 DOM，不输入、不点发送、不提交。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

import websocket

CDP = 'http://127.0.0.1:9222'

BEFORE_JS = r"""
(() => {
  const list = document.querySelector('.user-list-content');
  if (!list) return null;
  const items = [...list.querySelectorAll('li')].filter(li => li.getBoundingClientRect().height > 30);
  const li = items[1] || items[0];
  if (!li) return null;
  const area = li.querySelector('.friend-content') || li;
  const r = area.getBoundingClientRect();
  return {
    count: items.length,
    x: Math.round(r.x + r.width * 0.35), y: Math.round(r.y + r.height / 2),
    itemClasses: items.slice(0, 5).map((el, i) => ({i, cls: String(el.className),
      inner: (el.firstElementChild ? String(el.firstElementChild.className) : '')})),
    target: {name: (li.querySelector('.name-text') || {}).innerText || null,
             company: (li.querySelector('.title-box span:nth-child(2)') || {}).innerText || null}
  };
})()
"""

AFTER_JS = r"""
(() => {
  const outline = (el, depth, limit) => {
    if (!el || depth > limit) return [];
    const rows = [];
    for (const child of el.children) {
      rows.push({d: depth, tag: child.tagName.toLowerCase(),
        cls: (typeof child.className === 'string' ? child.className : '').slice(0, 90),
        text: (child.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50)});
      rows.push(...outline(child, depth + 1, limit));
      if (rows.length > 60) break;
    }
    return rows;
  };
  const list = document.querySelector('.user-list-content');
  const items = list ? [...list.querySelectorAll('li')].filter(li => li.getBoundingClientRect().height > 30) : [];
  const panel = document.querySelector('.chat-conversation');
  // 候选身份节点：面板内所有短文本元素，交给调用方人工判断哪个是名字/公司
  const shortText = panel ? [...panel.querySelectorAll('*')]
    .filter(el => el.children.length === 0)
    .map(el => ({cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60),
                 tag: el.tagName.toLowerCase(),
                 text: (el.innerText || '').trim().slice(0, 40)}))
    .filter(row => row.text).slice(0, 25) : [];
  return {
    itemClasses: items.slice(0, 5).map((el, i) => ({i, cls: String(el.className),
      inner: (el.firstElementChild ? String(el.firstElementChild.className) : '')})),
    panelOutline: panel ? outline(panel, 0, 3).slice(0, 40) : [],
    shortTextInPanel: shortText,
    panelText: panel ? (panel.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200) : null
  };
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


def connect(tab):
    ws = websocket.create_connection(tab['webSocketDebuggerUrl'], timeout=25, suppress_origin=True)
    ws.send(json.dumps({'id': 0, 'method': 'Runtime.enable'}))
    return ws


def chat_tab():
    found = [t for t in pages() if '/web/geek/chat' in t['url']]
    if found:
        return found[0]
    url = 'https://www.zhipin.com/web/geek/chat'
    req = urllib.request.Request(CDP + '/json/new?' + urllib.parse.quote(url, safe=''), method='PUT')
    with urllib.request.urlopen(req, timeout=10) as resp:
        fresh = json.loads(resp.read().decode())
    time.sleep(9)
    found = [t for t in pages() if t.get('id') == fresh['id']]
    return found[0] if found else None


def main():
    tab = chat_tab()
    if not tab:
        print('FAIL 没有 chat tab')
        return 2
    ws = connect(tab)
    try:
        before = ev(ws, BEFORE_JS, 1)
        if not before:
            print('FAIL 没有会话项')
            return 2
        print('=== 点击前 ===')
        print('会话数:', before['count'], '| 目标:', before['target'])
        print('前 5 项 class:', json.dumps(before['itemClasses'], ensure_ascii=False))
        for payload in (
            {'id': 10, 'method': 'Input.dispatchMouseEvent',
             'params': {'type': 'mouseMoved', 'x': before['x'], 'y': before['y']}},
            {'id': 11, 'method': 'Input.dispatchMouseEvent',
             'params': {'type': 'mousePressed', 'x': before['x'], 'y': before['y'],
                        'button': 'left', 'buttons': 1, 'clickCount': 1}},
            {'id': 12, 'method': 'Input.dispatchMouseEvent',
             'params': {'type': 'mouseReleased', 'x': before['x'], 'y': before['y'],
                        'button': 'left', 'buttons': 0, 'clickCount': 1}},
        ):
            ws.send(json.dumps(payload))
            time.sleep(0.25)
        time.sleep(1.5)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    time.sleep(4.5)
    tab = [t for t in pages() if '/web/geek/chat' in t['url']]
    if not tab:
        print('FAIL 点击后 chat tab 消失')
        return 2
    ws = connect(tab[0])
    try:
        after = ev(ws, AFTER_JS, 1)
    finally:
        ws.close()
    with open('tmp/dom-snapshots/boss-identity.json', 'w', encoding='utf-8') as fh:
        json.dump({'before': before, 'after': after}, fh, ensure_ascii=False, indent=2)

    print('\n=== 点击后 ===')
    print('前 5 项 class:', json.dumps(after['itemClasses'], ensure_ascii=False))
    print('\n--- 选中态差异 ---')
    for b, a in zip(before['itemClasses'], after['itemClasses']):
        if b['cls'] != a['cls'] or b['inner'] != a['inner']:
            print('  idx %d: %r -> %r | inner %r -> %r'
                  % (b['i'], b['cls'], a['cls'], b['inner'], a['inner']))
    print('\n--- 面板结构（深度<=2）---')
    for row in after['panelOutline']:
        if row['d'] <= 2:
            print('  ' + '  ' * row['d'] + '%s.%s :: %r' % (row['tag'], row['cls'][:45], row['text']))
    print('\n--- 面板内短文本节点（身份候选）---')
    for row in after['shortTextInPanel'][:15]:
        print('  %s.%s :: %r' % (row['tag'], row['cls'][:45], row['text']))
    print('\n--- panelText ---')
    print(' ', after['panelText'])
    return 0


if __name__ == '__main__':
    import os
    os.makedirs('tmp/dom-snapshots', exist_ok=True)
    sys.exit(main())
