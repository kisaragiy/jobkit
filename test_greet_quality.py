"""招呼语质检的回归：真实被误杀的正文，不能再被误杀。

背景（2026-10-21 真机日志 apply_log.txt）
----------------------------------------
一轮 14 条投递里 **5 条**被「招呼语质检不通过」整条拒掉，退回没有任何技术证据的
回退模板。其中 4 条的拒绝理由是「没有人称」，而被拒的正文是：

    用 LangGraph 做过 Supervisor-Worker 多 Agent 编排框架，含自动 Replan 和
    fail-closed 熔断，368 条 pytest 实测通过…

这是中文口语省略主语的正常写法 ——「做过」就是「我做过」。
只认「我」这个字，等于把最自然的一类好话术全判死。

另一条是命中「贵司」。这种能安全替换（贵司→公司）的不该整条丢弃。
"""
from __future__ import annotations

import unittest

from greet_generator import soft_fix, validate_greeting

# 真实日志里被拒的正文（截断到日志显示长度，语义完整）
REAL_REJECTED = [
    "用 LangGraph 做过完整多 Agent 编排框架：Supervisor 拆任务、Worker 并行、"
    "验收不过自动 Replan，368 条 pytest 实测通过。",
    "做过 LangGraph 多 Agent 编排框架灵枢：Supervisor 拆任务、Worker 并行、"
    "验收不通过自动重做，带 SSE 流式输出。",
    "用 Python + LangGraph 做过一套多 Agent 编排框架：Supervisor 拆任务、"
    "Worker 并行，含 fail-closed 熔断和自动 Replan。",
]


class OmittedSubjectTest(unittest.TestCase):
    def test_real_rejected_texts_now_pass(self):
        """省略主语的自述句必须通过。"""
        for t in REAL_REJECTED:
            ok, why = validate_greeting(t)
            self.assertTrue(ok, f"仍被误杀：{why}｜{t[:30]}")

    def test_text_without_any_self_reference_still_rejected(self):
        """真的没有人称的文本仍要拒 —— 放宽不等于放行一切。"""
        t = "这个岗位非常适合您，欢迎来公司面谈，工作时间灵活，待遇从优，随时联系。"
        ok, why = validate_greeting(t)
        self.assertFalse(ok)
        self.assertIn("没有人称", why)


class SoftFixTest(unittest.TestCase):
    def test_guisi_replaced(self):
        self.assertEqual(soft_fix("看到贵司的需求"), "看到公司的需求")
        self.assertEqual(soft_fix("贵公司的岗位"), "公司的岗位")

    def test_guisi_text_passes_after_fix(self):
        """「贵司」能改就不该整条丢 —— 丢一条等于浪费一次投递额度。"""
        raw = ("您好，我在 AI应用开发 这个岗位上看到贵司的需求，"
               "我用 FastAPI + LangGraph 做过多 Agent 编排，368 条 pytest 通过，方便发份简历吗？")
        self.assertFalse(validate_greeting(raw)[0])        # 原文被禁词拦
        fixed = soft_fix(raw)
        self.assertTrue(validate_greeting(fixed)[0])       # 改完就通过
        self.assertNotIn("贵司", fixed)

    def test_other_banned_still_rejected(self):
        """改不了的（命令语气/谈入职）仍然要拒。"""
        for bad in ("赶紧发简历给我，能否入职？我用 FastAPI 做过项目，368 条测试通过。",
                    "我用 Python 做过项目，请问能否入职，我做过多 Agent 编排框架。"):
            self.assertFalse(validate_greeting(bad)[0], bad)


if __name__ == "__main__":
    unittest.main()
