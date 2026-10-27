"""智联 URL 构造器的测试。

重点：城市码自动解析必须**不需要人工维护**，且失败时给的是人能看懂的提示，
不能是「你去地址栏读一个参数」这种把内部细节甩给用户的文案。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dom import zhilian_urls as ZU  # noqa: E402


class CityCodeTest(unittest.TestCase):
    def test_known_city(self):
        self.assertEqual(ZU.city_code("广州"), "763")

    def test_alias_normalized(self):
        self.assertEqual(ZU.city_code("广州市"), ZU.city_code("广州"))
        self.assertEqual(ZU.city_code("sz"), ZU.city_code("深圳"))

    def test_cache_hit_does_not_touch_browser(self):
        """缓存里有的城市，绝不重新打开浏览器。"""
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "codes.json"
            cache.write_text(json.dumps({"佛山": "768"}, ensure_ascii=False), encoding="utf-8")
            old = ZU.CODE_CACHE
            ZU.CODE_CACHE = cache
            try:
                self.assertEqual(ZU.city_code("佛山", learn=False), "768")
            finally:
                ZU.CODE_CACHE = old

    def test_unknown_city_without_learn_raises(self):
        with tempfile.TemporaryDirectory() as d:
            old = ZU.CODE_CACHE
            ZU.CODE_CACHE = Path(d) / "none.json"
            try:
                with self.assertRaises(ZU.UnknownCityError):
                    ZU.city_code("火星", learn=False)
            finally:
                ZU.CODE_CACHE = old

    def test_error_message_is_for_humans(self):
        """报错不能把「读地址栏 jl 参数」这种内部细节甩给用户。"""
        with tempfile.TemporaryDirectory() as d:
            old = ZU.CODE_CACHE
            ZU.CODE_CACHE = Path(d) / "none.json"
            try:
                with self.assertRaises(ZU.UnknownCityError) as ctx:
                    ZU.city_code("火星", learn=False)
            finally:
                ZU.CODE_CACHE = old
            msg = str(ctx.exception)
            self.assertNotIn("jl", msg)
            self.assertNotIn("地址栏", msg)
            self.assertIn("火星", msg)

    def test_learn_result_written_to_cache(self):
        """学到码之后要落盘，下次零成本。"""
        with tempfile.TemporaryDirectory() as d:
            old_cache, old_learn = ZU.CODE_CACHE, ZU._learn_code
            ZU.CODE_CACHE = Path(d) / "codes.json"
            ZU._learn_code = lambda name: "768" if name == "佛山" else ""
            try:
                self.assertEqual(ZU.city_code("佛山"), "768")
                self.assertEqual(json.loads(ZU.CODE_CACHE.read_text("utf-8")), {"佛山": "768"})
                ZU._learn_code = lambda name: ""          # 第二次不该再学
                self.assertEqual(ZU.city_code("佛山"), "768")
            finally:
                ZU.CODE_CACHE, ZU._learn_code = old_cache, old_learn

    def test_learn_returns_empty_when_site_unknown(self):
        """citymap 里没有的城市 → 学不到就返回空串，绝不猜一个码。"""
        with tempfile.TemporaryDirectory() as d:
            old = ZU.CITYMAP
            ZU.CITYMAP = Path(d) / "map.json"
            ZU.CITYMAP.write_text("{}", encoding="utf-8")
            try:
                self.assertEqual(ZU._learn_code("火星"), "")
            finally:
                ZU.CITYMAP = old

    def test_learn_rejects_wrong_city_page(self):
        """子站把我们带到了别的城市时，那个码不能用。"""
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "map.json"
            mp.write_text(json.dumps({"佛山": "https://www.zhaopin.com/fs/"},
                                     ensure_ascii=False), encoding="utf-8")
            old_map, old_run = ZU.CITYMAP, None
            ZU.CITYMAP = mp
            import dom_channel as DC
            old_run = DC._run
            DC._run = lambda *a, **k: {"c": {"name": "广州", "code": "763"}}
            try:
                self.assertEqual(ZU._learn_code("佛山"), "")
            finally:
                ZU.CITYMAP, DC._run = old_map, old_run


class BuildUrlTest(unittest.TestCase):
    def test_shape(self):
        url = ZU.build_search_url("AI开发", "广州")
        self.assertTrue(url.startswith("https://www.zhaopin.com/jobs/?"))
        self.assertIn("pageMode=search", url)
        self.assertIn("jl=763", url)
        self.assertIn("kw=AI%E5%BC%80%E5%8F%91", url)

    def test_empty_keyword_rejected(self):
        with self.assertRaises(ValueError):
            ZU.build_search_url("   ", "广州")

    def test_page_one_has_no_paging_param(self):
        self.assertNotIn("&p=", ZU.build_search_url("Python", "广州", page=1))
        self.assertIn("&p=2", ZU.build_search_url("Python", "广州", page=2))


class CityMapFileTest(unittest.TestCase):
    def test_citymap_is_populated(self):
        """397 城的拼音表必须在仓库里，否则自动学习要靠网络从头来。"""
        data = ZU._read_json(ZU.CITYMAP)
        self.assertGreater(len(data), 300)
        for city in ("广州", "深圳", "佛山", "东莞"):
            self.assertIn(city, data)
            self.assertTrue(data[city].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
