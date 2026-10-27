"""投递前过滤层（纯逻辑，stdlib only）。

对标 GetJob 2.3.1 的过滤链设计（逻辑自研重写）：

    岗位要求(经验/学历/薪资/规模) → HR 活跃度 → 双休 → 工作时间

四道都是「缺字段就放行」——宁可多投一个可疑岗，也不因为平台没给字段而把好岗全砍掉。
每个开关默认关闭，由 settings 显式打开；之所以默认关，是因为这些规则会显著改变投递结果，
不打招呼就生效等于偷偷改变用户意图。

对外接口：
    check_job(job, cfg) -> (ok: bool, reason: str)
    单独调用：check_requirements / check_hr_activity / check_double_rest / check_work_hours
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 通用

UNLIMITED = "__unlimited__"


def _half_width(s: str) -> str:
    """全角 → 半角，去 \u3000，压缩空白。平台文案混用全半角是常态。"""
    out = []
    for ch in str(s or ""):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif ch == "\u3000":
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _to_list(raw) -> list[str]:
    """配置可能是 list、JSON 字符串、或逗号分隔串。"""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if raw in (None, ""):
        return []
    text = str(raw).strip()
    if not text or text == "[]":
        return []
    import json
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except (ValueError, TypeError):
        pass
    inner = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    return [s.strip() for s in inner.split(",") if s.strip()]


def _enabled(cfg: dict, key: str) -> bool:
    v = (cfg or {}).get(key)
    return v is True or v == 1 or v == "1"


# ---------------------------------------------------------------- HR 活跃度
# 档位从新鲜到陈旧排列，下标越小越活跃。判断「活跃不足」= 岗位下标 > 用户下限下标。
# 默认下限 week：2 周内活跃的都还能收，更冷的不值得投。

HR_LEVELS: list[dict] = [
    {"code": "online", "name": "在线", "aliases": ["在线", "当前在线", "当前活跃"]},
    {"code": "just_active", "name": "刚刚活跃",
     "aliases": ["刚刚活跃", "刚刚在线", "刚刚回复", "离线"]},
    {"code": "today", "name": "今日活跃", "aliases": ["今日活跃", "高回复率"]},
    {"code": "days_3", "name": "3日内活跃", "aliases": ["3日内活跃", "三日内活跃"]},
    {"code": "week", "name": "本周活跃", "aliases": ["本周活跃", "5日内活跃", "7日内活跃"]},
    {"code": "weeks_2", "name": "2周内活跃", "aliases": ["2周内活跃"]},
    {"code": "month", "name": "本月活跃", "aliases": ["本月活跃"]},
    {"code": "months_2", "name": "2月内活跃", "aliases": ["2月内活跃"]},
    {"code": "months_3", "name": "3月内活跃", "aliases": ["3月内活跃"]},
    {"code": "months_4", "name": "4月内活跃", "aliases": ["4月内活跃"]},
    {"code": "half_year", "name": "近半年活跃", "aliases": ["近半年活跃"]},
    {"code": "stale", "name": "半年前活跃", "aliases": ["半年前活跃"]},
]

DEFAULT_HR_MIN_CODE = "week"

# 参数化文案：各平台的「N 分钟前在线」「今日回复 12 次」都归到同一档
HR_PATTERNS: list[tuple[str, list[str]]] = [
    ("online", [r"^(当前)?在线$", r"^当前活跃$"]),
    ("just_active", [
        r"^刚刚(活跃|在线|回复)$",
        r"^\d+分钟前(在线|活跃|回复|在搜人才)$",
        r"^\d+分钟前处理简历$",
        r"^\d+分钟内回复可能性大$",
    ]),
    ("today", [
        r"^\d+小时前(在线|活跃|回复)$",
        r"^今日(活跃|回复\d+\+?次|查看简历\d+[份次]|处理简历\d+次|搜人才\d+次)$",
        r"^\d+小时内回复可能性大$",
        r"^高回复率$",
    ]),
    ("days_3", [r"^[1-3]天前(在线|活跃)?$", r"^([1-3]|[一二三])日内活跃$", r"^[1-3]天内处理简历$"]),
    ("week", [
        r"^[4-7]天前(在线|活跃)?$",
        r"^([4-7]|[四五六七])日内活跃$",
        r"^[4-7]天内处理简历$",
        r"^本周(活跃|搜人才\d+次|查看简历\d+[份次]|发面试邀请\d+个|处理简历\d+次)$",
    ]),
    ("weeks_2", [r"^2周内活跃$"]),
    ("month", [r"^本月活跃$"]),
    ("months_2", [r"^2月内活跃$"]),
    ("months_3", [r"^3月内活跃$"]),
    ("months_4", [r"^4月内活跃$"]),
    ("half_year", [r"^近半年活跃$"]),
    ("stale", [r"^半年前活跃$"]),
]

# 从「本周活跃 · 刚刚回复」这类拼接串里捞活跃文案，避免整串匹配失败
HR_TOKEN_RE = re.compile(
    r"(当前活跃|当前在线|刚刚(?:活跃|在线|回复)"
    r"|\d+分钟前处理简历|\d+分钟前(?:在线|活跃|回复|在搜人才)|[1-7]天内处理简历"
    r"|\d+小时前(?:在线|活跃|回复)|\d+天前(?:在线|活跃)?"
    r"|(?:[1-7]|[一二三四五六七])日内活跃"
    r"|今日(?:活跃|回复\d+\+?次|查看简历\d+[份次]|处理简历\d+次|搜人才\d+次)"
    r"|本周(?:活跃|搜人才\d+次|查看简历\d+[份次]|发面试邀请\d+个|处理简历\d+次)"
    r"|高回复率|\d+(?:分钟|小时)内回复可能性大"
    r"|(?<![\u4e00-\u9fff])在线(?![\u4e00-\u9fff])|离线)"
)

_COMPILED_HR = [(code, [re.compile(p) for p in pats]) for code, pats in HR_PATTERNS]


def normalize_hr_status(raw) -> str | None:
    if raw is None:
        return None
    return _half_width(raw) or None


def _level_index(levels: list[dict], code: str) -> int | None:
    for i, lv in enumerate(levels):
        if lv.get("code") == code:
            return i
    return None


def resolve_hr_index(raw, levels: list[dict] | None = None) -> int | None:
    """把平台文案映射到档位下标；识别不出返回 None（= 放行）。"""
    q = normalize_hr_status(raw)
    if not q:
        return None
    lv = levels or HR_LEVELS

    for i, level in enumerate(lv):
        for alias in level.get("aliases") or []:
            if _half_width(alias) == q:
                return i

    for code, pats in _COMPILED_HR:
        if any(p.match(q) for p in pats):
            idx = _level_index(lv, code)
            if idx is not None:
                return idx

    # 拼接串：取最「新鲜」的一段
    tokens = [t.strip() for t in re.split(r"\s*·\s*", q) if t.strip()]
    tokens += [m.group(1) for m in HR_TOKEN_RE.finditer(q)]
    best: int | None = None
    for token in tokens:
        if token == q:
            continue
        for i, level in enumerate(lv):
            if any(_half_width(a) == token for a in level.get("aliases") or []):
                best = i if best is None else min(best, i)
                break
        else:
            for code, pats in _COMPILED_HR:
                if any(p.match(token) for p in pats):
                    idx = _level_index(lv, code)
                    if idx is not None:
                        best = idx if best is None else min(best, idx)
                    break
    return best


def check_hr_activity(job: dict, cfg: dict, levels: list[dict] | None = None) -> str | None:
    """返回过滤原因；None 表示通过（含未启用 / 字段缺失 / 识别不出）。"""
    if not _enabled(cfg, "hr_activity_filter"):
        return None
    raw = (job or {}).get("boss_active") or (job or {}).get("hrActiveStatus")
    idx = resolve_hr_index(raw, levels)
    if idx is None:
        return None
    lv = levels or HR_LEVELS
    min_code = (cfg or {}).get("hr_activity_min") or DEFAULT_HR_MIN_CODE
    min_idx = _level_index(lv, min_code)
    if min_idx is None:
        return None
    if idx > min_idx:
        return "活跃不足"
    return None


# ---------------------------------------------------------------- 岗位要求

EXP_BUCKETS = ["在校生", "应届生", "1年以内", "1-3年", "3-5年", "5-10年", "10年以上"]
EXP_RANGES = [
    ("1年以内", 0.0, 1.0),
    ("1-3年", 1.0, 3.0),
    ("3-5年", 3.0, 5.0),
    ("5-10年", 5.0, 10.0),
    ("10年以上", 10.0, float("inf")),
]

DEGREE_BUCKETS = ["初中及以下", "中专/中技", "高中", "大专", "本科", "硕士", "博士"]

SCALE_RANGES = [
    ("0-20人", 0, 20),
    ("20-99人", 20, 99),
    ("100-499人", 100, 499),
    ("500-999人", 500, 999),
    ("1000-9999人", 1000, 9999),
    ("10000人以上", 10000, float("inf")),
]


def _overlap(a_lo, a_hi, b_lo, b_hi) -> bool:
    return a_lo <= b_hi and a_hi >= b_lo


def normalize_exp_bucket(raw) -> str | None:
    """岗位经验 → 设置页档位；「不限」返回 UNLIMITED；识别不出 None（放行）。"""
    original = str(raw or "").strip()
    if not original:
        return None
    if "不限" in original:
        return UNLIMITED
    if "在校" in original and "应届" not in original:
        return "在校生"
    if "应届" in original:
        return "应届生"
    if re.search(r"无需经验|无经验", original):
        return "1年以内"

    t = re.sub(r"\s+", "", original).replace("工作经验", "").replace("经验", "")
    if not t:
        return None
    if "半年" in t:
        return "1年以内"

    if re.search(r"以内|以下|不足", t):
        m = re.search(r"\d+(?:\.\d+)?", t)
        n = float(m.group()) if m else 1.0
        if n <= 1:
            return "1年以内"
        return _match_range(n, n)

    m = re.match(r"^(\d+(?:\.\d+)?)年(?:及)?以上$", t)
    if m:
        n = float(m.group(1))
        return "10年以上" if n >= 10 else None

    m = re.match(r"^(\d+(?:\.\d+)?)[-~～—–至到－](\d+(?:\.\d+)?)年?$", t)
    if m:
        return _match_range(float(m.group(1)), float(m.group(2)))

    m = re.match(r"^(\d+(?:\.\d+)?)年$", t)
    if m:
        n = float(m.group(1))
        return _match_range(n, n)
    return None


def _match_range(lo: float, hi: float) -> str | None:
    best, best_overlap = None, 0.0
    for name, b_lo, b_hi in EXP_RANGES:
        top = 99.0 if b_hi == float("inf") else b_hi
        ov = min(hi, top) - max(lo, b_lo)
        if ov < 0:
            continue
        if abs(lo - b_lo) < 0.01 and abs(hi - top) < 0.01:
            return name
        if ov > best_overlap + 1e-9:
            best, best_overlap = name, ov
    return best


def normalize_degree_bucket(raw) -> str | None:
    n = str(raw or "").strip()
    if not n:
        return None
    if "不限" in n:
        return UNLIMITED
    for pat, bucket in [
        (r"初中", "初中及以下"), (r"中专|中技", "中专/中技"), (r"高中", "高中"),
        (r"大专|专科", "大专"), (r"本科", "本科"),
        (r"硕士|研究生|MBA|mba", "硕士"), (r"博士", "博士"),
    ]:
        if re.search(pat, n):
            return bucket
    return None


def parse_scale_range(raw) -> tuple[float, float] | None:
    s = re.sub(r"\s+", "", _half_width(raw))
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)万?人(?:及)?以上", s) or re.search(r"(\d+(?:\.\d+)?)人及以上", s)
    if m:
        n = float(m.group(1))
        if "万" in s and n < 1000:
            n *= 10000
        return (n, float("inf"))
    m = re.search(r"(\d+(?:\.\d+)?)[-~～—–至到－](\d+(?:\.\d+)?)人", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.match(r"^(\d+(?:\.\d+)?)人$", s)
    if m:
        n = float(m.group(1))
        return (n, n)
    return None


def parse_salary_k(salary: str) -> tuple[float, float] | None:
    """'12-18K·13薪' / '1.5-2万' / '8千-1.2万' / '面议' → (minK, maxK)；解析不出 None。

    ⚠ 日薪/时薪必须先折算：「100-110元/天」是 2.2-2.4K/月，不是 100-110K。
    不折算的话，岗位详情里会显示「月结余 6 万」这种一眼假的数（实测踩过）。
    """
    if not salary:
        return None
    s = _half_width(salary).replace(" ", "")
    if re.search(r"面议|面谈|薪资面议", s):
        return None
    # 日薪按 22 个工作日、时薪按 8 小时 × 22 天折算成月薪（K）
    m = re.search(r"(\d+(?:\.\d+)?)[-~～—–至到](\d+(?:\.\d+)?)元?/([天日])", s)
    if m:
        return (float(m.group(1)) * 22 / 1000,
                float(m.group(2)) * 22 / 1000)
    m = re.search(r"(\d+(?:\.\d+)?)[-~～—–至到](\d+(?:\.\d+)?)元?/(?:小时|时)", s)
    if m:
        return (float(m.group(1)) * 8 * 22 / 1000,
                float(m.group(2)) * 8 * 22 / 1000)
    # 单值日薪/时薪（「200元/天」「35元/时」）
    m = re.search(r"(\d+(?:\.\d+)?)元?/([天日]|小时|时)", s)
    if m:
        mult = 22 if m.group(2) in ("天", "日") else 8 * 22
        v = float(m.group(1)) * mult / 1000
        return (v, v)
    s = s.split("·")[0]
    s = re.sub(r"(元|人民币)?(/月|每月|/年|每年|/天|/小时)$", "", s)

    def unit_value(num: str, unit: str) -> float:
        v = float(num)
        if unit in ("万", "w", "W"):
            return v * 10
        if unit in ("千", "k", "K"):
            return v
        if unit == "元":
            return v / 1000
        return v if v >= 1000 else v  # 无单位时按 K 处理（BOSS 一般写 15-20K）

    m = re.match(r"^(\d+(?:\.\d+)?)([万千kKwW元]?)[-~～—–至到](\d+(?:\.\d+)?)([万千kKwW元]?)$", s)
    if m:
        lo_unit, hi_unit = m.group(2), m.group(4)
        # 只有一端标单位时，另一端沿用（「1.5-2万」= 1.5万–2万）
        if not lo_unit and hi_unit:
            lo_unit = hi_unit
        elif lo_unit and not hi_unit:
            hi_unit = lo_unit
        lo = unit_value(m.group(1), lo_unit)
        hi = unit_value(m.group(3), hi_unit)
        if lo >= 1000:  # 写的是 8000-12000
            lo, hi = lo / 1000, hi / 1000
        return (min(lo, hi), max(lo, hi))
    m = re.match(r"^(\d+(?:\.\d+)?)([万千kKwW元])$", s)
    if m:
        v = unit_value(m.group(1), m.group(2))
        if v >= 1000:
            v /= 1000
        return (v, v)
    return None


def check_requirements(job: dict, cfg: dict) -> str | None:
    """经验/学历/薪资/规模四维；任一维不过即返回原因。全部缺字段则放行。"""
    if not _enabled(cfg, "job_filter_enabled"):
        return None
    job = job or {}

    exp_sel = _to_list(cfg.get("job_filter_experience"))
    if exp_sel:
        bucket = normalize_exp_bucket(job.get("experience"))
        if bucket and bucket != UNLIMITED and bucket not in exp_sel:
            return "经验不符"

    deg_sel = _to_list(cfg.get("job_filter_degree"))
    if deg_sel:
        bucket = normalize_degree_bucket(job.get("education"))
        if bucket and bucket != UNLIMITED and bucket not in deg_sel:
            return "学历不符"

    min_k = _to_float(cfg.get("job_filter_salary_min_k"))
    max_k = _to_float(cfg.get("job_filter_salary_max_k"))
    if min_k is not None or max_k is not None:
        rng = parse_salary_k(job.get("salary"))
        if rng:
            lo = -float("inf") if min_k is None else min_k
            hi = float("inf") if max_k is None else max_k
            if not _overlap(rng[0], rng[1], lo, hi):
                return "薪资不符"

    scale_sel = _to_list(cfg.get("job_filter_scale"))
    if scale_sel:
        job_range = parse_scale_range(job.get("scale"))
        selected = [(name, lo, hi) for name, lo, hi in SCALE_RANGES if name in scale_sel]
        if job_range and selected:
            if not any(_overlap(job_range[0], job_range[1], lo, hi) for _, lo, hi in selected):
                return "规模不符"
    return None


def _to_float(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


# ---------------------------------------------------------------- 双休

def has_double_rest_hint(job: dict) -> bool:
    """福利标签 / 岗位名 / 描述里出现「双休」即算；描述里明确否认的不算。"""
    job = job or {}
    for w in job.get("welfare") or job.get("welfareList") or []:
        if isinstance(w, str) and "双休" in w:
            return True
    name = str(job.get("title") or job.get("jobName") or "")
    if "双休" in name:
        return True
    desc = str(job.get("job_description") or job.get("jobDescription") or "")
    if not desc:
        return False
    if re.search(r"非双休|无双休|不是双休", desc):
        return False
    return "双休" in desc


def check_double_rest(job: dict, cfg: dict) -> str | None:
    if not _enabled(cfg, "filter_double_rest"):
        return None
    return None if has_double_rest_hint(job) else "未标双休"


# ---------------------------------------------------------------- 工作时间

_TIME_RANGE_RE = re.compile(r"^(\d{1,2})[:：](\d{2})[-—–~～](\d{1,2})[:：](\d{2})$")
_TIME_IN_TEXT_RE = re.compile(r"(\d{1,2})[:：](\d{2})\s*[-—–~～]\s*(\d{1,2})[:：](\d{2})")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_CN_HOUR = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
_COLLOQUIAL_RE = re.compile(r"^(?:朝|早)(\d{1,2}|[零一两二三四五六七八九十]+)晚(\d{1,2}|[零一两二三四五六七八九十]+)$")


def _to_minutes(h, m) -> int | None:
    try:
        h, m = int(h), int(m)
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def normalize_hhmm(raw) -> str | None:
    if raw in (None, ""):
        return None
    m = _HHMM_RE.match(_half_width(raw))
    if not m:
        return None
    total = _to_minutes(m.group(1), m.group(2))
    if total is None:
        return None
    return f"{total // 60:02d}:{total % 60:02d}"


def hhmm_to_minutes(raw) -> int | None:
    n = normalize_hhmm(raw)
    if not n:
        return None
    h, m = n.split(":")
    return int(h) * 60 + int(m)


def parse_work_hours_text(text) -> tuple[int, int] | None:
    """'09:00-18:00' → (540, 1080)；跨夜或无法解析返回 None。"""
    compact = re.sub(r"\s+", "", _half_width(text))
    if not compact:
        return None
    m = _TIME_RANGE_RE.match(compact)
    if not m:
        return None
    start = _to_minutes(m.group(1), m.group(2))
    end = _to_minutes(m.group(3), m.group(4))
    if start is None or end is None or end < start:
        return None
    return (start, end)


def parse_colloquial_hours(text) -> tuple[int, int] | None:
    """'朝九晚六' → (540, 1080)。'晚六' 按 18:00 算。"""
    compact = re.sub(r"\s+", "", _half_width(text))
    m = _COLLOQUIAL_RE.match(compact)
    if not m:
        return None

    def cn(token: str) -> int | None:
        if token in _CN_HOUR:
            return _CN_HOUR[token]
        try:
            n = int(token)
        except ValueError:
            return None
        return n if 0 <= n <= 23 else None

    start_h, end_h = cn(m.group(1)), cn(m.group(2))
    if start_h is None or end_h is None:
        return None
    if 0 < end_h <= 12:
        end_h += 12
    if end_h > 23:
        return None
    start, end = start_h * 60, end_h * 60
    return (start, end) if end >= start else None


def extract_work_hours_from_text(text) -> tuple[int, int] | None:
    """长文里抽第一段 HH:mm-HH:mm。"""
    compact = re.sub(r"\s+", "", _half_width(text))
    if not compact:
        return None
    m = _TIME_IN_TEXT_RE.search(compact)
    if not m:
        return None
    start = _to_minutes(m.group(1), m.group(2))
    end = _to_minutes(m.group(3), m.group(4))
    if start is None or end is None or end < start:
        return None
    return (start, end)


_HOURS_KEYS = ("work_hours", "workHours", "work_time", "workTime")


def extract_job_work_hours(job: dict) -> tuple[int, int] | None:
    """先看结构化字段，再从标签/福利/描述里抽。"""
    job = job or {}
    for key in _HOURS_KEYS:
        val = job.get(key)
        if isinstance(val, (list, tuple)) and len(val) == 2:
            try:
                lo, hi = int(val[0]), int(val[1])
            except (TypeError, ValueError):
                continue
            if 0 <= lo <= hi <= 1439:
                return (lo, hi)
        elif isinstance(val, str):
            parsed = parse_work_hours_text(val) or parse_colloquial_hours(val)
            if parsed:
                return parsed

    candidates: list[str] = []
    for key in ("tags", "labels", "welfare", "welfareList"):
        for item in job.get(key) or []:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                for k in ("name", "tagName", "labelName", "title"):
                    if item.get(k):
                        candidates.append(str(item[k]))
                        break
    for text in candidates:
        parsed = parse_work_hours_text(text) or parse_colloquial_hours(text)
        if parsed:
            return parsed
    desc = str(job.get("job_description") or job.get("jobDescription") or "")
    return extract_work_hours_from_text(desc) if desc else None


def check_work_hours(job: dict, cfg: dict) -> str | None:
    """过早上班 / 过晚下班 / 未标工时（未标仅在 filter_unmarked_hours 打开时拦）。"""
    if not _enabled(cfg, "filter_work_hours"):
        return None
    hours = extract_job_work_hours(job)
    if not hours:
        return "未标工时" if _enabled(cfg, "filter_unmarked_hours") else None
    user_start = hhmm_to_minutes(cfg.get("work_hours_start"))
    user_end = hhmm_to_minutes(cfg.get("work_hours_end"))
    if user_start is not None and hours[0] < user_start:
        return "上班过早"
    if user_end is not None and hours[1] > user_end:
        return "下班过晚"
    return None


# ---------------------------------------------------------------- settings 适配

def from_settings(cfg: dict) -> dict:
    """把 jobkit settings.py 的嵌套配置拍平成过滤层要的形状。

    只做映射，不发明新开关：options.only_active_hr 打开才启用 HR 活跃过滤，
    options.requirement_filter 打开才启用要求二次过滤，其余缺省一律关。
    """
    cfg = cfg or {}
    options = cfg.get("options") or {}
    filters = cfg.get("filters") or {}
    limits = cfg.get("limits") or {}

    def _nums(v):
        return v if isinstance(v, (list, tuple)) else []

    return {
        "hr_activity_filter": bool(options.get("only_active_hr")),
        # 红线护栏默认开：收费/培训贷/传销拦下来不需要用户先知道有这功能
        "redline_guard": bool(options.get("redline_guard", True)),
        "hr_activity_min": filters.get("hr_activity_min") or DEFAULT_HR_MIN_CODE,
        "job_filter_enabled": bool(options.get("requirement_filter")),
        "job_filter_experience": _nums(filters.get("job_filter_experience")) or [],
        "job_filter_degree": _nums(filters.get("job_filter_degree")) or [],
        "job_filter_scale": _nums(filters.get("scales")) or [],
        "job_filter_salary_min_k": filters.get("salary_min_k"),
        # 注意：不回落 limits.salary_max —— 那条上限已由 rules.job_ok 执行，
        # 这里再算一遍就是两份规则，改一处忘一处的经典屎山。
        "job_filter_salary_max_k": filters.get("salary_max_k"),
        "filter_double_rest": bool(options.get("exclude_weekend_work")),
        "filter_work_hours": bool(filters.get("work_hours_enabled")),
        "filter_unmarked_hours": bool(filters.get("filter_unmarked_hours")),
        "work_hours_start": filters.get("work_hours_start") or "",
        "work_hours_end": filters.get("work_hours_end") or "",
    }


# ---------------------------------------------------------------- 外包/派遣

def check_company(job: dict, cfg: dict) -> str | None:
    """外包/人力派遣公司过滤。返回拒绝原因；None = 放行。

    **默认关闭**：外包岗不都是坏选择（大厂外包有时给得不低），一票否决
    容易误伤。默认只在界面打标签，用户明确要「只投自研」时才拦。
    要开启，在设置里把 company_filter_enabled 置真。
    """
    if not _enabled(cfg, "company_filter_enabled"):
        return None
    import company_guard as cg

    a = cg.analyze(job.get("company") or "")
    if a["level"] == "confirmed":
        return f"外包/派遣：{a['reason']}"
    if a["level"] == "suspected":
        return f"疑似外包：{a['reason']}"
    return None


# ---------------------------------------------------------------- 汇总

# 绝对硬伤：投了纯粹白费时间 + 泄个人信息，不给开关商量
ABSOLUTE_REDS = {"pay_to_work", "training_trap", "mlm"}


def check_redline(job: dict, cfg: dict) -> str | None:
    """岗位硬伤拦截。

    只拦**绝对硬伤**（收费/培训贷/传销）—— 这三类投了就是白费时间外加
    泄露个人信息，没有讨论空间。
    `salary_mismatch` 之类有误判可能，不在这里拦，用 `jobkit redline` 自己查。
    """
    if not _enabled(cfg, "redline_guard"):
        return None
    import redline

    r = redline.check({
        "title": job.get("title") or "",
        "company": job.get("company") or "",
        "salary": job.get("salary") or "",
        "job_description": (job.get("job_description")
                            or job.get("jd") or ""),
    })
    for f in r["reds"]:
        if f["id"] in ABSOLUTE_REDS:
            return f"红线：{f['name']}"
    return None


def check_job(job: dict, cfg: dict) -> tuple[bool, str]:
    """按固定顺序跑完整过滤链，返回 (是否通过, 原因)。

    顺序有讲究：先看硬性要求（改不了），再看 HR 活跃（会变），最后看工作条件（锦上添花）。
    这样拒绝理由最稳定，也最容易被用户理解。
    """
    for fn in (check_redline, check_requirements, check_company,
               check_hr_activity, check_double_rest, check_work_hours):
        reason = fn(job, cfg or {})
        if reason:
            return False, reason
    return True, ""
