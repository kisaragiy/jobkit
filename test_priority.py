"""投递优先级：六个维度的加减分、缺失字段容错、排序。"""
import time
import unittest

import priority as P


def job(**kw):
    base = {"job_id": "j1", "title": "Python后端开发", "company": "某科技有限公司",
            "salary": "8-10K", "city": "广州", "experience": "1-3年",
            "boss_active": "在线", "match_score": 80,
            "job_description": "负责后端服务开发。" * 30,
            "first_seen": time.time() - 86400 * 2}
    base.update(kw)
    return base


def why(s):
    return " · ".join(a["why"] for a in s["adjusts"] if a["why"])


class TestActive(unittest.TestCase):
    def test_just_active_best(self):
        a = P.score(job(boss_active="刚刚活跃"))["total"]
        b = P.score(job(boss_active="在线"))["total"]
        c = P.score(job(boss_active="离线"))["total"]
        self.assertGreater(a, b)
        self.assertGreater(b, c)

    def test_unknown_is_neutral(self):
        s = P.score(job(boss_active=""))
        self.assertIn("HR 活跃度未知", why(s))
        self.assertEqual(sum(a["delta"] for a in s["adjusts"]
                             if "HR" in a["why"]), 0)


class TestJD(unittest.TestCase):
    def test_rich_jd_bonus(self):
        s = P.score(job(job_description="字" * 400))
        self.assertIn("JD 完整", why(s))

    def test_missing_jd_no_penalty(self):
        """515 个岗缺 JD 是采集没抓详情，不是岗位差 —— 不扣分。"""
        rich = P.score(job(job_description="字" * 400))["total"]
        none = P.score(job(job_description=""))["total"]
        # 缺 JD 只少掉那 +5，不能额外扣
        self.assertEqual(rich - none, 5)
        self.assertIn("未抓 JD", why(P.score(job(job_description=""))))

    def test_missing_jd_flag(self):
        s = P.score(job(job_description=""))
        self.assertIn("无 JD", s["flags"])


class TestSalary(unittest.TestCase):
    def test_in_target_gz(self):
        s = P.score(job(salary="8-10K", city="广州"))
        self.assertEqual(P._salary_score("8-10K", "广州")[0], 5)

    def test_too_low(self):
        self.assertEqual(P._salary_score("5-8K", "广州")[0], -10)

    def test_too_high(self):
        self.assertEqual(P._salary_score("13-26K", "广州")[0], -5)

    def test_shenzhen_target_higher(self):
        """深圳下限 8K，广州 7K —— 7-9K 在深圳算偏低。"""
        self.assertEqual(P._salary_score("6-8K", "深圳")[0], -10)
        self.assertEqual(P._salary_score("9-11K", "深圳")[0], 5)

    def test_negotiable(self):
        self.assertEqual(P._salary_score("面议", "广州")[0], 0)

    def test_target_lookup(self):
        self.assertEqual(P._target_for("广州市"), (7, 9))
        self.assertEqual(P._target_for("深圳"), (8, 10))
        self.assertEqual(P._target_for("成都"), P.FALLBACK_TARGET)


class TestExperience(unittest.TestCase):
    def test_fresh_grad_bonus(self):
        self.assertGreater(P.EXP_SCORE["在校/应届"], P.EXP_SCORE["1-3年"])

    def test_senior_penalty(self):
        self.assertLess(P.EXP_SCORE["5-10年"], 0)

    def test_3_5_years_penalty(self):
        self.assertLess(P.EXP_SCORE["3-5年"], 0)


class TestYearTrap(unittest.TestCase):
    def test_2027_bracket(self):
        d, w = P._year_score("【2027年校招】AI应用开发")
        self.assertEqual(d, -15)

    def test_27_short_form(self):
        """「27届校招」没有「20」前缀，正则漏过一版。"""
        d, _ = P._year_score("【27届校招】AI应用开发")
        self.assertEqual(d, -15)

    def test_2025_ok(self):
        self.assertEqual(P._year_score("2025届校园招聘")[0], 0)

    def test_no_year(self):
        self.assertEqual(P._year_score("Python后端开发")[0], 0)


class TestOutsource(unittest.TestCase):
    def test_confirmed_penalty(self):
        d, w = P._outsource_score("中软国际科技服务有限公司")
        self.assertLess(d, 0)
        self.assertIn("外包", w)

    def test_normal_company_neutral(self):
        self.assertEqual(P._outsource_score("某科技有限公司")[0], 0)

    def test_empty_company(self):
        self.assertEqual(P._outsource_score("")[0], 0)


