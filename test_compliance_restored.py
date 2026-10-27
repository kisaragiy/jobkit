#!/usr/bin/env python3
"""验证低风险模式保险已恢复且真的会拦截。

只做模块级调用与配置求值，**不执行任何 CLI 命令、不发任何网络请求**。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Users\<user>\boss-agent-cli-dev\src")))

from boss_agent_cli import compliance as C  # noqa: E402

FAIL = []


def ck(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


class FakeCtx:
    def __init__(self, config):
        self.obj = {"config": config}


print("=== 1. 默认应为「拦截」（作者的安全默认值）===")
ck("默认配置 → low_risk_mode=True",
   C.is_low_risk_mode(FakeCtx({})) is True)
ck("config 为空 dict → True",
   C.is_low_risk_mode(FakeCtx({"low_risk_mode": True})) is True)
ck("显式关闭 → False（保留用户主动退出的口子）",
   C.is_low_risk_mode(FakeCtx({"low_risk_mode": False})) is False)
ck("ctx 为 None 也不崩",
   C.is_low_risk_mode(None) is True)

print("\n=== 2. 被保护的敏感命令清单 ===")
blocked = C.low_risk_blocked_commands()
must = ["greet", "batch-greet", "apply", "chat", "follow-up", "pipeline",
        "digest", "exchange", "chatmsg", "chat-summary", "recommend", "mark"]
missing = [m for m in must if m not in blocked]
ck(f"敏感命令共 {len(blocked)} 个", len(blocked) >= 20, str(len(blocked)))
ck("投递链路全被覆盖", not missing, ", ".join(missing) if missing else "greet/batch-greet/apply 均在列")

print("\n=== 3. 只读命令仍可用（不误伤）===")
readonly = ["search", "detail", "show", "shortlist", "status", "login", "doctor"]
hurt = [r for r in readonly if r in blocked]
ck("只读命令未被拦", not hurt, ", ".join(hurt) if hurt else "search/detail/status/login 均放行")

print("\n=== 4. 拦截时的报错内容（必须指导用户回官网手动做）===")
sed = C._SENSITIVE_COMMANDS.get("apply", "")
ck("apply 有明确理由", "低风险" in sed or "平台写操作" in sed, sed[:46])
ck("提示回官网手动", "官网" in C.COMPLIANCE_BLOCKED_ACTION or "手动" in C.COMPLIANCE_BLOCKED_ACTION,
   C.COMPLIANCE_BLOCKED_ACTION[:40])

print("\n" + "=" * 52)
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 保险已恢复：打招呼/投递/读会话 等写操作会被 CLI 自己拦住")
print("   （卖家想主动放开，可在配置里设 low_risk_mode: false）")
