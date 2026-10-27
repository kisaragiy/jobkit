"""异地收支测算：个税/社保/结余/警告。

关键：**不用人均统计数据**。支出全部由调用方给。
"""
import unittest

import living_cost as LC


class TestIncomeTax(unittest.TestCase):
    def test_zero_below_threshold(self):
        self.assertEqual(LC.income_tax(0), 0)
        self.assertEqual(LC.income_tax(-100), 0)

    def test_3pct_bracket(self):
        self.assertAlmostEqual(LC.income_tax(1000), 30.0)
        self.assertAlmostEqual(LC.income_tax(3000), 90.0)

    def test_10pct_bracket(self):
        self.assertAlmostEqual(LC.income_tax(5000), 290.0)

    def test_20pct_bracket(self):
        self.assertAlmostEqual(LC.income_tax(20000), 2590.0)


class TestTakeHome(unittest.TestCase):
    def test_shenzhen_8k(self):
        r = LC.take_home(8000, "深圳")
        self.assertAlmostEqual(r["social"], 1224.0)
        self.assertAlmostEqual(r["tax"], 8.28, places=2)
        self.assertAlmostEqual(r["net"], 6767.72, places=2)

    def test_shenzhen_9k(self):
        r = LC.take_home(9000, "深圳")
        self.assertAlmostEqual(r["social"], 1377.0)
        self.assertAlmostEqual(r["net"], 7589.31, places=2)

    def test_dongguan_7k_no_tax(self):
        """7K 在东莞扣完五险一金和专项后不缴个税。"""
        r = LC.take_home(7000, "东莞")
        self.assertEqual(r["tax"], 0)
        self.assertAlmostEqual(r["net"], 5929.0, places=2)

    def test_rent_deduction_differs(self):
        """深圳 1500、东莞 1100 —— 附加扣除标准不同，个税要跟着变。"""
        self.assertEqual(LC.take_home(8000, "深圳")["rent_deduction"], 1500)
        self.assertEqual(LC.take_home(8000, "东莞")["rent_deduction"], 1100)

    def test_no_rent_deduction(self):
        with_ded = LC.take_home(8000, "深圳", rent_deduction=True)
        without = LC.take_home(8000, "深圳", rent_deduction=False)
        self.assertGreaterEqual(without["tax"], with_ded["tax"])

    def test_unknown_city_uses_default(self):
        r = LC.take_home(8000, "火星")
        self.assertEqual(r["social_rate"], LC.DEFAULT_SOCIAL_RATE)


class TestBalance(unittest.TestCase):
    def test_empty_spend_warns(self):
        """一项不填 → 必须警告结余虚高，不能默默给个漂亮数字。"""
        b = LC.balance(8000, "深圳")
        self.assertAlmostEqual(b["left"], 6767.72, places=2)
        self.assertEqual(len(b["missing"]), 4)
        self.assertTrue(any("虚高" in w for w in b["warnings"]))

    def test_partial_spend_lists_missing(self):
        b = LC.balance(8000, "深圳", rent=3000)
        self.assertIn("吃饭", b["missing"])
        self.assertIn("交通", b["missing"])

    def test_full_calc(self):
        b = LC.balance(9000, "深圳", 3500, 1500, 300, 500)
        expected = 7589.31 - (3500 + 1500 + 300 + 500)
        self.assertAlmostEqual(b["left"], expected, delta=1)
        self.assertEqual(b["missing"], [])

    def test_save_rate(self):
        b = LC.balance(9000, "深圳", 3500, 1500, 300, 500)
        self.assertAlmostEqual(b["save_rate"],
                               round(b["left"] / 7589.31 * 100, 1), delta=0.2)

    def test_high_rent_warning(self):
        b = LC.balance(8000, "深圳", 5000, 1000, 300, 500)
        self.assertTrue(any("房租占" in w for w in b["warnings"]))

    def test_negative_left(self):
        b = LC.balance(6000, "深圳", 4000, 2000, 300, 500)
        self.assertLess(b["left"], 0)
        self.assertTrue(any("撑不住" in w for w in b["warnings"]))

    def test_low_savings_warning(self):
        b = LC.balance(8000, "深圳", 3500, 1500, 300, 500)
        self.assertTrue(any("低于 1000" in w for w in b["warnings"]))

    def test_comfortable(self):
        b = LC.balance(20000, "东莞", 2000, 1500, 300, 500)
        self.assertGreater(b["left"], 10000)
        self.assertFalse(any("撑不住" in w for w in b["warnings"]))

    def test_runway(self):
        """攒 3 个月开销要多久 = 月开销×3 / 月结余。"""
        b = LC.balance(15000, "深圳", 3000, 1500, 300, 500)
        total = 3000 + 1500 + 300 + 500
        self.assertAlmostEqual(b["runway_months"],
                               round(total * 3 / b["left"], 1), delta=0.2)

    def test_no_runway_when_negative(self):
        b = LC.balance(6000, "深圳", 4000, 2000, 300, 500)
        self.assertIsNone(b["runway_months"])


