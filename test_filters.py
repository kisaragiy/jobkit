"""filters.py 行为测试。用例对齐 GetJob 2.3.1 各 filter 模块的语义边界。"""
import unittest

import filters as F


class TestHrNormalize(unittest.TestCase):
    def test_alias(self):
        self.assertEqual(F.resolve_hr_index("刚刚活跃"), 1)
        self.assertEqual(F.resolve_hr_index("本周活跃"), 4)

    def test_parametric(self):
        self.assertEqual(F.resolve_hr_index("23分钟前在线"), 1)
        self.assertEqual(F.resolve_hr_index("3小时前在线"), 2)
        self.assertEqual(F.resolve_hr_index("今日回复12次"), 2)
        self.assertEqual(F.resolve_hr_index("5天内处理简历"), 4)

    def test_chinese_numerals(self):
        self.assertEqual(F.resolve_hr_index("三日内活跃"), 3)
        self.assertEqual(F.resolve_hr_index("7日内活跃"), 4)

    def test_full_width_input(self):
        self.assertEqual(F.resolve_hr_index("　本周活跃　"), 4)

    def test_joined_text_takes_freshest(self):
        # "本周活跃 · 刚刚回复" 应识别为 just_active（更新鲜）
        self.assertEqual(F.resolve_hr_index("本周活跃 · 刚刚回复"), 1)

    def test_unknown_returns_none(self):
        self.assertIsNone(F.resolve_hr_index("在线客服"))  # 职位名里的「在线」不算
        self.assertIsNone(F.resolve_hr_index(""))
        self.assertIsNone(F.resolve_hr_index(None))

    def test_offline_is_just_active(self):
        # BOSS 的「离线」出现在活跃度位置时按刚刚活跃算
        self.assertEqual(F.resolve_hr_index("离线"), 1)


class TestHrFilter(unittest.TestCase):
    cfg = {"hr_activity_filter": True, "hr_activity_min": "week"}

    def test_pass_fresh(self):
        self.assertIsNone(F.check_hr_activity({"boss_active": "今日活跃"}, self.cfg))

    def test_pass_at_boundary(self):
        self.assertIsNone(F.check_hr_activity({"boss_active": "本周活跃"}, self.cfg))

    def test_reject_stale(self):
        self.assertEqual(F.check_hr_activity({"boss_active": "本月活跃"}, self.cfg), "活跃不足")

    def test_missing_field_passes(self):
        self.assertIsNone(F.check_hr_activity({"boss_active": ""}, self.cfg))

    def test_disabled_by_default(self):
        self.assertIsNone(F.check_hr_activity({"boss_active": "半年前活跃"}, {}))

    def test_unrecognized_passes(self):
        self.assertIsNone(F.check_hr_activity({"boss_active": "回复很快"}, self.cfg))


class TestExperience(unittest.TestCase):
    def test_unlimited(self):
        self.assertEqual(F.normalize_exp_bucket("经验不限"), F.UNLIMITED)
        self.assertEqual(F.normalize_exp_bucket("不限"), F.UNLIMITED)

    def test_fresh_grad(self):
        self.assertEqual(F.normalize_exp_bucket("应届生"), "应届生")
        self.assertEqual(F.normalize_exp_bucket("在校生"), "在校生")

    def test_ranges(self):
        self.assertEqual(F.normalize_exp_bucket("1-3年"), "1-3年")
        self.assertEqual(F.normalize_exp_bucket("3-5年"), "3-5年")
        self.assertEqual(F.normalize_exp_bucket("1年以内"), "1年以内")
        self.assertEqual(F.normalize_exp_bucket("无需经验"), "1年以内")

    def test_above(self):
        self.assertEqual(F.normalize_exp_bucket("10年以上"), "10年以上")
        self.assertIsNone(F.normalize_exp_bucket("5年以上"))  # 5-10 与 10+ 都可能，不猜

    def test_unknown(self):
        self.assertIsNone(F.normalize_exp_bucket("若干"))
        self.assertIsNone(F.normalize_exp_bucket(""))


