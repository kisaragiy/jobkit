#!/usr/bin/env python3
"""按类别批量设置黑名单，并量化每条关键词的影响。

为什么要量化：黑名单最容易犯的错是「一刀切过头」，投递池被清空还不知道为什么。
这个脚本会逐条统计每个关键词挡掉多少岗位，让人能判断是否误伤。

用法：
    python apply_blacklist.py            # 应用预设黑名单
    python apply_blacklist.py --dry      # 只看影响，不写入
    python apply_blacklist.py --clear    # 清空黑名单
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request as u
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules  # noqa: E402
import settings as st  # noqa: E402
import store  # noqa: E402

API = "http://127.0.0.1:18770"

# ══════════════════════════════════════════════════════════
# 预设黑名单
# ══════════════════════════════════════════════════════════

# 公司名里出现 → 中介 / 人力 / 外包性质，不是真雇主
BL_COMPANY = [
    # 中介 / 人力
    "人力资源", "劳务", "派遣", "中介", "猎头", "人才服务", "职业介绍",
    "众包", "招聘服务", "人才市场", "人才代理",
    # 外包 / 驻场
    "外包", "驻场", "外派", "服务外包", "信息技术服务",
    # 明显的空壳/挂靠
    "信息技术有限公司分公司",
]

# 岗位名里出现 → 非目标岗位 或 低质量岗位
BL_TITLE = [
    # 销售 / 商务（与目标岗位无关）
    "销售", "电销", "电话销售", "商务", "BD", "业务员", "客户经理",
    "渠道", "地推", "招商", "代理",
    # 中介 / 招聘岗
    "招聘专员", "招聘主管", "人事", "HR", "猎头顾问",
    # 算法岗（简历无训练/论文证据，role-fit 判定不建议）
    "算法工程师", "算法专家", "算法研究员", "机器学习", "深度学习",
    "模型训练", "AI训练师", "训练师",
    # 垃圾岗位信号（急招/急聘只挡与 AI/编程无关的场景 —— 急招 AI 岗保留）
    r"急招", r"急聘", r"日结", r"兼职", r"临时", r"小时工", r"普工", r"操作工",
    r"骑手", r"外卖", r"分拣", r"打包", r"客服", r"文员", r"打字", r"录入",
    r"主播", r"带货", r"直播", r"电商运营", r"数据标注", r"标注",
    # 急招的非技术信号（可与 AI 岗共现，不一刀切；规则层只排除完全无关的）
    # 保留：只排「急招」出现在非技术岗位名（由下层 _kw_hit 再判断上下文）
    # — 已在 title 层保留急招，只在非技术 JD/技能中再强化过滤。
    # 低质量/伪技术
    "学徒", "实习", "培训生", "储备干部",
]

# 大厂（简历竞争力不足，投了也多半没回复，省下投递额度）
BL_BIGTECH = [
    "腾讯", "字节跳动", "阿里", "蚂蚁", "百度", "美团", "京东", "网易",
    "华为", "小米", "快手", "拼多多", "滴滴", "商汤", "旷视", "大疆",
    "蔚来", "小鹏", "理想汽车", "哔哩哔哩", "携程", "去哪儿", "搜狐",
    "新浪", "微博", "360", "OPPO", "vivo", "中兴", "联想", "科大讯飞",
]

# 职位描述（当前数据源无完整 JD，退回按技能字段匹配）
BL_JD = [
    "外包", "驻场", "人力派遣", "劳务派遣",
]

PRESET = {
    "company": BL_COMPANY + BL_BIGTECH,
    "title": BL_TITLE,
    "jd": BL_JD,
}


def fetch_overview():
    try:
        from local_client import local_opener
        with local_opener(API).open(API + "/api/overview", timeout=30) as r:
            return json.load(r)["data"]
    except Exception:
        return None


def save_via_api(cfg: dict) -> bool:
    """通过 API 写入 —— 保证服务进程里的设置缓存同步刷新。

    直接改 settings.json 会让已运行的 app.py 用旧缓存（rules 里 load() 有缓存）。
    """
    try:
        body = json.dumps({"config": cfg}).encode()
        req = u.Request(API + "/api/settings", data=body,
                        headers={"Content-Type": "application/json"})
        from local_client import local_opener
        with local_opener(API).open(req, timeout=30) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print("  ⚠️ API 写入失败，回退直接写文件:", e)
        st.save(cfg)
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只看影响，不写入")
    ap.add_argument("--clear", action="store_true", help="清空黑名单")
    args = ap.parse_args()

    c = store.connect()
    jobs = [dict(r) for r in c.execute("SELECT * FROM jobs")]
    c.close()

    if args.clear:
        cfg = st.load(force=True)
        for k in ("company", "title", "jd"):
            cfg["blacklist"][k] = []
        save_via_api(cfg)
        print("✅ 黑名单已清空")
        return 0

    before = sum(1 for j in jobs if rules.job_ok(j)[0])
    print(f"=== 应用前：通过筛选 {before} / {len(jobs)} ===\n")

    if not args.dry:
        cfg = st.load(force=True)
        cfg["blacklist"] = {**PRESET}
        if save_via_api(cfg):
            print("✅ 已写入（走 API，服务进程缓存同步刷新）\n")

    # 逐条量化影响：单独看这个关键词挡掉多少
    print("=== 每条关键词的影响（单独计算，可能有重叠）===")
    for field, words in PRESET.items():
        print(f"\n【{field}】共 {len(words)} 条")
        impacts = []
        for w in words:
            if field == "company":
                n = sum(1 for j in jobs if w in (j.get("company") or ""))
            elif field == "title":
                n = sum(1 for j in jobs if w in (j.get("title") or ""))
            else:
                n = sum(1 for j in jobs
                        if w in ((j.get("jd") or j.get("skills") or "")))
            if n:
                impacts.append((n, w))
        impacts.sort(reverse=True)
        for n, w in impacts:
            print(f"    {w:<16} 命中 {n:>3} 条")
        zero = [w for w in words if not any(w == x[1] for x in impacts)]
        if zero:
            print(f"    （命中 0 条，可保留待用：{'、'.join(zero[:12])}）")

    # 应用后的结果
    st.load(force=True)   # 强制重读（API 写入后本进程缓存也要刷）
    after = sum(1 for j in jobs if rules.job_ok(j)[0])
    print(f"\n=== 应用后：通过筛选 {after} / {len(jobs)}（减少 {before - after}）===")

    # 净影响：只看「原本通过、现在被挡」的，这才是黑名单真正新增的过滤
    print("\n=== 净新增拦截（原本能通过、现在被挡掉的）===")
    # 用「清空黑名单」的基线来对比，避免被内置规则掩盖
    base_cfg = st.load(force=True)
    saved_bl = dict(base_cfg.get("blacklist") or {})
    base_cfg["blacklist"] = {"company": [], "title": [], "jd": []}
    st._cache = base_cfg          # 直接改缓存，只看内置规则
    builtin_pass = {j["job_id"] for j in jobs if rules.job_ok(j)[0]}
    st._cache = None              # 恢复，走真实配置
    now_pass = {j["job_id"] for j in jobs if rules.job_ok(j)[0]}
    newly = builtin_pass - now_pass
    print(f"    黑名单新增挡掉 {len(newly)} 条（内置规则挡掉 {len(jobs) - len(builtin_pass)} 条）")
    if newly:
        bl_reasons = Counter()
        for j in jobs:
            if j["job_id"] in newly:
                _, why = rules.job_ok(j)
                bl_reasons[why] += 1
        for k, v in bl_reasons.most_common(14):
            print(f"      {k:<20} {v}")

    reasons = Counter()
    for j in jobs:
        ok, why = rules.job_ok(j)
        if not ok:
            reasons[why.split()[0]] += 1
    print("\n被挡原因分布：")
    for k, v in reasons.most_common(12):
        print(f"    {k:<14} {v}")

    print(f"\n=== 剩余投递池前 10 条（这就是你实际会投的）===")
    pool = [j for j in jobs if rules.job_ok(j)[0]
            and rules.parse_salary_min(j.get("salary") or "")]
    pool.sort(key=lambda j: -(rules.parse_salary_min(j.get("salary") or "") or 0))
    for j in pool[:10]:
        print(f"    {j.get('title','')[:30]:<32} {j.get('company','')[:16]:<18} "
              f"{j.get('salary',''):<14} {j.get('city','')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
