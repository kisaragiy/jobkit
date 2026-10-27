"""pace.py 的行为测试。

用例对齐 GetJob 2.3.1 的 pace-guard.test.js 关键断言（边界值一致），
外加持久化 / 坏文件降级这两条我们自己的约束。
"""
import json
import os
import tempfile
import unittest

import pace as P


class TestJitter(unittest.TestCase):
    def test_in_range(self):
        for _ in range(200):
            v = P.jitter_seconds((0.3, 0.8))
            self.assertGreaterEqual(v, 0.3)
            self.assertLessEqual(v, 0.8)

    def test_reversed_range_normalized(self):
        v = P.jitter_seconds((6, 1))
        self.assertGreaterEqual(v, 1)
        self.assertLessEqual(v, 6)


class TestFuseBackoff(unittest.TestCase):
    def test_sequence(self):
        pace = P.get_pace()
        self.assertEqual([P.fuse_wait_minutes(n, pace) for n in range(1, 7)],
                         [5, 8, 13, 21, 34, 34])

    def test_zero_clamps_to_first(self):
        self.assertEqual(P.fuse_wait_minutes(0), 5)

    def test_negative_clamps_to_first(self):
        self.assertEqual(P.fuse_wait_minutes(-3), 5)


class TestShouldFuse(unittest.TestCase):
    T0 = 1_700_000_000_000

    def test_click_fuse_inside_window(self):
        v = P.should_fuse(80, 0, self.T0, self.T0 + 14 * 60_000)
        self.assertTrue(v["fuse"])
        self.assertEqual(v["kind"], "clicks")

    def test_click_fuse_outside_window(self):
        v = P.should_fuse(80, 0, self.T0, self.T0 + 16 * 60_000)
        self.assertFalse(v["fuse"])

    def test_below_threshold(self):
        v = P.should_fuse(79, 0, self.T0, self.T0 + 14 * 60_000)
        self.assertFalse(v["fuse"])

    def test_desc_streak_wins(self):
        v = P.should_fuse(80, 50, self.T0, self.T0 + 14 * 60_000)
        self.assertTrue(v["fuse"])
        self.assertEqual(v["kind"], "desc")

    def test_desc_streak_alone(self):
        v = P.should_fuse(3, 50, self.T0, self.T0 + 1000)
        self.assertTrue(v["fuse"])
        self.assertEqual(v["kind"], "desc")

    def test_nothing_without_start(self):
        v = P.should_fuse(999, 0, None, self.T0)
        self.assertFalse(v["fuse"])


class TestTokenBucket(unittest.TestCase):
    T0 = 1_700_000_000_000

    def test_below_limit_no_wait(self):
        # 9 条（< 10）不等待；10 条刚好触顶，见下一个用例
        clicks = [self.T0 + i * 100 for i in range(9)]
        self.assertEqual(P.token_wait_ms(clicks, self.T0 + 2000), 0)

    def test_at_limit_waits_for_oldest_to_age_out(self):
        clicks = [self.T0 + i * 1000 for i in range(10)]
        now = self.T0 + 10_000
        wait = P.token_wait_ms(clicks, now)
        self.assertAlmostEqual(wait, 60_000 - 10_000, delta=1)

    def test_old_clicks_are_pruned(self):
        clicks = [self.T0 - 120_000 + i for i in range(10)]
        self.assertEqual(P.token_wait_ms(clicks, self.T0), 0)


