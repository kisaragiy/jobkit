"""回复草稿：认得准、缺事实不瞎编、不把系统消息当问题。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import reply_draft as RD

FULL = {"city": "深圳", "current_status": "离职", "available": "随时",
        "salary_target": "8-9K", "onsite_availability": "周三下午可到场"}


class DetectTest(unittest.TestCase):
    def test_three_questions_in_one_message(self):
        """真库原话：在深圳吗 可以现场面试吗 离职了吗"""
        hits = [h[0] for h in RD.detect("在深圳吗 可以现场面试吗 离职了吗")]
        self.assertIn("city", hits)
        self.assertIn("onsite", hits)
        self.assertIn("status", hits)

    def test_city_variants_collapse(self):
        hits = [h[0] for h in RD.detect("你在深圳吗，还是广州吗")]
        self.assertEqual(len([h for h in hits if h.startswith("city")]), 1,
                         "两个城市问法是同一件事，别问两遍")

    def test_outsource_is_detected(self):
        msg = ("岗位是外派到太平财险，学信网可查大专以上学历，地址：深圳粤海城T2 8楼，"
               "现场面试，内网开发，请问考虑吗？")
        hits = [h[0] for h in RD.detect(msg)]
        self.assertIn("outsource", hits)
        self.assertIn("onsite", hits)
        self.assertNotIn("consider", hits, "外派已覆盖「考虑吗」，再说一句「我有兴趣」会自相矛盾")


class NoFabricationTest(unittest.TestCase):
    """最重要的一组：不知道就不说。"""

    def test_missing_facts_returns_needs_not_text(self):
        res = RD.draft("在深圳吗 可以现场面试吗 离职了吗", {})
        self.assertEqual(res["text"], "", "不知道住哪就绝不能编「我在深圳」")
        self.assertIn("city", res["needs"])
        self.assertIn("current_status", res["needs"])

    def test_partial_profile_still_blocks(self):
        res = RD.draft("在深圳吗 离职了吗", {"city": "深圳"})
        self.assertEqual(res["text"], "")
        self.assertEqual(res["needs"], ["current_status"])

    def test_full_profile_produces_text(self):
        res = RD.draft("在深圳吗 可以现场面试吗 离职了吗", FULL)
        self.assertIn("深圳", res["text"])
        self.assertIn("现场面试", res["text"])
        self.assertIn("离职", res["text"])
        self.assertEqual(res["needs"], [])

    def test_salary_needs_target(self):
        res = RD.draft("方便说下期望薪资吗", {})
        self.assertEqual(res["text"], "")
        self.assertIn("salary_target", res["needs"])

    def test_salary_with_target(self):
        res = RD.draft("期望薪资多少", FULL)
        self.assertIn("8-9K", res["text"])

    def test_onsite_requires_explicit_availability(self):
        res = RD.draft("能来现场面试吗", {})
        self.assertEqual(res["text"], "")
        self.assertIn("onsite_availability", res["needs"])

    def test_employment_status_does_not_invent_notice_period(self):
        for status in ("在职", "应届", "离职"):
            text = RD.draft("目前状态", {"current_status": status})["text"]
            self.assertNotIn("随时", text)
            self.assertNotIn("一到两周", text)
            self.assertNotIn("灵活", text)


class SkipTest(unittest.TestCase):
    def test_my_own_rejection_is_skipped(self):
        res = RD.draft("对不起，我觉得该职位不太适合我，祝您早日找到满意的人选", FULL)
        self.assertTrue(res["skip"])

    def test_system_message_is_skipped(self):
        res = RD.draft("对方已同意，您的附件简历已发送给对方", FULL)
        self.assertTrue(res["skip"])

    def test_boss_rejection_is_skipped(self):
        """BOSS 标准拒信不是提问，别给它起草回复。"""
        res = RD.draft("感谢您的关注，很遗憾不能与您共事。祝您在BOSS直聘上找到更匹配的工作机会", FULL)
        self.assertTrue(res["skip"])
        self.assertIn("HR 已婉拒", res["reason"], "别把 HR 拒我说成我自己婉拒")

    def test_attachment_filename_is_skipped(self):
        res = RD.draft("张三-AI应用开发.pdf", FULL)
        self.assertTrue(res["skip"])

    def test_empty_message_is_skipped(self):
        self.assertTrue(RD.draft("", FULL)["skip"])


class UnknownTest(unittest.TestCase):
    def test_unrecognized_is_not_fabricated(self):
        res = RD.draft("我们这边是双休，弹性打卡，你之前做过什么项目", FULL)
        self.assertEqual(res["text"], "")
        self.assertFalse(res["skip"])
        self.assertIn('project_summary', res['needs'])


class OutsourceTest(unittest.TestCase):
    def test_outsource_asks_limits_instead_of_agreeing(self):
        """外派/驻场不能直接答「可以」—— 地点和周期不知道就先问清。"""
        res = RD.draft("岗位是外派到太平财险，现场面试，请问考虑吗", FULL)
        self.assertIn("驻场", res["text"])
        self.assertIn("确认", res["text"])


class ProfileIoTest(unittest.TestCase):
    def test_init_then_load(self):
        d = Path(tempfile.mkdtemp())
        p = d / "reply_profile.json"
        RD.save_profile({**RD.DEFAULT_PROFILE, "city": "深圳"}, p)
        loaded = RD.load_profile(p)
        self.assertEqual(loaded["city"], "深圳")
        self.assertIn("current_status", loaded, "缺的字段要回落默认空值")

    def test_missing_file_gives_empty_profile(self):
        p = Path(tempfile.mkdtemp()) / "nope.json"
        self.assertEqual(RD.load_profile(p), RD.DEFAULT_PROFILE)

    def test_broken_file_does_not_crash(self):
        p = Path(tempfile.mkdtemp()) / "bad.json"
        p.write_text("{不是 json", encoding="utf-8")
        self.assertEqual(RD.load_profile(p), RD.DEFAULT_PROFILE)


if __name__ == "__main__":
    unittest.main()
