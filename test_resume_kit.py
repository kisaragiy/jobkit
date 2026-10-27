"""公司名归一化 + 简历多版本。

两件事都是「跨表对不上就全废」的类型：
- 公司名不做归一化，会话和投递永远交集 0（实测过，40 条会话 0 匹配）
- 简历不记版本，「哪份简历有用」永远只能靠感觉
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import resume_kit
import store


class TestCompanyKey(unittest.TestCase):
    def test_strips_legal_suffix(self):
        self.assertEqual(store.company_key("上海兴岩信息科技有限公司"), "上海兴岩")
        self.assertEqual(store.company_key("中软国际"), "中软国际")

    def test_strips_region_prefix(self):
        self.assertEqual(store.company_key("广州市趋势信息技术有限公司"), "趋势")

    def test_empty(self):
        self.assertEqual(store.company_key(""), "")
        self.assertEqual(store.company_key(None), "")

    def test_match_short_vs_long(self):
        self.assertTrue(store.companies_match(
            "上海兴岩信息科技有限公司", "上海兴岩"))
        self.assertTrue(store.companies_match(
            "亿达信息", "亿达信息技术有限公司"))

    def test_does_not_swallow_similar_names(self):
        """华为 ≠ 华为云 —— 错配会把统计搅乱，保守优先。"""
        self.assertFalse(store.companies_match("华为", "华为云"))
        self.assertFalse(store.companies_match("字节跳动", "华为技术有限公司"))

    def test_empty_never_matches(self):
        self.assertFalse(store.companies_match("", "中软国际"))
        self.assertFalse(store.companies_match("", ""))


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = store.DB_PATH
        store.DB_PATH = Path(self.tmp) / "t.db"
        self.c = store.connect()
        # 别让自动登记去碰真实简历文件
        self._oldpath = resume_kit.DEFAULT_RESUME_PATH
        resume_kit.DEFAULT_RESUME_PATH = Path(self.tmp) / "nope.md"

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old
        resume_kit.DEFAULT_RESUME_PATH = self._oldpath


class TestResumeKit(_DB):
    def _mkfile(self, name="r.md"):
        p = Path(self.tmp) / name
        p.write_text("# 简历", encoding="utf-8")
        return str(p)

    def test_add_and_activate(self):
        a = resume_kit.add(self.c, "后端版", "python", self._mkfile("a.md"))
        b = resume_kit.add(self.c, "AIGC版", "aigc", self._mkfile("b.md"))
        self.assertTrue(resume_kit.activate(self.c, a))
        self.assertEqual(resume_kit.active(self.c)["resume_id"], a)
        self.assertTrue(resume_kit.activate(self.c, b))
        self.assertEqual(resume_kit.active(self.c)["resume_id"], b)
        # 只能有一个 active
        n = self.c.execute("SELECT COUNT(*) FROM resumes WHERE active=1").fetchone()[0]
        self.assertEqual(n, 1)

    def test_add_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            resume_kit.add(self.c, "x", "python", str(Path(self.tmp) / "no.md"))

    def test_add_empty_name_raises(self):
        with self.assertRaises(ValueError):
            resume_kit.add(self.c, "  ", "python", self._mkfile())

    def test_activate_unknown_id(self):
        self.assertFalse(resume_kit.activate(self.c, 999))

    def test_stats_lists_unknown_bucket(self):
        """早期投递（resume_id=0）必须单列，不能摊给任何版本。"""
        self.c.execute(
            "INSERT INTO greetings (job_id, status, resume_id) VALUES (?,?,?)",
            ("j_old", "sent", 0))
        self.c.commit()
        rows = resume_kit.stats(self.c)
        unknown = [r for r in rows if r["resume_id"] == 0]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["sent"], 1)

    def test_greet_records_active_resume(self):
        rid = resume_kit.add(self.c, "后端版", "python", self._mkfile(),
                             make_active=True)
        store.record_greet(self.c, "j1", "s1", "你好", "sent")
        got = self.c.execute(
            "SELECT resume_id FROM greetings WHERE job_id='j1'").fetchone()[0]
        self.assertEqual(got, rid)

    def test_regreet_does_not_zero_resume(self):
        """重跑投递流程不该把已记的版本号冲成 0。"""
        rid = resume_kit.add(self.c, "后端版", "python", self._mkfile(),
                             make_active=True)
        store.record_greet(self.c, "j1", "s1", "你好", "sent")
        self.c.execute("UPDATE resumes SET active=0")
        self.c.commit()
        store.record_greet(self.c, "j1", "s1", "你好", "sent")   # 无 active 版本
        got = self.c.execute(
            "SELECT resume_id FROM greetings WHERE job_id='j1'").fetchone()[0]
        self.assertEqual(got, rid)

    def test_rate_hidden_when_sample_small(self):
        rid = resume_kit.add(self.c, "后端版", "python", self._mkfile(),
                             make_active=True)
        self.c.execute(
            "INSERT INTO greetings (job_id, status, resume_id) VALUES (?,?,?)",
            ("j1", "sent", rid))
        self.c.commit()
        row = [r for r in resume_kit.stats(self.c) if r["resume_id"] == rid][0]
        self.assertFalse(row["sample_enough"])


class TestLinkConversations(_DB):
    def test_links_by_normalized_company(self):
        self.c.execute("INSERT INTO jobs (job_id, title, company, match_score) "
                       "VALUES (?,?,?,?)",
                       ("j1", "Java开发-保险", "上海兴岩信息科技有限公司", 70))
        self.c.execute("INSERT INTO conversations (conv_key, company, last_msg) "
                       "VALUES (?,?,?)",
                       ("郁女士|上海兴岩信息科技", "上海兴岩信息科技", "在吗"))
        self.c.commit()
        got = store.link_conversations(self.c)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["job_id"], "j1")

    def test_unmatched_stays_empty(self):
        """对不上就留空，不要瞎猜一个岗位。"""
        self.c.execute("INSERT INTO conversations (conv_key, company, last_msg) "
                       "VALUES (?,?,?)", ("k", "某某不存在公司", "hi"))
        self.c.commit()
        got = store.link_conversations(self.c)
        self.assertEqual(got[0]["job_id"], "")


if __name__ == "__main__":
    unittest.main()
