"""followup.py 单测（stdlib unittest，零外部依赖）。"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path

import followup as f


def local_ms(y, mo, d, h, mi, s=0) -> int:
    """本地时间 → 毫秒时间戳（发送时段测试用，跟随本机时区）。"""
    return int(datetime.datetime(y, mo, d, h, mi, s).timestamp() * 1000)


DAY = f.DAY_MS
NOW = local_ms(2026, 10, 19, 12, 0, 0)   # 周一中午，固定在发送时段内


def row(job_id, days_ago, status="sent", **extra):
    item = {
        "job_id": job_id,
        "security_id": "sec-" + job_id,
        "greeted_at": (NOW - days_ago * DAY) / 1000.0,   # store.py 写的是秒
        "message": "您好",
        "status": status,
        "error": None,
    }
    item.update(extra)
    return item


class StageTest(unittest.TestCase):
    def test_three_day_boundary(self):
        self.assertEqual(f.pick_stage(NOW - 3 * DAY, NOW), "gentle")
        self.assertEqual(f.pick_stage(NOW - 3 * DAY + 1000, NOW), "")      # 差 1 秒
        self.assertEqual(f.pick_stage(NOW - 3 * DAY - 1000, NOW), "gentle")  # 超过 1 秒
        self.assertEqual(f.pick_stage(NOW - 2 * DAY, NOW), "")

    def test_seven_day_boundary(self):
        self.assertEqual(f.pick_stage(NOW - 7 * DAY, NOW), "direct")
        self.assertEqual(f.pick_stage(NOW - 7 * DAY + 1000, NOW), "polite")
        self.assertEqual(f.pick_stage(NOW - 6 * DAY - 3600_000, NOW), "polite")

    def test_middle_is_polite(self):
        self.assertEqual(f.pick_stage(NOW - 4 * DAY, NOW), "polite")
        self.assertEqual(f.pick_stage(NOW - 5 * DAY, NOW), "polite")

    def test_bad_values(self):
        self.assertEqual(f.pick_stage(0, NOW), "")
        self.assertEqual(f.pick_stage(None, NOW), "")
        self.assertEqual(f.pick_stage(NOW + DAY, NOW), "")   # 未来时间


class DueFollowupsTest(unittest.TestCase):
    def setUp(self):
        self.state = f.load_state(Path(tempfile.gettempdir()) / "no_such_followup_state.json")

    def test_exact_three_days_due(self):
        rows = [row("a", 3), row("b", 3 - 1 / 86400)]   # b 差 1 秒
        due = f.due_followups(rows, NOW, self.state)
        self.assertEqual([it["job_id"] for it in due], ["a"])
        self.assertEqual(due[0]["stage"], "gentle")
        self.assertTrue(due[0]["message"])

    def test_oldest_first(self):
        due = f.due_followups([row("new", 4), row("old", 9)], NOW, self.state)
        self.assertEqual([it["job_id"] for it in due], ["old", "new"])

    def test_already_followed_job_excluded(self):
        state = {"sent": [{"job_id": "a", "ts": NOW, "stage": "gentle"}], "day": "", "count": 0}
        due = f.due_followups([row("a", 4), row("b", 4)], NOW, state)
        self.assertEqual([it["job_id"] for it in due], ["b"])

    def test_failed_status_excluded(self):
        due = f.due_followups([row("a", 4, status="failed"), row("b", 4, status="sent")], NOW, self.state)
        self.assertEqual([it["job_id"] for it in due], ["b"])

    def test_ttl_excluded(self):
        due = f.due_followups([row("a", 15)], NOW, self.state)
        self.assertEqual(due, [])

    def test_greeted_at_in_ms_also_works(self):
        item = row("a", 4)
        item["greeted_at"] = item["greeted_at"] * 1000.0   # 毫秒来源
        due = f.due_followups([item], NOW, self.state)
        self.assertEqual(len(due), 1)

    def test_garbage_rows_ignored(self):
        due = f.due_followups([None, {}, {"job_id": "x", "status": "sent", "greeted_at": "bad"}], NOW, self.state)
        self.assertEqual(due, [])

    def test_daily_limit_truncates_to_remaining(self):
        rows = [row(f"j{i}", 3 + i / 24.0) for i in range(21)]
        state = {"sent": [], "day": f._day_key(NOW), "count": 5}
        due = f.due_followups(rows, NOW, state, {"daily_limit": 20})
        self.assertEqual(len(due), 15)
        # 额度用完 → 一条都不发
        state["count"] = 20
        self.assertEqual(f.due_followups(rows, NOW, state, {"daily_limit": 20}), [])

    def test_daily_limit_fresh_state_caps_at_20(self):
        rows = [row(f"j{i}", 3 + i / 24.0) for i in range(21)]
        due = f.due_followups(rows, NOW, {"sent": [], "day": "", "count": 0}, {"daily_limit": 20})
        self.assertEqual(len(due), 20)

    def test_count_yesterday_does_not_block_today(self):
        rows = [row("a", 3)]
        state = {"sent": [], "day": "2020-01-01", "count": 20}
        self.assertEqual(len(f.due_followups(rows, NOW, state, {"daily_limit": 20})), 1)


class SendWindowTest(unittest.TestCase):
    def test_boundaries(self):
        ok, _ = f.check_send_window(local_ms(2026, 10, 19, 8, 59))
        self.assertFalse(ok)
        ok, _ = f.check_send_window(local_ms(2026, 10, 19, 9, 0))
        self.assertTrue(ok)
        ok, _ = f.check_send_window(local_ms(2026, 10, 19, 20, 59))
        self.assertTrue(ok)
        ok, why = f.check_send_window(local_ms(2026, 10, 19, 21, 0))
        self.assertFalse(ok)
        self.assertIn("21:00", why)

    def test_custom_window(self):
        ok, _ = f.check_send_window(local_ms(2026, 10, 19, 8, 0), {"send_start_hour": 8, "send_end_hour": 10})
        self.assertTrue(ok)


class MessageTest(unittest.TestCase):
    def test_three_stages_valid(self):
        texts = {}
        for stage in f.STAGES:
            text = f.build_message({"job_id": "a", "title": "后端开发工程师", "company": "某公司"}, stage)
            self.assertTrue(text.strip(), stage)
            self.assertLessEqual(len(text), 100, text)
            self.assertFalse(f.has_banned_word(text), text)
            texts[stage] = text
        self.assertEqual(len(set(texts.values())), 3)
        self.assertIn("后端开发工程师", texts["direct"])

    def test_no_title_ok(self):
        for stage in f.STAGES:
            self.assertLessEqual(len(f.build_message({}, stage)), 100)

    def test_long_title_clipped(self):
        text = f.build_message({"title": "超长岗位名" * 20}, "gentle")
        self.assertLessEqual(len(text), 100)

    def test_banned_fallback(self):
        text = f.build_message({"title": "测试"}, "gentle", llm=lambda job, stage: "方便说一下薪资范围吗")
        self.assertFalse(f.has_banned_word(text))
        self.assertLessEqual(len(text), 100)

    def test_llm_exception_falls_back(self):
        def boom(job, stage):
            raise RuntimeError("api down")
        self.assertTrue(f.build_message({"title": "测试"}, "polite", llm=boom))


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        state = {"sent": [], "day": "", "count": 0}
        self.assertTrue(f.mark_sent(state, "a", NOW, "gentle"))
        self.assertTrue(f.save_state(self.path, state))
        loaded = f.load_state(self.path)
        self.assertEqual(loaded["sent"][0]["job_id"], "a")
        self.assertEqual(loaded["sent"][0]["ts"], NOW)
        self.assertEqual(loaded["day"], f._day_key(NOW))
        self.assertEqual(loaded["count"], 1)

    def test_bad_json_degrades(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(f.load_state(self.path), {"sent": [], "day": "", "count": 0})

    def test_missing_file_degrades(self):
        self.assertEqual(f.load_state(self.path), {"sent": [], "day": "", "count": 0})

    def test_wrong_structure_degrades(self):
        self.path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
        self.assertEqual(f.load_state(self.path)["sent"], [])

    def test_mark_sent_idempotent(self):
        state = {"sent": [], "day": "", "count": 0}
        self.assertTrue(f.mark_sent(state, "a", NOW, "gentle"))
        self.assertFalse(f.mark_sent(state, "a", NOW, "direct"))
        self.assertEqual(len(state["sent"]), 1)
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["sent"][0]["stage"], "gentle")

    def test_mark_sent_new_day_resets_count(self):
        state = {"sent": [], "day": f._day_key(NOW), "count": 7}
        tomorrow = NOW + DAY
        f.mark_sent(state, "b", tomorrow, "polite")
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["day"], f._day_key(tomorrow))

    def test_mark_sent_rejects_blank(self):
        state = {"sent": [], "day": "", "count": 0}
        self.assertFalse(f.mark_sent(state, "", NOW, "gentle"))
        self.assertEqual(state["count"], 0)

    def test_save_trims_to_200(self):
        state = {"sent": [{"job_id": f"j{i}", "ts": NOW - (300 - i) * 1000, "stage": "gentle"}
                          for i in range(300)], "day": f._day_key(NOW), "count": 1}
        self.assertTrue(f.save_state(self.path, state))
        loaded = f.load_state(self.path)
        self.assertEqual(len(loaded["sent"]), 200)
        self.assertEqual(loaded["sent"][-1]["job_id"], "j299")

    def test_save_creates_parent_dir(self):
        nested = Path(self.tmp.name) / "a" / "b" / "state.json"
        self.assertTrue(f.save_state(nested, {"sent": [], "day": "", "count": 0}))
        self.assertTrue(nested.exists())

    def test_daily_count(self):
        self.assertEqual(f.daily_count({"sent": [], "day": f._day_key(NOW), "count": 3}, NOW), 3)
        self.assertEqual(f.daily_count({"sent": [], "day": "2000-01-01", "count": 3}, NOW), 0)
        self.assertEqual(f.daily_count({}, NOW), 0)
        self.assertEqual(f.daily_count({"sent": [], "day": f._day_key(NOW), "count": "x"}, NOW), 0)


def rec(job_id, hours_ago, round_no=1, stage="gentle"):
    """状态里一条已跟进记录（ts 用毫秒，和 state 内部口径一致）。"""
    return {"job_id": job_id, "ts": NOW - int(hours_ago * 3600_000),
            "stage": stage, "round": round_no}


class RoundTest(unittest.TestCase):
    """多轮跟进：轮次递增 / 冷却阶梯 / 14 天 TTL / 轮次上限。"""

    def state(self, *records):
        return {"sent": list(records), "day": "", "count": 0}

    # ── 阶梯本身 ──────────────────────────────────────────────
    def test_ladder_values_match_source(self):
        # 出处：GetJob followup-logic.js:11 FOLLOWUP_COOLDOWN_HOURS = [12,24,48,72]
        self.assertEqual(f.COOLDOWN_LADDER_HOURS, (12, 24, 48, 72))
        self.assertEqual(f.cooldown_hours_for_round(1), 12)
        self.assertEqual(f.cooldown_hours_for_round(2), 24)
        self.assertEqual(f.cooldown_hours_for_round(3), 48)
        self.assertEqual(f.cooldown_hours_for_round(4), 72)

    def test_ladder_is_monotonic_and_clamped(self):
        hours = [f.cooldown_hours_for_round(n) for n in range(1, 5)]
        self.assertEqual(hours, sorted(hours))
        self.assertEqual(len(set(hours)), 4)               # 逐轮变长，不重复
        self.assertEqual(f.cooldown_hours_for_round(9), 72)  # 超出阶梯停在最后一档
        self.assertEqual(f.cooldown_hours_for_round(0), 12)
        self.assertEqual(f.cooldown_hours_for_round("x"), 12)

    def test_bad_ladder_falls_back(self):
        self.assertEqual(f.get_cfg({"cooldown_ladder": (48, 12, 24)})["cooldown_ladder"],
                         f.COOLDOWN_LADDER_HOURS)              # 非递增 → 回落
        self.assertEqual(f.get_cfg({"cooldown_ladder": (12, 0)})["cooldown_ladder"],
                         f.COOLDOWN_LADDER_HOURS)              # 非正数 → 回落
        self.assertEqual(f.get_cfg({"cooldown_ladder": ["x"]})["cooldown_ladder"],
                         f.COOLDOWN_LADDER_HOURS)
        self.assertEqual(f.get_cfg({"cooldown_ladder": (6, 12, 24)})["cooldown_ladder"],
                         (6, 12, 24))                          # 合法自定义保留

    def test_max_rounds_never_exceeds_ladder(self):
        self.assertEqual(f.get_cfg({"max_rounds": 99})["max_rounds"], 4)
        self.assertEqual(f.get_cfg({"max_rounds": 2})["max_rounds"], 2)
        self.assertEqual(f.get_cfg({"max_rounds": 0})["max_rounds"], 1)

    # ── 轮次递增 ──────────────────────────────────────────────
    def test_round_increments_after_cooldown(self):
        rows = [row("a", 5)]                                  # 5 天前投递
        state = self.state(rec("a", 30))                      # 第 1 轮在 30h 前（>24h）
        due = f.due_followups(rows, NOW, state)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["round"], 2)
        self.assertEqual(due[0]["rounds_done"], 1)

    def test_round_2_waits_24h(self):
        rows = [row("a", 5)]
        # 第 2 轮冷却 = 阶梯第 2 档 24h：差 1 秒不算到
        self.assertEqual(f.due_followups(rows, NOW, self.state(rec("a", 24 - 1 / 3600))), [])
        due = f.due_followups(rows, NOW, self.state(rec("a", 24)))
        self.assertEqual([it["round"] for it in due], [2])

    def test_round_3_waits_48h(self):
        rows = [row("a", 6)]
        state = self.state(rec("a", 200, 1), rec("a", 47, 2))
        self.assertEqual(f.due_followups(rows, NOW, state), [])
        state = self.state(rec("a", 200, 1), rec("a", 48, 2))
        due = f.due_followups(rows, NOW, state)
        self.assertEqual([it["round"] for it in due], [3])

    def test_round_4_waits_72h(self):
        rows = [row("a", 7)]
        state = self.state(rec("a", 300, 1), rec("a", 200, 2), rec("a", 71, 3))
        self.assertEqual(f.due_followups(rows, NOW, state), [])
        state = self.state(rec("a", 300, 1), rec("a", 200, 2), rec("a", 72, 3))
        self.assertEqual([it["round"] for it in f.due_followups(rows, NOW, state)], [4])

    # ── 停止条件 ──────────────────────────────────────────────
    def test_stops_after_max_rounds(self):
        rows = [row("a", 10)]
        state = self.state(rec("a", 400, 1), rec("a", 300, 2),
                           rec("a", 200, 3), rec("a", 100, 4))
        self.assertEqual(f.due_followups(rows, NOW, state), [])
        # 上限可下调：跟过 2 轮就停
        state = self.state(rec("a", 400, 1), rec("a", 200, 2))
        self.assertEqual(f.due_followups(rows, NOW, state, {"max_rounds": 2}), [])

    def test_ttl_anchored_on_delivery_not_last_followup(self):
        # 投递 15 天 > TTL 14 天：哪怕 1 小时前才跟过，也不再跟
        rows = [row("a", 15)]
        self.assertEqual(f.due_followups(rows, NOW, self.state(rec("a", 1))), [])
        # 投递 13 天（未超 TTL）：冷却过了就还能跟
        rows = [row("b", 13)]
        self.assertEqual(len(f.due_followups(rows, NOW, self.state(rec("b", 25)))), 1)

    # ── 落盘 / 幂等 ───────────────────────────────────────────
    def test_mark_sent_records_round_and_refuses_early_replay(self):
        state = self.state()
        self.assertTrue(f.mark_sent(state, "a", NOW, "gentle"))
        self.assertEqual(state["sent"][0]["round"], 1)
        # 同一时刻重试：还没到第 2 轮冷却 → 不记
        self.assertFalse(f.mark_sent(state, "a", NOW, "gentle"))
        # 显式重放第 1 轮 → 不记
        self.assertFalse(f.mark_sent(state, "a", NOW, "gentle", round_no=1))
        # 过了 24h 才允许第 2 轮
        self.assertFalse(f.mark_sent(state, "a", NOW + 23 * 3600_000, "polite"))
        self.assertTrue(f.mark_sent(state, "a", NOW + 24 * 3600_000, "polite"))
        self.assertEqual([it["round"] for it in state["sent"]], [1, 2])
        # 第 2 轮落在次日 12:00 → 当日计数跨天重置为 1（不是累计 2）
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["day"], "2026-10-20")

    def test_mark_sent_same_day_accumulates_daily_count(self):
        state = self.state()
        self.assertTrue(f.mark_sent(state, "a", NOW, "gentle"))
        self.assertTrue(f.mark_sent(state, "b", NOW + 3600_000, "gentle"))
        # 同一天两个不同岗位 → 当日计数累加到 2
        self.assertEqual(state["count"], 2)

    def test_mark_sent_refuses_after_max_rounds(self):
        state = self.state(rec("a", 400, 1), rec("a", 300, 2),
                           rec("a", 200, 3), rec("a", 100, 4))
        self.assertFalse(f.mark_sent(state, "a", NOW, "direct"))
        self.assertEqual(len(state["sent"]), 4)

    def test_legacy_state_without_round_counts_as_one(self):
        state = {"sent": [{"job_id": "a", "ts": NOW - 30 * 3600_000, "stage": "gentle"}],
                 "day": "", "count": 0}
        due = f.due_followups([row("a", 5)], NOW, state)
        self.assertEqual([it["round"] for it in due], [2])

    def test_state_roundtrip_keeps_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = self.state(rec("a", 100, 1), rec("a", 10, 2))
            self.assertTrue(f.save_state(path, state))
            loaded = f.load_state(path)
            self.assertEqual([it["round"] for it in loaded["sent"]], [1, 2])
            # 落盘后再读回，第 3 轮冷却仍按 48h 判
            rows = [row("a", 6)]
            self.assertEqual(f.due_followups(rows, NOW, loaded), [])
            loaded["sent"][-1]["ts"] = NOW - 48 * 3600_000
            self.assertEqual([it["round"] for it in f.due_followups(rows, NOW, loaded)], [3])


if __name__ == "__main__":
    unittest.main()
