"""简历 × 岗位 JD 匹配度诊断（纯本地词表，零 token）。

解决什么问题
------------
投递前最该问的一句话是：「这份简历对着这个岗位，关键词够不够？」
招聘系统（ATS）和 HR 第一轮都是按词筛的。你的简历再好，如果 JD 写
「rerank」「vllm」，而你写的是「检索优化」「模型部署」，机器就是匹配不上。

这不是让你堆词，是让你知道**哪些真实做过的事没写出来**。

两个方向都给
------------
1. **缺失**：JD 有、简历没有 → 你可能是真没做过（那要补能力），
   也可能做了但没写（那就改简历）。这份报告不替你判断，只把差距摆出来。
2. **噪音**：简历有、这份 JD 没提 → 投这个岗时这部分的篇幅可以省下来，
   换成缺失词。同一份简历投所有岗必然稀释重点。
"""
from __future__ import annotations

import re

import tech_terms

# 覆盖率分档：不给百分比打分，给「该不该投」的判断
GRADES = ((80, "A", "关键词覆盖很好，简历这关基本不会被筛掉"),
          (60, "B", "够用，但漏掉的如果岗位核心，建议补上再投"),
          (40, "C", "覆盖一半以下，HR 很可能觉得不匹配"),
          (0, "D", "基本不对口，投了大概率是浪费一次机会"))


def _freq(text: str, term: str) -> int:
    """term 在 text 里出现几次（大小写不敏感）。"""
    return len(re.findall(re.escape(term), (text or "").lower()))


def check(resume_text: str, jd: str) -> dict:
    """对比简历和 JD。jd 为空时返回「无法判断」而不是给 0 分。"""
    res_terms = set(tech_terms.find_terms(resume_text))
    jd_terms = set(tech_terms.find_terms(jd))

    if not jd_terms:
        return {"ok": False, "reason": "这个 JD 里没提取到任何技术词，无法对比",
                "resume_terms": sorted(res_terms), "score": None, "grade": "—"}

    covered = sorted(jd_terms & res_terms)
    missing = sorted(jd_terms - res_terms, key=lambda t: (-_freq(jd, t), t))
    extra = sorted(res_terms - jd_terms)

    score = round(len(covered) / len(jd_terms) * 100)
    grade = next(g for th, g, _ in GRADES if score >= th)
    advice = next(a for th, _, a in GRADES if score >= th)

    # 缺失词按类别归组 —— 「AI/LLM 类缺 4 个」比一串词更有行动指引
    miss_by_cat: dict[str, list[str]] = {}
    for cat, terms in tech_terms.TERMS.items():
        got = [t for t in terms if t in missing]
        if got:
            miss_by_cat[cat] = got

    extra_by_cat: dict[str, list[str]] = {}
    for cat, terms in tech_terms.TERMS.items():
        got = [t for t in terms if t in extra]
        if got:
            extra_by_cat[cat] = got

    return {
        "ok": True,
        "score": score,
        "grade": grade,
        "advice": advice,
        "covered": covered,
        "missing": missing,
        "missing_by_cat": miss_by_cat,
        "extra": extra,
        "extra_by_cat": extra_by_cat,
        "jd_total": len(jd_terms),
        "resume_terms": sorted(res_terms),
    }


def render(r: dict, job_title: str = "", company: str = "") -> str:
    head = "简历 × 岗位匹配度"
    if job_title:
        head += f"\n岗位：{job_title}" + (f"  {company}" if company else "")

    if not r.get("ok"):
        return f"{head}\n{'=' * 56}\n⚠ {r['reason']}\n" \
               f"（JD 里没技术词通常是采集时 JD 没抓全，跑 enrich 或换个岗位）"

    lines = [head, "=" * 56,
             f"覆盖率 {r['score']}%（{r['jd_total'] - len(r['missing'])}/{r['jd_total']}）"
             f"  评级 {r['grade']}",
             f"→ {r['advice']}"]
    lines.append("")
    lines.append(f"✅ JD 要求且简历有（{len(r['covered'])}）："
                 + ("、".join(r["covered"]) if r["covered"] else "无"))

    if r["missing"]:
        lines.append("")
        lines.append(f"❌ JD 要求但简历没提（{len(r['missing'])}）："
                     + "、".join(r["missing"]))
        for cat, terms in r["missing_by_cat"].items():
            if len(terms) >= 2:
                lines.append(f"   · {cat}类缺 {len(terms)} 个：{'、'.join(terms)}")
        lines.append("   → 真没做过？补能力。做过但没写？改简历。")

    if r["extra"]:
        lines.append("")
        lines.append(f"💡 简历有但这 JD 没提（{len(r['extra'])}）："
                     + "、".join(r["extra"][:12])
                     + ("…" if len(r["extra"]) > 12 else ""))
        lines.append("   → 投这个岗可以把这部分篇幅让给上面的缺失词。")

    return "\n".join(lines)


def load_text(path: str) -> str:
    from pathlib import Path
    return Path(path).read_text(encoding="utf-8", errors="ignore")


# ── 批量：一批岗一起看 ──────────────────────────────────
# 同义词归一：同一技术两种写法不能重复计数（postgres / postgresql）
_TERM_SYNONYMS = {
    "postgresql": "postgres",
    "nodejs": "node.js",
    "k8s": "kubernetes",
    "llamaindex": "llama_index",
}


def _canon(term: str) -> str:
    return _TERM_SYNONYMS.get(term, term)


