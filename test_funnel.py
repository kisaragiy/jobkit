"""漏斗的完整性判断单测 —— 重点是不能把「数据缺失」粉饰成「转化率低」。"""
from __future__ import annotations

import unittest

import funnel as F


def base(**kw):
    d = {
        "jobs_all": 100, "jobs_with_jd": 90, "passed": 50,
        "applied": 40, "applied_linked": 40, "applied_with_snapshot": 40,
        "convs": 10, "convs_pending": 5, "convs_interview": 1, "convs_rejected": 1,
        "replied_companies": 3, "applied_companies": 10, "overlap": 1,
        "conv_linked": 5,
    }
    d.update(kw)
    return d


class TestIntegrity(unittest.TestCase):
    def test_clean_data_ok(self):
        self.assertTrue(F.integrity(base())["ok"])

    def test_low_link_ratio_warns(self):
        r = F.integrity(base(applied=100, applied_linked=3,
                             applied_with_snapshot=0))
        self.assertFalse(r["ok"])
        self.assertLess(r["linked_ratio"], 0.1)
        self.assertTrue(any("只有 3 条能关联" in w for w in r["warnings"]))

    def test_no_snapshot_warns(self):
        r = F.integrity(base(applied_with_snapshot=0))
        self.assertTrue(any("岗位快照" in w for w in r["warnings"]))

    def test_low_jd_coverage_warns(self):
        r = F.integrity(base(jobs_all=100, jobs_with_jd=10))
        self.assertTrue(any("JD" in w for w in r["warnings"]))

    def test_no_overlap_warns(self):
        r = F.integrity(base(replied_companies=5, applied_companies=10, overlap=0))
        self.assertTrue(any("完全没有交集" in w for w in r["warnings"]))


class TestLayers(unittest.TestCase):
    def test_small_sample_hides_rate(self):
        """投递样本不足时不能给比率 —— 3/5 算出来的 60% 是噪音。"""
        rows = F.layers(base(applied=5, applied_linked=5))
        self.assertTrue(all(r["rate"] is None for r in rows))
        self.assertTrue(any("不看比率" in r["note"] for r in rows))

    def test_enough_sample_shows_rate(self):
        rows = F.layers(base(applied=40))
        mined = {r["name"]: r for r in rows}
        self.assertAlmostEqual(mined["筛选通过"]["rate"], 0.5)
        self.assertAlmostEqual(mined["已投递"]["rate"], 0.8)

    def test_render_no_fake_rate(self):
        text = F.render(base(applied=3, applied_linked=1, applied_with_snapshot=0))
        self.assertIn("⚠", text)
        self.assertIn("不看比率", text)


if __name__ == "__main__":
    unittest.main()
