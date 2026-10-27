"""投递优先级：105 个过筛的岗，今天先投哪几个。

为什么需要它
------------
`rules.job_score` 是**采集排序**用的（关键词匹配 + 薪资 + 学历门槛），回答的是
「这个岗像不像我要的」。但投递是另一个问题：「今天我把有限的招呼语额度花在
谁身上，最可能收到回复」。

这两个问题不一样。关键词完全匹配的岗可能 HR 三个月没上线，投了也是石沉大海；
稍微不匹配但 HR 正在线的岗，反而可能当天就聊上。库里 32 投 7 回，**瓶颈是
回复，不是找岗**。所以排序要把「能不能被看到」算进去。

六个维度，以及为什么给这个权重
------------------------------
1. **简历匹配度**（基础分 0-100，来自 match_score）
   岗位本身对不对口。这是底座，其它都是加减项。

2. **HR 活跃度**（-5 ~ +8）
   权重最高的一档。「刚刚活跃」意味着这人现在就在 BOSS 上，消息列表里
   你的招呼语大概率落在第一屏；「离线」的岗不是不能投，但同一份精力
   投在线的更划算。20 个岗 `boss_active` 是空值（老数据没抓到），
   按 0 处理 —— 不知道就不加分也不扣分。

3. **JD 完整度**（0 ~ +5）
   有完整 JD 说明这岗描述得认真，也说明我们知道要什么，招呼语能写得准。
   ⚠ **没 JD 不扣分**：515 个岗缺 JD 是采集侧没抓详情页，不是岗位本身没写。
   扣分等于拿采集的短板惩罚岗位。只在理由里标「未抓 JD，投前点开看」。

4. **新鲜度**（-3 ~ +4）
   `first_seen` 越近越好。发布很久还挂着的岗，要么要求高没人接，
   要么就是在长期收简历。30 天以上的降权。

5. **外包降权**（-15 ~ 0）
   **不拦，只降权**。外包不都是坏选择，有时给得不低。但同样的匹配度下，
   自研公司优先 —— 这是用户没明说但基本成立的偏好。

6. **红线**（-25）
   已经在投递链上拦掉了绝对硬伤（收费/培训贷/传销）。这里的红线指
   `salary_mismatch` 这类不确定的，降权但不拦。

输出的是**理由**，不是裸分
--------------------------
一个 92 分和一个 88 分如果看不出差在哪，那分数就没用。所以每条都要说清
「为什么排这个位置」：HR 在线 +8、JD 完整 +4、12 天前发布 -1。用户看一眼
就知道这条值不值得投，也能发现我的判断哪里不对。
"""
from __future__ import annotations

import re
import time

# HR 活跃度 → 加减分
ACTIVE_SCORE = {
    "刚刚活跃": 8,
    "在线": 5,
    "今日活跃": 5,
    "本周活跃": 2,
    "本月活跃": 0,
    "离线": -5,
}

# 经验门槛 → 加减分
# 用户是 2025 届 + 空窗一年多。3-5 年以上的岗投了基本不回，与其说是
# 「够不着」，不如说是简历第一眼就被筛掉 —— 降权比投出去浪费额度划算。
EXP_SCORE = {
    "在校/应届": 8,
    "在校生": 8,
    "应届生": 8,
    "1年以内": 8,
    "经验不限": 5,
    "1-3年": 5,
    "3-5年": -10,
    "5-10年": -20,
    "10年以上": -20,
}

DEFAULT_N = 10

# 各城市的目标薪资区间（千元/月）
# 用户的预期：广州 7-9K、深圳 8-10K；东莞按同城口径取 7-8K。
# 差异来自租房成本 —— 深圳房租高 500-1000，所以下限抬 1K。
TARGET_K = {
    "深圳": (8, 10),
    "广州": (7, 9),
    "东莞": (7, 8),
}
FALLBACK_TARGET = (7, 10)


def _target_for(city: str) -> tuple[float, float]:
    c = (city or "").strip()
    for name, rng in TARGET_K.items():
        if name in c:
            return rng
    return FALLBACK_TARGET