def batch(resume_text: str, jobs: list[dict], top_k: int = 5) -> dict:
    """对一批岗位做批量覆盖分析。

    单岗诊断回答「这份简历对这个岗够不够」，批量回答的是另一个问题：
    **「这批岗共同缺什么」**。如果 10 个优先岗里 8 个要求某个词而简历没写，
    那不是「这个岗不合适」，是简历该补的洞 —— 补一个词能同时提升 8 个岗
    的覆盖率，这才是杠杆最大的动作。

    `lever` 就是算这个：按「补它能多覆盖几个岗」排序，并给出补完后的
    平均覆盖率变化。只统计有 JD 的岗（515 个没 JD 的岗只能跳过，
    我们不能凭空判断）。
    """
    from collections import Counter

    per: list[dict] = []
    miss_counter: Counter = Counter()
    jd_terms_by_job: list[tuple[dict, set]] = []

    for j in jobs:
        jd = (j.get("job_description") or "").strip()
        r = check(resume_text, jd)
        if not r["ok"]:
            continue
        per.append({
            "job": j, "score": r["score"], "grade": r["grade"],
            "missing": r["missing"], "jd_total": r["jd_total"],
        })
        jd_terms_by_job.append((j, set(tech_terms.find_terms(jd))))
        # 归一后再计数，否则同一个岗里 postgres + postgresql 会算成 2 个岗
        for t in {_canon(x) for x in r["missing"]}:
            miss_counter[t] += 1

    if not per:
        return {"ok": False, "checked": 0,
                "reason": "这批岗都没有可用的 JD（采集没抓详情页），无法对比",
                "skipped": len(jobs)}

    avg = round(sum(p["score"] for p in per) / len(per), 1)

    # 补词杠杆：某个缺失词出现在多少个岗里
    lever = [{"term": t, "jobs": n} for t, n in miss_counter.most_common()]
    lever = [x for x in lever if x["jobs"] >= 2]      # 只出现 1 次的没杠杆

    # 补完 top_k 个词后的平均覆盖率
    top_terms = {x["term"] for x in lever[:top_k]}
    res_terms = set(tech_terms.find_terms(resume_text))
    after = []
    for _j, terms in jd_terms_by_job:
        if not terms:
            continue
        covered = terms & (res_terms | top_terms)
        after.append(len(covered) / len(terms) * 100)
    avg_after = round(sum(after) / len(after), 1) if after else avg

    per.sort(key=lambda p: p["score"])

    # 按技术类别归组，便于看出「同类换名字」vs「跨方向」
    lever_by_cat: dict[str, list[dict]] = {}
    for x in lever[:10]:
        placed = False
        for cat, terms in tech_terms.TERMS.items():
            if x["term"] in terms:
                lever_by_cat.setdefault(cat, []).append(x)
                placed = True
                break
        if not placed:
            lever_by_cat.setdefault("其他", []).append(x)

    return {
        "ok": True,
        "checked": len(per),
        "skipped": len(jobs) - len(per),
        "avg_score": avg,
        "avg_after": avg_after,
        "gain": round(avg_after - avg, 1),
        "lever": lever[:10],
        "lever_by_cat": lever_by_cat,
        "top_terms": sorted(top_terms),
        "weakest": per[:5],          # 覆盖最差的 5 个岗
        "strongest": per[-3:][::-1],  # 覆盖最好的 3 个
    }


def render_batch(r: dict) -> str:
    if not r.get("ok"):
        return (f"批量诊断跳过：{r.get('reason', '')}\n"
                f"（共 {r.get('skipped', 0)} 个岗没有可用 JD）")

    L = [f"简历 × 批量岗位（{r['checked']} 个有 JD 的岗"
         + (f"，跳过 {r['skipped']} 个无 JD" if r["skipped"] else "") + "）", ""]
    L.append(f"平均覆盖率：{r['avg_score']}%")
    L.append("")

    if r["lever"]:
        L.append(f"🔧 补这几个词杠杆最大（补 top {len(r['top_terms'])} 个"
                 f"平均覆盖率 {r['avg_score']}% → {r['avg_after']}%"
                 f"，+{r['gain']}）：")
        # 按技术类别分组 —— 「数据库类缺 4 个」比一串词更有行动力，
        # 而且能看出哪些是同一类里换个名字（会 SQLite 但 JD 要 MySQL），
        # 哪些是跨方向（Python 岗要 Java，那不该硬补）。
        by_cat = r.get("lever_by_cat") or {}
        if by_cat:
            for cat, items in by_cat.items():
                names = "、".join(f"{x['term']}({x['jobs']}岗)" for x in items)
                L.append(f"   · {cat}：{names}")
        else:
            for x in r["lever"]:
                L.append(f"   · {x['term']}  —— {x['jobs']} 个岗要求，简历没写")
        L.append("   → 同类里换个名字的（会 SQLite、JD 要 MySQL）写清楚就行；")
        L.append("     跨方向的（Python 岗要 Java）那是能力缺口，别硬写。")
        L.append("")
    else:
        L.append("没有跨岗共现的缺失词 —— 缺的都是各岗特有的，不用为某一个岗改简历。")
        L.append("")

    if r["weakest"]:
        L.append("覆盖最差的岗（投这些等于简历第一关就被词筛掉）：")
        for p in r["weakest"]:
            j = p["job"]
            L.append(f"   {p['score']:>3}%  {j.get('title', '')[:28]}"
                     f"  {j.get('company', '')[:14]}")
            if p["missing"]:
                L.append(f"         缺：{'、'.join(p['missing'][:8])}")
        L.append("")

    if r["strongest"]:
        L.append("覆盖最好的岗（简历这关最可能过）：")
        for p in r["strongest"]:
            j = p["job"]
            L.append(f"   {p['score']:>3}%  {j.get('title', '')[:28]}"
                     f"  {j.get('company', '')[:14]}")

    return "\n".join(L)
