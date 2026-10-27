"""面试复盘：把面过的问题记下来，下次不重复踩。

为什么要有这个
--------------
面试最大的浪费不是"没通过"，是**同一道题你答砸了三次自己都没发现**。
面试完当场感觉"还行"，过两周再想只记得"聊了半小时"。没有记录，
每次都在从零开始。

记什么
------
每场面试：
- 公司 / 岗位 / 面到哪一轮 / 日期 / 结果
- 每个问题：问的什么、我怎么答的、我自己打几分（1-5）、下次该怎么答

为什么自己打分而不是系统打分
----------------------------
系统没法判"你答得好不好"——它没听见你说什么，也不知道面试官的反应。
但你自己知道：答完那一下心里是虚的还是稳的。这个分数是给你自己看的趋势，
不是给别人看的评价。

两个最有用的视角
----------------
1. **高频题**：同一道题在几场面试里都出现 → 这类必须准备到不用想
2. **反复踩坑**：同一道题问了多次、我每次都打低分 → 根本没解决

问题归一化按关键词，不按字面
----------------------------
「请介绍一下你的项目」和「介绍一下你的项目吧」是同一题。
所以比对时去掉标点、空白和常见礼貌前缀，「你自己怎么看」这类才算不同的题。
"""
from __future__ import annotations

import re
import time

STAGES = ("初面", "二面", "技术面", "HR面", "终面")
RESULTS = {"pending": "等结果", "pass": "过了", "reject": "没过"}

SCORE_LABELS = {
    0: "未评分",
    1: "完全答不上",
    2: "答得很乱",
    3: "勉强说清",
    4: "答得不错",
    5: "答得很稳",
}

# 归一化时剥掉的礼貌前缀/后缀
_NOISE = [
    "请问", "想问一下", "想了解一下", "你能", "你可以", "能不能", "可以",
    "简单", "大概", "一下", "呢", "吧", "吗", "啊", "的", "？", "?", "。",
    "，", ",", "、", " ", "　",
]


def _qkey(question: str) -> str:
    """归一化成可比对的键。"""
    s = question
    for n in _NOISE:
        s = s.replace(n, "")
    # 「请」只剥开头，避免把「申请」之类误伤
    s = re.sub(r"^请+", "", s.strip())
    return s.strip().lower()


# ── 写 ──────────────────────────────────────────────────
def add_interview(c, company: str, position: str = "", stage: str = "",
                  happened_at: float | None = None, note: str = "") -> int:
    if not company.strip():
        raise ValueError("公司名不能为空")
    cur = c.execute(
        "INSERT INTO interviews (company, position, stage, happened_at, note) "
        "VALUES (?,?,?,?,?)",
        (company.strip(), position.strip(), stage.strip(),
         happened_at or time.time(), note.strip()))
    c.commit()
    return cur.lastrowid


def add_question(c, interview_id: int, question: str,
                 my_answer: str = "", self_score: int = 0,
                 better: str = "") -> int:
    if not question.strip():
        raise ValueError("问题不能为空")
    if not 0 <= self_score <= 5:
        raise ValueError("自评分数要在 0-5 之间（0=未评分）")
    exists = c.execute("SELECT 1 FROM interviews WHERE id = ?",
                       (interview_id,)).fetchone()
    if not exists:
        raise ValueError(f"没有 id={interview_id} 的面试记录")
    cur = c.execute(
        "INSERT INTO interview_questions "
        "(interview_id, question, my_answer, self_score, better) "
        "VALUES (?,?,?,?,?)",
        (interview_id, question.strip(), my_answer.strip(),
         self_score, better.strip()))
    c.commit()
    return cur.lastrowid


def set_result(c, interview_id: int, result: str) -> bool:
    if result not in RESULTS:
        raise ValueError(f"结果只能是 {'/'.join(RESULTS)}")
    c.execute("UPDATE interviews SET result = ? WHERE id = ?",
              (result, interview_id))
    c.commit()
    return True


# ── 读 ──────────────────────────────────────────────────
def list_interviews(c, limit: int = 20) -> list[dict]:
    rows = c.execute(
        "SELECT id, company, position, stage, happened_at, result, note "
        "FROM interviews ORDER BY happened_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        n_q = c.execute("SELECT COUNT(*) FROM interview_questions "
                        "WHERE interview_id = ?", (r[0],)).fetchone()[0]
        out.append({
            "id": r[0], "company": r[1], "position": r[2], "stage": r[3],
            "happened_at": r[4], "result": r[5], "note": r[6],
            "question_count": n_q,
        })
    return out


def get_interview(c, interview_id: int) -> dict | None:
    r = c.execute(
        "SELECT id, company, position, stage, happened_at, result, note "
        "FROM interviews WHERE id = ?", (interview_id,)).fetchone()
    if not r:
        return None
    qs = c.execute(
        "SELECT id, question, my_answer, self_score, better "
        "FROM interview_questions WHERE interview_id = ? ORDER BY id",
        (interview_id,)).fetchall()
    return {
        "id": r[0], "company": r[1], "position": r[2], "stage": r[3],
        "happened_at": r[4], "result": r[5], "note": r[6],
        "questions": [{"id": q[0], "question": q[1], "my_answer": q[2],
                       "self_score": q[3], "better": q[4]} for q in qs],
    }


