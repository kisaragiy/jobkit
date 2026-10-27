"""岗位过滤规则 —— 从 boss-job-search skill 沉淀，全流程共用一份，避免两处漂移。"""
from __future__ import annotations

import re

import settings  # 模块级导入：打分函数也要读配置（函数内再 import 会漏）

# ── 学历 / 经验：用「等级比较」而不是字符串白名单 ────────────────
# 血教训（2026-09-19）：原来用集合精确匹配 → ①「大专」要求低于本科却被挡（40 条）
# ②「硕士」要求高于本科却放行 ③字段值是「在校/应届」而白名单写「应届」→ 28 条应届岗全挡。
# 岗位要求是【最低门槛】，所以判据是「要求等级 ≤ 候选人等级」。
EDU_RANK = {
    "学历不限": 0, "": 0,
    "初中及以下": 1, "小学": 1,
    "高中": 2, "中专": 2, "中专/中技": 2, "中技": 2, "职高": 2,
    "大专": 3, "专科": 3,
    "本科": 4, "全日制本科": 4,
    "硕士": 5, "研究生": 5, "硕士及以上": 5,
    "博士": 6,
}
CANDIDATE_EDU = 4            # 候选人：2025 届全日制本科

# 经验按「要求的最低年限」比较，候选人为 2025 届（约 1 年）
EXP_MIN_YEARS = {
    "在校/应届": 0, "应届": 0, "应届生": 0, "经验不限": 0, "无经验": 0,
    "不限": 0, "": 0,
    "1年以内": 0, "1年以下": 0, "一年以内": 0,
    "1-3年": 1, "1~3年": 1, "1—3年": 1, "2年": 2, "2-3年": 2,
    "3-5年": 3, "3年": 3, "3年以上": 3, "三年以上": 3,
    "5-10年": 5, "5年": 5, "5年以上": 5, "五年以上": 5,
    "10年以上": 10, "十年以上": 10,
}
CANDIDATE_EXP_YEARS = 1      # 2025 届毕业至今约 1 年

# 薪资上限（元/月）。目标区间 7-10K，留足上浮空间；超过这个数的岗基本够不着。
# 可用 settings.limits.salary_max 覆盖。
SALARY_MAX_DEFAULT = 15_000

# 学历可接受（保留旧名给外部引用，实际判断走 EDU_RANK）
EDUCATION_OK = {"本科", "硕士", "博士", "学历不限", ""}
# 经验可接受（同上，实际判断走 EXP_MIN_YEARS）
EXPERIENCE_OK = {"应届", "1年以内", "1-3年", "经验不限", ""}

# 薪资下限（元/月）
SALARY_MIN = 5000

# ══════════════════════════════════════════════════════════════
# 城市策略（用户要求 2026-09-19）
#   指定城市（默认 广州/深圳）→ 无条件投
#   其他城市（广东省内其他 / 广东省外）→ 必须有「能线上面试 / 可远程」信号
#   兼职且可远程 → 距离多远无所谓（remote_ok 通过即放行，不看城市）
# 效果上：城市不是硬限制（有线上信号的照样投），但没信号的非指定城市默认挡。
# ══════════════════════════════════════════════════════════════
CORE_CITIES = ["广州", "深圳"]

GUANGDONG = {
    "广州", "深圳", "珠海", "佛山", "东莞", "中山", "惠州", "江门", "肇庆",
    "汕头", "湛江", "茂名", "揭阳", "梅州", "清远", "韶关", "河源", "阳江",
    "潮州", "云浮", "汕尾",
}

# 强信号：明确写了面试方式或办公方式
_REMOTE_STRONG = re.compile(
    r"线上面试|视频面试|在线面试|远程面试|远程办公|线上办公|居家办公|在家办公"
    r"|远程工作|不需到岗|无需到岗|可远程面试"
)
# 弱信号：括号内出现「远程/线上/居家」—— 这些多写在岗位名后缀里，如
#   「AI应用工程师（远程岗）」「(兼职·远程·按单结算)」「（线上办公+27届优先）」
# 不用单字匹配是为了避开「线上运营」「线上客服」这类业务描述。
_REMOTE_PAREN = re.compile(r"[（(【\[]([^）)】\]]{0,24})[）)】\]]")


