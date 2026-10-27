/**
 * BOSS 页面拦截检测 + 处置策略。
 *
 * 为什么需要：投递跑到一半，页面弹个「个人信息保护须知」或「安全验证」，
 * 没有处理逻辑就整个卡死 —— 这是「能不能无人值守跑完」的分水岭。
 *
 * 设计原则（对标 GetJob 桌面端的实际实现后取的头部经验，代码自写）：
 *   1. 白名单优先 —— 已知的「招呼成功框 / 聊天框」不算拦截，否则会把正常流程当异常
 *   2. 尺寸门槛   —— 够大的 dialog 才算拦截，忽略小气泡
 *   3. 不点未知按钮 —— 认不出来的弹窗一律【停投】。乱点可能误操作或触发风控
 *   4. 验证码/风控 → 立即硬停，不重试、不换参数试探（沿用风控铁律）
 *   5. 隐私协议 → 唯一允许自动点的，且按钮文案必须【恰好】是「同意/同意并继续」。
 *      绝不能用 includes ——「拒绝同意」里也含「同意」二字，点错就是拒绝授权。
 *
 * 本文件既能在 Electron 渲染进程用（window.JKBlocker），
 * 也能被 node 直接 require 做单测（纯逻辑不依赖 DOM）。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.JKBlocker = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : null), function () {
  'use strict';

  // ══════════════════════════════════════════════════════════════
  // 站点配置
  // ══════════════════════════════════════════════════════════════
  var BOSS_CFG = {
    // 验证码/风控组件：出现即硬停
    captcha: '.geetest_panel, .geetest_fullpage, .nc-container, iframe[src*="captcha"]',
    // 可能成为拦截的弹窗容器（不用 [class*=modal] 这种泛匹配）
    dialogs: [
      '[role="dialog"]', '.dialog-wrap', '.boss-dialog', '.boss-popup',
      '.dialog-container', '.greet-boss-dialog', '.greet-boss-container',
      '.chat-block-dialog', '.ant-modal', '.ant-modal-wrap',
    ].join(', '),
    // 白名单：这些容器是正常流程，不算拦截
    allowedHints: ['greet-boss', 'chat-block'],
    allowedTextRe: '已向BOSS发送消息|留在此页|继续沟通|今天已与|今日已与',
    // 需要人工登录 → 停（不自动填任何东西）
    loginRe: '请先登录|请登录|账号登录|扫码登录|验证码登录',
    // 明确的风控/安全验证措辞 → 停
    blockRe: '验证码|安全验证|请完成验证|人机验证|滑动验证|异常验证|访问验证|账号异常',
    // 弹窗里含这些元素 = 它其实是页面主体，不是拦截层
    // ⚠️ 岗位卡片的选择器 2026-09-19 在真实页面实测校准过：
    //    老的 .job-card-wrapper / ul.job-list > li.job-card 现在命中 0 条（BOSS 已改版），
    //    现行的是 .job-card-box（LI）。新旧都留着以防后端灰度。
    skipIfContains: '.job-card-box, .job-card-wrapper, .job-list-box, ul.job-list, #chat-input, .chat-conversation',
    minW: 160,
    minH: 80,
  };

  /**
   * 岗位卡片选择器。
   * 实测（2026-09-19，https://www.zhipin.com/web/geek/jobs）：
   *    .job-card-box        15 条  ✅ 现行
   *    [class*="job-card"]  45 条  （含外层 wrap / footer，层级不对）
   *    .job-card-wrapper     0 条  ❌ 已失效
   *    ul.job-list > li      0 条  ❌ 已失效
   */
  var JOB_CARD_SEL = '.job-card-box, .job-card-wrapper, li.job-card, ul.job-list > li.job-card';

  var PRIVACY_TEXT = '个人信息保护须知';
  var PRIVACY_AGREE_LABELS = ['同意', '同意并继续'];

  // 沟通次数用尽 —— 属于「预期结束」，重试毫无意义
  var QUOTA_RE = /明天再来|明天再试|没有剩余|次数用尽|已达上限|达到上限|沟通已满|还剩0(?!\d)|剩余0(?!\d)/;

  // ══════════════════════════════════════════════════════════════
  // 纯逻辑（可单测，不碰 DOM）
  // ══════════════════════════════════════════════════════════════

  /** 去掉所有空白 —— 页面文案常被拆行，「还剩 0 次」要能匹配上 */
  function compact(s) {
    return String(s == null ? '' : s).replace(/\s+/g, '');
  }

  function preview(s, n) {
    n = n || 80;
    var t = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    return t.length > n ? t.slice(0, n) + '...' : t;
  }

  /** 按钮文案是否就是我们敢点的「同意」——必须恰好相等 */
  function isPrivacyAgreeLabel(text) {
    var t = compact(text);
    return PRIVACY_AGREE_LABELS.indexOf(t) >= 0;
  }

  /**
   * 判定一个弹窗样本是不是拦截。
   * @param {{width:number,height:number,text:string,classBlob:string,hasButton:boolean}} sample
   * @returns {null | {kind:string, preview:string}}
   */
  function classify(sample, cfg) {
    cfg = cfg || BOSS_CFG;
    var w = Number(sample && sample.width) || 0;
    var h = Number(sample && sample.height) || 0;
    if (w < (cfg.minW || 160) || h < (cfg.minH || 80)) return null;

    var text = String((sample && sample.text) || '');
    var blob = String((sample && sample.classBlob) || '').toLowerCase();

    // 白名单：已知的正常弹窗（招呼成功 / 聊天框）
    var hints = cfg.allowedHints || [];
    for (var i = 0; i < hints.length; i++) {
      var hint = String(hints[i] || '').toLowerCase();
      if (hint && blob.indexOf(hint) >= 0) return null;
    }
    if (cfg.allowedTextRe) {
      try { if (new RegExp(cfg.allowedTextRe).test(text)) return null; } catch (e) { }
    }

    // 隐私协议：唯一可以自动处理的
    if (compact(text).indexOf(PRIVACY_TEXT) >= 0) {
      return { kind: 'privacy', preview: preview(text) };
    }

    // 沟通次数用尽：预期结束，不重试
    if (QUOTA_RE.test(compact(text))) {
      return { kind: 'quota', preview: preview(text) };
    }

    // 需要登录 / 风控验证
    try { if (cfg.loginRe && new RegExp(cfg.loginRe).test(text)) return { kind: 'login', preview: preview(text) }; } catch (e) { }
    try { if (cfg.blockRe && new RegExp(cfg.blockRe).test(text)) return { kind: 'block', preview: preview(text) }; } catch (e) { }

    // 认不出来的：有按钮、或文案够长 → 当拦截处理（停投，绝不乱点）
    var hasButton = !!(sample && sample.hasButton);
    if (hasButton || compact(text).length >= 8) {
      return { kind: 'unknown', preview: preview(text || '未知弹窗') };
    }
    return null;
  }

  // 处置策略表：只有 privacy 允许自动点，其余一律停
  var ACTION = {
    privacy: 'dismiss',
    quota: 'stop',
    login: 'stop',
    captcha: 'stop',
    block: 'stop',
    unknown: 'stop',
  };

  // 停投原因分类：expected = 正常结束（不告警）；abnormal = 异常（要显眼告警）
  var EXPECTED_STOP = {
    quota: 'quota-exhausted',       // 沟通次数用尽
    login: 'not-logged-in',         // 需要重新登录
    'no-more-jobs': 'no-more-jobs',
    'user-stop': 'user-stop',
    'max-delivery': 'max-delivery',
  };
  var ABNORMAL_STOP = {
    captcha: 'captcha',             // 风控验证
    block: 'page-blocked',          // 安全验证/访问验证
    unknown: 'popup',               // 未知弹窗
    'list-timeout': 'list-timeout',
    'consecutive-fails': 'consecutive-fails',
  };

  function decide(blocker) {
    if (!blocker || !blocker.kind) return { action: 'continue', reason: '' };
    return { action: ACTION[blocker.kind] || 'stop', reason: blocker.kind, blocker: blocker };
  }

  /** 停投原因 → 是否需要高亮告警 */
  function stopReasonCode(kind) {
    if (EXPECTED_STOP[kind]) return { code: EXPECTED_STOP[kind], abnormal: false };
    if (ABNORMAL_STOP[kind]) return { code: ABNORMAL_STOP[kind], abnormal: true };
    return { code: 'unknown', abnormal: true };
  }

  function isJobListUrl(u) {
    u = String(u || '');
    return /zhipin\.com\/web\/geek\/(job|jobs)/.test(u) || /zhipin\.com\/overseas/.test(u);
  }

  // ══════════════════════════════════════════════════════════════
  // 注入页面的脚本（返回源码字符串，由 webview.executeJavaScript 执行）
  // ══════════════════════════════════════════════════════════════

  function buildDetectScript(cfg) {
    cfg = cfg || BOSS_CFG;
    return `(function () {
  var CFG = ${JSON.stringify(cfg)};
  var PRIVACY_TEXT = ${JSON.stringify(PRIVACY_TEXT)};
  var QUOTA_RE = ${QUOTA_RE.toString()};
  var AGREE_LABELS = ${JSON.stringify(PRIVACY_AGREE_LABELS)};

  function compact(s) { return String(s == null ? '' : s).replace(/\\s+/g, ''); }
  function preview(s, n) {
    n = n || 80;
    var t = String(s == null ? '' : s).replace(/\\s+/g, ' ').trim();
    return t.length > n ? t.slice(0, n) + '...' : t;
  }
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    try {
      var st = window.getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    } catch (e) { }
    return true;
  }
  function classBlob(el) {
    var parts = [];
    function add(n) {
      if (!n) return;
      if (n.className && typeof n.className === 'string') parts.push(n.className);
      if (n.id) parts.push(n.id);
    }
    add(el);
    var kids = el.querySelectorAll ? el.querySelectorAll('[class],[id]') : [];
    var cap = Math.min(kids.length, 80);
    for (var i = 0; i < cap; i++) add(kids[i]);
    return parts.join(' ').toLowerCase();
  }
  // 白名单要认 自身/祖先/后代 —— 聊天外层 wrap 的 class 会被截断，只查自身会误判
  function hasAllowedHint(el) {
    var n = el;
    for (var d = 0; n && d < 16; d++) {
      var b = ((n.className && typeof n.className === 'string' ? n.className : '') + ' ' + (n.id || '')).toLowerCase();
      for (var i = 0; i < CFG.allowedHints.length; i++) {
        if (b.indexOf(String(CFG.allowedHints[i]).toLowerCase()) >= 0) return true;
      }
      n = n.parentElement;
    }
    for (var j = 0; j < CFG.allowedHints.length; j++) {
      var h = String(CFG.allowedHints[j]).replace(/"/g, '');
      if (!h) continue;
      try { if (el.querySelector('[class*="' + h + '"], [id*="' + h + '"]')) return true; } catch (e) { }
    }
    return false;
  }
  function classify(sample) {
    var w = sample.width || 0, h = sample.height || 0;
    if (w < CFG.minW || h < CFG.minH) return null;
    var text = String(sample.text || '');
    var blob = String(sample.classBlob || '').toLowerCase();
    for (var i = 0; i < CFG.allowedHints.length; i++) {
      if (blob.indexOf(String(CFG.allowedHints[i]).toLowerCase()) >= 0) return null;
    }
    try { if (CFG.allowedTextRe && new RegExp(CFG.allowedTextRe).test(text)) return null; } catch (e) { }
    if (compact(text).indexOf(PRIVACY_TEXT) >= 0) return { kind: 'privacy', preview: preview(text) };
    if (QUOTA_RE.test(compact(text))) return { kind: 'quota', preview: preview(text) };
    try { if (CFG.loginRe && new RegExp(CFG.loginRe).test(text)) return { kind: 'login', preview: preview(text) }; } catch (e) { }
    try { if (CFG.blockRe && new RegExp(CFG.blockRe).test(text)) return { kind: 'block', preview: preview(text) }; } catch (e) { }
    if (sample.hasButton || compact(text).length >= 8) return { kind: 'unknown', preview: preview(text || '未知弹窗') };
    return null;
  }

  // 1) 验证码/风控组件
  try {
    if (CFG.captcha && document.querySelector(CFG.captcha)) {
      return { kind: 'captcha', preview: 'captcha-widget', url: location.href };
    }
  } catch (e) { }

  // 2) 逐个弹窗过
  var nodes = [];
  try { nodes = document.querySelectorAll(CFG.dialogs); } catch (e) { nodes = []; }
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    if (!visible(el)) continue;
    if (CFG.skipIfContains) {
      try { if (el.querySelector(CFG.skipIfContains)) continue; } catch (e) { }
    }
    if (hasAllowedHint(el)) continue;
    var rect = el.getBoundingClientRect();
    var txt = (el.innerText || el.textContent || '').trim();
    if (txt.length > 4000) txt = txt.slice(0, 4000);
    var btns = el.querySelectorAll('button, [role="button"], a.btn, .ant-btn, .el-button');
    var hasBtn = false;
    for (var b = 0; b < btns.length; b++) { if (visible(btns[b])) { hasBtn = true; break; } }
    var hit = classify({ width: rect.width, height: rect.height, text: txt, classBlob: classBlob(el), hasButton: hasBtn });
    if (hit) { hit.url = location.href; hit.selectorIndex = i; return hit; }
  }
  return null;
})()`;
  }

  /** 点「同意 / 同意并继续」——严格等值匹配，点到「拒绝同意」就是灾难 */
  function buildPrivacyAgreeScript(cfg) {
    cfg = cfg || BOSS_CFG;
    return `(function () {
  var CFG = ${JSON.stringify(cfg)};
  var PRIVACY_TEXT = ${JSON.stringify(PRIVACY_TEXT)};
  var AGREE_LABELS = ${JSON.stringify(PRIVACY_AGREE_LABELS)};
  function compact(s) { return String(s == null ? '' : s).replace(/\\s+/g, ''); }
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    try {
      var st = window.getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    } catch (e) { }
    return true;
  }
  var nodes = [];
  try { nodes = document.querySelectorAll(CFG.dialogs); } catch (e) { return { ok: false, reason: 'no-dialogs' }; }
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    if (!visible(el)) continue;
    var txt = (el.innerText || el.textContent || '');
    if (compact(txt).indexOf(PRIVACY_TEXT) < 0) continue;
    var btns = el.querySelectorAll('button, [role="button"], a.btn, .ant-btn, .el-button, span');
    for (var b = 0; b < btns.length; b++) {
      var btn = btns[b];
      if (!visible(btn)) continue;
      var label = compact(btn.innerText || btn.textContent || '');
      if (AGREE_LABELS.indexOf(label) < 0) continue;
      try { btn.click(); } catch (e) { return { ok: false, reason: 'click-failed', label: label }; }
      return { ok: true, clicked: label, url: location.href };
    }
    // 找到了隐私弹窗但没有可点的「同意」——不要乱点，报出来人工处理
    return { ok: false, reason: 'agree-button-not-found', text: compact(txt).slice(0, 120) };
  }
  return { ok: false, reason: 'no-privacy-dialog' };
})()`;
  }

  /** 等岗位列表就绪：轮询卡片出现，超时返回 false（调用方据此停投） */
  function buildWaitListReadyScript(timeoutMs) {
    timeoutMs = timeoutMs || 15000;
    return `(async function () {
  var CARD = ${JSON.stringify(JOB_CARD_SEL)};
  var deadline = Date.now() + ${timeoutMs};
  while (Date.now() < deadline) {
    try { if (document.querySelector(CARD)) return { ok: true, count: document.querySelectorAll(CARD).length, url: location.href }; } catch (e) { }
    await new Promise(function (r) { setTimeout(r, 500); });
  }
  return { ok: false, reason: 'list-timeout', url: location.href };
})()`;
  }

  /** 数当前列表里有多少岗位卡（0 张 = 不能投，必须停而不是盲点） */
  function buildCountCardsScript() {
    return `(function () {
  var CARD = ${JSON.stringify(JOB_CARD_SEL)};
  try {
    var n = document.querySelectorAll(CARD).length;
    return { count: n, ok: n > 0, url: location.href };
  } catch (e) { return { count: -1, ok: false, url: location.href }; }
})()`;
  }

  return {
    BOSS_CFG: BOSS_CFG,
    JOB_CARD_SEL: JOB_CARD_SEL,
    PRIVACY_TEXT: PRIVACY_TEXT,
    PRIVACY_AGREE_LABELS: PRIVACY_AGREE_LABELS,
    QUOTA_RE: QUOTA_RE,
    ACTION: ACTION,
    compact: compact,
    preview: preview,
    isPrivacyAgreeLabel: isPrivacyAgreeLabel,
    classify: classify,
    decide: decide,
    stopReasonCode: stopReasonCode,
    isJobListUrl: isJobListUrl,
    buildDetectScript: buildDetectScript,
    buildPrivacyAgreeScript: buildPrivacyAgreeScript,
    buildWaitListReadyScript: buildWaitListReadyScript,
    buildCountCardsScript: buildCountCardsScript,
  };
});
