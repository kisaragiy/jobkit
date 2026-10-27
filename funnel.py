"""求职漏斗：采集 → 筛选 → 投递 → 回复 → 面试，逐层转化 + 数据完整性体检。

诚实原则
--------
漏斗最容易骗自己的地方是「分母悄悄变了」：投了 32 条，但只有 3 条能关联到
岗位，如果直接把 3/32 当转化率，就是在编数字。

所以本模块**先报数据完整性，再报转化率**，并且：
- 关联不上的投递单独计数，不悄悄算进分母
- 样本不足（<30）时明确标注「样本太小，不看比率」
- 归因分组只在组内样本 >=8 时才输出
"""
from __future__ import annotations

from pathlib import Path

# 样本门槛：低于这个数，比率没有统计意义，只报绝对值
MIN_SAMPLE_FOR_RATE = 30
MIN_SAMPLE_FOR_SEGMENT = 8


def snapshot(c) -> dict:
    """读库算漏斗。不写任何数据。"""
    q = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731

    jobs_all = q("SELECT COUNT(*) FROM jobs")
    jobs_with_jd = q("SELECT COUNT(*) FROM jobs WHERE length(coalesce(job_description,''))>100")
    applied = q("SELECT COUNT(*) FROM greetings")
    applied_linked = q(
        "SELECT COUNT(*) FROM greetings g JOIN jobs j ON g.job_id=j.job_id")
    applied_with_snapshot = q(
        "SELECT COUNT(*) FROM greetings WHERE job_snapshot IS NOT NULL AND job_snapshot != ''")
    convs = q("SELECT COUNT(*) FROM conversations")
    convs_pending = q("SELECT COUNT(*) FROM conversations WHERE from_me=0 AND handled=0")
    convs_interview = q(
        "SELECT COUNT(*) FROM conversations WHERE category='interview' AND from_me=0")
    convs_rejected = q(
        "SELECT COUNT(*) FROM conversations WHERE category='rejected' AND from_me=0")

    # 通过筛选的（用现规则跑一遍，不读缓存，避免规则改了数字不跟着变）
    import rules
    passed = 0
    for row in c.execute("SELECT * FROM jobs"):
        try:
            ok, _ = rules.job_ok(dict(row))
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            passed += 1

    # 回复能关联到投递公司吗 —— 必须用 companies_match 而不是集合交集：
    # 「上海兴岩信息科技」和「上海兴岩信息科技有限公司」字面不同，同一家。
    import store as _store
    replied_companies = [r[0] for r in c.execute(
        "SELECT DISTINCT company FROM conversations WHERE company != ''")]
    applied_companies = [r[0] for r in c.execute(
        "SELECT DISTINCT j.company FROM greetings g JOIN jobs j ON g.job_id=j.job_id "
        "WHERE j.company != ''")]
    overlap = sum(1 for a in replied_companies
                  if any(_store.companies_match(a, b) for b in applied_companies))
    # 会话能匹配到库里岗位的条数（按公司名归一化）
    linked = _store.link_conversations(c)
    conv_linked = sum(1 for x in linked if x["job_id"])

    return {
        "jobs_all": jobs_all,
        "jobs_with_jd": jobs_with_jd,
        "passed": passed,
        "applied": applied,
        "applied_linked": applied_linked,
        "applied_with_snapshot": applied_with_snapshot,
        "convs": convs,
        "convs_pending": convs_pending,
        "convs_interview": convs_interview,
        "convs_rejected": convs_rejected,
        "replied_companies": len(replied_companies),
        "applied_companies": len(applied_companies),
        "overlap": overlap,
        "conv_linked": conv_linked,
    }


