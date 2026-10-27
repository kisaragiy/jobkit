"""schedule.py 单测 —— 只测纯逻辑，不碰文件之外的东西（临时目录里跑持久化）。

跑法：python -m unittest test_schedule
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import schedule as sched


def ms(y, mo, d, h=0, mi=0, s=0):
    """本地时间 → 毫秒时间戳（与模块内部一致，不看时区设置具体是哪个）。"""
    return int(round(datetime(y, mo, d, h, mi, s).timestamp() * 1000))


def daily(time="09:30", quantity=30, image_resume=0, enabled=1, sid="s-daily"):
    return {"id": sid, "time": time, "repeat": "daily", "quantity": quantity,
            "enabled": enabled, "image_resume": image_resume}


def once(date="2026-03-02", time="09:30", quantity=30, sid="s-once"):
    return {"id": sid, "time": time, "repeat": "once", "date": date,
            "quantity": quantity, "enabled": 1, "image_resume": 0}


class TestOccurrence(unittest.TestCase):
    def test_daily_occurrence_is_today(self):
        slot = daily("09:30")
        self.assertEqual(sched.slot_occurrence_ms(slot, ms(2026, 3, 2, 8, 0)),
                         ms(2026, 3, 2, 9, 30))

    def test_daily_after_time_rolls_to_tomorrow(self):
        slot = daily("09:30")
        row = sched.next_due([slot], ms(2026, 3, 2, 10, 0))
        self.assertIsNotNone(row)
        self.assertEqual(row["at_ms"], ms(2026, 3, 3, 9, 30))
        self.assertEqual(row["wait_seconds"], 23 * 3600 + 30 * 60)
        self.assertFalse(row["due"])

    def test_once_past_date_never_fires(self):
        slot = once(date="2026-03-01", time="09:30")
        now = ms(2026, 3, 2, 0, 1)
        self.assertIsNone(sched.next_due([slot], now))
        self.assertEqual(sched.pending_slots([slot], now), [])

    def test_once_fires_on_its_date(self):
        slot = once(date="2026-03-02", time="09:30")
        now = ms(2026, 3, 2, 9, 31)
        self.assertEqual(len(sched.pending_slots([slot], now)), 1)
        self.assertEqual(sched.next_due([slot], now)["wait_seconds"], 0)

    def test_bad_time_gives_none(self):
        self.assertIsNone(sched.slot_occurrence_ms({"time": "25:00", "repeat": "daily"},
                                                   ms(2026, 3, 2, 8, 0)))

    def test_disabled_slot_not_due(self):
        self.assertIsNone(sched.next_due([daily("09:30", enabled=0)], ms(2026, 3, 2, 8, 0)))


class TestNormalize(unittest.TestCase):
    def test_new_slot_defaults(self):
        slot = sched.new_slot()
        self.assertEqual(slot["time"], "09:30")
        self.assertEqual(slot["repeat"], "daily")
        self.assertEqual(slot["quantity"], 30)
        self.assertEqual(slot["enabled"], 1)
        self.assertTrue(slot["id"].startswith("s-"))
        self.assertNotIn("date", slot)

    def test_new_slot_once_needs_date(self):
        with self.assertRaises(ValueError):
            sched.new_slot("10:00", 10, "once")
        with self.assertRaises(ValueError):
            sched.new_slot("10:00", 10, "once", "2026-02-30")
        slot = sched.new_slot("10:00", 10, "once", "2026-03-02")
        self.assertEqual(slot["date"], "2026-03-02")

    def test_new_slot_bad_args(self):
        with self.assertRaises(ValueError):
            sched.new_slot("25:00")
        with self.assertRaises(ValueError):
            sched.new_slot("09:30", -1)
        with self.assertRaises(ValueError):
            sched.new_slot("09:30", 30, "weekly")

    def test_time_is_padded(self):
        slot = sched.normalize_slot({"time": "9:5", "repeat": "daily", "quantity": 5})
        self.assertEqual(slot["time"], "09:05")

    def test_invalid_time(self):
        for bad in ("24:00", "09:60", "930", "09:30:00", "", "abc", None, 9.5):
            self.assertIsNone(sched.normalize_slot({"time": bad, "repeat": "daily",
                                                    "quantity": 1}), bad)

    def test_invalid_date_for_once(self):
        for bad in ("2026-02-30", "2026-13-01", "2026/03/02", "", None, "20260302"):
            self.assertIsNone(sched.normalize_slot({"time": "09:30", "repeat": "once",
                                                    "date": bad, "quantity": 1}), bad)
        self.assertIsNone(sched.normalize_slot({"time": "09:30", "repeat": "once",
                                                "quantity": 1}))

    def test_invalid_quantity(self):
        for bad in (-5, "abc", None, [1], {"n": 1}):
            self.assertIsNone(sched.normalize_slot({"time": "09:30", "repeat": "daily",
                                                    "quantity": bad}), bad)

    def test_quantity_coerced(self):
        self.assertEqual(sched.normalize_slot({"time": "09:30", "repeat": "daily",
                                               "quantity": "30"})["quantity"], 30)
        self.assertEqual(sched.normalize_slot({"time": "09:30", "repeat": "daily",
                                               "quantity": 30.9})["quantity"], 30)
        self.assertEqual(sched.normalize_slot({"time": "09:30", "repeat": "daily",
                                               "quantity": 0})["quantity"], 0)

    def test_non_dict(self):
        for bad in (None, [], "09:30", 42):
            self.assertIsNone(sched.normalize_slot(bad))

    def test_normalize_is_idempotent(self):
        slot = sched.new_slot("09:30", 12, "once", "2026-03-02")
        self.assertEqual(sched.normalize_slot(slot), slot)


class TestPendingBoundaries(unittest.TestCase):
    def setUp(self):
        self.slot = daily("09:30")
        self.occ = ms(2026, 3, 2, 9, 30)

    def test_exactly_due(self):
        rows = sched.pending_slots([self.slot], self.occ)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["late_seconds"], 0)

    def test_one_second_early(self):
        self.assertEqual(sched.pending_slots([self.slot], self.occ - 1000), [])

    def test_late_4m59s(self):
        rows = sched.pending_slots([self.slot], self.occ + 299000)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["late_seconds"], 299)

    def test_late_5m01s(self):
        self.assertEqual(sched.pending_slots([self.slot], self.occ + 301000), [])

    def test_late_5m00s_still_in_window(self):
        self.assertEqual(len(sched.pending_slots([self.slot], self.occ + 300000)), 1)

    def test_done_today_not_pending(self):
        slot = dict(self.slot, last_run_ms=self.occ)
        self.assertEqual(sched.pending_slots([slot], self.occ + 1000), [])
        # 今天跑过不影响明天
        self.assertEqual(len(sched.pending_slots([slot], ms(2026, 3, 3, 9, 30))), 1)

    def test_runs_param_suppresses(self):
        runs = [{"slot_id": self.slot["id"], "at_ms": self.occ + 5000, "sent": 1}]
        self.assertEqual(sched.pending_slots([self.slot], self.occ + 10000, runs), [])

    def test_pending_sorted(self):
        a = daily("09:30", sid="a")
        b = daily("09:29", sid="b")
        rows = sched.pending_slots([a, b], ms(2026, 3, 2, 9, 31))
        self.assertEqual([r["slot_id"] for r in rows], ["b", "a"])


class TestEstimate(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(sched.estimate_minutes(0), 3)

    def test_one_text(self):
        self.assertEqual(sched.estimate_minutes(1), 4)          # ceil(45/60)=1 +3

    def test_ten_image(self):
        self.assertEqual(sched.estimate_minutes(10, image_resume=True), 18)  # 15 + 3

    def test_zero_quantity_not_below_floor(self):
        self.assertGreaterEqual(sched.estimate_minutes(0, image_resume=True), 3)

    def test_garbage_quantity_falls_back_to_floor(self):
        self.assertEqual(sched.estimate_minutes("abc"), 3)
        self.assertEqual(sched.estimate_minutes(-10), 3)

    def test_scales_with_quantity(self):
        self.assertGreater(sched.estimate_minutes(100), sched.estimate_minutes(10))
        self.assertGreater(sched.estimate_minutes(10, True), sched.estimate_minutes(10, False))


class TestOverlap(unittest.TestCase):
    NOW = ms(2026, 3, 2, 8, 0)

    def test_near_slots_warn(self):
        # 30 岗文字 → 26 分钟，09:30 与 09:35 只差 5 分钟
        warnings = sched.overlap_warning([daily("09:30"), daily("09:35", sid="s2")], self.NOW)
        self.assertEqual(len(warnings), 1)
        self.assertIn("09:35", warnings[0])

    def test_far_slots_do_not_warn(self):
        warnings = sched.overlap_warning([daily("09:30"), daily("14:00", sid="s2")], self.NOW)
        self.assertEqual(warnings, [])

    def test_different_dates_do_not_warn(self):
        a = once(date="2026-03-02", time="09:30", sid="a")
        b = once(date="2026-03-05", time="09:30", sid="b")
        self.assertEqual(sched.overlap_warning([a, b], self.NOW), [])

    def test_once_same_day_overlap(self):
        # 10 岗 → 11 分钟；09:30 与 09:40 重叠
        a = once(date="2026-03-02", time="09:30", quantity=10, sid="a")
        b = once(date="2026-03-02", time="09:40", quantity=10, sid="b")
        warnings = sched.overlap_warning([a, b], self.NOW)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-03-02 09:40", warnings[0])

    def test_quantity_affects_warning(self):
        a = daily("09:30", quantity=1, sid="a")        # 4 分钟
        b = daily("09:35", quantity=1, sid="b")
        self.assertEqual(sched.overlap_warning([a, b], self.NOW), [])
        b2 = daily("09:33", quantity=1, sid="b2")      # 3 分钟 < 4 → 重叠
        self.assertEqual(len(sched.overlap_warning([a, b2], self.NOW)), 1)

    def test_disabled_slot_ignored(self):
        warnings = sched.overlap_warning([daily("09:30", enabled=0), daily("09:35", sid="s2")],
                                         self.NOW)
        self.assertEqual(warnings, [])

    def test_same_minute_not_overlap(self):
        self.assertEqual(sched.overlap_warning([daily("09:30"), daily("09:30", sid="s2")],
                                               self.NOW), [])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "schedule.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        slot = sched.new_slot("09:30", 12)
        saved = sched.save(self.path, {"slots": [slot], "runs": []})
        self.assertEqual(saved["slots"], [slot])
        self.assertEqual(sched.load(self.path), {"slots": [slot], "runs": []})
        # 没有残留 .tmp
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_missing_file_is_empty(self):
        self.assertEqual(sched.load(Path(self.tmp.name) / "nope.json"),
                         {"slots": [], "runs": []})

    def test_broken_json_degrades(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(sched.load(self.path), {"slots": [], "runs": []})

    def test_wrong_shape_degrades(self):
        self.path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        self.assertEqual(sched.load(self.path), {"slots": [], "runs": []})

    def test_bad_slots_dropped_but_good_kept(self):
        data = {"slots": [daily("09:30"), {"time": "99:99"}, "nonsense"], "runs": []}
        sched.save(self.path, data)
        loaded = sched.load(self.path)
        self.assertEqual(len(loaded["slots"]), 1)
        self.assertEqual(loaded["slots"][0]["time"], "09:30")

    def test_mark_run_appends_and_stamps_slot(self):
        slot = sched.new_slot("09:30", 5)
        data = {"slots": [slot], "runs": []}
        run = sched.mark_run(slot["id"], ms(2026, 3, 2, 9, 30), 5, "跑了 5 个", data=data)
        self.assertEqual(run["sent"], 1)
        self.assertEqual(data["slots"][0]["last_run_ms"], ms(2026, 3, 2, 9, 30))
        self.assertEqual(len(data["runs"]), 1)

    def test_mark_run_keeps_last_50(self):
        slot = sched.new_slot("09:30", 5)
        data = {"slots": [slot], "runs": []}
        for i in range(60):
            sched.mark_run(slot["id"], ms(2026, 3, 2, 9, 30) + i * 1000, 1, f"run{i}", data=data)
        self.assertEqual(len(data["runs"]), 50)
        self.assertEqual(data["runs"][-1]["note"], "run59")
        self.assertEqual(data["runs"][0]["note"], "run10")

    def test_mark_run_persists_when_data_omitted(self):
        slot = sched.new_slot("09:30", 5)
        sched.save(self.path, {"slots": [slot], "runs": []})
        sched.mark_run(slot["id"], ms(2026, 3, 2, 9, 30), 0, "失败", path=self.path)
        loaded = sched.load(self.path)
        self.assertEqual(len(loaded["runs"]), 1)
        self.assertEqual(loaded["runs"][0]["sent"], 0)
        self.assertEqual(loaded["slots"][0]["last_run_ms"], ms(2026, 3, 2, 9, 30))


if __name__ == "__main__":
    unittest.main()
