"""通勤估算：区/镇中心直线距离 → 估算门到门时间。

诚实边界（必须先说清）
----------------------
BOSS 只给到「凤岗镇」「天河区」这种**区镇级**，没有公司门牌号。所以：
- 算的是**区镇中心到区镇中心**的直线距离
- 真实通勤会因公司在区内位置不同差 15-40 分钟
- 结论用于**粗筛**（这个区的岗要不要看），不能用来决定"几点起床"

坐标来源
--------
OpenStreetMap Nominatim（免 key，需遵守其使用政策：1 req/s、带 User-Agent、
标注 attribution）。**不内置手写坐标** —— 编坐标比不算更糟，用户会当成真的。

直连不通（国内常见），走本地代理 7899；都失败就明确返回「查不到」，
绝不拿个默认值糊过去。

组合地名会查错（血的教训）
--------------------------
查「虎门镇威远岛」返回 (22.57, 113.04) —— 那是广州南沙，差 60 公里。
Nominatim 匹配失败时会降级到别的同名地点，而且**看起来像个正常结果**。
所以查出坐标后必须校验 display_name 里是否含目标城市，不含就拒绝使用。
宁可报「查不到」，也不能给一个看着像答案、实际错 60 公里的坐标。
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_PATH = DATA_DIR / "geo_cache.json"
HOME_PATH = DATA_DIR / "home.json"
PROXY = "http://127.0.0.1:7899"
UA = "jobkit/1.0 (personal job search tool)"
PROVINCE = "广东省"

# 直线距离 → 实际道路的放大系数。市区路网弯曲 + 绕行，1.35-1.45 是常见经验值。
ROAD_FACTOR = 1.4
# 门到门平均速度（含步行、等车、换乘）。按距离分档 —— 不能一个数用到底：
# 市区 5 公里堵在红绿灯上是 20 km/h，但 50 公里跨镇基本走快速路/高速，
# 还按 22 算会把凤岗→虎门（直线 51km）算成 194 分钟，实际开车 1 小时多。
SPEED_KMH = 22.0          # 兼容旧调用；实际用 _speed_for()


def _speed_for(straight_km: float) -> float:
    """按直线距离挑门到门平均速度。"""
    if straight_km <= 10:
        return 20.0       # 短途：市区拥堵、红绿灯、换乘
    if straight_km <= 25:
        return 28.0       # 中距离：主干道 + 少量快速路
    if straight_km <= 50:
        return 38.0       # 跨镇：快速路为主
    return 45.0           # 远郊：高速为主

_LAST_REQ = [0.0]
# 记住哪条通道能通：None=还没试过。第一次碰完就固定下来，
# 避免每个地名都先等一次直连超时（几十个区镇会拖成几分钟）。
_CHANNEL = [None]


def _cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_cache(d: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                          encoding="utf-8")


# ── 「家」在哪 ──────────────────────────────────────────
def save_home(name: str, city: str,
              lat: float | None = None, lon: float | None = None) -> dict:
    """存家的位置。给了坐标就直接用，不再联网查（防查成别的地方）。"""
    if lat is None or lon is None:
        g = _geocode_raw(name, city)
        if not g:
            raise ValueError(f"查不到「{name}」的坐标，请手动提供 lat/lon")
        lat, lon = g["lat"], g["lon"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    d = {"name": name, "city": city, "lat": lat, "lon": lon}
    HOME_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return d


def load_home() -> dict | None:
    """读家。没配过返回 None —— 调用方自己决定怎么办，不默认任何坐标。"""
    if not HOME_PATH.exists():
        return None
    try:
        d = json.loads(HOME_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if d.get("lat") is None or d.get("lon") is None:
        return None
    return d


def _fetch(url: str, use_proxy: bool, timeout: float = 20) -> bytes:
    handlers = []
    if use_proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": PROXY, "https": PROXY}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))   # 明确不用系统代理
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _geocode_raw(name: str, city: str = "", *, allow_fetch: bool = True) -> dict | None:
    """地名 → {lat, lon, display_name}，含城市校验。查不到/不匹配返回 None。

    `allow_fetch=False` 只用缓存，不联网。详情页用它：打开一个岗位不该
    等 20 秒 Nominatim 超时，缓存没有就让用户点一下再算。
    """
    key = f"{name}|{city}"
    cache = _cache()
    if key in cache and cache[key]:
        v = cache[key]
        if isinstance(v, dict):
            return v
        if isinstance(v, list) and len(v) == 2:        # 旧格式兼容
            return {"lat": v[0], "lon": v[1], "display_name": ""}
    if not allow_fetch:
        return None
    prov = _province_for(city)
    q = ",".join(p for p in (name, city, prov, "中国") if p)
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1}))

    # Nominatim 政策：1 req/s
    wait = 1.1 - (time.time() - _LAST_REQ[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_REQ[0] = time.time()

    raw = None
    # 已探明通道就直接用，不再每次先等直连超时
    order = ((_CHANNEL[0], not _CHANNEL[0]) if _CHANNEL[0] is not None
             else (False, True))
    for use_proxy in order:
        try:
            raw = _fetch(url, use_proxy, timeout=5 if not use_proxy else 20)
            _CHANNEL[0] = use_proxy          # 记住这条通
            break
        except Exception:  # noqa: BLE001
            continue
    if raw is None:
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not data:
        return None

    try:
        lat = float(data[0]["lat"])
        lon = float(data[0]["lon"])
        disp = data[0].get("display_name", "") or ""
    except (KeyError, IndexError, TypeError, ValueError):
        return None

    # 城市校验：查到的地名里得真出现目标城市，否则是降级匹配到别处了
    if city and city not in disp:
        return None

    out = {"lat": lat, "lon": lon, "display_name": disp}
    cache[key] = out
    _save_cache(cache)
    return out


def geocode(name: str, city: str = "", *, allow_fetch: bool = True) -> tuple[float, float] | None:
    """区镇名 → (lat, lon)。查不到返回 None，不猜。"""
    g = _geocode_raw(name, city, allow_fetch=allow_fetch)
    return (g["lat"], g["lon"]) if g else None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点直线距离（公里）。"""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


