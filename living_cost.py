"""异地入职的收支测算：这份工资在那边每月能剩多少。

为什么不看人均
--------------
人均消费支出是**所有人平均**，包括有房无贷的和月租八千的。刚毕业一个人
租房过日子的成本结构，跟"人均"完全是两回事 —— 用它算等于拿马云的收入
估你的房租。所以这个工具**不用任何人均统计数**。

那算什么
--------
分两块，一块能算准，一块给参考：

1. **到手工资**（这块能算准，不是估算）
   社保公积金 = 税前 × 15.3%（养老8 + 医疗2 + 失业0.3 + 公积金5）
   个税 = 七级超额累进，含租房专项附加扣除（深圳/广州 1500，其他 1100）
   依据：《个人所得税法》《个人所得税专项附加扣除暂行办法》

2. **支出**，两条路：
   - **自己填**：房租、吃饭、交通、其他，你要过什么生活只有你知道。
   - **分区档位**：`balance(8000, "深圳", district="南山区", tier="normal")`。
     数据在 `data/living_cost.json`（五城 38 个区/镇街，租房/住家/合租AA 三种
     场景 × 省着过/正常两档），每档带**分项区间 + 房租来源 URL + 估算依据**。
     房租是平台挂牌实测（带月份和链接），水电/餐饮/交通/通讯日杂是估算项——
     输出里会逐条标出来，并给出区间（档位值是分项中值，租在区间上沿要往上加）。
     重建数据：`python build_living_cost_data.py --src <原始JSON>`。

   没填的项会单独列出来提醒：少填一项，算出来的结余就虚高一块。

输出里多给两个数：
- **结余率**：结余占到手比例。低于 20% 说明抗风险能力很弱。
- **攒出 3 个月开销要多久**：这才是"这份工作能不能让我站稳"的真实答案。
"""
from __future__ import annotations

import json
import os
import re

# 个人缴纳比例（养老8 + 医疗2 + 失业0.3 + 公积金5）
SOCIAL_RATE = {"深圳": 0.153, "广州": 0.153, "东莞": 0.153,
               "珠海": 0.153, "佛山": 0.153, "惠州": 0.153}
DEFAULT_SOCIAL_RATE = 0.153

# 租房专项附加扣除（元/月）：直辖市、省会、计划单列市 1500，其他 1100
RENT_DEDUCTION = {"深圳": 1500, "广州": 1500, "东莞": 1100,
                  "珠海": 1100, "佛山": 1100, "惠州": 1100}
DEFAULT_RENT_DEDUCTION = 1100

# 个税月度税率表（综合所得年度税率表折月，够用在 10K 以内）
TAX_BRACKETS = (
    (3000, 0.03, 0),
    (12000, 0.10, 210),
    (25000, 0.20, 1410),
    (35000, 0.25, 2660),
    (55000, 0.30, 4410),
    (80000, 0.35, 7160),
    (float("inf"), 0.45, 15160),
)

SPEND_KEYS = ("rent", "food", "transport", "other")
SPEND_LABELS = {"rent": "房租", "food": "吃饭",
                "transport": "交通", "other": "其他"}

# ---- 预置档位（data/living_cost.json）-------------------------------------
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "living_cost.json")
# 场景：租房（默认）/ 住家（东莞虎门威远岛通勤≤60分钟的镇街，rent=0）/
#       合租AA（佛山三水与同学分摊）
SCENE_LABELS = {"rent": "租房", "home": "住家", "co_rent": "合租AA"}
TIER_LABELS = {"frugal": "省着过", "normal": "正常"}
COST_ITEMS = ("rent", "utilities", "food", "transport", "comm_daily")
COST_ITEM_LABELS = {"rent": "房租", "utilities": "水电燃气", "food": "吃饭",
                    "transport": "交通", "comm_daily": "通讯日杂"}
