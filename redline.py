"""岗位红线检测：投递前把坑岗筛掉。

为什么要有这个
--------------
投一个岗要花时间：看 JD、判断匹配、写招呼语、等回复。投到坑岗上，
这段时间全废，而且更重要的是**它会污染你的漏斗数据** —— 你会以为是
"简历不行"，其实是岗位本来就不招人。

所以红线要在投之前判，不是在投完之后总结。

两级：red 别投 / yellow 注意
----------------------------
- **red**：硬伤。收钱的、骗培训的、明显假岗。这一条命中就该跳过。
- **yellow**：可疑。急招、单休、纯提成。不一定假，但要有心理准备。

判据全部是**文本特征**，不是我的感觉
------------------------------------
每条规则都有具体的词和具体的理由。命中的时候会把命中的原话打出来，
你能自己复核"是不是这个意思"，而不是信我的结论。

不做什么
--------
- 不判定"这家公司是骗子"。只判"这个岗位描述里有这些特征"。
- 不替代看 JD。红线少不代表好岗，红线多基本可以跳过。
"""
from __future__ import annotations

import re

# 每条：id / 级别 / 触发词 / 为什么 / 下一步
RULES = [
    # ── red：硬伤 ──────────────────────────────────────
    {
        "id": "pay_to_work",
        "level": "red",
        "name": "要你交钱",
        "patterns": ["押金", "保证金", "服装费", "培训费", "工装费", "体检费自理",
                     "入职费", "资料费"],
        "why": "正规公司不会向应聘者收任何费用。《劳动合同法》第九条明确禁止。",
    },
    {
        "id": "training_trap",
        "level": "red",
        "name": "培训贷/包就业嫌疑",
        "patterns": ["接受小白", "零基础培训", "包就业", "包分配", "培训上岗",
                     "先培训后上岗", "学完推荐就业", "无需经验可培训"],
        "why": "「培训后包就业」是培训贷的标准话术：先让你贷款上课，"
               "课上完推荐几个不对口的岗，贷款还得自己还。",
    },
    {
        "id": "salary_vague",
        "level": "red",
        "name": "薪资面议",
        "patterns": ["薪资面议", "工资面议", "薪资面谈", "具体详谈", "面议"],
        "why": "明确标价的岗都不敢写价，要么低得难看，要么按人下菜。"
               "对方连范围都不给，你的期望薪资只能被压。",
    },
    {
        "id": "mlm",
        "level": "red",
        "name": "疑似传销/拉人头",
        "patterns": ["发展下线", "团队裂变", "拉新奖励", "推荐有奖", "代理加盟",
                     "团队计酬"],
        "why": "按人头计酬、靠发展下线赚钱，不是雇佣关系。",
    },
    {
        "id": "no_contract",
        "level": "red",
        "name": "不签合同/不缴社保",
        "patterns": ["不缴社保", "不交社保", "无社保", "不签合同", "劳务协议",
                     "兼职协议"],
        "why": "不缴社保等于没有医疗、养老、失业保障，也影响你以后买房落户。",
    },

    # ── yellow：可疑 ────────────────────────────────────
    {
        "id": "mass_hiring",
        "level": "yellow",
        "name": "铺量招聘",
        "patterns": ["急招", "急聘", "大量招聘", "招若干", "多名", "数名",
                     "长期招聘", "随时面试"],
        "why": "持续急招同一个岗，常见于两种：人员流动极大（留不住人），"
               "或者在收简历池（不是真缺人）。",
    },
    {
        "id": "commission",
        "level": "yellow",
        "name": "纯提成/低底薪",
        "patterns": ["底薪+提成", "无底薪", "多劳多得", "业绩提成", "高提成",
                     "上不封顶"],
        "why": "写「上不封顶」通常意味着底薪很低。要看清楚底薪是多少再谈。",
    },
    {
        "id": "overwork",
        "level": "yellow",
        "name": "工时偏长",
        "patterns": ["单休", "大小周", "996", "早9晚9", "做六休一",
                     "弹性工作", "能接受加班"],
        "why": "「弹性工作」多数指上班弹性、下班不弹性。单休等于每月多上 4 天。",
    },
    {
        "id": "outsource",
        "level": "yellow",
        "name": "外包/驻场/派遣",
        "patterns": ["驻场", "外派", "派遣", "外包", "甲方现场", "客户现场"],
        "why": "外包岗做的是甲方安排的事，技术成长和话语权都受限于合同。"
               "不是不能去，但要清楚自己在签什么。",
    },
    {
        "id": "multi_role",
        "level": "yellow",
        "name": "一人多岗",
        # "全栈" 不算 —— 全栈开发是正常职位，命中它会把 58 个岗全标上
        "patterns": ["前后端都要", "兼做", "同时负责", "一人负责", "全能",
                     "兼管", "兼岗"],
        "why": "小公司常见。要确认薪资是否对得起工作量，以及有没有人带你。",
    },
    {
        "id": "part_time",
        "level": "yellow",
        "name": "兼职/按单结算",
        "patterns": ["兼职", "日结", "按单结算", "临时工", "小时工",
                     "项目制结算", "劳务兼职"],
        "why": "兼职/按单不是劳动关系，没有社保和最低工资保障。"
               "如果挂着正职的活却按单结算，实际上是在规避用工成本。",
    },
    {
        "id": "pressure",
        "level": "yellow",
        "name": "抗压话术",
        "patterns": ["抗压能力强", "能承受压力", "有激情", "狼性", "拼搏",
                     "奋斗者"],
        "why": "把「抗压」写进硬性要求，通常意味着管理和流程有问题，"
               "靠个人硬扛补上。",
    },
]