class TestDegree(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(F.normalize_degree_bucket("本科"), "本科")
        self.assertEqual(F.normalize_degree_bucket("硕士研究生"), "硕士")
        self.assertEqual(F.normalize_degree_bucket("大专及以上"), "大专")
        self.assertEqual(F.normalize_degree_bucket("学历不限"), F.UNLIMITED)
        self.assertIsNone(F.normalize_degree_bucket(""))


class TestSalary(unittest.TestCase):
    def test_k_range(self):
        self.assertEqual(F.parse_salary_k("12-18K"), (12.0, 18.0))
        self.assertEqual(F.parse_salary_k("12-18K·13薪"), (12.0, 18.0))

    def test_wan(self):
        self.assertEqual(F.parse_salary_k("1.5-2万"), (15.0, 20.0))

    def test_yuan(self):
        self.assertEqual(F.parse_salary_k("8000-12000"), (8.0, 12.0))
        self.assertEqual(F.parse_salary_k("8000-12000元/月"), (8.0, 12.0))

    def test_single(self):
        self.assertEqual(F.parse_salary_k("15K"), (15.0, 15.0))

    def test_negotiable(self):
        self.assertIsNone(F.parse_salary_k("面议"))
        self.assertIsNone(F.parse_salary_k(""))

    def test_reversed_range_normalized(self):
        self.assertEqual(F.parse_salary_k("18-12K"), (12.0, 18.0))

    def test_daily_pay_converted(self):
        """日薪必须先折月薪：100-110元/天 = 2.2-2.4K/月，不是 100-110K。"""
        lo, hi = F.parse_salary_k("100-110元/天")
        self.assertAlmostEqual(lo, 2.2, places=2)
        self.assertAlmostEqual(hi, 2.42, places=2)
        lo, hi = F.parse_salary_k("300-350元/天")
        self.assertAlmostEqual(lo, 6.6, places=2)
        self.assertAlmostEqual(hi, 7.7, places=2)

    def test_daily_pay_no_yuan_suffix(self):
        lo, hi = F.parse_salary_k("120-180/天")
        self.assertAlmostEqual(lo, 2.64, places=2)
        self.assertAlmostEqual(hi, 3.96, places=2)

    def test_single_daily_pay(self):
        self.assertEqual(F.parse_salary_k("200元/天"), (4.4, 4.4))

    def test_hourly_pay_converted(self):
        lo, hi = F.parse_salary_k("30-40元/小时")
        self.assertAlmostEqual(lo, 5.28, places=2)
        self.assertAlmostEqual(hi, 7.04, places=2)

    def test_monthly_not_mistaken_for_daily(self):
        self.assertEqual(F.parse_salary_k("8000-12000元/月"), (8.0, 12.0))
        self.assertEqual(F.parse_salary_k("15K/月"), (15.0, 15.0))


class TestRequirements(unittest.TestCase):
    def cfg(self, **kw):
        base = {"job_filter_enabled": True}
        base.update(kw)
        return base

    def test_disabled(self):
        self.assertIsNone(F.check_requirements({"experience": "5-10年"}, {}))

    def test_experience_mismatch(self):
        cfg = self.cfg(job_filter_experience=["应届生", "1年以内", "1-3年"])
        self.assertEqual(F.check_requirements({"experience": "5-10年"}, cfg), "经验不符")
        self.assertIsNone(F.check_requirements({"experience": "1-3年"}, cfg))

    def test_unlimited_passes(self):
        cfg = self.cfg(job_filter_experience=["应届生"])
        self.assertIsNone(F.check_requirements({"experience": "经验不限"}, cfg))

    def test_degree_mismatch(self):
        cfg = self.cfg(job_filter_degree=["本科"])
        self.assertEqual(F.check_requirements({"education": "博士"}, cfg), "学历不符")

    def test_salary_overlap(self):
        cfg = self.cfg(job_filter_salary_min_k=8, job_filter_salary_max_k=20)
        self.assertIsNone(F.check_requirements({"salary": "10-15K"}, cfg))
        self.assertEqual(F.check_requirements({"salary": "3-5K"}, cfg), "薪资不符")

    def test_salary_missing_passes(self):
        cfg = self.cfg(job_filter_salary_min_k=8, job_filter_salary_max_k=20)
        self.assertIsNone(F.check_requirements({"salary": "面议"}, cfg))

    def test_scale_overlap(self):
        cfg = self.cfg(job_filter_scale=["100-499人", "500-999人"])
        self.assertIsNone(F.check_requirements({"scale": "规模: 500-999人"}, cfg))
        self.assertEqual(F.check_requirements({"scale": "0-20人"}, cfg), "规模不符")

    def test_scale_missing_passes(self):
        cfg = self.cfg(job_filter_scale=["100-499人"])
        self.assertIsNone(F.check_requirements({}, cfg))


class TestDoubleRest(unittest.TestCase):
    def test_hint_in_welfare(self):
        self.assertTrue(F.has_double_rest_hint({"welfare": ["双休", "五险一金"]}))

    def test_hint_in_title(self):
        self.assertTrue(F.has_double_rest_hint({"title": "Java开发（双休）"}))

    def test_negation_in_desc(self):
        self.assertFalse(F.has_double_rest_hint({"job_description": "本公司非双休，接受再来"}))

    def test_filter(self):
        cfg = {"filter_double_rest": True}
        self.assertEqual(F.check_double_rest({"title": "前端"}, cfg), "未标双休")
        self.assertIsNone(F.check_double_rest({"title": "前端（双休）"}, cfg))
        self.assertIsNone(F.check_double_rest({"title": "前端"}, {}))  # 默认关


class TestWorkHours(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(F.parse_work_hours_text("09:00-18:00"), (540, 1080))
        self.assertEqual(F.parse_work_hours_text("9：00-18：00"), (540, 1080))
        self.assertIsNone(F.parse_work_hours_text("18:00-09:00"))  # 跨夜不猜

    def test_colloquial(self):
        self.assertEqual(F.parse_colloquial_hours("朝九晚六"), (540, 1080))
        self.assertEqual(F.parse_colloquial_hours("早8晚5"), (480, 1020))
        self.assertIsNone(F.parse_colloquial_hours("弹性工作"))

    def test_extract_from_desc(self):
        self.assertEqual(F.extract_work_hours_from_text("工作时间 9:30-19:30，双休"), (570, 1170))

    def test_job_hours_from_tags(self):
        self.assertEqual(F.extract_job_work_hours({"tags": ["双休", "朝九晚六"]}), (540, 1080))

    def test_filter_early_late(self):
        cfg = {"filter_work_hours": True, "work_hours_start": "09:30", "work_hours_end": "19:00"}
        self.assertEqual(F.check_work_hours({"work_hours": (480, 1080)}, cfg), "上班过早")
        self.assertEqual(F.check_work_hours({"work_hours": (600, 1260)}, cfg), "下班过晚")
        self.assertIsNone(F.check_work_hours({"work_hours": (570, 1080)}, cfg))

    def test_unmarked(self):
        cfg = {"filter_work_hours": True, "filter_unmarked_hours": True}
        self.assertEqual(F.check_work_hours({"title": "后端"}, cfg), "未标工时")
        cfg2 = {"filter_work_hours": True}
        self.assertIsNone(F.check_work_hours({"title": "后端"}, cfg2))


class TestFromSettings(unittest.TestCase):
    def test_defaults_all_off(self):
        c = F.from_settings({})
        self.assertFalse(c["hr_activity_filter"])
        self.assertFalse(c["job_filter_enabled"])
        self.assertFalse(c["filter_double_rest"])
        self.assertEqual(c["hr_activity_min"], "week")

    def test_maps_options(self):
        c = F.from_settings({"options": {"only_active_hr": True,
                                         "requirement_filter": True,
                                         "exclude_weekend_work": True},
                             "filters": {"scales": ["100-499人"],
                                         "salary_min_k": 8,
                                         "job_filter_experience": ["1-3年"]},
                             "limits": {"salary_max": 12000}})
        self.assertTrue(c["hr_activity_filter"])
        self.assertTrue(c["job_filter_enabled"])
        self.assertTrue(c["filter_double_rest"])
        self.assertEqual(c["job_filter_scale"], ["100-499人"])
        self.assertEqual(c["job_filter_salary_min_k"], 8)
        self.assertIsNone(c["job_filter_salary_max_k"])  # 不回落 limits.salary_max
        self.assertEqual(c["job_filter_experience"], ["1-3年"])


class TestChain(unittest.TestCase):
    def test_order_requirements_first(self):
        cfg = {"job_filter_enabled": True, "job_filter_experience": ["应届生"],
               "filter_double_rest": True}
        job = {"experience": "5-10年", "title": "后端"}
        ok, reason = F.check_job(job, cfg)
        self.assertFalse(ok)
        self.assertEqual(reason, "经验不符")  # 先报硬性要求，不报双休

    def test_all_pass(self):
        cfg = {"job_filter_enabled": True, "job_filter_experience": ["1-3年"],
               "filter_double_rest": True, "filter_work_hours": True,
               "work_hours_start": "09:30", "work_hours_end": "19:00"}
        job = {"experience": "1-3年", "title": "后端（双休）", "work_hours": (570, 1080)}
        self.assertEqual(F.check_job(job, cfg), (True, ""))

    def test_empty_config_passes_everything(self):
        self.assertEqual(F.check_job({"experience": "10年以上"}, {}), (True, ""))


class TestRedlineGuard(unittest.TestCase):
    """投递前的硬伤拦截：只拦收费/培训贷/传销，其余不拦。"""

    CFG = {"redline_guard": True}

    def job(self, jd="", title="Python开发", salary="12-18K"):
        return {"title": title, "salary": salary, "company": "某公司",
                "job_description": jd}

    def test_deposit_blocked(self):
        r = F.check_redline(self.job("入职需缴纳服装费 200 元"), self.CFG)
        self.assertIn("交钱", r)

    def test_training_trap_blocked(self):
        r = F.check_redline(self.job("零基础培训，包就业"), self.CFG)
        self.assertIn("培训贷", r)

    def test_mlm_blocked(self):
        r = F.check_redline(self.job("发展下线，团队裂变"), self.CFG)
        self.assertIn("传销", r)

    def test_salary_mismatch_not_blocked(self):
        """薪资不匹配有误判可能，不在这条链上拦（用 jobkit redline 自己查）。"""
        r = F.check_redline(self.job("负责后端开发", salary="3-4K"), self.CFG)
        self.assertIsNone(r)

    def test_clean_job_passes(self):
        self.assertIsNone(F.check_redline(
            self.job("负责后端服务开发，使用 FastAPI"), self.CFG))

    def test_switch_off(self):
        self.assertIsNone(F.check_redline(
            self.job("入职需缴纳服装费 200 元"), {"redline_guard": False}))

    def test_on_by_default(self):
        self.assertTrue(F.from_settings({})["redline_guard"])

    def test_explicit_off(self):
        self.assertFalse(F.from_settings(
            {"options": {"redline_guard": False}})["redline_guard"])

    def test_in_chain_before_requirements(self):
        """红线排在要求过滤前面 —— 坑岗不必再看经验学历。"""
        cfg = {"redline_guard": True, "job_filter_enabled": True,
               "job_filter_experience": ["应届生"]}
        job = self.job("入职需缴纳押金 500 元")
        job["experience"] = "5-10年"
        ok, reason = F.check_job(job, cfg)
        self.assertFalse(ok)
        self.assertIn("红线", reason)

    def test_missing_jd_field_ok(self):
        """job dict 没有 job_description 也不能崩。"""
        self.assertIsNone(F.check_redline({"title": "后端", "salary": "12-15K"},
                                          self.CFG))


if __name__ == "__main__":
    unittest.main(verbosity=2)
