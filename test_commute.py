"""通勤估算：距离/分档/跨市不给假数字。

测试不联网 —— geocode 用 monkeypatch 顶掉。
"""
import unittest

import commute


class TestHaversine(unittest.TestCase):
    def setUp(self):
        self.tianhe = (23.1271, 113.3564)      # 广州天河区
        self.nanshan = (22.5333, 113.9300)     # 深圳南山区
        self.panyu = (22.9375, 113.3847)       # 广州番禺区

    def test_cross_city_distance(self):
        d = commute.haversine_km(self.tianhe, self.nanshan)
        self.assertGreater(d, 80)
        self.assertLess(d, 95)

    def test_same_city_distance(self):
        d = commute.haversine_km(self.tianhe, self.panyu)
        self.assertGreater(d, 15)
        self.assertLess(d, 28)

    def test_zero_distance(self):
        self.assertAlmostEqual(commute.haversine_km(self.tianhe, self.tianhe), 0.0, places=3)


class TestLevels(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(commute.level_of(2)[0], "很近")
        self.assertEqual(commute.level_of(10)[0], "可接受")
        self.assertEqual(commute.level_of(25)[0], "偏")
        self.assertEqual(commute.level_of(80)[0], "远")

    def test_province_only_for_guangdong(self):
        """拼「广东省」去查成都的区会一个结果都没有。"""
        self.assertEqual(commute._province_for("东莞"), "广东省")
        self.assertEqual(commute._province_for("广州"), "广东省")
        self.assertEqual(commute._province_for("成都"), "")
        self.assertEqual(commute._province_for(""), "")

    def test_boundaries(self):
        self.assertEqual(commute.level_of(4.99)[0], "很近")
        self.assertEqual(commute.level_of(5)[0], "可接受")


class TestEstimate(unittest.TestCase):
    def setUp(self):
        self._orig = commute.geocode
        commute.geocode = lambda name, city="", **kw: {
            ("天河区", "广州"): (23.1271, 113.3564),
            ("番禺区", "广州"): (22.9375, 113.3847),
            ("南山区", "深圳"): (22.5333, 113.9300),
            ("虎门镇", "东莞"): (22.8162, 113.6684),
            ("大朗镇", "东莞"): (22.9800, 113.9500),
        }.get((name, city))

    def tearDown(self):
        commute.geocode = self._orig

    def test_same_city_gives_minutes(self):
        r = commute.estimate("天河区", "广州", "番禺区", "广州")
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["minutes"])
        self.assertGreater(r["minutes"], 30)
        self.assertFalse(r["cross_city"])

    def test_cross_city_no_minutes(self):
        """跨市不能给分钟数 —— 用市区速度算会得出 5 小时这种荒谬值。"""
        r = commute.estimate("天河区", "广州", "南山区", "深圳")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["minutes"])
        self.assertEqual(r["level"], "跨城")
        self.assertIn("12306", r["note"])

    def test_unknown_district(self):
        r = commute.estimate("天河区", "广州", "不存在的区", "广州")
        self.assertFalse(r["ok"])
        self.assertIn("查不到", r["reason"])

    def test_unknown_home(self):
        r = commute.estimate("火星", "火星", "番禺区", "广州")
        self.assertFalse(r["ok"])

    def test_no_district(self):
        r = commute.estimate("天河区", "广州", "", "广州")
        self.assertFalse(r["ok"])
        self.assertIn("没有区镇", r["reason"])

    def test_far_same_city_not_called_cross_city(self):
        """东莞虎门→大朗 35km 是同城，不能因为远就叫跨城。"""
        r = commute.estimate("虎门镇", "东莞", "大朗镇", "东莞")
        self.assertTrue(r["ok"])
        self.assertFalse(r["cross_city"])
        self.assertNotEqual(r["level"], "跨城")
        self.assertIsNotNone(r["minutes"])

    def test_road_distance_larger_than_straight(self):
        r = commute.estimate("天河区", "广州", "番禺区", "广州")
        self.assertGreater(r["road_km"], r["straight_km"])


class TestRender(unittest.TestCase):
    def test_render_ok(self):
        r = {"ok": True, "home": "广州天河区", "job": "广州番禺区",
             "straight_km": 21.1, "road_km": 29.5, "minutes": 80,
             "level": "偏", "note": "单程 1 小时以上", "cross_city": False}
        out = commute.render(r)
        self.assertIn("80 分钟", out)
        self.assertIn("区镇中心估算", out)

    def test_render_cross_city(self):
        r = {"ok": True, "home": "广州天河区", "job": "深圳南山区",
             "straight_km": 87.9, "road_km": None, "minutes": None,
             "level": "跨城", "note": "请用地图或 12306 自己查", "cross_city": True}
        out = commute.render(r)
        self.assertIn("跨城", out)
        self.assertNotIn("分钟（", out)

    def test_render_fail(self):
        out = commute.render({"ok": False, "reason": "查不到"})
        self.assertIn("无法估算", out)


class TestSpeedTiers(unittest.TestCase):
    """速度不能一个数用到底：跨镇走快速路，市区堵红绿灯。"""

    def test_short_is_slowest(self):
        self.assertLess(commute._speed_for(5), commute._speed_for(20))

    def test_long_is_fastest(self):
        self.assertGreater(commute._speed_for(60), commute._speed_for(30))

    def test_far_town_not_absurd(self):
        """凤岗→虎门直线 50.8km，按旧固定 22km/h 会算成 194 分钟（实际 1 小时多）。"""
        road = 50.8 * commute.ROAD_FACTOR
        minutes = road / commute._speed_for(50.8) * 60
        self.assertLess(minutes, 130)
        self.assertGreater(minutes, 60)

    def test_near_town_reasonable(self):
        road = 16.2 * commute.ROAD_FACTOR
        minutes = road / commute._speed_for(16.2) * 60
        self.assertLess(minutes, 70)


class TestCachedOnly(unittest.TestCase):
    """allow_fetch=False 只读缓存 —— 详情页打开不该等 Nominatim 超时。"""

    def setUp(self):
        self._orig_geo = commute.geocode
        self._orig_home = commute.load_home
        commute.load_home = lambda: {"name": "威远岛", "city": "东莞",
                                     "lat": 22.8040, "lon": 113.6396}

    def tearDown(self):
        commute.geocode = self._orig_geo
        commute.load_home = self._orig_home

    def test_no_fetch_says_not_cached(self):
        commute.geocode = lambda name, city="", **kw: None
        r = commute.estimate(None, None, "凤岗镇", "东莞", allow_fetch=False)
        self.assertFalse(r["ok"])
        self.assertTrue(r["need_fetch"])
        self.assertIn("还没缓存", r["reason"])

    def test_with_fetch_says_not_found(self):
        commute.geocode = lambda name, city="", **kw: None
        r = commute.estimate(None, None, "凤岗镇", "东莞", allow_fetch=True)
        self.assertFalse(r["ok"])
        self.assertNotIn("need_fetch", r)
        self.assertIn("查不到", r["reason"])

    def test_cached_still_works(self):
        commute.geocode = lambda name, city="", **kw: (22.7500, 114.1400)
        r = commute.estimate(None, None, "凤岗镇", "东莞", allow_fetch=False)
        self.assertTrue(r["ok"])
        self.assertGreater(r["straight_km"], 0)


if __name__ == "__main__":
    unittest.main()
