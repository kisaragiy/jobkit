#!/usr/bin/env python3
"""实测：本地小模型能不能干 jobkit 的 LLM 活。

对比对象是 DeepSeek 云端（greet_generator 默认走它）。
跑法：python test_local_llm.py [模型名]
"""
import sys
import time
import json
from pathlib import Path

# 这是「实测脚本」不是单测：顶层就会连本地模型发请求。
# 不加这道门的话，`python -m unittest discover -p "test_*.py"` 会把它当测试收集，
# import 阶段直接打网络 → 整个套件卡死几十秒（2026-10-19 实测 40s+ 超时）。
if __name__ != "__main__":
    import unittest
    raise unittest.SkipTest("实测脚本，用 `python test_local_llm.py` 直接跑，不参与 discover")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

OLLAMA = "http://172.22.175.253:11434/v1"

JD = ("岗位：python｜公司：华遥愿金（深圳·南山区·科技园）｜薪资：8-13K\n"
      "要求：1年以内经验，本科，Docker、MySQL，有量化交易开发经验优先。\n"
      "职责：Python 后端开发，数据接口与策略相关模块。")

models = sys.argv[1:] or ["qwen3.5:9b", "qwen3:1.7b"]

resume_path = Path(r"C:\面试\Resume.md")
resume = resume_path.read_text(encoding="utf-8", errors="replace") if resume_path.exists() else ""

print("=" * 62)
print("本地小模型能力实测 · 招呼语生成")
print("=" * 62)
print(f"  ollama     : {OLLAMA}")
print(f"  简历长度   : {len(resume)} 字")
print(f"  JD 长度    : {len(JD)} 字")
print(f"  待测模型   : {models}")

import greet_generator as gg

for model in models:
    print("\n" + "─" * 62)
    print(f"▶ {model}")
    print("─" * 62)
    t0 = time.time()
    try:
        res, meta = gg.generate(
            JD, resume, company="华遥愿金", title="python", count=1,
            model=model, api_key="ollama", base_url=OLLAMA, temperature=0.7,
        )
        dt = time.time() - t0
        txt = gg.render(res) or ""
        print(f"  ⏱  耗时     : {dt:.0f}s")
        print(f"  📏 输出长度 : {len(txt)} 字")
        if meta:
            u = meta.get("usage") or {}
            print(f"  🔢 token    : {json.dumps(u, ensure_ascii=False)[:120]}")
        if not txt.strip():
            print("  ❌ 输出为空 —— 典型症状：qwen3.5 系列默认思考模式吃掉了预算")
            print("     处置：需要 think:false 或换非思考模型")
            continue
        print("\n  ── 生成结果 ──")
        for line in txt.strip().splitlines():
            print("   ", line[:100])
        # 关键：事实核对（有没有编造简历里没有的东西）
        bad = [w for w in ("硕士", "博士", "5年", "三年", "大厂", "字节", "腾讯", "阿里") if w in txt]
        print(f"\n  🔍 事实风险词: {bad if bad else '未发现 ✅'}")
    except Exception as e:
        print(f"  ❌ 失败 ({time.time()-t0:.0f}s): {type(e).__name__}: {str(e)[:250]}")
