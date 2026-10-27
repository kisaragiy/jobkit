"""JD 清洗测试 —— 详情页抓下来的文本混着界面装饰，必须洗掉。

真机抓到的原文（BOSS 详情页）：
    微信扫码分享 举报
    职位描述
    Docker / MySQL / ...
    ...
    张和荣
    本月活跃
    广州旭桓腾进出口
    ·
    招聘者

前两行和最后四行都不是岗位内容。不洗的话，这些字会被关键词匹配读到，
也会显示在 `jobkit show` 里 —— 用户一眼看出软件很粗糙。
"""
from __future__ import annotations

import unittest

import dom_channel as D


RAW = (
    "微信扫码分享 举报\n"
    "职位描述\n"
    "Docker\nMySQL\nFastAPI\n"
    "【岗位职责】\n"
    "1. 负责后端接口开发；\n"
    "2. 维护数据管道。\n"
    "【任职要求】\n"
    "熟悉 Python / FastAPI / MySQL\n"
    "张和荣\n"
    "本月活跃\n"
    "广州旭桓腾进出口有限公司\n"
    "·\n"
    "招聘者"
)


class TestCleanJd(unittest.TestCase):
    def test_strips_head_noise(self):
        out = D.clean_jd(RAW)
        self.assertTrue(out.startswith("职位描述"))
        self.assertNotIn("微信扫码分享", out)
        self.assertNotIn("举报", out)

    def test_strips_tail_recruiter_block(self):
        out = D.clean_jd(RAW)
        self.assertNotIn("张和荣", out)
        self.assertNotIn("本月活跃", out)
        self.assertNotIn("招聘者", out)
        self.assertTrue(out.endswith("熟悉 Python / FastAPI / MySQL"))

    def test_keeps_body_newlines(self):
        """正文的换行必须保留 —— 压成一坨就没法读了。"""
        out = D.clean_jd(RAW)
        self.assertIn("\n1. 负责后端接口开发；\n", out)
        self.assertGreater(out.count("\n"), 6)

    def test_keeps_all_body_content(self):
        out = D.clean_jd(RAW)
        for frag in ("岗位职责", "任职要求", "数据管道", "Docker"):
            self.assertIn(frag, out)

    def test_empty_and_none(self):
        self.assertEqual(D.clean_jd(""), "")
        self.assertEqual(D.clean_jd(None), "")

    def test_clean_text_untouched(self):
        """本来干净的 JD 不能被误伤。"""
        plain = "职位描述\n负责后端开发。\n要求熟悉 Python。"
        self.assertEqual(D.clean_jd(plain), plain)

    def test_only_noise_becomes_empty(self):
        self.assertEqual(D.clean_jd("微信扫码分享 举报\n"), "")

    def test_crlf_normalized(self):
        out = D.clean_jd("职位描述\r\n正文一\r\n正文二")
        self.assertNotIn("\r", out)
        self.assertIn("正文一\n正文二", out)

    def test_activity_word_in_body_is_not_tail(self):
        """正文里提到「活跃」不该被当成招聘者活跃状态截断。"""
        text = "职位描述\n要求：社区活跃度高\n负责内容运营"
        self.assertIn("社区活跃度高", D.clean_jd(text))

    def test_split_baoju_anchor(self):
        """真机见过「举\\n报」被拆成两行 —— 正则清不掉，得靠标题锚点。"""
        raw = "举\n报\n职位描述\n负责后端开发"
        out = D.clean_jd(raw)
        self.assertTrue(out.startswith("职位描述"))
        self.assertNotIn("举", out.split("\n")[0])

    def test_anchor_only_within_head(self):
        """正文中间出现「职位描述」不能截断 —— 只认开头 60 字内。"""
        text = "职位描述\n" + "正文" * 40 + "\n关于职位描述的补充说明"
        self.assertIn("关于职位描述的补充说明", D.clean_jd(text))


class TestDetailJdWiring(unittest.TestCase):
    """fetch_detail 出去的 JD 必须已经洗过 —— 不能指望调用方记得洗。"""

    def test_normalize_detail_job_cleans_jd(self):
        snap = {
            "title": "后端开发", "company": "某公司",
            "jobDescription": RAW,
            "detailFound": True,
        }
        job = D.normalize_detail_job(snap, url="https://www.zhipin.com/job_detail/abc123.html")
        self.assertIsNotNone(job)
        self.assertTrue(job["job_description"].startswith("职位描述"))
        self.assertNotIn("张和荣", job["job_description"])


if __name__ == "__main__":
    unittest.main()
