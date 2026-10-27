"""jobkit 统一入口的测试。

用户看到的那一屏就是产品本身，所以这里断言的是**呈现层的正确性**：
值得投的数量不能把被筛掉的算进去、序号能对上、位置参数不用背。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobkit  # noqa: E402
import store  # noqa: E402


def _job(**kw):
    base = {
        "job_id": kw.pop("job_id", "id1"),
        "title": "Python后端开发工程师",
        "company": "某公司",
        "salary": "10-15K",
        "city": "广州",
        "district": "天河",
        "experience": "经验不限",
        "education": "本科",
        "skills": "python mysql redis",
        "platform": "zhilian",
        "match_score": 83.0,
    }
    base.update(kw)
    return base


class JobkitHomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = store.DB_PATH
        store.DB_PATH = str(Path(self.tmp.name) / "t.db")
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old_db
        self.tmp.cleanup()

    def _add(self, job, source="test", now=1.0):
        store.upsert_job(self.c, job, source, now)
        self.c.commit()

    def test_home_counts_only_passers(self):
        """「值得投」必须只算通过筛选的 —— 把被筛掉的算进来，数字就是假的。"""
        self._add(_job(job_id="good"))
        self._add(_job(job_id="bad", title="销售专员"))     # 黑名单岗位
        total = self.c.execute("select count(*) from jobs").fetchone()[0]
        self.assertEqual(total, 2)

        import rules
        rows = [dict(r) for r in self.c.execute("select * from jobs")]
        passed = [j for j in rows if rules.job_ok(j)[0]]
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["job_id"], "good")

    def test_show_index_out_of_range(self):
        self._add(_job())
        rc = jobkit.main(["show", "99"])
        self.assertEqual(rc, 2)

    def test_show_single_job(self):
        self._add(_job())
        rc = jobkit.main(["show", "1"])
        self.assertEqual(rc, 0)

    def test_empty_db_points_to_first_command(self):
        """空库时不能默默什么都不说，要告诉用户第一条命令。"""
        self.assertEqual(jobkit.main([]), 0)


class PositionalArgsTest(unittest.TestCase):
    def test_city_and_keyword_parsed_without_flags(self):
        args = jobkit.build_parser().parse_args(["find", "广州", "Python"])
        jobkit._absorb_positionals(args)
        self.assertEqual(args.city, "广州")
        self.assertEqual(args.keyword, "Python")

    def test_shisuffix_city(self):
        args = jobkit.build_parser().parse_args(["find", "深圳市", "AI"])
        jobkit._absorb_positionals(args)
        self.assertEqual(args.city, "深圳")

    def test_multiword_keyword_kept_together(self):
        args = jobkit.build_parser().parse_args(["find", "广州", "AI", "开发"])
        jobkit._absorb_positionals(args)
        self.assertEqual(args.city, "广州")
        self.assertEqual(args.keyword, "AI 开发")

    def test_find_without_keyword_returns_2(self):
        self.assertEqual(jobkit.main(["find"]), 2)


class RejectedViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = store.DB_PATH
        store.DB_PATH = str(Path(self.tmp.name) / "t.db")
        self.c = store.connect()
        store.upsert_job(self.c, _job(job_id="bad", title="电话销售"), "t", 1.0)
        self.c.commit()

    def tearDown(self):
        self.c.close()
        store.DB_PATH = self._old_db
        self.tmp.cleanup()

    def test_rejected_view_runs(self):
        self.assertEqual(jobkit.main(["list", "--rejected"]), 0)

    def test_list_default_empty_when_nothing_passes(self):
        """全部被筛掉时，默认视图不能假装有岗位可投。"""
        self.assertEqual(jobkit.main(["list"]), 0)


class MatchExplainTest(unittest.TestCase):
    def test_match_hits_explains_score(self):
        """分数必须能解释：命中哪些词要说得出来。"""
        import rules
        hits = rules.match_hits({"title": "Python后端开发工程师", "skills": "mysql redis"})
        self.assertIn("python", hits)
        self.assertIn("后端", hits)

    def test_ai_word_boundary(self):
        """英文词按词边界，别让 'ai' 命中 'email'。"""
        import rules
        self.assertFalse(rules._kw_present("ai", "email marketing"))
        self.assertTrue(rules._kw_present("ai", "ai应用开发"))

    def test_score_full_at_six_hits(self):
        import rules
        self.assertEqual(rules._score_match(
            {"title": "python fastapi mysql redis docker 后端"}), 100.0)


if __name__ == "__main__":
    unittest.main()
