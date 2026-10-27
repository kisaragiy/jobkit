#!/usr/bin/env python3
import unittest as _ut
if __name__ != "__main__":
    # 这个脚本会真的去试投递（预期被门禁拦下）。只能是显式运行，不能被 discover import。
    raise _ut.SkipTest("真机门禁验证脚本，用 python 直接运行，不是 unittest 模块")

"""验证投递通道门禁：headless 时必须拒绝投递。

安全前提：本脚本先确认「真实浏览器通道确实不可用」，再跑非 dry-run 的投递，
预期被门禁拦下（exit 3）且不发送任何招呼。若发现 CDP/Bridge 可用，直接跳过测试。
"""
import subprocess
import sys
import urllib.request as u
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
FAIL = []


def ck(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


print("=== 1. 确认当前没有真实浏览器通道（否则测试不安全）===")
import apply_pipeline as ap  # noqa: E402

ch, why = ap.browser_channel()
ok, ch2, _ = ap.delivery_channel_ok()
print(f"     通道 = {ch} · {why}")
print(f"     放行 = {ok}")
ck("当前处于 headless（测试前提成立）", ch == "headless",
   f"实际 {ch} —— 若为 bridge/cdp 则本测试跳过")

if ch != "headless":
    print("\n⚠️ 真实浏览器通道可用，跳过危险测试（不能拿真账号试误投）。")
    sys.exit(0)

print("\n=== 2. 门禁判定 ===")
ck("headless → 不放行", ok is False)
ck("headless → 返回通道名 headless", ch2 == "headless")

print("\n=== 3. 实跑非 dry-run 投递，预期被拦（不发送）===")
r = subprocess.run(
    [sys.executable, str(HERE / "apply_pipeline.py"), "--limit", "1"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=str(HERE), timeout=180)
out = (r.stdout or "") + (r.stderr or "")
print("     exit code =", r.returncode)
for line in out.splitlines():
    if any(k in line for k in ("浏览器通道", "拒绝投递", "headless", "登录态", "招呼语", "本轮结束")):
        print("       |", line[:96])

ck("被门禁拦下（exit=3）", r.returncode == 3, f"实际 {r.returncode}")
ck("打印了拒绝原因", "已拒绝投递" in out)
ck("给出了 Bridge 修复步骤", "bridge.daemon" in out and "extension" in out)
ck("没有走到发送（无「招呼语」生成）", "招呼语" not in out)
ck("没有走到流程结束（无「本轮结束」）", "本轮结束" not in out)

print("\n=== 4. dry-run 仍应放行（演练不受限）===")
r2 = subprocess.run(
    [sys.executable, str(HERE / "apply_pipeline.py"), "--dry-run", "--limit", "1"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=str(HERE), timeout=180)
out2 = (r2.stdout or "") + (r2.stderr or "")
ck("dry-run 正常执行", "本轮结束" in out2 or r2.returncode == 0, f"exit={r2.returncode}")
ck("dry-run 不发送", "发送 0" in out2 or "试运行" in out2)

print("\n" + "=" * 54)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 门禁有效：headless 下拒绝真实投递，dry-run 不受影响")
