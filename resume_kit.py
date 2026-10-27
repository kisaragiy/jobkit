"""简历多版本 + 效果追踪。

为什么值得做
------------
「哪份简历有用」是求职里最贵的未知数。你以为 A 版本写得好，结果回复都
来自 B。没有版本记录，这个问题永远只能靠感觉回答。

设计要点
--------
1. **自动登记**：库里没有版本时，把已有的 C:/面试/Resume.md 认成「当前简历」，
   不让用户先去学命令（用户不该为解决代码层问题动手）。
2. **投递带版本**：每次投递记 resume_id，从这版起样本才可信；之前的历史
   记录 resume_id=0，统计里单列「未知版本」，不能算进任何一版的胜率。
3. **回复按公司名归一化匹配**：和漏斗同一套 companies_match，避免两处口径打架。
4. **样本不足不报比率**：少于 10 次投递只报绝对数，否则 1/2=50% 会骗人。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import store

MIN_SAMPLE_FOR_RATE = 10
DEFAULT_RESUME_PATH = Path(r"C:\面试\Resume.md")


def _now() -> float:
    return time.time()


def ensure_default(c: sqlite3.Connection) -> int:
    """库里没版本时，把现有简历登记为 v1。返回版本数。"""
    n = c.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]
    if n:
        return n
    if not DEFAULT_RESUME_PATH.exists():
        return 0
    c.execute(
        "INSERT INTO resumes (name, direction, path, active, created_at, note) "
        "VALUES (?,?,?,?,?,?)",
        ("当前简历（自动登记）", "通用", str(DEFAULT_RESUME_PATH), 1, _now(),
         "第一次打开时自动登记。改简历内容后建议先 add 一个新版本再 use。"))
    c.commit()
    return 1


def list_all(c: sqlite3.Connection) -> list[dict]:
    ensure_default(c)
    return [dict(r) for r in c.execute(
        "SELECT * FROM resumes ORDER BY active DESC, resume_id")]


def active(c: sqlite3.Connection) -> dict | None:
    ensure_default(c)
    row = c.execute("SELECT * FROM resumes WHERE active=1 LIMIT 1").fetchone()
    return dict(row) if row else None


def add(c: sqlite3.Connection, name: str, direction: str, path: str,
        note: str = "", make_active: bool = False) -> int:
    path = str(Path(path))
    if not Path(path).exists():
        raise FileNotFoundError(f"简历文件不存在：{path}")
    if not name.strip() or not direction.strip():
        raise ValueError("name / direction 不能为空")
    cur = c.execute(
        "INSERT INTO resumes (name, direction, path, active, created_at, note) "
        "VALUES (?,?,?,?,?,?)",
        (name.strip(), direction.strip(), path, 1 if make_active else 0, _now(), note))
    if make_active:
        c.execute("UPDATE resumes SET active=0 WHERE resume_id != ?", (cur.lastrowid,))
    c.commit()
    return cur.lastrowid


def activate(c: sqlite3.Connection, resume_id: int) -> bool:
    exists = c.execute("SELECT 1 FROM resumes WHERE resume_id=?", (resume_id,)).fetchone()
    if not exists:
        return False
    c.execute("UPDATE resumes SET active=0")
    c.execute("UPDATE resumes SET active=1 WHERE resume_id=?", (resume_id,))
    c.commit()
    return True


def stats(c: sqlite3.Connection) -> list[dict]:
    """按版本统计投递 / 回复 / 面试。回复靠公司名归一化匹配会话。"""
    ensure_default(c)
    convs = [dict(r) for r in c.execute(
        "SELECT company, category, from_me, handled FROM conversations WHERE company != ''")]

    out = []
    for rv in list_all(c):
        rid = rv["resume_id"]
        companies = [x[0] for x in c.execute(
            "SELECT DISTINCT j.company FROM greetings g "
            "LEFT JOIN jobs j ON g.job_id=j.job_id "
            "WHERE g.resume_id=? AND j.company IS NOT NULL AND j.company != ''", (rid,))]
        sent = c.execute(
            "SELECT COUNT(*) FROM greetings WHERE resume_id=?", (rid,)).fetchone()[0]

        replied_companies, interview_companies = set(), set()
        for co in companies:
            for cv in convs:
                if not store.companies_match(co, cv["company"]):
                    continue
                if cv["from_me"]:
                    continue
                replied_companies.add(co)
                if cv["category"] == "interview":
                    interview_companies.add(co)
        out.append({
            **rv,
            "sent": sent,
            "companies": len(companies),
            "replied": len(replied_companies),
            "interview": len(interview_companies),
            "rate": (len(replied_companies) / len(companies)) if companies else None,
            "sample_enough": len(companies) >= MIN_SAMPLE_FOR_RATE,
        })

    # 早期投递（resume_id 未知）单列，不摊给任何版本
    unknown = c.execute(
        "SELECT COUNT(*) FROM greetings WHERE coalesce(resume_id,0)=0").fetchone()[0]
    if unknown:
        out.append({
            "resume_id": 0, "name": "（未知版本）", "direction": "—", "path": "",
            "active": 0, "created_at": None, "note": "本版之前投的记录，无法归因",
            "sent": unknown, "companies": 0, "replied": 0, "interview": 0,
            "rate": None, "sample_enough": False,
        })
    return out


def render(c: sqlite3.Connection) -> str:
    rows = stats(c)
    if not rows:
        return ("还没有简历版本。\n"
                "  python jobkit.py resume add --name \"Python后端-秋招\" "
                "--direction python --path \"C:/面试/Resume.md\"")

    lines = ["简历版本", "=" * 72]
    for r in rows:
        mark = "★" if r["active"] else " "
        name = f"{mark} [{r['resume_id']}] {r['name']}"
        lines.append(f"{name}   （{r['direction']}）")
        if r["resume_id"] == 0:
            lines.append(f"     投递 {r['sent']} 次 · {r['note']}")
            continue
        rate = f"{r['rate']:.0%}" if r["rate"] is not None and r["sample_enough"] else "—"
        warn = "" if r["sample_enough"] else f"（样本 <{MIN_SAMPLE_FOR_RATE}，不看比率）"
        lines.append(f"     投递 {r['sent']} 次 · 覆盖 {r['companies']} 家公司 · "
                     f"有回复 {r['replied']} · 面试 {r['interview']} · 回复率 {rate}{warn}")
        if r["path"]:
            lines.append(f"     {r['path']}")
        lines.append("")

    lines.append("说明：★ = 当前使用的版本；回复按公司名归一化匹配会话，")
    lines.append("      所以「投 A 面 B」的外包岗可能算偏，样本大了才看得出。")
    return "\n".join(lines)


if __name__ == "__main__":
    c = store.connect()
    try:
        print(render(c))
    finally:
        c.close()
