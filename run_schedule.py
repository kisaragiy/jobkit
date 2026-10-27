#!/usr/bin/env python3
"""定时投递槽位的执行入口 —— 把 schedule.py 的「纯逻辑」接到 cron / 手动触发上。

职责边界（刻意的，不要糊）：
  · schedule.py        只回答「什么时候该投」——本脚本负责叫醒它、回写状态、给退出码
  · apply_pipeline.py  唯一的投递实现，通道门禁（Bridge/CDP 都不可用 → exit 3）原样复用
  · 本脚本不 import requests、不碰浏览器、不自己发请求。真实发送只有一条路：
    apply_pipeline.main()，它自己的门禁说了算

默认 dry-run：只打印「谁会跑、跑什么、什么时候跑」，不发送、不写槽位状态。
只有显式 --send 才会真的调 apply_pipeline。

退出码（项目约定，cron 依赖）：
    0 = OK        今天的槽位跑完了 / 没到点 / 另一个 runner 正在跑（都算正常，cron 静默）
    2 = ALERT     需要人看：通道门禁拒绝、登录/额度问题、这一波重试到上限
    1 = ERROR     脚本自身坏了（异常、读写状态失败）
  绝不用 1 表示「今天没事做」。

幂等：同一个槽位同一次到点只投一次。
  · 成功 → schedule.mark_run() 写 last_run_ms + runs 到 schedule.json，pending_slots 下次就过滤掉
  · 失败/被拦 → 不写 last_run_ms（窗口内还能重试），但把尝试记进 exec_runner_state.json，
    同一波失败 MAX_ATTEMPTS_PER_OCCURRENCE 次后放弃并 exit 2，避免每 30 秒锤一次

用法：
    python run_schedule.py --once --dry-run          # 看计划（默认就是 dry-run）
    python run_schedule.py --once --send             # 真投（只在到点 5 分钟窗口内）
    python run_schedule.py --loop --send             # 常驻，每 30 秒看一次
    python run_schedule.py --once --dry-run --now-ms 1790000000000   # 复盘某个时刻

cron 示例（每 5 分钟看一眼，槽位自己决定投不投；--once 保证跑一轮就退）：
    */5 * * * * cd /d C:\\Users\\user\\jobkit && python run_schedule.py --once --send >> logs\\schedule.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import schedule as S  # noqa: E402

# ── 退出码契约 ────────────────────────────────────────────────
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALERT = 2

# ── 路径与常量 ────────────────────────────────────────────────
SCHEDULE_PATH = S.SCHEDULE_PATH                 # schedule.json（槽位 + runs 台账）
STATE_PATH = HERE / "exec_runner_state.json"    # 执行尝试台账（失败/被拦的重试计数）
LOCK_FILE = HERE / "exec_runner.lock"           # 与 run_followup 共用的单实例锁
LOCK_STALE_SEC = 3600                           # 锁超过 1 小时视为陈尸（进程崩了没释放）
MAX_ATTEMPTS_PER_OCCURRENCE = 2                 # 同一槽位同一波最多试几次

SEPARATOR = "═" * 62


# ── 单实例锁（实现方式对齐 collector.acquire_lock）─────────────
def _pid_alive(pid) -> bool:
    """pid 还活着吗。查不出来按「活的」处理（保守，宁可少跑不并发）。

    Windows 上 os.kill(pid, 0) 对不存在的 pid 抛 OSError(winerror=87)，
    对存在但没权限的进程抛 PermissionError —— 两种要和 ProcessLookupError 分开。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87:    # ERROR_INVALID_PARAMETER：pid 不存在
            return False
        return True