# (场景, 档位) → 数据里的房租区间键；None = 该场景没有房租区间（住家）
RENT_RANGE_KEY = {("rent", "frugal"): "city_village_studio",
                  ("rent", "normal"): "city_village_one_bedroom",
                  ("co_rent", "frugal"): "apartment_shared_room",
                  ("co_rent", "normal"): "apartment_shared_room",
                  ("home", "frugal"): None, ("home", "normal"): None}

_data_cache: dict | None = None


def load_data() -> dict:
    """读内置档位数据；文件缺失/坏了直接抛错 —— 不静默返回空。"""
    global _data_cache
    if _data_cache is None:
        if not os.path.exists(DATA_PATH):
            raise FileNotFoundError(
                f"缺 {DATA_PATH} —— 用 build_living_cost_data.py 从原始 JSON 重建")
        with open(DATA_PATH, encoding="utf-8") as f:
            _data_cache = json.load(f)
    return _data_cache


def reset_cache() -> None:
    global _data_cache
    _data_cache = None


def covered_cities() -> list:
    return list(load_data()["cities"].keys())


def districts(city: str) -> list:
    c = load_data()["cities"].get(city)
    return list(c["districts"].keys()) if c else []


def data_meta() -> dict:
    d = load_data()
    return {"collected_at": d.get("collected_at", ""),
            "coverage": d.get("coverage", {}),
            "source": d.get("source_title") or d.get("source", "")}


# 区名归一化：采集端写「南山区/南城街道/虎门镇/大鹏新区」，数据里是「南山/南城/虎门」
_SUFFIXES = ("经济开发区", "开发区", "街道", "新区", "镇", "区", "市")


def _bare(name: str) -> str:
    """去掉括号补充说明和行政级别后缀，只留核心地名。"""
    n = re.sub(r"[（(].*?[)）]", "", (name or "").strip())
    for suf in _SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf) + 1:
            return n[: -len(suf)]
    return n


def resolve_district(city: str, district: str) -> str | None:
    """采集/用户写的区名 → 数据里的规范区名；对不上返回 None（不猜）。"""
    names = districts(city)
    raw = (district or "").strip()
    if not raw:
        return None
    if raw in names:
        return raw
    b = _bare(raw)
    for n in names:
        if _bare(n) == b:
            return n
    # 括号里放别名的情况：数据名「唐家湾（高新区）」← 输入「高新区」
    for n in names:
        for inner in re.findall(r"[（(](.*?)[)）]", n):
            if b == _bare(inner) or b == inner:
                return n
    return None


def _agg(city: str, scene: str, tier: str) -> dict | None:
    """没给区时取城市内各区该档位的中位数（分区档位中位，不是人均统计）。"""
    c = load_data()["cities"].get(city)
    if not c:
        return None
    rows = [d["tiers"][scene][tier] for d in c["districts"].values()
            if scene in d["tiers"] and tier in d["tiers"][scene]]
    if not rows:
        return None

    def med(key: str) -> float:
        vals = sorted(r[key] for r in rows)
        mid = len(vals) // 2
        v = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        return round(v)

    out = {k: med(k) for k in COST_ITEMS}
    out["total"] = med("total")
    out["note"] = f"城市内 {len(rows)} 个区/镇街该档位的中位，具体到区会更准"
    return {"breakdown": out, "n": len(rows)}


