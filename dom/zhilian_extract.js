/*
 * dom/zhilian_extract.js — 智联招聘（zhaopin.com）DOM 提取模板
 *
 * 由 dom/zhilian_selectors.extract_js() 渲染：替换 __JOBKIT_SELECTORS__ / __JOBKIT_ENTRY__。
 * 返回值只报告「页面事实」，不在 JS 里决定 reason —— listFound / cardCount / login
 * 分开报，让 Python 侧能区分「选择器失效」和「今天真没岗」。
 *
 * 布局（两种，来自智联页面实际形态）：
 *   classic —— /sou/...         div.positionlist > div.joblist-box__item
 *   split   —— /jobs?...        .job-split-layout__left / .job-list-panel > .job-card
 *
 * ⚠️ split（新版左右分栏）目前只做了「识别 + 报 unsupported」，因为现有证据里
 *    只拿到卡片容器类名，字段级选择器没有出处。宁可大声说不支持，也不猜。
 *
 * 注意：不要在本文件里写 `var location = ...`，会遮蔽 window.location（历史坑）。
 */
(function () {
  'use strict';

  var S = __JOBKIT_SELECTORS__;
  var ENTRY = __JOBKIT_ENTRY__;

  function squash(s) {
    return String(s == null ? '' : s).replace(/[\s\u00a0]+/g, ' ').trim();
  }

  function textOf(el) {
    return el ? squash(el.textContent || '') : '';
  }

  function q(root, sel) {
    if (!root || !sel) return null;
    try { return root.querySelector(sel); } catch (e) { return null; }
  }

  function qa(root, sel) {
    if (!root || !sel) return [];
    try { return Array.prototype.slice.call(root.querySelectorAll(sel)); } catch (e) { return []; }
  }

  function attrOf(el, name) {
    if (!el || !el.getAttribute) return '';
    return String(el.getAttribute(name) || '');
  }

  /** 智联详情页形态：/jobdetail/{id}.htm */
  function jobIdFromUrl(u) {
    var m = String(u || '').match(/\/jobdetail\/([A-Za-z0-9_~%.\-]+?)\.htm/i);
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  /** 绝对 URL：卡片有时给相对路径 */
  function absoluteUrl(href) {
    var h = String(href || '').trim();
    if (!h) return '';
    try { return new URL(h, window.location.href).href; } catch (e) { return h; }
  }

  function probeLogin() {
    var cta = q(document, S.NO_LOGIN);
    var head = q(document, S.USER_HEADER);
    return {
      loggedOutCta: textOf(cta).slice(0, 40),
      headerFound: !!head,
      loginUrl: /passport\.zhaopin\.com\/login/i.test(String(window.location.href || ''))
    };
  }

  /** 智联的风控/验证页：命中就整个作废，不假装没数据
   *  特征出处：page-challenge.js:29-49 */
  function probeChallenge() {
    var hits = [];
    if (q(document, S.CHALLENGE_WIDGET)) hits.push('widget');
    var bodyText = (document.body && document.body.textContent) || '';
    if (S.CHALLENGE_TEXT_RE) {
      try {
        if (new RegExp(S.CHALLENGE_TEXT_RE).test(bodyText.slice(0, 4000))) hits.push('text');
      } catch (e) { /* 正则坏了不该让整页失败 */ }
    }
    if (S.CHALLENGE_TITLE_RE) {
      try {
        if (new RegExp(S.CHALLENGE_TITLE_RE).test(String(document.title || ''))) hits.push('title');
      } catch (e) { /* 同上 */ }
    }
    return { hit: hits.join(',') };
  }

  function dig(obj, path, dflt) {
    var cur = obj;
    var parts = String(path || '').split('.');
    for (var i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== 'object') return dflt;
      cur = cur[parts[i]];
    }
    return cur == null ? dflt : cur;
  }

  function parseJson(s) {
    if (!s || typeof s !== 'string') return null;
    try { return JSON.parse(s); } catch (e) { return null; }
  }

  function stateOf() {
    try {
      var st = window.__INITIAL_STATE__;
      return (st && typeof st === 'object') ? st : null;
    } catch (e) { return null; }
  }

  /**
   * positionList 的一条 → jobkit 字段（真机实测的 127 字段里挑有用的）。
   * 只做映射与清洗，不做业务判断（过滤/打分在 Python 侧）。
   */
  function mapPosition(p) {
    if (!p || typeof p !== 'object') return null;
    var base = dig(p, 'jobDetailData.position.base', {}) || {};
    var desc = dig(p, 'jobDetailData.position.desc', {}) || {};
    var custom = parseJson(p.cardCustomJson) || {};

    var jobId = squash(base.positionNumber) || squash(p.number) || squash(p.jobId);
    var title = squash(base.positionName) || squash(p.name);
    if (!jobId || !title) return null;

    var salary = squash(base.salary) || squash(p.salary60) || squash(p.salaryReal);
    if (!salary) salary = squash(custom.salary60);

    var url = squash(p.positionURL) || squash(p.positionUrl) || squash(base.positionUrl);
    if (!url) url = 'https://www.zhaopin.com/jobdetail/' + encodeURIComponent(jobId) + '.htm';

    var jobDesc = squash(desc.description) || squash(p.jobDescription) || squash(p.jobSummary);
    var loc = splitCity(custom.address);

    return {
      job_id: jobId,
      security_id: '',
      title: title,
      company: squash(p.companyName) || squash(custom.companyName),
      salary: salary,
      city: loc.city,
      city_id: squash(p.cityId),
      district: squash(p.cityDistrict) || loc.district,
      experience: squash(base.positionWorkingExp) || squash(p.workingExp),
      education: squash(base.education) || squash(p.education),
      skills: Array.isArray(p.jobSkillTags) ? p.jobSkillTags.join('/').slice(0, 300) : '',
      industry: squash(p.industryName),
      scale: squash(p.companySize),
      boss_name: '',
      boss_title: '',
      boss_active: '',
      job_description: jobDesc,
      url: url,
      applied: !!p.hasAppliedPosition,
      collected: !!p.collected,
      company_number: squash(p.companyNumber)
    };
  }

  function detectLayout() {
    // split 优先判断：新版分栏页里没有 classic 的容器
    if (document.querySelector('.job-list-panel') || document.querySelector('.job-split-layout__left')) {
      return 'split';
    }
    if (q(document, S.JOB_LIST)) return 'classic';
    return '';
  }

  /** "广州·天河区" / "广州" / "深圳 南山区" → city + district */
  function splitCity(raw) {
    var s = squash(raw);
    if (!s) return { city: '', district: '' };
    var parts = s.split(/[·\-\s\/]+/).filter(function (x) { return !!x; });
    return { city: parts[0] || '', district: parts[1] || '' };
  }

  /** 经典布局单卡 */
  function classicCard(card) {
    var titleEl = q(card, S.JOB_TITLE);
    var href = absoluteUrl(attrOf(titleEl, 'href'));
    var loc = splitCity(textOf(q(card, S.JOB_LOCATION)));
    var infoItems = qa(card, S.JOB_INFO_ITEM);
    return {
      job_id: jobIdFromUrl(href),
      security_id: '',                 // 智联没有 boss 的 securityId 概念
      title: textOf(titleEl),
      company: textOf(q(card, S.COMPANY_NAME)),
      salary: textOf(q(card, S.JOB_SALARY)),
      city: loc.city,
      district: loc.district,
      experience: textOf(infoItems[1]) || '',
      education: textOf(infoItems[2]) || '',
      skills: '',
      industry: '',
      scale: '',
      boss_name: '',
      boss_title: '',
      boss_active: textOf(q(card, S.HR_ACTIVE)).slice(0, 20),
      url: href,
      layout: 'classic'
    };
  }

  function extractList() {
    var out = {
      entry: 'list',
      platform: 'zhilian',
      url: String(window.location.href || ''),
      ready: document.readyState === 'complete',
      hasBody: !!(document.body),
      login: probeLogin(),
      restricted: probeChallenge(),
      layout: '',
      listFound: false,
      source: '',
      cardCount: 0,
      count: 0,
      dropped: 0,
      emptyMarker: false,
      unsupported: '',
      paging: {},
      jobs: [],
      stamp: Date.now()
    };

    out.layout = detectLayout();

    // ── 主路径：__INITIAL_STATE__.positionList ──
    // 真机实测：卡片没有 job_id / href、标题文本为空、薪资被遮罩成 **-**元，
    // 但 state 里每条有 127 个字段（含 JD 全文与真实薪资）。这才是智联的正确读法。
    var st = stateOf();
    var plist = (st && Array.isArray(st.positionList)) ? st.positionList : null;

    if (plist) {
      out.listFound = true;
      out.source = 'state';
      out.cardCount = plist.length;
      out.paging = {
        pageIndex: st.pageIndex,
        pageSize: st.pageSize,
        hasMore: !!st.hasMore,
        queryParams: st.queryParams || null,
        listEmptyReason: st.listEmptyReason || ''
      };
      for (var i = 0; i < plist.length && out.jobs.length < 200; i++) {
        var job = null;
        try { job = mapPosition(plist[i]); } catch (e) { job = null; }
        if (!job) { out.dropped++; continue; }
        out.jobs.push(job);
      }
      out.count = out.jobs.length;
      out.emptyMarker = plist.length === 0;
      if (out.jobs.length === 0 && plist.length > 0) {
        out.unsupported = 'state-shape-changed';   // 有数据但映射不出来 = 字段级改版
      }
      return out;
    }

    // ── 兜底：state 不可用（老版 /sou/ 页面）时走 DOM ──
    if (out.layout === 'classic') {
      var cards = qa(document, S.JOB_CARD);
      out.listFound = true;
      out.source = 'dom';
      out.cardCount = cards.length;
      for (var k = 0; k < cards.length; k++) {
        var cj = null;
        try { cj = classicCard(cards[k]); } catch (e) { cj = null; }
        if (!cj || !cj.title || !cj.job_id) { out.dropped++; continue; }
        out.jobs.push(cj);
      }
      out.count = out.jobs.length;
      out.emptyMarker = cards.length === 0;
      return out;
    }

    // 既没 state 也没已知布局 → 明确「不认识」，交给 Python 判 selector-miss
    if (!out.hasBody) return out;
    out.unsupported = out.layout ? 'dom-unusable' : 'no-known-layout';
    return out;
  }

  /**
   * 详情：state.jobDetail 有数据就直接用，不再依赖详情页选择器。
   * 实测形状：{detailedCompany, detailedPosition, guestJobDescClamp, taskId}
   */
  function extractDetail() {
    var st = stateOf();
    var jd = st && st.jobDetail;
    var out = {
      entry: 'detail',
      platform: 'zhilian',
      url: String(window.location.href || ''),
      ready: document.readyState === 'complete',
      login: probeLogin(),
      restricted: probeChallenge(),
      jobId: jobIdFromUrl(window.location.href),
      detailFound: false,
      source: '',
      unsupported: '',
      stamp: Date.now()
    };

    if (jd && typeof jd === 'object') {
      var base = dig(jd, 'detailedPosition.base', {}) || {};
      var desc = dig(jd, 'detailedPosition.desc', {}) || {};
      var company = dig(jd, 'detailedCompany', {}) || {};
      out.source = 'state';
      out.jobId = squash(base.positionNumber) || out.jobId;
      out.title = squash(base.positionName);
      out.salary = squash(base.salary);
      out.education = squash(base.education);
      out.experience = squash(base.positionWorkingExp);
      out.company = squash(company.companyName) || squash(company.name);
      out.job_description = squash(desc.description);
      out.detailFound = !!(base.positionNumber || base.positionName);
      if (!out.detailFound) out.unsupported = 'detail-state-shape-changed';
      return out;
    }

    out.unsupported = 'detail-state-missing';
    return out;
  }

  var snap = (ENTRY === 'detail') ? extractDetail() : extractList();
  return JSON.stringify(snap);
})()
