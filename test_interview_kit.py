"""面试题库的解析层单测（不调模型，只测纯逻辑）。"""
from __future__ import annotations

import unittest

import interview_kit as ik

SAMPLE = """【技术】
1. 本地部署大模型遇到 OOM 你怎么优化？
2. RAG 检索不准怎么改进。
3. LangChain 工具调用死循环怎么办？

【项目】
4. 讲讲你的灵枢项目
5. 你的 AIGC 管线怎么做质检？

【行为】
6. 业务部门提了一个不靠谱的 AI 需求你怎么办？
"""


class TestParse(unittest.TestCase):
    def test_groups_split(self):
        g = ik._parse(SAMPLE)
        self.assertEqual(len(g["技术"]), 3)
        self.assertEqual(len(g["项目"]), 2)
        self.assertEqual(len(g["行为"]), 1)

    def test_question_marks_normalized(self):
        g = ik._parse(SAMPLE)
        for cat in ("技术", "项目", "行为"):
            for q in g[cat]:
                self.assertTrue(q.endswith("？"), f"应以？结尾：{q}")
                self.assertNotIn("。？", q, f"不该出现双标点：{q}")
                self.assertNotIn("?.？", q)

    def test_empty_input(self):
        g = ik._parse("")
        self.assertEqual(sum(len(v) for v in g.values()), 0)

    def test_no_section_means_no_items(self):
        """没有分段标记时不该硬塞 —— 宁可空，也不要归类错。"""
        g = ik._parse("1. 这是一个没有分段的题目\n2. 第二个")
        self.assertEqual(sum(len(v) for v in g.values()), 0)


class TestGuide(unittest.TestCase):
    def test_every_category_has_guide(self):
        for cat in ("技术", "项目", "行为"):
            self.assertGreaterEqual(len(ik.guide_for(cat)), 2, cat)

    def test_render_warns_about_fabrication(self):
        """渲染结果必须带「项目题是推测的」提醒，否则用户会照编。"""
        text = ik.render(ik._parse(SAMPLE))
        self.assertIn("推测", text)
        self.assertIn("别照着编", text)

    def test_render_counts(self):
        text = ik.render(ik._parse(SAMPLE))
        self.assertIn("共 6 题", text)


if __name__ == "__main__":
    unittest.main()