class TestRender(unittest.TestCase):
    def test_render_full(self):
        out = LC.render(LC.balance(9000, "深圳", 3500, 1500, 300, 500))
        self.assertIn("到手", out)
        self.assertIn("房租", out)
        self.assertIn("月结余", out)
        self.assertIn("结余率", out)

    def test_render_unfilled(self):
        out = LC.render(LC.balance(8000, "深圳", 3000))
        self.assertIn("未填", out)
        self.assertIn("虚高", out)

    def test_no_average_data_used(self):
        """渲染结果里不能出现人均消费支出 —— 那是被明确否决的口径。"""
        out = LC.render(LC.balance(9000, "深圳", 3500, 1500, 300, 500))
        self.assertNotIn("人均", out)

    def test_render_disclaims(self):
        out = LC.render(LC.balance(9000, "深圳", 3500, 1500, 300, 500))
        self.assertIn("不猜生活成本", out)

    def test_render_fail(self):
        self.assertIn("失败", LC.render({"ok": False, "reason": "x"}))


class TestPresetData(unittest.TestCase):
    """内置档位数据（data/living_cost.json）：结构、口径、归一化、取数。"""

    def test_covers_five_cities(self):
        self.assertEqual(sorted(LC.covered_cities()),
                         sorted(["广州", "深圳", "东莞", "佛山", "珠海"]))
        cov = LC.data_meta()["coverage"]
        self.assertEqual(cov["cities"], 5)
        self.assertEqual(cov["districts"], 38)
        self.assertEqual(cov["tiers"], 88)

    def test_every_tier_sums_to_total(self):
        """分项和必须等于 total —— 数据坏掉不许静默进库。"""
        d = LC.load_data()
        n = 0
        for city, c in d["cities"].items():
            for dist, dd in c["districts"].items():
                for scene, tiers in dd["tiers"].items():
                    for tier, v in tiers.items():
                        with self.subTest(city=city, dist=dist, scene=scene, tier=tier):
                            self.assertEqual(
                                sum(v[k] for k in LC.COST_ITEMS), v["total"])
                            self.assertGreater(v["total"], 0)
                        n += 1
        self.assertEqual(n, 88)

    def test_every_district_has_rent_scene(self):
        d = LC.load_data()
        for city, c in d["cities"].items():
            for dist, dd in c["districts"].items():
                with self.subTest(city=city, dist=dist):
                    self.assertEqual(sorted(dd["tiers"]["rent"]), ["frugal", "normal"])

    def test_every_tier_has_source_or_note(self):
        """每档都得能追溯：房租有来源，档位有 note 或估算依据。"""
        d = LC.load_data()
        for city, c in d["cities"].items():
            for dist, dd in c["districts"].items():
                with self.subTest(city=city, dist=dist):
                    self.assertTrue(dd["rent_source"].get("url")
                                    or dd["rent_source"].get("platform"))
                    self.assertTrue(dd["estimate_basis"].get("food"))
                    for tiers in dd["tiers"].values():
                        for tier, v in tiers.items():
                            self.assertTrue(v["note"])


