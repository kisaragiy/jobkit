/**
 * 历史 DOM 模板与纯函数测试夹具；不在生产页面加载。
 * 原 runBatch 执行循环已移除，所有模式都返回 kernel-required。
 *
 * 页面结构（2026-09-19 在真实页面实测校准）：
 *   .recommend-result-job
 *     ├── 15 × .job-card-box        ← 左列：岗位卡片（368 宽）
 *     └── .job-detail-container     ← 右列：当前选中岗的详情（752 宽）
 *           .job-detail-header > .job-detail-op > a.op-btn-chat   ← 立即沟通（全页仅 1 个）
 *
 * ⚠️ 所以流程必须是：点卡片 → 等详情加载 → 点详情里的「立即沟通」。
 *    全页只有一个沟通按钮，不能"遍历卡片找按钮"。
 *
 * ⛔ 不可逆边界：「点 op-btn-chat」会在 BOSS 上真实开聊（默认招呼直接发出）。
 *    因此该动作被隔离在 clickChat() 里，dry-run 模式下永不执行。
 *
 * 设计约束（沿用风控铁律）：
 *   1. 90 秒内不重复点同一张卡（防误连点）
 *   2. 节奏：间隔 60-120s 随机，每 5 个休息 10-15min，日上限 130
 *   3. 风控信号（captcha/block）→ 立即硬停，不重试不试探
 *   4. 卡片数 0 / 详情未就绪 / 找不到沟通按钮 → 停，不盲点
 *   5. 每一步都先过拦截检测
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.JKDelivery = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : null), function () {
  'use strict';

  var CARD_SEL = '.job-card-box, .job-card-wrapper, li.job-card';
  var CHAT_BTN_SEL = '.op-btn-chat, .btn-startchat, a[class*="op-btn-chat"]';
  var DETAIL_BOX_SEL = '.job-detail-container, .job-detail-box';

  // ══════════════════════════════════════════════════════════════
  // 纯逻辑（可单测）
  // ══════════════════════════════════════════════════════════════

  /**
   * 从沟通按钮的 ka 属性里抠出 job id。
   * 实测：ka="cpc_job_list_chat_1489b9c1f8d362a60nB92Ni8GFpR"
   * 这个 id 与岗位详情链接 /job_detail/<id>.html 里的 id 一致，可用于对账。
   */
  function extractJobId(ka) {
    var s = String(ka || '');
    var m = s.match(/[0-9a-f]{12,}[A-Za-z0-9~_-]*/);
    if (m) return m[0];
    var p = s.split('_');
    return p.length ? p[p.length - 1] : '';
  }

  /** 从 /job_detail/<id>.html 抠 id */
  function extractJobIdFromHref(href) {
    var m = String(href || '').match(/\/job_detail\/([A-Za-z0-9~_-]+)\.html/);
    return m ? m[1] : '';
  }

  function clampSec(v, lo, hi) {
    v = Number(v);
    if (!isFinite(v)) return lo;
    return Math.min(hi, Math.max(lo, v));
  }

  /**
   * 生成节奏计划：每个动作前的等待秒数。
   * 间隔 60-120s 随机；每 restEvery 个插入一次长休息。
   */
  function planPacing(count, opts) {
    opts = opts || {};
    var lo = clampSec(opts.minSec, 5, 600) || 60;
    var hi = clampSec(opts.maxSec, lo, 900) || 120;
    var restEvery = Math.max(1, Number(opts.restEvery) || 5);
    var restSec = clampSec(opts.restSec, 30, 3600) || 600;
    var out = [];
    var rnd = opts.random || Math.random;
    for (var i = 0; i < count; i++) {
      var wait = Math.round(lo + rnd() * (hi - lo));
      var isRest = (i > 0 && i % restEvery === 0);
      out.push({ index: i, waitSec: isRest ? restSec : wait, isRest: isRest });
    }
    return out;
  }

  /**
   * 熔断判定。返回 {stop:boolean, reason:string}
   * reason 用 stopReasonCode 的编码，便于和拦截模块对齐。
   */
  function shouldStop(state) {
    state = state || {};
    if (state.userStop) return { stop: true, reason: 'user-stop' };
    if (state.riskSignal) return { stop: true, reason: 'risk' };
    if (state.delivered >= state.maxDelivery) return { stop: true, reason: 'max-delivery' };
    if ((state.consecutiveFails || 0) >= (state.maxConsecutiveFails || 3)) {
      return { stop: true, reason: 'consecutive-fails' };
    }
    if (state.listEmpty) return { stop: true, reason: 'no-more-jobs' };
    return { stop: false, reason: '' };
  }

  /** 默认参数（与风控铁律一致） */
  var DEFAULT_OPTS = {
    maxDelivery: 130,
    minSec: 60,
    maxSec: 120,
    restEvery: 5,
    restSec: 600,
    maxConsecutiveFails: 3,
    detailTimeoutMs: 12000,
  };

  // ══════════════════════════════════════════════════════════════
  // 注入页面的脚本（全部只读，不改页面状态）
  // ══════════════════════════════════════════════════════════════

  /** 读列表：每张卡的 jobId / 标题 / 公司 / 薪资文本 */
  function buildCollectCardsScript() {
    return `(function () {
  var CARD = ${JSON.stringify(CARD_SEL)};
  function txt(el, sel) { try { var n = el.querySelector(sel); return n ? (n.innerText || '').replace(/\\s+/g, ' ').trim() : ''; } catch (e) { return ''; } }
  var cards = document.querySelectorAll(CARD);
  var out = [];
  for (var i = 0; i < cards.length; i++) {
    var c = cards[i];
    var a = null;
    try { a = c.querySelector('a.job-name, a[href*="/job_detail/"]'); } catch (e) { }
    out.push({
      index: i,
      jobId: a ? (String(a.getAttribute('href') || '').match(/\\/job_detail\\/([A-Za-z0-9~_-]+)\\.html/) || [])[1] || '' : '',
      title: a ? (a.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60) : '',
      company: txt(c, '.boss-name, .company-name, .boss-info'),
      salaryRaw: txt(c, '.salary, .job-salary, [class*="salary"]'),
      raw: (c.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160)
    });
  }
  return { count: out.length, cards: out, url: location.href };
})()`;
  }

  /** 读右侧详情面板当前显示的是哪个岗（只读，用于确认点卡是否生效） */
  function buildReadDetailScript() {
    return `(function () {
  var SEL = ${JSON.stringify(DETAIL_BOX_SEL)};
  var box = document.querySelector(SEL);
  if (!box) return { found: false };
  function txt(sel) { try { var n = box.querySelector(sel); return n ? (n.innerText || '').replace(/\\s+/g, ' ').trim() : ''; } catch (e) { return ''; } }
  var btn = box.querySelector(${JSON.stringify(CHAT_BTN_SEL)});
  var r = box.getBoundingClientRect();
  return {
    found: true,
    title: txt('.job-name, .name, h1'),
    company: txt('.company-name, .boss-name, .company-info'),
    salary: txt('.salary, [class*="salary"]'),
    hasChatBtn: !!btn,
    chatBtnKa: btn ? (btn.getAttribute('ka') || '') : '',
    chatBtnText: btn ? (btn.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 20) : '',
    boxSize: { w: Math.round(r.width), h: Math.round(r.height) },
    url: location.href
  };
})()`;
  }

  /** 点第 i 张卡（把它的详情加载到右栏）—— 只触发页面自身的选中逻辑，不产生沟通 */
  function buildFocusCardScript(index) {
    return `(function () {
  var CARD = ${JSON.stringify(CARD_SEL)};
  var cards = document.querySelectorAll(CARD);
  if (!cards.length) return { ok: false, reason: 'no-cards' };
  if (${Number(index)} >= cards.length) return { ok: false, reason: 'index-out-of-range' };
  var c = cards[${Number(index)}];
  var a = null;
  try { a = c.querySelector('a.job-name, a[href*="/job_detail/"]'); } catch (e) { }
  var target = a || c;
  try { target.scrollIntoView({ block: 'center' }); } catch (e) { }
  try { target.click(); } catch (e) { return { ok: false, reason: 'click-failed', err: String(e) }; }
  return { ok: true, index: ${Number(index)}, clicked: target.tagName };
})()`;
  }

  /** 等右侧详情加载完成：出现详情容器 + 沟通按钮 */
  function buildWaitDetailReadyScript(timeoutMs) {
    timeoutMs = timeoutMs || 12000;
    return `(async function () {
  var BOX = ${JSON.stringify(DETAIL_BOX_SEL)};
  var BTN = ${JSON.stringify(CHAT_BTN_SEL)};
  var deadline = Date.now() + ${timeoutMs};
  while (Date.now() < deadline) {
    try {
      var b = document.querySelector(BOX);
      if (b && b.querySelector(BTN)) {
        var t = b.querySelector('.job-name, .name, h1');
        return { ok: true, title: t ? (t.innerText || '').trim().slice(0, 60) : '', url: location.href };
      }
    } catch (e) { }
    await new Promise(function (r) { setTimeout(r, 400); });
  }
  return { ok: false, reason: 'detail-timeout', url: location.href };
})()`;
  }

  /**
   * ⛔ 真正不可逆的一步：点「立即沟通」。
   * 绝不在 dry-run 里调用。调用方必须显式传入 confirm=true。
   */
  function buildClickChatScript(confirm) {
    return `(function () {
  if (${confirm ? 'true' : 'false'} !== true) return { ok: false, reason: 'not-confirmed' };
  var BOX = ${JSON.stringify(DETAIL_BOX_SEL)};
  var BTN = ${JSON.stringify(CHAT_BTN_SEL)};
  var b = document.querySelector(BOX);
  var btn = b ? b.querySelector(BTN) : document.querySelector(BTN);
  if (!btn) return { ok: false, reason: 'chat-button-not-found' };
  var r = btn.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return { ok: false, reason: 'chat-button-invisible' };
  var ka = btn.getAttribute('ka') || '';
  try { btn.click(); } catch (e) { return { ok: false, reason: 'click-failed', err: String(e) }; }
  return { ok: true, ka: ka, text: (btn.innerText || '').trim().slice(0, 20), url: location.href };
})()`;
  }

  /**
   * 投递主循环。
   *
   * 为什么用依赖注入：循环的「控制流」才是最容易写错的地方（什么时候停、什么时候跳过、
   * 什么时候绝不点），而它跟真实 DOM 耦合后就只能靠真机碰运气验证。
   * 把原语注入进来，控制流就能被完整单测覆盖。
   *
   * deps（全部 async）：
   *   detectBlocker()      -> null | {kind}
   *   decideBlocker(b)     -> 'dismiss' | 'stop'
   *   stopReasonCode(kind) -> {code, abnormal}
   *   dismissPrivacy()     -> {ok, reason?}
   *   collectCards()       -> {count, cards:[{index,jobId,title,company}]}
   *   focusCard(i)         -> {ok, reason?}
   *   waitDetailReady()    -> {ok, reason?}
   *   readDetail()         -> {hasChatBtn, title, chatBtnKa, ...}
   *   clickChat(confirm)   -> {ok, reason?}      ⛔ confirm=false 必须拒绝
   *   makeGreeting(det, card) -> {message, reason?}   ★ 真发模式要
   *   typeGreeting(text)   -> {ok, reason?}           ★
   *   sendGreeting(confirm)-> {ok, reason?}           ★ ⛔ confirm=false 必须拒绝
   *   readConversation()   -> {count, messages:[{text}]}  ★ 确认「送达」
   *   record({jobId,status,message,...}) -> 任意   ★ 台账回写（可选）
   *   sleep(ms)
   *
   * opts.dryRun 默认 true —— 演练模式走完全部流程，但永不调用 clickChat。
   * opts.dailyCap / opts.sentToday —— 日配额闸门（真发模式生效）。
   */
  async function runBatch() {
    return { delivered: 0, failed: 0, filtered: 0, reason: 'kernel-required',
             dryRun: true, log: ['旧浏览器执行循环已移除，请使用唯一 Kernel。'] };
  }

  /**
   * 在聊天页输入招呼语。
   * ⚠️ 输入框是 contenteditable，不是 <textarea>（实测 textarea 命中 0）。
   * 必须走 execCommand('insertText') 或派发 input 事件，否则框架（Vue/React）收不到值，
   * 表现为"界面看着有字，点发送发出去是空的"。
   */
  function buildTypeGreetingScript(text) {
    return `(function () {
  var TXT = ${JSON.stringify(text)};
  var el = document.querySelector('#chat-input')
        || document.querySelector('.chat-editor')
        || document.querySelector('[contenteditable="true"]');
  if (!el) return { ok: false, reason: 'input-not-found' };

  function sendBtn() {
    var sels = ['.btn-send', '[class*="send-btn"]'];
    for (var i = 0; i < sels.length; i++) {
      try { var b = document.querySelector(sels[i]); if (b) return b; } catch (e) { }
    }
    return null;
  }
  function btnDisabled(b) {
    if (!b) return null;
    try { return !!b.disabled || /disabled/i.test(String(b.className || '')); } catch (e) { return null; }
  }

  try { el.focus(); } catch (e) { }

  // 方式①：execCommand —— 最接近真实键入，框架能收到
  try { el.innerHTML = ''; document.execCommand('insertText', false, TXT); } catch (e) { }

  // 方式②：直接设 textContent + 派发 input
  if (!(el.innerText || el.textContent || '').trim()) {
    el.textContent = TXT;
    try { el.dispatchEvent(new InputEvent('input', { bubbles: true, data: TXT, inputType: 'insertText' })); } catch (e) { }
  }

  var cur = (el.innerText || el.textContent || '').trim();
  var dis = btnDisabled(sendBtn());

  // ⚠️ 关键判据：有字但发送按钮仍是 disabled = 框架根本没收到值。
  // 血教训（2026-09-21）：原实现只看"输入框有没有字"就返回 ok ——
  // 结果发送按钮还是灰的，点下去必然失败（greetings 表里留下了 send-button-disabled）。
  // 所以这里再补一轮更"重"的事件，并且把按钮状态作为 ok 的判据。
  if (cur && dis === true) {
    try {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: TXT }));
      el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'a' }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } catch (e) { }
    dis = btnDisabled(sendBtn());
  }

  return {
    ok: !!cur && dis !== true,
    typed: cur.slice(0, 60),
    len: cur.length,
    sendDisabled: dis,
    reason: !cur ? '输入框仍为空' : (dis === true ? '输入框有字但发送按钮仍禁用（框架没收到值）' : ''),
    url: location.href,
  };
})()`;
  }

  /** ⛔ 真正把消息发出去的一步。同样要求 confirm=true。 */
  function buildSendScript(confirm) {
    return `(function () {
  if (${confirm ? 'true' : 'false'} !== true) return { ok: false, reason: 'not-confirmed' };
  var SELS = ['.btn-send', '[class*="send-btn"]', 'button[type="submit"]'];
  var btn = null;
  for (var i = 0; i < SELS.length && !btn; i++) {
    try { btn = document.querySelector(SELS[i]); } catch (e) { }
  }
  if (!btn) return { ok: false, reason: 'send-button-not-found' };
  var r = btn.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return { ok: false, reason: 'send-button-invisible' };
  var dis = false;
  try { dis = !!btn.disabled || /disabled/i.test(String(btn.className || '')); } catch (e) { }
  if (dis) return { ok: false, reason: 'send-button-disabled' };
  try { btn.click(); } catch (e) { return { ok: false, reason: 'click-failed', err: String(e) }; }
  return { ok: true, cls: String(btn.className || '').slice(0, 40), url: location.href };
})()`;
  }

  /** 读完当前会话里的消息（用于确认招呼真的发出去了） */
  function buildReadConversationScript() {
    return `(function () {
  var MSG = '.message-item, .chat-message, [class*="message-item"]';
  var nodes = [];
  try { nodes = document.querySelectorAll(MSG); } catch (e) { }
  var out = [];
  for (var i = 0; i < Math.min(nodes.length, 30); i++) {
    var t = (nodes[i].innerText || '').replace(/\\s+/g, ' ').trim();
    if (t) out.push({ cls: String(nodes[i].className || '').slice(0, 44), text: t.slice(0, 140) });
  }
  var cur = '';
  try { var c = document.querySelector('#chat-input, .chat-editor, [contenteditable="true"]'); cur = c ? (c.innerText || c.textContent || '').trim() : ''; } catch (e) { }
  return { count: out.length, messages: out, inputNow: cur.slice(0, 60), url: location.href };
})()`;
  }

  return {
    CARD_SEL: CARD_SEL,
    CHAT_BTN_SEL: CHAT_BTN_SEL,
    DETAIL_BOX_SEL: DETAIL_BOX_SEL,
    DEFAULT_OPTS: DEFAULT_OPTS,
    extractJobId: extractJobId,
    extractJobIdFromHref: extractJobIdFromHref,
    planPacing: planPacing,
    shouldStop: shouldStop,
    buildCollectCardsScript: buildCollectCardsScript,
    buildReadDetailScript: buildReadDetailScript,
    buildFocusCardScript: buildFocusCardScript,
    buildWaitDetailReadyScript: buildWaitDetailReadyScript,
    buildClickChatScript: buildClickChatScript,
    buildTypeGreetingScript: buildTypeGreetingScript,
    buildSendScript: buildSendScript,
    buildReadConversationScript: buildReadConversationScript,
    runBatch: runBatch,
  };
});
