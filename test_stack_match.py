"""技术栈匹配单测 —— 重点防误杀（误杀比漏判贵）。"""
from __future__ import annotations

import unittest

import stack_match as S

PROF = {
    "skills": ["python", "fastapi", "langchain", "rag", "agent", "llm", "prompt"],
    "weak": ["vue", "react", "javascript"],
    "never": ["php"],
    "core_lang": ["python"],
    "acceptable_lang": ["java", "golang"],
}


def job(jd: str, **kw) -> dict:
    d = {"title": "AI应用开发", "company": "某公司", "job_description": jd}
    d.update(kw)
    return d


class ExtractTest(unittest.TestCase):
    def test_java_not_matching_javascript(self):
        """javascript 绝不能被识别成 java —— 这是最贵的误判。"""
        st = S.extract_stacks("前端开发，熟悉 JavaScript 和 React")
        self.assertIn("javascript", st["langs"])
        self.assertNotIn("java", st["langs"])

    def test_java_detected_when_real(self):
        st = S.extract_stacks("熟悉 Java 与 Spring Boot，三年经验")
        self.assertIn("java", st["langs"])
        self.assertIn("spring", st["frameworks"])

    def test_es_not_matching_common_words(self):
        """'yes'/'these' 这类词不该被当成 Elasticsearch。"""
        st = S.extract_stacks("yes we can, these are the requirements")
        self.assertNotIn("elasticsearch", st["frameworks"])

    def test_es_detected_uppercase(self):
        st = S.extract_stacks("熟悉 ES 和 MySQL")
        self.assertIn("elasticsearch", st["frameworks"])

    def test_cpp(self):
        st = S.extract_stacks("要求 C++ 开发经验")
        self.assertIn("c++", st["langs"])

    def test_empty_jd(self):
        st = S.extract_stacks("")
        self.assertEqual(st["langs"], [])
        self.assertEqual(st["chars"], 0)


class JudgeLangTest(unittest.TestCase):
    def test_pure_java_is_mismatch(self):
        out = S.match(job("熟练掌握 Java、Spring Boot、MyBatis，负责后端服务开发"), PROF)
        self.assertEqual(out["verdict"], "mismatch")
        self.assertLess(out["delta"], 0)
        self.assertIn("硬缺", out["why"])

    def test_python_or_java_is_not_mismatch(self):
        """「Python 或 Java 任一门」不能判不匹配 —— 这是最常见的误杀场景。"""
        out = S.match(job("后端开发，熟悉 Python 或 Java 任一门即可"), PROF)
        self.assertNotEqual(out["verdict"], "mismatch")
        self.assertIn(out["verdict"], ("acceptable", "ok", "strong", "weak"))

    def test_python_only_is_strong_or_ok(self):
        out = S.match(job("用 Python + FastAPI 做 LLM 应用，熟悉 RAG 与 Agent"), PROF)
        self.assertIn(out["verdict"], ("strong", "ok"))
        self.assertGreater(out["delta"], 0)

    def test_never_language_is_mismatch(self):
        out = S.match(job("PHP 后端开发，熟悉 Python 也可"), PROF)
        self.assertEqual(out["verdict"], "mismatch")

    def test_no_jd_is_unknown(self):
        out = S.match(job(""), PROF)
        self.assertEqual(out["verdict"], "unknown")
        self.assertEqual(out["delta"], 0)

    def test_short_jd_still_works(self):
        out = S.match(job("Java"), PROF)
        self.assertEqual(out["verdict"], "mismatch")

    def test_hits_and_gaps_reported(self):
        out = S.match(job("Python + LangChain 做 RAG，需要 Pytorch 经验"), PROF)
        self.assertIn("langchain", out["hits"])
        self.assertIn("pytorch", out["gaps"])

    def test_many_gaps_no_hits_penalised(self):
        out = S.match(job("Python 开发，需掌握 PyTorch、TensorFlow、Milvus、vLLM、Dify"), PROF)
        self.assertLessEqual(out["delta"], 3)

    def test_custom_profile_respected(self):
        """换画像结论要跟着变，证明画像真的在用。"""
        java_guy = {"skills": ["java", "spring", "mybatis"], "weak": [], "never": [],
                    "core_lang": ["java"], "acceptable_lang": ["python"]}
        jd = job("熟练掌握 Java、Spring Boot、MyBatis")
        self.assertEqual(S.match(jd, PROF)["verdict"], "mismatch")
        self.assertIn(S.match(jd, java_guy)["verdict"], ("strong", "ok"))


class RenderTest(unittest.TestCase):
    def test_groups_by_verdict(self):
        rows = [
            dict(S.match(job("Java + Spring"), PROF), title="后端", company="A"),
            dict(S.match(job("Python + LangChain + RAG"), PROF), title="AI", company="B"),
        ]
        text = S.render(rows)
        self.assertIn("别投", text)
        self.assertIn("A", text)

    def test_empty(self):
        self.assertIn("没有", S.render([]))


if __name__ == "__main__":
    unittest.main()