def plan_cost(city: str, district: str | None = None,
              tier: str = "normal", scene: str = "rent") -> dict:
    """取某城某区某档的月支出。查不到就说查不到，不编数、不静默按 0 算。"""
    if tier not in TIER_LABELS:
        return {"ok": False,
                "reason": f"档位只有 {'/'.join(TIER_LABELS)}，你给的是「{tier}」"}
    if scene not in SCENE_LABELS:
        return {"ok": False,
                "reason": f"场景只有 {'/'.join(SCENE_LABELS)}，你给的是「{scene}」"}

    data = load_data()
    c = data["cities"].get(city)
    if not c:
        return {"ok": False, "missing_city": True,
                "reason": f"没有「{city}」的预置数据"
                          f"（覆盖：{'、'.join(covered_cities())}）"
                          f" —— 支出请手填，我不给你编一个"}

    meta = {"collected_at": data.get("collected_at", ""),
            "source_title": data.get("source_title") or data.get("source", "")}

    if district:
        name = resolve_district(city, district)
        if not name:
            return {"ok": False, "unmatched_district": True,
                    "reason": f"{city} 没有「{district}」的预置数据"
                              f"（覆盖：{'、'.join(districts(city))}）"
                              f" —— 换一个区，或去掉区用城市中位，或手填支出"}
        d = c["districts"][name]
        if scene not in d["tiers"] or tier not in d["tiers"][scene]:
            return {"ok": False, "missing_scene": True,
                    "available_scenes": sorted(d["tiers"].keys()),
                    "reason": f"{city}{name} 没有「{SCENE_LABELS[scene]}/"
                              f"{TIER_LABELS[tier]}」档（数据里未提供，"
                              f"通常意味着这种住法在该区不适用）"}
        v = dict(d["tiers"][scene][tier])
        rent_range = None
        rk = RENT_RANGE_KEY.get((scene, tier))
        if rk and (d.get("rent_ranges") or {}).get(rk):
            rent_range = list(d["rent_ranges"][rk])
        food_range = (d.get("food_ranges") or {}).get(tier)
        out = {"city": city, "district": name, "district_input": district,
               "basis": "district", "rent_source": d.get("rent_source") or {},
               "job_hint": d.get("job_hint") or "",
               "estimate_basis": d.get("estimate_basis") or {},
               "rent_range": rent_range,
               "food_range": list(food_range) if food_range else None,
               "covered_districts": 1}
    else:
        a = _agg(city, scene, tier)
        if not a:
            return {"ok": False, "missing_scene": True,
                    "reason": f"{city} 没有「{SCENE_LABELS[scene]}/"
                              f"{TIER_LABELS[tier]}」档"}
        v = dict(a["breakdown"])
        out = {"city": city, "district": None, "district_input": None,
               "basis": "city_median", "rent_source": {},
               "job_hint": "", "estimate_basis": {},
               "rent_range": None, "food_range": None,
               "covered_districts": a["n"]}

    total = v["total"]
    # 区间：档位值是分项中值，房租/餐饮按数据区间上下浮动
    rng = None
    if scene == "home":
        rng = [total, total]      # 住家无房租，区间不存在
    elif out["rent_range"] or out["food_range"]:
        rent_lo, rent_hi = (out["rent_range"] or [v["rent"], v["rent"]])
        food_lo, food_hi = (out["food_range"] or [v["food"], v["food"]])
        low = round(total + (rent_lo - v["rent"]) + (food_lo - v["food"]))
        high = round(total + (rent_hi - v["rent"]) + (food_hi - v["food"]))
        rng = [max(0, low), high]

    out.update({
        "ok": True,
        "scene": scene, "scene_label": SCENE_LABELS[scene],
        "tier": tier, "tier_label": TIER_LABELS[tier],
        "breakdown": {k: v[k] for k in COST_ITEMS},
        "total": total,
        "total_range": rng,
        "note": v.get("note") or "",
        # 给 balance() 的四栏口径：水电+通讯日杂并入「其他」，省一层参数
        "spend": {"rent": v["rent"], "food": v["food"],
                  "transport": v["transport"],
                  "other": v["utilities"] + v["comm_daily"]},
        "estimated": True,
        "estimate_items": ["utilities", "food", "transport", "comm_daily"],
        **meta,
    })
    return out


