"""把外部采集的「五城生活成本」JSON 转成 jobkit 内置的分区档位数据。

为什么要有这一步
----------------
数据是豆包那边抓的（统计局公报 + 58/安居客挂牌，逐项带 URL 和估算依据），
原始文件 155KB、字段为阅读设计（人看的大段 note、重复的 basis、城市级工资参考）。
jobkit 只需要「某城某区的四档月支出」，所以这里做一次降维：

- 只留 frugal / normal / home / co_rent 四档 + 区间 + 来源 + 估算依据
- 丢掉城市级工资参考（jobkit 自己有 filters/priority 的目标薪资逻辑，不重复）
- 每条档位必须满足 `分项之和 == total`，不满足直接报错退出（数据坏了不许静默进库）

用法
----
    python build_living_cost_data.py --src "C:/path/五城生活成本数据.json"
    python build_living_cost_data.py --src ... --check   # 只校验不写盘

数据口径以源文件为准：rent 默认整租、水电默认民水民电、工资一律税前。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SRC_DEFAULT = ("C:/Users/<user>/Doubao/chats/2026-10-20/new-chat/"
               "广深莞佛珠五城生活成本数据.json")
OUT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "living_cost.json")

TIERS = ("frugal", "normal", "home", "co_rent")
COST_FIELDS = ("rent", "utilities", "food", "transport", "comm_daily")
RANGE_KEYS = ("city_village_studio", "city_village_one_bedroom",
              "apartment_shared_room", "apartment_one_bedroom")


class BuildError(RuntimeError):
    pass


def _check_tier(city: str, district: str, tier: str, v: dict) -> dict:
    missing = [f for f in COST_FIELDS if not isinstance(v.get(f), (int, float))]
    if missing:
        raise BuildError(f"{city}/{district}/{tier} 缺分项：{missing}")
    s = sum(v[f] for f in COST_FIELDS)
    if s != v.get("total"):
        raise BuildError(
            f"{city}/{district}/{tier} 分项和 {s} != total {v.get('total')}")
    return {f: v[f] for f in COST_FIELDS} | {"total": v["total"],
                                            "note": v.get("note") or ""}


def build(src: str) -> dict:
    with open(src, encoding="utf-8") as f:
        raw = json.load(f)

    cities: dict = {}
    n_districts = n_tiers = 0

    for c in raw.get("cities") or []:
        name = (c.get("city") or "").strip()
        if not name:
            raise BuildError("有城市缺 city 字段")
        ds: dict = {}
        for d in c.get("districts") or []:
            dname = (d.get("district") or "").strip()
            if not dname:
                raise BuildError(f"{name} 有区缺 district 字段")

            rent = d.get("rent") or {}
            ranges = {k: rent.get(k) for k in RANGE_KEYS}
            ranges["apartment_one_bedroom_avg"] = rent.get(
                "apartment_one_bedroom_avg")
            food = d.get("food") or {}
            food_ranges = {"frugal": food.get("frugal_monthly"),
                           "normal": food.get("normal_monthly")}

            tiers: dict = {}   # scene -> {frugal, normal}

            def add(scene: str, tier: str, v) -> None:
                if tier not in ("frugal", "normal"):
                    return
                if not isinstance(v, dict):
                    raise BuildError(f"{name}/{dname}/{scene}/{tier} 不是对象")
                tiers.setdefault(scene, {})[tier] = _check_tier(
                    name, dname, f"{scene}.{tier}", v)

            for tier, v in (d.get("monthly_cost") or {}).items():
                add("rent", tier, v)
            for tier, v in (d.get("home_mode") or {}).items():
                add("home", tier, v)
            for tier, v in (d.get("co_rent") or {}).items():
                add("co_rent", tier, v)

            if not tiers.get("rent"):
                raise BuildError(f"{name}/{dname} 缺租房档（monthly_cost）")

            ds[dname] = {
                "job_hint": d.get("job_hint") or "",
                "rent_source": rent.get("source") or {},
                "shared_rent_note": rent.get("shared_rent_note") or "",
                "rent_ranges": ranges,
                "food_ranges": food_ranges,
                "estimate_basis": {
                    "utilities": (d.get("utilities") or {}).get("basis") or "",
                    "food": food.get("basis") or "",
                    "transport": (d.get("transport") or {}).get("basis") or "",
                    "comm_daily": (d.get("comm_daily") or {}).get("basis") or "",
                },
                "utilities_caliber": (d.get("utilities") or {}).get("caliber") or "",
                "food_caliber": food.get("caliber") or "",
                "transport_mode": (d.get("transport") or {}).get("mode") or "",
                "tiers": tiers,
            }
            n_districts += 1

        cities[name] = {
            "data_year": c.get("data_year") or "",
            "user_context": c.get("user_context") or "",
            "note": c.get("special_notes") or "",
            "districts": ds,
        }
        for d in ds.values():
            n_tiers += sum(len(v) for v in d["tiers"].values())

    if not cities:
        raise BuildError("源文件里没有城市数据")

    return {
        "schema": 1,
        "title": "五城分区生活成本档位（jobkit 内置）",
        "source": os.path.basename(src),
        "source_title": raw.get("document_title") or "",
        "collected_at": raw.get("collected_at") or "",
        "currency": raw.get("currency") or "CNY",
        "unit": "元/月",
        "caliber": {
            "rent": raw.get("rent_caliber") or "",
            "utility": raw.get("utility_caliber") or "",
            "wage": raw.get("wage_caliber") or "",
            "estimate": raw.get("estimate_rule") or "",
        },
        "definitions": raw.get("definitions") or {},
        "coverage": {"cities": len(cities), "districts": n_districts,
                     "tiers": n_tiers},
        "cities": cities,
    }


def render_summary(out: dict) -> str:
    cov = out["coverage"]
    lines = [f"城市 {cov['cities']} · 区/镇街 {cov['districts']} · 档位 {cov['tiers']}"
             f"（采集 {out['collected_at']}）"]
    for city, c in out["cities"].items():
        keys = set()
        for d in c["districts"].values():
            keys |= set(d["tiers"].keys())
        lines.append(f"  {city:<4} {len(c['districts']):>2} 区  档位 {'/'.join(sorted(keys))}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建 jobkit 内置生活成本档位数据")
    ap.add_argument("--src", default=SRC_DEFAULT, help="源 JSON 路径")
    ap.add_argument("--out", default=OUT_DEFAULT, help="输出路径")
    ap.add_argument("--check", action="store_true", help="只校验，不写盘")
    a = ap.parse_args(argv)

    if not os.path.exists(a.src):
        print(f"❌ 源文件不存在：{a.src}")
        return 2
    try:
        out = build(a.src)
    except BuildError as e:
        print(f"❌ 数据不合格，未写盘：{e}")
        return 1

    print(render_summary(out))
    if a.check:
        print("✅ 校验通过（--check 未写盘）")
        return 0

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(a.out)
    print(f"✅ 已写 {a.out}（{size/1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
