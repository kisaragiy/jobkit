#!/usr/bin/env python3
"""BOSS 岗位采集 · DOM 第二通道（不依赖 boss-agent-cli）。

用途
    主通道 `collector.run_search()` 调外部 CLI（`uv run boss search`）拿接口数据；
    本模块是第二通道：直接连**已登录的 BOSS 页面**（Chrome/Edge 的 CDP 调试端口），
    注入 dom/boss_extract.js 从 DOM 里读列表 / 详情，归一到 jobs 表字段。

前置条件
    1. 浏览器带调试端口启动（例如 `msedge.exe --remote-debugging-port=9222`），
       并且已经在里面登录 zhipin.com；
    2. 不要裸用 `--user-data-dir` 新目录 —— 那样没有登录态，只会拿到 not-logged-in。
    3. 本模块**不会**启动浏览器、不写库、不投递。

对外接口
    fetch_jobs(keyword, city, limit=30) -> (ok, jobs|None, reason)
    fetch_detail(url)                   -> (ok, job|None, reason)

reason 取值（具体，不接受模糊）
    ok             成功（jobs 非空 / 详情字段齐）
    empty          真空页：列表容器在、0 张卡（ok=True, jobs=[]）
    selector-miss  选择器失效：找不到列表容器 / 卡片在但一个字段都归一不出来
                   —— **绝不当成空列表返回**，否则会把改版误判成「今天没岗」
    not-logged-in  登录页 URL / 顶栏登录按钮 / 「登录后可查看」文案
    restricted     访问受限 / 账号异常 / 操作过于频繁 / 安全验证页
    page-error     CDP 连不上、页面没渲染完、注入异常、返回非 JSON

字段归一（jobs 表，见 store.JOB_FIELDS）
    job_id, security_id, title, company, salary, city, district, experience,
    education, skills, boss_active, company_scale
    另附 job_url / welfare / job_description / introduce（详情通道），写库时忽略即可。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

from dom import boss_selectors as S

# ══════════════════════════════════════════════════════════════
# reason 常量
# ══════════════════════════════════════════════════════════════
REASON_OK = "ok"
REASON_EMPTY = "empty"
REASON_SELECTOR_MISS = "selector-miss"
REASON_NOT_LOGGED_IN = "not-logged-in"
REASON_RESTRICTED = "restricted"
REASON_PAGE_ERROR = "page-error"

CDP_BASE = "http://127.0.0.1:9222"   # 与 boss_session.py 同一端口约定
ZHIPIN_MARK = "zhipin.com"
MAX_JOBS = 200                       # 注入脚本单页上限，防止异常页面卡死

_LOGIN_URL_RE = re.compile(S.LOGIN_URL_RE, re.I)
_SECURITY_URL_RE = re.compile(S.SECURITY_URL_RE, re.I)
_JOB_ID_RE = re.compile(r"/job_detail/([\w-]+)")
_TRUNC_RE = re.compile(r"(?:\.{3}|…)$")
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")
_HR_ACTIVE_RE = re.compile(r"活跃|刚刚|回复|分钟|小时|天前|日内|周内|本周|本月|半年")
_EXP_RE = re.compile(r"(\d+-\d+年|\d+年以内|\d+年以上|经验不限|应届|在校)")
_EDU_RE = re.compile(r"(本科|大专|硕士|博士|中专|高中|初中|学历不限|MBA)")
_SCALE_RE = re.compile(r"(\d+-\d+人|\d+人以上|少于\d+人|10000人以上)")
# 福利词：出现在卡片标签里时是福利，不是技能（welfare 另有字段，别混进 skills）
_WELFARE_RE = re.compile(
    r"双休|五险一金|六险一金|年终奖|绩效奖|股票|期权|包吃|包住|餐补|房补|交通补|通讯补"
    r"|节日福利|定期体检|团建|弹性工作|零食|下午茶|免费班车|带薪年假|补充医疗"
)


class DomChannelError(Exception):
    """通道内部错误；reason 落到 reason 契约里。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# ══════════════════════════════════════════════════════════════
# 纯函数：URL / 字段归一（离线可测）
# ══════════════════════════════════════════════════════════════
def clean(v) -> str:
    """单行文本：去首尾、压空白。"""
    return re.sub(r"\s+", " ", str(v or "")).strip()


