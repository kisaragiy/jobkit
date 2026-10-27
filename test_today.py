"""每日指挥中心：只读库、不抓取；系统消息不混进「等你回」；退出码符合契约。"""
from __future__ import annotations

import sqlite3
import unittest

import store
import today as T

NOW = 1784000000          # 秒


def mem_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    store._migrate(c)
    return c


def conv(key, hr, company, msg, *, from_me=0, handled=0, hr_read=0, ts=None):
    store_now = ts if ts is not None else NOW - 3600
    return {"conv_key": key, "hr_name": hr, "company": company, "position": "AI应用开发",
            "last_msg": msg, "msg_hash": f"h-{key}", "category": "normal",
            "from_me": 1 if from_me else 0, "last_time": "10:00", "avatar": "",
            "hr_read": hr_read, "hr_read_at": store_now if hr_read else None,
            "_ts": store_now, "_handled": handled}


def seed(c, rows):
    for r in rows:
        store.upsert_conversations(c, [{
            "conv_key": r["conv_key"], "hr_name": r["hr_name"], "company": r["company"],
            "position": r["position"], "last_msg": r["last_msg"], "msg_hash": r["msg_hash"],
            "category": r["category"], "from_me": r["from_me"], "last_time": r["last_time"],
            "avatar": r["avatar"], "hr_read": r["hr_read"], "hr_read_at": r["hr_read_at"],
        }], now=r["_ts"])
        if r["_handled"]:
            c.execute("UPDATE conversations SET handled=1 WHERE conv_key=?", (r["conv_key"],))
    c.commit()


class BuildTest(unittest.TestCase):
    def test_system_message_not_counted_as_pending(self):
        """系统回执不该算「HR 在等你」——否则每天早上都白焦虑一次。"""
        c = mem_db()
        seed(c, [
            conv("a", "庞女士", "中软国际", "你在深圳吗 可以现场面试吗"),
            conv("b", "马先生", "枫季林", "对方已同意，您的附件简历已发送给对方"),
            conv("c", "仇健", "微创软件", "张三-AI应用开发.pdf"),
        ])
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertEqual(len(d["pending"]), 1)
        self.assertEqual(d["pending"][0]["company"], "中软国际")
        self.assertEqual(d["ignorable"], 2)
        c.close()

    def test_read_no_reply_counted(self):
        c = mem_db()
        seed(c, [
            conv("x", "付先生", "点点点", "你好，贵公司还招人吗？",
                 from_me=1, hr_read=1, ts=NOW - 100 * 3600),
            conv("y", "刘女士", "亿泽", "你好，贵公司还招人吗？",
                 from_me=1, hr_read=1, ts=NOW - 100 * 3600),
        ])
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertEqual(len(d["read_no_reply"]), 2)
        c.close()

    def test_handled_not_pending(self):
        c = mem_db()
        seed(c, [conv("h", "庞女士", "中软国际", "你在深圳吗", handled=1)])
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertEqual(d["pending"], [])
        c.close()

    def test_scam_in_chat_is_flagged(self):
        """JD 干净但聊天里要钱 —— 这才是骗局真正的发生方式。"""
        c = mem_db()
        seed(c, [conv("s", "王经理", "某科技",
                      "面试通过了，麻烦先去指定医院体检，费用285元自理再联系我")])
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertTrue(d["pending"][0].get("scam"), d["pending"][0])
        out = T.render(d)
        self.assertIn("在要钱", out)
        c.close()

    def test_clean_chat_not_flagged(self):
        c = mem_db()
        seed(c, [conv("n", "庞女士", "中软国际", "你在深圳吗 可以现场面试吗")])
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertNotIn("scam", d["pending"][0])
        self.assertNotIn("在要钱", T.render(d))
        c.close()

    def test_sent_today_uses_local_midnight(self):
        """北京时间早上 8 点前，昨天的投递不能算今天。

        `now - now%86400` 是 UTC 日界 —— 北京时间要等到早上 8:00 才换日，
        所以 21 号早上 7 点会把 20 号的投递算成「今日已投」。
        """
        import datetime
        now_dt = datetime.datetime(2026, 10, 21, 7, 0, 0)
        now_s = now_dt.timestamp()
        yest_23 = (now_dt - datetime.timedelta(hours=8)).timestamp()   # 20 号 23:00
        c = mem_db()
        c.execute("INSERT INTO greetings (job_id, greeted_at, status) VALUES ('a', ?, 'sent')",
                  (yest_23,))
        c.execute("INSERT INTO greetings (job_id, greeted_at, status) VALUES ('b', ?, 'sent')",
                  (now_s - 60,))
        c.commit()
        d = T.build(conn=c, now_ms=int(now_s * 1000))
        self.assertEqual(d["sent_today"], 1)
        c.close()

    def test_failed_today_uses_attempted_at(self):
        """失败时 greeted_at 是 NULL —— 统计必须看 attempted_at，否则永远数不到。

        2026-10-21 实测：日志写「失败 3」，库里一条都查不到，因为
        record_greet 对非 sent 状态把 greeted_at 写成 NULL，按时间过滤全被排除。
        """
        c = mem_db()
        c.execute("INSERT INTO greetings (job_id, status, attempted_at) VALUES ('jf','failed',?)",
                  (NOW,))
        c.commit()
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertEqual(d["failed_today"], 1)
        c.close()

    def test_old_failure_without_attempted_at_not_counted(self):
        """历史记录没有 attempted_at → 不能当成今天失败（宁少数不虚报）。"""
        c = mem_db()
        c.execute("INSERT INTO greetings (job_id, status) VALUES ('old','failed')")
        c.commit()
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertEqual(d["failed_today"], 0)
        c.close()

    def test_empty_db_does_not_error(self):
        c = mem_db()
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertEqual(d["errors"], [])
        self.assertEqual(d["pending"], [])
        c.close()