# ── 复盘 ────────────────────────────────────────────────
def review(c) -> dict:
    """跨面试的复盘：高频题 + 反复踩坑。"""
    rows = c.execute(
        "SELECT q.question, q.self_score, i.company "
        "FROM interview_questions q JOIN interviews i ON i.id = q.interview_id"
    ).fetchall()

    if not rows:
        return {"ok": True, "total_questions": 0, "interviews": 0,
                "frequent": [], "stuck": [], "avg_score": None,
                "warnings": ["还没有面试记录 —— 先记一场再复盘"]}

    groups: dict[str, dict] = {}
    for question, score, company in rows:
        k = _qkey(question)
        g = groups.setdefault(k, {"question": question, "asked": 0,
                                  "scores": [], "companies": []})
        g["asked"] += 1
        if score:
            g["scores"].append(score)
        if company not in g["companies"]:
            g["companies"].append(company)

    frequent = []
    stuck = []
    for g in groups.values():
        avg = round(sum(g["scores"]) / len(g["scores"]), 1) if g["scores"] else None
        item = {"question": g["question"], "asked": g["asked"],
                "companies": g["companies"], "avg_score": avg}
        if g["asked"] >= 2:
            frequent.append(item)
        # 问了 2 次以上、每次都不超过 2 分 = 根本没解决
        if (g["asked"] >= 2 and len(g["scores"]) >= 2
                and max(g["scores"]) <= 2):
            stuck.append(item)

    scores = [s for s in (r[1] for r in rows) if s]
    n_iv = c.execute("SELECT COUNT(*) FROM interviews").fetchone()[0]

    warnings = []
    if n_iv < 3:
        warnings.append(f"只有 {n_iv} 场面试 —— 样本太少，"
                        f"「高频题」还看不出规律")

    frequent.sort(key=lambda x: (-x["asked"], x["avg_score"] or 9))
    return {
        "ok": True,
        "total_questions": len(rows),
        "interviews": n_iv,
        "frequent": frequent,
        "stuck": stuck,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "warnings": warnings,
    }


# ── 渲染 ────────────────────────────────────────────────
def render_list(rows: list[dict]) -> str:
    if not rows:
        return "还没有面试记录。用 `jobkit interview add --company X` 记一场。"
    L = ["面试记录：", ""]
    for r in rows:
        when = (time.strftime("%Y-%m-%d", time.localtime(r["happened_at"]))
                if r["happened_at"] else "日期未记")
        stage = r["stage"] or "未标轮次"
        result = RESULTS.get(r["result"], r["result"])
        L.append(f"  #{r['id']:<3} {when}  {r['company']}  {r['position']}")
        L.append(f"       {stage} · {result} · 记了 {r['question_count']} 个问题")
    return "\n".join(L)


def render_one(iv: dict) -> str:
    when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(iv["happened_at"]))
            if iv["happened_at"] else "日期未记")
    L = [f"面试 #{iv['id']}  {iv['company']}  {iv['position']}",
         f"  {when} · {iv['stage'] or '未标轮次'} · "
         f"{RESULTS.get(iv['result'], iv['result'])}"]
    if iv["note"]:
        L.append(f"  备注：{iv['note']}")
    L.append("")

    if not iv["questions"]:
        L.append("  （没记问题 —— 下次面完立刻记，隔天就想不起来了）")
        return "\n".join(L)

    L.append(f"问题（{len(iv['questions'])} 个）：")
    for q in iv["questions"]:
        score = q["self_score"]
        mark = f"{score}/5 {SCORE_LABELS.get(score, '')}" if score else "未评分"
        L.append(f"  · {q['question']}   [{mark}]")
        if q["my_answer"]:
            L.append(f"    我当时答：{q['my_answer']}")
        if q["better"]:
            L.append(f"    下次该答：{q['better']}")
    return "\n".join(L)


def render_review(r: dict) -> str:
    if not r.get("ok"):
        return f"复盘失败：{r.get('reason', '')}"
    if not r["total_questions"]:
        return "\n".join(r["warnings"])

    L = [f"面试复盘：{r['interviews']} 场 · {r['total_questions']} 个问题", ""]
    if r["avg_score"]:
        L.append(f"自评均分：{r['avg_score']}/5")
        L.append("")

    if r["stuck"]:
        L.append("🔴 反复踩坑（问了多次，每次都不超过 2 分）：")
        for s in r["stuck"]:
            L.append(f"  · {s['question']}")
            L.append(f"    问了 {s['asked']} 次 · 均分 {s['avg_score']}/5 · "
                     f"{'、'.join(s['companies'])}")
        L.append("")

    if r["frequent"]:
        L.append("🟡 高频题（出现 2 次以上，要准备到不用想）：")
        for f in r["frequent"]:
            avg = f"{f['avg_score']}/5" if f["avg_score"] else "未评分"
            L.append(f"  · {f['question']}")
            L.append(f"    问了 {f['asked']} 次 · 均分 {avg}")

    if r["warnings"]:
        L.append("")
        for w in r["warnings"]:
            L.append(f"⚠ {w}")

    L.append("")
    L.append("分数是你自己打的主观分，只用来观察趋势，不是能力评价。")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import store

    conn = store.connect()
    try:
        print(render_review(review(conn)))
        print()
        print(render_list(list_interviews(conn)))
    finally:
        conn.close()
