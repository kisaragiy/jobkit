'use strict';
/*
 * dom/detail_stub_harness.js — detail 入口的「可执行桩 DOM」（离线单测用）
 *
 * 为什么要有它
 *   纯字符串断言只能证明「选择器还写在文件里」，证明不了「职位描述段落能被切出来、
 *   公司介绍不会串进 JD、选择器全丢时不会拿空串冒充成功」。这里手搓一个最小假 DOM
 *   （只实现详情分支真正调用到的 API），用 node:vm 跑 Python 侧渲染出来的那一份 JS，
 *   喂手工构造的 DOM 结构，拿回真快照。
 *
 * 用法
 *   node dom/detail_stub_harness.js <case>   < 渲染后的 boss_extract.js
 *     stdin  = boss_selectors.extract_js('detail') 的结果（保证测的是真模板）
 *     stdout = 快照 JSON
 *     退出码 2 = 用例名不认识；3 = 脚本没返回字符串；其余非 0 = 桩本身崩了
 *
 * 不联网、不启动浏览器、只需要 node 标准库。
 */
const vm = require('vm');
const fs = require('fs');

const BLOCK_TAGS = new Set([
  'div', 'section', 'article', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'ul', 'ol', 'li', 'header', 'footer', 'label', 'main', 'aside', 'dl', 'dt', 'dd', 'body',
]);

function el(tag, attrs, ...kids) {
  return new El(tag, attrs, kids);
}

class El {
  constructor(tag, attrs, kids) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = attrs || {};
    this.className = this.attrs.class || this.attrs.className || '';
    this.id = this.attrs.id || '';
    this.children = [];
    this.parentElement = null;
    this._text = null;
    for (const kid of kids || []) {
      if (kid === null || kid === undefined || kid === '') continue;
      if (kid instanceof El) {
        kid.parentElement = this;
        this.children.push(kid);
      } else {
        const leaf = new El('#text', {}, []);
        leaf._text = String(kid);
        leaf.parentElement = this;
        this.children.push(leaf);
      }
    }
  }

  // 真实 innerText：块级元素之间补换行（JD 正文的分段全靠这个）
  get innerText() {
    if (this._text !== null) return this._text;
    const sep = BLOCK_TAGS.has(this.tagName.toLowerCase()) ? '\n' : '';
    return this.children.map((c) => c.innerText).filter((s) => s !== '').join(sep);
  }

  get textContent() {
    return this.innerText;
  }

  get nextElementSibling() {
    const p = this.parentElement;
    if (!p) return null;
    let i = p.children.indexOf(this) + 1;
    while (i < p.children.length) {
      if (p.children[i].tagName !== '#TEXT') return p.children[i];
      i += 1;
    }
    return null;
  }

  getAttribute(name) {
    const v = this.attrs[name];
    return v === undefined || v === null ? null : String(v);
  }

  getBoundingClientRect() {
    return { width: 300, height: 24, top: 10, left: 0, bottom: 34, right: 300 };
  }

  cloneNode() { return this; }

  remove() {}

  matches(sel) { return matchSingle(this, sel); }

  querySelector(sel) {
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }

  querySelectorAll(sel) {
    const selectors = String(sel).split(',').map((s) => s.trim()).filter(Boolean);
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child.tagName === '#TEXT') continue;
        if (selectors.some((s) => matchSingle(child, s))) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

// ── 选择器匹配（只覆盖本仓库用到的写法：tag / .class / [attr] / [attr*=v] 后代组合）──
const CLASS_RE = /\.([A-Za-z0-9_-]+)/g;
const ATTR_RE = /\[([A-Za-z0-9_-]+)\s*([*^$]?)=\s*"([^"]*)"\]/g;

function matchCompound(node, compound) {
  const tag = compound.match(/^[A-Za-z][A-Za-z0-9-]*/);
  if (tag && node.tagName.toLowerCase() !== tag[0].toLowerCase()) return false;

  const cls = String(node.className || '');
  CLASS_RE.lastIndex = 0;
  let m;
  while ((m = CLASS_RE.exec(compound))) {
    if (!new RegExp('(^|\\s)' + m[1] + '(\\s|$)').test(cls)) return false;
  }

  ATTR_RE.lastIndex = 0;
  while ((m = ATTR_RE.exec(compound))) {
    const actual = node.getAttribute(m[1]) || '';
    const want = m[3];
    const ok = m[2] === '*' ? actual.indexOf(want) >= 0
      : m[2] === '^' ? actual.indexOf(want) === 0
        : m[2] === '$' ? actual.slice(-want.length) === want
          : actual === want;
    if (!ok) return false;
  }
  return true;
}

function matchSingle(node, sel) {
  const parts = String(sel).trim().split(/\s+/);
  if (!matchCompound(node, parts[parts.length - 1])) return false;
  let i = parts.length - 2;
  let cur = node.parentElement;
  while (i >= 0 && cur) {
    if (matchCompound(cur, parts[i])) i -= 1;
    cur = cur.parentElement;
  }
  return i < 0;
}

