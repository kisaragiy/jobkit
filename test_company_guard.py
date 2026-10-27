"""外包识别的单测 —— 重点是「不误伤」：单信号不能判，默认不拦。"""
from __future__ import annotations

import unittest

import company_guard as cg
import filters as F


class TestNameMatch(unittest.TestCase):
    def test_known_firms(self):
        for co in ("中软国际", "上海法本信息技术股份有限公司", "亿达信息",
                   "上海思芮", "外企德科数字", "中电金信", "微创软件",
                   "博彦科技", "宇信科技"):
            a = cg.analyze(co)
            self.assertEqual(a["level"], "confirmed", f"{co} 应判 confirmed")
            self.assertIn("命中", a["reason"])

    def test_staffing_keyword(self):
        a = cg.analyze("广州某某人力资源服务有限公司")
        self.assertEqual(a["level"], "confirmed")

    def test_real_company_not_flagged(self):
        for co in ("字节跳动", "华为技术有限公司", "阿里巴巴集团",
                   "广州市趋势信息技术有限公司", "比特鹰（深圳）科技有限公司"):
            self.assertEqual(cg.analyze(co)["level"], "ok", f"{co} 不该被判外包")

    def test_empty(self):
        self.assertEqual(cg.analyze("")["level"], "ok")
        self.assertEqual(cg.analyze(None)["level"], "ok")


class TestSignals(unittest.TestCase):
    def _jobs(self, titles, cities=None):
        cities = cities or ["广州"]
        return [{"title": t, "city": cities[i % len(cities)],
                 "industry": "计算机软件"} for i, t in enumerate(titles)]

    def test_single_signal_not_enough(self):
        """只有「跨多城市」一个信号，不能判 —— 误伤真公司的代价更高。"""
        jobs = self._jobs(["AI应用开发"], cities=["广州", "深圳", "东莞"])
        self.assertEqual(cg.analyze("某公司", jobs)["level"], "ok")

    def test_two_signals_suspected(self):
        titles = ["AI应用开发", "销售经理", "行政专员", "会计",
                  "前端开发", "运维工程师", "市场推广", "客服专员"]
        jobs = self._jobs(titles, cities=["广州", "深圳", "东莞"])
        a = cg.analyze("某某科技", jobs)
        self.assertEqual(a["level"], "suspected")
        self.assertGreaterEqual(len(a["signals"]), 2)

    def test_too_few_jobs_no_signal(self):
        """样本太少（<8 岗）不做统计判断，避免 2 个岗位就误判。"""
        jobs = self._jobs(["A", "B", "C", "D", "E"], cities=["广州", "深圳", "东莞"])
        self.assertEqual(cg.analyze("小公司", jobs)["level"], "ok")


class TestFilterIntegration(unittest.TestCase):
    JOB = {"company": "中软国际", "title": "AI应用开发工程师"}

    def test_default_does_not_block(self):
        """默认放行 —— 外包岗不都是坏选择，不能一票否决。"""
        self.assertIsNone(F.check_company(self.JOB, {}))

    def test_explicit_switch_blocks(self):
        reason = F.check_company(self.JOB, {"company_filter_enabled": True})
        self.assertIsNotNone(reason)
        self.assertIn("外包", reason)

    def test_switch_passes_normal_company(self):
        job = {"company": "字节跳动"}
        self.assertIsNone(F.check_company(job, {"company_filter_enabled": True}))

    def test_check_job_chain_still_passes_by_default(self):
        ok, reason = F.check_job(self.JOB, {})
        self.assertTrue(ok, f"默认不该拦，实际被拦：{reason}")


if __name__ == "__main__":
    unittest.main()
