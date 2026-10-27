#!/usr/bin/env python3
"""BOSS 直聘 DOM 通道 · 选择器常量表（迁移自 GetJob 2.3.1）。

来源（逐条对应，便于回查）：
  tmp/getjob231/platforms/boss/selectors.js              列表/聊天选择器
  tmp/getjob231/platforms/boss/page-hydrate.js           卡片各字段 + HR 活跃 + Vue 兜底
  tmp/getjob231/platforms/boss/delivery-engine.js:3104   卡片兜底链（a.job-name / [href*=job_detail]）
  tmp/getjob231/platforms/boss/job-detail-dom.js         详情页 JD / 公司介绍 / 工商全称
  tmp/getjob231/platforms/shared/logged-out-detect.js    登录态判定（顶栏 + 未登录 CTA）
  tmp/getjob231/platforms/boss/city-picker.js            城市筛选面板（识别弹窗，不是投递阻断）
  tmp/getjob231/platforms/boss/filter-url.js             job_detail / jobs 列表 URL 形态

设计原则：
  1. 这里只放「数据」（选择器、文案、城市码），不放抓取逻辑；
  2. 选择器顺序 = 优先精确、再兜底；不要为了「好看」删兜底，BOSS 改版很勤；
  3. 每条选择器带一行中文注释说明抓什么，抓不到的字段在 dom_channel 里降级为空串；
  4. 城市码是从 boss-agent-cli 的 boss.yaml lookups.city 静态抄过来的，避免跨项目 import。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
JS_TEMPLATE = HERE / "boss_extract.js"

# ══════════════════════════════════════════════════════════════
# 列表页选择器
# ══════════════════════════════════════════════════════════════
SELECTORS: dict[str, str] = {
    # ── 列表骨架 ──────────────────────────────────────────────
    "JOB_LIST": "ul.rec-job-list, .job-list-box, ul.job-list",  # 列表外层容器：找不到 = 选择器失效（绝不能当 0 条岗）。ul.rec-job-list 为 2026-10-27 真机实测
    "JOB_CARD": ".job-card-wrapper, ul.job-list > li.job-card, li.job-card.job-card-box",  # 单张岗位卡（容器内定位用）
    "JOB_CARD_LOOSE": "li.job-card, li.job-card-box, .job-card-wrapper",  # 卡片兜底查询：容器改版但卡片类名还在时仍可抓
    "JOB_EMPTY": ".job-list-empty",                      # 「没有找到相关职位」占位块 = 真空页
    "JOB_LOADING": ".job-loading",                       # 列表加载中骨架
    "JOB_NEXT_PAGE": ".options-pages a.next",             # 翻页「下一页」

    # ── 卡片字段 ──────────────────────────────────────────────
    "JOB_TITLE": 'a.job-name, [class*="job-name"]',      # 岗位标题
    "JOB_LINK": 'a.job-name[href*="/job_detail/"], a[href*="/job_detail/"]',  # 详情链接 → job_id / securityId
    "JOB_SALARY": '.job-salary, [class*="salary"]',      # 薪资文案「10-15K·13薪」
    "JOB_COMPANY": '.boss-name, .company-name, [class*="company-name"]',  # 公司名（卡片上常被截成「某某…」）
    "JOB_LOCATION": '.company-location, [class*="company-location"]',  # 城市·区域「广州·天河区」
    "JOB_TAG_LIST": ".tag-list li",                      # 经验/学历/技能标签（顺序：经验→学历→技能）
    "JOB_BOSS_ACTIVE": '.boss-active-time, span.boss-active-time, [class*="boss-active-time"]',  # HR 活跃文案「刚刚活跃/3小时前」
    "JOB_BOSS_ONLINE": '.boss-online-tag, [class*="boss-online-tag"]',  # HR 当前在线（在线时活跃文案只剩这个）

    # ── 登录态 ────────────────────────────────────────────────
    "HEADER": "#header, .header-v2, header, li.nav-figure",  # 顶栏：渲染出来且没有未登录 CTA = 已登录
    "LOGGED_OUT_CTA": "a.header-login-btn, .header-login-btn",  # 顶栏「登录」按钮，只在未登录时可见

    # ── 详情页骨架 / HR 区块（GetJob 有明确出处）─────────────────
    # 出处：job-detail-dom.js:198（右栏根）——职位描述只在这个容器里找，别扫全页
    "DETAIL_PANE": ".job-detail-box, .job-card-detail, .job-detail-container",
    # 出处：delivery-engine.js:4204 / page-hydrate.js:136（HR 活跃文案的扫描根）
    "DETAIL_BOSS_PANE": ".job-boss-info, .job-detail-box, .job-card-detail",
    # 出处：page-hydrate.js:138 / delivery-engine.js:4206（姓名节点，含活跃角标，取文时须剔除装饰）
    "DETAIL_BOSS_NAME": ".job-boss-info h2.name",
    # 出处：page-hydrate.js:140 / delivery-engine.js:4208（「姓名 · 职位」一行，末段才是职位）
    "DETAIL_BOSS_ATTR": ".job-boss-info .boss-info-attr",
    # 出处：delivery-engine.js:4199,4252（对岗闸门：ka 里必须含本岗 encryptId）+ selectors.js:5（海外页变体）
    "DETAIL_CHAT_BUTTON": 'a.op-btn.op-btn-chat, [ka*="overseas_job_list_chat"], [ka*="job-detail-chat"], .btn-startchat',

    # ── 详情页字段（GetJob 对 BOSS 详情页**没有**专门的业务选择器）──
    # GetJob 的 BOSS 字段来自接口（job-mapper.js mapJobDetail）；下面几条只能取它跨平台
    # 诊断探针里的选择器串，属于「字符串有出处、业务路径未验证」。
    # 出处：debug-helper.js:3949-3953（jobTitle 探针 firstMatch 链末段的通用兜底）
    "DETAIL_TITLE": '.job-title, [class*="position-name"], h1',
    # 出处：debug-helper.js:1842（detailRoot 薪资探针）+ page-hydrate.js:115（卡片同款）
    "DETAIL_SALARY": '.job-salary, [class*="job-salary"]',
    # 出处：debug-helper.js:3857,3862,3963（banner / companyEl 探针链）
    "DETAIL_COMPANY": '.job-banner [class*="company"], [class*="company-name"], .comp-name',
    # 自拟·未验证 —— 字符串本身来自 delivery-engine.js:3136（卡片层 .tag-list li），
    # 但 GetJob 从未在详情页抓过标签；详情页标签容器是否同名，本仓库无证据。
    "DETAIL_TAGS": ".tag-list li",

    # ── 区块标题定位（JD / 公司介绍的主路径，出处明确）───────────
    # 出处：job-detail-dom.js:187（第一轮：语义标题节点）
    "DETAIL_SECTION_HEAD_SEL": (
        'h1, h2, h3, h4, h5, .title, [class*="title"], '
        '[class*="sec-title"], [class*="section-title"]'
    ),
    # 出处：job-detail-dom.js:191（第二轮：纯文本短节点，<=12 字才算标题）
    "DETAIL_SECTION_HEAD_LOOSE_SEL": "div, span, p, section, article, label",
    # 出处：job-detail-dom.js:202（工商信息块遍历范围）
    "DETAIL_GS_BLOCK_SEL": "div, section, article",

    # ── 城市筛选（列表顶栏下拉，不是风控弹窗）────────────────
    "CITY_PANEL": ".city-box, .city-list, .city-select-wrapper, [class*=\"city-drop\"], [class*=\"city-picker\"], [class*=\"hot-city\"], .city-area-wrapper",  # 城市下拉面板
    "CITY_LABEL": '.search-input-box .city-label, .search-input-box .city-sel, .job-search-form [class*="city"], .city-label, .nav-city-name',  # 顶栏当前城市标签
}

# ══════════════════════════════════════════════════════════════
# 页面文案（判定用，不是选择器）
# ══════════════════════════════════════════════════════════════
# 风控/限流文案：命中即 restricted，绝不重试、绝不换选择器重抓
RESTRICTED_TEXTS: tuple[str, ...] = (
    "访问受限",
    "账号异常",
    "操作过于频繁",
    "请求过于频繁",
    "异常访问",
    "安全验证",
    "请稍后再试",
)

# 未登录文案：顶栏 CTA 之外的第二判据（登录墙弹窗）
LOGIN_HINTS: tuple[str, ...] = (
    "登录后可查看",
    "登录后查看",
    "请先登录",
    "立即登录",
)

# 登录页 URL（filter-url.js 的 isBossSecurityUrl + logged-out-detect 的 loginUrlRe 合并）
LOGIN_URL_RE = r"/web/user/|/user/login|passport"
# Boss 安全验证页：只能人工过，引擎不许继续点列表
SECURITY_URL_RE = r"/web/passport/zp/security"

# 详情页区块标题（job-detail-dom.js:210,215 的 findHeading 入参）
SECTION_JD = "职位描述"
SECTION_INTRO = "公司介绍"
# job-detail-dom.js:134（OTHER_HEAD）——兄弟块一开头就是这些标题，说明本区块已结束
SECTION_HEADS: tuple[str, ...] = ("职位描述", "公司介绍", "工作地址", "竞争力分析", "工商信息", "相似职位", "看过该职位")

# JD 截断词：命中这些标题就认为出了本区块（正则源码，注入 JS 用）
# 出处：job-detail-dom.js:29（BOSS_JD_SECTION_STOP_RE）
JD_STOP_RE = r"\n(?:工作地址|公司介绍|竞争力分析|BOSS\s*安全提示|工商信息|相似职位|看过该职位)"
# 出处：job-detail-dom.js:31（BOSS_INTRODUCE_SECTION_STOP_RE）
INTRO_STOP_RE = r"\n(?:工商信息|相似职位|看过该职位|BOSS\s*安全提示)"
# 出处：job-detail-dom.js:134（脚本内 OTHER_HEAD）
OTHER_HEAD_RE = r"^(工作地址|公司介绍|竞争力分析|工商信息|相似职位|看过该职位|BOSS\s*安全提示)"
# 出处：job-detail-dom.js:176（section 包裹节点判据：命中即用父级整段文本）
SECTION_WRAP_RE = r"job-detail-body|job-sec|job-detail-section|sec-text|job-banner-info"

# 区块长度门禁：短于此长度视为「没抽到」，不是空字符串成功
# 出处：job-detail-dom.js:69-70（JD <20 丢弃、>4000 截断）、:81-82（介绍 <10 / >2000）、:213-219
DETAIL_JD_MIN = 20
DETAIL_JD_MAX = 4000
DETAIL_INTRO_MIN = 10
DETAIL_INTRO_MAX = 2000

# 工商信息区块：接口 brandName 常截断，详情页这块是全称
# 出处：job-detail-dom.js:120-121（工商信息 / 公司名称）
GS_SECTION_LABEL = "工商信息"
GS_NAME_LABEL = "公司名称"
# 出处：job-detail-dom.js:127（法定代表人|成立日期|企业类型|经营状态|注册资|查看全部）
GS_NAME_STOP_RE = r"法定代表人|成立日期|企业类型|经营状态|注册资|查看全部"

# ══════════════════════════════════════════════════════════════
# 城市码（抄自 boss-agent-cli boss.yaml lookups.city，静态数据）
# ══════════════════════════════════════════════════════════════
CITY_CODES: dict[str, str] = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100", "深圳": "101280600",
    "杭州": "101210100", "成都": "101270100", "南京": "101190100", "武汉": "101200100",
    "西安": "101110100", "苏州": "101190400", "长沙": "101250100", "郑州": "101180100",
    "重庆": "101040100", "天津": "101030100", "合肥": "101220100", "厦门": "101230200",
    "济南": "101120100", "青岛": "101120200", "大连": "101070200", "宁波": "101210400",
    "福州": "101230100", "东莞": "101281600", "珠海": "101280700", "佛山": "101280800",
    "昆明": "101290100", "贵阳": "101260100", "太原": "101100100", "南昌": "101240100",
    "南宁": "101300100", "石家庄": "101090100", "哈尔滨": "101050100", "长春": "101060100",
    "沈阳": "101070100", "海口": "101310100", "兰州": "101160100", "乌鲁木齐": "101130100",
    "无锡": "101190200", "常州": "101191100", "温州": "101210700", "惠州": "101280300",
}

# 搜索列表 URL（filter-url.js JOBS_LIST_PATH + search-keywords.js 的 query 参数）
LIST_URL = "https://www.zhipin.com/web/geek/jobs?query={query}&city={city}"
JOB_DETAIL_URL = "https://www.zhipin.com/job_detail/{job_id}.html"


def city_code(city: str) -> str:
    """城市名 → BOSS 城市码；已经是数字码的原样返回；认不出来返回空串。"""
    s = (city or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    return CITY_CODES.get(s, "")


def selectors_json() -> str:
    """选择器表 → JS 对象字面量（注入用）。ensure_ascii=False 保留中文。"""
    return json.dumps(SELECTORS, ensure_ascii=False, separators=(",", ":"))


def texts_json() -> str:
    """文案/正则表 → JS 对象字面量。只在注入时拼一次，JS 侧不重复写死。"""
    return json.dumps(
        {
            "restricted": list(RESTRICTED_TEXTS),
            "loginHints": list(LOGIN_HINTS),
            "loginUrlRe": LOGIN_URL_RE,
            "securityUrlRe": SECURITY_URL_RE,
            "sectionJd": SECTION_JD,
            "sectionIntro": SECTION_INTRO,
            "jdStopRe": JD_STOP_RE,
            "introStopRe": INTRO_STOP_RE,
            "otherHeadRe": OTHER_HEAD_RE,
            "sectionWrapRe": SECTION_WRAP_RE,
            "jdMinLen": DETAIL_JD_MIN,
            "jdMaxLen": DETAIL_JD_MAX,
            "introMinLen": DETAIL_INTRO_MIN,
            "introMaxLen": DETAIL_INTRO_MAX,
            "gsSection": GS_SECTION_LABEL,
            "gsName": GS_NAME_LABEL,
            "gsStopRe": GS_NAME_STOP_RE,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def extract_js(entry: str = "list") -> str:
    """渲染 boss_extract.js：注入选择器表 + 本次入口。

    entry: 'list' → __jobkitExtractList()；'detail' → __jobkitExtractDetail()
    返回的是可以直接丢给 CDP Runtime.evaluate 的单个表达式（返回 JSON 字符串）。
    """
    if entry not in ("list", "detail"):
        raise ValueError(f"未知入口: {entry!r}（只支持 list / detail）")
    tpl = JS_TEMPLATE.read_text(encoding="utf-8")
    if "__JOBKIT_SELECTORS__" not in tpl or "__JOBKIT_ENTRY__" not in tpl:
        # 模板被改坏时必须炸，不能静默注入一段跑不出结果的 JS
        raise RuntimeError("boss_extract.js 缺少 __JOBKIT_SELECTORS__ / __JOBKIT_ENTRY__ 占位符")
    return (
        tpl.replace("__JOBKIT_SELECTORS__", selectors_json())
        .replace("__JOBKIT_TEXTS__", texts_json())
        .replace("__JOBKIT_ENTRY__", json.dumps(entry))
    )
