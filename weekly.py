"""求职周报：这周做了什么、结果如何、卡在哪。

为什么要有这个
--------------
求职是长跑，最容易发生的不是"投了没回"，是**两周没动自己都没察觉**。
每天看榜单、每天想改简历，但一个月过去投递数还是 0 —— 这种感觉会把人
磨平。周报把"我这周到底干了什么"变成可看的数字，不靠感觉。

口径必须诚实（BOSS 的数据只有一半能用）
---------------------------------------
- **投递数**：准。greetings.greeted_at 是本地记的真实时间。
- **回复数**：**不准**。BOSS 聊天页只给"昨天/刚刚"这种相对时间，
  conversations 里能记的只有「我们什么时候第一次看到它」。
  所以这个数字的意思是「本周新发现的会话」，不是「本周 HR 回复了你」。
  界面上必须这么写，不能说成回复数。
- **面试/拒信**：来自会话分类，同理。

卡在哪一层是**推断**，不是结论
------------------------------
投了 20 个 0 回复 → 可能是岗位不匹配，也可能是简历第一眼不行，也可能是
HR 根本没点开。这三种没法从这个数据里区分。所以只给"最可能的下一步"，
并且明说是推断。

连续没动了要直说
----------------
如果最近一次投递在 N 天前，周报第一行就得写出来。这个数字不好看，
但看不见它才是真的浪费时间。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
TARGETS_PATH = DATA_DIR / "targets.json"

DEFAULT_TARGETS = {"apply": 20, "interview": 2}

# 多少个自然周完全没投递就该报警
IDLE_WEEKS_ALERT = 2


# ── 周的范围 ────────────────────────────────────────────
def week_bounds(offset: int = 0, today: dt.date | None = None) -> tuple[float, float, str]:
    """第 offset 周（0=本周，-1=上周）的起止时间戳 + 标签。

    周一 00:00 起，周日 23:59:59 止（本地时区）。
    """
    today = today or dt.date.today()
    monday = today - dt.timedelta(days=today.isoweekday() - 1)
    monday += dt.timedelta(weeks=offset)
    start = dt.datetime.combine(monday, dt.time.min)
    end = start + dt.timedelta(days=7)
    label = f"{monday:%m-%d} ~ {monday + dt.timedelta(days=6):%m-%d}"
    if offset == 0:
        label += "（本周）"
    elif offset == -1:
        label += "（上周）"
    return start.timestamp(), end.timestamp(), label


# ── 目标 ────────────────────────────────────────────────
def load_targets() -> dict:
    if TARGETS_PATH.exists():
        try:
            t = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_TARGETS, **t}
        except (OSError, json.JSONDecodeError):
            return dict(DEFAULT_TARGETS)
    return dict(DEFAULT_TARGETS)


def save_targets(**kw) -> dict:
    t = load_targets()
    t.update({k: v for k, v in kw.items() if v is not None})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TARGETS_PATH.write_text(json.dumps(t, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return t


# ── 统计 ────────────────────────────────────────────────
def collect(c, start: float, end: float) -> dict:
    """一个时间窗内的动作与结果。"""
    applied = c.execute(
        "SELECT COUNT(*) FROM greetings WHERE greeted_at >= ? AND greeted_at < ? "
        "AND status = 'sent'", (start, end)).fetchone()[0]

    # 新会话：只能用「我们第一次看到」的时间 —— 不是 HR 回复时间
    convs = c.execute(
        "SELECT COUNT(*) FROM conversations WHERE first_seen >= ? AND first_seen < ?",
        (start, end)).fetchone()[0]

    by_cat = dict(c.execute(
        "SELECT category, COUNT(*) FROM conversations "
        "WHERE first_seen >= ? AND first_seen < ? GROUP BY category",
        (start, end)).fetchall())

    return {
        "applied": applied,
        "new_convs": convs,
        "interview": by_cat.get("interview", 0),
        "rejected": by_cat.get("rejected", 0),
        "resume_sent": by_cat.get("resume_sent", 0),
        "pending": by_cat.get("pending", 0),
        "by_category": by_cat,
    }


def last_applied_ts(c) -> float | None:
    row = c.execute(
        "SELECT MAX(greeted_at) FROM greetings WHERE status = 'sent'").fetchone()
    return row[0] if row and row[0] else None


def idle_days(c, today: dt.date | None = None) -> int | None:
    """距上次投递过了几天。从没投过返回 None。"""
    ts = last_applied_ts(c)
    if not ts:
        return None
    today = today or dt.date.today()
    last = dt.date.fromtimestamp(ts)
    return (today - last).days


# ── 诊断（推断，不是结论）────────────────────────────────
def diagnose(cur: dict, prev: dict) -> list[str]:
    """根据四层数字给「最可能的下一步」。全部是推断。"""
    out = []
    a, r = cur["applied"], cur["new_convs"]

    if a == 0 and prev.get("applied", 0) == 0:
        out.append("本期和上期都没投递 —— 没有新样本，等于没有进展。"
                   "先定一个小目标，比如今天就投 3 个。")
        return out

    if a == 0:
        out.append("本期没投，上期投了 %d 个 —— 别停，停了前面的沉没成本就白费。"
                   % prev["applied"])
        return out

    if a >= 10 and r == 0:
        out.append(
            f"投了 {a} 个，0 个会话 —— 大概率卡在「岗位匹配」或「简历第一眼」。"
            f"两个都便宜可查：先用 jobkit resume-check 看简历对目标岗覆盖率，"
            f"再用 jobkit company 排掉外包。")
    elif a >= 10 and r > 0 and cur["interview"] == 0:
        out.append(
            f"投了 {a} 个，来了 {r} 个会话但 0 面试 —— 卡在「聊得下去」这一层。"
            f"看 jobkit hr 把期望薪资/空窗期那几题过一遍。")
    elif cur["interview"] > 0:
        out.append(f"本期有 {cur['interview']} 个面试 —— 别再盲目加投，"
                   f"把面试准备好，这一场比十个新投递值钱。")

    if cur["rejected"] > 0:
        out.append(f"本期 {cur['rejected']} 个拒信 —— 记下是被哪一层拒的"
                   f"（简历/初聊/面试后），下次不再重复同样的错。")
    return out


def build(c, offset: int = 0, today: dt.date | None = None) -> dict:
    start, end, label = week_bounds(offset, today)
    p_start, p_end, p_label = week_bounds(offset - 1, today)

    cur = collect(c, start, end)
    prev = collect(c, p_start, p_end)
    targets = load_targets()

    # 历史周要按「那一周结束那天」算闲置天数，不能按今天 ——
    # 否则查三个月前的周报会说"95 天前没投"，那是在说今天，不是那一周。
    ref_day = today or dt.date.today()
    if offset < 0:
        ref_day = dt.date.fromtimestamp(end) - dt.timedelta(days=1)
    days = idle_days(c, ref_day)

    warnings = []
    if days is not None and days > IDLE_WEEKS_ALERT * 7:
        suffix = "（截至该周末）" if offset < 0 else ""
        warnings.append(f"最近一次投递在 {days} 天前{suffix} —— 这个数比任何报表都重要")

    apply_rate = (round(cur["applied"] / targets["apply"] * 100)
                  if targets.get("apply") else None)

    return {
        "ok": True,
        "label": label,
        "prev_label": p_label,
        "cur": cur,
        "prev": prev,
        "targets": targets,
        "apply_rate": apply_rate,
        "idle_days": days,
        "diagnosis": diagnose(cur, prev),
        "warnings": warnings,
    }


def render(w: dict) -> str:
    if not w.get("ok"):
        return f"周报生成失败：{w.get('reason', '')}"

    cur, prev, t = w["cur"], w["prev"], w["targets"]
    L = [f"求职周报  {w['label']}", ""]

    def row(name, now, before=None, target=None):
        s = f"  {name:<8}{now:>5}"
        if target is not None:
            s += f"   （目标 {target}，达成 {round(now / target * 100) if target else 0}%）"
        if before is not None:
            delta = now - before
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            s += f"   环比 {arrow}{abs(delta) if delta else ''}"
        return s

    L.append(row("投递", cur["applied"], prev["applied"], t.get("apply")))
    L.append(row("新会话", cur["new_convs"], prev["new_convs"]))
    L.append(f"  {'面试':<8}{cur['interview']:>5}")
    L.append(f"  {'拒信':<8}{cur['rejected']:>5}")

    L.append("")
    L.append(f"  新会话按「我们第一次看到」算，不是 HR 回复时间 —— "
             f"BOSS 只给相对时间")

    if w["diagnosis"]:
        L.append("")
        L.append("诊断（推断，不是结论）：")
        for d in w["diagnosis"]:
            L.append(f"  · {d}")

    if w["warnings"]:
        L.append("")
        for x in w["warnings"]:
            L.append(f"⚠ {x}")

    return "\n".join(L)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import store

    offset = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    conn = store.connect()
    try:
        print(render(build(conn, offset)))
    finally:
        conn.close()
