"""空窗期套件的单测 —— 重点是「场景必须完整」和「雷区能被抓住」。"""
from __future__ import annotations

import unittest

import gap_kit as g


class TestScenarios(unittest.TestCase):
    REQUIRED = ("label", "one_liner", "expanded", "followup", "pitfalls")

    def test_every_scenario_complete(self):
        for key in g.all_scenarios():
            s = g.get(key)
            for f in self.REQUIRED:
                self.assertTrue(s.get(f), f"{key} 缺字段 {f}")

    def test_pitfalls_not_empty(self):
        """每个场景都要给「别说什么」——这比「该说什么」更容易踩坑。"""
        for key in g.all_scenarios():
            self.assertGreaterEqual(len(g.get(key)["pitfalls"]), 1, key)

    def test_unknown_raises(self):
        with self.assertRaises(KeyError):
            g.get("no_such_scenario")


class TestAnswerCheck(unittest.TestCase):
    def test_catches_red_flags(self):
        hits = g.check_answer("我毕业后一直找不到工作，在家躺了一段时间")
        joined = " ".join(hits)
        self.assertIn("找不到工作", joined)
        self.assertIn("躺", joined)

    def test_catches_apology(self):
        self.assertTrue(g.check_answer("这段时间比较惭愧，不好意思一直在学习"))

    def test_catches_too_short(self):
        self.assertTrue(g.check_answer("做项目"))

    def test_good_answer_passes(self):
        text = ("毕业后我没有马上投简历，而是独立做了三个项目：灵枢、AIGC 管线、jobkit，"
                "代码和文档都在 GitHub，现在作品成型了开始找工作。")
        self.assertEqual(g.check_answer(text), [])


class TestResumeAdvice(unittest.TestCase):
    def test_advice_must_write(self):
        self.assertTrue(g.RESUME_ADVICE["should_write"])

    def test_example_has_link(self):
        """示例里必须有可验证的链接，否则等于空话。"""
        self.assertIn("GitHub", g.RESUME_ADVICE["example"])


if __name__ == "__main__":
    unittest.main()
