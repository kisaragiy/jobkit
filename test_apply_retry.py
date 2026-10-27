"""写操作结果未知时禁止重发，并保留可核对的续跑提示。"""
from __future__ import annotations

import unittest

import apply_pipeline as AP


class TransientTest(unittest.TestCase):
    def test_network_error_is_transient(self):
        self.assertTrue(AP.is_transient("[NETWORK_ERROR] HTTP 502"))
        self.assertTrue(AP.is_transient("[TIMEOUT] 150s"))

    def test_auth_and_param_are_not_transient(self):
        self.assertFalse(AP.is_transient("[AUTH_EXPIRED] 登录已过期"))
        self.assertFalse(AP.is_transient("[INVALID_PARAM] No such option: -k"))
        self.assertFalse(AP.is_transient(""))


class SendWithRetryTest(unittest.TestCase):
    def setUp(self):
        self.naps = []

    def _sender(self, results):
        calls = []

        def fn(sid, jid, msg):
            calls.append((sid, jid))
            return results[len(calls) - 1]
        fn.calls = calls
        return fn

    def _sleep(self, s):
        self.naps.append(s)

    def test_success_no_retry(self):
        s = self._sender([(True, "")])
        ok, err, n = AP.send_with_retry("s", "j", "m", sender=s,
                                        sleep=self._sleep, log=lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(n, 1)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(self.naps, [])

    def test_network_error_is_unknown_and_not_retried(self):
        s = self._sender([(False, "[NETWORK_ERROR] HTTP 502"), (True, "")])
        ok, err, n = AP.send_with_retry("s", "j", "m", sender=s,
                                        sleep=self._sleep, log=lambda *_: None)
        self.assertFalse(ok)
        self.assertIn("OUTCOME_UNKNOWN", err)
        self.assertEqual(n, 1)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(self.naps, [])

    def test_timeout_never_replays_a_possibly_accepted_write(self):
        s = self._sender([(False, "[NETWORK_ERROR] 502"), (False, "[NETWORK_ERROR] 502")])
        ok, err, n = AP.send_with_retry("s", "j", "m", sender=s,
                                        sleep=self._sleep, log=lambda *_: None)
        self.assertFalse(ok)
        self.assertEqual(n, 1)
        self.assertEqual(len(s.calls), 1, "不允许重试未知结果")

    def test_sender_exception_has_unknown_outcome(self):
        def sender(*args):
            raise TimeoutError("synthetic")
        ok, err, n = AP.send_with_retry("s", "j", "m", sender=sender)
        self.assertFalse(ok)
        self.assertIn("OUTCOME_UNKNOWN", err)
        self.assertTrue(AP.should_break(err))
        self.assertEqual(n, 1)

    def test_auth_expired_never_retried(self):
        s = self._sender([(False, "[AUTH_EXPIRED] 登录已过期")])
        ok, err, n = AP.send_with_retry("s", "j", "m", sender=s,
                                        sleep=self._sleep, log=lambda *_: None)
        self.assertFalse(ok)
        self.assertEqual(n, 1)
        self.assertEqual(self.naps, [])

    def test_invalid_param_not_retried(self):
        """参数错了重试还是错 —— 那不叫抖动。"""
        s = self._sender([(False, "[INVALID_PARAM] No such option: -k")])
        ok, err, n = AP.send_with_retry("s", "j", "m", sender=s,
                                        sleep=self._sleep, log=lambda *_: None)
        self.assertEqual(n, 1)


class UnusableSecurityIdTest(unittest.TestCase):
    def test_dash_prefix_rejected(self):
        """'-' 开头会被 boss CLI 当命令行选项解析 → No such option: -k。"""
        self.assertTrue(AP.unusable_security_id("-kurpN4f0DFlu-X1M5wbvlRe9rPgq0jG6oNt"))

    def test_placeholder_prefix_rejected(self):
        """cpc_job_list_chat_ 是聊天页采集的占位符，不是真 ID。"""
        self.assertTrue(AP.unusable_security_id("cpc_job_list_chat_31de78e18cfb"))

    def test_normal_id_accepted(self):
        for sid in ("nuus5b_GvfNHz-L1WLps-O0Yum3aHppdEiE",
                    "8Byy5Vc1QGQYV-Q1qR9oSe2jveZGlB"):
            self.assertFalse(AP.unusable_security_id(sid), sid)

    def test_empty_is_unusable(self):
        """空值也算不可用，但调用方要单独统计「缺字段」（那是采集短板，不是脏数据）。"""
        self.assertTrue(AP.unusable_security_id(""))
        self.assertTrue(AP.unusable_security_id(None))

    def test_leading_space_not_confused(self):
        self.assertTrue(AP.unusable_security_id("  -weird"))


class BossPlatformTest(unittest.TestCase):
    def test_null_platform_is_boss(self):
        """历史数据没有 platform 字段，那些是 BOSS 的，不能排除。"""
        self.assertTrue(AP.is_boss_platform(None))
        self.assertTrue(AP.is_boss_platform(""))

    def test_boss_spellings(self):
        for p in ("boss", "BOSS", " Boss "):
            self.assertTrue(AP.is_boss_platform(p), p)

    def test_external_platforms_excluded(self):
        """外部 feed 的岗位没有 security_id，投递通道用不了。"""
        for p in ("51job", "zhilian", "智联", "猎聘", "web"):
            self.assertFalse(AP.is_boss_platform(p), p)

    def test_load_apply_jobs_counts_excluded(self):
        import sqlite3
        import store
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(store.SCHEMA)
        rows = [("b1", "BOSS岗", None), ("b2", "BOSS岗2", "boss"),
                ("e1", "外部1", "51job"), ("e2", "外部2", "智联"), ("e3", "外部3", "猎聘")]
        for jid, title, plat in rows:
            c.execute("INSERT INTO jobs (job_id, title, platform) VALUES (?,?,?)",
                      (jid, title, plat))
        c.commit()
        jobs, n_external = AP.load_apply_jobs(c)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(n_external, 3)
        self.assertEqual({j["job_id"] for j in jobs}, {"b1", "b2"})
        c.close()


class ResumeHintTest(unittest.TestCase):
    def test_reports_remaining(self):
        h = AP.resume_hint(14, 70, 11, 3, "[AUTH_EXPIRED]")
        self.assertIn("还剩 56 条", h)
        self.assertIn("11", h)
        self.assertIn("3", h)

    def test_auth_tells_how_to_relogin(self):
        h = AP.resume_hint(14, 70, 11, 3, "[AUTH_EXPIRED] 登录已过期")
        self.assertIn("boss_login.py", h)
        self.assertIn("参数不用改", h)

    def test_no_remaining_returns_empty(self):
        self.assertEqual(AP.resume_hint(70, 70, 60, 10, ""), "")

    def test_says_no_duplicate(self):
        h = AP.resume_hint(2, 10, 2, 0, "[NETWORK_ERROR]")
        self.assertIn("待核对", h)

    def test_unknown_outcome_does_not_recommend_rerunning(self):
        h = AP.resume_hint(2, 10, 1, 1, "[OUTCOME_UNKNOWN]")
        self.assertIn("核对", h)
        self.assertNotIn("重跑刚才", h)


if __name__ == "__main__":
    unittest.main()
