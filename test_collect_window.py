"""采集时间窗口的单测 —— 重点是跨午夜，这是最容易写错的地方。"""
from __future__ import annotations

import unittest
from datetime import datetime

import collect_window as cw


class TestParse(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(cw.parse_hhmm("22:00"), 22 * 60)
        self.assertEqual(cw.parse_hhmm("7:05"), 7 * 60 + 5)
        self.assertEqual(cw.parse_hhmm("00:00"), 0)

    def test_bad_raises(self):
        for bad in ("", "22", "abc", "25:00", "10:60", "10-00"):
            with self.assertRaises(ValueError, msg=bad):
                cw.parse_hhmm(bad)


class TestWindow(unittest.TestCase):
    """默认窗口 22:00 → 07:00，跨午夜，左闭右开。"""

    def at(self, h, m=0):
        return datetime(2026, 10, 20, h, m)

    def test_night_is_inside(self):
        for h in (22, 23, 0, 3, 6):
            self.assertTrue(cw.in_window(self.at(h, 30)), f"{h}:30 应在窗口内")

    def test_day_is_outside(self):
        for h in (7, 9, 12, 18, 21):
            self.assertFalse(cw.in_window(self.at(h, 30)), f"{h}:30 应在窗口外")

    def test_boundaries(self):
        self.assertTrue(cw.in_window(self.at(22, 0)), "22:00 是起点，含")
        self.assertFalse(cw.in_window(self.at(7, 0)), "07:00 是终点，不含")
        self.assertFalse(cw.in_window(self.at(21, 59)))

    def test_same_start_end_means_all_day(self):
        """起止相同不能变成「永不执行」——配错了应该是全天，才好排查。"""
        self.assertTrue(cw.in_window(self.at(12, 0), start="09:00", end="09:00"))

    def test_daytime_range(self):
        self.assertTrue(cw.in_window(self.at(10, 0), start="08:00", end="12:00"))
        self.assertFalse(cw.in_window(self.at(13, 0), start="08:00", end="12:00"))
        self.assertFalse(cw.in_window(self.at(7, 59), start="08:00", end="12:00"))


class TestHelpers(unittest.TestCase):
    def test_describe_cross_midnight(self):
        d = cw.describe("22:00", "07:00")
        self.assertIn("跨天", d)
        self.assertIn("9h00m", d)

    def test_describe_same_day(self):
        d = cw.describe("08:00", "12:00")
        self.assertIn("4h00m", d)
        self.assertNotIn("跨天", d)

    def test_next_open_tonight(self):
        now = datetime(2026, 10, 20, 12, 0)
        nxt = cw.next_open(now, "22:00")
        self.assertEqual(nxt, datetime(2026, 10, 20, 22, 0))

    def test_next_open_tomorrow(self):
        """已经过了开启时刻 → 顺延到明天，不能返回过去的时间。"""
        now = datetime(2026, 10, 20, 23, 0)
        nxt = cw.next_open(now, "22:00")
        self.assertEqual(nxt, datetime(2026, 10, 21, 22, 0))

    def test_check_or_reason_text(self):
        ok, why = cw.check_or_reason(datetime(2026, 10, 20, 12, 0))
        self.assertFalse(ok)
        self.assertIn("22:00", why)
        self.assertIn("--force", why)

        ok2, why2 = cw.check_or_reason(datetime(2026, 10, 20, 23, 0))
        self.assertTrue(ok2)
        self.assertIn("窗口内", why2)


if __name__ == "__main__":
    unittest.main()
