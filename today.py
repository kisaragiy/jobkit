"""每日指挥中心：一屏看完今天该干什么。

不抓数据、不开浏览器 —— 只读库，所以随时能跑、可测、不会因为 CDP 没开而失败。
要刷新聊天页请先跑 `jobkit chat`（或交给定时任务）。

排序原则：**按「现在不动手会损失什么」排**，不按功能模块排。
HR 在等回复 = 机会在过期，永远排第一。
"""
from __future__ import annotations

import time

EXIT_OK = 0
EXIT_ALERT = 2
EXIT_ERROR = 1

# 超过这个时长，会话数据就不该被当成「现在」用
STALE_HOURS = 2.0


def build(conn=None, now_ms: int | None = None) -> dict:
    """聚合四类信息。任何一个来源坏了都不影响其它（各自 try）。"""
    import store

    now = float(now_ms if now_ms is not None else time.time() * 1000) / 1000.0
    now_ms_i = int(now * 1000)
    own = conn is None
    conn = conn if conn is not None else store.connect()
    out = {"now": now, "errors": []}
    # 0) 数据新鲜度：先算，因为其它数字可能都已经过时了
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) FROM conversations").fetchone()
        newest = float(row[0]) if row and row[0] else None
        out["conv_updated_at"] = newest
        out["conv_age_hours"] = (round((now - newest) / 3600, 1)
                                 if newest else None)
        out["conv_total"] = int(conn.execute(
            "SELECT COUNT(*) FROM conversations").fetchone()[0])
    except Exception as exc:                      # noqa: BLE001
        out["conv_updated_at"] = None
        out["conv_age_hours"] = None
        out["conv_total"] = 0
        out["errors"].append(f"读会话时间失败：{exc}")

    try:
        # 1) 待我回
        try:
            convs = store.list_conversations(conn, pending_only=False, limit=300)
        except Exception as exc:                      # noqa: BLE001
            convs = []
            out["errors"].append(f"读会话表失败：{exc}")

        try:
            import run_chat_watch as W
            pending_all = [c for c in convs
                           if not c.get("from_me") and not c.get("handled")]
            # 系统消息 / 我自己的婉拒 / 附件回执不该算「机会在过期」——
            # 混进来的话每天早上显示「6 条等你回」，其实只有 3 条真要回，白焦虑。
            try:
                import reply_draft as RD
                prof = RD.load_profile()
                actionable = [c for c in pending_all
                              if not RD.draft(c.get("last_msg") or "", prof)["skip"]]
            except Exception:                      # noqa: BLE001
                actionable = pending_all
            out["pending"] = W.rank(actionable, now_ms=now_ms_i)
            out["ignorable"] = len(pending_all) - len(actionable)
            out["read_no_reply"] = W.read_no_reply(convs, now_ms=now_ms_i)
            # 骗局多发生在聊天里而不是 JD 里：JD 写得正经，聊着聊着开始要钱。
            # 在你要回复之前先扫一遍，比投递前扫更救命。
            try:
                import tools as TL
                for c in out["pending"]:
                    r = TL.check_company(c.get("company") or "", "",
                                         c.get("last_msg") or "")
                    if r.get("level") == "high":
                        c["scam"] = [h["label"] for h in r["hits"]
                                     if h["level"] == "high"]
            except Exception:                      # noqa: BLE001
                pass
        except Exception as exc:                      # noqa: BLE001
            out["pending"], out["read_no_reply"], out["ignorable"] = [], [], 0
            out["errors"].append(f"排序会话失败：{exc}")

        # 2) 跟进到期
        try:
            import run_followup as RF
            import followup as F
            rows = RF.load_greeted_rows(conn)
            out["due"] = F.due_followups(rows, now_ms_i) if rows else []
            out["greeted_total"] = len(rows)
        except Exception as exc:                      # noqa: BLE001
            out["due"], out["greeted_total"] = [], 0
            out["errors"].append(f"算跟进到期失败：{exc}")

        # 3) 今日投递 / 今日失败
        try:
            # 按**本地**日界算，不是 now - now%86400 那种（那是 UTC 日界，
            # 北京时间要等到早上 8 点才换日，会把昨天的算成今天）
            import datetime
            today0 = datetime.datetime.fromtimestamp(now).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()

            def _sec(v) -> float:
                """秒/毫秒混存兜底 —— 历史上两种都出现过。"""
                v = float(v or 0)
                return v / 1000.0 if v > 1e11 else v

            # 成功看 greeted_at，失败看 attempted_at（失败时 greeted_at 是 NULL，
            # 拿它过滤会把失败整条漏掉）
            rows_g = conn.execute(
                "SELECT status, greeted_at, attempted_at FROM greetings").fetchall()
            out["sent_today"] = sum(
                1 for st, g, _a in rows_g if st == "sent" and _sec(g) >= today0)
            out["failed_today"] = sum(
                1 for st, _g, a in rows_g if st == "failed" and _sec(a) >= today0)
        except Exception as exc:                      # noqa: BLE001
            out["sent_today"] = out["failed_today"] = 0
            out["errors"].append(f"统计今日投递失败：{exc}")

        # 4) 可投岗位
        try:
            row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
            out["jobs_total"] = int(row[0]) if row else 0
        except Exception:                             # noqa: BLE001
            out["jobs_total"] = 0
    finally:
        if own:
            conn.close()
    return out


