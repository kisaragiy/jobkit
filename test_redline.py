"""岗位红线检测：规则命中、薪资换算、误判防护。"""
import unittest

import redline


def job(title="", jd="", salary="", company="某公司"):
    return {"title": title, "company": company,
            "salary": salary, "job_description": jd}


def ids(r):
    return {f["id"] for f in r["reds"] + r["yellows"]}


class TestSalaryK(unittest.TestCase):
    def test_k_range(self):
        self.assertEqual(redline._salary_k("12-21K·13薪"), (12.0, 21.0))

    def test_month_yuan(self):
        self.assertEqual(redline._salary_k("2000-4000元/月"), (2.0, 4.0))

    def test_daily_converts_to_monthly(self):
        """300-350元/天 ≈ 6.6-7.7K/月，不能被当成 300-350 元/月。"""
        lo, hi = redline._salary_k("300-350元/天")
        self.assertAlmostEqual(lo, 6.6, places=1)
        self.assertAlmostEqual(hi, 7.7, places=1)

    def test_daily_without_yuan(self):
        lo, hi = redline._salary_k("120-180/天")
        self.assertAlmostEqual(lo, 2.64, places=2)

    def test_hourly(self):
        lo, hi = redline._salary_k("5-20元/时")
        self.assertAlmostEqual(lo, 0.88, places=2)
        self.assertAlmostEqual(hi, 3.52, places=2)

    def test_unparseable(self):
        self.assertIsNone(redline._salary_k("面议"))
        self.assertIsNone(redline._salary_k(""))


class TestHardRules(unittest.TestCase):
    def test_deposit_is_red(self):
        r = redline.check(job(jd="入职需缴纳服装费 200 元"))
        self.assertIn("pay_to_work", ids(r))
        self.assertEqual(r["verdict"], "skip")

    def test_training_trap(self):
        r = redline.check(job(jd="零基础培训，学完包就业"))
        self.assertIn("training_trap", ids(r))
        self.assertEqual(r["verdict"], "skip")

    def test_salary_vague(self):
        r = redline.check(job(salary="薪资面议"))
        self.assertIn("salary_vague", ids(r))

    def test_no_social_insurance(self):
        r = redline.check(job(jd="不缴社保，签劳务协议"))
        self.assertIn("no_contract", ids(r))

    def test_mlm(self):
        r = redline.check(job(jd="发展下线，团队裂变收益"))
        self.assertIn("mlm", ids(r))


class TestSalaryMismatch(unittest.TestCase):
    def test_low_pay_dev_is_red(self):
        r = redline.check(job("AI应用开发工程师", salary="2000-4000元/月"))
        self.assertIn("salary_mismatch", ids(r))
        self.assertEqual(r["verdict"], "skip")

    def test_internship_exempt(self):
        """实习生日薪 2K 多/月是正常的，不该判红线。"""
        r = redline.check(job("AI应用开发实习生", salary="100-110元/天"))
        self.assertNotIn("salary_mismatch", ids(r))

    def test_big_company_intern_daily_rate(self):
        """300-350元/天 ≈ 7K/月，正常实习价。"""
        r = redline.check(job("软件开发实习（AI应用方向）",
                              salary="300-350元/天"))
        self.assertNotIn("salary_mismatch", ids(r))

    def test_normal_salary_ok(self):
        r = redline.check(job("Python后端开发", salary="10-15K"))
        self.assertNotIn("salary_mismatch", ids(r))

    def test_senior_title_low_pay(self):
        r = redline.check(job("资深前端开发工程师", salary="5-20元/时"))
        self.assertIn("salary_mismatch", ids(r))


class TestSoftRules(unittest.TestCase):
    def test_mass_hiring(self):
        r = redline.check(job("Python开发（急聘3人）"))
        self.assertIn("mass_hiring", ids(r))

    def test_outsourcing(self):
        r = redline.check(job(jd="需长期驻场客户现场"))
        self.assertIn("outsource", ids(r))

    def test_overwork(self):
        r = redline.check(job(jd="大小周，能接受加班"))
        self.assertIn("overwork", ids(r))

    def test_part_time(self):
        r = redline.check(job("AI开发工程师(兼职·按单结算)"))
        self.assertIn("part_time", ids(r))

    def test_fullstack_not_flagged(self):
        """「全栈」是正常职位，不能算一人多岗 —— 第一版命中 58 个。"""
        r = redline.check(job("全栈开发工程师", salary="15-25K"))
        self.assertNotIn("multi_role", ids(r))

    def test_real_multi_role(self):
        r = redline.check(job(jd="同时负责前后端和运维，兼岗"))
        self.assertIn("multi_role", ids(r))


class TestVerdict(unittest.TestCase):
    def test_clean_job(self):
        r = redline.check(job("Python后端开发", salary="12-18K",
                              jd="负责后端服务开发。使用 FastAPI、MySQL。" * 6))
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["reds"], [])

    def test_one_yellow_is_note(self):
        r = redline.check(job("后端开发", salary="12-15K",
                              jd="需要长期驻场客户现场。" * 8))
        self.assertEqual(r["verdict"], "note")

    def test_three_yellows_is_risky(self):
        r = redline.check(job("后端开发", salary="12-15K",
                              jd="驻场，大小周，底薪+提成。" * 5))
        self.assertEqual(r["verdict"], "risky")

    def test_empty_jd_warns(self):
        r = redline.check(job("后端开发", salary="12-15K"))
        self.assertTrue(any("完全没有 JD" in w for w in r["warnings"]))

    def test_short_jd_warns(self):
        r = redline.check(job("后端开发", salary="12-15K", jd="写代码"))
        self.assertTrue(any("太短" in w for w in r["warnings"]))


class TestRender(unittest.TestCase):
    def test_render_shows_hits(self):
        r = redline.check(job("AI开发", salary="2000-4000元/月"))
        out = redline.render(r)
        self.assertIn("薪资与岗位不匹配", out)
        self.assertIn("2000-4000元/月", out)

    def test_render_disclaims(self):
        out = redline.render(redline.check(job("后端", salary="12-15K")))
        self.assertIn("不判公司真伪", out)


if __name__ == "__main__":
    unittest.main()