def render_plan(p: dict) -> str:
    if not p.get("ok"):
        return f"预置档位取不到：{p.get('reason', '')}"
    where = f"{p['city']}{p['district']}" if p["district"] \
        else f"{p['city']}（城市中位）"
    rng = p.get("total_range")
    rng_txt = f"，区间 {rng[0]:,}-{rng[1]:,}" if rng and rng[0] != rng[1] else ""
    lines = [f"{where} · {p['scene_label']}/{p['tier_label']}："
             f"约 {p['total']:,} 元/月{rng_txt}"]
    for k in COST_ITEMS:
        lines.append(f"  {COST_ITEM_LABELS[k]:<5}{p['breakdown'][k]:>6,}")
    if p.get("note"):
        lines.append(f"  注：{p['note']}")
    rs = p.get("rent_source") or {}
    if rs.get("platform"):
        lines.append(f"  房租来源：{rs['platform']} {rs.get('month', '')} "
                     f"{rs.get('url', '')}".rstrip())
    lines.append(f"  水电/餐饮/交通/通讯日杂为估算项（采集 "
                 f"{p.get('collected_at', '')}）；房租为平台挂牌口径")
    return "\n".join(lines)


def _row(p: dict, pretax: float) -> dict:
    r = {"district": p.get("district") or "城市中位",
         "scene": p["scene"], "tier": p["tier"],
         "tier_label": p["tier_label"],
         "total": p["total"], "total_range": p.get("total_range")}
    if pretax:
        th = take_home(pretax, p["city"])
        r["left"] = round(th["net"] - p["total"])
        r["save_rate"] = round(r["left"] / th["net"] * 100, 1) if th["net"] else None
    return r


def compare_tiers(city: str, pretax: float = 0, district: str | None = None,
                  scene: str = "rent") -> dict:
    """对比：给了区就同区各档，不给就全城各区；按总支出升序。"""
    if city not in covered_cities():
        return {"ok": False,
                "reason": f"没有「{city}」的预置数据"
                          f"（覆盖：{'、'.join(covered_cities())}）"}
    rows = []
    targets = [district] if district else districts(city)
    for name in targets:
        for tier in TIER_LABELS:
            p = plan_cost(city, name, tier, scene)
            if p.get("ok"):
                rows.append(_row(p, pretax))
            elif not district:
                continue        # 全城扫描时缺档跳过，单区查询时必须报错
    if not rows:
        bad = plan_cost(city, district, "normal", scene) if district else {}
        return {"ok": False,
                "reason": bad.get("reason") or "没有可对比的档位"}
    rows.sort(key=lambda r: r["total"])
    return {"ok": True, "city": city, "scene": scene, "rows": rows,
            "meta": data_meta()}


def render_compare(c: dict, pretax: float = 0) -> str:
    if not c.get("ok"):
        return f"对比失败：{c.get('reason', '')}"
    head = f"{c['city']} · {SCENE_LABELS.get(c['scene'], c['scene'])}档"
    if pretax:
        head += f" · 税前 {pretax/1000:g}K"
    lines = [head + f"（数据采集 {c['meta'].get('collected_at', '')}）", ""]
    lines.append(f"{'区/镇街':<10}{'档位':<7}{'月支出':>8}{'区间':>16}"
                 + (f"{'结余':>9}{'结余率':>8}" if pretax else ""))
    for r in c["rows"]:
        rng = r.get("total_range")
        rng_txt = f"{rng[0]:,}-{rng[1]:,}" if rng and rng[0] != rng[1] else "-"
        line = f"{r['district']:<10}{r['tier_label']:<7}{r['total']:>8,}{rng_txt:>16}"
        if pretax:
            line += f"{r['left']:>+9,}{(r.get('save_rate') or 0):>7.1f}%"
        lines.append(line)
    lines.append("")
    lines.append("支出是分区档位（房租有来源 URL，日常四项为估算项），不是实测账单；"
                 "结余 = 税前精算到手 - 支出。")
    return "\n".join(lines)


def income_tax(taxable: float) -> float:
    """月度应纳税所得额 → 个税。"""
    if taxable <= 0:
        return 0.0
    for cap, rate, deduct in TAX_BRACKETS:
        if taxable <= cap:
            return max(0.0, taxable * rate - deduct)
    return 0.0


