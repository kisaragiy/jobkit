#!/usr/bin/env python3
"""台账看板 —— 看清"抓了什么 / 投了谁 / 还差什么"。

用法：
    python ledger.py                # 文本看板
    python ledger.py --html         # 输出 HTML 看板（可放进桌面预览窗格）
    python ledger.py --pending 30   # 只看待投递候选前 30 条
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules  # noqa: E402
import store  # noqa: E402


def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return "—"


def day_key(ts) -> str:
    if not ts:
        return "未知日期"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (ValueError, OSError, TypeError):
        return "未知日期"


def collect(c: sqlite3.Connection) -> dict:
    jobs = [dict(r) for r in c.execute("SELECT * FROM jobs")]
    greets = {r["job_id"]: dict(r) for r in c.execute("SELECT * FROM greetings")}
    job_by_id = {j["job_id"]: j for j in jobs}

    # 给每个岗位打过滤标记
    for j in jobs:
        j.update(dict(zip(("pass", "reject_reason"), rules.job_ok(j))))

    passed = [j for j in jobs if j["pass"]]
    rejected = [j for j in jobs if not j["pass"]]

    sent = {jid: g for jid, g in greets.items() if g.get("status") == "sent"}
    failed = {jid: g for jid, g in greets.items() if g.get("status") == "failed"}

    pending = [j for j in passed if j["job_id"] not in sent]
    # 待投递排序：Boss 活跃优先 → 有 match_score 的高分优先
    def rank(j):
        return (
            0 if (j.get("boss_active") or "").strip() in ("在线", "刚刚活跃", "今日活跃") else 1,
            -(j.get("match_score") or 0),
            j.get("company") or "",
        )
    pending.sort(key=rank)

    applied = []
    for jid, g in sent.items():
        j = job_by_id.get(jid, {})
        snap = {}
        raw = g.get("job_snapshot")
        if raw:
            try:
                snap = json.loads(raw) or {}
            except (ValueError, TypeError):
                snap = {}
        # jobs 表现有的字段优先（更新），快照兜底（岗位下架/被新搜索覆盖时）
        merged = {**snap, **{k: v for k, v in j.items() if v}}
        applied.append({
            "job_id": jid,
            "greeted_at": g.get("greeted_at"),
            "title": merged.get("title") or "",
            "company": merged.get("company") or "",
            "salary": merged.get("salary") or "",
            "city": merged.get("city") or "",
            "url": merged.get("url") or "",
            "source": g.get("error") or "",
            # 没有岗位名的记录单独归组，不再逐条打「信息缺失」占位符
            "has_info": bool(merged.get("title")),
        })
    applied.sort(key=lambda x: -(x["greeted_at"] or 0))
    unknown = [a for a in applied if not a["has_info"]]
    applied = [a for a in applied if a["has_info"]]

    return {"jobs": jobs, "passed": passed, "rejected": rejected,
            "pending": pending, "applied": applied, "unknown_applied": unknown,
            "failed": failed}


def render_text(d: dict, pending_limit: int) -> str:
    out = []
    jobs, passed, rejected = d["jobs"], d["passed"], d["rejected"]
    pending, applied = d["pending"], d["applied"]
    today = datetime.now().strftime("%Y-%m-%d")

    today_sent = [a for a in applied if day_key(a["greeted_at"]) == today]
    unknown = d.get("unknown_applied") or []

    out.append("═" * 66)
    out.append("  jobkit 台账看板")
    out.append("═" * 66)
    out.append(f"\n■ 概况")
    out.append(f"   岗位库        {len(jobs)} 条（通过筛选 {len(passed)} · 被规则拒绝 {len(rejected)}）")
    out.append(f"   已投递        {len(applied) + len(unknown)} 条（今日 {len(today_sent)} 条）")
    out.append(f"   待投递        {len(pending)} 条")
    if d["failed"]:
        out.append(f"   投递失败      {len(d['failed'])} 条（见 --html 详情）")

    # 城市分布
    city: dict[str, int] = {}
    for j in passed:
        city[j.get("city") or "未标"] = city.get(j.get("city") or "未标", 0) + 1
    if city:
        out.append(f"\n   通过筛选的岗位城市分布：" +
                   " · ".join(f"{k} {v}" for k, v in sorted(city.items(), key=lambda x: -x[1])[:8]))

    # 拒绝原因 top
    reasons: dict[str, int] = {}
    for j in rejected:
        r = (j.get("reject_reason") or "").split(" ")[0]
        reasons[r] = reasons.get(r, 0) + 1
    if reasons:
        out.append(f"   拒绝主因：" + " · ".join(f"{k}×{v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:5]))

    out.append(f"\n■ 待投递候选（前 {min(pending_limit, len(pending))} 条，按 Boss 活跃度 + 匹配分排序）")
    if not pending:
        out.append("   （空 —— 需要先跑一轮 boss search 抓岗）")
    for i, j in enumerate(pending[:pending_limit], 1):
        act = (j.get("boss_active") or "").strip()
        flag = "🟢" if act in ("在线", "刚刚活跃", "今日活跃") else "  "
        score = f"{j['match_score']:.0f}" if j.get("match_score") else "—"
        out.append(f"  {flag}{i:>2}. {j.get('title','')[:30]:<32} {j.get('company','')[:16]:<18} "
                   f"{j.get('salary','')[:14]:<15} {j.get('city','')}  分{score}")

    out.append(f"\n■ 已投递台账（最近 20 条 / 共 {len(applied)} 条有岗位信息）")
    if not applied:
        out.append("   （无记录）")
    for a in applied[:20]:
        out.append(f"  {fmt_ts(a['greeted_at']):<12} {a['title'][:30]:<32} "
                   f"{a['company'][:16]:<18} {a['salary'][:14]:<15} {a['city']}")
    if unknown:
        out.append(f"\n   ○ {len(unknown)} 条早期投递只存了 job_id，没有岗位信息")
        out.append(f"     原因：那些记录是旧版写的（当时投递不存岗位名），岗位后来")
        out.append(f"     被新搜索结果覆盖或下架，补不回来。从本版起投递会存快照。")

    out.append("\n" + "─" * 66)
    out.append("下一步：python apply_pipeline.py --dry-run   预览本轮要投哪些岗（不发送）")
    return "\n".join(out)


CSS = """
:root{--fg:var(--foreground,#111);--mut:var(--muted-foreground,#666);--bd:var(--border,#e5e5e5);--ac:var(--accent,#00bebd);--card:var(--card,#fff)}
body{margin:0;font-family:var(--app-font,system-ui,-apple-system,'Segoe UI',sans-serif);color:var(--fg)}
h1{font-size:17px;margin:0 0 14px}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.card{border:1px solid var(--bd);border-radius:9px;padding:10px 14px;min-width:96px;background:var(--card)}
.card .n{font-size:23px;font-weight:650;line-height:1.15}
.card .l{font-size:11px;color:var(--mut);margin-top:3px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-bottom:18px}
th{text-align:left;font-weight:600;color:var(--mut);border-bottom:1px solid var(--bd);padding:6px 8px;font-size:11px}
td{padding:6px 8px;border-bottom:1px solid var(--bd);vertical-align:top}
tr.pass td:first-child{border-left:3px solid var(--ac)}
.tag{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:4px;border:1px solid var(--bd);color:var(--mut)}
h2{font-size:13px;margin:20px 0 8px;color:var(--mut);font-weight:600}
"""


def render_html(d: dict, pending_limit: int) -> str:
    jobs, passed, pending = d["jobs"], d["passed"], d["pending"]
    applied = d["applied"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_sent = [a for a in applied if day_key(a["greeted_at"]) == today]
    unknown = d.get("unknown_applied") or []

    def esc(s):
        return html.escape(str(s if s is not None else ""))

    rows = []
    for i, j in enumerate(pending[:pending_limit], 1):
        act = (j.get("boss_active") or "").strip()
        online = act in ("在线", "刚刚活跃", "今日活跃")
        rows.append(
            f"<tr><td>{i}</td><td>{esc(j.get('title',''))}</td><td>{esc(j.get('company',''))}</td>"
            f"<td>{esc(j.get('salary',''))}</td><td>{esc(j.get('city',''))}</td>"
            f"<td>{'🟢 ' + esc(act) if online else esc(act)}</td>"
            f"<td>{esc(j.get('education',''))}</td>"
            f"<td>{j['match_score']:.0f}</td></tr>"
            if j.get("match_score") else
            f"<tr><td>{i}</td><td>{esc(j.get('title',''))}</td><td>{esc(j.get('company',''))}</td>"
            f"<td>{esc(j.get('salary',''))}</td><td>{esc(j.get('city',''))}</td>"
            f"<td>{'🟢 ' + esc(act) if online else esc(act)}</td>"
            f"<td>{esc(j.get('education',''))}</td><td>—</td></tr>"
        )

    arows = "".join(
        f"<tr><td>{fmt_ts(a['greeted_at'])}</td><td>{esc(a['title'])}</td>"
        f"<td>{esc(a['company'])}</td><td>{esc(a['salary'])}</td><td>{esc(a['city'])}</td></tr>"
        for a in applied[:60]
    ) or '<tr><td colspan="5" style="color:var(--mut)">无记录</td></tr>'

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>jobkit 台账看板</title><style>{CSS}</style></head><body>
<h1>jobkit 台账看板 <span class="tag">{datetime.now().strftime('%Y-%m-%d %H:%M')}</span></h1>
<div class="cards">
  <div class="card"><div class="n">{len(jobs)}</div><div class="l">岗位库</div></div>
  <div class="card"><div class="n">{len(passed)}</div><div class="l">通过筛选</div></div>
  <div class="card"><div class="n">{len(pending)}</div><div class="l">待投递</div></div>
  <div class="card"><div class="n">{len(applied) + len(unknown)}</div><div class="l">已投递</div></div>
  <div class="card"><div class="n">{len(today_sent)}</div><div class="l">今日投递</div></div>
</div>
<h2>待投递候选（前 {min(pending_limit, len(pending))} 条 · Boss 活跃优先）</h2>
<table><thead><tr><th>#</th><th>岗位</th><th>公司</th><th>薪资</th><th>城市</th><th>Boss</th><th>学历</th><th>匹配分</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="8" style="color:var(--mut)">空 —— 需要先跑 boss search 抓岗</td></tr>'}</tbody></table>
<h2>已投递台账（最近 60 条 / 共 {len(applied)} 条有岗位信息）</h2>
{'<p style="font-size:12px;color:var(--mut);margin:-4px 0 10px">另有 ' + str(len(unknown)) + ' 条早期投递只存了 job_id（旧版不存岗位名，岗位已被新搜索覆盖），无法补全。从本版起投递会存完整快照。</p>' if unknown else ''}
<table><thead><tr><th>时间</th><th>岗位</th><th>公司</th><th>薪资</th><th>城市</th></tr></thead>
<tbody>{arows}</tbody></table>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", action="store_true", help="输出 HTML 看板")
    ap.add_argument("--out", default=str(Path(__file__).parent / "ledger.html"))
    ap.add_argument("--pending", type=int, default=25, help="待投递显示条数")
    args = ap.parse_args()

    c = store.connect()
    d = collect(c)
    c.close()

    if args.html:
        Path(args.out).write_text(render_html(d, args.pending), encoding="utf-8")
        print(args.out)
    else:
        print(render_text(d, args.pending))
    return 0


if __name__ == "__main__":
    sys.exit(main())