def remote_ok(job: dict) -> tuple[bool, str]:
    """岗位是否「能线上面试 / 可远程」。返回 (是否, 命中依据)。"""
    text = " ".join(str(job.get(k) or "") for k in ("title", "skills", "jd"))
    m = _REMOTE_STRONG.search(text)
    if m:
        return True, m.group(0)
    for seg in _REMOTE_PAREN.findall(text):
        if re.search(r"远程|线上|居家", seg):
            return True, "(" + seg.strip()[:20] + ")"
    return False, ""

# 标题排除
EXCLUDE_TITLE_PATTERNS = [
    # 非 AI 岗
    r"实习", r"实习生", r"学徒", r"培训", r"讲师", r"教师",
    r"销售", r"业务", r"客服", r"运营", r"剪辑",
    r"外卖", r"司机",
    # 管理岗（不适合应届）
    r"主管", r"总监", r"合伙人", r"总裁", r"VP",
    r"技术负责人", r"技术总监", r"架构师",
    # 高阶 / 负责人级（2025 届够不着，投了是浪费额度）
    r"负责人", r"首席", r"CIO", r"CTO", r"CEO", r"CTO",
    r"专家", r"资深", r"高级", r"主任", r"总经理", r"厂长",
    # 伪 AI 岗（本质廉价劳动力）
    r"数据标注", r"标注员", r"标注师", r"数据标记",
    r"ai\s*训练师", r"训练师", r"标注", r"标图", r"数据清洗",
]

# 公司排除
EXCLUDE_COMPANY_PATTERNS = [
    r"人力资源", r"外包", r"劳务", r"派遣",
]

_TITLE_RES = [re.compile(p, re.I) for p in EXCLUDE_TITLE_PATTERNS]
_COMPANY_RES = [re.compile(p, re.I) for p in EXCLUDE_COMPANY_PATTERNS]


def parse_salary_min(salary: str) -> int | None:
    """把 '8000-25000元/月' / '1.5-2.5万' / '10-20K' 解析成月薪下限（元）。

    ⚠️ 「13薪 / 15薪」是发薪月数，不是工资金额，必须先剔除。
    血教训（2026-09-19）：'25-50K·15薪' 会被抽出 ['25','50','15']，
    min=15 → 15000，导致高薪岗绕过薪资上限（实测漏了 20-50K 的岗）。
    """
    if not salary:
        return None
    s = salary.lower().replace(",", "")
    # 先去掉「15薪 / ·15薪 / x15薪」这类月数标记
    s = re.sub(r"[·・x×*]?\s*\d+\s*薪", "", s)
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return None
    vals = [float(n) for n in nums]
    if "万" in s:
        vals = [v * 10000 for v in vals]
    elif re.search(r"\d\s*[kK]", s):
        vals = [v * 1000 for v in vals]
    return int(min(vals))


def _norm(s: str) -> str:
    """去掉全部空白字符后再匹配。

    血教训（2026-09-19）：招聘方会用「AI项 目 城 市 合 伙 人」这种插空格写法
    规避关键词过滤 —— 直接匹配「合伙人」会漏掉。所有关键词/正则匹配前先做这一步。
    """
    return re.sub(r"\s+", "", s or "")


def _kw_hit(text: str, keywords: list[str]) -> str | None:
    """文本里命中任一关键词（不区分大小写、忽略空白）则返回该词。

    用子串匹配而不是正则 —— 用户填的是普通词（如"外包"），
    当正则用会因特殊字符报错或误匹配。
    """
    if not text:
        return None
    low = _norm(text).lower()
    for k in keywords:
        k = _norm(k)
        if k and k.lower() in low:
            return k
    return None


