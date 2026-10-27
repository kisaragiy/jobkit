#!/usr/bin/env python3
"""feed_external 的单元测试。

跑法（临时库，不碰真库）：
    cd C:/Users/<user>/jobkit
    python -m unittest test_feed_external
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import feed_external as fe  # noqa: E402
import store  # noqa: E402


class TestCleanTitle(unittest.TestCase):
    def test_zhaopin_single_job(self):
        t = "大模型应用开发工程师招聘_广州市点易资讯科技有限公司招聘 - 智联招聘"
        self.assertEqual(fe.clean_title(t), "大模型应用开发工程师")

    def test_51job_wrapped(self):
        t = ("【广州-天河区AI应用开发工程师-广州-27届校招(15k-25k)(J10788)招聘_"
             "AI应用开发工程师-广州-27届校招(15k-25k)(J10788)】-广州-天河区前程无忧")
        self.assertEqual(fe.clean_title(t), "AI应用开发工程师")

    def test_liepin_agent(self):
        t = "【深圳 AI agent开发招聘】-猎头顾问深圳招聘信息-猎聘"
        # 忠于平台原文（猎聘写的是小写 agent）
        self.assertEqual(fe.clean_title(t), "AI agent开发")

    def test_generic_cleanup(self):
        t = "招聘官网-职位详情"
        # 关键词表无命中，兜底清理后不应为空且不含平台名
        out = fe.clean_title(t)
        self.assertIn("招聘官网", out)

    def test_longest_match_wins(self):
        # 「AI应用开发工程师」在更左的位置，但「大模型应用开发工程师」更长 → 取长词
        t = "大模型应用开发工程师（AI应用开发方向）招聘"
        self.assertEqual(fe.clean_title(t), "大模型应用开发工程师")

    def test_left_but_no_longer(self):
        # 原文没有完整的「大模型应用开发工程师」，最左的「AI应用开发工程师」最长 → 返回它
        t = "AI应用开发工程师（大模型应用开发方向）"
        self.assertEqual(fe.clean_title(t), "AI应用开发工程师")


class TestExtractSalary(unittest.TestCase):
    def test_k_range(self):
        self.assertEqual(fe.extract_salary("AI agent开发 50-80k·16薪"), "50-80k")

    def test_wan_range(self):
        self.assertEqual(fe.extract_salary("大模型应用开发工程师 25-35k"), "25-35k")

    def test_yuan_month(self):
        self.assertEqual(fe.extract_salary("AI应用开发工程师 2000-4000元/月"), "2000-4000元/月")

    def test_no_salary(self):
        self.assertEqual(fe.extract_salary("没有薪资的岗位"), "")


class TestExtractCity(unittest.TestCase):
    def test_district(self):
        city, dist = fe.extract_city("广州-天河区 | 本科 | 招10人", "广州")
        self.assertEqual(city, "广州")
        self.assertEqual(dist, "天河区")

    def test_hint_fallback(self):
        city, dist = fe.extract_city("没有城市信息的描述", "东莞")
        self.assertEqual(city, "东莞")
        self.assertEqual(dist, "")


class TestExtractMisc(unittest.TestCase):
    def test_experience(self):
        self.assertEqual(fe.extract_experience("深圳 1-8年 统招本科 招1人"), "1-8年")
        self.assertEqual(fe.extract_experience("在校生/应届生 | 招10人"), "在校生/应届生")
        self.assertEqual(fe.extract_experience("无"), "")

    def test_experience_year_not_swallowed(self):
        # 「2026年」是年份不是经验（踩过的坑）
        self.assertEqual(fe.extract_experience("【2026年广州AI招聘信息】 3-5年"), "3-5年")
        self.assertEqual(fe.extract_experience("招聘信息 2026年10月"), "")

    def test_district_not_title_garbage(self):
        # 「广州-天河区」后面紧跟标题垃圾时，区名不能把垃圾吞进来
        city, dist = fe.extract_city("广州-天河区AI应用开发工程师-广州-27届校招招聘", "广州")
        self.assertEqual(city, "广州")
        self.assertEqual(dist, "天河区")
        city, dist = fe.extract_city("东莞-东坑镇 | 大专", "东莞")
        self.assertEqual(dist, "东坑镇")

    def test_education(self):
        self.assertEqual(fe.extract_education("统招本科 招1人"), "统招本科")
        self.assertEqual(fe.extract_education("本科及以上学历"), "本科及以上")

    def test_skills(self):
        s = fe.extract_skills("熟悉Python、FastAPI，掌握Dify和vLLM部署")
        self.assertIn("Python", s)
        self.assertIn("Dify", s)
        self.assertIn("vLLM", s)

    def test_job_id_stable(self):
        u = "https://m.zhaopin.com/jobs/CC299508210J41020307804.htm"
        self.assertEqual(fe.job_id_from_url(u), fe.job_id_from_url(u))
        self.assertNotEqual(fe.job_id_from_url(u), fe.job_id_from_url(u + "?x=1"))


class TestParseRecord(unittest.TestCase):
    def test_no_url_is_none(self):
        self.assertIsNone(fe.parse_record({"title": "x", "snippet": "y"}))

    def test_full_parse(self):
        rec = {
            "title": "【深圳 AI agent开发招聘】-猎头顾问深圳招聘信息-猎聘",
            "host": "猎聘",
            "url": "https://www.liepin.com/a/80151993.shtml",
            "snippet": "# AI agent开发 50-80k·16薪\n深圳 1-8年 统招本科 招1人",
            "publish_time": "2026-10-20",
        }
        j = fe.parse_record(rec, "深圳")
        self.assertEqual(j["title"], "AI agent开发")
        self.assertEqual(j["salary"], "50-80k")
        self.assertEqual(j["city"], "深圳")
        self.assertEqual(j["experience"], "1-8年")
        self.assertEqual(j["education"], "统招本科")
        self.assertEqual(j["platform"], "猎聘")
        self.assertTrue(j["url"].startswith("https"))
        self.assertEqual(j["security_id"], "")
        self.assertIsNone(j["match_score"])


class TestImportFlow(unittest.TestCase):
    """monkey-patch store.DB_PATH 指向临时库（JOBKIT_DB 环境变量在 import 时
    就已固化到 store.DB_PATH，测试里改环境变量不生效——这是踩过的坑）。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self._old_db = store.DB_PATH
        store.DB_PATH = Path(self.db_path)

    def tearDown(self):
        store.DB_PATH = self._old_db
        if Path(self.db_path).exists():
            Path(self.db_path).unlink()

    def test_upsert_and_dedupe(self):
        raw = {
            "feed_time": "2026-10-21T22:30:00",
            "queries": [
                {"query": "AI应用开发 招聘 广州", "city_hint": "广州", "results": [
                    {"title": "大模型应用开发工程师招聘_广州点易公司招聘 - 智联招聘",
                     "host": "智联招聘",
                     "url": "https://m.zhaopin.com/jobs/A.htm",
                     "snippet": "广州-番禺区 本科 3-5年 15-25k\n熟悉大模型"},
                    {"title": "无 url 记录", "host": "猎聘",
                     "url": "", "snippet": "无"},
                ]},
            ],
        }
        jobs, fails = fe.parse_all(raw)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(fails), 1)

        c = store.connect()
        now = 1000.0
        self.assertTrue(store.upsert_job(c, jobs[0], fe.SOURCE_TAG, now))
        c.commit()
        row = c.execute("SELECT * FROM jobs WHERE job_id=?", (jobs[0]["job_id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["platform"], "智联")
        self.assertEqual(row["title"], "大模型应用开发工程师")
        self.assertEqual(row["sources"], fe.SOURCE_TAG)
        self.assertEqual(row["job_id"], jobs[0]["job_id"])

        # 同 url 再导 → 不新增，仅更新 last_seen
        again, _ = fe.dedupe_against_db(c, [jobs[0]])
        self.assertEqual(len(again), 0)
        total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(total, 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