def take_home(pretax: float, city: str, rent_deduction: bool = True) -> dict:
    """税前月薪 → 到手。社保公积金和个税都能算准，这块没有估算。"""
    rate = SOCIAL_RATE.get(city, DEFAULT_SOCIAL_RATE)
    social = round(pretax * rate, 2)

    ded = 0.0
    if rent_deduction:
        ded = RENT_DEDUCTION.get(city, DEFAULT_RENT_DEDUCTION)

    taxable = pretax - social - 5000 - ded
    tax = round(income_tax(taxable), 2)
    net = round(pretax - social - tax, 2)

    return {
        "pretax": pretax,
        "social_rate": rate,
        "social": social,
        "tax": tax,
        "rent_deduction": ded,
        "net": net,
        "effective_rate": round((pretax - net) / pretax * 100, 1) if pretax else 0,
    }


def balance(pretax: float, city: str,
            rent: float = 0, food: float = 0,
            transport: float = 0, other: float = 0,
            district: str | None = None, tier: str | None = None,
            scene: str = "rent") -> dict:
    """核心：这份工资在那边每月能剩多少。

    支出两种给法：填了数值就用你的（显式优先），没填的项用 district/tier
    取预置档位兜底。取不到档位就返回 ok=False —— 缺数据时绝不按 0 算
    再给个漂亮结余。
    """
    plan = None
    preset: dict = {}
    if tier or district:
        plan = plan_cost(city, district, tier or "normal", scene)
        if not plan.get("ok"):
            return {"ok": False, "city": city, "reason": plan.get("reason", ""),
                    "plan": plan}

    spend = {"rent": rent, "food": food,
             "transport": transport, "other": other}
    if plan:
        # 预置档位齐全时按档位兜底：房租为 0（住家档）也是有效值，不算"没填"
        for k in SPEND_KEYS:
            if not spend[k]:
                spend[k] = plan["spend"][k]
                preset[k] = plan["spend"][k]

    th = take_home(pretax, city)
    total_spend = sum(spend.values())
    left = round(th["net"] - total_spend, 2)

    missing = [] if plan else [
        SPEND_LABELS[k] for k in SPEND_KEYS if not spend.get(k)]
    warnings = []

    if plan:
        label = (f"{plan['district'] or '城市中位'}·"
                 f"{plan['scene_label']}/{plan['tier_label']}")
        warnings.append(
            f"支出含 {len(preset)} 项预置档位（{label}，水电/餐饮/交通/日杂"
            f"为估算项，采集 {plan.get('collected_at', '')}）—— 不是你的真实账单，"
            f"拿到租房报价后改成实填")
        if plan.get("basis") == "city_median":
            warnings.append(
                f"没指定区，用的是{plan['city']} {plan['covered_districts']} 个区"
                f"的档位中位 —— 岗位带区时再算一次更准")
        if len(preset) < 4:
            warnings.append(
                f"其中 {len(preset)} 项用预置兜底、{4 - len(preset)} 项用你填的值"
                f"（手填的优先）")
    if missing:
        warnings.append(
            f"没填：{'、'.join(missing)} —— 这几项按 0 算，结余是虚高的")

    out = {
        "ok": True,
        "city": city,
        "pretax": pretax,
        "take_home": th,
        "spend": spend,
        "total_spend": total_spend,
        "left": left,
        "save_rate": None,
        "runway_months": None,
        "missing": missing,
        "warnings": warnings,
        "preset": preset,
        "plan": plan,
    }

    if th["net"]:
        out["save_rate"] = round(left / th["net"] * 100, 1)

    if spend["rent"] and th["net"]:
        out["rent_ratio"] = round(spend["rent"] / th["net"] * 100, 1)
        if out["rent_ratio"] > 40:
            warnings.append(
                f"房租占到手 {out['rent_ratio']}% —— 超过 30% 就很难存钱，"
                f"这个价位要再压一压")

    if left < 0:
        warnings.append("月结余是负的 —— 这份工资撑不住这套开销，得改其中一头")
    elif left < 1000:
        warnings.append("月结余低于 1000 —— 出一次意外就见底，没有容错")

    # 攒应急金：3 个月开销是最低标准
    if left > 0 and total_spend > 0:
        out["runway_months"] = round(total_spend * 3 / left, 1)

    return out