def _guess_edu_rank(edu: str) -> int:
    """未收录的学历写法，按关键词猜等级；猜不出按本科处理（宁放勿误杀）。"""
    e = edu or ""
    if not e:
        return 0
    for kw, rk in (("不限", 0), ("博士", 6), ("硕士", 5), ("研究生", 5),
                   ("本科", 4), ("大专", 3), ("专科", 3),
                   ("中专", 2), ("中技", 2), ("高中", 2), ("初中", 1)):
        if kw in e:
            return rk
    return CANDIDATE_EDU


def _guess_exp_years(exp: str) -> int:
    """未收录的经验写法，抽数字当最低年限；抽不到按 0（不误杀）。"""
    nums = re.findall(r"\d+", exp or "")
    if not nums:
        return 0
    return int(min(int(n) for n in nums))


def job_ok(job: dict) -> tuple[bool, str]:
    """返回 (是否通过, 拒绝原因)。

    顺序：用户黑名单 → 用户白名单 → 硬性规则（可被 options.requirement_filter 关闭）。
    用户配置优先，保证「我在界面上加的规则」一定先于内置规则生效。
    """
    import settings

    title = job.get("title") or ""
    company = job.get("company") or ""
    # 职位描述：有 JD 文本就用，没有则退回技能要求（数据源限制，见 README）
    # 注意 job_description：智联通道把 JD 存在这个字段（BOSS 侧叫 jd）
    jd = (job.get("jd") or job.get("job_description") or job.get("skills") or "")

    bl_company, bl_title, bl_jd, wl_title = settings.as_text_rules()

    hit = _kw_hit(company, bl_company)
    if hit:
        return False, f"黑名单公司 {hit}"
    hit = _kw_hit(title, bl_title)
    if hit:
        # 豁免：兼职 + 可远程。
        # 用户规则（2026-09-19）：「兼职可远程的话距离多远无所谓」
        # 而更早又要求黑名单排除「兼职」。两条并不冲突 —— 排的是垃圾兼职
        # （骑手/打字/地推），不是「可远程的技术兼职」。所以这里开口子。
        # 开关：filters.allow_remote_parttime（默认 True）
        allowed = False
        if hit == "兼职" and (settings.load().get("filters") or {}).get("allow_remote_parttime", True):
            rok, _rw = remote_ok(job)
            allowed = rok
        if not allowed:
            return False, f"黑名单岗位 {hit}"
    hit = _kw_hit(jd, bl_jd)
    if hit:
        return False, f"黑名单JD {hit}"

    if wl_title and not _kw_hit(title, wl_title):
        return False, "白名单未命中"

    if not settings.load().get("options", {}).get("requirement_filter", True):
        return True, ""      # 用户关掉了硬性过滤，只走黑白名单

    for p in _TITLE_RES:
        if p.search(_norm(title)):
            return False, f"标题命中排除词 {p.pattern}"
    for p in _COMPANY_RES:
        if p.search(_norm(company)):
            return False, f"公司命中排除词 {p.pattern}"

    # 学历：岗位要求是「最低门槛」，所以要求等级 ≤ 候选人等级 才可投
    edu = (job.get("education") or "").strip()
    rank = EDU_RANK.get(edu)
    if rank is None:
        rank = _guess_edu_rank(edu)
    if rank > CANDIDATE_EDU:
        return False, f"学历要求高于本科 {edu}"

    # 经验：同理，要求的最低年限 ≤ 候选人年限 才可投
    exp = (job.get("experience") or "").strip()
    need = EXP_MIN_YEARS.get(exp)
    if need is None:
        need = _guess_exp_years(exp)
    if need > CANDIDATE_EXP_YEARS:
        return False, f"经验要求高于可投 {exp}"

    # 薪资：低于下限 或 高于上限 都挡
    lo = parse_salary_min(job.get("salary") or "")
    if lo is not None:
        if lo < SALARY_MIN:
            return False, f"薪资低于 {SALARY_MIN}"
        cap = (settings.load().get("limits", {}) or {}).get("salary_max")
        if cap is None:
            cap = SALARY_MAX_DEFAULT
        if cap and lo > cap:
            return False, f"薪资高于上限 {cap}"

    # ── 用户筛选条件（投递设置页）──
    cfg = settings.load()
    flt = cfg.get("filters") or {}

    # 城市策略（见文件上方 CORE_CITIES 说明）
    cities = flt.get("cities") or []
    cv = (job.get("city") or "").strip()
    if cities:
        # 用户显式填了城市白名单 → 硬过滤，覆盖下面的分级逻辑
        if cv and not any(x in cv or cv in x for x in cities):
            return False, f"城市不符 {cv}"
    elif cv:
        core = flt.get("core_cities") or CORE_CITIES
        if not any(x in cv or cv in x for x in core):
            rok, _rw = remote_ok(job)
            if not rok:
                return False, f"非指定城市且无线上面试信号 {cv}"

    ceil = (flt.get("edu_ceiling") or "").strip()
    if ceil:
        cap = EDU_RANK.get(ceil)
        if cap is not None and rank > cap:
            return False, f"学历门槛高于设定 {edu}"

    ecap = (flt.get("exp_ceiling") or "").strip()
    if ecap:
        capy = EXP_MIN_YEARS.get(ecap)
        if capy is not None and need > capy:
            return False, f"经验门槛高于设定 {exp}"

    smin = int(flt.get("salary_min") or 0)
    if smin and lo is not None and lo < smin:
        return False, f"薪资低于设定 {smin}"

    scales = flt.get("scales") or []
    if scales:
        sv = (job.get("scale") or "").strip()
        if sv and not any(x in sv or sv in x for x in scales):
            return False, f"公司规模不符 {sv}"

    inds = flt.get("industries") or []
    if inds:
        iv = (job.get("industry") or "").strip()
        if iv and not any(x in iv or iv in x for x in inds):
            return False, f"行业不符 {iv}"

    if flt.get("exclude_daily_pay", True) and _is_daily_pay(job.get("salary") or ""):
        return False, "日薪/兼职计薪"

    return True, ""


