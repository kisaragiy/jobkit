"""投递记录岗位快照 —— 台账里不再出现「岗位信息缺失」。

背景：早期 28 条投递只存了 job_id。岗位被新搜索结果覆盖后，jobs 表里查
不到，台账就只剩一个占位符。现在投递时把岗位信息快照进 greetings 表。
"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import ledger
import store


class TestSnapshotWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = store.DB_PATH
        store.DB_PATH = Path(self.tmp) / "t.db"
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old

    def test_snapshot_keeps_display_fields(self):
        job = {"job_id": "j1", "title": "Python 后端", "company": "某公司",
               "salary": "10-15K", "city": "广州", "url": "https://x/1",
               "job_description": "很长的 JD" * 100}
        snap = json.loads(store._snapshot(job))
        self.assertEqual(snap["title"], "Python 后端")
        self.assertEqual(snap["company"], "某公司")
        self.assertIn("url", snap)
        # JD 体积大、台账不看，不该进快照
        self.assertNotIn("job_description", snap)

    def test_snapshot_drops_empty(self):
        snap = json.loads(store._snapshot({"job_id": "j", "title": "T",
                                           "company": "", "city": None}))
        # job_id 由 greetings 表自己存，不进快照
        self.assertEqual(snap, {"title": "T"})

    def test_snapshot_none_for_no_job(self):
        self.assertIsNone(store._snapshot(None))
        self.assertIsNone(store._snapshot({}))

    def test_record_greet_writes_snapshot(self):
        store.record_greet(self.c, "j1", "s1", "hi", "sent",
                           job={"job_id": "j1", "title": "岗位A", "company": "公司A"})
        row = self.c.execute(
            "SELECT job_snapshot FROM greetings WHERE job_id='j1'").fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(json.loads(row[0])["title"], "岗位A")

    def test_snapshot_not_lost_on_reregister(self):
        """重跑投递流程时没带 job，不能把已存的快照抹掉。"""
        store.record_greet(self.c, "j1", "s1", "hi", "sent",
                           job={"job_id": "j1", "title": "岗位A"})
        store.record_greet(self.c, "j1", "s1", "hi", "failed", "超时")
        row = self.c.execute(
            "SELECT job_snapshot, status FROM greetings WHERE job_id='j1'").fetchone()
        self.assertEqual(row[1], "failed")
        self.assertEqual(json.loads(row[0])["title"], "岗位A")

    def test_migrate_adds_column_to_old_db(self):
        """老库（没有 job_snapshot 列）连上来要自动补列。

        造法：先用当前 schema 建库，再把列 DROP 掉 —— 手写一份老 schema
        容易和 SCHEMA 里的索引对不上（试过，sqlite 会报 no such column）。
        """
        old = Path(self.tmp) / "old.db"
        self._old2 = store.DB_PATH
        store.DB_PATH = old
        try:
            c = store.connect()
            try:
                c.execute("ALTER TABLE greetings DROP COLUMN job_snapshot")
                c.commit()
            except sqlite3.OperationalError as e:
                self.skipTest(f"该 SQLite 不支持 DROP COLUMN: {e}")
            c.close()
            cols = {r[1] for r in store.connect().execute("PRAGMA table_info(greetings)")}
            self.assertIn("job_snapshot", cols)
        finally:
            store.DB_PATH = self._old2


class TestLedgerRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = store.DB_PATH
        store.DB_PATH = Path(self.tmp) / "t.db"
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old

    def _greet(self, jid, snap=None):
        self.c.execute(
            "INSERT INTO greetings (job_id, security_id, greeted_at, message, status, job_snapshot)"
            " VALUES (?,?,?,?,?,?)",
            (jid, "s", time.time(), "hi", "sent",
             json.dumps(snap, ensure_ascii=False) if snap else None))
        self.c.commit()

    def test_snapshot_used_when_job_gone(self):
        """岗位不在 jobs 表里，靠快照补出岗位名。"""
        self._greet("j1", {"title": "岗位A", "company": "公司A", "salary": "10-15K"})
        d = ledger.collect(self.c)
        self.assertEqual(len(d["applied"]), 1)
        self.assertEqual(d["applied"][0]["title"], "岗位A")
        self.assertEqual(d["unknown_applied"], [])

    def test_live_job_overrides_snapshot(self):
        """jobs 表里还有这条岗位时，用最新的（薪资可能变了）。"""
        self.c.execute(
            "INSERT INTO jobs (job_id, title, company, salary) VALUES ('j1','旧名','公司','8-9K')")
        self._greet("j1", {"title": "旧名", "company": "公司", "salary": "8-9K"})
        self.c.execute("UPDATE jobs SET salary='12-18K' WHERE job_id='j1'")
        self.c.commit()
        d = ledger.collect(self.c)
        self.assertEqual(d["applied"][0]["salary"], "12-18K")

    def test_no_info_goes_to_unknown_group(self):
        """既没快照、jobs 表也没有 → 归到 unknown，不再逐条占位。"""
        self._greet("j1")
        self._greet("j2")
        d = ledger.collect(self.c)
        self.assertEqual(d["applied"], [])
        self.assertEqual(len(d["unknown_applied"]), 2)

    def test_broken_snapshot_does_not_crash(self):
        """快照是坏 JSON 时不能把整个台账搞崩。"""
        self.c.execute(
            "INSERT INTO greetings (job_id, security_id, greeted_at, message, status, job_snapshot)"
            " VALUES ('j1','s',?, 'hi','sent','{不是JSON')", (time.time(),))
        self.c.commit()
        d = ledger.collect(self.c)
        self.assertEqual(len(d["unknown_applied"]), 1)

    def test_text_render_has_no_per_row_placeholder(self):
        """文本看板不该再出现每条一句「岗位信息缺失」。"""
        self._greet("j1")
        d = ledger.collect(self.c)
        text = ledger.render_text(d, 5)
        self.assertNotIn("(岗位信息缺失，该岗不在搜索缓存中)", text)
        self.assertIn("早期投递只存了 job_id", text)

    def test_html_render_mentions_unknown(self):
        self._greet("j1")
        d = ledger.collect(self.c)
        html = ledger.render_html(d, 5)
        self.assertIn("早期投递只存了 job_id", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
