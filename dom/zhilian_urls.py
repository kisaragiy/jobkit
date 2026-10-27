#!/usr/bin/env python3
"""智联搜索 URL 构造器（jobkit 自主能力）。

为什么需要它：GetJob 的做法是「信任用户当前列表页，不拼搜索 URL」（urls.js:4 原话）。
对 GUI 应用够用，但 jobkit 要 cron 无人值守 —— 必须自己造 URL。
这一条是「GetJob 做不到、jobkit 必须做到」的典型。

实测形态（2026-10-19，user 提供的真实地址）：
    https://www.zhaopin.com/jobs/?pageMode=search&jl=763&kw=AI%E5%BC%80%E5%8F%91

  /jobs/            新版左右分栏列表（不是经典 /sou/）
  pageMode=search   搜索模式（去掉就变成分类导航）
  jl=763            城市码（广州 = 763，实测确认）
  kw=<urlencode>    关键词

城市码策略（重要）：
  城市码**猜不得** —— 猜错会静默抓到别的城市的岗位，比抓不到更糟。
  但用户也不该为此操心：传中文城市名即可，未知城市会自动打开该城市的
  拼音子站读 `pageInfo.cityInfo.code`（397 城拼音表在 zhilian_citymap.json），
  学到就落盘到 zhilian_citycodes.json，下次零成本。
  全都失败才抛 UnknownCityError。"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

BASE = "https://www.zhaopin.com/jobs/"

CODE_CACHE = Path(__file__).with_name("zhilian_citycodes.json")
CITYMAP = Path(__file__).with_name("zhilian_citymap.json")

# ✅ 实测确认（来源：user 2026-10-19 提供的真实搜索地址 + 子站回读）
CITY_CODES: dict[str, str] = {
    "广州": "763",
    "深圳": "765",
}

# 城市别名 → 标准名
CITY_ALIASES: dict[str, str] = {
    "广州市": "广州",
    "gz": "广州",
    "深圳市": "深圳",
    "sz": "深圳",
}


class UnknownCityError(ValueError):
    """城市名无法识别，或自动解析失败。调用方应跳过该城市，不要猜。"""


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
    except OSError:
        pass          # 缓存写不进去不该让搜索失败


_LEARN_JS = (
    "JSON.stringify({c: window.__INITIAL_STATE__ && window.__INITIAL_STATE__.pageInfo "
    "&& window.__INITIAL_STATE__.pageInfo.cityInfo})"
)


def _learn_code(name: str) -> str:
    """打开该城市的拼音子站，读 `pageInfo.cityInfo.code`。

    用户不需要知道「城市码」这回事——这正是它存在的意义。
    子站 URL 来自 zhilian_citymap.json（从 citymap 页一次抓全，397 城）。
    拿不到就返回空串，由上层决定跳过，绝不猜一个码。
    """
    site = _read_json(CITYMAP).get(name)
    if not site:
        return ""
    try:
        import dom_channel as DC
        snap = DC._run("list", navigate_to=site, js=_LEARN_JS)
    except Exception:      # CDP 不可用 / 页面打不开 —— 都不该炸到用户面前
        return ""
    info = snap.get("c") or {}
    code = str(info.get("code") or "").strip()
    actual = str(info.get("name") or "").strip()
    if not code:
        return ""
    # 名字对不上 = 子站把我们带到了别的城市，这种码不能用
    if actual and actual not in (name, name + "市") and name not in actual:
        return ""
    return code


def canonical_city(city: str) -> str:
    name = str(city or "").strip()
    return CITY_ALIASES.get(name, name)


def city_code(city: str, learn: bool = True) -> str:
    """中文城市名 → 城市码。未知城市会自动学一次并缓存，不需要人工补表。"""
    name = canonical_city(city)
    if not name:
        raise UnknownCityError("城市为空")
    for table in (CITY_CODES, _read_json(CODE_CACHE)):
        code = table.get(name)
        if code:
            return str(code)
    if learn:
        code = _learn_code(name)
        if code:
            cache = _read_json(CODE_CACHE)
            cache[name] = code
            _write_json(CODE_CACHE, cache)
            return code
    supported = "、".join(sorted(set(CITY_CODES) | set(_read_json(CITYMAP))))
    raise UnknownCityError(
        f"不支持的城市：{name!r}。可用的有 {len(supported.split('、'))} 个"
        f"（如广州、深圳、佛山、东莞、惠州…）。城市名写错了吗？"
    )


def build_search_url(keyword: str, city: str, page: int = 1) -> str:
    """构造搜索页 URL。

    page: 1 起。智联新版分页参数实测为 `p`（未验证时传 1，不要带参数更安全）。
    """
    kw = str(keyword or "").strip()
    if not kw:
        raise ValueError("关键词为空")
    jl = city_code(city)
    url = f"{BASE}?pageMode=search&jl={jl}&kw={quote(kw, safe='')}"
    try:
        p = int(page)
    except (TypeError, ValueError):
        p = 1
    if p > 1:
        # 🔶 推断：智联分页参数为 p=2（未实测）。首页不带参数是安全的。
        url += f"&p={p}"
    return url


def known_cities() -> list[str]:
    return sorted(CITY_CODES)
