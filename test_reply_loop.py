"""回音闭环单测（stdlib unittest，零外部依赖）。

覆盖两件事：
  A. followup.split_by_reply —— 对方已回话的投递行不再进跟进队列（P0-2）
  B. run_chat_watch —— 「有 HR 消息就喊人」的排序 / 输出 / 退出码（P0-1）

全部用注入的 stub，不碰真库、不连浏览器。
"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path

import followup as F
import run_chat_watch as W
import run_followup as RF

NOW_MS = int(datetime.datetime(2026, 10, 20, 12, 0, 0).timestamp() * 1000)
DAY = F.DAY_MS


def greeted_row(job_id: str, company: str, days_ago: float = 4) -> dict:
    return {
        "job_id": job_id,
        "security_id": "sec-" + job_id,
        "greeted_at": (NOW_MS - days_ago * DAY) / 1000.0,   # store 写秒
        "status": "sent",
        "message": "您好",
        "error": None,
        "title": "AI应用开发",
        "company": company,
        "city": "广州",
    }


def conv(hr: str, company: str, category: str = "normal", *,
         first_seen: float = 0.0, updated_at: float = 0.0, msg: str = "你好") -> dict:
    return {
        "conv_key": f"{hr}|{company}",
        "hr_name": hr,
        "company": company,
        "position": "AI应用开发",
        "last_msg": msg,
        "category": category,
        "from_me": 0,
        "last_time": "10:41",
        "first_seen": first_seen,
        "updated_at": updated_at,
        "pending_since": first_seen,
        "handled": 0,
    }


# ---------------------------------------------------------------- A. 回音过滤

class SplitByReplyTest(unittest.TestCase):
    """对方回过话的投递行必须被摘出来，否则会对着已回复的人继续催。"""

    def test_replied_company_is_skipped(self):
        rows = [greeted_row("j1", "四箐园"), greeted_row("j2", "中软国际")]
        out = F.split_by_reply(rows, ["中软国际"])
        self.assertEqual([r["job_id"] for r in out["active"]], ["j1"])
        self.assertEqual([s["job_id"] for s in out["skipped"]], ["j2"])
        self.assertEqual(out["skipped"][0]["matched"], "中软国际")

    def test_no_replies_keeps_everything(self):
        rows = [greeted_row("j1", "四箐园")]
        out = F.split_by_reply(rows, [])
        self.assertEqual(len(out["active"]), 1)
        self.assertEqual(out["skipped"], [])

    def test_company_name_variants_match_via_matcher(self):
        """BOSS 会话里的公司名比岗位库短（上海兴岩信息科技 vs ...有限公司）。"""
        rows = [greeted_row("j1", "上海兴岩信息科技有限公司")]
        out = F.split_by_reply(
            rows, ["上海兴岩信息科技"],
            matcher=lambda a, b: "上海兴岩" in a and "上海兴岩" in b,
        )
        self.assertEqual(out["active"], [])
        self.assertEqual(len(out["skipped"]), 1)

    def test_blank_company_never_matches(self):
        """岗位没存公司名时不能因为空字符串被误判成「已回复」。"""
        rows = [greeted_row("j1", "")]
        out = F.split_by_reply(rows, ["", "四箐园"])
        self.assertEqual(len(out["active"]), 1)

    def test_bad_rows_are_kept_not_dropped(self):
        """脏数据（非 dict / 缺 company）应留在 active 里，不能静默丢掉。"""
        rows = [None, {"job_id": "x"}]
        out = F.split_by_reply(rows, ["中软国际"])
        self.assertEqual(len(out["active"]), 2)


class RunCycleReplyFilterTest(unittest.TestCase):
    """run_cycle 必须真的用上过滤结果，并且在输出里说明跳过了谁。"""

    def _run(self, replied, *, no_check=False):
        lines = []
        state = Path(tempfile.gettempdir()) / "no_such_reply_state.json"
        code = RF.run_cycle(
            send=False,
            state_path=state,
            rows_fn=lambda: [greeted_row("j1", "四箐园"),
                             greeted_row("j2", "中软国际")],
            replied_fn=lambda: replied,
            reply_check=not no_check,
            now_ms=NOW_MS,
            out=lines.append,
        )
        return code, "\n".join(lines)

    def test_replied_row_is_not_in_plan(self):
        code, text = self._run(["中软国际"])
        self.assertEqual(code, RF.EXIT_OK)
        self.assertIn("跳过", text)
        # 只跳了 1 条，所以计划里只剩四箐园那一条
        self.assertIn("[1/1]", text)
        self.assertIn("四箐园", text)

    def test_reply_check_can_be_disabled(self):
        code, text = self._run(["中软国际"], no_check=True)
        self.assertNotIn("跳过", text)

    def test_replied_fn_failure_is_reported_not_swallowed(self):
        """读不到会话表 = 无法确认谁回过话，必须出声（不能静默当空）。"""
        lines = []
        state = Path(tempfile.gettempdir()) / "no_such_reply_state.json"

        def boom():
            raise RuntimeError("db locked")

        code = RF.run_cycle(
            send=False, state_path=state,
            rows_fn=lambda: [greeted_row("j1", "四箐园")],
            replied_fn=boom, now_ms=NOW_MS, out=lines.append,
        )
        text = "\n".join(lines)
        self.assertEqual(code, RF.EXIT_ERROR)
        self.assertIn("回音", text)


# ---------------------------------------------------------------- B. 消息提醒

class RankTest(unittest.TestCase):
    def test_interview_first_then_resume_then_normal(self):
        items = [conv("A", "甲公司", "normal", updated_at=300),
                 conv("B", "乙公司", "resume_sent", updated_at=200),
                 conv("C", "丙公司", "interview", updated_at=100)]
        order = [r["company"] for r in W.rank(items, now_ms=NOW_MS)]
        self.assertEqual(order, ["丙公司", "乙公司", "甲公司"])

    def test_same_category_newest_first(self):
        items = [conv("A", "旧", "normal", updated_at=100),
                 conv("B", "新", "normal", updated_at=900)]
        self.assertEqual([r["company"] for r in W.rank(items, now_ms=NOW_MS)], ["新", "旧"])

    def test_rejected_last_and_unknown_category_tolerated(self):
        items = [conv("A", "拒信", "rejected", updated_at=999),
                 conv("B", "怪类", "weird", updated_at=999),
                 conv("C", "面试", "interview", updated_at=1)]
        order = [r["company"] for r in W.rank(items, now_ms=NOW_MS)]
        self.assertEqual(order[0], "面试")
        self.assertEqual(order[-1], "拒信")
        self.assertIn("怪类", order)

    def test_age_hours_derived_from_pending_since(self):
        it = conv("A", "甲公司", "interview", first_seen=(NOW_MS / 1000) - 5 * 3600)
        r = W.rank([it], now_ms=NOW_MS)[0]
        self.assertAlmostEqual(r["age_hours"], 5.0, places=1)


class NewFlagTest(unittest.TestCase):
    def test_first_run_marks_nothing_new(self):
        items = [conv("A", "甲公司", first_seen=100.0)]
        r = W.rank(items, now_ms=NOW_MS, last_run=None)[0]
        self.assertFalse(r["is_new"])

    def test_seen_after_last_run_is_new(self):
        items = [conv("A", "新消息", first_seen=200.0), conv("B", "旧消息", first_seen=100.0)]
        rows = W.rank(items, now_ms=NOW_MS, last_run=150.0)
        by = {r["company"]: r["is_new"] for r in rows}
        self.assertTrue(by["新消息"])
        self.assertFalse(by["旧消息"])


class DecideTest(unittest.TestCase):
    def test_pending_means_alert(self):
        self.assertEqual(W.decide([conv("A", "甲", "interview")], sync_ok=True), W.EXIT_ALERT)

    def test_clean_and_synced_is_ok(self):
        self.assertEqual(W.decide([], sync_ok=True), W.EXIT_OK)

    def test_sync_failed_and_nothing_known_is_error(self):
        """抓不到 + 库里也没有 → 不能断言「没人找我」，这是 ERROR。"""
        self.assertEqual(W.decide([], sync_ok=False), W.EXIT_ERROR)

    def test_sync_failed_but_pending_known_still_alert(self):
        self.assertEqual(W.decide([conv("A", "甲", "interview")], sync_ok=False), W.EXIT_ALERT)


class RenderTest(unittest.TestCase):
    def test_renders_category_and_hr_and_snippet(self):
        items = W.rank([conv("庞女士", "中软国际", "interview", msg="你目前在深圳吗 可以现场面试吗")],
                       now_ms=NOW_MS)
        text = W.render(items)
        self.assertIn("面试", text)
        self.assertIn("中软国际", text)
        self.assertIn("庞女士", text)
        self.assertIn("现场面试", text)

    def test_rejected_rendered_as_low_priority(self):
        text = W.render(W.rank([conv("A", "中软国际", "rejected")], now_ms=NOW_MS))
        self.assertIn("拒信", text)


class FillModeTest(unittest.TestCase):
    """--fill：填进聊天框但不发送。走 sender，且日志不能说成 dry-run。"""

    def test_fill_calls_sender_even_if_send_false(self):
        lines = []
        calls = []
        state = Path(tempfile.mkdtemp()) / "fill_state.json"

        def sender(item, now_ms):
            calls.append(item.get("company"))
            return True, ""

        code = RF.run_cycle(
            send=False, fill=True,
            state_path=state,
            rows_fn=lambda: [greeted_row("j1", "四箐园")],
            gate_fn=lambda: (True, "cdp", "ok"),
            sender=sender,
            replied_fn=lambda: [],
            now_ms=NOW_MS,
            out=lines.append,
        )
        text = "\n".join(lines)
        self.assertEqual(code, RF.EXIT_OK)
        self.assertIn("填入待你确认", text, "日志不能说成 dry-run，会让人以为没动手")
        self.assertEqual(calls, ["四箐园"], "fill 必须真的调 sender 去填")

    def test_plain_dry_run_still_does_not_call_sender(self):
        lines = []
        calls = []
        state = Path(tempfile.mkdtemp()) / "fill_state_dry.json"
        code = RF.run_cycle(
            send=False, fill=False,
            state_path=state,
            rows_fn=lambda: [greeted_row("j1", "四箐园")],
            sender=lambda item, now_ms: (calls.append(1), (True, ""))[1],
            replied_fn=lambda: [], now_ms=NOW_MS, out=lines.append,
        )
        self.assertEqual(code, RF.EXIT_OK)
        self.assertEqual(calls, [], "默认 dry-run 绝不能动手")


class ReadNoReplyTest(unittest.TestCase):
    """「已读没回」必须和「待我回」分开报，且不能把我自己发的婉拒算进去。"""

    NOW = 1784000000  # 秒

    def _row(self, **kw):
        base = {"hr_name": "庞女士", "company": "中软国际", "from_me": 1, "hr_read": 1,
                "last_msg": "您好，想确认下岗位进展", "updated_at": self.NOW - 3600}
        base.update(kw)
        return base

    def test_read_and_mine_is_included(self):
        out = W.read_no_reply([self._row()], now_ms=self.NOW * 1000)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["tier"], "fresh")

    def test_unread_is_excluded(self):
        out = W.read_no_reply([self._row(hr_read=0)], now_ms=self.NOW * 1000)
        self.assertEqual(out, [])

    def test_hr_message_is_excluded(self):
        out = W.read_no_reply([self._row(from_me=0)], now_ms=self.NOW * 1000)
        self.assertEqual(out, [])

    def test_my_own_rejection_is_excluded(self):
        """我自己发的婉拒不是「待跟进」——报出来等于自己催自己。"""
        row = self._row(last_msg="对不起，我觉得该职位不太适合我，祝您早日找到满意的人选")
        out = W.read_no_reply([row], now_ms=self.NOW * 1000)
        self.assertEqual(out, [])

    def test_tiers(self):
        rows = [
            self._row(company="A", updated_at=self.NOW - 3600),        # 1h
            self._row(company="B", updated_at=self.NOW - 30 * 3600),   # 30h
            self._row(company="C", updated_at=self.NOW - 100 * 3600),  # 100h
        ]
        out = W.read_no_reply(rows, now_ms=self.NOW * 1000)
        tiers = {d["company"]: d["tier"] for d in out}
        self.assertEqual(tiers["A"], "fresh")
        self.assertEqual(tiers["B"], "waiting")
        self.assertEqual(tiers["C"], "cold")

    def test_cold_first(self):
        rows = [
            self._row(company="新鲜", updated_at=self.NOW - 3600),
            self._row(company="冷了", updated_at=self.NOW - 100 * 3600),
        ]
        out = W.read_no_reply(rows, now_ms=self.NOW * 1000)
        self.assertEqual(out[0]["company"], "冷了", "最需要做决定的排前面")

    def test_missing_timestamp_is_unknown_not_crash(self):
        out = W.read_no_reply([self._row(updated_at=None)], now_ms=self.NOW * 1000)
        self.assertEqual(out[0]["tier"], "unknown")

    def test_hr_read_at_wins_over_updated_at(self):
        """有准确已读时刻就用它，别拿「最后看到会话」的时间冒充。"""
        row = self._row(updated_at=self.NOW - 3600,
                        hr_read_at=self.NOW - 100 * 3600)
        out = W.read_no_reply([row], now_ms=self.NOW * 1000)
        self.assertEqual(out[0]["tier"], "cold")
        self.assertEqual(out[0]["time_basis"], "read_at")

    def test_render_says_when_time_is_a_guess(self):
        row = self._row(updated_at=self.NOW - 3600)   # 没有 hr_read_at
        text = W.render_read_no_reply(W.read_no_reply([row], now_ms=self.NOW * 1000))
        self.assertIn("非已读时刻", text, "时间不准就必须说出来")

    def test_render_empty_is_empty_string(self):
        self.assertEqual(W.render_read_no_reply([]), "")

    def test_render_mentions_count_and_advice(self):
        rows = [self._row(updated_at=self.NOW - 100 * 3600)]
        text = W.render_read_no_reply(W.read_no_reply(rows, now_ms=self.NOW * 1000))
        self.assertIn("已读没回", text)
        self.assertIn("中软国际", text)
        self.assertIn("放弃", text)


if __name__ == "__main__":
    unittest.main()