def _salary_score(salary: str, city: str) -> tuple[int, str]:
    """薪资落点 → (加减分, 理由)。

    用区间中点判断：低于目标下限说明这岗给的就不是这个价；高于上限
    往往意味着要求资深。都不是不能投，只是同样的匹配度下该往后排。
    """
    import filters as F

    k = F.parse_salary_k(salary or "")
    if not k:
        return 0, "薪资面议或解析不出"
    lo, hi = k
    mid = (lo + hi) / 2
    t_lo, t_hi = _target_for(city)

    if mid < t_lo:
        return -10, f"薪资偏低（中点 {mid:.1f}K < 目标 {t_lo}-{t_hi}K）-10"
    if mid > t_hi:
        return -5, f"薪资高于目标（中点 {mid:.1f}K > {t_lo}-{t_hi}K）-5"
    return 5, f"薪资正落目标区间（{t_lo}-{t_hi}K）+5"


def _year_score(title: str) -> tuple[int, str]:
    """校招年份陷阱 → (加减分, 理由)。

    用户是 2025 届、已毕业。挂「2027 校招」「2028 届」的岗是给还没毕业的
    在校生的，投了连简历关都过不去 —— 这在关键词匹配上是满分的，
    不看年份就会排到最前面。实测确实排进过前 5。
    """
    t = title or ""
    y = None
    m = re.search(r"20(2[6-9])", t)          # 2027 届 / 2027校招
    if m:
        y = m.group(1)
    else:
        m = re.search(r"(?<!\d)(2[6-9])\s*届", t)   # 27届 / 28 届
        if m:
            y = m.group(1)
    if y and ("校招" in t or "届" in t):
        return -15, f"20{y} 校招（你是 2025 届，不够格）-15"
    return 0, ""


def _jd_score(jd: str) -> tuple[int, str]:
    """JD 完整度 → (加分, 理由)。缺 JD 不扣分，因为那是采集侧没抓。"""
    n = len((jd or "").strip())
    if n >= 300:
        return 5, f"JD 完整（{n} 字）+5"
    if n >= 100:
        return 2, f"JD 一般（{n} 字）+2"
    if n == 0:
        return 0, "未抓 JD，投前点开看"
    return 0, f"JD 太短（{n} 字），内容不明"


def _fresh_score(first_seen, now=None) -> tuple[int, str]:
    """发布时间 → (加分, 理由)。"""
    if not first_seen:
        return 0, "发布时长未知"
    now = now or time.time()
    days = (now - first_seen) / 86400
    if days < 3:
        return 4, f"{days:.0f} 天内发布 +4"
    if days < 7:
        return 2, f"{days:.0f} 天前发布 +2"
    if days < 30:
        return 0, f"{days:.0f} 天前发布"
    return -3, f"挂了 {days:.0f} 天 -3"


def _outsource_score(company: str) -> tuple[int, str]:
    """外包降权（不拦）。"""
    if not company:
        return 0, ""
    try:
        import company_guard as cg
        a = cg.analyze(company)
    except Exception:
        return 0, ""
    level = a.get("level")
    if level == "confirmed":
        return -15, f"外包/派遣 -15"
    if level == "suspected":
        return -8, f"疑似外包 -8"
    return 0, ""


def _redline_score(job: dict) -> tuple[int, str]:
    """不确定的硬伤降权。绝对硬伤在投递链上已经拦了。"""
    try:
        import redline
    except Exception:
        return 0, ""
    r = redline.check({
        "title": job.get("title") or "",
        "company": job.get("company") or "",
        "salary": job.get("salary") or "",
        "job_description": job.get("job_description") or "",
    })
    for f in r.get("reds") or []:
        if f["id"] == "salary_mismatch":
            return -25, f"薪资与岗位不匹配 -25"
    return 0, ""


def _stack_score(job: dict) -> tuple[int, str]:
    """技术栈匹配 → (加减分, 理由)。没 JD 就不给分，不假装知道。"""
    try:
        import stack_match as sm
    except Exception:  # noqa: BLE001
        return 0, ""
    if not (job.get("job_description") or "").strip():
        return 0, ""
    try:
        m = sm.match(job)
    except Exception:  # noqa: BLE001
        return 0, ""
    if not m.get("delta"):
        return 0, ""
    label = sm.VERDICT_LABEL.get(m["verdict"], m["verdict"])
    return int(m["delta"]), f"技术栈 {label}：{m['why']}"