class TestDistrictResolve(unittest.TestCase):
    def test_suffix_stripped(self):
        self.assertEqual(LC.resolve_district("广州", "天河区"), "天河")
        self.assertEqual(LC.resolve_district("深圳", "南山区"), "南山")
        self.assertEqual(LC.resolve_district("东莞", "南城街道"), "南城")
        self.assertEqual(LC.resolve_district("东莞", "虎门镇"), "虎门")
        self.assertEqual(LC.resolve_district("深圳", "大鹏新区"), "大鹏")

    def test_exact_hit(self):
        self.assertEqual(LC.resolve_district("广州", "天河"), "天河")

    def test_paren_alias(self):
        self.assertEqual(LC.resolve_district("珠海", "高新区"), "唐家湾（高新区）")
        self.assertEqual(LC.resolve_district("珠海", "唐家湾"), "唐家湾（高新区）")

    def test_unknown_returns_none(self):
        self.assertIsNone(LC.resolve_district("深圳", "杭州西湖"))
        self.assertIsNone(LC.resolve_district("广州", ""))
        self.assertIsNone(LC.resolve_district("火星", "天河"))


class TestPlanCost(unittest.TestCase):
    def test_district_breakdown(self):
        p = LC.plan_cost("广州", "天河区", "normal")
        self.assertTrue(p["ok"])
        self.assertEqual(p["district"], "天河")
        self.assertEqual(p["total"], 3050)
        self.assertEqual(p["breakdown"]["rent"], 800)
        self.assertEqual(p["breakdown"]["utilities"], 200)
        self.assertEqual(p["breakdown"]["comm_daily"], 300)
        # 给 balance 的四栏口径：水电 + 通讯日杂 并入 other
        self.assertEqual(p["spend"]["other"], 500)
        self.assertEqual(sum(p["spend"].values()), p["total"])

    def test_total_range_brackets_total(self):
        """区间来自房租/餐饮原始区间：下限 ≤ 档位值 ≤ 上限。"""
        p = LC.plan_cost("广州", "天河", "normal")
        lo, hi = p["total_range"]
        self.assertLess(lo, p["total"])
        self.assertGreater(hi, p["total"])
        # 3050 + (600-800) + (1200-1500)
        self.assertEqual(lo, 2550)
        self.assertEqual(hi, 3550)

    def test_estimate_flags(self):
        p = LC.plan_cost("深圳", "南山", "frugal")
        self.assertTrue(p["estimated"])
        self.assertEqual(sorted(p["estimate_items"]),
                         sorted(["utilities", "food", "transport", "comm_daily"]))
        self.assertIn("58同城", p["rent_source"]["platform"])
        self.assertTrue(p["rent_source"]["url"].startswith("http"))
        self.assertEqual(p["collected_at"], "2026-10-20")

    def test_city_median_when_no_district(self):
        p = LC.plan_cost("广州", None, "normal")
        self.assertTrue(p["ok"])
        self.assertEqual(p["basis"], "city_median")
        self.assertIsNone(p["district"])
        self.assertEqual(p["covered_districts"], 11)
        self.assertEqual(p["total"], 3050)

    def test_home_mode_zero_rent(self):
        p = LC.plan_cost("东莞", "虎门镇", "normal", "home")
        self.assertTrue(p["ok"])
        self.assertEqual(p["breakdown"]["rent"], 0)
        self.assertEqual(p["total"], 1970)
        self.assertEqual(p["total_range"], [1970, 1970])   # 住家没有区间
        self.assertEqual(p["spend"]["transport"], 120)

    def test_home_mode_not_available(self):
        """塘厦通勤超 60 分钟 —— 没有住家档，必须报缺不是给 0 房租。"""
        p = LC.plan_cost("东莞", "塘厦", "normal", "home")
        self.assertFalse(p["ok"])
        self.assertTrue(p.get("missing_scene"))
        self.assertIn("塘厦", p["reason"])

    def test_co_rent(self):
        p = LC.plan_cost("佛山", "三水区", "normal", "co_rent")
        self.assertTrue(p["ok"])
        self.assertEqual(p["total"], 2550)
        self.assertEqual(p["breakdown"]["rent"], 450)

    def test_unknown_city_is_loud(self):
        p = LC.plan_cost("惠州", "惠城区", "normal")
        self.assertFalse(p["ok"])
        self.assertTrue(p["missing_city"])
        self.assertIn("惠州", p["reason"])

    def test_unknown_district_is_loud(self):
        p = LC.plan_cost("深圳", "罗福", "normal")
        self.assertFalse(p["ok"])
        self.assertTrue(p["unmatched_district"])

    def test_bad_tier_and_scene(self):
        self.assertFalse(LC.plan_cost("广州", "天河", "cheap")["ok"])
        self.assertFalse(LC.plan_cost("广州", "天河", "normal", "hotel")["ok"])

    def test_render_plan_has_source_line(self):
        out = LC.render_plan(LC.plan_cost("深圳", "南山区", "normal"))
        self.assertIn("深圳南山", out)
        self.assertIn("58同城", out)
        self.assertIn("估算项", out)


