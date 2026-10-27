#!/usr/bin/env python3
"""智联招聘 DOM 通道（jobkit 的第二个采集平台）。

设计原则：
  · 复用 dom_channel 的 CDP 连接 / 导航 / 注入层（不复制一套浏览器代码）
  · reason 契约与 BOSS 通道**完全一致**，上层路由不需要知道平台差异
  · 取不到就大声失败，绝不把「布局不认识」说成「今天没岗」

与 BOSS 的差异（真实存在，不是缺陷）：
  · 智联没有 securityId 概念 → security_id 恒为空，不影响后续投递（它不是 CLI 必需品）
  · 详情页字段选择器暂无证据 → fetch_detail 直接返回 unsupported
  · 有新版左右分栏布局，字段选择器暂无证据 → 报 selector-miss 并注明 unsupported
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dom_channel as DC          # noqa: E402  复用 CDP 层与 reason 契约
from dom import zhilian_selectors as Z  # noqa: E402

PLATFORM = Z.PLATFORM_ID
MAX_JOBS = 200

REASON_OK = DC.REASON_OK
REASON_EMPTY = DC.REASON_EMPTY
REASON_SELECTOR_MISS = DC.REASON_SELECTOR_MISS
REASON_NOT_LOGGED_IN = DC.REASON_NOT_LOGGED_IN
REASON_RESTRICTED = DC.REASON_RESTRICTED
REASON_PAGE_ERROR = DC.REASON_PAGE_ERROR


def _clean(v) -> str:
    return " ".join(str(v or "").split()).strip()


def normalize_job(raw: dict) -> dict | None:
    """智联卡片 → jobkit 的 jobs 表字段。返回 None = 这条不要（缺关键字段）。"""
    if not isinstance(raw, dict):
        return None
    job_id = _clean(raw.get("job_id"))
    title = _clean(raw.get("title"))
    if not job_id or not title:
        return None
    return {
        "job_id": job_id,
        "security_id": "",            # 智联无此概念
        "title": title,
        "company": _clean(raw.get("company")),
        "salary": _clean(raw.get("salary")),
        "city": _clean(raw.get("city")),
        "district": _clean(raw.get("district")),
        "experience": _clean(raw.get("experience")),
        "education": _clean(raw.get("education")),
        "skills": "",
        "industry": "",
        "scale": "",
        # 智联卡片里 companyinfo 区块不保证是 HR 本人，所以不冒充 boss_active
        "boss_name": "",
        "boss_title": "",
        "boss_active": "",
        "platform": PLATFORM,
        "url": _clean(raw.get("url")),
        # JD 全文：智联 state 里直接有，不用再进详情页（GetJob 做不到这一点）
        # 保留换行（JD 的分段是有意义的结构），只去首尾空白
        "job_description": str(raw.get("job_description") or "").strip(),
        "city_id": _clean(raw.get("city_id")),
        "company_number": _clean(raw.get("company_number")),
    }


def classify_page(snap: dict) -> str:
    """把页面事实映射成 reason。顺序有讲究：先排除「不是这个页面」的情况。"""
    if not isinstance(snap, dict):
        return REASON_PAGE_ERROR

    # 风控页最优先：命中就整页作废，哪怕看起来有数据
    if (snap.get("restricted") or {}).get("hit"):
        return REASON_RESTRICTED

    # 未登录**不阻断采集**：真机实测未登录时 state.positionList 依然有完整数据
    # （含 JD 全文与真实薪资，DOM 只是把薪资显示成 **-**元）。
    # 登录只影响「投递/打招呼」，不影响「发现」——把能拿的数据丢掉才是错的。
    has_state_data = (snap.get("source") == "state") and bool(snap.get("jobs"))
    if has_state_data:
        return REASON_OK

    login = snap.get("login") or {}
    if login.get("loginUrl") or login.get("loggedOutCta"):
        return REASON_NOT_LOGGED_IN

    if snap.get("unsupported"):
        # 布局不认识 = 我们取不到，不是没岗
        return REASON_SELECTOR_MISS

    if not snap.get("listFound"):
        return REASON_SELECTOR_MISS

    if int(snap.get("cardCount") or 0) == 0:
        return REASON_EMPTY

    return REASON_OK


def fetch_jobs(url: str, limit: int = 30):
    """抓一个智联列表页。返回 (ok, jobs, reason)，语义与 dom_channel.fetch_jobs 一致。"""
    if not str(url or "").strip():
        return False, None, REASON_PAGE_ERROR
    if not Z.is_zhilian_url(url):
        return False, None, REASON_PAGE_ERROR

    try:
        snap = DC._run("list", navigate_to=str(url).strip(), js=Z.extract_js("list"))
    except DC.DomChannelError as e:
        return False, None, e.reason

    reason = classify_page(snap)
    if reason == REASON_EMPTY:
        return True, [], REASON_EMPTY
    if reason != REASON_OK:
        return False, None, reason

    jobs = []
    dropped = 0
    for raw in (snap.get("jobs") or [])[:MAX_JOBS]:
        job = normalize_job(raw)
        if job:
            jobs.append(job)
        else:
            dropped += 1

    # 卡片在、但一条都归一不出来 = 字段级改版，仍然是选择器失效
    if not jobs:
        return False, None, REASON_SELECTOR_MISS

    try:
        limit_n = max(0, int(limit))
    except (TypeError, ValueError):
        limit_n = 30
    return True, jobs[:limit_n], REASON_OK


def fetch_detail(url: str):
    """智联详情页。当前**故意不可用**：字段选择器无证据，返回 unsupported。

    返回 (ok, job, reason)。调用方应当把它当作 selector-miss 处理。
    """
    return False, None, REASON_SELECTOR_MISS


def probe(url: str = "https://www.zhaopin.com/sou/") -> dict:
    """探活：报告当前页面能不能被我们读懂（给 dom_probe 之类的工具用）。"""
    out = {"platform": PLATFORM, "url": url, "ok": False, "reason": REASON_PAGE_ERROR, "detail": {}}
    try:
        snap = DC._run("list", navigate_to=url, js=Z.extract_js("list"))
    except DC.DomChannelError as e:
        out["reason"] = e.reason
        out["error"] = str(e)
        return out
    reason = classify_page(snap)
    out["ok"] = reason in (REASON_OK, REASON_EMPTY)
    out["reason"] = reason
    out["detail"] = {
        "layout": snap.get("layout"),
        "listFound": snap.get("listFound"),
        "cardCount": snap.get("cardCount"),
        "count": snap.get("count"),
        "dropped": snap.get("dropped"),
        "unsupported": snap.get("unsupported"),
        "login": snap.get("login"),
        "restricted": snap.get("restricted"),
        "sample": (snap.get("jobs") or [])[:2],
    }
    return out


if __name__ == "__main__":
    import json
    target = sys.argv[1] if len(sys.argv) > 1 else "https://www.zhaopin.com/sou/"
    print(json.dumps(probe(target), ensure_ascii=False, indent=2))
