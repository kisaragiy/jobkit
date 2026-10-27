"""公司画像：只用本地已有数据推断，不编工商信息。

能说什么 / 不能说什么
--------------------
能做：这家公司在**你的采集样本里**有多少岗、薪资跨度、规模标注、HR 活跃度、
      挂了多久、标题里有没有风险词、是否被识别为外包。
不能做：注册资本、真实员工数、拖欠工资、裁员——这些要工商/舆情数据，
      本工具没接，也不猜。

所以报告开头必须写清「基于 N 个在招岗位的观察」，不能让人误以为是尽调结论。

最有用的两个信号
----------------
1. **挂岗时长**：first_seen 到现在多久还在挂。挂半年还在招的岗位，
   大概率是「常年挂着收集简历」或根本没 HC —— 投了就是浪费一次。
2. **标题风险词**：「接受小白」「零基础可」「包就业」是培训贷/低质岗位的
   典型话术，先筛掉能省大量时间。
"""
from __future__ import annotations

import re
import time
from collections import Counter

# 标题/描述里出现就该警惕的话术。每条都对应一种常见套路，不是瞎列。
RISK_PATTERNS = [
    ("培训贷嫌疑", r"(接受小白|无经验可|零基础可|包就业|培训生|学徒制|先培训)"),
    ("铺量招聘", r"(大量招聘|急聘|急招|诚聘|高薪诚聘|长期招聘)"),
    ("描述模糊", r"(待遇面议|薪资面议|具体面谈)"),
    ("疑似刷简历", r"(储备干部|管培生|人才储备)"),
]

# 规模标注 → 风险提示。0-20 人公司招「全栈 + AI」通常是一个人干所有事。
SMALL_SCALES = ("0-20人", "20-99人")


def _parse_salary(s: str) -> tuple[float, float] | None:
    """'12-21K·13薪' / '2000-4000元/月' → (low, high) 千元/月。"""
    s = (s or "").strip()
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(K|k|千|万|元)", s)
    if not m:
        m1 = re.search(r"(\d+(?:\.\d+)?)\s*(K|k|千|万|元)", s)
        if not m1:
            return None
        v = float(m1.group(1))
        unit = m1.group(2)
        if unit in ("K", "k", "千"):
            return v, v
        if unit == "万":
            return v * 10, v * 10
        return v / 1000, v / 1000
    lo, hi, unit = float(m.group(1)), float(m.group(2)), m.group(3)
    if unit == "万":
        lo, hi = lo * 10, hi * 10
    elif unit == "元":
        lo, hi = lo / 1000, hi / 1000
    return lo, hi


def profile(c, company: str) -> dict:
    """按公司名聚合本地岗位数据。名称为空返回 ok=False。"""
    company = (company or "").strip()
    if not company:
        return {"ok": False, "reason": "公司名为空"}

    rows = [dict(r) for r in c.execute(
        "SELECT * FROM jobs WHERE company = ?", (company,))]
    if not rows:
        # 退一步做模糊匹配（「中软国际」可能存成「中软国际科技服务有限公司」）
        import store
        rows = [dict(r) for r in c.execute("SELECT * FROM jobs")
                if store.companies_match(company, r["company"])]
    if not rows:
        return {"ok": False, "reason": f"库里没有「{company}」的岗位"}

    now = time.time()
    salaries = [x for x in (_parse_salary(r.get("salary")) for r in rows) if x]
    scales = Counter(r["scale"] for r in rows if r.get("scale"))
    industries = Counter(r["industry"] for r in rows if r.get("industry"))
    active = Counter(r["boss_active"] for r in rows if r.get("boss_active"))

    # 挂岗时长：用最早的 first_seen 到现在的天数
    firsts = [r["first_seen"] for r in rows if r.get("first_seen")]
    oldest_days = round((now - min(firsts)) / 86400) if firsts else None

    # 风险词（标题 + JD 前 200 字）
    risks = []
    for name, pat in RISK_PATTERNS:
        hits = [r["title"] for r in rows
                if re.search(pat, (r.get("title") or "") + " "
                            + (r.get("job_description") or "")[:200])]
        if hits:
            risks.append({"kind": name,
                          "count": len(hits),
                          "example": hits[0][:40]})

    try:
        import company_guard
        out_label = company_guard.label(company)
    except Exception:  # noqa: BLE001
        out_label = ""

    real_scales = [s for s in scales if s in SMALL_SCALES]
    return {
        "ok": True,
        "company": rows[0]["company"],
        "job_count": len(rows),
        "salary_min": round(min(s[0] for s in salaries), 1) if salaries else None,
        "salary_max": round(max(s[1] for s in salaries), 1) if salaries else None,
        "salary_samples": len(salaries),
        "scales": scales.most_common(),
        "industries": industries.most_common(3),
        "boss_active": active.most_common(),
        "out_sourcing": out_label,
        "oldest_days": oldest_days,
        "risks": risks,
        "small_scale": bool(real_scales) and len(real_scales) == len(scales),
        "titles": [r["title"] for r in rows[:6]],
    }


def render(p: dict) -> str:
    if not p.get("ok"):
        return f"公司画像：无法查询 —— {p.get('reason', '原因未知')}"

    lines = [f"公司画像：{p['company']}",
             "=" * 58,
             f"基于你采集样本里的 {p['job_count']} 个在招岗位观察"
             f"（不是尽调结论，只是本地数据）", ""]

    if p["salary_min"] is not None:
        lines.append(f"薪资跨度：{p['salary_min']}K - {p['salary_max']}K"
                     f"（{p['salary_samples']} 个岗位有明确薪资）")
        span = p["salary_max"] - p["salary_min"]
        if span > 15:
            lines.append(f"  ⚠ 跨度 {span:.0f}K 很大 —— 同公司同名义岗位差这么多，"
                         f"通常说明岗位实际差异大或薪资虚标")
    if p["scales"]:
        lines.append("规模标注：" + "、".join(f"{k}({v})" for k, v in p["scales"]))
    if p["industries"]:
        lines.append("行业：" + "、".join(f"{k}({v})" for k, v in p["industries"]))
    if p["boss_active"]:
        lines.append("HR 活跃：" + "、".join(f"{k}({v})" for k, v in p["boss_active"]))

    if p["out_sourcing"]:
        lines.append(f"⚠ 外包/派遣：{p['out_sourcing']}")

    if p["oldest_days"] is not None and p["oldest_days"] >= 60:
        lines.append(f"⚠ 最早的岗位已挂了 {p['oldest_days']} 天还没消失 —— "
                     f"长期挂岗通常意味着收集简历或没有真实 HC")

    if p["small_scale"]:
        lines.append("⚠ 岗位全部来自 100 人以下的公司 —— "
                     "这类岗位往往是「一个人干所有事」，问清职责边界")

    if p["risks"]:
        lines.append("")
        lines.append("标题/描述里的风险话术：")
        for r in p["risks"]:
            lines.append(f"  · {r['kind']}（{r['count']} 个岗位）例：{r['example']}")

    lines.append("")
    lines.append("在招岗位示例：")
    for t in p["titles"]:
        lines.append(f"  · {t}")

    lines.append("")
    lines.append("查不到的东西（本工具没接这些数据源，不猜）：注册资本、真实人数、")
    lines.append("拖欠工资/裁员记录。这些要天眼查/脉脉，需要的话自己去查。")
    return "\n".join(lines)
