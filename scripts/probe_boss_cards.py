"""抓 BOSS 搜索页职位卡片的真实结构（只读）。

输出每张卡片的字段提取路径候选：职位名 / 公司 / 薪资 / 地点 / jobId / 详情链接。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

import websocket

CDP = 'http://127.0.0.1:9222'

CARDS_JS = r"""
(() => {
  const list = document.querySelector('ul.rec-job-list');
  if (!list) return {found: false, reason: 'ul.rec-job-list 不存在'};
  const cards = [...list.children];
  const first = cards[0];
  const shape = (el) => {
    if (!el) return null;
    const out = [];
    const walk = (node, depth) => {
      if (depth > 3 || out.length > 40) return;
      for (const child of node.children) {
        out.push({d: depth, tag: child.tagName.toLowerCase(),
          cls: (typeof child.className === 'string' ? child.className : '').slice(0, 90),
          text: (child.innerText || '').trim().slice(0, 60)});
        walk(child, depth + 1);
      }
    };
    walk(el, 0);
    return out;
  };
  return {
    found: true, count: cards.length,
    firstTag: first ? first.tagName.toLowerCase() : null,
    firstClass: first ? (typeof first.className === 'string' ? first.className : '') : null,
    firstOuter: first ? first.outerHTML.slice(0, 1200) : null,
    firstShape: shape(first),
    links: first ? [...first.querySelectorAll('a')].map(a => ({
      cls: (typeof a.className === 'string' ? a.className : '').slice(0, 70),
      href: a.getAttribute('href'), text: (a.innerText || '').trim().slice(0, 30)})).slice(0, 8) : [],
  };
})()
"""


def page_targets():
    with urllib.request.urlopen(CDP + '/json/list', timeout=5) as resp:
        return [t for t in json.loads(resp.read().decode()) if t.get('type') == 'page']


def evaluate(ws_url, expression, timeout=30):
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.enable'}))
        ws.send(json.dumps({'id': 2, 'method': 'Runtime.evaluate',
                            'params': {'expression': expression, 'returnByValue': True}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get('id') == 2:
                if 'error' in msg:
                    raise RuntimeError(msg['error'])
                return msg['result']['result'].get('value')
        raise TimeoutError('evaluate timeout')
    finally:
        ws.close()


def main():
    targets = [t for t in page_targets() if '/web/geek/job' in t['url']]
    if not targets:
        url = 'https://www.zhipin.com/web/geek/job?query=Python&city=101280100'
        req = urllib.request.Request(CDP + '/json/new?' + urllib.parse.quote(url, safe=''),
                                     method='PUT')
        with urllib.request.urlopen(req, timeout=10) as resp:
            target = json.loads(resp.read().decode())
        time.sleep(8)
        targets = [t for t in page_targets() if t.get('id') == target['id']]
    if not targets:
        print('FAIL 拿不到搜索页 target')
        return 2
    result = evaluate(targets[0]['webSocketDebuggerUrl'], CARDS_JS)
    with open('tmp/dom-snapshots/boss-cards.json', 'w', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    if not result.get('found'):
        print('FAIL', result.get('reason'))
        return 2
    print('cards:', result['count'], '| first:', result['firstTag'], result['firstClass'])
    print('--- first card shape (depth<=2) ---')
    for row in result['firstShape']:
        if row['d'] <= 2:
            print('  ' + '  ' * row['d'] + f"{row['tag']}.{row['cls'][:40]} :: {row['text'][:40]!r}")
    print('--- links ---')
    for link in result['links']:
        print('  ', link['cls'][:40], link['href'], repr(link['text']))
    return 0


if __name__ == '__main__':
    import os
    os.makedirs('tmp/dom-snapshots', exist_ok=True)
    sys.exit(main())
