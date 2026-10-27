/**
 * jobkit · BOSS 直聘 DOM 通道注入脚本（迁移自 GetJob 2.3.1）
 *
 * 出处：
 *   platforms/boss/page-hydrate.js       卡片 DOM + Vue 实例兜底 + HR 活跃文案
 *   platforms/boss/job-mapper.js         薪资 PUA 字体混淆过滤、hrActive 清洗、公司名优选
 *   platforms/boss/job-detail-dom.js     职位描述 / 公司介绍 / 工商全称（DOM 区块优先，正则仅兜底）
 *   platforms/shared/logged-out-detect.js 登录态（顶栏 + 未登录 CTA，不看用户名）
 *   platforms/boss/filter-url.js         安全验证页 URL
 *
 * 两个占位符由 Python 侧替换（dom/boss_selectors.py）：
 *   __JOBKIT_SELECTORS__  选择器表 JSON
 *   __JOBKIT_TEXTS__      判定文案 / 正则源码 JSON
 *   __JOBKIT_ENTRY__      本次要跑的入口：'list' | 'detail'
 *
 * 对外两个入口（同一次注入后都挂在 window 上，可直接复用）：
 *   window.__jobkitExtractList()   -> JSON 字符串
 *   window.__jobkitExtractDetail() -> JSON 字符串
 *
 * 返回值只报告「页面事实」，不在这里决定 reason：listFound / cardCount / login / restricted
 * 分开报，好让 Python 侧区分「选择器失效」（没有列表容器）与「真没岗」（有容器 0 条卡）。
 */
