"""骗局检测的召回测试：用真实案例的话术，看规则漏不漏。

为什么需要这个文件
------------------
真库 142 条 JD 扫出 high 0 / medium 0。这有两种解释，无法区分：
  (a) 库里确实干净
  (b) 规则太松，什么都抓不到
光看扫描结果分不出是哪一种 —— 必须拿**已知是骗局**的话术喂进去看能不能抓到。

样本来源：小红书真实经历《在BOSS直聘找了三个月工作，我被骗了6800》
（废柴不废，2026-07-14，见 docs/jobkit-improve-directions-20261021.md T13）。
六种套路的话术与金额都是原文记录，不是我编的。

兜底原则
--------
六种套路的共同点是「入职前让你掏钱」。所以哪怕某种具体话术没被单独覆盖，
只要「要钱」这个动作被抓到就必须 high —— 这是最后一道网。
"""
from __future__ import annotations

import unittest

import tools

# (套路名, 真实话术片段, 期望最低级别)
CASES = [
    ("招转培",
     "入职前需参加岗前培训，培训费1980元，考核通过即可入职，未通过不予退还", "high"),
    ("招转培·变体",
     "零基础可做，公司先安排三个月岗前培训，学费从工资里扣", "high"),
    ("面试变刷单",
     "先充值100元返130元建立信任，任务完成后本金和佣金一起结算", "high"),
    ("套路运",
     "月薪8000无押金，到岗后签署合作协议，需先缴纳租车费9800元", "high"),
    ("付费内推",
     "8000元包进字节跳动，内推服务费入职一个月后退还", "high"),
    ("体检费",
     "面试通过后需到指定医院体检，费用285元自理，体检合格即可上岗", "high"),
    ("传销式",
     "发展下线3人即可转正，拉人头有额外提成，团队裂变收益上不封顶", "high"),
    ("账号冻结",
     "该招聘账号已冻结，请勿投递", "high"),
]

# 正常 JD 不能误报（误报比漏判更伤：报错了用户直接关掉功能）
CLEAN = [
    "负责公司 AI 应用后端开发，要求熟悉 Python / FastAPI，有 LLM 应用经验优先",
    "参与大模型 Agent 平台建设，熟悉 LangGraph、RAG 检索增强，五险一金，双休",
    "月薪 15-25K，六险一金，弹性工作，提供年度体检和培训机会",
]


class RecallTest(unittest.TestCase):
    def test_six_scam_patterns_are_caught(self):
        """六种套路全部要抓到，漏一种就说明规则有洞。"""
        missed = []
        for name, text, want in CASES:
            r = tools.check_company("某公司", "", text)
            if tools._RISK_ORDER[r["level"]] < tools._RISK_ORDER[want]:
                missed.append(f"{name}: 判成 {r['level']}，原文「{text[:24]}…」")
        self.assertEqual(missed, [], "漏掉的话术：\n" + "\n".join(missed))

    def test_ask_for_money_always_high(self):
        """兜底网：只要话里在要钱，不管什么名目都得 high。"""
        for text in ("入职需缴纳押金", "需先交服务费", "请先支付工装费用"):
            r = tools.check_company("某公司", "", text)
            self.assertEqual(r["level"], "high", text)

    def test_normal_jd_not_flagged(self):
        """正常 JD 不能报 high。"""
        for text in CLEAN:
            r = tools.check_company("某科技公司", "", text)
            self.assertNotEqual(r["level"], "high", f"误报：{text}")


if __name__ == "__main__":
    unittest.main()