def render(b: dict) -> str:
    if not b.get("ok"):
        return f"收支测算失败：{b.get('reason', '')}"

    th = b["take_home"]
    plan = b.get("plan")
    head = f"收支测算：{b['city']} · 税前 {b['pretax']:,.0f} 元/月"
    if plan:
        where = plan["district"] or "城市中位"
        head += f" · {where} {plan['scene_label']}/{plan['tier_label']}"
    lines = [head]

    if plan and plan.get("total_range") and \
            plan["total_range"][0] != plan["total_range"][1]:
        r = plan["total_range"]
        lines.append(f"该档月支出参考 {plan['total']:,}（区间 {r[0]:,}-{r[1]:,}，"
                     f"租在区间上沿就往上涨）")

    lines += [
        "",
        f"到手       {th['net']:>9,.0f}"
        f"   社保公积金 {th['social_rate']*100:.1f}% = {th['social']:,.0f}"
        f"，个税 {th['tax']:,.0f}",
    ]

    for k in SPEND_KEYS:
        v = b["spend"][k]
        label = SPEND_LABELS[k]
        if k == "rent" and plan and plan.get("scene") == "home":
            lines.append(f"{label:<9}  {'住家':>9}   （无房租，住自己家）")
            continue
        if v:
            extra = ""
            if k == "rent" and b.get("rent_ratio"):
                extra = f"   （占到手 {b['rent_ratio']}%）"
            if b.get("preset", {}).get(k):
                extra += "   （预置档位）"
            lines.append(f"{label:<9}  {-v:>9,.0f}{extra}")
        else:
            lines.append(f"{label:<9}  {'未填':>9}")

    lines.append("")
    money = f"{b['left']:+,.0f}"        # +1,789 / -500
    lines.append(f"月结余     {money:>9}")

    if b.get("save_rate") is not None:
        sr = b["save_rate"]
        lines.append(f"结余率     {sr:>8.1f}%"
                     + ("    （低于 20% 抗风险很弱）" if sr < 20 else ""))

    if b.get("runway_months"):
        lines.append(f"攒 3 个月开销要 {b['runway_months']:g} 个月")

    if b["warnings"]:
        lines.append("")
        for w in b["warnings"]:
            lines.append(f"⚠ {w}")

    lines.append("")
    lines.append("社保公积金和个税按现行规则精算；支出按你填的数，"
                 "本工具不猜生活成本。")
    if b.get("plan"):
        lines.append("预置档位（data/living_cost.json）是可核对的参考，不是你的账单："
                     "房租有来源 URL，日常四项是估算项，用前看上面的区间。")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python living_cost.py <城市> <税前月薪K> [房租] [吃饭] [交通] [其他]")
        print("例:   python living_cost.py 深圳 9 3500 1500 300 500")
        print("      python living_cost.py 广州 8 --district 天河 --tier normal")
        sys.exit(2)
    city = sys.argv[1]
    pretax = float(sys.argv[2]) * 1000
    argv = sys.argv[3:]
    kw = {}
    for flag in ("--district", "--tier", "--scene"):
        if flag in argv:
            i = argv.index(flag)
            kw[flag[2:]] = argv[i + 1]
            del argv[i:i + 2]
    vals = [float(x) for x in argv[:4]]
    vals += [0.0] * (4 - len(vals))
    print(render(balance(pretax, city, *vals, **kw)))
