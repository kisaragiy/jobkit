"""智联招聘（zhaopin.com）DOM 选择器表 + 提取脚本渲染。

⚠️ 独立性说明：下面的选择器是**参考实现**（GetJob 2.3.1）里的字符串，只作为
「智联页面长什么样」的证据来源。jobkit 不 import 它、不读它的路径、运行时无任何依赖。
验收标准是真机命中率，不是「GetJob 里有没有」。

出处（✅ 查证过，文件:行号）：
  zhilian/selectors.js:4-24        列表卡片 / 字段 / 登录态 / 投递按钮
  zhilian/page-challenge.js:29-49  人机验证页的文案正则与控件选择器
  zhilian/urls.js:8-31             列表路径 /sou/、/jobs?、详情 /jobdetail/{id}.htm
  zhilian/job-mapper.js:9,49,386    jobId 从 jobdetail/{id}.htm 提取

⚠️ 已知缺口（故意留空，不是忘了）：
  · 详情页字段选择器**无出处** → detail 入口返回 unsupported，不用空串冒充成功
  · 新版左右分栏 /jobs? 只拿到卡片容器，字段选择器无出处 → 报 unsupported
"""
from __future__ import annotations

import json
from pathlib import Path

JS_TEMPLATE = Path(__file__).parent / "zhilian_extract.js"

PLATFORM_ID = "zhilian"
PLATFORM_NAME = "智联招聘"
SITE_HOST = "zhaopin.com"

# ── 列表（经典布局 /sou/）────────────────────────────────────────
# 出处：zhilian/selectors.js:4-9
SELECTORS = {
    "JOB_LIST": "div.positionlist",
    "JOB_CARD": "div.joblist-box__item",
    "JOB_TITLE": "a.jobinfo__name",
    "JOB_SALARY": "p.jobinfo__salary",
    "JOB_LOCATION": "div.jobinfo__other-info div.jobinfo__other-info-item > span",
    "COMPANY_NAME": 'a.companyinfo__name, div.companyinfo__name, [class*="companyinfo__name"]',
    "HR_ACTIVE": '[class*="companyinfo"]',

    # 机制等价改写：原文用 `nth-child(2)` / `nth-child(3)` 取经验/学历，
    # 这里改成「取所有 item 再按下标读」，避免 selector 依赖兄弟顺序（更抗改版）。
    # 出处：zhilian/selectors.js:7-8
    "JOB_INFO_ITEM": "div.jobinfo__other-info-item",

    # 新版左右分栏（只用于「识别」，不用于取字段）
    # 出处：zhilian/selectors.js:20-24
    "JOBS_SPLIT_LAYOUT": ".job-split-layout__left, .job-list-panel",
    "JOBS_SPLIT_CARD": ".job-list-panel .job-card",

    # 登录态
    # 出处：zhilian/selectors.js:14-15
    "NO_LOGIN": 'a.home-header__c-no-login, .page-header__not-login, [class*="c-no-login"]',
    "USER_HEADER": (".home-header__c-login, .header-nav__c-login, span.c-login__top__name, "
                    "article.login-after, .page-header, .page-header__user__logged-in"),
}

# ── 人机验证 / 风控页 ────────────────────────────────────────────
# 出处：zhilian/page-challenge.js:41-49（控件）与 :29-30（文案）
CHALLENGE_WIDGET = (
    "p.waf-nc-title, .aliyun-captcha, #nocaptcha, "
    "#tcaptcha_iframe_eo, #tcaptcha_wrapper_transform_eo, "
    '[class*="captcha"], [id*="captcha"]'
)
CHALLENGE_TEXT_RE = (
    "你是不是真人|认证你是真人|证明你是真人|确认你是真人|确认您是真人|你是真人|"
    "不是机器人|真人验证|智能验证|点击按钮开始验证|点击完成验证|请点击验证|点我验证|"
    "安全验证|访问验证|请完成验证|人机验证|请按住滑块|滑动验证|异常验证|"
    "请拖动滑块|向右滑动|正在验证连接安全性|请勾选下方复选框|Security Verification|EdgeOne"
)
CHALLENGE_TITLE_RE = "真人验证|安全验证|访问验证|人机验证|智能验证|你是不是真人|Security Verification|EdgeOne"


def selectors_json() -> str:
    """渲染进 JS 的选择器表（含正则字符串）。"""
    payload = dict(SELECTORS)
    payload["CHALLENGE_WIDGET"] = CHALLENGE_WIDGET
    payload["CHALLENGE_TEXT_RE"] = CHALLENGE_TEXT_RE
    payload["CHALLENGE_TITLE_RE"] = CHALLENGE_TITLE_RE
    return json.dumps(payload, ensure_ascii=False)


def extract_js(entry: str = "list") -> str:
    """渲染 zhilian_extract.js，返回可直接丢给 CDP Runtime.evaluate 的表达式。"""
    if entry not in ("list", "detail"):
        raise ValueError(f"未知入口: {entry!r}（只支持 list / detail）")
    tpl = JS_TEMPLATE.read_text(encoding="utf-8")
    for token in ("__JOBKIT_SELECTORS__", "__JOBKIT_ENTRY__"):
        if token not in tpl:
            raise RuntimeError(f"zhilian_extract.js 缺少 {token} 占位符（模板被改坏了）")
    return (
        tpl.replace("__JOBKIT_SELECTORS__", selectors_json())
        .replace("__JOBKIT_ENTRY__", json.dumps(entry))
    )


def is_zhilian_url(url: str) -> bool:
    return "zhaopin.com" in str(url or "")


def is_list_url(url: str) -> bool:
    """出处：zhilian/urls.js:24-31（/sou/ 经典列表，/jobs? 新版分栏）"""
    u = str(url or "")
    return "/sou" in u or ("/jobs" in u and "?" in u)


def is_detail_url(url: str) -> bool:
    """出处：zhilian/urls.js:8 / job-mapper.js:9"""
    return "/jobdetail/" in str(url or "")


def job_id_from_url(url: str) -> str:
    import re
    m = re.search(r"/jobdetail/([A-Za-z0-9_~%.\-]+?)\.htm", str(url or ""))
    return m.group(1) if m else ""
