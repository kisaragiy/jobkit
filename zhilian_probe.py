#!/usr/bin/env python3
"""智联真机结构探针：把分栏布局的**真实 HTML** 挖出来，据此标定选择器。

为什么不能靠快照：markdown 化的页面内容能看出「有哪些字段」，
但看不出 class 名、层级、哪个节点是卡片根。选择器只能从真 HTML 标定。

用法：
    python zhilian_probe.py                       # 用默认广州 AI 开发
    python zhilian_probe.py "<完整URL>"
    python zhilian_probe.py --html out.html       # 把卡片 HTML 落到指定文件

输出：
    1. 各种候选选择器的命中数量（判断哪种布局）
    2. 第一张卡片根节点的 class 与 outerHTML（默认落 tmp/zhilian_card.html）
    3. 卡片内所有 class 名的去重清单（写选择器时的原料）

不登录也能跑，但未登录时薪资是脱敏的（`**-**元`），字段结构仍然可读。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dom_channel as DC          # noqa: E402
import dom_probe as DP            # noqa: E402  复用浏览器启动
from dom import zhilian_urls as ZU  # noqa: E402

DEFAULT_URL = "https://www.zhaopin.com/jobs/?pageMode=search&jl=763&kw=AI%E5%BC%80%E5%8F%91"

PROBE_JS = r"""
(function () {
  var out = {
    url: String(window.location.href || ''),
    title: String(document.title || ''),
    ready: document.readyState,
    probes: {},
    cardRootClass: '',
    cardHtml: '',
    classNames: [],
    anchors: []
  };

  var CANDIDATES = {
    'classic.positionlist': 'div.positionlist',
    'classic.joblist-box__item': 'div.joblist-box__item',
    'split.layout': '.job-split-layout__left, .job-list-panel',
    'split.job-card': '.job-list-panel .job-card',
    'any.job-card': '[class*="job-card"]',
    'any.position': '[class*="position"]',
    'a.jobdetail': 'a[href*="/jobdetail/"]',
    'login.cta': 'a.home-header__c-no-login, .page-header__not-login, [class*="c-no-login"]',
    'login.user': '.home-header__c-login, .header-nav__c-login, .page-header__user__logged-in'
  };
  for (var k in CANDIDATES) {
    try { out.probes[k] = document.querySelectorAll(CANDIDATES[k]).length; }
    catch (e) { out.probes[k] = -1; }
  }

  // 卡片样本 + Vue 实例探测（智联卡片不放 job_id/href，数据大概率在 Vue 组件里）
  var cards = document.querySelectorAll('.job-list-panel .job-card');
  out.cardSamples = [];
  for (var ci = 0; ci < cards.length && ci < 3; ci++) {
    var c = cards[ci];
    var tEl = c.querySelector('.job-card__title-clamp');
    var sEl = c.querySelector('.job-card__salary');
    var coEl = c.querySelector('.job-card__company-name');
    var locEl = c.querySelector('.job-card__location span');
    out.cardSamples.push({
      title: tEl ? String(tEl.textContent || '').trim().slice(0, 40) : '',
      aria: tEl ? String(tEl.getAttribute('aria-label') || '').slice(0, 40) : '',
      htmlLen: tEl ? String(tEl.innerHTML || '').length : 0,
      salary: sEl ? String(sEl.textContent || '').trim() : '',
      company: coEl ? String(coEl.textContent || '').trim().slice(0, 30) : '',
      location: locEl ? String(locEl.getAttribute('title') || locEl.textContent || '').trim() : '',
      vueKeys: Object.keys(c).filter(function (k) { return k.indexOf('__vue') === 0; })
    });
  }

  if (cards.length) {
    var c0 = cards[0];
    out.vueKeysFirst = Object.keys(c0).filter(function (k) { return k.indexOf('__vue') === 0; });
    for (var vi = 0; vi < out.vueKeysFirst.length; vi++) {
      try {
        var inst = c0[out.vueKeysFirst[vi]];
        if (!inst) continue;
        out.vueInfo = out.vueInfo || {};
        out.vueInfo['keys_' + vi] = Object.keys(inst).slice(0, 30);
        var data = inst.$data || inst.data || null;
        if (data) out.vueInfo['dataKeys_' + vi] = Object.keys(data).slice(0, 30);
        // 找形如 job / position 的对象
        var probeKeys = ['job', 'jobInfo', 'position', 'item', 'data'];
        for (var pi = 0; pi < probeKeys.length; pi++) {
          var pk = probeKeys[pi];
          var cand = data ? data[pk] : null;
          if (cand && typeof cand === 'object') {
            out.vueInfo['candidate_' + pk] = JSON.stringify(cand).slice(0, 800);
          }
        }
      } catch (e) { out.vueInfo = out.vueInfo || {}; out.vueInfo['err_' + vi] = String(e).slice(0, 80); }
    }
  } else {
    out.vueKeysFirst = [];
  }

  // 全局 store 线索
  try {
    out.hasPinia = !!document.querySelector('#app') && typeof window.__VUE__ !== 'undefined';
    out.windowKeysWithData = Object.keys(window).filter(function (k) {
      return /store|state|initial|__NUXT|__NEXT|vue/i.test(k);
    }).slice(0, 20);
  } catch (e) { /* ignore */ }

  // __INITIAL_STATE__：智联把列表数据放在这里（DOM 文本被脱敏/延迟渲染）
  try {
    var st = window.__INITIAL_STATE__ || {};
    out.stateKeys = Object.keys(st).slice(0, 50);
    out.jobArrays = [];
    var walk = function (o, path, depth) {
      if (depth > 4 || !o || typeof o !== 'object' || out.jobArrays.length > 4) return;
      var keys;
      try { keys = Object.keys(o); } catch (e) { return; }
      for (var i = 0; i < keys.length && i < 60; i++) {
        var k = keys[i], v;
        try { v = o[k]; } catch (e) { continue; }
        if (!v || typeof v !== 'object') continue;
        if (Array.isArray(v) && v.length && v[0] && typeof v[0] === 'object') {
          var fk = Object.keys(v[0]);
          var hit = false;
          for (var j = 0; j < fk.length; j++) {
            if (/job|position|number/i.test(fk[j])) { hit = true; break; }
          }
          if (hit) {
            out.jobArrays.push({
              path: path + '.' + k,
              len: v.length,
              keys: fk.slice(0, 25),
              sample: JSON.stringify(v[0]).slice(0, 700)
            });
          }
        }
        walk(v, path + '.' + k, depth + 1);
      }
    };
    walk(st, 'state', 0);

    // 完整字段清单（写提取器要用）
    var plist = st.positionList || [];
    out.positionKeys = plist.length ? Object.keys(plist[0]).sort() : [];
    out.positionOne = plist.length ? JSON.stringify(plist[0]).slice(0, 3500) : '';
    out.detailKeys = (st.jobDetail && typeof st.jobDetail === 'object') ? Object.keys(st.jobDetail).sort() : [];
    out.meta = {
      pageIndex: st.pageIndex, pageSize: st.pageSize,
      positionCount: st.positionCount, hasMore: st.hasMore,
      queryParams: st.queryParams, listEmptyReason: st.listEmptyReason
    };
  } catch (e) { out.stateErr = String(e).slice(0, 120); }


  var card = document.querySelector('.job-list-panel .job-card')
          || document.querySelector('[class*="job-card"]');
  if (card) {
    out.cardRootClass = String(card.className || '');
    out.cardHtml = String(card.outerHTML || '').slice(0, 7000);
    out.cardAttrs = {};
    for (var ai = 0; ai < card.attributes.length; ai++) {
      var at = card.attributes[ai];
      out.cardAttrs[at.name] = String(at.value || '').slice(0, 200);
    }
    var seen0 = {};
    var nodes0 = card.querySelectorAll('*');
    for (var j0 = 0; j0 < nodes0.length && j0 < 400; j0++) {
      var parts0 = String(nodes0[j0].className || '').split(/\s+/);
      for (var p0 = 0; p0 < parts0.length; p0++) { if (parts0[p0]) seen0[parts0[p0]] = 1; }
    }
    out.classNames = Object.keys(seen0).slice(0, 120);
  }

  // 右侧详情面板（第一张卡被点开时才有）
  var panel = document.querySelector('.job-detail-panel');
  if (panel) {
    out.panelHtml = String(panel.outerHTML || '').slice(0, 3000);
  }

  var anchors = document.querySelectorAll('a[href*="/jobdetail/"]');
  for (var n = 0; n < anchors.length && n < 3; n++) {
    out.anchors.push({
      href: String(anchors[n].getAttribute('href') || ''),
      text: String(anchors[n].textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
      cls: String(anchors[n].className || '')
    });
  }

  return JSON.stringify(out);
})()
"""


def main() -> int:
    args = sys.argv[1:]
    url = DEFAULT_URL
    html_out = HERE / "tmp" / "zhilian_card.html"

    i = 0
    while i < len(args):
        if args[i] == "--html" and i + 1 < len(args):
            html_out = Path(args[i + 1])
            i += 2
            continue
        if not args[i].startswith("--"):
            url = args[i]
        i += 1

    browser = DP.find_browser()
    if not browser:
        print("❌ 没找到 Chrome/Edge")
        return 1
    if not DP.cdp_alive():
        print(f"启动浏览器（独立 profile）：{browser}")
        DP.launch(browser)
        import time
        for _ in range(15):
            if DP.cdp_alive():
                break
            time.sleep(0.5)

    print(f"导航：{url}")
    try:
        snap = DC._run("list", navigate_to=url, timeout=45.0, settle=2.5, js=PROBE_JS)
    except DC.DomChannelError as e:
        print(f"❌ {e.reason}: {e}")
        return 2

    print("\n=== 页面")
    print(f"  title : {snap.get('title')}")
    print(f"  ready : {snap.get('ready')}")
    print("\n=== 选择器命中")
    for k, v in (snap.get("probes") or {}).items():
        flag = "✅" if isinstance(v, int) and v > 0 else "  "
        print(f"  {flag} {k:28s} {v}")
    print(f"\n=== 卡片样本（前 3 张）")
    for i, s in enumerate(snap.get("cardSamples") or [], 1):
        print(f"  [{i}] title={s.get('title')!r} aria={s.get('aria')!r} htmlLen={s.get('htmlLen')}")
        print(f"      salary={s.get('salary')!r} company={s.get('company')!r} loc={s.get('location')!r}")
        print(f"      vueKeys={s.get('vueKeys')}")
    vi = snap.get("vueInfo") or {}
    if vi:
        print("\n=== Vue 实例探测")
        for k, v in vi.items():
            print(f"  {k}: {v}")
    print(f"\n=== window 上的 store/state 线索\n  {snap.get('windowKeysWithData')}")
    print(f"\n=== __INITIAL_STATE__ keys\n  {snap.get('stateKeys')}")
    for ja in snap.get("jobArrays") or []:
        print(f"\n=== 找到岗位数组 {ja.get('path')}（{ja.get('len')} 条）")
        print(f"  keys: {ja.get('keys')}")
        print(f"  sample: {ja.get('sample')}")
    if snap.get("stateErr"):
        print(f"  stateErr: {snap.get('stateErr')}")
    print(f"\n=== positionList 字段（{len(snap.get('positionKeys') or [])} 个）")
    print(f"  {snap.get('positionKeys')}")
    print(f"\n=== 分页元信息\n  {snap.get('meta')}")
    print(f"\n=== jobDetail 字段\n  {snap.get('detailKeys')}")
    print(f"\n=== 第一条完整记录")
    print(f"  {snap.get('positionOne')}")

    print(f"\n=== 卡片根 class\n  {snap.get('cardRootClass')!r}")
    attrs = snap.get("cardAttrs") or {}
    if attrs:
        print("\n=== 卡片属性（job_id 可能藏在这里）")
        for k, v in attrs.items():
            print(f"  {k} = {v!r}")
    print(f"\n=== 前 3 个 jobdetail 链接")
    for a in snap.get("anchors") or []:
        print(f"  {a.get('cls')!r:30s} {a.get('href')[:70]}")
        print(f"      text={a.get('text')!r}")

    classes = snap.get("classNames") or []
    print(f"\n=== 卡片内 class（{len(classes)} 个）")
    for c in classes:
        print(f"  {c}")

    html = snap.get("cardHtml") or ""
    if html:
        html_out.parent.mkdir(parents=True, exist_ok=True)
        html_out.write_text(html, encoding="utf-8")
        print(f"\n卡片 HTML（{len(html)} 字符）已落盘：{html_out}")
    else:
        print("\n⚠️ 没拿到卡片 HTML —— 可能页面还没渲染完，或者不在列表页")

    panel = snap.get("panelHtml") or ""
    if panel:
        panel_out = html_out.with_name("zhilian_panel.html")
        panel_out.write_text(panel, encoding="utf-8")
        print(f"详情面板 HTML（{len(panel)} 字符）已落盘：{panel_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
