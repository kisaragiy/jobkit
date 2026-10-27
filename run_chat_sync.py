"""定时抓取 BOSS 消息（给 Windows 计划任务 / cron 用）。

返回值契约（和 scripts/lib/contract.py 一致）
----------------------------------------------
0 = 正常（没新消息 / 一切正常）
2 = 需要人看（有新回复 或 有面试邀约）
1 = 出错（浏览器没开 / 没登录 / 页面改版）

为什么 2 表示"有新消息"而不是 1：新消息是**正常业务事件**，不是故障。
把它当 error 会让监控告警噪音化，真故障反而被淹掉。

为什么抓取失败不能静默返回 0：空列表会被当成"今天没人理你"，
用户就真的不看了 —— 这是最坏的失败方式（假阴性）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_sync  # noqa: E402
import store  # noqa: E402


def main() -> int:
    try:
        result = chat_sync.sync()
    except chat_sync.ChatUnavailable as e:
        # 浏览器/登录/页面问题 —— 必须显式报错，不能返回空列表糊过去
        print(f"ALERT 消息抓取失败：{e}")
        print("（这不是「今天没人理你」——是抓不到。检查浏览器是否开着且已登录）")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"ERROR {type(e).__name__}: {e}")
        return 1

    new = result.get("new_replies", 0)
    pending = result.get("pending", 0)
    summary = result.get("summary") or {}
    rows = result.get("rows") or []

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    if not new:
        print(f"OK {ts} 无新消息 · 待处理 {pending}")
        return 0

    # 有新回复：把最重要的几类拎出来
    important = [r for r in rows if r.get("category") in ("interview", "rejected")
                 and not r.get("from_me")]
    print(f"ALERT {ts} 新消息 {new} 条 · 待处理 {pending}"
          f"（面试 {summary.get('interview', 0)} / 拒信 {summary.get('rejected', 0)}）")
    for r in important[:5]:
        tag = "🔥 面试/邀约" if r["category"] == "interview" else "❌ 拒信"
        print(f"  {tag}  {r.get('hr_name', '')} · {r.get('company', '')}")
        print(f"    {(r.get('last_msg') or '')[:80]}")
    if any(r.get("category") == "interview" for r in important):
        print("  → 面试邀约优先处理：python jobkit.py chat")
    return 2


if __name__ == "__main__":
    sys.exit(main())