def acquire_lock(path=LOCK_FILE) -> tuple[bool, str]:
    """拿单实例锁。返回 (是否拿到, 说明)。

    O_CREAT|O_EXCL 独占创建，天然互斥；陈尸锁（进程已死 或 超过 1 小时）可抢占。
    run_schedule 与 run_followup 共用同一个锁文件 —— 两种 runner 也不能并发。
    """
    p = Path(path)
    now = time.time()
    if p.exists():
        try:
            info = json.loads(p.read_text(encoding="utf-8")) or {}
            age = now - float(info.get("at") or 0)
            pid = info.get("pid")
            if age < LOCK_STALE_SEC:
                if _pid_alive(pid):
                    who = info.get("script") or "另一个 runner"
                    return False, f"{who} 正在跑（pid={pid}，{age / 60:.0f} 分钟前启动）"
                # pid 已死 → 陈尸，清掉
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
        try:
            p.unlink()
        except OSError:
            pass
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, json.dumps({
                "pid": os.getpid(), "at": now, "script": "run_schedule.py",
            }).encode("utf-8"))
        finally:
            os.close(fd)
        return True, ""
    except OSError:
        return False, "拿不到执行锁（可能刚好被另一个进程抢占）"


def release_lock(path=LOCK_FILE) -> None:
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except OSError:
        pass


