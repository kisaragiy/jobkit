"""招呼语评分：规则必须能区分「群发空话」和「有干货」。"""
import unittest

import greeting_kit as G

BAD = "你好，还招人吗？"
GOOD = ("您好，看到岗位要 LangGraph 和 RAG。我做过灵枢多 Agent 编排，"
        "Supervisor 拆任务、Worker 并行，验收不过自动重试，线上跑了 3 个月。"
        "方便聊 5 分钟吗？")


class TestScore(unittest.TestCase):
    def test_empty_scores_zero(self):
        self.assertEqual(G.check("")["score"], 0)
        self.assertEqual(G.check("   ")["score"], 0)
        self.assertEqual(G.check("")["grade"], "F")

    def test_bad_opening_heavily_penalized(self):
        r = G.check(BAD)
        self.assertLess(r["score"], 40)
        self.assertTrue(any("还招人吗" in i for i in r["issues"]))

    def test_good_scores_high(self):
        r = G.check(GOOD)
        self.assertGreaterEqual(r["score"], 85)
        self.assertEqual(r["grade"], "A")
        self.assertEqual(r["issues"], [])

    def test_good_beats_bad_by_wide_margin(self):
        self.assertGreater(G.check(GOOD)["score"] - G.check(BAD)["score"], 50)

    def test_no_tech_penalized(self):
        r = G.check("您好，我很努力，学习能力强，希望有机会加入贵司，期待您的回复。")
        self.assertTrue(any("技术栈" in i for i in r["issues"]))

    def test_too_short_penalized(self):
        r = G.check("您好")
        self.assertTrue(any("太短" in i for i in r["issues"]))

    def test_too_long_penalized(self):
        r = G.check("您好，" + "我做过 Python 项目，上线部署，QPS 很高。" * 20)
        self.assertTrue(any("太长" in i or "太长" in s for i in r["issues"]))

    def test_toolong_issue_word(self):
        r = G.check("您好，" + "Python 项目上线部署 QPS。" * 30)
        self.assertGreater(r["length"], G.MAX_LEN)
        self.assertTrue(any("太长" in i for i in r["issues"]))

    def test_evidence_detected(self):
        r = G.check(GOOD)
        self.assertTrue(any("成果" in s for s in r["strengths"]))

    def test_ask_at_end_detected(self):
        r = G.check(GOOD)
        self.assertTrue(any("回复的抓手" in s for s in r["strengths"]))

    def test_no_call_to_action_penalized(self):
        r = G.check("您好，我做过 LangGraph 多 Agent 项目，上线部署，跑了 3 个月。")
        self.assertTrue(any("结尾" in i for i in r["issues"]))

    def test_score_bounded(self):
        for t in ["", "你好", BAD, GOOD, "您好，" * 200]:
            s = G.check(t)["score"]
            self.assertGreaterEqual(s, 0)
            self.assertLessEqual(s, 100)


class TestJDMatch(unittest.TestCase):
    def test_jd_overlap_credited(self):
        r = G.check(GOOD, jd="需要 LangGraph、RAG 相关经验")
        self.assertTrue(any("命中 JD" in s for s in r["strengths"]))

    def test_jd_mismatch_penalized(self):
        r = G.check(GOOD, jd="需要 Unity 游戏开发、C# 三年经验")
        self.assertTrue(any("JD 里一个都没有" in i for i in r["issues"]))

    def test_no_jd_no_penalty(self):
        """没传 JD 时不该因为「匹配」扣分 —— 那不是用户的错。"""
        a = G.check(GOOD)["score"]
        b = G.check(GOOD, jd="")["score"]
        self.assertEqual(a, b)


class TestRender(unittest.TestCase):
    def test_render_has_score_and_grade(self):
        out = G.render(G.check(BAD))
        self.assertIn("分", out)
        self.assertIn("D", out)

    def test_render_clean_when_no_issues(self):
        out = G.render(G.check(GOOD))
        self.assertIn("没有发现明显问题", out)


if __name__ == "__main__":
    unittest.main()
