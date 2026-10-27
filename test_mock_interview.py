"""答题卡：题目切分要准（BOSS 文本有隐形字符）、题型认得对、不编内容。"""
from __future__ import annotations

import unittest

import mock_interview as MI

REAL = ("1. 展示一条你制作的代表性AIGC视频，完整讲出整条项目执行过程。​"
        "2. AIGC视频出现产品变形、人物形象不一致、动作僵硬不自然，你如何排查与解决？"
        "3. 你独立搭建过哪些ComfyUI画布式工作流？请讲清流程逻辑")


class SplitTest(unittest.TestCase):
    def test_real_message_with_zero_width_chars(self):
        """真库原文里「过程。」和「2.」之间夹了 U+200B，不清掉就切不开。"""
        qs = MI.split_questions(REAL)
        self.assertEqual(len(qs), 3, f"应切成 3 问，实际 {qs}")
        self.assertTrue(qs[0].startswith("展示一条"))
        self.assertTrue(qs[1].startswith("AIGC视频出现产品变形"))
        self.assertTrue(qs[2].startswith("你独立搭建过"))

    def test_question_mark_separator(self):
        qs = MI.split_questions("薪资这块你怎么看？3. 你独立搭建过哪些画布式工作流？")
        self.assertEqual(len(qs), 2, f"实际 {qs}")
        self.assertEqual(qs[0], "薪资这块你怎么看")
        self.assertEqual(qs[1], "你独立搭建过哪些画布式工作流")

    def test_decimal_number_not_split(self):
        """「8.5K」里的 8. 不是题号。"""
        qs = MI.split_questions("期望薪资 8.5K 可以吗")
        self.assertEqual(len(qs), 1)

    def test_no_numbering_is_one_question(self):
        qs = MI.split_questions("你目前在深圳吗 可以现场面试吗")
        self.assertEqual(len(qs), 1)

    def test_empty(self):
        self.assertEqual(MI.split_questions(""), [])


class ClassifyTest(unittest.TestCase):
    def test_aigc(self):
        self.assertEqual(MI.classify("你搭建过哪些ComfyUI工作流"), "aigc")

    def test_backend(self):
        self.assertEqual(MI.classify("Redis 缓存怎么防止击穿，数据库索引怎么建"), "backend")

    def test_behavior(self):
        self.assertEqual(MI.classify("你为什么离职，职业规划是什么"), "behavior")

    def test_project(self):
        self.assertEqual(MI.classify("介绍一下你做过的项目，技术难点在哪"), "project")

    def test_unknown(self):
        self.assertEqual(MI.classify("今天天气不错"), "unknown")


class CardTest(unittest.TestCase):
    def test_card_has_structure(self):
        c = MI.card("AIGC 视频出现产品变形怎么排查")
        self.assertEqual(c["kind"], "aigc")
        self.assertTrue(c["checks"])
        self.assertTrue(c["frame"])
        self.assertTrue(c["followups"])
        self.assertTrue(c["redline"])

    def test_card_does_not_invent_facts(self):
        """答题卡只能给结构和字段提示，不能替用户写项目内容。"""
        c = MI.card("介绍一下你的项目")
        blob = " ".join(c["frame"] + c["checks"] + c["followups"])
        self.assertNotIn("我做过", blob)
        self.assertNotIn("我们团队", blob)
        # 「结果 + 数字（QPS / 延迟…）」是让他填什么，不是替他编数字
        self.assertIn("数字", blob)

    def test_unknown_kind_still_gives_advice(self):
        c = MI.card("随便聊聊")
        self.assertEqual(c["kind"], "unknown")
        self.assertTrue(c["frame"], "认不出的题也要给「先确认理解」的建议")


class PrepareTest(unittest.TestCase):
    def test_need_from_you_mentions_missing_material(self):
        pack = MI.prepare(REAL)
        self.assertTrue(pack["need_from_you"])
        joined = " ".join(pack["need_from_you"])
        self.assertIn("数字", joined)

    def test_need_from_you_deduped(self):
        pack = MI.prepare(REAL)   # 3 道 AIGC 题，同类素材只该列一次
        self.assertEqual(len(pack["need_from_you"]), len(set(pack["need_from_you"])))

    def test_render_contains_each_question(self):
        pack = MI.prepare(REAL)
        out = MI.render(pack)
        for q in pack["questions"]:
            self.assertIn(q[:20], out)


if __name__ == "__main__":
    unittest.main()
