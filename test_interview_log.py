"""面试复盘：记录、归一化、高频题、反复踩坑。"""
import tempfile
import unittest
from pathlib import Path

import interview_log as IL
import store


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = store.DB_PATH
        store.DB_PATH = Path(self.tmp) / "t.db"
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old

    def seed(self, company="A公司", questions=None):
        iid = IL.add_interview(self.c, company)
        for q, score in (questions or []):
            IL.add_question(self.c, iid, q, self_score=score)
        return iid


class TestQKey(unittest.TestCase):
    def test_politeness_stripped(self):
        """「请介绍一下你的项目」和「介绍一下你的项目吧」是同一题。"""
        a = IL._qkey("请介绍一下你的项目")
        b = IL._qkey("介绍一下你的项目吧")
        self.assertEqual(a, b)

    def test_different_questions_differ(self):
        self.assertNotEqual(IL._qkey("你为什么离职"),
                            IL._qkey("你的期望薪资"))

    def test_empty(self):
        self.assertEqual(IL._qkey("？"), "")


class TestWrite(_DB):
    def test_add_interview(self):
        iid = IL.add_interview(self.c, "某公司", "Python后端", "初面")
        self.assertGreater(iid, 0)

    def test_empty_company_rejected(self):
        with self.assertRaises(ValueError):
            IL.add_interview(self.c, "   ")

    def test_add_question(self):
        iid = IL.add_interview(self.c, "某公司")
        qid = IL.add_question(self.c, iid, "为什么离职", "想换方向", 3, "别说得太负面")
        self.assertGreater(qid, 0)

    def test_empty_question_rejected(self):
        iid = IL.add_interview(self.c, "某公司")
        with self.assertRaises(ValueError):
            IL.add_question(self.c, iid, "  ")

    def test_score_out_of_range(self):
        iid = IL.add_interview(self.c, "某公司")
        with self.assertRaises(ValueError):
            IL.add_question(self.c, iid, "问题", self_score=6)
        with self.assertRaises(ValueError):
            IL.add_question(self.c, iid, "问题", self_score=-1)

    def test_nonexistent_interview(self):
        with self.assertRaises(ValueError):
            IL.add_question(self.c, 9999, "问题")

    def test_set_result(self):
        iid = IL.add_interview(self.c, "某公司")
        IL.set_result(self.c, iid, "pass")
        self.assertEqual(IL.get_interview(self.c, iid)["result"], "pass")

    def test_bad_result(self):
        iid = IL.add_interview(self.c, "某公司")
        with self.assertRaises(ValueError):
            IL.set_result(self.c, iid, "maybe")


class TestRead(_DB):
    def test_get_with_questions(self):
        iid = self.seed(questions=[("Q1", 3), ("Q2", 4)])
        iv = IL.get_interview(self.c, iid)
        self.assertEqual(len(iv["questions"]), 2)
        self.assertEqual(iv["questions"][0]["question"], "Q1")

    def test_missing_interview(self):
        self.assertIsNone(IL.get_interview(self.c, 12345))

    def test_list_counts(self):
        self.seed(questions=[("Q1", 3), ("Q2", 4)])
        rows = IL.list_interviews(self.c)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question_count"], 2)


class TestReview(_DB):
    def test_empty(self):
        r = IL.review(self.c)
        self.assertEqual(r["total_questions"], 0)
        self.assertTrue(r["warnings"])

    def test_frequent_question(self):
        """同一题在两家公司都问了 → 高频。"""
        self.seed("A公司", [("介绍一下你的项目", 3)])
        self.seed("B公司", [("介绍一下你的项目吧", 4)])
        r = IL.review(self.c)
        self.assertEqual(len(r["frequent"]), 1)
        self.assertEqual(r["frequent"][0]["asked"], 2)
        self.assertEqual(len(r["frequent"][0]["companies"]), 2)

    def test_stuck_question(self):
        """问了两次、两次都 ≤2 分 → 反复踩坑。"""
        self.seed("A公司", [("空窗期怎么解释", 1)])
        self.seed("B公司", [("空窗期怎么解释", 2)])
        r = IL.review(self.c)
        self.assertEqual(len(r["stuck"]), 1)
        self.assertEqual(r["stuck"][0]["asked"], 2)

    def test_improved_question_not_stuck(self):
        """第一次低分、第二次高分 → 不算踩坑（你解决了）。"""
        self.seed("A公司", [("空窗期怎么解释", 1)])
        self.seed("B公司", [("空窗期怎么解释", 5)])
        r = IL.review(self.c)
        self.assertEqual(r["stuck"], [])
        self.assertEqual(len(r["frequent"]), 1)

    def test_avg_score(self):
        self.seed("A公司", [("Q1", 4), ("Q2", 2)])
        r = IL.review(self.c)
        self.assertEqual(r["avg_score"], 3.0)

    def test_few_interviews_warns(self):
        self.seed("A公司", [("Q1", 3)])
        r = IL.review(self.c)
        self.assertTrue(any("样本太少" in w for w in r["warnings"]))

    def test_unscored_excluded_from_avg(self):
        self.seed("A公司", [("Q1", 0), ("Q2", 4)])
        r = IL.review(self.c)
        self.assertEqual(r["avg_score"], 4.0)


class TestRender(_DB):
    def test_list_empty(self):
        self.assertIn("还没有面试记录", IL.render_list([]))

    def test_list_shows(self):
        self.seed("某公司", [("Q1", 3)])
        out = IL.render_list(IL.list_interviews(self.c))
        self.assertIn("某公司", out)

    def test_one_shows_answer_and_better(self):
        iid = IL.add_interview(self.c, "某公司")
        IL.add_question(self.c, iid, "为什么离职", "想换方向", 3, "别抱怨前公司")
        out = IL.render_one(IL.get_interview(self.c, iid))
        self.assertIn("我当时答", out)
        self.assertIn("下次该答", out)

    def test_review_disclaims_scoring(self):
        self.seed("A公司", [("Q1", 3)])
        out = IL.render_review(IL.review(self.c))
        self.assertIn("不是能力评价", out)


if __name__ == "__main__":
    unittest.main()