def fmt_age(hours: float) -> str:
    """把小时数说成人话。"""
    if hours < 1:
        return f"{max(1, int(hours * 60))} 分钟"
    return f"{hours:.1f} 小时"


def freshness_line(d: dict) -> str:
    """数据新鲜度。

    为什么必须放在最上面：一个指挥中心拿 6 小时前的会话数据说「有 3 条 HR 在等你回」，
    比不做还糟 —— 用户会以为自己看的是当前状态。
    """
    age = d.get("conv_age_hours")
    if age is None:
        return "⚠️ 还没有会话数据 —— 先跑 python jobkit.py chat 同步一次"
    if age >= STALE_HOURS:
        return (f"⚠️ 会话数据是 {fmt_age(age)}前的 —— 这期间的新消息看不到，"
                f"先跑 python jobkit.py chat 刷新")
    return f"会话数据 {fmt_age(age)}前同步（{d.get('conv_total', 0)} 条）"


def render(d: dict) -> str:
    pending = d.get("pending") or []
    read_nr = d.get("read_no_reply") or []
    due = d.get("due") or []
    lines = [f"🐈‍⬛ 今天该干什么 · {time.strftime('%m-%d %H:%M')}",
             freshness_line(d), ""]

    if pending:
        lines.append(f"① 有 {len(pending)} 条 HR 消息等你回 —— 机会在过期，先动这个")
        for it in pending[:5]:
            age = it.get("age_hours")
            age_s = f"（挂 {age:.0f}h）" if age is not None else ""
            lines.append(f"   · 【{it.get('label')}】{it.get('company') or '（公司名缺失）'}"
                         f" · {it.get('hr_name') or 'HR'}{age_s}")
            snippet = str(it.get("last_msg") or "")[:40].replace("\n", " ")
            if snippet:
                lines.append(f"       {snippet}")
            if it.get("scam"):
                lines.append(f"       ⚠️ 这段话在要钱（{'、'.join(it['scam'])}）"
                             " —— 入职前先掏钱一律是骗，别再往下聊")
        if len(pending) > 5:
            lines.append(f"   …还有 {len(pending) - 5} 条")
        if d.get("ignorable"):
            lines.append(f"   （另有 {d['ignorable']} 条是系统消息/回执/已婉拒，不用管）")
        lines.append("   → python jobkit.py reply")
    else:
        lines.append("① 没有等你回的消息")

    lines.append("")
    if read_nr:
        cold = [x for x in read_nr if x.get("tier") == "cold"]
        lines.append(f"② {len(read_nr)} 条已读没回（对方看过了，问题在话术）")
        # 同一句话被多个人已读不回 = 话术本身的问题，不是运气
        from collections import Counter
        msgs = Counter((x.get("last_msg") or "").strip() for x in read_nr)
        if msgs:
            top, n = msgs.most_common(1)[0]
            if n >= 2 and top:
                lines.append(f"   其中 {n} 条是同一句话：「{top[:30]}」")
                lines.append("   同一句话没人回，说明问题在这句话，不在运气")
        if cold:
            lines.append(f"   另有 {len(cold)} 条已过 3 天 —— 换一句具体的话术，或放弃")
    else:
        lines.append("② 没有已读没回的")

    lines.append("")
    if due:
        lines.append(f"③ {len(due)} 条投递该跟进了")
        for it in due[:3]:
            lines.append(f"   · {it.get('company') or '（岗位名缺失）'} · 第 {it.get('round')} 轮")
        lines.append("   → python jobkit.py followup --dry-run 看话术")
    else:
        lines.append("③ 没有到期跟进")

    lines.append("")
    sent_today = d.get("sent_today", 0)
    failed_today = d.get("failed_today", 0)
    seg = f"④ 今日已投 {sent_today}"
    if failed_today:
        seg += f" · 失败 {failed_today}（失败的不算投过，重跑会自动重排）"
    seg += f" · 库里 {d.get('jobs_total', 0)} 个岗位"
    lines.append(seg)

    if d.get("sync_error"):
        lines.append("")
        lines.append(d["sync_error"])

    if d.get("errors"):
        lines.append("")
        for e in d["errors"]:
            lines.append(f"⚠️ {e}")
    return "\n".join(lines)