class TestRedlinePenalty(unittest.TestCase):
    def test_salary_mismatch_penalized(self):
        d, w = P._redline_score({"title": "AI开发工程师",
                                 "salary": "3-4K", "company": "X",
                                 "job_description": ""})
        self.assertEqual(d, -25)

    def test_normal_job_neutral(self):
        self.assertEqual(P._redline_score(job())[0], 0)


class TestScoreRobustness(unittest.TestCase):
    def test_empty_job(self):
        """全空 dict 也不能崩。"""
        s = P.score({})
        self.assertEqual(s["base"], 0)
        self.assertIsInstance(s["total"], float)

    def test_missing_fields(self):
        s = P.score({"title": "后端"})
        self.assertIsInstance(s["total"], float)

    def test_total_is_base_plus_adjusts(self):
        s = P.score(job())
        self.assertAlmostEqual(s["total"],
                               s["base"] + sum(a["delta"] for a in s["adjusts"]))


class TestRank(unittest.TestCase):
    def test_sorts_desc(self):
        jobs = [job(job_id="a", match_score=50),
                job(job_id="b", match_score=90),
                job(job_id="c", match_score=70)]
        rows = P.rank(jobs, 10)
        self.assertEqual([r["job_id"] for r in rows], ["b", "c", "a"])

    def test_limit(self):
        jobs = [job(job_id=str(i), match_score=i) for i in range(20)]
        self.assertEqual(len(P.rank(jobs, 5)), 5)

    def test_tie_break_by_match(self):
        """同分按 match_score 排，避免顺序随机。"""
        jobs = [job(job_id="low", match_score=60),
                job(job_id="high", match_score=95)]
        for j in jobs:
            j["boss_active"] = j["experience"] = j["salary"] = ""
            j["job_description"] = ""
            j["first_seen"] = None
        rows = P.rank(jobs, 10)
        self.assertEqual(rows[0]["job_id"], "high")

    def test_active_beats_higher_match(self):
        """HR 在线的低匹配岗可以盖过 HR 离线的稍高匹配岗。"""
        jobs = [job(job_id="offline_high", match_score=85, boss_active="离线"),
                job(job_id="online_low", match_score=80, boss_active="刚刚活跃")]
        rows = P.rank(jobs, 10)
        self.assertEqual(rows[0]["job_id"], "online_low")


class TestRender(unittest.TestCase):
    def test_empty(self):
        self.assertIn("没有可投的岗位", P.render([]))

    def test_shows_reason(self):
        out = P.render(P.rank([job()], 5), 100)
        self.assertIn("匹配", out)
        self.assertIn("HR", out)

    def test_disclaims(self):
        out = P.render(P.rank([job()], 5))
        self.assertIn("不是录用概率", out)

    def test_shows_pool_size(self):
        out = P.render(P.rank([job()], 5), 633)
        self.assertIn("候选池 633", out)


class TestStackIntegration(unittest.TestCase):
    """技术栈判定必须真的影响排序 —— 否则就是个只会打印的死模块。"""

    def _job(self, jd: str, title="AI应用开发") -> dict:
        return {"job_id": "x", "title": title, "company": "某公司", "salary": "10-15K",
                "city": "广州", "experience": "经验不限", "boss_active": "刚刚活跃",
                "job_description": jd}

    def test_java_job_scored_lower_than_python_job(self):
        py = P.score(self._job("Python + LangChain + RAG 做 LLM 应用"))
        java = P.score(self._job("Java + Spring Boot + MyBatis 后端开发"))
        self.assertLess(java["total"], py["total"])
        self.assertIn("技术栈不匹配", java["flags"])

    def test_no_jd_adds_no_stack_adjust(self):
        out = P.score(self._job(""))
        self.assertFalse(any("技术栈" in a["why"] for a in out["adjusts"]))
        self.assertNotIn("技术栈不匹配", out["flags"])

    def test_stack_adjust_has_reason(self):
        out = P.score(self._job("Java + Spring Boot"))
        stack_items = [a for a in out["adjusts"] if "技术栈" in a["why"]]
        self.assertEqual(len(stack_items), 1)
        self.assertTrue(stack_items[0]["why"].strip())


if __name__ == "__main__":
    unittest.main()
