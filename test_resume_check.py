"""技术词表 + 简历诊断。

重点防两类错：
1. 短英文词按子串匹配 → "es" 命中 "express"，覆盖率虚高，掩盖真实缺口
2. JD 提取不到词时给 0 分 → 用户以为简历不行，其实是采样问题
"""
import unittest

import resume_check as RC
import tech_terms as T

RESUME = """张三，2025届，Python 后端 + LLM Agent。
项目：灵枢用 LangGraph 编排多 Agent，FastAPI 提供接口，Redis 缓存，Docker 部署。
AIGC 管线用 ComfyUI + SDXL 出图。"""


class TestShortTermBoundary(unittest.TestCase):
    def test_es_not_match_express(self):
        self.assertNotIn("es", T.find_terms("express framework"))

    def test_go_not_match_google(self):
        self.assertNotIn("go", T.find_terms("google cloud"))

    def test_go_matches_standalone(self):
        self.assertIn("go", T.find_terms("熟悉 go 语言"))

    def test_es_matches_standalone(self):
        self.assertIn("es", T.find_terms("ES 集群运维"))

    def test_rag_not_match_storage(self):
        self.assertNotIn("rag", T.find_terms("storage layer"))

    def test_rag_matches(self):
        self.assertIn("rag", T.find_terms("RAG 检索增强"))

    def test_long_terms_still_substring(self):
        """长词走子串匹配，不该被边界规则误伤（python3 里的 python）。"""
        self.assertIn("python", T.find_terms("python3.11 环境"))
        self.assertIn("comfyui", T.find_terms("comfyui工作流"))


class TestCheck(unittest.TestCase):
    def test_full_coverage(self):
        jd = "需要 Python、LangGraph、FastAPI、Redis、Docker"
        r = RC.check(RESUME, jd)
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["score"], 80)
        self.assertEqual(r["missing"], [])

    def test_missing_detected(self):
        jd = "需要 Python、LangGraph、MySQL、Kafka、Spring"
        r = RC.check(RESUME, jd)
        self.assertIn("mysql", r["missing"])
        self.assertIn("kafka", r["missing"])

    def test_covered_detected(self):
        jd = "需要 Python、LangGraph、MySQL"
        r = RC.check(RESUME, jd)
        self.assertIn("python", r["covered"])
        self.assertIn("langgraph", r["covered"])

    def test_extra_detected(self):
        jd = "需要 Python"
        r = RC.check(RESUME, jd)
        self.assertIn("comfyui", r["extra"])

    def test_missing_grouped_by_category(self):
        jd = "需要 Python、Rerank、向量数据库、模型部署、微调"
        r = RC.check(RESUME, jd)
        self.assertIn("AI/LLM", r["missing_by_cat"])

    def test_empty_jd_no_score(self):
        """JD 提不到词时不能给 0 分 —— 那不是简历的问题。"""
        r = RC.check(RESUME, "我们招一个勤奋的人，待遇面议")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["score"])
        self.assertIn("无法对比", r["reason"])

    def test_empty_resume_all_missing(self):
        r = RC.check("", "需要 Python、Docker")
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["grade"], "D")
        self.assertEqual(len(r["missing"]), 2)

    def test_grade_thresholds(self):
        # 5/5 全覆盖 → A
        r = RC.check(RESUME, "Python LangGraph FastAPI Redis Docker")
        self.assertEqual(r["grade"], "A")
        # 明显不全 → 低于 A
        r2 = RC.check(RESUME, "Spring MyBatis Kafka RabbitMQ Clickhouse Hbase")
        self.assertIn(r2["grade"], ("C", "D"))


class TestRender(unittest.TestCase):
    def test_render_has_score_and_missing(self):
        out = RC.render(RC.check(RESUME, "需要 Python、MySQL"))
        self.assertIn("覆盖率", out)
        self.assertIn("mysql", out)

    def test_render_handles_bad_jd(self):
        out = RC.render(RC.check(RESUME, "面议"))
        self.assertIn("⚠", out)
        self.assertNotIn("覆盖率 0%", out)


class TestBatch(unittest.TestCase):
    """批量：跨岗高频缺失词 + 补词杠杆。"""

    RESUME = "Python FastAPI Docker SQLite pytest"

    def jobs(self, *jds):
        return [{"job_description": j, "title": f"岗{i}",
                 "company": f"公司{i}"} for i, j in enumerate(jds)]

    def test_empty_list(self):
        r = RC.batch(self.RESUME, [])
        self.assertFalse(r["ok"])

    def test_no_jd_skipped(self):
        r = RC.batch(self.RESUME, self.jobs(""))
        self.assertFalse(r["ok"])
        self.assertEqual(r["skipped"], 1)

    def test_jd_without_terms_skipped(self):
        """JD 里没有技术词 → 无法对比，跳过而不是给 0 分。"""
        r = RC.batch(self.RESUME, self.jobs("负责日常事务，待遇从优"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["skipped"], 1)

    def test_finds_high_freq_missing(self):
        r = RC.batch(self.RESUME, self.jobs(
            "Python MySQL Redis 微服务架构" * 5,
            "Python MySQL Kafka 分布式" * 5,
        ))
        self.assertTrue(r["ok"])
        terms = {x["term"] for x in r["lever"]}
        self.assertIn("mysql", terms)

    def test_single_occurrence_not_lever(self):
        """只在 1 个岗出现的缺失词没杠杆，不列。"""
        r = RC.batch(self.RESUME, self.jobs(
            "Python MySQL" * 5,
            "Python Rust" * 5,
        ))
        terms = {x["term"] for x in r["lever"]}
        self.assertNotIn("rust", terms)

    def test_gain_is_positive(self):
        r = RC.batch(self.RESUME, self.jobs(
            "Python MySQL Redis" * 5,
            "Python MySQL Kafka" * 5,
        ))
        self.assertGreaterEqual(r["gain"], 0)
        self.assertGreaterEqual(r["avg_after"], r["avg_score"])

    def test_by_cat_groups(self):
        r = RC.batch(self.RESUME, self.jobs(
            "Python MySQL Redis" * 5,
            "Python MySQL Kafka" * 5,
        ))
        self.assertIn("mysql", str(r["lever_by_cat"]))

    def test_avg_score_range(self):
        r = RC.batch(self.RESUME, self.jobs("Python FastAPI" * 5))
        self.assertGreaterEqual(r["avg_score"], 0)
        self.assertLessEqual(r["avg_score"], 100)

    def test_weakest_sorted_asc(self):
        r = RC.batch(self.RESUME, self.jobs(
            "Python FastAPI" * 5,
            "Java C++ Golang Rust" * 5,
        ))
        scores = [p["score"] for p in r["weakest"]]
        self.assertEqual(scores, sorted(scores))

    def test_render_disclaims(self):
        r = RC.batch(self.RESUME, self.jobs("Python MySQL" * 5,
                                            "Python MySQL Redis" * 5))
        out = RC.render_batch(r)
        self.assertIn("别硬写", out)

    def test_render_failure_is_honest(self):
        out = RC.render_batch(RC.batch(self.RESUME, self.jobs("")))
        self.assertIn("没有可用 JD", out)


if __name__ == "__main__":
    unittest.main()