def decide(d: dict) -> int:
    """有 HR 在等 → 2（要你处理）；数据读不出来 → 1；没事 → 0。"""
    if d.get("errors"):
        return EXIT_ERROR
    if d.get("pending"):
        return EXIT_ALERT
    return EXIT_OK


def _sync_now() -> tuple[bool, str]:
    """可选：当场抓一次聊天页。抓不到不抛异常 —— 浏览器没开是常态。"""
    try:
        import run_chat_watch as W
        ok, err, _rows = W._do_sync()
        return ok, err
    except Exception as exc:                      # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="每日指挥中心：一屏看完今天该干什么（只读库，不抓取）",
        epilog="退出码：0=没事 2=有 HR 等你回 1=数据读不出来")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--sync", action="store_true",
                    help="先抓一次聊天页再算（需要浏览器在 /web/geek/chat）")
    args = ap.parse_args(argv)

    sync_note = ""
    if args.sync:
        ok, err = _sync_now()
        sync_note = "" if ok else f"⚠️ 同步失败（{err[:80]}），下面是库里已有的数据"

    d = build()
    if sync_note:
        # 单独放，不进 errors —— errors 会让退出码变成 1（脚本坏了），
        # 但同步失败只是通道没开，库里的数据照样能看
        d["sync_error"] = sync_note
    if args.json:
        import json
        print(json.dumps({
            "pending": len(d.get("pending") or []),
            "read_no_reply": len(d.get("read_no_reply") or []),
            "due": len(d.get("due") or []),
            "ignorable": d.get("ignorable", 0),
            "sent_today": d.get("sent_today", 0),
            "failed_today": d.get("failed_today", 0),
            "jobs_total": d.get("jobs_total", 0),
            "conv_age_hours": d.get("conv_age_hours"),
            "conv_total": d.get("conv_total", 0),
            "stale": (d.get("conv_age_hours") or 0) >= STALE_HOURS,
            "sync_error": d.get("sync_error") or "",
            "errors": d.get("errors") or [],
        }, ensure_ascii=False))
    else:
        print(render(d))
    return decide(d)


if __name__ == "__main__":
    import sys
    sys.exit(main())