def build_list_url(keyword: str, city: str) -> str:
    """关键词 + 城市 → BOSS 列表 URL；城市认不出来返回空串。"""
    code = S.city_code(city)
    kw = clean(keyword)
    if not code or not kw:
        return ""
    from urllib.parse import quote

    return S.LIST_URL.format(query=quote(kw, safe=""), city=code)


def job_id_from_url(url: str) -> str:
    m = _JOB_ID_RE.search(str(url or ""))
    return m.group(1) if m else ""


def security_id_from_url(url: str) -> str:
    try:
        return (parse_qs(urlparse(str(url or "")).query).get("securityId") or [""])[0]
    except ValueError:
        return ""


def is_job_detail_url(url: str) -> bool:
    return "/job_detail/" in str(url or "")


def prefer_company_name(*candidates) -> str:
    """公司名优选：未截断的优先，同档取更长（迁移自 company-name-prefer.js）。"""
    names = [clean(c) for c in candidates]
    names = [n for n in names if n]
    if not names:
        return ""
    complete = [n for n in names if not _TRUNC_RE.search(n)]
    pool = complete or names
    return max(pool, key=len)


def pick_readable_salary(*candidates) -> str:
    """跳过私有区字体混淆文本（海外页薪资是「鬼画符」）。"""
    for c in candidates:
        s = clean(c)
        if not s:
            continue
        if _PUA_RE.search(s):
            continue
        return s
    return ""


def sanitize_boss_active(raw) -> str:
    """HR 活跃文案：丢掉「离线」和无法识别的字符串，绝不编造在线。"""
    s = clean(raw)
    if not s or s == "离线":
        return ""
    if s in ("在线", "当前在线"):
        return s
    if _HR_ACTIVE_RE.search(s):
        return s
    return ""


def split_city_district(text) -> tuple[str, str]:
    """「广州·天河区」→ ("广州", "天河区")；没有分隔符就整串当城市。"""
    s = clean(text)
    if not s:
        return "", ""
    parts = [p for p in re.split(r"[·・]+", s) if p]
    if not parts:
        return s, ""
    return parts[0], "".join(parts[1:])


def split_tags(tags) -> tuple[str, str, list[str]]:
    """卡片/详情标签 → (经验, 学历, 技能)。

    BOSS 卡片顺序是「经验 → 学历 → 技能」，但会随城市/岗位变化；先按文案模式认，
    一条都认不出来时才退回位置兜底（tags[0]=经验, tags[1]=学历）。
    """
    items = [clean(t) for t in (tags or []) if clean(t)]
    exp = edu = ""
    skills: list[str] = []
    for t in items:
        if not exp and _EXP_RE.search(t):
            exp = t
            continue
        if not edu and _EDU_RE.search(t):
            edu = t
            continue
        skills.append(t)
    if items and not exp:
        exp = items[0]
        skills = [s for s in skills if s != exp]
    if len(items) > 1 and not edu:
        edu = items[1]
        skills = [s for s in skills if s != edu]
    out: list[str] = []
    for s in skills:
        if s not in out:
            out.append(s)
    return exp, edu, out


def _dedupe(items) -> list[str]:
    out: list[str] = []
    for it in items or []:
        s = clean(it)
        if s and s not in out:
            out.append(s)
    return out


