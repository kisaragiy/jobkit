"""公司性质识别：外包 / 人力派遣 / 自研。

为什么值得单独做
----------------
投人力外包和投自研是两条完全不同的路：
- 派遣岗大概率是「驻场做客户的项目」，技术栈由客户决定，不由你决定
- 项目结束即走人，稳定性差
- 简历上「中软国际（驻场某银行）」和「某银行」是两件事
- 对「想做 AI 应用开发」的人来说，外包岗大概率是 CRUD + 运维

设计原则：**能解释为什么**，不做「看着像就判」。
三层判定：
1. confirmed —— 命中已知外包名录（人工维护，可在本文件里加）
2. suspected —— 命中 2 个以上独立信号（岗位广撒网 / 岗位类型分散 / 跨多城市重复）
3. ok —— 没命中

误报代价 = 少投一个真公司；漏报代价 = 白投一次。
所以单信号一律不判 suspected，必须 ≥2 个独立信号。
"""
from __future__ import annotations

import re

# ── 第一层：已知 IT 外包 / 人力派遣（子串匹配，命中即 confirmed）──────────
# 只放「明确以承接外包/派遣为主业」的公司。像东软、太极、神州数码有自研业务，
# 放进来会误伤，所以只在有额外信号时才判 suspected。
CONFIRMED_OUTSOURCING = (
    "中软国际", "软通动力", "文思海辉", "中电金信", "博彦科技", "法本",
    "亿达信息", "思芮", "外企德科", "FESCO", "微创软件", "上海兴岩",
    "赛意信息", "拓维信息", "润和软件", "京北方", "宇信科技", "长亮科技",
    "高伟达", "浩鲸", "易思博", "佰钧成", "护航科技", "华信咨询",
    "中电科金仓", "艾克斯特", "东方国信", "中科江南",
)

# 名称里带这些词，基本就是人力服务公司
STAFFING_KEYWORDS = (
    "人力资源", "劳务派遣", "劳务服务", "人力外包", "人才服务",
    "外服", "灵活用工", "众包", "人力科技",
)

# ── 第二层：外包特征信号 ────────────────────────────────────────────
MIN_JOBS_FOR_SIGNAL = 8        # 岗位数太少，统计没意义
WIDE_TITLE_THRESHOLD = 6       # 不同岗位类型 ≥6 种
WIDE_CITY_THRESHOLD = 3        # 跨 ≥3 个城市


def _norm_title(t: str) -> str:
    """把岗位名归一化：去掉城市/级别/括号后缀，只留职能主干。

    「AI应用开发工程师（广州）」和「AI应用开发工程师-深圳」应算同一类。
    """
    s = str(t or "")
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"[-—/|].*$", "", s)
    s = re.sub(r"(高级|资深|初级|中级|senior|junior|急招|双休|包吃住)", "", s, flags=re.I)
    return s.strip()


def _name_hits(company: str) -> list[str]:
    co = str(company or "")
    hits = [k for k in CONFIRMED_OUTSOURCING if k in co]
    hits += [k for k in STAFFING_KEYWORDS if k in co]
    return hits


def analyze(company: str, jobs: list[dict] | None = None) -> dict:
    """判定一家公司。

    jobs 传该公司在库里的所有岗位（可空）。为空时只能靠名录判断。
    返回 {"level": "confirmed"|"suspected"|"ok", "reason": str, "signals": [...]}
    """
    co = str(company or "").strip()
    if not co:
        return {"level": "ok", "reason": "", "signals": []}

    hits = _name_hits(co)
    if hits:
        return {
            "level": "confirmed",
            "reason": f"已知人力外包/派遣：命中「{'、'.join(sorted(set(hits)))}」",
            "signals": [f"名录:{h}" for h in sorted(set(hits))],
        }

    if not jobs:
        return {"level": "ok", "reason": "", "signals": []}

    signals = []
    titles = {_norm_title(j.get("title")) for j in jobs if j.get("title")}
    cities = {j.get("city") for j in jobs if j.get("city")}
    industries = {j.get("industry") for j in jobs if j.get("industry")}

    if len(jobs) >= MIN_JOBS_FOR_SIGNAL and len(titles) >= WIDE_TITLE_THRESHOLD:
        signals.append(f"广撒网：{len(jobs)} 个岗位覆盖 {len(titles)} 种职能")
    if len(cities) >= WIDE_CITY_THRESHOLD:
        signals.append(f"跨 {len(cities)} 个城市重复挂岗")
    if len(industries) >= 3:
        signals.append(f"同时涉及 {len(industries)} 个不同行业")

    if len(signals) >= 2:
        return {"level": "suspected",
                "reason": "疑似外包/派驻：" + "；".join(signals),
                "signals": signals}
    return {"level": "ok", "reason": "", "signals": signals}


def label(company: str) -> str:
    """给界面用的短标签。"""
    a = analyze(company)
    return {"confirmed": "外包", "suspected": "疑似外包", "ok": ""}[a["level"]]


def is_outsourcing(company: str) -> bool:
    return analyze(company)["level"] == "confirmed"


def scan_db(db_path=None) -> list[dict]:
    """扫描整库，返回所有被判定为外包/疑似外包的公司 + 岗位数。

    只读，不改任何数据 —— 判定结果由调用方决定怎么用（过滤/提醒/打灰）。
    """
    import sqlite3
    from pathlib import Path

    path = db_path or (Path(__file__).parent / "jobkit.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT company, title, city, industry FROM jobs WHERE company IS NOT NULL AND company != ''"
    )]
    c.close()

    by_co: dict[str, list[dict]] = {}
    for r in rows:
        by_co.setdefault(r["company"], []).append(r)

    out = []
    for co, jobs in by_co.items():
        a = analyze(co, jobs)
        if a["level"] != "ok":
            out.append({"company": co, "jobs": len(jobs),
                        "level": a["level"], "reason": a["reason"]})
    out.sort(key=lambda x: (x["level"] != "confirmed", -x["jobs"]))
    return out


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    items = scan_db(path)
    confirmed = [x for x in items if x["level"] == "confirmed"]
    suspected = [x for x in items if x["level"] == "suspected"]
    n_jobs = sum(x["jobs"] for x in items)
    print(f"命中 {len(items)} 家（明确 {len(confirmed)} / 疑似 {len(suspected)}），"
          f"涉及 {n_jobs} 个岗位\n")
    for x in items:
        tag = "⛔" if x["level"] == "confirmed" else "⚠"
        print(f"{tag} {x['company'][:22]:22s} {x['jobs']:3d} 岗  {x['reason'][:60]}")