def integrity(d: dict) -> dict:
    """数据完整性体检 —— 漏斗可信度的前提。

    返回 {"ok": bool, "warnings": [...], "linked_ratio": float}
    """
    warnings = []
    ratio = (d["applied_linked"] / d["applied"]) if d["applied"] else 0.0
    if d["applied"] and ratio < 0.9:
        warnings.append(
            f"投递 {d['applied']} 条，只有 {d['applied_linked']} 条能关联到岗位"
            f"（{ratio:.0%}）。其余是早期版本投的，岗位信息没保存。"
        )
    if d["applied"] and d["applied_with_snapshot"] == 0:
        warnings.append(
            "所有投递都没有岗位快照 —— 这版之前投的记录无法归因，"
            "从本版起新投递会存快照。"
        )
    if d["jobs_all"] and d["jobs_with_jd"] / d["jobs_all"] < 0.5:
        warnings.append(
            f"只有 {d['jobs_with_jd']}/{d['jobs_all']} 个岗位有完整 JD，"
            f"面试题和 JD 匹配都会因此不准（跑 python jobkit.py enrich 补）。"
        )
    if (d["applied_companies"] >= 5 and d["overlap"] == 0
            and d["convs_pending"]):
        warnings.append(
            "有回复的会话和已投递公司完全没有交集 —— 说明回复不是这条链路来的，"
            "或者公司名对不上（外包常见：简历投 A，实际招人的是 B）。"
        )
    elif d["applied_companies"] < 5 and d["convs_pending"]:
        warnings.append(
            f"能关联到的投递公司只有 {d['applied_companies']} 家，"
            f"不足以判断「回复是否来自投递」——这个结论要等投递数据补全。"
        )
    return {"ok": not warnings, "warnings": warnings, "linked_ratio": ratio,
            "sample_enough": d["applied"] >= MIN_SAMPLE_FOR_RATE}


def layers(d: dict) -> list[dict]:
    """漏斗各层。比值只在样本够时给，否则给 None。"""
    enough = d["applied"] >= MIN_SAMPLE_FOR_RATE
    rows = [
        {"name": "采集岗位", "n": d["jobs_all"], "rate": None, "note": ""},
        {"name": "筛选通过", "n": d["passed"],
         "rate": (d["passed"] / d["jobs_all"]) if d["jobs_all"] else None,
         "note": "按当前规则重算，不是历史快照"},
        {"name": "已投递", "n": d["applied"],
         "rate": (d["applied"] / d["passed"]) if d["passed"] else None,
         "note": "投递/通过筛选"},
        {"name": "会话触达", "n": d["convs"], "rate": None,
         "note": f"BOSS 会话总数（含手动沟通），能对上库里岗位 {d['conv_linked']} 条"},
        {"name": "对方回过话", "n": d["convs_pending"] + d["convs_rejected"] + d["convs_interview"],
         "rate": None,
         "note": "按会话统计，和投递不是同一口径（含非投递来源）"},
        {"name": "其中有面试", "n": d["convs_interview"], "rate": None, "note": ""},
        {"name": "拒信", "n": d["convs_rejected"], "rate": None, "note": ""},
    ]
    if not enough:
        for r in rows:
            r["rate"] = None
        rows[-1]["note"] += f"（投递样本 {d['applied']} < {MIN_SAMPLE_FOR_RATE}，不看比率）"
    return rows


def render(d: dict) -> str:
    lines = ["求职漏斗", "=" * 46]
    for r in layers(d):
        rate = f"{r['rate']:.1%}" if r["rate"] is not None else "—"
        line = f"  {r['name']:<8s} {r['n']:>5d}   {rate:>7s}"
        if r["note"]:
            line += f"   {r['note']}"
        lines.append(line)

    integ = integrity(d)
    lines.append("")
    lines.append("数据完整性")
    lines.append("-" * 46)
    if integ["ok"]:
        lines.append("  ✅ 各层数据完整，转化率可信")
    else:
        for w in integ["warnings"]:
            lines.append(f"  ⚠ {w}")
    if not integ["sample_enough"]:
        lines.append(f"  · 投递样本 {d['applied']} 条，"
                     f"低于 {MIN_SAMPLE_FOR_RATE} 条，比率波动会很大")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).parent))
    import store

    c = store.connect()
    try:
        print(render(snapshot(c)))
    finally:
        c.close()
