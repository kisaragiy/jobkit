"""HR 应答库：结构完整性 + 占位符替换 + 雷区必须有。"""
import unittest

import hr_answers as H

REQUIRED = ("q", "why", "safe", "aggressive", "pitfalls")


class TestTopics(unittest.TestCase):
    def test_all_topics_present(self):
        self.assertEqual(len(H.all_topics()), 6)
        self.assertIn("salary", H.all_topics())

    def test_each_topic_complete(self):
        for k in H.all_topics():
            t = H.get(k)
            for f in REQUIRED:
                self.assertIn(f, t, f"{k} 缺 {f}")
            self.assertTrue(t["q"].strip(), f"{k} 问题为空")
            self.assertTrue(t["safe"].strip(), f"{k} 缺安全答法")
            self.assertTrue(t["aggressive"].strip(), f"{k} 缺进攻答法")

    def test_safe_and_aggressive_differ(self):
        """两版答案一样就等于只给了一版 —— 那说明这题没做。"""
        for k in H.all_topics():
            t = H.get(k)
            self.assertNotEqual(t["safe"], t["aggressive"], f"{k} 两版答案重复")

    def test_pitfalls_non_empty(self):
        for k in H.all_topics():
            self.assertGreaterEqual(len(H.get(k)["pitfalls"]), 2,
                                    f"{k} 雷区太少（至少 2 条才有用）")

    def test_unknown_topic_raises(self):
        with self.assertRaises(KeyError):
            H.get("nope")


class TestRender(unittest.TestCase):
    def test_placeholders_filled(self):
        out = H.render_one("salary", target="9-10K", target_high="12K")
        self.assertIn("9-10K", out)
        self.assertIn("12K", out)
        self.assertNotIn("{target}", out.split("要填的地方")[0])

    def test_render_one_has_pitfalls(self):
        out = H.render_one("overtime")
        self.assertIn("雷区", out)

    def test_render_one_has_fill_hint(self):
        out = H.render_one("leave")
        self.assertIn("要填的地方", out)
        self.assertIn("gap", out)   # 指向空窗期套件

    def test_render_index_lists_all(self):
        out = H.render()
        for k in H.all_topics():
            self.assertIn(H.get(k)["q"][:8], out)

    def test_no_brace_left_in_body(self):
        """除了「要填的地方」小节，正文不该残留 {xxx}。"""
        for k in H.all_topics():
            body = H.render_one(k).split("要填的地方")[0]
            self.assertNotIn("{target}", body, f"{k} 正文残留占位符")


if __name__ == "__main__":
    unittest.main()
