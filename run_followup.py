#!/usr/bin/env python3
"""投递后跟进引擎的执行入口 —— 把 followup.py 的判定层接到 cron / 手动触发上。

职责边界：
  · followup.py   纯判定：谁该跟进、什么档位、文案、发送时段、每日额度、幂等登记
  · 本脚本        取 greetings 台账 → 调判定 → （默认只打印 / --send 才发）→ 记状态 → 退出码
  · 真实发送      先过 apply_pipeline.delivery_channel_ok()（同一条通道门禁），
                  发送函数是注入点：默认实现明确报「通道未接入」，绝不自己起 HTTP

为什么 --send 默认发不出去：
  boss-agent-cli 目前没有「给求职者发聊天消息」的命令（chat / chatmsg 都是读）。
  没有通道还硬发只会伪造成功，所以默认 sender 直接返回 NO_SENDER_CHANNEL，exit 2。
  等有真通道时，把 sender 换掉即可，判定/幂等/门禁都不用改。

默认 dry-run：只打印计划，不发送、不写 followup_state.json。
真实发送必须显式 --send，且复用既有门禁。

退出码（项目约定）：0 = OK（cron 静默）2 = ALERT（需要人看）1 = ERROR（脚本自身坏了）

用法：
    python run_followup.py --once --dry-run      # 看今天该跟谁（默认）
    python run_followup.py --once --send         # 真发（无通道时会被拒，exit 2）
    python run_followup.py --loop                # 常驻，每 300 秒看一次
    python run_followup.py --once --dry-run --cfg '{"ttl_days": 10}'
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

import followup as F  # noqa: E402
# 进程级约束只留一份实现：锁、退出码、路径都从 run_schedule 借用
from run_schedule import (  # noqa: E402
    EXIT_ALERT, EXIT_ERROR, EXIT_OK, LOCK_FILE, acquire_lock, release_lock,
)

STATE_PATH = HERE / "followup_state.json"
SEPARATOR = "═" * 62

# 一出现就该停手的错误（再发下去只会更糟）
SEND_BLOCKERS = (
    "NO_SENDER_CHANNEL", "AUTH_EXPIRED", "RATE_LIMITED", "ACCOUNT_RISK",
    "FORBIDDEN", "GREET_LIMIT", "COMPLIANCE_BLOCKED", "OUTCOME_UNKNOWN", "KERNEL_REQUIRED", "风控", "登录已过期",
)

STAGE_LABEL = {"gentle": "轻提醒", "polite": "礼貌追问", "direct": "问结果"}


def _fmt_ms(ms) -> str:
    return datetime.fromtimestamp(float(ms) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def load_greeted_rows(conn=None) -> list[dict]:
    """从 jobkit.db 读已投递且还活着的记录（greetings JOIN jobs 补岗位名）。

    岗位名/公司名先取 jobs 表，取不到就从 **greetings.job_snapshot 兜底** ——
    jobs 表会被新搜索覆盖、岗位也会下架，实测 28 条历史投递的 job_id 在 jobs
    里已全查不到；只有快照能证明「当初投的是谁」。没有公司名就定位不到聊天
    会话，跟进/已读未回/归因全断在这里。

    store.connect() 会顺手建表：库不存在就是空库 → 返回 []，不是错误。
    """
    import store
    own = conn is None
    conn = conn if conn is not None else store.connect()
    try:
        rows = conn.execute(
            "SELECT g.job_id, g.security_id, g.greeted_at, g.status, g.message, g.error, "
            "       COALESCE(NULLIF(j.title, ''), "
            "                CASE WHEN json_valid(g.job_snapshot) "
            "                     THEN json_extract(g.job_snapshot, '$.title') END) AS title, "
            "       COALESCE(NULLIF(j.company, ''), "
            "                CASE WHEN json_valid(g.job_snapshot) "
            "                     THEN json_extract(g.job_snapshot, '$.company') END) AS company, "
            "       COALESCE(NULLIF(j.city, ''), "
            "                CASE WHEN json_valid(g.job_snapshot) "
            "                     THEN json_extract(g.job_snapshot, '$.city') END) AS city "
            "FROM greetings g LEFT JOIN jobs j ON j.job_id = g.job_id "
            "WHERE g.status = 'sent'"
        ).fetchall()
    finally:
        if own:
            conn.close()
    return [dict(r) for r in rows]


def load_replied_companies() -> list[str]:
    """有 HR 消息（from_me=0）的公司名 —— 用来把「已经回过话」的投递摘出跟进队列。

    只认对方发过消息的会话：我们自己发出去的招呼不算回音。
    """
    import store
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT company FROM conversations "
            "WHERE from_me=0 AND company IS NOT NULL AND company != ''"
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows if str(r[0] or "").strip()]


def _replied_matcher():
    """公司名匹配用 store 的归一化规则（会话名比岗位库短，等值比会漏）。"""
    import store
    return store.companies_match


def _channel_gate() -> tuple[bool, str, str]:
    """复用 apply_pipeline 的通道门禁（Bridge/CDP 才算放行，headless 一律拒）。"""
    import apply_pipeline as AP
    return AP.delivery_channel_ok()


def make_cdp_sender(*, live: bool = True, keep_text: bool = False):
    """真实发送：走 chat_send（在已登录聊天页里替前端输入 + 点发送）。

    live=False 时只把文字填进输入框再清空，用来验证通道而不打扰任何人。
    live=False + keep_text=True：填好**留着**，让用户自己看一眼点发送（风险为零）。
    会话按**公司名唯一匹配**定位；找不到或匹配到多家一律不发（不猜）。
    """
    filled = {"n": 0}

    def _send(item: dict, now_ms: int) -> tuple[bool, str]:
        import chat_send as CS
        company = str(item.get("company") or "").strip()
        text = str(item.get("message") or "").strip()
        if not company:
            return False, "NO_COMPANY: 记录缺公司名，无法定位会话"
        if not text:
            return False, "NO_MESSAGE: 跟进文案为空"
        # 聊天窗口只有一个输入框：填第二条会覆盖第一条，用户来不及发。
        if keep_text and filled["n"] >= 1:
            return False, "ONE_AT_A_TIME: 已填好一条，去浏览器里发完再跑一次"
        try:
            res = CS.send(company, text, dry_run=not live, keep_text=keep_text)
        except CS.SendSkip as exc:
            return False, f"SKIP: {exc}"
        except CS.SendError as exc:
            return False, f"SEND_FAILED: {exc}"
        if res.get("dry_run"):
            if keep_text:
                filled["n"] += 1
            return True, ""
        if not res.get("ok") or not res.get("delivery_confirmed"):
            return False, "OUTCOME_UNKNOWN: 尚无平台送达确认，需在原平台核对"
        return True, ""

    return _send


def default_sender(item: dict, now_ms: int) -> tuple[bool, str]:
    """默认发送实现：没有通道就明确失败，不假装成功。"""
    return False, (
        "NO_SENDER_CHANNEL: 跟进真实发送通道尚未接入"
        "（boss-agent-cli 没有给求职者发消息的命令；chat/chatmsg 都是读）"
    )


def _is_blocker(err: str) -> bool:
    raw = str(err or "")
    return any(sig in raw for sig in SEND_BLOCKERS)


def run_cycle(*, send: bool = False, state_path=None, rows_fn=None, sender=None,
              gate_fn=None, now_ms=None, cfg=None, out=print,
              replied_fn=None, matcher_fn=None, reply_check: bool = True,
              fill: bool = False) -> int:
    """跑一轮跟进判定（+ 可选发送）。返回退出码。

    注入点：rows_fn() -> list[dict]、sender(item, now) -> (ok, err)、gate_fn() -> (ok, ch, why)、
    replied_fn() -> list[str]（已回话公司）、matcher_fn() -> (a, b) -> bool（公司名匹配）。
    单测全部用 stub，绝不碰真库/真网络。
    """
    if send and fill:
        out("发送与仅填入草稿不能同时启用。")
        return EXIT_ERROR
    state_path = Path(state_path or STATE_PATH)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    conf = F.get_cfg(cfg)

    out(SEPARATOR)
    out(f"投递后跟进 · {_fmt_ms(now)} · " + ("真实发送（--send）" if send else
                                            "填入待你确认（--fill）" if fill else
                                            "dry-run（只打印，不发送）"))
    out(SEPARATOR)

    try:
        rows = (rows_fn or load_greeted_rows)()
    except Exception as exc:               # noqa: BLE001
        out(f"❌ 读投递台账失败：{type(exc).__name__}: {exc}")
        return EXIT_ERROR

    # 回音过滤：对方已经回过话的投递不再催。读不到会话表就停手 —— 盲催比不催更伤人。
    skipped: list[dict] = []
    if reply_check:
        try:
            replied = (replied_fn or load_replied_companies)()
        except Exception as exc:           # noqa: BLE001
            out(f"❌ 回音过滤失败：读不到会话表（{type(exc).__name__}: {exc}）")
            out("   无法确认谁已经回过话 —— 本轮不跟进，避免对着已回复的人继续催。")
            return EXIT_ERROR
        if matcher_fn is None:
            try:
                matcher_fn = _replied_matcher()
            except Exception:              # noqa: BLE001 —— 拿不到归一化就退回等值比
                matcher_fn = None
        split = F.split_by_reply(rows, replied, matcher_fn)
        rows, skipped = split["active"], split["skipped"]
        if replied or skipped:
            out(f"回音过滤：{len(replied)} 家已回话 → 跳过 {len(skipped)} 条不催")
            for s in skipped:
                out(f"     · {(s.get('title') or '（岗位名缺失）')[:30]} @ {s.get('company')}"
                    f"（对方已回话，不需要跟进）")

    try:
        state = F.load_state(state_path, strict=True)
    except (OSError, ValueError) as exc:
        out(f"🛑 无法读取跟进执行状态，未发送：{type(exc).__name__}")
        return EXIT_ERROR
    if state.get("pending"):
        out("🛑 有执行中或发送结果待核对的跟进，已停止自动处理；请先在原平台核对。")
        return EXIT_ALERT
    window_ok, window_why = F.check_send_window(now, conf)
    due = F.due_followups(rows, now, state, conf)

    out(f"发送时段：{'✅ ' if window_ok else '⏸ '}{window_why}")
    out(f"今日已发：{F.daily_count(state, now)} / {conf['daily_limit']}"
        f"（剩余额度 {F.remaining_quota(state, now, conf)}）")
    out(f"台账：{len(rows)} 条已投递记录 · 到期跟进 {len(due)} 条"
        f"（满 {conf['gentle_days']} 天开始，超过 {conf['ttl_days']} 天不再打扰）")

    if not due:
        out("没有到期的跟进。")
        return EXIT_OK

    for idx, item in enumerate(due, 1):
        job_id = item.get("job_id")
        title = (item.get("title") or "（岗位名缺失）")[:40]
        company = (item.get("company") or "")[:20]
        stage = item.get("stage") or "polite"
        out("")
        out(f"[{idx}/{len(due)}] {title}" + (f" @ {company}" if company else ""))
        out(f"     投递 {item.get('age_days')} 天前 · 档位 {stage}（{STAGE_LABEL.get(stage, stage)}）"
            f" · job_id={job_id}")
        out(f"     话术：{item.get('message')}")

    if not send and not fill:
        out("")
        out("dry-run：不发送、不写 followup_state.json（同样的计划下次还会出现）。")
        return EXIT_OK

    if not window_ok:
        out("")
        out(f"⏸ 不在发送时段，{len(due)} 条攒着，等窗口到了再发（exit 0，不是故障）。")
        return EXIT_OK

    ch_ok, ch, ch_why = (gate_fn or _channel_gate)()
    out("")
    out(f"浏览器通道：{'✅' if ch_ok else '❌'} {ch_why}（通道={ch}）")
    if not ch_ok:
        out("🛑 通道门禁拒绝：没有发送任何消息（禁止降级 headless）。")
        return EXIT_ALERT

    send_one = sender or default_sender
    sent = failed = 0
    for idx, item in enumerate(due, 1):
        job_id = item.get("job_id")
        if not fill:
            state.setdefault("pending", {})[job_id] = {"state": "executing", "ts": now}
            if not F.save_state(state_path, state):
                out("🛑 无法保存发送前状态，未调用发送工具。")
                return EXIT_ERROR
        try:
            ok, err = send_one(item, now)
        except Exception as exc:
            ok, err = False, f"OUTCOME_UNKNOWN: {type(exc).__name__}"
        if fill:
            if not ok:
                out(f"❌ 填入失败：{err}")
                return EXIT_ALERT
            out("已填入草稿，待人工确认；未记为已发送。")
            continue
        uncertain = not ok and any(code in str(err).upper() for code in (
            "OUTCOME_UNKNOWN", "TIMEOUT", "NETWORK_ERROR", "ECONNRESET", "HTTP_5", "SERVER_ERROR"))
        if uncertain:
            state["pending"][job_id] = {"state": "outcome_unknown", "ts": now}
            saved = F.save_state(state_path, state)
            out("🛑 发送结果待核对，未自动重发；后续运行也会停止自动处理。")
            return EXIT_ALERT if saved else EXIT_ERROR
        state["pending"].pop(job_id, None)
        if ok:
            F.mark_sent(state, job_id, now, item.get("stage"))
            sent += 1
            if not F.save_state(state_path, state):
                out("🛑 已调用发送但状态保存失败，停止后续处理；需核对发送结果。")
                return EXIT_ERROR
            out(f"[{idx}/{len(due)}] ✅ 已发送 job_id={job_id}")
            continue
        failed += 1
        if not F.save_state(state_path, state):
            out("🛑 状态保存失败，停止后续处理。")
            return EXIT_ERROR
        out(f"[{idx}/{len(due)}] ❌ 未发送 job_id={job_id}：{str(err)[:160]}")
        if _is_blocker(err):
            out("🛑 命中发送阻断信号，立即停止；恢复前需核对账号与渠道状态。")
            break

    if sent:
        out(f"状态已写：{state_path}")
    out("")
    out(f"本轮结束：发送 {sent} · 失败 {failed}")
    return EXIT_ALERT if failed else EXIT_OK


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="投递后跟进执行入口（默认 dry-run）",
        epilog="退出码：0=OK 2=ALERT 1=ERROR",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印计划（默认）")
    mode.add_argument("--send", action="store_true",
                      help="真实发送：先过 apply_pipeline 的通道门禁；无通道时会被拒")
    mode.add_argument("--fill", action="store_true",
                      help="把跟进文案填进聊天输入框但不发，你自己看一眼点发送"
                           "（一次只填一条；发完再跑一次）")

    how = ap.add_mutually_exclusive_group()
    how.add_argument("--once", action="store_true", help="跑一轮就退出（cron 用，默认）")
    how.add_argument("--loop", action="store_true", help="常驻，每 --interval 秒检查一次")

    ap.add_argument("--interval", type=int, default=300, metavar="SEC",
                    help="--loop 模式下两次检查的间隔秒数，默认 300")
    ap.add_argument("--state", default=str(STATE_PATH), help="跟进状态文件（默认 followup_state.json）")
    ap.add_argument("--lock", default=str(LOCK_FILE), help="单实例锁文件（默认 exec_runner.lock）")
    ap.add_argument("--cfg", default=None,
                    help='参数覆盖，JSON 字符串，如 \'{"ttl_days": 10, "daily_limit": 10}\'')
    ap.add_argument("--now-ms", type=int, default=None, help="指定毫秒时间戳（复盘/测试用）")
    ap.add_argument("--no-reply-check", action="store_true",
                    help="跳过回音过滤（默认开：对方已回过话的投递不再催）")

    args = ap.parse_args(argv)
    if args.cfg:
        try:
            over = json.loads(args.cfg)
        except ValueError:
            ap.error("--cfg 必须是合法 JSON")
        if not isinstance(over, dict):
            ap.error("--cfg 必须是 JSON 对象")
        args.cfg = over
    else:
        args.cfg = {}
    if args.loop and args.now_ms is not None:
        ap.error("--now-ms 只能配合 --once 使用")
    if args.loop and args.interval < 5:
        ap.error("--interval 至少 5 秒")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    common = dict(send=bool(args.send), fill=bool(args.fill),
                  state_path=args.state, cfg=args.cfg,
                  reply_check=not args.no_reply_check,
                  sender=(make_cdp_sender(live=True) if args.send
                          else make_cdp_sender(live=False, keep_text=True) if args.fill
                          else None))

    try:
        ok, why = acquire_lock(args.lock)
        if not ok:
            print(f"⏭ 本轮跳过：{why}")
            return EXIT_OK
        try:
            if args.loop:
                print(f"常驻模式：每 {args.interval} 秒检查一次"
                      f"（{'--send' if args.send else 'dry-run'}），Ctrl+C 退出")
                while True:
                    code = run_cycle(**common)
                    if code == EXIT_ERROR:
                        return code
                    time.sleep(args.interval)
            return run_cycle(now_ms=args.now_ms, **common)
        finally:
            release_lock(args.lock)
    except KeyboardInterrupt:
        print("\n⏹ 手动中断。")
        return EXIT_OK
    except Exception:                      # noqa: BLE001 —— 顶层边界
        traceback.print_exc()
        print("❌ run_followup 自身异常（exit 1）")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
