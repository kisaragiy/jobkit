#!/usr/bin/env python3
"""dom_channel / boss_selectors / boss_extract.js 的离线单测。

不依赖浏览器、不联网、不读 jobkit.db：
  - JS 只做「生成结果的字符串断言」（占位符替换干净、关键选择器都在、内嵌 JSON 可解析）；
  - 归一与判定全部用假快照喂纯函数；
  - fetch_jobs / fetch_detail 用 mock 顶掉 _run()，验返回契约与 reason。

跑法：在 jobkit-wt-dom 根目录  python -m unittest test_dom_channel -v
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dom_channel as D            # noqa: E402
from dom import boss_selectors as S  # noqa: E402


def _snap(**kw) -> dict:
    """列表页快照基线：已登录、有容器、两张卡。"""
    base = {
        "entry": "list",
        "url": "https://www.zhipin.com/web/geek/jobs?query=python&city=101280100",
        "ready": True,
        "hasBody": True,
        "login": {"loggedOutCta": "", "headerFound": True, "bodyHint": "", "loginUrl": False},
        "restricted": {"hit": ""},
        "listFound": True,
        "cardCount": 2,
        "jobs": [],
    }
    base.update(kw)
    return base


def _detail_snap(**kw) -> dict:
    base = {
        "entry": "detail",
        "url": "https://www.zhipin.com/job_detail/abc123.html?securityId=sec-9",
        "ready": True,
        "hasBody": True,
        "login": {"loggedOutCta": "", "headerFound": True, "bodyHint": "", "loginUrl": False},
        "restricted": {"hit": ""},
        "title": "Python 后端工程师",
        "salary": "10-15K",
        "company": "某某科技…",
        "gsCompany": "广州某某科技有限公司",
        "tags": ["1-3年", "本科", "FastAPI"],
        "bossActive": "刚刚活跃",
        "bossName": "李女士",
        "bossTitle": "招聘者",
        "jobDescription": "负责后端接口开发\n熟悉 Python / FastAPI\n双休",
        "introduce": "一家做 AI 应用的公司",
        "detailFound": True,
    }
    base.update(kw)
    return base


def _card(**kw) -> dict:
    """模拟 boss_extract.js 返回的单张卡片。"""
    base = {
        "encryptId": "abc123",
        "securityId": "",
        "jobUrl": "https://www.zhipin.com/job_detail/abc123.html",
        "title": "Python 后端工程师",
        "salary": "10-15K",
        "company": "某某科技…",
        "companyTitle": "",
        "companyBrand": "",
        "location": "广州·天河区",
        "tags": ["1-3年", "本科", "FastAPI", "双休"],
        "experience": "",
        "degree": "",
        "bossActive": "刚刚活跃",
        "companyScale": "100-499人",
        "skills": [],
        "welfare": ["双休"],
        "cardText": "Python 后端工程师 10-15K 广州·天河区 1-3年 本科 某某科技…",
    }
    base.update(kw)
    return base


# ══════════════════════════════════════════════════════════════
class TestSelectorsAndJs(unittest.TestCase):
    def test_selectors_cover_required_fields(self):
        need = [
            "JOB_LIST", "JOB_CARD", "JOB_TITLE", "JOB_COMPANY", "JOB_SALARY",
            "JOB_LOCATION", "JOB_TAG_LIST", "JOB_BOSS_ACTIVE", "JOB_BOSS_ONLINE",
            "HEADER", "LOGGED_OUT_CTA", "DETAIL_TITLE", "DETAIL_COMPANY",
        ]
        for k in need:
            self.assertIn(k, S.SELECTORS, f"缺字段选择器 {k}")
            self.assertTrue(S.SELECTORS[k].strip(), f"{k} 是空的")

    def test_extract_js_is_string_with_key_selectors(self):
        js = S.extract_js("list")
        self.assertIsInstance(js, str)
        self.assertGreater(len(js), 3000)
        for frag in (
            ".job-list-box",
            ".job-card-wrapper",
            "a.job-name",
            ".job-salary",
            ".company-location",
            ".tag-list li",
            ".boss-active-time",
            ".boss-online-tag",
            ".header-login-btn",
        ):
            self.assertIn(frag, js, f"注入脚本里没有关键选择器 {frag}")
        self.assertNotIn("__JOBKIT_SELECTORS__", js)
        self.assertNotIn("__JOBKIT_TEXTS__", js)
        self.assertNotIn("__JOBKIT_ENTRY__", js)

    def test_extract_js_declares_both_entries(self):
        js = S.extract_js("list")
        self.assertIn("__jobkitExtractList", js)
        self.assertIn("__jobkitExtractDetail", js)

    def test_entry_injection_differs(self):
        a = S.extract_js("list")
        b = S.extract_js("detail")
        self.assertIn('var ENTRY = "list";', a)
        self.assertIn('var ENTRY = "detail";', b)

    def test_bad_entry_rejected(self):
        with self.assertRaises(ValueError):
            S.extract_js("bogus")

    def test_injected_json_is_parseable(self):
        js = S.extract_js("list")
        m = re.search(r"var S = (\{.*?\});", js, re.S)
        self.assertIsNotNone(m, "找不到注入的选择器对象")
        self.assertEqual(json.loads(m.group(1)), S.SELECTORS)
        m2 = re.search(r"var T = (\{.*?\});", js, re.S)
        self.assertIsNotNone(m2)
        self.assertEqual(json.loads(m2.group(1))["sectionJd"], "职位描述")

    def test_js_keeps_empty_vs_miss_signals(self):
        """两个判据必须是分开的字段，否则 down 层没法区分改版与真没岗。"""
        js = S.extract_js("list")
        self.assertIn("listFound", js)
        self.assertIn("cardCount", js)
        self.assertIn("emptyMarker", js)


# ══════════════════════════════════════════════════════════════
class TestNormalizeListJob(unittest.TestCase):
    def test_full_card(self):
        job = D.normalize_list_job(_card())
        self.assertIsNotNone(job)
        self.assertEqual(job["job_id"], "abc123")
        self.assertEqual(job["title"], "Python 后端工程师")
        self.assertEqual(job["company"], "某某科技…")
        self.assertEqual(job["salary"], "10-15K")
        self.assertEqual(job["city"], "广州")
        self.assertEqual(job["district"], "天河区")
        self.assertEqual(job["experience"], "1-3年")
        self.assertEqual(job["education"], "本科")
        self.assertIn("FastAPI", job["skills"])
        self.assertNotIn("双休", job["skills"])   # 双休是福利，不是技能
        self.assertEqual(job["boss_active"], "刚刚活跃")
        self.assertEqual(job["company_scale"], "100-499人")
        self.assertEqual(job["job_url"], "https://www.zhipin.com/job_detail/abc123.html")

    def test_security_id_from_url(self):
        job = D.normalize_list_job(
            _card(jobUrl="https://www.zhipin.com/job_detail/abc123.html?securityId=se%2B7")
        )
        self.assertEqual(job["security_id"], "se+7")

    def test_id_from_url_when_encrypt_id_missing(self):
        job = D.normalize_list_job(_card(encryptId=""))
        self.assertEqual(job["job_id"], "abc123")

    def test_drop_card_without_id(self):
        self.assertIsNone(D.normalize_list_job(_card(encryptId="", jobUrl="")))

    def test_drop_card_without_title(self):
        self.assertIsNone(D.normalize_list_job(_card(title="")))

    def test_missing_fields_do_not_crash(self):
        job = D.normalize_list_job({"encryptId": "x1", "title": "岗"})
        self.assertEqual(job["job_id"], "x1")
        for k in ("security_id", "company", "salary", "city", "district",
                  "experience", "education", "boss_active", "company_scale"):
            self.assertEqual(job[k], "", f"{k} 应该是空串")
        self.assertEqual(job["skills"], [])

    def test_tolerates_wrong_types(self):
        job = D.normalize_list_job(
            {"encryptId": "x1", "title": "岗", "tags": "1-3年", "skills": "FastAPI", "welfare": 0}
        )
        self.assertEqual(job["job_id"], "x1")
        self.assertIsInstance(job["skills"], list)

    def test_non_dict_returns_none(self):
        self.assertIsNone(D.normalize_list_job(None))
        self.assertIsNone(D.normalize_list_job("x"))
        self.assertIsNone(D.normalize_list_job([]))

    def test_pua_salary_falls_back(self):
        """海外页薪资是私有区字体糊字：要跳过，不能被当薪资写库。"""
        self.assertEqual(D.pick_readable_salary("\ue001\ue002K", "12-18K"), "12-18K")
        self.assertEqual(D.pick_readable_salary("\ue001\ue002K"), "")

    def test_offline_is_dropped(self):
        self.assertEqual(D.sanitize_boss_active("离线"), "")
        self.assertEqual(D.sanitize_boss_active("当前在线"), "当前在线")
        self.assertEqual(D.sanitize_boss_active("3小时前活跃"), "3小时前活跃")
        self.assertEqual(D.sanitize_boss_active("随便写的"), "")
        self.assertEqual(D.normalize_list_job(_card(bossActive="离线"))["boss_active"], "")

    def test_company_prefers_full_name(self):
        job = D.normalize_list_job(_card(company="某某科技…", companyTitle="某某科技有限公司"))
        self.assertEqual(job["company"], "某某科技有限公司")
        # 都截断时取长的
        self.assertEqual(D.prefer_company_name("甲…", "甲乙丙…"), "甲乙丙…")

    def test_skills_from_vue_dedupe(self):
        job = D.normalize_list_job(_card(skills=["FastAPI", "FastAPI", "Python"]))
        self.assertEqual(job["skills"].count("FastAPI"), 1)
        self.assertIn("Python", job["skills"])

    def test_location_without_separator(self):
        self.assertEqual(D.split_city_district("广州"), ("广州", ""))
        self.assertEqual(D.split_city_district(""), ("", ""))


# ══════════════════════════════════════════════════════════════
class TestClassifyPage(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(D.classify_page(_snap()), D.REASON_OK)

    def test_empty_is_not_miss(self):
        """有容器但 0 张卡 = 真空页（ok=True），绝不等于选择器失效。"""
        self.assertEqual(D.classify_page(_snap(cardCount=0)), D.REASON_EMPTY)

    def test_selector_miss_when_no_container(self):
        self.assertEqual(
            D.classify_page(_snap(listFound=False, cardCount=0)),
            D.REASON_SELECTOR_MISS,
        )

    def test_restricted_by_text(self):
        self.assertEqual(
            D.classify_page(_snap(restricted={"hit": "操作过于频繁"})),
            D.REASON_RESTRICTED,
        )

    def test_restricted_by_security_url(self):
        snap = _snap(url="https://www.zhipin.com/web/passport/zp/security.html")
        self.assertEqual(D.classify_page(snap), D.REASON_RESTRICTED)

    def test_restricted_beats_login(self):
        """受限页常同时带登录按钮：必须先报 restricted，别让人误以为重新登录就行。"""
        snap = _snap(
            restricted={"hit": "访问受限"},
            login={"loggedOutCta": "a.header-login-btn", "headerFound": True},
        )
        self.assertEqual(D.classify_page(snap), D.REASON_RESTRICTED)

    def test_not_logged_in_by_url(self):
        snap = _snap(url="https://www.zhipin.com/web/user/?ka=header-login")
        self.assertEqual(D.classify_page(snap), D.REASON_NOT_LOGGED_IN)

    def test_not_logged_in_by_cta(self):
        snap = _snap(login={"loggedOutCta": "a.header-login-btn", "headerFound": True})
        self.assertEqual(D.classify_page(snap), D.REASON_NOT_LOGGED_IN)

    def test_not_logged_in_by_body_hint(self):
        snap = _snap(login={"loggedOutCta": "", "bodyHint": "登录后可查看"})
        self.assertEqual(D.classify_page(snap), D.REASON_NOT_LOGGED_IN)

    def test_page_error_when_not_ready(self):
        self.assertEqual(D.classify_page(_snap(ready=False)), D.REASON_PAGE_ERROR)
        self.assertEqual(D.classify_page(_snap(hasBody=False)), D.REASON_PAGE_ERROR)

    def test_page_error_on_js_exception(self):
        self.assertEqual(D.classify_page({"error": "boom"}), D.REASON_PAGE_ERROR)

    def test_page_error_on_garbage(self):
        self.assertEqual(D.classify_page(None), D.REASON_PAGE_ERROR)
        self.assertEqual(D.classify_page("x"), D.REASON_PAGE_ERROR)

    def test_detail_ok_and_miss(self):
        self.assertEqual(D.classify_page(_detail_snap(), entry="detail"), D.REASON_OK)
        self.assertEqual(
            D.classify_page(_detail_snap(detailFound=False), entry="detail"),
            D.REASON_SELECTOR_MISS,
        )


# ══════════════════════════════════════════════════════════════
class TestFetchJobsContract(unittest.TestCase):
    def test_build_list_url(self):
        self.assertEqual(
            D.build_list_url("python", "广州"),
            "https://www.zhipin.com/web/geek/jobs?query=python&city=101280100",
        )
        self.assertIn("city=101280100", D.build_list_url("python", "101280100"))  # 数字码直通
        self.assertEqual(D.build_list_url("python", "不存在的城市"), "")

    def test_unknown_city_fails_without_touching_browser(self):
        with mock.patch.object(D, "_run", side_effect=AssertionError("不该连 CDP")):
            ok, jobs, reason = D.fetch_jobs("python", "火星")
        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertEqual(reason, D.REASON_PAGE_ERROR)

    def test_success_limits(self):
        snap = _snap(jobs=[_card(encryptId="a1", jobUrl=""), _card(encryptId="a2", jobUrl="")])
        with mock.patch.object(D, "_run", return_value=snap):
            ok, jobs, reason = D.fetch_jobs("python", "广州", limit=1)
        self.assertTrue(ok)
        self.assertEqual(reason, D.REASON_OK)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "a1")

    def test_empty_page_returns_ok_with_empty_list(self):
        with mock.patch.object(D, "_run", return_value=_snap(cardCount=0, jobs=[])):
            ok, jobs, reason = D.fetch_jobs("python", "广州")
        self.assertTrue(ok)
        self.assertEqual(jobs, [])
        self.assertEqual(reason, D.REASON_EMPTY)

    def test_selector_miss_is_not_empty(self):
        with mock.patch.object(D, "_run", return_value=_snap(listFound=False, cardCount=0)):
            ok, jobs, reason = D.fetch_jobs("python", "广州")
        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertEqual(reason, D.REASON_SELECTOR_MISS)

    def test_cards_present_but_all_unmappable_is_miss(self):
        bad = [{"encryptId": "", "title": ""}, {"title": "只有标题"}]  # 无 id
        with mock.patch.object(D, "_run", return_value=_snap(cardCount=2, jobs=bad)):
            ok, jobs, reason = D.fetch_jobs("python", "广州")
        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertEqual(reason, D.REASON_SELECTOR_MISS)

    def test_restricted_propagates(self):
        with mock.patch.object(D, "_run", return_value=_snap(restricted={"hit": "账号异常"})):
            ok, jobs, reason = D.fetch_jobs("python", "广州")
        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertEqual(reason, D.REASON_RESTRICTED)

    def test_not_logged_in_propagates(self):
        snap = _snap(login={"loggedOutCta": "a.header-login-btn", "headerFound": True})
        with mock.patch.object(D, "_run", return_value=snap):
            ok, jobs, reason = D.fetch_jobs("python", "广州")
        self.assertFalse(ok)
        self.assertEqual(reason, D.REASON_NOT_LOGGED_IN)

    def test_page_error_propagates(self):
        with mock.patch.object(D, "_run", side_effect=D.DomChannelError(D.REASON_PAGE_ERROR, "CDP 不可用")):
            ok, jobs, reason = D.fetch_jobs("python", "广州")
        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertEqual(reason, D.REASON_PAGE_ERROR)

    def test_limit_zero_is_ok_but_empty_list(self):
        with mock.patch.object(D, "_run", return_value=_snap(jobs=[_card()])):
            ok, jobs, reason = D.fetch_jobs("python", "广州", limit=0)
        self.assertTrue(ok)
        self.assertEqual(jobs, [])
        self.assertEqual(reason, D.REASON_OK)


# ══════════════════════════════════════════════════════════════
class TestFetchDetailContract(unittest.TestCase):
    def test_success(self):
        with mock.patch.object(D, "_run", return_value=_detail_snap()):
            ok, job, reason = D.fetch_detail("https://www.zhipin.com/job_detail/abc123.html")
        self.assertTrue(ok)
        self.assertEqual(reason, D.REASON_OK)
        self.assertEqual(job["job_id"], "abc123")
        self.assertEqual(job["security_id"], "sec-9")
        self.assertEqual(job["company"], "广州某某科技有限公司")   # 工商全称优先
        self.assertEqual(job["company_full_name"], "广州某某科技有限公司")
        self.assertEqual(job["experience"], "1-3年")
        self.assertEqual(job["education"], "本科")
        self.assertIn("FastAPI", job["skills"])
        self.assertIn("双休", job["job_description"])             # JD 保留换行不崩
        self.assertIn("AI 应用", job["introduce"])

    def test_empty_url(self):
        ok, job, reason = D.fetch_detail("")
        self.assertFalse(ok)
        self.assertIsNone(job)
        self.assertEqual(reason, D.REASON_PAGE_ERROR)

    def test_detail_selector_miss(self):
        with mock.patch.object(D, "_run", return_value=_detail_snap(
            title="", company="", gsCompany="", jobDescription="", detailFound=False
        )):
            ok, job, reason = D.fetch_detail("https://www.zhipin.com/job_detail/abc123.html")
        self.assertFalse(ok)
        self.assertIsNone(job)
        self.assertEqual(reason, D.REASON_SELECTOR_MISS)

    def test_detail_without_job_id(self):
        with mock.patch.object(D, "_run", return_value=_detail_snap(
            url="https://www.zhipin.com/web/geek/jobs"
        )):
            ok, job, reason = D.fetch_detail("https://www.zhipin.com/web/geek/jobs")
        self.assertFalse(ok)
        self.assertIsNone(job)
        self.assertEqual(reason, D.REASON_SELECTOR_MISS)

    def test_detail_login_wall(self):
        snap = _detail_snap(url="https://www.zhipin.com/web/user/?ka=header-login")
        with mock.patch.object(D, "_run", return_value=snap):
            ok, job, reason = D.fetch_detail("https://www.zhipin.com/web/user/?ka=header-login")
        self.assertFalse(ok)
        self.assertEqual(reason, D.REASON_NOT_LOGGED_IN)


# ── 桩 DOM：真模板 + 真 DOM 结构 ──────────────────────────────────
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "需要 node 才能跑桩 DOM")
class DetailStubDomTest(unittest.TestCase):
    """执行 extract_js('detail') 渲染出的真模板，喂手工构造的 DOM。

    验的是「切段算法」，不是「选择器字符串还写在文件里」：
      · JD 只能取「职位描述」那一块，不能把公司介绍/相似职位/工作地址扫进来；
      · JD 与公司介绍必须各归各位；
      · 全页无锚点时必须给明确失败原因，不许拿空串冒充成功。
    """

    @classmethod
    def setUpClass(cls):
        cls.rendered = S.extract_js("detail")
        cls.harness = HERE / "dom" / "detail_stub_harness.js"

    def _run(self, case: str) -> dict:
        proc = subprocess.run(
            [NODE, str(self.harness), case],
            input=self.rendered, capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, f"harness 崩了: {proc.stderr}")
        return json.loads(proc.stdout)

    def test_jd_is_cut_at_next_heading(self):
        d = self._run("jd-section")
        self.assertIn("交易系统后端接口", d["jobDescription"])
        # 兄弟块（公司介绍 / 相似职位）绝不能混进 JD
        self.assertNotIn("跨境电商", d["jobDescription"])
        self.assertNotIn("游戏策划", d["jobDescription"])
        self.assertEqual(d["jdFrom"], "heading-sibling")
        self.assertIn("跨境电商", d["introduce"])

    def test_intro_and_address_do_not_leak_into_jd(self):
        d = self._run("intro-separation")
        self.assertIn("Python 与 Go", d["jobDescription"])
        self.assertNotIn("游戏公司", d["jobDescription"])
        self.assertNotIn("珠江新城", d["jobDescription"])   # 工作地址不能串
        self.assertIn("游戏公司", d["introduce"])

    def test_no_anchor_fails_loudly(self):
        d = self._run("no-selectors")
        self.assertFalse(d["detailFound"])
        self.assertTrue(d["detailFail"].strip(), "失败必须有原因，不能空串")
        self.assertEqual(d["jobDescription"], "")
        self.assertEqual(d["introduce"], "")
        self.assertIn("jobDescription", d["missing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