class TestVisitWindow(unittest.TestCase):
    T0 = 1_700_000_000_000
    WINDOW_MS = 60 * 60_000

    def _visits(self, n, kind="filtered", step=0):
        return [{"ts": self.T0 + i * step, "kind": kind} for i in range(n)]

    def test_full_window(self):
        stats = P.window_stats(self._visits(200), self.T0 + self.WINDOW_MS)
        self.assertEqual(stats["total"], 200)
        self.assertEqual(stats["remaining"], 0)
        self.assertEqual(stats["next_at_ms"], self.T0 + self.WINDOW_MS)

    def test_boundary_is_inclusive(self):
        # 闭区间：now - ts == window 仍计入；多 1ms 才开始出窗
        visits = self._visits(200, step=1000)
        stats = P.window_stats(visits, self.T0 + self.WINDOW_MS)
        self.assertEqual(stats["total"], 200)
        stats2 = P.window_stats(visits, self.T0 + self.WINDOW_MS + 1)
        self.assertEqual(stats2["total"], 199)
        self.assertEqual(stats2["remaining"], 1)

    def test_kind_counts(self):
        visits = [
            {"ts": self.T0, "kind": "filtered"},
            {"ts": self.T0 + 1, "kind": "filtered"},
            {"ts": self.T0 + 2, "kind": "delivered"},
            {"ts": self.T0 + 3, "kind": "chatted"},
        ]
        stats = P.window_stats(visits, self.T0 + 4)
        self.assertEqual(stats["counts"]["filtered"], 2)
        self.assertEqual(stats["counts"]["delivered"], 1)
        self.assertEqual(stats["counts"]["chatted"], 1)
        self.assertEqual(stats["total"], 4)

    def test_unknown_kind_counts_as_filtered(self):
        stats = P.window_stats([{"ts": self.T0, "kind": "nope"}], self.T0 + 1)
        self.assertEqual(stats["counts"]["filtered"], 1)


class TestRestrictedPage(unittest.TestCase):
    def test_hit(self):
        self.assertEqual(P.text_hits_restricted("您的账户存在异常行为，请稍后再试"), "您的账户存在异常行为")

    def test_miss(self):
        self.assertIsNone(P.text_hits_restricted("共 200 个岗位"))

    def test_empty(self):
        self.assertIsNone(P.text_hits_restricted(""))


class TestPaceState(unittest.TestCase):
    T0 = 1_700_000_000_000

    def test_record_and_window(self):
        s = P.PaceState()
        s.record("delivered", self.T0)
        s.record("filtered", self.T0 + 1000)
        self.assertEqual(s.window_remaining(self.T0 + 2000), 198)
        self.assertFalse(s.is_blocked(self.T0 + 2000))

    def test_blocked_when_window_full(self):
        s = P.PaceState()
        for i in range(200):
            s.record("filtered", self.T0 + i)
        self.assertTrue(s.is_blocked(self.T0 + 500))
        self.assertEqual(s.next_action_at(self.T0 + 500), self.T0 + 60 * 60_000)

    def test_apply_fuse_sets_deadline(self):
        s = P.PaceState()
        s.begin_round(self.T0)
        for i in range(80):
            s.record_click(self.T0 + i, desc_ok=True)
        v = s.apply_fuse(self.T0 + 81)
        self.assertTrue(v["fuse"])
        self.assertEqual(v["wait_minutes"], 5)
        self.assertTrue(s.is_blocked(self.T0 + 82))
        self.assertFalse(s.is_blocked(self.T0 + 6 * 60_000))

    def test_fuse_escalates(self):
        s = P.PaceState()
        waits = []
        now = self.T0
        for _ in range(3):
            s.begin_round(now)
            for i in range(80):
                s.record_click(now + i)
            v = s.apply_fuse(now + 100)
            waits.append(v["wait_minutes"])
            now = v["until_ms"] + 1  # 跳过冷却期
        self.assertEqual(waits, [5, 8, 13])

    def test_desc_miss_resets_on_success(self):
        s = P.PaceState()
        s.record_click(self.T0, desc_ok=False)
        s.record_click(self.T0 + 1, desc_ok=False)
        self.assertEqual(s.desc_miss_streak, 2)
        s.record_click(self.T0 + 2, desc_ok=True)
        self.assertEqual(s.desc_miss_streak, 0)

    def test_round_trip(self):
        s = P.PaceState()
        s.record("delivered", self.T0)
        s.record_click(self.T0, desc_ok=False)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pace.json")
            s.save(path)
            loaded = P.PaceState.load(path)
        self.assertEqual(len(loaded.visits), 1)
        self.assertEqual(loaded.visits[0]["kind"], "delivered")
        self.assertEqual(loaded.desc_miss_streak, 1)

    def test_corrupt_file_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pace.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            s = P.PaceState.load(path)
        self.assertEqual(s.visits, [])
        self.assertEqual(s.window_remaining(self.T0), 200)

    def test_overrides_are_sanitized(self):
        s = P.PaceState.load("__missing__.json", {"DETAIL_PER_MINUTE": "abc", "VISIT_WINDOW_MAX": 1})
        self.assertEqual(s.pace["DETAIL_PER_MINUTE"], 10)  # 非法值回落默认
        self.assertEqual(s.pace["VISIT_WINDOW_MAX"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