LEVELS = (
    (5, "很近", "骑车/步行都行"),
    (15, "可接受", "地铁/公交 40 分钟内"),
    (30, "偏", "单程 1 小时以上，要想清楚"),
    (float("inf"), "远", "单程 1.5 小时以上，同城也算远"),
)

# 省份不能无脑拼。查「武侯区」时拼上「广东省」会一个结果都没有。
# 只给广东的城市补省份（重名区镇多，补上更准）；其它城市靠 city 本身够用。
_GUANGDONG = {
    "广州", "深圳", "东莞", "佛山", "珠海", "惠州", "中山", "江门", "肇庆",
    "汕头", "潮州", "揭阳", "汕尾", "梅州", "河源", "韶关", "清远",
    "阳江", "茂名", "湛江", "云浮",
}


def _province_for(city: str) -> str:
    return PROVINCE if city in _GUANGDONG else ""


def level_of(km: float) -> tuple[str, str]:
    for th, name, note in LEVELS:
        if km < th:
            return name, note
    return "远", ""


def estimate(home_name: str | None, home_city: str | None,
             job_district: str, job_city: str, *,
             allow_fetch: bool = True) -> dict:
    """估一段通勤。home 不传就读 home.json。查不到坐标时 ok=False 并说明原因。

    `allow_fetch=False` 只读地理缓存，不联网 —— 前端详情页默认用它，
    让用户点一下再联网算，避免开个岗位等 20 秒。
    """
    if not job_district:
        return {"ok": False, "reason": "岗位没有区镇信息（BOSS 页面没给）"}

    cfg = load_home() if not (home_name and home_city) else None
    if cfg:
        h = (cfg["lat"], cfg["lon"])
        home_name, home_city = cfg["name"], cfg["city"]
    else:
        if not home_name or not home_city:
            return {"ok": False,
                    "reason": "还没设置家的位置（jobkit commute --set-home）"}
        h = geocode(home_name, home_city, allow_fetch=allow_fetch)
        if not h:
            if allow_fetch:
                return {"ok": False,
                        "reason": f"查不到「{home_name}」的坐标（网络或地名问题）"}
            return {"ok": False, "need_fetch": True,
                    "reason": f"「{home_name}」的坐标还没缓存"}

    j = geocode(job_district, job_city, allow_fetch=allow_fetch)
    if not j:
        if allow_fetch:
            return {"ok": False,
                    "reason": f"查不到「{job_district}」的坐标（或城市不匹配）"}
        return {"ok": False, "need_fetch": True,
                "reason": f"「{job_district}」的坐标还没缓存"}

    straight = haversine_km(h, j)
    cross = home_city != job_city

    # 跨市不给分钟数。城际交通差异太大（高铁 30 分钟 / 自驾 2 小时 /
    # 大巴 3 小时），用市区速度算出来必然是错的（实测会算出 336 分钟）。
    # 给错数字比不给更糟 —— 用户会当真。
    if cross:
        return {
            "ok": True,
            "home": f"{home_city}{home_name}",
            "job": f"{job_city}{job_district}",
            "straight_km": round(straight, 1),
            "road_km": None,
            "minutes": None,
            "level": "跨城",
            "note": "跨市通勤：本工具不给分钟数——高铁/自驾/大巴能差 2 小时以上，"
                    "请用地图或 12306 自己查",
            "cross_city": True,
        }

    road = straight * ROAD_FACTOR
    minutes = round(road / _speed_for(straight) * 60)
    lv, note = level_of(straight)
    return {
        "ok": True,
        "home": f"{home_city}{home_name}",
        "job": f"{job_city}{job_district}",
        "straight_km": round(straight, 1),
        "road_km": round(road, 1),
        "minutes": minutes,
        "level": lv,
        "note": note,
        "cross_city": False,
    }


def render(r: dict) -> str:
    if not r.get("ok"):
        return f"通勤：无法估算 —— {r.get('reason', '')}"
    head = f"通勤：{r['home']} → {r['job']}"
    if r.get("minutes") is None:
        return (f"{head}\n"
                f"  直线 {r['straight_km']} km · {r['level']}\n"
                f"  {r['note']}")
    return (f"{head}\n"
            f"  直线 {r['straight_km']} km · 估算路程 {r['road_km']} km · "
            f"约 {r['minutes']} 分钟（{r['level']}）\n"
            f"  {r['note']}\n"
            f"  注：区镇中心估算，公司在区内具体位置不同会差 15-40 分钟")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if not args:
        cfg = load_home()
        print(f"当前家：{cfg}" if cfg
              else "还没设置家的位置。用法: python commute.py set 威远岛 东莞")
        sys.exit(0)
    if args[0] == "set":
        if len(args) < 3:
            print("用法: python commute.py set <区镇> <市> [lat] [lon]")
            sys.exit(2)
        lat = float(args[3]) if len(args) > 3 else None
        lon = float(args[4]) if len(args) > 4 else None
        print("已设置：", save_home(args[1], args[2], lat, lon))
        sys.exit(0)
    if len(args) >= 4:
        print(render(estimate(args[0], args[1], args[2], args[3])))
        sys.exit(0)
    print("用法: python commute.py <家的区镇> <家所在市> <岗位区镇> <岗位市>")
    print('例:  python commute.py 虎门镇 东莞 南山区 深圳')
    sys.exit(2)