class TestBalanceWithPreset(unittest.TestCase):
    def test_preset_fills_all_four(self):
        b = LC.balance(8000, "深圳", district="南山区", tier="normal")
        self.assertTrue(b["ok"])
        self.assertEqual(sorted(b["preset"]), ["food", "other", "rent", "transport"])
        self.assertEqual(b["spend"]["rent"], 2500)
        self.assertEqual(b["total_spend"], 4880)
        self.assertAlmostEqual(b["left"], 6767.72 - 4880, places=2)
        self.assertEqual(b["missing"], [])
        self.assertTrue(any("预置档位" in w for w in b["warnings"]))
        self.assertTrue(any("估算项" in w for w in b["warnings"]))

    def test_manual_value_wins(self):
        b = LC.balance(8000, "深圳", rent=1000, district="南山区", tier="frugal")
        self.assertEqual(b["spend"]["rent"], 1000)
        self.assertNotIn("rent", b["preset"])
        self.assertEqual(len(b["preset"]), 3)

    def test_home_mode_zero_rent_is_valid(self):
        """住家档 rent=0 是有效值，不能被当成"没填"再报虚高。"""
        b = LC.balance(9000, "东莞", district="虎门镇", tier="normal", scene="home")
        self.assertTrue(b["ok"])
        self.assertEqual(b["spend"]["rent"], 0)
        self.assertEqual(b["missing"], [])
        self.assertFalse(any("虚高" in w for w in b["warnings"]))
        self.assertNotIn("rent_ratio", b)

    def test_no_data_fails_loudly(self):
        """没覆盖的城市：报错，不许按 0 支出算出一个漂亮结余。"""
        b = LC.balance(8000, "惠州", district="惠城区", tier="normal")
        self.assertFalse(b["ok"])
        self.assertNotIn("left", b)
        self.assertIn("惠州", b["reason"])

    def test_city_median_warns(self):
        b = LC.balance(8000, "广州", tier="normal")
        self.assertTrue(b["ok"])
        self.assertTrue(any("城市中位" in w or "档位中位" in w for w in b["warnings"]))

    def test_render_marks_preset_and_estimate(self):
        out = LC.render(LC.balance(9000, "深圳", district="南山区", tier="normal"))
        self.assertIn("预置档位", out)
        self.assertIn("估算项", out)
        self.assertNotIn("人均", out)

    def test_render_home_mode(self):
        out = LC.render(LC.balance(9000, "东莞", district="虎门镇",
                                   tier="normal", scene="home"))
        self.assertIn("住家", out)
        self.assertIn("无房租", out)