JD_MIN_CHARS = 100


def _salary_k(salary: str) -> tuple[float, float] | None:
    """薪资 → (低, 高) 千元/月。

    ⚠ 日薪必须先判：「300-350元/天」按 22 个工作日折算约 6.6-7.7K/月，
    是正常实习价。第一版把「元/天」当成「元/月」处理，把华为云计算、
    字节、Shopee 的实习生全标成"薪资与岗位不匹配"—— 62 个里大部分是
    这个误判。折算逻辑统一放在 filters.parse_salary_k，这里只做委托，
    避免两处口径漂移。
    """
    if not salary:
        return None
    import filters as F      # 延迟导入：filters 内部也 import redline，避免循环
    return F.parse_salary_k(salary)


def _hits(text: str, patterns: list[str]) -> list[str]:
    """返回命中的原词。"""
    return [p for p in patterns if p in text]


def check(job: dict) -> dict:
    """单个岗位 → 红线清单。

    job: {title, company, salary, job_description}
    """
    title = job.get("title") or ""
    jd = job.get("job_description") or ""
    salary = job.get("salary") or ""
    text = f"{title}\n{jd}\n{salary}"

    found = []
    for rule in RULES:
        hits = _hits(text, rule["patterns"])
        if hits:
            found.append({
                "id": rule["id"],
                "level": rule["level"],
                "name": rule["name"],
                "hits": hits,
                "why": rule["why"],
            })

    reds = [f for f in found if f["level"] == "red"]
    yellows = [f for f in found if f["level"] == "yellow"]

    # 薪资和岗位级别不匹配：写「开发/工程师/算法」，上限却不到 5K。
    # 这是纯文本判不了的一条，必须看数字。
    sk = _salary_k(salary)
    if (sk and sk[1] < 5
            and re.search(r"开发|工程师|架构|算法|后端|前端|数据", title)
            and "实习" not in title and "见习" not in title):
        reds.append({
            "id": "salary_mismatch",
            "level": "red",
            "name": "薪资与岗位不匹配",
            "hits": [salary],
            "why": f"标题是技术开发岗，薪资上限只有 {sk[1]:g}K —— "
                   f"正常技术岗不会是这个价。要么岗位名是假的（实际是"
                   f"销售/客服/录入），要么按单结算不保底。",
        })

    # JD 太短 = 根本没写清楚，本身就是信号
    warnings = []
    if not jd:
        warnings.append("完全没有 JD 正文 —— 无法判断做什么")
    elif len(jd) < JD_MIN_CHARS:
        warnings.append(f"JD 只有 {len(jd)} 字 —— 太短，看不出实际工作内容")

    if reds:
        verdict = "skip"
    elif len(yellows) >= 3:
        verdict = "risky"
    elif yellows:
        verdict = "note"
    else:
        verdict = "ok"

    return {
        "ok": True,
        "title": title,
        "company": job.get("company") or "",
        "salary": salary,
        "reds": reds,
        "yellows": yellows,
        "warnings": warnings,
        "verdict": verdict,
    }


