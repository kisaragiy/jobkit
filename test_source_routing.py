#!/usr/bin/env python3
"""collector 采集通道分发（cli / dom / auto）的契约测试。

不联网、不启浏览器：把 run_search / _dom_search 换成桩，只验分发与降级规则。
重点守住三条：
  ① 默认必须还是 cli —— 不能让 DOM 通道悄悄接管既有行为；
  ② auto 只在「通道不可用」时回落，selector-miss 也算通道问题，不能伪装成今天没岗位；
  ③ restricted（风控）永不回落 —— 换通道继续请求等于绕过平台的负反馈。
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import collector as C  # noqa: E402


OK_JOBS = [{"job_id": "abc", "title": "AI应用开发"}]


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._run_search = C.run_search
        self._dom_search = C._dom_search

        def fake_cli(kw, city, timeout=120):
            self.calls.append("cli")
            return True, OK_JOBS, "cli-raw"

        def fake_dom(kw, city, limit=30):
            self.calls.append("dom")
            return True, OK_JOBS, "DOM ok(1)"

        C.run_search = fake_cli
        C._dom_search = fake_dom

    def tearDown(self):
        C.run_search = self._run_search
        C._dom_search = self._dom_search

    # ── 默认行为 ────────────────────────────────────────────
    def test_default_source_is_cli(self):
        self.assertEqual(C.SOURCE_CLI, "cli")
        self.assertEqual(C.collect.__defaults__[-1], C.SOURCE_CLI)
        self.assertEqual(C._collect_inner.__defaults__[-1], C.SOURCE_CLI)

    def test_search_default_calls_cli_only(self):
        ok, jobs, raw = C.search_jobs("AI", "广州")
        self.assertTrue(ok)
        self.assertEqual(self.calls, ["cli"])

    def test_explicit_dom_never_calls_cli(self):
        ok, _, raw = C.search_jobs("AI", "广州", source=C.SOURCE_DOM)
        self.assertTrue(ok)
        self.assertEqual(self.calls, ["dom"])
        self.assertTrue(raw.startswith("DOM"))

    def test_auto_prefers_dom(self):
        ok, _, _ = C.search_jobs("AI", "广州", source=C.SOURCE_AUTO)
        self.assertTrue(ok)
        self.assertEqual(self.calls, ["dom"])   # DOM 成功就不碰 CLI

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            C.search_jobs("AI", "广州", source="selenium")

    # ── 降级规则 ────────────────────────────────────────────
    def _dom_fails(self, raw):
        C._dom_search = lambda kw, city, limit=30: (False, None, raw)

    def test_auto_falls_back_on_page_error(self):
        self._dom_fails("DOM page-error CDP 连不上")
        ok, _, _ = C.search_jobs("AI", "广州", source=C.SOURCE_AUTO)
        self.assertTrue(ok)
        self.assertEqual(self.calls, ["cli"])

    def test_auto_falls_back_on_not_logged_in(self):
        self._dom_fails("DOM not-logged-in")
        C.search_jobs("AI", "广州", source=C.SOURCE_AUTO)
        self.assertEqual(self.calls, ["cli"])

    def test_auto_falls_back_on_selector_miss(self):
        """选择器失效 = 通道坏了，不能当成「今天没岗位」静默成功。"""
        self._dom_fails("DOM selector-miss")
        C.search_jobs("AI", "广州", source=C.SOURCE_AUTO)
        self.assertEqual(self.calls, ["cli"])

    def test_auto_never_falls_back_on_restricted(self):
        """风控是平台负反馈：换通道继续请求 = 绕过风控。"""
        self._dom_fails("ACCOUNT_RISK DOM restricted")
        ok, jobs, raw = C.search_jobs("AI", "广州", source=C.SOURCE_AUTO)
        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertEqual(self.calls, [])                      # CLI 一次都没调
        self.assertIn("ACCOUNT_RISK", raw)

    def test_dom_mode_reports_restricted_as_risk(self):
        self._dom_fails("ACCOUNT_RISK DOM restricted")
        ok, jobs, raw = C.search_jobs("AI", "广州", source=C.SOURCE_DOM)
        self.assertFalse(ok)
        self.assertEqual(C.classify_failure(raw), "risk")


class DomNormalizeTest(unittest.TestCase):
    def setUp(self):
        # _dom_search 现在会准备浏览器；归一化测试只覆盖假数据，绝不启动真实进程。
        browser = mock.patch.object(C, "_ensure_browser", return_value=(True, "offline stub"))
        browser.start()
        self.addCleanup(browser.stop)

    def test_company_scale_mapped_to_scale(self):

        class FakeDom:
            @staticmethod
            def fetch_jobs(kw, city, limit=30):
                return True, [{"job_id": "x", "company_scale": "500-999人"}], "ok"

        with mock.patch.dict(sys.modules, {"dom_channel": FakeDom}):
            ok, jobs, _ = C._dom_search("AI", "广州")

        self.assertTrue(ok)
        self.assertEqual(jobs[0]["scale"], "500-999人")
        self.assertNotIn("company_scale", jobs[0])
        self.assertEqual(jobs[0]["source_channel"], "dom")

    def test_import_failure_is_channel_error_not_crash(self):
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            if name == "dom_channel":
                raise ImportError("no websocket")
            return real_import(name, *a, **kw)

        builtins.__import__ = boom
        try:
            ok, jobs, raw = C._dom_search("AI", "广州")
        finally:
            builtins.__import__ = real_import

        self.assertFalse(ok)
        self.assertIsNone(jobs)
        self.assertIn("page-error", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
