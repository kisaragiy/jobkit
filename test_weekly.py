"""求职周报：周范围、统计口径、诊断、目标。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import store
import weekly


class TestWeekBounds(unittest.TestCase):
    def test_monday_start(self):
        """10-20 是周二，本周应从 10-19 周一 00:00 开始。"""
        start, end, label = weekly.week_bounds(0, today=dt.date(2026, 10, 20))
        s = dt.datetime.fromtimestamp(start)
        e = dt.datetime.fromtimestamp(end)
        self.assertEqual(s.date(), dt.date(2026, 10, 19))
        self.assertEqual(s.hour, 0)
        self.assertEqual(e.date(), dt.date(2026, 10, 26))
        self.assertIn("本周", label)

    def test_monday_itself(self):
        start, _, _ = weekly.week_bounds(0, today=dt.date(2026, 10, 19))
        self.assertEqual(dt.datetime.fromtimestamp(start).date(),
                         dt.date(2026, 10, 19))

    def test_sunday_belongs_to_same_week(self):
        start, _, _ = weekly.week_bounds(0, today=dt.date(2026, 10, 25))
        self.assertEqual(dt.datetime.fromtimestamp(start).date(),
                         dt.date(2026, 10, 19))

    def test_prev_week(self):
        start, end, label = weekly.week_bounds(-1, today=dt.date(2026, 10, 20))
        self.assertEqual(dt.datetime.fromtimestamp(start).date(),
                         dt.date(2026, 10, 12))
        self.assertEqual(dt.datetime.fromtimestamp(end).date(),
                         dt.date(2026, 10, 19))
        self.assertIn("上周", label)

    def test_week_is_exactly_7_days(self):
        start, end, _ = weekly.week_bounds(0, today=dt.date(2026, 10, 20))
        self.assertEqual(end - start, 7 * 86400)

    def test_cross_year(self):
        start, end, _ = weekly.week_bounds(0, today=dt.date(2027, 1, 1))
        self.assertEqual(dt.datetime.fromtimestamp(start).date(),
                         dt.date(2026, 12, 28))
        self.assertEqual(dt.datetime.fromtimestamp(end).date(),
                         dt.date(2027, 1, 4))


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_db = store.DB_PATH
        self._old_t = weekly.TARGETS_PATH
        store.DB_PATH = Path(self.tmp) / "t.db"
        weekly.TARGETS_PATH = Path(self.tmp) / "targets.json"
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old_db
        weekly.TARGETS_PATH = self._old_t

    def greet(self, ts, status="sent"):
        self._n = getattr(self, "_n", 0) + 1
        self.c.execute(
            "INSERT INTO greetings (job_id, security_id, greeted_at, status) "
            "VALUES (?,?,?,?)", (f"j{ts}_{self._n}", "", ts, status))
        self.c.commit()

    def conv(self, key, ts, cat="pending"):
        self.c.execute(
            "INSERT INTO conversations (conv_key, category, first_seen) "
            "VALUES (?,?,?)", (key, cat, ts))
        self.c.commit()

    def ts(self, y, m, d):
        return dt.datetime(y, m, d, 12, 0).timestamp()


class TestCollect(_DB):
    def test_counts_applied(self):
        self.greet(self.ts(2026, 10, 20))
        self.greet(self.ts(2026, 10, 21))
        s, e, _ = weekly.week_bounds(0, today=dt.date(2026, 10, 20))
        self.assertEqual(weekly.collect(self.c, s, e)["applied"], 2)

    def test_ignores_failed_status(self):
        """没发出去的（failed/pending）不算投递。"""
        self.greet(self.ts(2026, 10, 20), status="sent")
        self.greet(self.ts(2026, 10, 20), status="failed")
        s, e, _ = weekly.week_bounds(0, today=dt.date(2026, 10, 20))
        self.assertEqual(weekly.collect(self.c, s, e)["applied"], 1)

    def test_out_of_range_excluded(self):
        self.greet(self.ts(2026, 10, 10))
        s, e, _ = weekly.week_bounds(0, today=dt.date(2026, 10, 20))
        self.assertEqual(weekly.collect(self.c, s, e)["applied"], 0)

    def test_conversation_categories(self):
        self.conv("a", self.ts(2026, 10, 20), "interview")
        self.conv("b", self.ts(2026, 10, 20), "rejected")
        self.conv("c", self.ts(2026, 10, 20), "pending")
        s, e, _ = weekly.week_bounds(0, today=dt.date(2026, 10, 20))
        r = weekly.collect(self.c, s, e)
        self.assertEqual(r["new_convs"], 3)
        self.assertEqual(r["interview"], 1)
        self.assertEqual(r["rejected"], 1)


class TestIdle(_DB):
    def test_never_applied(self):
        self.assertIsNone(weekly.idle_days(self.c, dt.date(2026, 10, 20)))

    def test_idle_days(self):
        self.greet(self.ts(2026, 7, 17))
        self.assertEqual(weekly.idle_days(self.c, dt.date(2026, 10, 20)), 95)

    def test_today_applied(self):
        self.greet(self.ts(2026, 10, 20))
        self.assertEqual(weekly.idle_days(self.c, dt.date(2026, 10, 20)), 0)


class TestDiagnose(unittest.TestCase):
    def z(self, **kw):
        return {**{"applied": 0, "new_convs": 0, "interview": 0,
                   "rejected": 0}, **kw}

    def test_nothing_two_weeks(self):
        d = weekly.diagnose(self.z(), self.z())
        self.assertTrue(any("都没投递" in x for x in d))

    def test_stopped_after_applying(self):
        d = weekly.diagnose(self.z(), self.z(applied=15))
        self.assertTrue(any("上期投了" in x for x in d))

    def test_many_applied_no_reply(self):
        d = weekly.diagnose(self.z(applied=20), self.z(applied=10))
        self.assertTrue(any("岗位匹配" in x for x in d))

    def test_replies_no_interview(self):
        d = weekly.diagnose(self.z(applied=15, new_convs=5), self.z())
        self.assertTrue(any("聊得下去" in x for x in d))

    def test_has_interview(self):
        d = weekly.diagnose(self.z(applied=5, new_convs=2, interview=1), self.z())
        self.assertTrue(any("面试" in x for x in d))

    def test_rejection_mentioned(self):
        d = weekly.diagnose(self.z(applied=5, rejected=1), self.z())
        self.assertTrue(any("拒信" in x for x in d))


class TestTargets(_DB):
    def test_defaults(self):
        t = weekly.load_targets()
        self.assertEqual(t["apply"], 20)

    def test_save_and_load(self):
        weekly.save_targets(apply=10)
        self.assertEqual(weekly.load_targets()["apply"], 10)

    def test_partial_update_keeps_other(self):
        weekly.save_targets(apply=10)
        t = weekly.save_targets(interview=5)
        self.assertEqual(t["apply"], 10)
        self.assertEqual(t["interview"], 5)

    def test_corrupt_file_falls_back(self):
        weekly.TARGETS_PATH.write_text("{broken", encoding="utf-8")
        self.assertEqual(weekly.load_targets()["apply"], 20)


class TestBuild(_DB):
    def test_applied_week(self):
        for i in range(5):
            self.greet(self.ts(2026, 10, 20) + i)
        w = weekly.build(self.c, 0, today=dt.date(2026, 10, 20))
        self.assertEqual(w["cur"]["applied"], 5)
        self.assertEqual(w["apply_rate"], 25)      # 5/20

    def test_idle_warning(self):
        self.greet(self.ts(2026, 7, 17))
        w = weekly.build(self.c, 0, today=dt.date(2026, 10, 20))
        self.assertEqual(w["idle_days"], 95)
        self.assertTrue(any("天前" in x for x in w["warnings"]))

    def test_historical_week_uses_that_week_end(self):
        """查 7 月那周不能用今天的日期算闲置天数。"""
        self.greet(self.ts(2026, 7, 17))
        w = weekly.build(self.c, 0, today=dt.date(2026, 7, 17))
        self.assertEqual(w["idle_days"], 0)


class TestRender(_DB):
    def test_render_has_disclaimer(self):
        """必须写清新会话不是 HR 回复时间。"""
        w = weekly.build(self.c, 0, today=dt.date(2026, 10, 20))
        out = weekly.render(w)
        self.assertIn("不是 HR 回复时间", out)

    def test_render_fail(self):
        self.assertIn("失败", weekly.render({"ok": False, "reason": "x"}))


if __name__ == "__main__":
    unittest.main()