def score(job: dict, now: float | None = None) -> dict:
    """给单个岗位算投递优先级。

    返回 {total, base, adjusts:[{delta, why}], flags:[str]}
    """
    base = float(job.get("match_score") or 0)
    adjusts: list[dict] = []
    flags: list[str] = []

    active = (job.get("boss_active") or "").strip()
    if active in ACTIVE_SCORE:
        d = ACTIVE_SCORE[active]
        if d:
            adjusts.append({"delta": d, "why": f"HR {active} {'+' if d > 0 else ''}{d}"})
        else:
            adjusts.append({"delta": 0, "why": f"HR {active}"})
    else:
        adjusts.append({"delta": 0, "why": "HR 活跃度未知"})

    d, why = _jd_score(job.get("job_description") or "")
    adjusts.append({"delta": d, "why": why})
    if not (job.get("job_description") or "").strip():
        flags.append("无 JD")

    d, why = _fresh_score(job.get("first_seen"), now)
    adjusts.append({"delta": d, "why": why})

    exp = (job.get("experience") or "").strip()
    if exp in EXP_SCORE:
        d = EXP_SCORE[exp]
        adjusts.append({"delta": d,
                        "why": f"经验 {exp} {'+' if d > 0 else ''}{d}"})
    else:
        adjusts.append({"delta": 0, "why": f"经验要求未知（{exp or '空'}）"})

    d, why = _salary_score(job.get("salary") or "", job.get("city") or "")
    adjusts.append({"delta": d, "why": why})

    d, why = _year_score(job.get("title") or "")
    if why:
        adjusts.append({"delta": d, "why": why})
        flags.append("校招年份不符")

    d, why = _outsource_score(job.get("company") or "")
    if why:
        adjusts.append({"delta": d, "why": why})
        flags.append("外包")

    d, why = _redline_score(job)
    if why:
        adjusts.append({"delta": d, "why": why})
        flags.append("薪资异常")

    d, why = _stack_score(job)
    if why:
        adjusts.append({"delta": d, "why": why})
        flags.append("技术栈不匹配" if d <= -20 else "技术栈")

    total = round(base + sum(a["delta"] for a in adjusts), 1)
    return {"total": total, "base": base, "adjusts": adjusts, "flags": flags}


def rank(jobs: list[dict], n: int = DEFAULT_N, now: float | None = None) -> list[dict]:
    """排序并取前 n 个。同分按 match_score 再排，避免顺序随机。"""
    scored = []
    for j in jobs:
        s = score(j, now)
        scored.append({**j, "_priority": s})
    scored.sort(key=lambda x: (-x["_priority"]["total"],
                               -(x.get("match_score") or 0)))
    return scored[:n]


def render(rows: list[dict], total_pool: int | None = None) -> str:
    if not rows:
        return "没有可投的岗位 —— 先跑采集，或用 `jobkit list` 看看库里有什么。"

    head = f"今日优先投递（{len(rows)} 个"
    if total_pool is not None and total_pool > len(rows):
        head += f" / 候选池 {total_pool} 个"
    head += "）："

    L = [head, ""]
    for i, r in enumerate(rows, 1):
        p = r["_priority"]
        L.append(f"{i:>2}. {p['total']:>5.1f}  {r.get('title') or '（无标题）'}")
        meta = " · ".join(x for x in (r.get("company"), r.get("salary"),
                                      r.get("district") or r.get("city")) if x)
        if meta:
            L.append(f"          {meta}")
        why = " · ".join(a["why"] for a in p["adjusts"] if a["why"])
        L.append(f"          匹配 {p['base']:.0f} · {why}")
        if p["flags"]:
            L.append(f"          ⚠ {'、'.join(p['flags'])}")
        L.append("")

    L.append("分数是排序参考，不是录用概率。基准分来自简历匹配度（0-100），")
    L.append("加减项会把它推到 100 以上或压到负值 —— 只看相对顺序，不用管绝对值。")
    L.append("HR 活跃度变化最快，隔天重跑一次。")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import store

    conn = store.connect()
    try:
        jobs = [dict(r) for r in
                conn.execute("SELECT * FROM jobs ORDER BY match_score DESC")]
        rows = rank(jobs, 10)
        print(render(rows, len(jobs)))
    finally:
        conn.close()