# ── 小工具 ───────────────────────────────────────────────────
def _fmt_ms(ms) -> str:
    return datetime.fromtimestamp(float(ms) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def _wait_label(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "时间未知"
    if s <= 0:
        return "现在就到点"
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"还需等 {h} 小时 {m} 分钟"
    return f"还需等 {max(1, m)} 分钟"


def _write_json_atomic(path, payload) -> bool:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def load_state(path) -> dict:
    """执行尝试台账；文件缺失/坏 JSON 都回退空结构，不抛。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"attempts": []}
    if not isinstance(raw, dict):
        return {"attempts": []}
    attempts = [a for a in (raw.get("attempts") or []) if isinstance(a, dict)]
    return {"attempts": attempts[-200:]}


def _attempt_count(state: dict, slot_id, at_ms) -> int:
    """这一波（同槽位同到点）失败/被拦了几次。sent 不算失败。"""
    n = 0
    for a in state.get("attempts") or []:
        if a.get("slot_id") != slot_id or a.get("at_ms") != at_ms:
            continue
        if a.get("result") != "sent":
            n += 1
    return n


def _record_attempt(path, slot_id, at_ms, result, note="", now_ms=None) -> None:
    st = load_state(path)
    st["attempts"].append({
        "slot_id": slot_id,
        "at_ms": int(at_ms),
        "result": result,
        "note": str(note or "")[:200],
        "ts": int(now_ms if now_ms is not None else time.time() * 1000),
    })
    st["attempts"] = st["attempts"][-200:]
    _write_json_atomic(path, st)


# ── 投递调用（唯一真实发送路径）──────────────────────────────
def _pipeline_main(argv):
    """调 apply_pipeline.main()，argv 走它自己的 argparse（门禁/登录/节奏都在里面）。"""
    import apply_pipeline as AP
    old = sys.argv
    sys.argv = ["apply_pipeline.py"] + [str(x) for x in argv]
    try:
        return int(AP.main() or 0)
    finally:
        sys.argv = old


def _invoke_pipeline(slot: dict, apply_fn, out) -> tuple[int, str]:
    """把一轮槽位交给投递流程。返回 (exit code, 说明)。

    只传 --limit（槽位数量）；不加 --dry-run 之类会篡改语义的开关。
    注意：apply_pipeline 没有「图片简历」开关，image_resume=1 的槽位这里只做提示。
    """
    qty = int(slot.get("quantity") or 0)
    fn = apply_fn or _pipeline_main
    try:
        code = int(fn(["--limit", str(qty)]))
        return code, f"apply_pipeline exit {code}"
    except SystemExit as exc:              # 被调用方自己 sys.exit()
        code = int(exc.code or 0)
        return code, f"apply_pipeline SystemExit {code}"
    except Exception as exc:               # noqa: BLE001 —— 边界处兜住，转成退出码
        out(f"     ❌ 投递流程抛异常：{type(exc).__name__}: {exc}")
        return EXIT_ERROR, f"{type(exc).__name__}: {exc}"


def _mark_done(schedule_path, slot_id, now_ms, note) -> bool:
    """回写这一波已执行（last_run_ms + runs），落盘失败返回 False。"""
    data = S.load(schedule_path)
    S.mark_run(slot_id, now_ms, True, note, data=data)
    S.save(schedule_path, data)
    return True


# ── 一轮 ─────────────────────────────────────────────────────
def run_cycle(*, send: bool = False, schedule_path=None, state_path=None,
              apply_fn=None, now_ms=None, out=print) -> int:
    """检查一轮：把到点且未执行的槽位跑掉（或只打印）。返回退出码。

    apply_fn(argv_list) -> int 是测试注入点；默认走 apply_pipeline.main()。
    """
    schedule_path = Path(schedule_path or SCHEDULE_PATH)
    state_path = Path(state_path or STATE_PATH)
    now = int(now_ms if now_ms is not None else time.time() * 1000)

    out(SEPARATOR)
    out(f"定时投递 · {_fmt_ms(now)} · " + ("真实投递（--send）" if send else "dry-run（只打印，不发送）"))
    out(SEPARATOR)

    data = S.load(schedule_path)
    slots = data.get("slots") or []
    if not slots:
        out(f"没有配置任何槽位（{schedule_path} 的 slots 为空）。")
        out("先建时段（jobkit 界面 / 手工写 schedule.json），否则这里什么都不会做。")
        return EXIT_OK

    pending = S.pending_slots(slots, now, data.get("runs"))
    if not pending:
        nxt = S.next_due(slots, now)
        if nxt:
            slot = nxt["slot"]
            out(f"没有到点的槽位。下一次：{S.slot_when_label(slot)}"
                f"（{_fmt_ms(nxt['at_ms'])}，{_wait_label(nxt['wait_seconds'])}）"
                f" · {int(slot.get('quantity') or 0)} 个")
        else:
            out("没有可用槽位：全部已停用，或 once 时段都已过期。")
        return EXIT_OK

    out(f"到点槽位 {len(pending)} 个（错过窗口 {S.MISS_WINDOW_MS // 60000} 分钟，迟到超过就不补跑）：")
    state = load_state(state_path)
    alert = False
    error = False

    for idx, row in enumerate(pending, 1):
        slot = row["slot"]
        slot_id = row["slot_id"]
        at_ms = int(row["at_ms"])
        qty = int(slot.get("quantity") or 0)
        image = bool(slot.get("image_resume"))

        out("")
        out(f"[{idx}/{len(pending)}] {S.slot_when_label(slot)} · {qty} 个"
            f" · {'图片简历' if image else '文字简历'}")
        out(f"     到点 {_fmt_ms(at_ms)}（迟到 {row['late_seconds']} 秒）")
        out(f"     估算耗时 {S.estimate_minutes(qty, image)} 分钟（保守值，不是上限）")

        if qty <= 0:
            out("     ⏭ 跳过：quantity=0（这一波没额度，不投也不标记）")
            continue
        if image:
            out("     ⚠️ apply_pipeline 没有图片简历开关，本轮按文字简历执行")

        tried = _attempt_count(state, slot_id, at_ms)
        if tried >= MAX_ATTEMPTS_PER_OCCURRENCE:
            out(f"     ⛔ 这一波已经失败/被拦 {tried} 次，不再重试（等下一个时段或人工处理）")
            alert = True
            continue

        if not send:
            out(f"     将要执行：python apply_pipeline.py --limit {qty}")
            out("     （dry-run：不发送、不写槽位状态，下次还会打印同样的计划）")
            continue

        out(f"     执行：python apply_pipeline.py --limit {qty}")
        code, detail = _invoke_pipeline(slot, apply_fn, out)

        if code == EXIT_OK:
            _mark_done(schedule_path, slot_id, now, f"run_schedule: limit={qty}")
            _record_attempt(state_path, slot_id, at_ms, "sent", detail, now)
            out("     ✅ 槽位已标记执行（实际发送结果以上方 apply_pipeline 输出为准）")
            continue

        result = "blocked" if code == 3 else "failed"
        _record_attempt(state_path, slot_id, at_ms, result, detail, now)
        if code == 3:
            out("     🛑 通道门禁拒绝：没有发送任何消息（Bridge/CDP 都不可用，禁止降级 headless）")
            alert = True
        elif code == EXIT_ERROR:
            out("     ❌ 投递脚本自身出错（exit 1）")
            error = True
        else:
            out(f"     ⚠️ 投递前置检查未通过（exit {code}）：登录态/额度/队列问题，需要人看")
            alert = True

    out("")
    out(SEPARATOR)
    if error:
        out("本轮结束：脚本错误（exit 1）")
        return EXIT_ERROR
    if alert:
        out("本轮结束：有需要人看的项（exit 2）")
        return EXIT_ALERT
    out("本轮结束：正常（exit 0）" + ("" if send else " · dry-run 未发送"))
    return EXIT_OK


# ── CLI ─────────────────────────────────────────────────────
def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="定时投递槽位执行入口（默认 dry-run）",
        epilog="退出码：0=OK 2=ALERT 1=ERROR",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="只打印计划，不发送（默认行为，写出来是为了自证）")
    mode.add_argument("--send", action="store_true",
                      help="真实投递：复用 apply_pipeline 的通道门禁，没有别的发送路径")

    how = ap.add_mutually_exclusive_group()
    how.add_argument("--once", action="store_true", help="跑一轮就退出（cron 用，默认）")
    how.add_argument("--loop", action="store_true", help="常驻，每 --interval 秒检查一次")

    ap.add_argument("--interval", type=int, default=30, metavar="SEC",
                    help="--loop 模式下两次检查的间隔秒数，默认 30")
    ap.add_argument("--schedule", default=str(SCHEDULE_PATH), help="槽位文件（默认 schedule.json）")
    ap.add_argument("--state", default=str(STATE_PATH), help="执行尝试台账（默认 exec_runner_state.json）")
    ap.add_argument("--lock", default=str(LOCK_FILE), help="单实例锁文件（默认 exec_runner.lock）")
    ap.add_argument("--now-ms", type=int, default=None,
                    help="用指定毫秒时间戳代替当前时间（复盘/测试用）")

    args = ap.parse_args(argv)
    if args.loop and args.now_ms is not None:
        ap.error("--now-ms 只能配合 --once 使用（常驻模式必须跟着真实时间走）")
    if args.loop and args.interval < 5:
        ap.error("--interval 至少 5 秒")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    send = bool(args.send)
    common = dict(send=send, schedule_path=args.schedule, state_path=args.state)

    try:
        ok, why = acquire_lock(args.lock)
        if not ok:
            # 并发保护触发：不是故障，是设计。cron 静默即可。
            print(f"⏭ 本轮跳过：{why}")
            return EXIT_OK

        try:
            if args.loop:
                print(f"常驻模式：每 {args.interval} 秒检查一次"
                      f"（{'--send' if send else 'dry-run'}），Ctrl+C 退出")
                while True:
                    code = run_cycle(**common)
                    if code == EXIT_ERROR:
                        return code
                    try:
                        time.sleep(args.interval)
                    except KeyboardInterrupt:
                        raise
            return run_cycle(now_ms=args.now_ms, **common)
        finally:
            release_lock(args.lock)

    except KeyboardInterrupt:
        print("\n⏹ 手动中断（本轮已发出的不受影响）。")
        return EXIT_OK
    except Exception:                      # noqa: BLE001 —— 顶层边界
        traceback.print_exc()
        print("❌ run_schedule 自身异常（exit 1）")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
