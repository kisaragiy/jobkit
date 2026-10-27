"""简历诊断：四块判定要严（成果必须有数字）、项目主旨按标题定、组合提示要触发得起来。"""
from __future__ import annotations

import unittest

import resume_diagnose as RD

FULL = """- 技术架构：FastAPI + LangGraph 分层，Docker 部署
- 项目描述：解决调研报告人工整理慢的问题，给运营用
- 技术描述：Supervisor 拆任务，Worker 并行实现，失败自动 replan
- 项目成果：175 条 pytest 全通过，故障拦截率 100%
"""

NO_RESULT = """- 技术架构：Docker 部署
- 项目描述：解决调研慢的问题
- 技术描述：核心链路用队列实现
"""

FAKE_RESULT = """- 技术架构：Docker 部署
- 项目描述：解决调研慢的问题
- 技术描述：核心链路用队列实现
- 项目成果：性能大幅提升，用户体验显著改善
"""


class SplitTest(unittest.TestCase):
    def test_split_by_h3(self):
        text = "### 项目A\n正文A\n### 项目B\n正文B"
        ps = RD.split_projects(text)
        self.assertEqual([p["title"] for p in ps], ["项目A", "项目B"])
        self.assertIn("正文A", ps[0]["body"])

    def test_no_h3_is_one_project(self):
        ps = RD.split_projects("就是一个项目，没有小标题")
        self.assertEqual(len(ps), 1)

    def test_empty(self):
        self.assertEqual(RD.split_projects(""), [])


class FourBlocksTest(unittest.TestCase):
    def test_complete(self):
        r = RD.four_blocks(FULL)
        self.assertTrue(r["complete"], f"缺了 {r['missing']}")

    def test_missing_result(self):
        r = RD.four_blocks(NO_RESULT)
        self.assertIn("项目成果", r["missing"])

    def test_result_words_without_number_is_not_result(self):
        """「性能大幅提升」等于没说 —— 没有实测数字不算成果。"""
        r = RD.four_blocks(FAKE_RESULT)
        self.assertIn("项目成果", r["missing"])
        self.assertTrue(r["blocks"].get("成果_有词无数字"))


class ClassifyTest(unittest.TestCase):
    def test_title_decides_over_body(self):
        """AIGC 项目正文提「部署/缓存」不该被判成后端项目。"""
        kinds = RD.classify("AIGC 自动化图像生产管线（个人技术项目）",
                            "FastAPI 部署，Redis 缓存，接口限流，数据库读写分离")
        self.assertEqual(kinds, ["aigc"], "正文工程词不能抢标题的主旨")

    def test_agent_title(self):
        self.assertEqual(RD.classify("灵枢 · 多 Agent 编排框架", ""), ["agent"])

    def test_body_used_only_without_title_clue(self):
        kinds = RD.classify("个人项目", "爬虫采集抓取数据清洗 报表分析")
        self.assertIn("data", kinds)

    def test_body_needs_two_hits(self):
        kinds = RD.classify("个人项目", "用了 Flask 接口")
        self.assertNotIn("backend", kinds, "单个词就判后端会把诊断搞成「都没问题」")


class PortfolioTest(unittest.TestCase):
    def test_two_ai_projects_no_backend(self):
        ps = [{"title": "多 Agent 编排框架", "body": ""},
              {"title": "AIGC 图像管线", "body": ""}]
        r = RD.portfolio(ps)
        joined = " ".join(r["tips"])
        self.assertIn("AI 应用方向", joined)
        self.assertIn("后端", joined)

    def test_with_backend_project_no_warning(self):
        ps = [{"title": "多 Agent 编排框架", "body": ""},
              {"title": "AIGC 图像管线", "body": ""},
              {"title": "高并发后端服务", "body": ""}]
        r = RD.portfolio(ps)
        self.assertNotIn("缺一个", " ".join(r["tips"]))

    def test_empty_projects(self):
        r = RD.portfolio([])
        self.assertTrue(r["tips"], "空简历也要给一句结论，不能静默")


class DiagnoseTest(unittest.TestCase):
    def test_end_to_end(self):
        text = ("### 灵枢 · 多 Agent 编排框架\n" + FULL +
                "\n### AIGC 图像管线\n" + FULL)
        res = RD.diagnose(text)
        self.assertEqual(len(res["projects"]), 2)
        self.assertTrue(all(not p["missing_blocks"] for p in res["projects"]))
        out = RD.render(res)
        self.assertIn("组合诊断", out)


if __name__ == "__main__":
    unittest.main()