(function () {
  'use strict';

  var S = __JOBKIT_SELECTORS__;
  var T = __JOBKIT_TEXTS__;
  var ENTRY = __JOBKIT_ENTRY__;

  var PUA_MIN = 0xE000;   // 私有区字符：海外页薪资用 kanzhun-mix-num 字体，textContent 是这种鬼画符
  var PUA_MAX = 0xF8FF;
  var BODY_TEXT_CAP = 30000;   // 正文扫描上限，避免超长页面把注入拖慢

  // ── 基础工具 ────────────────────────────────────────────────
  function compact(s) {
    return String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  }

  /** 只去首尾空白、保留内部换行：职位描述这类正文要用它，别把 JD 压成一行。 */
  function trimEdges(s) {
    return String(s == null ? '' : s).replace(/^\s+|\s+$/g, '');
  }

  /** 区块正文：innerText 保留版式换行，取不到时退回 textContent。 */
  function sectionText(el) {
    if (!el) return '';
    var s = '';
    try { s = String(el.innerText || ''); } catch (e) { s = ''; }
    if (!s) s = String(el.textContent || '');
    return trimEdges(s);
  }

  function q(root, sel) {
    try { return (root || document).querySelector(sel); } catch (e) { return null; }
  }

  function qa(root, sel) {
    try { return (root || document).querySelectorAll(sel); } catch (e) { return []; }
  }

  function txt(el) {
    return el ? compact(el.textContent) : '';
  }

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    try {
      var st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    } catch (e) { /* 跨域框读不到样式，按可见处理 */ }
    return true;
  }

  function bodyText() {
    var s = '';
    try { s = String((document.body && document.body.innerText) || ''); } catch (e) { s = ''; }
    return s.slice(0, BODY_TEXT_CAP);
  }

  function isTrunc(s) {
    return /(?:\.{3}|…)$/.test(compact(s));
  }

  // ── 薪资：跳过 PUA 混淆文本 ─────────────────────────────────
  function isObfuscatedSalaryText(text) {
    var s = String(text || '');
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c >= PUA_MIN && c <= PUA_MAX) return true;
    }
    return false;
  }

  /** 按顺序取第一个「非空且不是混淆字」的薪资候选。 */
  function pickReadableSalary() {
    for (var i = 0; i < arguments.length; i++) {
      var s = compact(arguments[i]);
      if (!s) continue;
      if (isObfuscatedSalaryText(s)) continue;
      return s;
    }
    return '';
  }

  // ── HR 活跃度（「离线」绝不留下，否则 = 把死 HR 当活跃）──────
  function sanitizeHrActive(raw) {
    var s = compact(raw);
    if (!s || s === '离线') return '';
    if (/^(当前)?在线$/.test(s)) return s;
    if (/活跃|刚刚|回复|分钟|小时|天前|日内|周内|本周|本月|半年/.test(s)) return s;
    return '';
  }

  /** 右栏「当前在线」标签优先于「3小时前」这类文案。 */
  function readHrActive(root) {
    root = root || document;
    var time = sanitizeHrActive(txt(q(root, S.JOB_BOSS_ACTIVE)));
    var tag = sanitizeHrActive(txt(q(root, S.JOB_BOSS_ONLINE)));
    if (tag && /^(当前)?在线$/.test(tag)) return tag;
    return time || tag;
  }

  // ── Vue 实例兜底（DOM 抓不全时用；线上 Vue3 仍有 __vueParentComponent）
  function pickVueJob(obj) {
    if (!obj || typeof obj !== 'object') return null;
    var name = obj.jobName;
    var id = obj.encryptJobId || obj.encryptId;
    var sal = obj.salaryDesc;
    if (!name || !(id || sal)) return null;
    return {
      encryptJobId: String(obj.encryptJobId || obj.encryptId || ''),
      encryptBossId: String(obj.encryptBossId || ''),
      securityId: String(obj.securityId || ''),
      jobName: String(obj.jobName || ''),
      salaryDesc: String(obj.salaryDesc || ''),
      jobExperience: String(obj.jobExperience || ''),
      jobDegree: String(obj.jobDegree || ''),
      cityName: String(obj.cityName || ''),
      areaDistrict: String(obj.areaDistrict || ''),
      brandName: String(obj.brandName || ''),
      brandScaleName: String(obj.brandScaleName || ''),
      bossName: String(obj.bossName || ''),
      bossTitle: String(obj.bossTitle || ''),
      activeTimeDesc: String(obj.activeTimeDesc || ''),
      bossOnline: !!obj.bossOnline,
      skills: Array.isArray(obj.skills) ? obj.skills.slice(0, 20) : [],
      welfareList: Array.isArray(obj.welfareList) ? obj.welfareList.slice(0, 20) : [],
      jobLabels: Array.isArray(obj.jobLabels) ? obj.jobLabels.slice(0, 20) : []
    };
  }

  function readVueJob(el) {
    var cur = el;
    for (var up = 0; up < 8 && cur; up++) {
      var inst = cur.__vueParentComponent || cur.__vue__;
      if (inst) {
        var sources = [inst.data, inst._data, inst.props, inst.setupState, inst.ctx];
        for (var si = 0; si < sources.length; si++) {
          var src = sources[si];
          if (!src) continue;
          var hit = pickVueJob(src.job || src.jobInfo || src.item || src);
          if (hit) return hit;
        }
      }
      cur = cur.parentElement;
    }
    return null;
  }

  // ── id 提取 ─────────────────────────────────────────────────
  function idFromHref(href) {
    var m = String(href || '').match(/\/job_detail\/([\w-]+)/);
    return m ? m[1] : '';
  }

  /** security_id 是「打招呼」要用的凭证；列表链接有时带 ?securityId=... */
  function securityIdFromHref(href) {
    var m = String(href || '').match(/[?&]securityId=([\w%.-]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // ── 页面状态探针（只报事实，reason 由 Python 定）───────────
  function probeLogin() {
    var out = { loggedOutCta: '', headerFound: false, bodyHint: '', loginUrl: false };
    var href = String(location.href || '');
    try { out.loginUrl = new RegExp(T.loginUrlRe, 'i').test(href); } catch (e) { out.loginUrl = false; }

    var header = null;
    var hs = String(S.HEADER || '').split(',');
    for (var i = 0; i < hs.length; i++) {
      var sel = hs[i].trim();
      if (!sel) continue;
      header = q(document, sel);
      if (header && visible(header)) break;
      header = null;
    }
    out.headerFound = !!header;

    var ctas = qa(document, S.LOGGED_OUT_CTA);
    for (var c = 0; c < ctas.length; c++) {
      var el = ctas[c];
      if (!visible(el)) continue;
      var top = el.getBoundingClientRect().top;
      if (top > 80) continue;   // 只认顶栏的登录按钮，别把正文里的「立即登录」当未登录 CTA
      out.loggedOutCta = S.LOGGED_OUT_CTA;
      break;
    }

    var bt = bodyText();
    for (var h = 0; h < T.loginHints.length; h++) {
      if (bt.indexOf(T.loginHints[h]) >= 0) { out.bodyHint = T.loginHints[h]; break; }
    }
    return out;
  }

  function probeRestricted() {
    var href = String(location.href || '');
    try {
      if (new RegExp(T.securityUrlRe, 'i').test(href)) return { hit: 'security-page' };
    } catch (e) { /* 正则坏了就当没命中，别让整次注入失败 */ }
    var bt = bodyText();
    for (var i = 0; i < T.restricted.length; i++) {
      if (bt.indexOf(T.restricted[i]) >= 0) return { hit: T.restricted[i] };
    }
    return { hit: '' };
  }

  // ── 列表卡片 ────────────────────────────────────────────────
  /** 防嵌套：li.job-card-box 里可能还包一层 .job-card-wrapper，别抓两遍。 */
  function isNestedCard(el) {
    var p = el.parentElement;
    for (var i = 0; i < 6 && p; i++) {
      try {
        if (p.matches && p.matches(S.JOB_CARD_LOOSE)) return true;
      } catch (e) { return false; }
      p = p.parentElement;
    }
    return false;
  }

  function readCard(card) {
    var vue = null;
    try { vue = readVueJob(card); } catch (e) { vue = null; }

    var linkEl = q(card, S.JOB_LINK) || q(card, 'a[href*="/job_detail/"]');
    var href = linkEl ? String(linkEl.getAttribute('href') || linkEl.href || '') : '';
    if (href && href.charAt(0) === '/') href = String(location.origin || '') + href;

    var id = (vue && vue.encryptJobId) || idFromHref(href);
    var title = txt(q(card, S.JOB_TITLE)) || (vue ? vue.jobName : '');
    var salary = pickReadableSalary(vue && vue.salaryDesc, txt(q(card, S.JOB_SALARY)));

    var companyEl = q(card, S.JOB_COMPANY);
    var company = txt(companyEl);
    var companyTitle = '';
    if (companyEl) {
      companyTitle = compact(companyEl.getAttribute('title') || companyEl.getAttribute('aria-label') || '');
    }

    // 注意：变量名不能叫 location —— 会 var 提升覆盖掉 window.location（真实 bug，烟测抓到的）
    var locText = txt(q(card, S.JOB_LOCATION));
    if (!locText && vue) {
      locText = compact((vue.cityName || '') + (vue.areaDistrict ? '·' + vue.areaDistrict : ''));
    }

    var tags = [];
    var tagNodes = qa(card, S.JOB_TAG_LIST);
    for (var t = 0; t < tagNodes.length && tags.length < 20; t++) {
      var tag = txt(tagNodes[t]);
      if (tag) tags.push(tag);
    }

    var cardText = compact(card.innerText || card.textContent || '');
    var scale = (vue && vue.brandScaleName) || '';
    if (!scale) {
      var m = cardText.match(/(\d+-\d+人|\d+人以上|少于\d+人|10000人以上)/);
      if (m) scale = m[1];
    }

    return {
      encryptId: String(id || ''),
      securityId: (vue && vue.securityId) || securityIdFromHref(href),
      jobUrl: href || (id ? 'https://www.zhipin.com/job_detail/' + id + '.html' : ''),
      title: compact(title),
      salary: salary,
      company: company,
      companyTitle: companyTitle,
      companyBrand: (vue && vue.brandName) || '',
      location: locText,
      tags: tags,
      experience: (vue && vue.jobExperience) || '',
      degree: (vue && vue.jobDegree) || '',
      bossActive: readHrActive(card),
      companyScale: scale,
      skills: (vue && vue.skills) || [],
      welfare: (vue && vue.welfareList) || [],
      cardText: cardText.slice(0, 400)
    };
  }

  function extractList() {
    var out = {
      entry: 'list',
      url: String(location.href || ''),
      ready: document.readyState === 'complete',
      hasBody: false,
      login: probeLogin(),
      restricted: probeRestricted(),
      listFound: false,
      cardCount: 0,
      count: 0,
      dropped: 0,
      emptyMarker: false,
      jobs: [],
      stamp: Date.now()
    };
    try { out.hasBody = compact(document.body && document.body.innerText).length > 0; } catch (e) { out.hasBody = false; }

    var container = q(document, S.JOB_LIST);
    var cards = qa(document, S.JOB_CARD + ',' + S.JOB_CARD_LOOSE);
    // 没有容器但卡片还在（改版把容器类名换了）：仍按「找得到岗位」处理，交给上层判断
    out.listFound = !!container || cards.length > 0;
    out.emptyMarker = !!q(document, S.JOB_EMPTY);
    out.cardCount = cards.length;

    for (var i = 0; i < cards.length && out.jobs.length < 200; i++) {
      var card = cards[i];
      try {
        if (card.classList && card.classList.contains('job-card-detail')) continue;  // 右栏详情壳不是卡片
        if (isNestedCard(card)) continue;
        var job = readCard(card);
        if (!job.encryptId && !job.title) { out.dropped++; continue; }
        out.jobs.push(job);
      } catch (e) {
        out.dropped++;
      }
    }
    out.count = out.jobs.length;
    return out;
  }

  // ── 详情页：区块标题定位（主路径）→ 扁平正则（仅兜底）─────────
  // 机制来源：job-detail-dom.js（GetJob 2.3.1）。结构照搬，代码自己写。
  function stripHeading(text, label) {
    return trimEdges(String(text || '').replace(new RegExp('^' + label + '\\s*[:：]?\\s*'), ''));
  }

  function cutAtStops(text, stopRe) {
    var rest = String(text || '');
    var cut = rest.search(stopRe);
    if (cut >= 0) rest = rest.slice(0, cut);
    return trimEdges(rest);
  }

  /** 【仅兜底】整屏 innerText 按标题词切段；命中不了就返回空串，绝不当主路径。 */
  function sliceSectionFallback(raw, label, stopRe) {
    var body = String(raw || '');
    var startRe = new RegExp(label + '\\s*');
    var m = body.search(startRe);
    if (m < 0) return '';
    return cutAtStops(body.slice(m).replace(startRe, ''), stopRe);
  }

  function isSectionHeading(el, label) {
    if (!el) return false;
    var t = compact(sectionText(el));
    return t === label || t === label + ':' || t === label + '：';
  }

  /** 两轮找标题：语义标题节点 → 纯文本短节点（<=12 字）。 */
  function findSectionHeading(root, label) {
    if (!root) return null;
    var strong = qa(root, S.DETAIL_SECTION_HEAD_SEL);
    for (var i = 0; i < strong.length; i++) {
      if (isSectionHeading(strong[i], label)) return strong[i];
    }
    var loose = qa(root, S.DETAIL_SECTION_HEAD_LOOSE_SEL);
    for (var j = 0; j < loose.length; j++) {
      if (!isSectionHeading(loose[j], label)) continue;
      if (sectionText(loose[j]).length <= 12) return loose[j];
    }
    return null;
  }

  var WRAP_RE = null;
  function wrapRe() {
    if (!WRAP_RE) WRAP_RE = new RegExp(T.sectionWrapRe, 'i');
    return WRAP_RE;
  }

  var OTHER_HEAD = null;
  function otherHeadRe() {
    if (!OTHER_HEAD) OTHER_HEAD = new RegExp(T.otherHeadRe);
    return OTHER_HEAD;
  }

  /**
   * 标题节点 → 兄弟块 / 所属 section。
   * from 只是「这段字是哪条路拿到的」，给上层区分主路径与兜底，不用它决定取舍。
   */
  function sectionFromHeading(heading, label, stopRe, minLen) {
    if (!heading) return { text: '', from: '' };

    var sib = heading.nextElementSibling;
    while (sib) {
      var st = sectionText(sib);
      if (st && !otherHeadRe().test(compact(st))) {
        var bodyTxt = cutAtStops(stripHeading(st, label), stopRe);
        if (bodyTxt.length >= minLen) return { text: bodyTxt, from: 'heading-sibling' };
      }
      sib = sib.nextElementSibling;
    }

    var p = heading.parentElement;
    for (var up = 0; up < 5 && p; up++) {
      var cls = String(p.className || '');
      var tag = String(p.tagName || '').toLowerCase();
      if (tag === 'section' || wrapRe().test(cls) || up === 0) {
        var pt = cutAtStops(stripHeading(sectionText(p), label), stopRe);
        if (pt.length >= minLen) return { text: pt, from: 'heading-ancestor' };
      }
      p = p.parentElement;
    }
    return { text: '', from: '' };
  }

  /** 抽一段区块正文：标题主路径 → 整屏正则兜底；不到 minLen 一律当没抽到。 */
  function readDetailSection(root, body, label, stopRe, minLen, maxLen) {
    var hit = sectionFromHeading(findSectionHeading(root, label), label, stopRe, minLen);
    var text = hit.text;
    var from = hit.from;
    if (!text) {
      text = sliceSectionFallback(body, label, stopRe);
      from = text ? 'text-fallback' : '';
    }
    if (text && text.length < minLen) { text = ''; from = ''; }
    if (text.length > maxLen) text = text.slice(0, maxLen);
    return { text: text, from: from };
  }

  /** 工商信息 → 公司名称：接口 brandName 常被截断，这里是全称。 */
  function pickGsName(raw) {
    var gs = String(raw || '').indexOf(T.gsSection);
    if (gs < 0) return '';
    var idx = String(raw).indexOf(T.gsName, gs);
    if (idx < 0) return '';
    var after = String(raw).slice(idx + T.gsName.length).replace(/^[:：\s]+/, '');
    var val = after.split('\n')[0].replace(/\s+/g, ' ').trim();
    if (!val || val.length < 4 || isTrunc(val)) return '';
    var cut = val.search(new RegExp(T.gsStopRe));
    if (cut > 0) val = val.slice(0, cut).replace(/\s+/g, ' ').trim();
    if (!val || val.length < 4 || isTrunc(val)) return '';
    return val.slice(0, 80);
  }

  function pickGsCompanyName() {
    var marked = qa(document, S.DETAIL_GS_BLOCK_SEL);
    var best = '';
    for (var i = 0; i < marked.length; i++) {
      var raw = String(marked[i].innerText || '').trim();
      if (raw.indexOf(T.gsSection) < 0 || raw.indexOf(T.gsName) < 0) continue;
      var name = pickGsName(raw);
      // 取名字最长的那个容器（= 包含最完整区块的外层）
      if (name && name.length >= best.length) best = name;
    }
    if (best) return best;
    return pickGsName(bodyText());
  }

  function stripHrDecorations(el) {
    if (!el) return '';
    var clone = el.cloneNode(true);
    var drop = clone.querySelectorAll('.boss-active-time, .boss-online-tag, .icon-vip, [class*="boss-online-tag"], [class*="boss-active-time"]');
    for (var i = 0; i < drop.length; i++) drop[i].remove();
    return compact(clone.textContent);
  }

  function hrTitleFromAttr(attr) {
    var s = compact(attr);
    if (!s) return '';
    var parts = s.split('·').map(function (p) { return p.trim(); }).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : '';
  }

  /** 对岗闸门（GetJob delivery-engine.js:4248-4256 的同一判据）：
   *  沟通按钮 ka 里带本岗 encryptId 才说明右栏已切到本岗；读不到 ka = null（无法判定，不是 false）。*/
  function readChatGate() {
    var out = { chatKa: '', jobId: '', matched: null };
    var m = String(location.pathname || location.href || '').match(/\/job_detail\/([\w-]+)/);
    out.jobId = m ? m[1] : '';
    var btn = q(document, S.DETAIL_CHAT_BUTTON);
    if (btn) out.chatKa = compact(btn.getAttribute('ka') || '');
    if (out.chatKa && out.jobId) out.matched = out.chatKa.indexOf(out.jobId) >= 0;
    return out;
  }

  /** 详情页抽不出东西时必须说清是哪一类失败，别让空串冒充成功。 */
  function detailFailReason(out) {
    var href = String(location.href || '');
    if (!out.hasBody) return 'empty-body: body.innerText 为空（白屏或页面没渲染完）';
    if (href.indexOf('/job_detail/') < 0) return 'not-detail-url: 当前页不是 /job_detail/ 详情页';
    if (out.detailMatched === false) {
      return 'detail-mismatch: 沟通按钮 ka=' + out.chatKa + ' 不含本页岗位 ' + out.detailJobId + '（右栏还停在上一个岗位）';
    }
    return 'no-anchor: 职位描述/公司介绍标题、工商信息块、标题/薪资/公司选择器全部未命中（BOSS 改版或登录墙）';
  }

  function extractDetail() {
    var out = {
      entry: 'detail',
      url: String(location.href || ''),
      ready: document.readyState === 'complete',
      hasBody: false,
      login: probeLogin(),
      restricted: probeRestricted(),
      title: '',
      titleSource: '',
      salary: '',
      company: '',
      gsCompany: '',
      tags: [],
      bossActive: '',
      bossName: '',
      bossTitle: '',
      jobDescription: '',
      jdFrom: '',
      introduce: '',
      introFrom: '',
      detailJobId: '',
      chatKa: '',
      detailMatched: null,
      foundFields: [],
      missing: [],
      detailFound: false,
      detailFail: '',
      stamp: Date.now()
    };
    try { out.hasBody = compact(document.body && document.body.innerText).length > 0; } catch (e) { out.hasBody = false; }

    var paneEl = q(document, S.DETAIL_PANE);
    var pane = paneEl || document;
    var body = String((paneEl && paneEl.innerText) || (document.body && document.body.innerText) || '');

    var gate = readChatGate();
    out.detailJobId = gate.jobId;
    out.chatKa = gate.chatKa;
    out.detailMatched = gate.matched;

    // 标题：选择器命中才算「页面自证」；document.title 只当展示兜底，不计入 foundFields
    var titleSel = txt(q(document, S.DETAIL_TITLE));
    if (titleSel) {
      out.title = titleSel;
      out.titleSource = 'selector';
    } else {
      var docTitle = compact(String(document.title || '').split(/[-_|]/)[0]);
      if (docTitle) { out.title = docTitle; out.titleSource = 'document-title'; }
    }

    out.salary = pickReadableSalary(txt(q(document, S.DETAIL_SALARY)));
    out.company = txt(q(document, S.DETAIL_COMPANY));
    out.gsCompany = pickGsCompanyName();

    var tagNodes = qa(document, S.DETAIL_TAGS);
    for (var t = 0; t < tagNodes.length && out.tags.length < 20; t++) {
      var tag = txt(tagNodes[t]);
      if (tag) out.tags.push(tag);
    }

    out.bossActive = readHrActive(q(document, S.DETAIL_BOSS_PANE) || document);
    out.bossName = stripHrDecorations(q(document, S.DETAIL_BOSS_NAME));
    out.bossTitle = hrTitleFromAttr(txt(q(document, S.DETAIL_BOSS_ATTR)));

    var jd = readDetailSection(pane, body, T.sectionJd, new RegExp(T.jdStopRe),
      T.jdMinLen, T.jdMaxLen);
    out.jobDescription = jd.text;
    out.jdFrom = jd.from;

    var intro = readDetailSection(pane, body, T.sectionIntro, new RegExp(T.introStopRe),
      T.introMinLen, T.introMaxLen);
    out.introduce = intro.text;
    out.introFrom = intro.from;

    var found = [];
    if (titleSel) found.push('title');
    if (out.salary) found.push('salary');
    if (out.company) found.push('company');
    if (out.gsCompany) found.push('gsCompany');
    if (out.bossName) found.push('bossName');
    if (out.tags.length) found.push('tags');
    if (out.jobDescription) found.push('jobDescription');
    if (out.introduce) found.push('introduce');
    out.foundFields = found;
    out.detailFound = found.length > 0;

    var missing = [];
    if (!titleSel) missing.push('title');
    if (!out.jobDescription) missing.push('jobDescription');
    if (!out.introduce) missing.push('introduce');
    if (!out.gsCompany) missing.push('gsCompany');
    out.missing = missing;

    if (!out.detailFound) out.detailFail = detailFailReason(out);
    return out;
  }

  // ── 对外入口 ────────────────────────────────────────────────
  window.__jobkitExtractList = extractList;
  window.__jobkitExtractDetail = extractDetail;

  var result;
  try {
    result = (ENTRY === 'detail') ? extractDetail() : extractList();
  } catch (e) {
    result = {
      entry: ENTRY,
      url: String(location.href || ''),
      error: String((e && e.message) || e),
      stamp: Date.now()
    };
  }
  return JSON.stringify(result);
})()
