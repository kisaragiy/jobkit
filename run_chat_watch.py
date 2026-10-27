#!/usr/bin/env python3
"""HR 回消息的信号灯 —— 有 HR 回话就喊人，没有就闭嘴。

为什么需要这个模块（2026-10-20）
--------------------------------
jobkit 会抓会话（`chat_sync.py` → `conversations` 表），界面里也有待回列表，
但**没有任何东西会主动告诉你「HR 回了」**。真机数据：2026-10-20 一天里就有
4 条面试/要简历的消息躺在库里（handled=0 从未被处理过一次），而人不会天天
主动去翻聊天页。采集、分析、投递都做了，唯独「对方说话了」这个神经末梢是断的。

职责边界
--------
  · chat_sync.py   抓取 + 写库（连 CDP 读 BOSS 聊天页）
  · 本脚本         读待处理会话 → 按紧急度排序 → 人读摘要 / JSON / 退出码
  · 不做          不标记 handled（要不要标记是人决定的，机器不替人结案）

退出码（项目约定）：0 = OK（cron 静默）2 = ALERT（有待回消息）1 = ERROR（抓不到且无法判定）

用法：
    python run_chat_watch.py                  # 抓一次 + 报待回
    python run_chat_watch.py --no-sync        # 不抓，只看库里已有的
    python run_chat_watch.py --json           # 机器消费
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

EXIT_OK, EXIT_ERROR, EXIT_ALERT = 0, 1, 2

STATE_PATH = HERE / "chat_watch_state.json"
SEPARATOR = "─" * 56

#: 紧急度：面试邀约可能今天就要回，拒信只是记录，排最后。
URGENCY = {"interview": 0, "resume_sent": 1, "normal": 2, "system": 3, "rejected": 4}
LABEL = {
    "interview": "面试/邀约",
    "resume_sent": "简历/附件",
    "normal": "普通消息",
    "system": "系统消息",
    "rejected": "拒信",
}


def _cat(item: dict) -> str:
    c = str(item.get("category") or "").strip().lower()
    return c if c in URGENCY else "normal"


def rank(items, now_ms: int | None = None, last_run: float | None = None) -> list[dict]:
    """排序 + 补 age_hours / is_new。

    · 紧急度：面试 > 简历/附件 > 普通 > 系统 > 拒信
    · 同紧急度：最近有动静的在前
    · is_new：会话首次出现晚于上次运行（首次运行 last_run=None → 全部不算新，
      只报基线，免得第一次跑就把历史消息全标成「新」）
    """
    now = float(now_ms if now_ms is not None else time.time() * 1000) / 1000.0
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        d = dict(it)
        cat = _cat(d)
        d["category"] = cat
        d["label"] = LABEL[cat]
        d["urgency"] = URGENCY[cat]
        ps = d.get("pending_since")
        try:
            d["age_hours"] = round((now - float(ps)) / 3600.0, 1) if ps else None
        except (TypeError, ValueError):
            d["age_hours"] = None
        if d.get("age_hours") is not None and d["age_hours"] < 0:
            d["age_hours"] = 0.0
        fs = d.get("first_seen")
        try:
            d["is_new"] = bool(last_run is not None and fs is not None
                               and float(fs) > float(last_run))
        except (TypeError, ValueError):
            d["is_new"] = False
        out.append(d)
    out.sort(key=lambda x: (x["urgency"], -float(x.get("updated_at") or 0)))
    return out


TIER_LABEL = {
    "fresh": "刚看到",
    "waiting": "等回音",
    "cold": "冷了",
    "unknown": "时间未知",
}

# 我自己主动收尾的话（婉拒/告别）—— 这不是「待跟进」，报出来等于自己催自己
_CLOSING_HINTS = ("不适合我", "不太适合我", "祝您早日找到", "祝早日找到合适")


def _is_closing(msg: str) -> bool:
    return any(h in msg for h in _CLOSING_HINTS)


def read_no_reply(rows, now_ms: int | None = None, *, fresh_hours: float = 24.0,
                  stale_hours: float = 72.0) -> list[dict]:
    """「我最后说的、对方已经读了、但没回」—— 按时间分两档。

    为什么单列：真机 67 条会话里，我发的 55 条根本没被看、6 条被读。
    已读不回和未读是**完全不同的问题**：
      · 未读 → 对方没上线/消息沉了，催也没用，别浪费额度
      · 已读不回 → 对方看了，话没打动他，该换话术或干脆放弃
    混在一起报，用户只会得到「都没戏」的挫败感。

    排除我自己发的婉拒/致谢：那是我主动结束的，不是待跟进。
    """
    now = float(now_ms if now_ms is not None else time.time() * 1000) / 1000.0
    out = []
    for it in rows or []:
        if not isinstance(it, dict):
            continue
        if not it.get("from_me") or not it.get("hr_read"):
            continue
        msg = str(it.get("last_msg") or "")
        if _is_closing(msg):
            continue
        # 时间基准：hr_read_at = 我们第一次看到「已读」的时刻（准）；
        # 只有它缺失时才退回 updated_at（那是「最后一次看到会话」，每次抓都刷新，
        # 只能算个上限，不能当成「对方读了多久」）。
        ts = it.get("hr_read_at")
        basis = "read_at"
        if not ts:
            ts = it.get("updated_at")
            basis = "updated_at"
        try:
            age = round((now - float(ts)) / 3600.0, 1) if ts else None
        except (TypeError, ValueError):
            age = None
        if age is not None and age < 0:
            age = 0.0
        if age is None:
            tier = "unknown"
        elif age < fresh_hours:
            tier = "fresh"      # 刚读，别急着催
        elif age < stale_hours:
            tier = "waiting"    # 等了一天多，可以再问一次
        else:
            tier = "cold"       # 三天没回，基本可以放弃了
        d = dict(it)
        d.update({"age_hours": age, "tier": tier,
                  "time_basis": basis,
                  "tier_label": TIER_LABEL[tier]})
        out.append(d)
    # 越冷越靠前：这些最需要用户做决定（换话术 / 放弃）
    order = {"cold": 0, "waiting": 1, "unknown": 2, "fresh": 3}
    out.sort(key=lambda d: (order.get(d["tier"], 9), -(d.get("age_hours") or 0)))
    return out


def render_read_no_reply(items) -> str:
    """已读未回单独一节。空列表返回空串（不打印空标题）。"""
    if not items:
        return ""
    lines = [f"👀 已读没回（{len(items)} 条）——对方看过了，问题在话术不在送达："]
    for d in items:
        who = d.get("hr_name") or ""
        com = d.get("company") or ""
        age = d.get("age_hours")
        if age is None:
            age_s = "时间未知"
        elif d.get("time_basis") == "read_at":
            # 这是「我们第一次观测到已读」的时刻，不是对方点开的那一刻 ——
            # 说成「已读 N 小时」等于编时间，宁可说清楚。
            age_s = "刚刚观测到已读" if age < 1 else f"{age:.0f} 小时前观测到已读"
        else:
            age_s = f"最后看到 {age:.0f} 小时前（非已读时刻）"
        snippet = str(d.get("last_msg") or "")[:34].replace("\n", " ")
        lines.append(f"  [{d['tier_label']}] {who} @ {com} · {age_s}")
        lines.append(f"     我说：{snippet}")
    cold = [d for d in items if d["tier"] == "cold"]
    if cold:
        lines.append(f"  → 其中 {len(cold)} 条已过 3 天，"
                     f"建议换一句具体的话术，或直接放弃这条线。")
    return "\n".join(lines)


def decide(items, *, sync_ok: bool = True) -> int:
    """退出码决策。

    抓不到 + 库里也没有待回 → **不能**说「没人找我」，那是 ERROR（无法判定），
    不是 OK。静默失败是这套脚本最想避免的事。
    """
    if items:
        return EXIT_ALERT
    return EXIT_OK if sync_ok else EXIT_ERROR


def _snippet(text, limit: int = 64) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


def render(items, *, sync_ok: bool = True, sync_error: str = "",
           data_as_of: float | None = None) -> str:
    """人读摘要。cron 直接把这段投递出去。"""
    if not items:
        return ""
    lines = [f"🐈‍⬛ jobkit · HR 消息待回 {len(items)} 条"]
    counts: dict[str, int] = {}
    for it in items:
        counts[it["label"]] = counts.get(it["label"], 0) + 1
    lines.append("   " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    if not sync_ok:
        as_of = ""
        if data_as_of:
            as_of = "（数据截至 " + datetime.fromtimestamp(data_as_of).strftime("%m-%d %H:%M") + "）"
        lines.append(f"  ⚠️ 本次没抓到聊天页{as_of}：{sync_error or '通道不可用'}")
        lines.append("     以下是库里已有的待回，可能不是最新")
    for it in items:
        age = it.get("age_hours")
        age_s = f"已挂起 {age:.1f}h" if age is not None else "挂起时长未知"
        flag = " 🆕" if it.get("is_new") else ""
        lines.append("")
        lines.append(f"【{it['label']}】{it.get('company') or '（公司名缺失）'}"
                     f" · {it.get('hr_name') or 'HR'}（{age_s}）{flag}")
        lines.append(f"    {_snippet(it.get('last_msg'))}")
    return "\n".join(lines)


def load_state(path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, data: dict) -> bool:
    """原子写；失败返回 False（调用方负责出声）。"""
    try:
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError:
        return False


def load_pending(conn) -> list[dict]:
    """读「对方发过话、我还没处理」的会话。"""
    import store
    return store.list_conversations(conn, pending_only=True, limit=200)


def _do_sync() -> tuple[bool, str, list[dict] | None]:
    """抓一次聊天页。返回 (ok, error, rows)；抓不到不抛给上层。"""
    import chat_sync
    try:
        res = chat_sync.sync()
        return True, "", res.get("rows") or []
    except Exception as exc:               # noqa: BLE001 —— 通道不可用是常态，不是崩溃
        return False, f"{type(exc).__name__}: {exc}", None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="HR 回消息提醒（默认抓一次再报待回）",
        epilog="退出码：0=没有待回 2=有待回消息 1=抓不到且无法判定",
    )
    ap.add_argument("--no-sync", action="store_true", help="跳过抓取，只读库里已有的")
    ap.add_argument("--json", action="store_true", help="输出单行 JSON")
    ap.add_argument("--state", default=str(STATE_PATH), help="上次运行时间戳（标 🆕 用）")
    ap.add_argument("--db", default=None, help="指定 jobkit.db 路径（复盘/测试用）")
    ap.add_argument("--limit", type=int, default=20, help="最多报几条，默认 20")
    args = ap.parse_args(argv)

    now = time.time()
    now_ms = int(now * 1000)
    state = load_state(args.state)
    last_run = state.get("last_run")
    last_run = float(last_run) if isinstance(last_run, (int, float)) else None

    sync_ok, sync_error = True, ""
    if not args.no_sync:
        sync_ok, sync_error, _ = _do_sync()

    try:
        import store
        if args.db:
            store.DB_PATH = Path(args.db)
        conn = store.connect()
        try:
            pending = load_pending(conn)
            # 全表：算「已读没回」。和 pending 是两个问题，不能混。
            all_convs = store.list_conversations(conn, pending_only=False, limit=300)
        finally:
            conn.close()
    except Exception as exc:               # noqa: BLE001
        print(f"❌ 读会话表失败（{type(exc).__name__}: {exc}）—— 无法判定是否有待回消息")
        return EXIT_ERROR

    items = rank(pending, now_ms=now_ms, last_run=last_run)[:max(1, args.limit)]
    read_items = read_no_reply(all_convs, now_ms=now_ms)[:max(1, args.limit)]
    code = decide(items, sync_ok=sync_ok)

    # 连续抓不到的计数：交给 cron wrapper 判「是浏览器没开的常态，还是通道真的坏了」
    try:
        prev_streak = int(state.get("sync_fail_streak") or 0)
    except (TypeError, ValueError):
        prev_streak = 0
    streak = 0 if sync_ok else prev_streak + 1

    if args.json:
        print(json.dumps({
            "pending": len(items),
            "read_no_reply": len(read_items),
            "sync_ok": sync_ok,
            "sync_error": sync_error,
            "sync_fail_streak": streak,
            "items": items,
            "read_items": read_items,
        }, ensure_ascii=False))
    else:
        text = render(items, sync_ok=sync_ok, sync_error=sync_error,
                      data_as_of=(now if sync_ok else None))
        if text:
            print(text)
        rn = render_read_no_reply(read_items)
        if rn:
            print()
            print(rn)
        if code == EXIT_ERROR:
            print(f"⚠️ 没抓到聊天页（{sync_error}），库里也没有待回记录 —— 不能断定没人找你。")

    if not save_state(args.state, {"last_run": now, "pending": len(items),
                                   "sync_fail_streak": streak}):
        print(f"⚠️ 提醒状态写失败（{args.state}）—— 下次可能重复标 🆕", file=sys.stderr)

    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_OK)
    except Exception:                      # noqa: BLE001 —— 顶层边界
        traceback.print_exc()
        print("❌ run_chat_watch 自身异常（exit 1）")
        sys.exit(EXIT_ERROR)
