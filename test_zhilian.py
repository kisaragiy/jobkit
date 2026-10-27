#!/usr/bin/env python3
"""智联 DOM 通道的单测（不联网、不启动浏览器）。

重点覆盖两类最容易出错的逻辑：
  1. classify_page 的**判定顺序**——「取不到」不能变成「没岗」；
  2. normalize_job 的字段契约——智联没有的东西必须留空，不能编。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import zhilian_channel as ZC          # noqa: E402
from dom import zhilian_selectors as Z  # noqa: E402


def snap(**kw) -> dict:
    """一份「正常经典列表页」的基线快照。"""
    base = {
        "entry": "list",
        "platform": "zhilian",
        "url": "https://www.zhaopin.com/sou/1",
        "ready": True,
        "hasBody": True,
        "login": {"loggedOutCta": "", "headerFound": True, "loginUrl": False},
        "restricted": {"hit": ""},
        "layout": "classic",
        "listFound": True,
        "cardCount": 2,
        "count": 2,
        "dropped": 0,
        "unsupported": "",
        "jobs": [],
    }
    base.update(kw)
    return base


class ClassifyTest(unittest.TestCase):
    def test_normal_page_is_ok(self):
        self.assertEqual(ZC.classify_page(snap()), ZC.REASON_OK)

    def test_login_wall_by_url(self):
        s = snap(login={"loggedOutCta": "", "headerFound": False, "loginUrl": True})
        self.assertEqual(ZC.classify_page(s), ZC.REASON_NOT_LOGGED_IN)

    def test_login_wall_by_cta(self):
        s = snap(login={"loggedOutCta": "登录/注册", "headerFound": False, "loginUrl": False})
        self.assertEqual(ZC.classify_page(s), ZC.REASON_NOT_LOGGED_IN)

    def test_unlogged_but_state_data_is_ok(self):
        """真机实测：未登录时 state.positionList 依然有完整数据（含 JD/真实薪资）。

        未登录只影响「投递」，不影响「发现」——把能拿的数据丢掉才是错的。
        """
        s = snap(
            login={"loggedOutCta": "登录/注册", "headerFound": False, "loginUrl": False},
            source="state",
            jobs=[{"job_id": "a", "title": "b"}],
        )
        self.assertEqual(ZC.classify_page(s), ZC.REASON_OK)

    def test_restricted_still_beats_state_data(self):
        """风控页即使带着数据也必须判 restricted。"""
        s = snap(restricted={"hit": "widget"}, source="state", jobs=[{"job_id": "a"}])
        self.assertEqual(ZC.classify_page(s), ZC.REASON_RESTRICTED)

    def test_restricted_beats_everything_except_login(self):
        s = snap(restricted={"hit": "widget,text"})
        self.assertEqual(ZC.classify_page(s), ZC.REASON_RESTRICTED)

    def test_restricted_takes_priority_over_login(self):
        """风控页通常同时是未登录态，但判 restricted 更有信息量：
        「被风控」能让人去处理，「去登录」会让人白试一次。"""
        s = snap(login={"loggedOutCta": "登录", "headerFound": False, "loginUrl": False},
                 restricted={"hit": "widget"})
        self.assertEqual(ZC.classify_page(s), ZC.REASON_RESTRICTED)

    def test_unknown_layout_is_selector_miss_not_empty(self):
        """最关键的断言：布局不认识 ≠ 今天没岗。"""
        s = snap(layout="", listFound=False, cardCount=0, count=0, unsupported="no-known-layout")
        self.assertEqual(ZC.classify_page(s), ZC.REASON_SELECTOR_MISS)
        self.assertNotEqual(ZC.classify_page(s), ZC.REASON_EMPTY)

    def test_split_layout_is_selector_miss(self):
        s = snap(layout="split", unsupported="split-layout-fields-unknown", cardCount=10)
        self.assertEqual(ZC.classify_page(s), ZC.REASON_SELECTOR_MISS)

    def test_real_empty_page(self):
        """容器在、卡片 0 张 = 真的没岗。"""
        s = snap(cardCount=0, count=0)
        self.assertEqual(ZC.classify_page(s), ZC.REASON_EMPTY)

    def test_non_dict_is_page_error(self):
        self.assertEqual(ZC.classify_page(None), ZC.REASON_PAGE_ERROR)
        self.assertEqual(ZC.classify_page("x"), ZC.REASON_PAGE_ERROR)


class NormalizeTest(unittest.TestCase):
    def test_full_card(self):
        job = ZC.normalize_job({
            "job_id": "CC120000010J00123456789",
            "title": "Python 后端工程师",
            "company": "某某科技",
            "salary": "10-15K",
            "city": "广州",
            "district": "天河区",
            "experience": "1-3年",
            "education": "本科",
            "url": "https://www.zhaopin.com/jobdetail/CC120000010J00123456789.htm",
        })
        self.assertIsNotNone(job)
        self.assertEqual(job["job_id"], "CC120000010J00123456789")
        self.assertEqual(job["city"], "广州")
        self.assertEqual(job["platform"], "zhilian")

    def test_security_id_is_always_empty(self):
        job = ZC.normalize_job({"job_id": "a", "title": "b"})
        self.assertEqual(job["security_id"], "")

    def test_boss_active_not_faked(self):
        """智联卡片里的 companyinfo 不保证是 HR 本人，不许冒充活跃状态。"""
        job = ZC.normalize_job({"job_id": "a", "title": "b", "boss_active": "刚刚活跃"})
        self.assertEqual(job["boss_active"], "")
        self.assertEqual(job["boss_name"], "")

    def test_missing_key_fields_drops(self):
        self.assertIsNone(ZC.normalize_job({"title": "有标题没 id"}))
        self.assertIsNone(ZC.normalize_job({"job_id": "只有id"}))
        self.assertIsNone(ZC.normalize_job(None))
        self.assertIsNone(ZC.normalize_job("字符串不是卡片"))

    def test_whitespace_is_squashed(self):
        job = ZC.normalize_job({"job_id": " a ", "title": " Python   后端 \n"})
        self.assertEqual(job["job_id"], "a")
        self.assertEqual(job["title"], "Python 后端")

    def test_job_description_keeps_newlines(self):
        """JD 的分段是有意义的结构，只去首尾空白，不压成一行。"""
        jd = "岗位职责：\n1. 后端开发\n2. 接口设计\n"
        job = ZC.normalize_job({"job_id": "a", "title": "b", "job_description": jd})
        self.assertEqual(job["job_description"], jd.strip())
        self.assertIn("\n", job["job_description"])


class FetchJobsTest(unittest.TestCase):
    def test_empty_url_is_page_error(self):
        self.assertEqual(ZC.fetch_jobs("")[2], ZC.REASON_PAGE_ERROR)

    def test_foreign_url_is_refused(self):
        """拿 BOSS 的 URL 喂智联通道必须被拒，不能混淆平台。"""
        self.assertEqual(
            ZC.fetch_jobs("https://www.zhipin.com/web/geek/jobs?query=python")[2],
            ZC.REASON_PAGE_ERROR,
        )

    def test_cards_but_all_unmappable_is_selector_miss(self):
        dom = mock.Mock()
        dom._run = mock.Mock(return_value=snap(jobs=[{"garbage": 1}]))
        dom.DomChannelError = Exception
        dom.REASON_EMPTY = ZC.REASON_EMPTY
        with mock.patch.object(ZC, "DC", dom):
            ok, jobs, reason = ZC.fetch_jobs("https://www.zhaopin.com/sou/1")
        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertEqual(reason, ZC.REASON_SELECTOR_MISS)

    def test_empty_page_returns_ok_with_empty_list(self):
        dom = mock.Mock()
        dom._run = mock.Mock(return_value=snap(cardCount=0, count=0, jobs=[]))
        dom.DomChannelError = Exception
        dom.REASON_EMPTY = ZC.REASON_EMPTY
        with mock.patch.object(ZC, "DC", dom):
            ok, jobs, reason = ZC.fetch_jobs("https://www.zhaopin.com/sou/1")
        self.assertTrue(ok)
        self.assertEqual(jobs, [])
        self.assertEqual(reason, ZC.REASON_EMPTY)

    def test_success_and_limit(self):
        raw = [{"job_id": f"id{i}", "title": f"岗位{i}"} for i in range(5)]
        dom = mock.Mock()
        dom._run = mock.Mock(return_value=snap(cardCount=5, count=5, jobs=raw))
        dom.DomChannelError = Exception
        with mock.patch.object(ZC, "DC", dom):
            ok, jobs, reason = ZC.fetch_jobs("https://www.zhaopin.com/sou/1", limit=3)
        self.assertTrue(ok)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(reason, ZC.REASON_OK)


class ScriptRenderTest(unittest.TestCase):
    def test_placeholders_are_replaced(self):
        js = Z.extract_js("list")
        self.assertNotIn("__JOBKIT_SELECTORS__", js)
        self.assertNotIn("__JOBKIT_ENTRY__", js)
        self.assertIn("joblist-box__item", js)     # 经典列表卡片
        self.assertIn("tcaptcha", js)  # 验证码特征（page-challenge.js:46-47）

    def test_bad_entry_raises(self):
        with self.assertRaises(ValueError):
            Z.extract_js("login")

    def test_url_helpers(self):
        self.assertTrue(Z.is_zhilian_url("https://www.zhaopin.com/sou/1"))
        self.assertFalse(Z.is_zhilian_url("https://www.zhipin.com/x"))
        self.assertTrue(Z.is_detail_url("https://www.zhaopin.com/jobdetail/CC123.htm"))
        self.assertEqual(
            Z.job_id_from_url("https://www.zhaopin.com/jobdetail/CC123.htm?x=1"), "CC123"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