class DecideTest(unittest.TestCase):
    def test_pending_is_alert(self):
        self.assertEqual(T.decide({"pending": [1], "errors": []}), T.EXIT_ALERT)

    def test_nothing_is_ok(self):
        self.assertEqual(T.decide({"pending": [], "errors": []}), T.EXIT_OK)

    def test_errors_beat_everything(self):
        self.assertEqual(T.decide({"pending": [], "errors": ["boom"]}), T.EXIT_ERROR)


class RenderTest(unittest.TestCase):
    def test_includes_all_four_sections(self):
        d = {"now": NOW, "pending": [], "read_no_reply": [], "due": [],
             "sent_today": 3, "jobs_total": 657, "ignorable": 0, "errors": []}
        out = T.render(d)
        for mark in ("①", "②", "③", "④"):
            self.assertIn(mark, out)

    def test_repeated_message_called_out(self):
        """同一句话被多个人已读不回，要说成话术问题，不是运气。"""
        rows = [{"hr_name": "A", "company": "甲公司", "tier": "fresh", "age_hours": 1,
                 "last_msg": "你好，贵公司还招人吗？"},
                {"hr_name": "B", "company": "乙公司", "tier": "fresh", "age_hours": 1,
                 "last_msg": "你好，贵公司还招人吗？"}]
        d = {"now": NOW, "pending": [], "read_no_reply": rows, "due": [],
             "sent_today": 0, "jobs_total": 0, "ignorable": 0, "errors": []}
        out = T.render(d)
        self.assertIn("同一句话", out)
        self.assertIn("不在运气", out)

    def test_errors_surface_in_output(self):
        d = {"now": NOW, "pending": [], "read_no_reply": [], "due": [],
             "sent_today": 0, "jobs_total": 0, "ignorable": 0,
             "errors": ["读会话表失败：db locked"]}
        self.assertIn("db locked", T.render(d))


class FreshnessTest(unittest.TestCase):
    def test_fresh_data_says_how_long_ago(self):
        """数据是新的也要说清多久前同步的 —— 让人知道自己在看什么。"""
        line = T.freshness_line({"conv_age_hours": 0.5, "conv_total": 67})
        self.assertIn("30 分钟前", line)
        self.assertNotIn("⚠️", line)

    def test_stale_data_warns_and_tells_how_to_refresh(self):
        """拿 7 小时前的数据说「有 HR 在等你回」，比不做还糟。"""
        line = T.freshness_line({"conv_age_hours": 7.3, "conv_total": 67})
        self.assertIn("⚠️", line)
        self.assertIn("7.3 小时", line)
        self.assertIn("jobkit.py chat", line)

    def test_no_data_tells_how_to_start(self):
        line = T.freshness_line({"conv_age_hours": None})
        self.assertIn("还没有会话数据", line)
        self.assertIn("jobkit.py chat", line)

    def test_build_reports_age_from_max_updated_at(self):
        c = mem_db()
        seed(c, [conv("a", "庞女士", "中软国际", "你在深圳吗", ts=NOW - 3 * 3600)])
        d = T.build(conn=c, now_ms=NOW * 1000)
        self.assertAlmostEqual(d["conv_age_hours"], 3.0, places=2)
        self.assertIn("3.0 小时", T.render(d))
        c.close()

    def test_render_puts_freshness_before_todo(self):
        """新鲜度必须在第一条待办之前 —— 否则数字读起来像当前的。"""
        d = {"now": NOW, "pending": [], "read_no_reply": [], "due": [],
             "sent_today": 0, "jobs_total": 0, "ignorable": 0, "errors": [],
             "conv_age_hours": 7.0, "conv_total": 67}
        out = T.render(d)
        self.assertLess(out.index("会话数据是"), out.index("①"))


class SyncErrorTest(unittest.TestCase):
    def test_sync_error_does_not_change_exit_code(self):
        """同步失败只是通道没开，库里数据照样能看 —— 不能报 ERROR。"""
        d = {"pending": [], "errors": [], "sync_error": "⚠️ 同步失败（浏览器没开）"}
        self.assertEqual(T.decide(d), T.EXIT_OK)

    def test_sync_error_is_shown(self):
        d = {"now": NOW, "pending": [], "read_no_reply": [], "due": [],
             "sent_today": 0, "jobs_total": 0, "ignorable": 0, "errors": [],
             "conv_age_hours": 1.0, "conv_total": 67,
             "sync_error": "⚠️ 同步失败（浏览器没开）"}
        self.assertIn("同步失败", T.render(d))


if __name__ == "__main__":
    unittest.main()
