#!/usr/bin/env python3
"""验证 low_risk_mode=false 已通过配置文件生效（正规路径，非改代码）。

只做配置加载与模块求值，不发任何网络请求。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Users\<user>\boss-agent-cli-dev\src")))

from boss_agent_cli import compliance as C  # noqa: E402
from boss_agent_cli.config import load_config  # noqa: E402

CFG_PATH = Path(r"C:\Users\<user>\.boss-agent\config.json")
FAIL = []


def ck(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


print("=== 1. 配置文件里确实是 false（显式配置，不是改代码）===")
raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
ck("config.json low_risk_mode = false", raw.get("low_risk_mode") is False,
   str(raw.get("low_risk_mode")))
ck("留有说明备注", bool(raw.get("_low_risk_mode_note")))

print("\n=== 2. CLI 的配置加载器读到了 ===")
cfg = load_config(CFG_PATH)
ck("load_config → low_risk_mode=False", cfg.get("low_risk_mode") is False,
   str(cfg.get("low_risk_mode")))
ck("其他默认值未被破坏", cfg.get("platform") == "zhipin" and cfg.get("role") == "candidate",
   f"platform={cfg.get('platform')} role={cfg.get('role')}")
ck("请求延迟默认值仍在", cfg.get("request_delay") == [1.5, 3.0], str(cfg.get("request_delay")))


class FakeCtx:
    def __init__(self, config):
        self.obj = {"config": config}


print("\n=== 3. 保险状态 ===")
ck("低风险模式 → 已关闭（写操作放行）",
   C.is_low_risk_mode(FakeCtx(cfg)) is False)
print("     注意：关掉的是「命令拦截」，不是「风控防护」。")
print(f"     受影响的命令仍会被记录（{len(C.low_risk_blocked_commands())} 个敏感命令清单保留）")

print("\n=== 4. 回退路径仍然可用（改回 true 即恢复拦截）===")
ck("把 low_risk_mode 设回 True → 重新拦截",
   C.is_low_risk_mode(FakeCtx({**cfg, "low_risk_mode": True})) is True)

print("\n=== 5. 源码未被改动（确认走的不是改代码那条路）===")
src = Path(r"C:\Users\<user>\boss-agent-cli-dev\src\boss_agent_cli\compliance.py").read_text(encoding="utf-8")
ck("仍保留作者原实现", "config.get(\"low_risk_mode\", True)" in src)
ck("没有 Bypass 字样", "Bypass" not in src)

print("\n" + "=" * 54)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 配置生效：low_risk_mode=false 通过配置文件设置，作者源码保持原样")
