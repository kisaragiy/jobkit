"""已读状态入库测试 —— B2「已读未回」的地基。

最关键的回归：**消息内容没变、只有已读状态变了**，也必须写进库。
以前 hash 相同就只刷新 updated_at，已读状态永远进不了库。
"""
from __future__ import annotations

import sqlite3
import unittest

import chat_sync
import store


def mem_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    store._migrate(c)
    return c


def conv(**kw) -> dict:
    base = {
        "conv_key": "庞女士|中软国际",
        "hr": "庞女士",
        "company": "中软国际",
        "position": "AI应用开发",
        "last_msg": "您好，想确认下岗位进展",
        "status": "",
        "time": "10:30",
        "avatar": "",
        "from_me": True,
        "category": "normal",
        "hash": "abc123",
        "hr_read": 0,
    }
    base.update(kw)
    return base


class NormalizeTest(unittest.TestCase):
    def test_read_marker_sets_hr_read(self):
        rows = chat_sync.normalize([{"hr": "庞女士", "company": "中软国际",
                                    "last_msg": "您好", "status": "[已读]"}])
        self.assertEqual(rows[0]["hr_read"], 1)
        self.assertTrue(rows[0]["from_me"])

    def test_no_status_means_unread(self):
        rows = chat_sync.normalize([{"hr": "庞女士", "company": "中软国际",
                                    "last_msg": "您好", "status": ""}])
        self.assertEqual(rows[0]["hr_read"], 0)

    def test_hr_message_has_no_read_flag(self):
        """对方发的消息不会有「已读」标记（那是给我看的我方才有的）。"""
        rows = chat_sync.normalize([{"hr": "庞女士", "company": "中软国际",
                                    "last_msg": "方便发份简历吗", "status": ""}])
        self.assertFalse(rows[0]["from_me"])
        self.assertEqual(rows[0]["hr_read"], 0)


class UpsertReadStateTest(unittest.TestCase):
    def test_schema_has_hr_read(self):
        c = mem_db()
        cols = {r[1] for r in c.execute("PRAGMA table_info(conversations)")}
        self.assertIn("hr_read", cols)
        c.close()

    def test_read_flag_persisted_on_insert(self):
        c = mem_db()
        store.upsert_conversations(c, [conv(hr_read=1)], now=1000.0)
        row = c.execute("SELECT hr_read FROM conversations").fetchone()
        self.assertEqual(row["hr_read"], 1)
        c.close()

    def test_read_flag_updates_when_hash_unchanged(self):
        """核心回归：我发消息 → 对方读了。内容没变，但已读必须入库。"""
        c = mem_db()
        store.upsert_conversations(c, [conv(hr_read=0)], now=1000.0)
        before = c.execute("SELECT hr_read, handled FROM conversations").fetchone()
        self.assertEqual(before["hr_read"], 0)

        # 同一 hash、同一内容，只有已读状态变了
        store.upsert_conversations(c, [conv(hr_read=1)], now=2000.0)
        after = c.execute("SELECT hr_read, msg_hash, handled FROM conversations").fetchone()
        self.assertEqual(after["hr_read"], 1, "已读状态没写进库 —— 已读未回永远判不出来")
        self.assertEqual(after["msg_hash"], "abc123")
        c.close()

    def test_unchanged_update_does_not_reset_handled(self):
        """刷新已读状态不能把用户标记过的「已处理」冲掉。"""
        c = mem_db()
        store.upsert_conversations(c, [conv(hr_read=0)], now=1000.0)
        c.execute("UPDATE conversations SET handled=1")
        store.upsert_conversations(c, [conv(hr_read=1)], now=2000.0)
        row = c.execute("SELECT handled FROM conversations").fetchone()
        self.assertEqual(row["handled"], 1)
        c.close()

    def test_hr_read_at_set_on_transition(self):
        """已读时刻只在 0→1 那一刻记一次，后续抓取不能把它刷成「现在」。"""
        c = mem_db()
        store.upsert_conversations(c, [conv(hr_read=0)], now=1000.0)
        self.assertIsNone(c.execute("SELECT hr_read_at FROM conversations").fetchone()[0])

        store.upsert_conversations(c, [conv(hr_read=1)], now=2000.0)   # 这一刻变已读
        at1 = c.execute("SELECT hr_read_at FROM conversations").fetchone()[0]
        self.assertEqual(at1, 2000.0)

        store.upsert_conversations(c, [conv(hr_read=1)], now=9000.0)   # 之后再抓
        at2 = c.execute("SELECT hr_read_at FROM conversations").fetchone()[0]
        self.assertEqual(at2, 2000.0, "已读时刻被后续抓取覆盖了 —— 分档会永远显示「刚读到」")

    def test_updated_at_not_bumped_when_nothing_changed(self):
        """消息没变就不该刷新 updated_at —— 否则「最后消息时间」失去意义。"""
        c = mem_db()
        store.upsert_conversations(c, [conv(hr_read=0)], now=1000.0)
        store.upsert_conversations(c, [conv(hr_read=0)], now=5000.0)
        row = c.execute("SELECT updated_at FROM conversations").fetchone()
        self.assertEqual(row["updated_at"], 1000.0)

    def test_migrate_is_idempotent(self):
        c = mem_db()
        store._migrate(c)   # 再跑一次不能报错
        store._migrate(c)
        c.close()


if __name__ == "__main__":
    unittest.main()