# ══════════════════════════════════════════════════════════════
# 优先级排序（用户要求：支持「单个条件」与「混合条件加权」两种）
# ══════════════════════════════════════════════════════════════

def _is_daily_pay(salary: str) -> bool:
    """日薪/小时薪岗 —— 不是正式全职，默认排除。

    数据里真有「200-250元/天」这种（实测 13 条）。
    """
    return bool(re.search(r"元\s*/\s*(天|日|小时|周)|/\s*(天|日|小时)|每天|每小时", str(salary or "")))


# 简历里的真实技术词，用于算匹配度（都是简历上真实存在的，不编）
RESUME_KEYWORDS = [
    "python", "fastapi", "flask", "django", "langgraph", "langchain", "mcp", "agent",
    "ai", "llm", "rag", "prompt", "embedding", "comfyui", "vlm", "pytest",
    "mysql", "redis", "sql", "docker", "linux", "git", "api",
    "智能体", "大模型", "多智能体", "提示词", "后端", "服务端", "微调", "推理",
    "向量", "爬虫", "并发", "视觉",
]

# 命中这么多个词算满配。分母固定，才能保证以后加词不会把所有人的分数一起压低。
_MATCH_FULL_HITS = 6.0


def _kw_present(kw: str, text: str) -> bool:
    """英文词按词边界匹配，避免 "ai" 命中 "email"/"said"；中文直接子串。"""
    if kw.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None
    return kw in text


def pure_match(job: dict) -> float:
    """对外暴露「纯匹配度」——写回 match_score 时用这个。

    不能用 job_score：那是含薪资/学历/活跃度的混合总分，
    写进 match_score 后再被当匹配度读，会形成自我强化（踩过一次）。
    """
    return _score_match(job)