// ── 用例：DOM 结构 + 页面 URL ─────────────────────────────────
const CASES = {
  // ① 有「职位描述」标题 + 兄弟块：JD 只能是这一块，不能扫进公司介绍/相似职位
  'jd-section': () => ({
    href: 'https://www.zhipin.com/job_detail/abc123.html?securityId=sec-9',
    title: 'Python 后端工程师-BOSS直聘',
    kids: [
      el('div', { class: 'job-detail-box' },
        el('div', { class: 'job-sec' },
          el('h3', {}, '职位描述'),
          el('div', { class: 'job-sec-text' },
            '负责交易系统后端接口开发与性能优化。\n要求熟悉 Python / MySQL / Redis。'),
          el('h3', {}, '公司介绍'),
          el('div', { class: 'job-sec-text' }, '我们是一家专注跨境电商的科技公司，团队 200 人。'),
          el('h3', {}, '相似职位'),
          el('div', {}, '游戏策划 20-30K'))),
    ],
  }),

  // ② 公司介绍与 JD 必须互不串味（中间还夹了「工作地址」）
  'intro-separation': () => ({
    href: 'https://www.zhipin.com/job_detail/abc123.html',
    title: '后端工程师-BOSS直聘',
    kids: [
      el('div', { class: 'job-detail-box' },
        el('h3', {}, '职位描述'),
        el('div', { class: 'job-sec-text' },
          '负责后端服务开发，要求本科以上学历，熟悉 Python 与 Go。'),
        el('h3', {}, '工作地址'),
        el('div', {}, '广州市天河区珠江新城'),
        el('h3', {}, '公司介绍'),
        el('div', { class: 'job-sec-text' }, '我们是游戏公司，主营海外发行与联运。'),
        el('h3', {}, '相似职位'),
        el('div', {}, '游戏策划 20-30K')),
    ],
  }),

  // ③ 选择器全丢：只有一句页脚文案，必须给出明确失败原因，而不是空串成功
  'no-selectors': () => ({
    href: 'https://www.zhipin.com/job_detail/abc123.html',
    title: 'BOSS直聘-互联网招聘',
    kids: [el('div', {}, 'BOSS直聘')],
  }),

  // ④ 没有标题节点：只能走整屏正则兜底，且 from 必须自报 text-fallback
  'text-fallback': () => ({
    href: 'https://www.zhipin.com/job_detail/abc123.html',
    title: '风控工程师-BOSS直聘',
    kids: [
      el('div', { class: 'job-detail-box' },
        el('div', {},
          '职位描述\n负责风控引擎开发，熟悉 Python 与 Spark。\n'
          + '公司介绍\n我们是一家做金融风控的科技公司，员工 300 人。\n'
          + '相似职位\n游戏策划 20-30K')),
    ],
  }),

  // ⑤ 工商信息块：拿全称，且要在「法定代表人」前截断
  'gs-name': () => ({
    href: 'https://www.zhipin.com/job_detail/abc123.html',
    title: '后端工程师-BOSS直聘',
    kids: [
      el('div', { class: 'job-detail-box' },
        el('h3', {}, '职位描述'),
        el('div', { class: 'job-sec-text' }, '负责后端开发，要求三年以上经验，熟悉 Python。'),
        el('div', { class: 'gs-block' },
          '工商信息\n公司名称 广州某某科技有限公司\n法定代表人 张三\n成立日期 2015-01-01')),
    ],
  }),

  // ⑥ 沟通按钮 ka 指向别的岗位 = 右栏还没切过来（GetJob 的对岗闸门）
  'detail-mismatch': () => ({
    href: 'https://www.zhipin.com/job_detail/abc123.html',
    title: 'BOSS直聘',
    kids: [
      el('div', { class: 'job-detail-box' },
        el('a', { class: 'op-btn op-btn-chat', ka: 'job-detail-chat_OTHER999' }, '立即沟通')),
    ],
  }),
};

function buildDocument(spec) {
  const html = el('html', {}, el('body', {}, ...spec.kids));
  const body = html.children[0];
  const location = {
    href: spec.href,
    pathname: spec.href.replace(/^https?:\/\/[^/]+/, '').split('?')[0],
    origin: 'https://www.zhipin.com',
  };
  const document = {
    title: spec.title || '',
    readyState: 'complete',
    body,
    documentElement: html,
    querySelector: (sel) => html.querySelector(sel),
    querySelectorAll: (sel) => html.querySelectorAll(sel),
  };
  return { document, location };
}

function main() {
  const caseName = process.argv[2];
  if (!CASES[caseName]) {
    process.stderr.write('unknown case: ' + caseName + '\n');
    process.exit(2);
  }
  const spec = CASES[caseName]();
  const { document, location } = buildDocument(spec);
  const sandbox = {
    document,
    location,
    window: {
      document,
      location,
      getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    },
    Date, JSON, Math, RegExp, String, Number, Boolean, Array, Object,
    decodeURIComponent, encodeURIComponent, console,
  };
  const js = fs.readFileSync(0, 'utf8');
  const context = vm.createContext(sandbox);
  const value = vm.runInContext(js, context, { timeout: 5000, filename: 'boss_extract.rendered.js' });
  if (typeof value !== 'string') {
    process.stderr.write('注入脚本没有返回字符串: ' + typeof value + '\n');
    process.exit(3);
  }
  process.stdout.write(value);
}

main();
