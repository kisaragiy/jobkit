#!/usr/bin/env python3
"""security_id 详情页回填的单测。

背景：boss-agent-cli 的 greet 签名要求 securityId，但列表页不一定给。
jobkit 的解法是「缺就先进详情页把它补回来」——这是 GetJob 不需要、
所以参考实现里没有、必须自己造的一环。

这里只测回填函数本身（不连浏览器、不联网），验证四件事：
  1. 没有 job_id 时连浏览器都不碰；
  2. 成功时返回干净 token，并且真的去 /job_detail/<id>.html 取；
  3. 详情页失败时返回空串（不猜、不编）；
  4. DOM 层抛异常时也不炸穿调用方。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_pipeline as AP  # noqa: E402


def _fake_dom(returns=None, raises=None):
    mod = mock.Mock()
    if raises is not None:
        mod.fetch_detail.side_effect = raises
    else:
        mod.fetch_detail.return_value = returns
    return mod


class EnrichSecurityIdTest(unittest.TestCase):
    def test_empty_job_id_never_touches_browser(self):
        dom = _fake_dom(returns=(True, {"security_id": "x"}, "ok"))
        with mock.patch.dict(sys.modules, {"dom_channel": dom}):
            self.assertEqual(AP._enrich_security_id(""), "")
            self.assertEqual(AP._enrich_security_id("   "), "")
            self.assertEqual(AP._enrich_security_id(None), "")
        dom.fetch_detail.assert_not_called()

    def test_success_returns_trimmed_token(self):
        dom = _fake_dom(returns=(True, {"security_id": " sec-1 "}, "ok"))
        with mock.patch.dict(sys.modules, {"dom_channel": dom}):
            self.assertEqual(AP._enrich_security_id("abc123"), "sec-1")
        dom.fetch_detail.assert_called_once()
        url = dom.fetch_detail.call_args[0][0]
        self.assertEqual(url, "https://www.zhipin.com/job_detail/abc123.html")

    def test_detail_failure_returns_empty_not_guess(self):
        dom = _fake_dom(returns=(False, None, "selector-miss"))
        with mock.patch.dict(sys.modules, {"dom_channel": dom}):
            self.assertEqual(AP._enrich_security_id("abc123"), "")

    def test_missing_field_in_snapshot_returns_empty(self):
        dom = _fake_dom(returns=(True, {"security_id": ""}, "ok"))
        with mock.patch.dict(sys.modules, {"dom_channel": dom}):
            self.assertEqual(AP._enrich_security_id("abc123"), "")

    def test_dom_exception_does_not_escape(self):
        dom = _fake_dom(raises=RuntimeError("cdp down"))
        with mock.patch.dict(sys.modules, {"dom_channel": dom}):
            self.assertEqual(AP._enrich_security_id("abc123"), "")

    def test_import_failure_returns_empty(self):
        """dom 模块缺失（例如没装/被裁掉）时也必须安全降级。"""
        with mock.patch.dict(sys.modules, {"dom_channel": None}):
            # None 放进 sys.modules 会让 import 抛异常
            self.assertEqual(AP._enrich_security_id("abc123"), "")


class CliSurfaceTest(unittest.TestCase):
    """参数必须真实存在——否则「文档写了、命令跑不起来」。"""

    def test_enrich_flags_are_registered(self):
        import inspect
        src = inspect.getsource(AP.main)
        self.assertIn("--enrich-detail", src)
        self.assertIn("--enrich-detail-limit", src)

    def test_default_is_off(self):
        """默认必须是关的：隐式开浏览器属于越权。"""
        import inspect
        src = inspect.getsource(AP.main)
        self.assertIn('"--enrich-detail", action="store_true"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