def normalize_list_job(raw: dict) -> dict | None:
    """注入脚本的卡片 dict → jobs 表字段；缺 job_id / title 的卡片直接丢掉。"""
    if not isinstance(raw, dict):
        return None
    url = clean(raw.get("jobUrl"))
    job_id = clean(raw.get("encryptId")) or job_id_from_url(url)
    title = clean(raw.get("title"))
    if not job_id or not title:
        return None

    city, district = split_city_district(raw.get("location"))
    exp, edu, tag_skills = split_tags(raw.get("tags"))

    company = prefer_company_name(raw.get("companyBrand"), raw.get("companyTitle"), raw.get("company"))
    scale = clean(raw.get("companyScale")) or ""
    if not scale:
        m = _SCALE_RE.search(clean(raw.get("cardText")))
        scale = m.group(1) if m else ""

    # 卡片标签里混着福利（「双休」等）：拆到 welfare，skills 只留真技能
    welfare = _dedupe(raw.get("welfare"))
    skills: list[str] = []
    for item in list(raw.get("skills") or []) + tag_skills:
        s = clean(item)
        if not s:
            continue
        if _WELFARE_RE.search(s):
            if s not in welfare:
                welfare.append(s)
            continue
        skills.append(s)

    return {
        "job_id": job_id,
        "security_id": clean(raw.get("securityId")) or security_id_from_url(url),
        "title": title,
        "company": company,
        "salary": pick_readable_salary(raw.get("salary")),
        "city": city,
        "district": district,
        "experience": clean(raw.get("experience")) or exp,
        "education": clean(raw.get("degree")) or edu,
        "skills": _dedupe(skills),
        "boss_active": sanitize_boss_active(raw.get("bossActive")),
        "company_scale": scale,
        "welfare": welfare,
        "job_url": url or S.JOB_DETAIL_URL.format(job_id=job_id),
    }


_JD_HEAD_RE = re.compile(r"^\s*(?:微信扫码分享|扫码分享|举报|收藏|分享)\s*")
_JD_TAIL_RE = re.compile(
    r"\n\s*[^\n]{1,24}\s*\n\s*(?:本月活跃|今日活跃|刚刚活跃|3日内活跃|本周活跃|半年前活跃)"
    r"[\s\S]*$"
)


def clean_jd(text) -> str:
    """清洗详情页 JD 里的界面噪音。

    真机实测：抓到的文本开头是「微信扫码分享 举报」，结尾是
    「张和荣 / 本月活跃 / 广州某某公司 / · / 招聘者」——都是页面装饰，
    不是岗位内容。不洗掉的话，JD 全文喂给关键词匹配会被污染。
    """
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    for _ in range(4):                        # 头部可能连着多个噪音词
        stripped = _JD_HEAD_RE.sub("", t)
        if stripped == t:
            break
        t = stripped
    t = _JD_TAIL_RE.sub("", t)
    # 「举报」在页面上可能被拆成两行（真机见过 "举\n报"），上面的正则清不掉。
    # 用 JD 区块自身的标题做锚点最稳：BOSS 详情页正文固定以「职位描述」起头。
    idx = t.find("职位描述")
    if 0 <= idx <= 60:
        t = t[idx:]
    return t.strip()


def normalize_detail_job(snap: dict, url: str = "") -> dict | None:
    """详情页注入结果 → jobs 字段（+ JD / 公司介绍 / 工商全称）。"""
    if not isinstance(snap, dict):
        return None
    page_url = clean(snap.get("url")) or clean(url)
    job_id = job_id_from_url(page_url) or job_id_from_url(url)
    if not job_id:
        return None

    title = clean(snap.get("title"))
    company = prefer_company_name(snap.get("gsCompany"), snap.get("company"))
    jd = clean_jd(snap.get("jobDescription"))
    if not (title or company or jd):
        return None

    exp, edu, skills = split_tags(snap.get("tags"))
    city, district = split_city_district(snap.get("location"))
    return {
        "job_id": job_id,
        "security_id": security_id_from_url(page_url) or security_id_from_url(url),
        "title": title,
        "company": company,
        "company_full_name": clean(snap.get("gsCompany")),
        "salary": pick_readable_salary(snap.get("salary")),
        "city": city,
        "district": district,
        "experience": exp,
        "education": edu,
        "skills": skills,
        "boss_active": sanitize_boss_active(snap.get("bossActive")),
        "boss_name": clean(snap.get("bossName")),
        "boss_title": clean(snap.get("bossTitle")),
        "company_scale": "",
        "job_description": jd[:4000],
        "introduce": str(snap.get("introduce") or "").strip()[:2000],
        "job_url": page_url or S.JOB_DETAIL_URL.format(job_id=job_id),
    }