def match_hits(job: dict) -> list[str]:
    """命中了简历里的哪些技术词 —— 分数要能解释，不能是个黑箱。"""
    text = " ".join([str(job.get("title") or ""), str(job.get("skills") or ""),
                     str(job.get("jd") or job.get("job_description") or "")]).lower()
    return [k for k in RESUME_KEYWORDS if _kw_present(k, text)]


def _score_match(job: dict) -> float:
    """匹配度：抓取时算好的 match_score 优先，没有就用简历关键词重合度。"""
    v = job.get("match_score")
    try:
        if v is not None and float(v) > 0:
            return min(100.0, float(v))
    except (TypeError, ValueError):
        pass
    text = " ".join([str(job.get("title") or ""), str(job.get("skills") or ""),
                     str(job.get("jd") or job.get("job_description") or "")]).lower()
    hit = sum(1 for k in RESUME_KEYWORDS if _kw_present(k, text))
    return min(100.0, hit * 100.0 / _MATCH_FULL_HITS)


def _score_salary(job: dict) -> float:
    """薪资：在上限内越高越好（够得着的高薪优先）。"""
    lo = parse_salary_min(job.get("salary") or "")
    if lo is None:
        return 0.0
    cap = SALARY_MAX_DEFAULT
    try:
        cap = int((settings.load().get("limits") or {}).get("salary_max") or cap)
    except (TypeError, ValueError):
        pass
    if cap <= 0:
        return 0.0
    return max(0.0, min(100.0, lo * 100.0 / cap))


def _score_edu_low_first(job: dict) -> float:
    """★ 降维投递：岗位学历门槛越低，对本科生越好拿，越该优先。

    用户洞察：本科去投「招大专」的岗，竞争者整体更弱，比挤「要求本科」的岗更容易过。
    """
    e = (job.get("education") or "").strip()
    r = EDU_RANK.get(e)
    if r is None:
        r = _guess_edu_rank(e)
    return {0: 100.0, 2: 90.0, 3: 85.0, 4: 50.0}.get(r, 10.0)


def _score_boss_active(job: dict) -> float:
    return 100.0 if "在线" in str(job.get("boss_active") or "") else 0.0


def _score_exp_low_first(job: dict) -> float:
    """经验门槛越低越优先（同降维逻辑）。"""
    e = (job.get("experience") or "").strip()
    n = EXP_MIN_YEARS.get(e)
    if n is None:
        n = _guess_exp_years(e)
    return {0: 100.0, 1: 80.0}.get(n, 20.0)


SORT_FUNCS = {
    "match": _score_match,
    "salary": _score_salary,
    "edu_low_first": _score_edu_low_first,
    "exp_low_first": _score_exp_low_first,
    "boss_active": _score_boss_active,
}
SORT_LABELS = {
    "match": "简历匹配度",
    "salary": "薪资高低",
    "edu_low_first": "学历门槛低优先（降维投递）",
    "exp_low_first": "经验门槛低优先",
    "boss_active": "HR 在线优先",
}


def job_score(job: dict, sort_cfg: dict | None = None) -> float:
    """按配置给岗位打分（0-100）。mode=single 取单键；mode=mixed 按权重加权。"""
    cfg = sort_cfg if sort_cfg is not None else (settings.load().get("sort") or {})
    mode = str(cfg.get("mode") or "mixed").lower()

    if mode == "single":
        fn = SORT_FUNCS.get(str(cfg.get("single") or "match"))
        return round(fn(job), 2) if fn else 0.0

    num = den = 0.0
    for k in (cfg.get("keys") or []):
        fn = SORT_FUNCS.get(str((k or {}).get("by")))
        if not fn:
            continue
        try:
            w = float((k or {}).get("weight") or 0)
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            continue
        num += fn(job) * w
        den += w
    return round(num / den, 2) if den else 0.0


def annotate(job: dict) -> dict:
    """给岗位打上通过/拒绝标记，用于看板展示。"""
    ok, why = job_ok(job)
    job = dict(job)
    job["pass"] = ok
    job["reject_reason"] = why
    return job
