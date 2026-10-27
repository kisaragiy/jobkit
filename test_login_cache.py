#!/usr/bin/env python3
"""验证「登录态缓存」修复：UI 刷新不再打平台接口。

做法：把 boss 调用打桩计数，然后连续调用接口，
确认真实平台调用只发生 1 次。全程零网络请求。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import app as A  # noqa: E402

CALLS: list = []
A.ap.run_boss = lambda argv, timeout=60: (CALLS.append(list(argv)) or (True, "{}"))
A.ap.login_ready = lambda: (True, "stub")

FAIL = []


def ck(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


print("=== 登录态缓存隔离测试（零平台请求）===")

# 1) 首次：必须真实调用平台（此时应发生 1 次）
r1 = A.login_status()
ck("首次检查 → 真实调用平台", not r1["data"].get("cached"), f"平台调用 {len(CALLS)} 次")

# 2) 紧接着第二次：应命中缓存，不再调用
n_before = len(CALLS)
r2 = A.login_status()
ck("第二次 → 命中缓存", r2["data"].get("cached") is True,
   f"新增平台调用 {len(CALLS) - n_before} 次（应为 0）")
ck("第二次确实没打平台", len(CALLS) == n_before)

# 3) 模拟多次页面刷新（原来每 2 分钟轮询 + 每次加载都调）
n_before = len(CALLS)
for _ in range(30):
    A.login_status()
ck("30 次页面刷新 → 0 次平台调用", len(CALLS) == n_before,
   f"累计平台调用 {len(CALLS)} 次")

# 4) live=true 也限流（60 秒内不重复）
n_before = len(CALLS)
r4 = A.login_status(live=True)
ck("live 检查 60s 内被限流", len(CALLS) == n_before and "note" in r4["data"],
   r4["data"].get("note", ""))

# 5) 缓存过期后应重新调用
A._login_cache["t"] = 0.0
n_before = len(CALLS)
A.login_status()
ck("缓存过期后恢复真实调用", len(CALLS) == n_before + 1,
   f"新增 {len(CALLS) - n_before} 次")

print("\n" + "=" * 46)
print(f"平台调用总次数：{len(CALLS)}")
if FAIL:
    print("❌ 失败：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 修复有效：无论页面刷新多少次，平台调用都被压到 1 次/10 分钟")
