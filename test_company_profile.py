"""公司画像：薪资解析 + 风险信号 + 不编造数据。"""
import tempfile
import unittest
from pathlib import Path

import company_profile as CP
import store


class TestSalaryParse(unittest.TestCase):
    def test_k_range(self):
        self.assertEqual(CP._parse_salary("12-21K·13薪"), (12.0, 21.0))

    def test_k_range_plain(self):
        self.assertEqual(CP._parse_salary("8-12K"), (8.0, 12.0))

    def test_yuan_per_month(self):
        lo, hi = CP._parse_salary("2000-4000元/月")
        self.assertAlmostEqual(lo, 2.0)
        self.assertAlmostEqual(hi, 4.0)

    def test_wan(self):
        lo, hi = CP._parse_salary("1-2万")
        self.assertAlmostEqual(lo, 10.0)
        self.assertAlmostEqual(hi, 20.0)

    def test_single_value(self):
        lo, hi = CP._parse_salary("15K")
        self.assertEqual((lo, hi), (15.0, 15.0))

    def test_unparseable(self):
        self.assertIsNone(CP._parse_salary("面议"))
        self.assertIsNone(CP._parse_salary(""))
        self.assertIsNone(CP._parse_salary(None))


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = store.DB_PATH
        store.DB_PATH = Path(self.tmp) / "t.db"
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old

    def add(self, title, company="某公司", salary="10-15K", scale="100-499人",
            industry="人工智能", boss_active="在线", days_ago=1, jd=""):
        import time
        self.c.execute(
            "INSERT INTO jobs (job_id, title, company, salary, city, district, "
            "scale, industry, boss_active, first_seen, last_seen, job_description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{company}_{title}", title, company, salary, "广州", "天河区",
             scale, industry, boss_active,
             time.time() - days_ago * 86400, time.time(), jd))
        self.c.commit()


class TestProfile(_DB):
    def test_empty_name(self):
        self.assertFalse(CP.profile(self.c, "")["ok"])

    def test_unknown_company(self):
        r = CP.profile(self.c, "不存在的公司")
        self.assertFalse(r["ok"])
        self.assertIn("没有", r["reason"])

    def test_basic_aggregate(self):
        self.add("Python后端")
        self.add("AI应用开发", salary="20-30K")
        p = CP.profile(self.c, "某公司")
        self.assertTrue(p["ok"])
        self.assertEqual(p["job_count"], 2)
        self.assertEqual(p["salary_min"], 10.0)
        self.assertEqual(p["salary_max"], 30.0)

    def test_fuzzy_company_match(self):
        self.add("后端", company="上海兴岩信息科技有限公司")
        p = CP.profile(self.c, "上海兴岩")
        self.assertTrue(p["ok"], "短名应该能模糊匹配到全称")

    def test_old_posting_flagged(self):
        """挂 90 天还在 → 大概率没真实 HC。"""
        self.add("后端", days_ago=90)
        p = CP.profile(self.c, "某公司")
        self.assertGreaterEqual(p["oldest_days"], 89)

    def test_risk_training_loan(self):
        self.add("Java开发（接受小白，包就业）")
        p = CP.profile(self.c, "某公司")
        kinds = [r["kind"] for r in p["risks"]]
        self.assertIn("培训贷嫌疑", kinds)

    def test_risk_mass_hiring(self):
        self.add("Python开发（急聘3个）")
        p = CP.profile(self.c, "某公司")
        kinds = [r["kind"] for r in p["risks"]]
        self.assertIn("铺量招聘", kinds)

    def test_risk_vague_salary(self):
        self.add("后端", jd="薪资面议，具体详谈")
        p = CP.profile(self.c, "某公司")
        kinds = [r["kind"] for r in p["risks"]]
        self.assertIn("描述模糊", kinds)

    def test_no_risk_when_clean(self):
        self.add("Python后端开发", jd="负责后端服务开发，使用 FastAPI")
        p = CP.profile(self.c, "某公司")
        self.assertEqual(p["risks"], [])

    def test_small_scale_flag(self):
        self.add("全栈", scale="0-20人")
        p = CP.profile(self.c, "某公司")
        self.assertTrue(p["small_scale"])


class TestRender(unittest.TestCase):
    def test_render_has_disclaimer(self):
        """必须写清「不是尽调结论」，否则用户会当成背调结果。"""
        p = {"ok": True, "company": "某公司", "job_count": 2,
             "salary_min": 10.0, "salary_max": 20.0, "salary_samples": 2,
             "scales": [("100-499人", 2)], "industries": [("AI", 2)],
             "boss_active": [("在线", 2)], "out_sourcing": "", "oldest_days": 5,
             "risks": [], "small_scale": False, "titles": ["后端"]}
        out = CP.render(p)
        self.assertIn("不是尽调结论", out)
        self.assertIn("查不到的东西", out)

    def test_render_wide_salary_warns(self):
        p = {"ok": True, "company": "某公司", "job_count": 2,
             "salary_min": 5.0, "salary_max": 30.0, "salary_samples": 2,
             "scales": [], "industries": [], "boss_active": [],
             "out_sourcing": "", "oldest_days": None, "risks": [],
             "small_scale": False, "titles": []}
        self.assertIn("跨度", CP.render(p))

    def test_render_fail(self):
        self.assertIn("无法查询", CP.render({"ok": False, "reason": "没有"}))


if __name__ == "__main__":
    unittest.main()
