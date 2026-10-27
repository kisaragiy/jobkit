"""待处理时长（age_hours）与 pending_since 重置。

防的错：对方又发新消息时如果沿用旧 pending_since，界面会显示
「挂起 3 天」，把刚来的消息说成放了三天 —— 提醒直接失效。
"""
import tempfile
import unittest
from pathlib import Path

import store


def conv(key, msg="你好", from_me=False, cat="normal"):
    return {"conv_key": key, "hr": "HR", "company": "某公司", "position": "后端",
            "last_msg": msg, "hash": f"h_{key}_{msg}", "category": cat,
            "from_me": 1 if from_me else 0, "time": "刚刚"}


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = store.DB_PATH
        store.DB_PATH = Path(self.tmp) / "t.db"
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old


class TestPendingAge(_DB):
    def test_new_pending_has_age(self):
        store.upsert_conversations(self.c, [conv("a")], now=1000.0)
        rows = store.list_conversations(self.c)
        self.assertIsNotNone(rows[0]["age_hours"])
        self.assertGreaterEqual(rows[0]["age_hours"], 0)

    def test_my_message_has_no_age(self):
        """最后一句是我说的 → 不算待处理，不该有挂起时长。"""
        store.upsert_conversations(self.c, [conv("a", from_me=True)], now=1000.0)
        rows = store.list_conversations(self.c)
        self.assertIsNone(rows[0]["age_hours"])

    def test_new_reply_resets_pending_since(self):
        store.upsert_conversations(self.c, [conv("a", msg="第一条")], now=1000.0)
        first = self.c.execute(
            "SELECT pending_since FROM conversations WHERE conv_key='a'").fetchone()[0]
        self.assertEqual(first, 1000.0)

        # 一天后对方又发了一条 —— pending_since 必须跟着新消息走
        store.upsert_conversations(self.c, [conv("a", msg="第二条")], now=1000.0 + 86400)
        second = self.c.execute(
            "SELECT pending_since FROM conversations WHERE conv_key='a'").fetchone()[0]
        self.assertEqual(second, 1000.0 + 86400,
                         "新消息没重置 pending_since → 界面会显示挂起 1 天，实际刚来")

    def test_handled_flag_cleared_on_new_reply(self):
        store.upsert_conversations(self.c, [conv("a", msg="旧")], now=1000.0)
        self.c.execute("UPDATE conversations SET handled=1 WHERE conv_key='a'")
        self.c.commit()
        store.upsert_conversations(self.c, [conv("a", msg="新")], now=2000.0)
        h = self.c.execute(
            "SELECT handled FROM conversations WHERE conv_key='a'").fetchone()[0]
        self.assertEqual(h, 0)

    def test_unchanged_keeps_handled(self):
        """消息没变时刷新 updated_at，但不能把用户标的「已处理」冲掉。"""
        store.upsert_conversations(self.c, [conv("a", msg="同一条")], now=1000.0)
        self.c.execute("UPDATE conversations SET handled=1 WHERE conv_key='a'")
        self.c.commit()
        store.upsert_conversations(self.c, [conv("a", msg="同一条")], now=2000.0)
        h = self.c.execute(
            "SELECT handled FROM conversations WHERE conv_key='a'").fetchone()[0]
        self.assertEqual(h, 1)

    def test_reply_after_handled_becomes_pending(self):
        """已处理过，对方又发新消息 → 重新变成待处理。"""
        store.upsert_conversations(self.c, [conv("a", msg="旧")], now=1000.0)
        self.c.execute("UPDATE conversations SET handled=1 WHERE conv_key='a'")
        self.c.commit()
        st = store.upsert_conversations(self.c, [conv("a", msg="新")], now=3000.0)
        self.assertEqual(st["new_replies"], 1)
        self.assertEqual(st["pending"], 1)


if __name__ == "__main__":
    unittest.main()