def classify_page(snap, entry: str = "list") -> str:
    """页面事实 → reason。判定顺序：URL 登录页 → 风控 → 未登录 → 页面未就绪 → 列表。

    独立成纯函数，是为了不靠浏览器就能把「未登录 / 受限 / 改版 / 真空」四种情况
    用假快照锁死在单测里。
    """
    if not isinstance(snap, dict):
        return REASON_PAGE_ERROR
    if snap.get("error"):
        return REASON_PAGE_ERROR

    url = str(snap.get("url") or "")
    # 安全验证页必须排在登录页前面：它的 URL 也带 passport，先判登录会把风控说成「重新登录就行」
    if _SECURITY_URL_RE.search(url):
        return REASON_RESTRICTED
    if _LOGIN_URL_RE.search(url):
        return REASON_NOT_LOGGED_IN

    restricted = snap.get("restricted") or {}
    if isinstance(restricted, dict) and restricted.get("hit"):
        return REASON_RESTRICTED

    login = snap.get("login") or {}
    if isinstance(login, dict):
        if login.get("loggedOutCta") or login.get("bodyHint"):
            return REASON_NOT_LOGGED_IN

    # 页面没渲染完（白屏 / 还没到 zhipin）：page-error，不是「没岗」
    if snap.get("ready") is False or snap.get("hasBody") is False:
        return REASON_PAGE_ERROR

    if entry == "list":
        if not snap.get("listFound"):
            return REASON_SELECTOR_MISS
        try:
            count = int(snap.get("cardCount") or 0)
        except (TypeError, ValueError):
            count = 0
        if count == 0:
            return REASON_EMPTY
        return REASON_OK

    if not snap.get("detailFound"):
        return REASON_SELECTOR_MISS
    return REASON_OK


