"""台账快照兜底：jobs 表被覆盖后，仍能从 greetings.job_snapshot 认出「投的是谁」。

实测背景：28 条历史投递的 job_id 在 jobs 表里全查不到（岗位被新搜索覆盖），
导致 company 全空 → 定位不到聊天会话 → 跟进/已读未回/归因全断。
"""
from __future__ import annotations

import sqlite3
import unittest

import run_followup as RF
import store


def mem_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    store._migrate(c)
    return c


JOB = {"company": "四箐园", "title": "AI应用开发", "city": "广州",
       "salary": "8-12K", "url": "https://example.invalid/j/1"}


class SnapshotFallbackTest(unittest.TestCase):
    def test_company_from_snapshot_when_jobs_row_missing(self):
        c = mem_db()
        store.record_greet(c, "j1", "sec1", "您好", "sent", job=JOB)
        # 故意不往 jobs 插行 —— 模拟岗位被新搜索覆盖
        rows = RF.load_greeted_rows(conn=c)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "四箐园")
        self.assertEqual(rows[0]["title"], "AI应用开发")
        self.assertEqual(rows[0]["city"], "广州")
        c.close()

    def test_jobs_table_wins_over_snapshot(self):
        c = mem_db()
        store.record_greet(c, "j1", "sec1", "您好", "sent", job=JOB)
        c.execute("INSERT INTO jobs (job_id, company, title, city) VALUES (?,?,?,?)",
                  ("j1", "新公司名", "新岗位", "深圳"))
        rows = RF.load_greeted_rows(conn=c)
        self.assertEqual(rows[0]["company"], "新公司名", "jobs 表是准的，优先它")
        c.close()

    def test_broken_snapshot_does_not_explode(self):
        """快照是脏数据（非 JSON）时不能把整轮跟进搞崩。"""
        c = mem_db()
        c.execute("INSERT INTO greetings (job_id, security_id, greeted_at, message, status, "
                  "job_snapshot) VALUES (?,?,?,?,?,?)",
                  ("j9", "sec9", 1700000000.0, "您好", "sent", "{不是 json"))
        rows = RF.load_greeted_rows(conn=c)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["company"])
        c.close()

    def test_snapshot_only_written_for_sent(self):
        """failed/preview 不该占着 greeted_at，但快照仍可留作排查。"""
        c = mem_db()
        store.record_greet(c, "j2", "sec2", "您好", "failed", "boom", job=JOB)
        row = c.execute("SELECT greeted_at, job_snapshot FROM greetings WHERE job_id='j2'").fetchone()
        self.assertIsNone(row["greeted_at"])
        self.assertIn("四箐园", row["job_snapshot"])
        c.close()


if __name__ == "__main__":
    unittest.main()