class TestCompare(unittest.TestCase):
    def test_city_rows_sorted(self):
        c = LC.compare_tiers("深圳", 8000)
        self.assertTrue(c["ok"])
        self.assertEqual(len(c["rows"]), 20)     # 10 区 × 2 档
        totals = [r["total"] for r in c["rows"]]
        self.assertEqual(totals, sorted(totals))
        self.assertIn("left", c["rows"][0])

    def test_district_two_rows(self):
        c = LC.compare_tiers("广州", 8000, district="天河区")
        self.assertTrue(c["ok"])
        self.assertEqual(len(c["rows"]), 2)
        self.assertEqual(c["rows"][0]["district"], "天河")

    def test_unknown_city(self):
        self.assertFalse(LC.compare_tiers("惠州", 8000)["ok"])

    def test_render_compare(self):
        out = LC.render_compare(LC.compare_tiers("东莞", 9000), 9000)
        self.assertIn("结余率", out)
        self.assertIn("估算项", out)


class TestAppPanel(unittest.TestCase):
    """岗位详情面板（app._p_living）：日薪折算、无区回退、无数据城市。"""

    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app

    def test_daily_pay_not_inflated(self):
        """日薪岗不能算成「月结余六万」—— 按 22 天折月薪后再减支出。"""
        lv = self.app._p_living({"city": "广州", "district": "天河区",
                                 "salary": "100-110元/天"})
        self.assertTrue(lv["ok"])
        self.assertTrue(lv["daily_pay"])
        self.assertAlmostEqual(lv["pretax_lo"], 2200, delta=1)
        for t in lv["tiers"]:
            self.assertLess(t["left"], lv["pretax_lo"])

    def test_monthly_pay_not_flagged_daily(self):
        lv = self.app._p_living({"city": "深圳", "district": "南山区",
                                 "salary": "8-12K"})
        self.assertFalse(lv["daily_pay"])
        self.assertEqual(lv["pretax_lo"], 8000)

    def test_no_district_falls_back_to_city_median(self):
        lv = self.app._p_living({"city": "广州", "district": "",
                                 "salary": "8-12K"})
        self.assertTrue(lv["ok"])
        self.assertFalse(lv["matched"])
        self.assertEqual(lv["basis"], "city_median")
        self.assertEqual(len(lv["tiers"]), 2)

    def test_unknown_city_is_loud(self):
        lv = self.app._p_living({"city": "惠州", "district": "惠城区",
                                 "salary": "7-9K"})
        self.assertFalse(lv["ok"])
        self.assertIn("惠州", lv["reason"])

    def test_dongguan_gets_home_scenario(self):
        lv = self.app._p_living({"city": "东莞", "district": "虎门镇",
                                 "salary": "7-9K"})
        self.assertTrue(lv["ok"])
        self.assertIn("home", lv)
        self.assertEqual(lv["home"]["total"], 1970)

    def test_negotiable_salary_no_left(self):
        lv = self.app._p_living({"city": "广州", "district": "天河区",
                                 "salary": "面议"})
        self.assertTrue(lv["ok"])
        self.assertEqual(lv["pretax_lo"], 0)
        self.assertNotIn("left", lv["tiers"][0])

    def test_unknown_district_falls_back_to_city_median(self):
        """大朗镇没预置数据 —— 给全城中位但必须标 fallback，不能冒充那个镇。"""
        lv = self.app._p_living({"city": "东莞", "district": "大朗镇",
                                 "salary": "300-350元/天"})
        self.assertTrue(lv["ok"])
        self.assertTrue(lv["fallback"])
        self.assertEqual(lv["basis"], "city_median")
        self.assertIn("大朗镇", lv["note"])
        self.assertIn("全城中位", lv["note"])

    def test_no_city(self):
        self.assertFalse(self.app._p_living({"city": "", "salary": "8-12K"})["ok"])


if __name__ == "__main__":
    unittest.main()