# ══════════════════════════════════════════════════════════════
# CDP 层（复用 boss_session.py 的手搓 WebSocket，不引第三方依赖）
# ══════════════════════════════════════════════════════════════
def _opener():
    """绕开系统代理：本机 Clash 会把 localhost 请求也代理掉（实测 502）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cdp_targets(timeout: float = 4.0) -> list[dict]:
    with _opener().open(CDP_BASE + "/json/list", timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


def pick_target(targets: list[dict], want_url: str = "") -> dict | None:
    """优先 URL 已经匹配的页面 → 任意 zhipin 页 → 任意 page。不新开标签页。"""
    pages = [
        t for t in targets or []
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        return None
    if want_url:
        want_path = urlparse(want_url).path
        for t in pages:
            if urlparse(str(t.get("url") or "")).path == want_path:
                return t
    for t in pages:
        if ZHIPIN_MARK in str(t.get("url") or ""):
            return t
    return pages[0]


def _ws(ws_url: str, timeout: float = 10.0):
    from boss_session import WS   # 延迟导入：单测不依赖它

    return WS(ws_url, timeout=timeout)


def _eval_value(ws, expr: str, timeout: float = 20.0, msg_id: int = 11):
    res = ws.call(
        "Runtime.evaluate",
        {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": False,
            "userGesture": False,
            "timeout": int(timeout * 1000),
        },
        msg_id=msg_id,
        deadline=timeout,
    )
    if res.get("exceptionDetails"):
        det = res["exceptionDetails"]
        desc = det.get("text") or (det.get("exception") or {}).get("description") or ""
        raise DomChannelError(REASON_PAGE_ERROR, f"页面执行异常: {desc}")
    result = res.get("result") or {}
    return result.get("value")


def _poll_js() -> str:
    """等到「页面加载完 + 有列表容器或卡片」才抽取，避免刚导航时的假 selector-miss。"""
    list_sel = json.dumps(S.SELECTORS["JOB_LIST"], ensure_ascii=False)
    card_sel = json.dumps(S.SELECTORS["JOB_CARD_LOOSE"], ensure_ascii=False)
    return (
        "(function(){var d=document;"
        "var ok=(d.readyState==='complete')&&(!!d.querySelector(%s)||d.querySelectorAll(%s).length>0);"
        "return ok?'ready':String(d.readyState||'');})()" % (list_sel, card_sel)
    )


def _navigate_and_settle(ws, url: str, timeout: float = 20.0, settle: float = 1.0) -> None:
    ws.call("Page.navigate", {"url": url}, msg_id=1, deadline=10.0)
    deadline = time.time() + max(2.0, timeout)
    while time.time() < deadline:
        try:
            state = _eval_value(ws, _poll_js(), timeout=5.0, msg_id=12)
        except DomChannelError:
            state = ""
        except Exception:
            state = ""
        if state == "ready":
            break
        time.sleep(0.4)
    if settle > 0:
        time.sleep(settle)


def _run(entry: str, navigate_to: str = "", timeout: float = 30.0, settle: float = 1.0,
         js: str | None = None) -> dict:
    """连 CDP →（可选）导航 → 注入 → 解析。任何一层失败都落成 DomChannelError。

    js: 传入自己的提取脚本（别的平台用），不传则用 BOSS 的模板。
    """
    try:
        targets = cdp_targets()
    except Exception as e:   # 连不上调试端口 / 端口没开
        raise DomChannelError(REASON_PAGE_ERROR, f"CDP 不可用: {type(e).__name__}: {e}") from e

    target = pick_target(targets, want_url=navigate_to)
    if not target:
        raise DomChannelError(REASON_PAGE_ERROR, "没有可用的浏览器页面")

    try:
        ws = _ws(target["webSocketDebuggerUrl"])
    except Exception as e:
        raise DomChannelError(REASON_PAGE_ERROR, f"连不上页面调试通道: {e}") from e

    try:
        if navigate_to:
            _navigate_and_settle(ws, navigate_to, timeout=timeout, settle=settle)
        expr = js if js is not None else S.extract_js(entry)
        value = _eval_value(ws, expr, timeout=timeout)
    except DomChannelError:
        raise
    except Exception as e:
        raise DomChannelError(REASON_PAGE_ERROR, f"{type(e).__name__}: {e}") from e
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not isinstance(value, str) or not value.strip():
        raise DomChannelError(REASON_PAGE_ERROR, "注入脚本没有返回字符串")
    try:
        snap = json.loads(value)
    except ValueError as e:
        raise DomChannelError(REASON_PAGE_ERROR, f"注入结果不是合法 JSON: {e}") from e
    if not isinstance(snap, dict):
        raise DomChannelError(REASON_PAGE_ERROR, "注入结果不是对象")
    return snap


# ══════════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════════
def fetch_jobs(keyword: str, city: str, limit: int = 30):
    """按关键词 + 城市抓当前列表页。

    返回 (ok, jobs, reason)：
      ok=True  → jobs 一定是 list（可能为空 = 真空页，reason='empty'）
      ok=False → jobs 为 None，reason 说明是哪一类失败
    注意：会导航现有 BOSS 标签页到目标搜索 URL（不新开窗口、不启动浏览器）。
    """
    url = build_list_url(keyword, city)
    if not url:
        return False, None, REASON_PAGE_ERROR   # 城市认不出来 / 关键词为空

    try:
        snap = _run("list", navigate_to=url)
    except DomChannelError as e:
        return False, None, e.reason

    reason = classify_page(snap, entry="list")
    if reason == REASON_EMPTY:
        return True, [], REASON_EMPTY
    if reason != REASON_OK:
        return False, None, reason

    try:
        limit_n = max(0, int(limit))
    except (TypeError, ValueError):
        limit_n = 30

    jobs = []
    for raw in (snap.get("jobs") or [])[:MAX_JOBS]:
        job = normalize_list_job(raw)
        if job:
            jobs.append(job)

    # 卡片在、但一个都归一不出来 = 字段级改版，仍然算选择器失效
    if not jobs:
        return False, None, REASON_SELECTOR_MISS
    return True, jobs[:limit_n], REASON_OK


def fetch_detail(url: str):
    """抓单个 BOSS 岗位详情（JD / 公司介绍 / 工商全称）。

    返回 (ok, job, reason)。同样会导航现有 BOSS 标签页到该 URL。
    """
    if not str(url or "").strip():
        return False, None, REASON_PAGE_ERROR
    target_url = str(url).strip()

    try:
        snap = _run("detail", navigate_to=target_url)
    except DomChannelError as e:
        return False, None, e.reason

    reason = classify_page(snap, entry="detail")
    if reason != REASON_OK:
        return False, None, reason

    job = normalize_detail_job(snap, target_url)
    if not job:
        return False, None, REASON_SELECTOR_MISS
    return True, job, REASON_OK