VERDICT_TEXT = {
    "skip": "建议跳过",
    "risky": "可疑项较多，投之前想清楚",
    "note": "有可疑项，可以投但要留意",
    "ok": "没检出明显红线",
}


def render(r: dict) -> str:
    if not r.get("ok"):
        return f"红线检测失败：{r.get('reason', '')}"

    head = f"{r['title']}  {r['company']}"
    if r["salary"]:
        head += f"  {r['salary']}"
    L = [head, "", f"结论：{VERDICT_TEXT.get(r['verdict'], '')}"]

    for f in r["reds"]:
        L.append(f"  🔴 {f['name']}  —— 命中：{'、'.join(f['hits'])}")
        L.append(f"     {f['why']}")

    for f in r["yellows"]:
        L.append(f"  🟡 {f['name']}  —— 命中：{'、'.join(f['hits'])}")
        L.append(f"     {f['why']}")

    if r["warnings"]:
        for w in r["warnings"]:
            L.append(f"  ⚠ {w}")

    if not (r["reds"] or r["yellows"] or r["warnings"]):
        L.append("  （无）")

    L.append("")
    L.append("只判文本特征，不判公司真伪。命中原词都在上面，可以自己复核。")
    return "\n".join(L)


def scan(c, limit: int | None = None) -> dict:
    """扫全库，按红线数排序。用于"我这堆岗里有多少是坑"。"""
    rows = c.execute(
        "SELECT title, company, salary, job_description FROM jobs").fetchall()
    if limit:
        rows = rows[:limit]

    results = [check({"title": t, "company": co, "salary": s,
                      "job_description": jd}) for t, co, s, jd in rows]

    return {
        "total": len(results),
        "skip": sum(1 for r in results if r["verdict"] == "skip"),
        "risky": sum(1 for r in results if r["verdict"] == "risky"),
        "note": sum(1 for r in results if r["verdict"] == "note"),
        "ok": sum(1 for r in results if r["verdict"] == "ok"),
        "by_rule": _rule_counts(c),
    }


def _rule_counts(c) -> list[tuple[str, int]]:
    rows = c.execute(
        "SELECT title, job_description, salary FROM jobs").fetchall()
    counts = {}
    for t, jd, s in rows:
        r = check({"title": t, "job_description": jd, "salary": s})
        for f in r["reds"] + r["yellows"]:
            counts[f["name"]] = counts.get(f["name"], 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


def render_scan(s: dict) -> str:
    L = [f"全库红线扫描：{s['total']} 个岗位", ""]
    L.append(f"  建议跳过（有硬伤）  {s['skip']:>4}")
    L.append(f"  可疑项多            {s['risky']:>4}")
    L.append(f"  有可疑项            {s['note']:>4}")
    L.append(f"  未检出              {s['ok']:>4}")
    if s["by_rule"]:
        L.append("")
        L.append("命中最多的问题：")
        for name, n in s["by_rule"][:8]:
            L.append(f"  {n:>4}  {name}")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import store

    conn = store.connect()
    try:
        if len(sys.argv) > 1 and sys.argv[1] != "scan":
            row = conn.execute(
                "SELECT title, company, salary, job_description FROM jobs "
                "WHERE title LIKE ? LIMIT 1", (f"%{sys.argv[1]}%",)).fetchone()
            if not row:
                print(f"找不到含「{sys.argv[1]}」的岗位")
                sys.exit(2)
            print(render(check({"title": row[0], "company": row[1],
                                "salary": row[2], "job_description": row[3]})))
        else:
            print(render_scan(scan(conn)))
    finally:
        conn.close()
