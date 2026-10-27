#!/usr/bin/env python3
"""wrapper：跑一轮大规模收集。
修正点：实测 TOKEN_REFRESH_FAILED 是间歇性网络抖动（同关键词时好时坏），
把它从 RISK 移到 TRANSIENT，并提高抖动容忍次数。其余逻辑复用 collector。
"""
import argparse
import sys
from pathlib import Path
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import collect_window  # noqa: E402
import collector  # noqa: E402

# 把 TOKEN_REFRESH_FAILED 从风控移到抖动
collector.RISK_SIGNALS = tuple(s for s in collector.RISK_SIGNALS if s != "TOKEN_REFRESH_FAILED")
collector.TRANSIENT_SIGNALS = tuple(collector.TRANSIENT_SIGNALS) + ("TOKEN_REFRESH_FAILED",)
collector.MAX_TRANSIENT_FAILS = 10  # 间歇抖动频繁，提高容忍

kws = ["AI应用开发","大模型应用开发","AI Agent开发","智能体开发","LLM应用开发",
       "RAG开发","Python后端 AI","大模型落地","AIGC应用开发","LangChain",
       "AI后端开发","大模型工程化"]
cts = ["广州","深圳","东莞","佛山","惠州","珠海"]

def main() -> int:
    ap = argparse.ArgumentParser(description="大规模采集岗位（默认只在夜间窗口跑）")
    ap.add_argument("--force", action="store_true", help="忽略时间窗口，立即采集")
    ap.add_argument("--start", default=collect_window.DEFAULT_START,
                    help=f"窗口开始时刻，默认 {collect_window.DEFAULT_START}")
    ap.add_argument("--end", default=collect_window.DEFAULT_END,
                    help=f"窗口结束时刻，默认 {collect_window.DEFAULT_END}")
    ap.add_argument("--max-searches", type=int, default=60)
    a = ap.parse_args()

    ok, reason = collect_window.check_or_reason(start=a.start, end=a.end)
    if not ok and not a.force:
        print(reason)
        return 0                     # 窗口外跳过属正常，不是错误
    if not ok:
        print(f"⚠ 已用 --force 跳过窗口限制（{reason}）")

    result = collector.collect(keywords=kws, cities=cts, max_searches=a.max_searches)
    print("\n=== 最终结果 ===")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
