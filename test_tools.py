#!/usr/bin/env python3
import unittest as _ut
if __name__ != "__main__":
    # 这是「跑起来才算数」的验算脚本：import 就会执行全部手算核对并 print。
    raise _ut.SkipTest("验算脚本，用 python 直接运行，不是 unittest 模块")

"""tools.py 验算 —— 用可手算核对的样例，确保税算得准。

关键判据不是"跑通了"，而是"手算数字对得上"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tools  # noqa: E402
from rules import parse_salary_min  # noqa: E402  # 薪资解析回归用

FAIL = []


def ck(name, got, want, tol=0.01):
    """断言。

    容差默认 0.01 —— 只用来吸收浮点误差。
    血教训：曾把默认容差设成 1.0，结果 got=0/want=1、got=1/want=0 这类
    布尔/计数不符被静默放过，两个真 bug 被测试脚手架藏住了。
    \"跑通\" 不等于 \"检出了\"。
    """
    ok = abs(got - want) <= tol
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got} · 期望 {want}")
    if not ok:
        FAIL.append(name)


print("=== 1. 工资个税（广州 / 公积金5% / 无专项附加）===")
# 手算：base=10000；养老800 医疗200 失业20=1020；公积金500；专项扣除1520/月
# 年应纳税所得额 = 120000-60000-18240 = 41760 → 10% 速扣2520 → 税 1656 → 月税 138
# 月到手 = 10000-1520-138 = 8342
r = tools.calc_salary(10000)
ck("五险一金/月", r["五险一金"]["合计"], 1520)
ck("年应纳税所得额", r["工资个税"]["年应纳税所得额"], 41760)
ck("全年工资税额", r["工资个税"]["全年税额"], 1656)
ck("月均到手", r["到手"]["月均到手"], 8342)

print("\n=== 2. 年终奖两种计税（月薪1万 + 年终奖3万）===")
# 单独：30000/12=2500 → 3% → 900
# 并入：应纳税所得额 120000+30000-60000-18240=71760 → 10%速扣2520 → 4656；年终奖部分=4656-1656=3000
# → 单独计税更省 2100
r = tools.calc_salary(10000, bonus=30000)
ck("单独计税税额", r["年终奖对比"]["单独计税"]["税额"], 900)
ck("并入计税税额", r["年终奖对比"]["并入综合所得"]["税额"], 3000)
ck("更省方式", 1 if r["年终奖对比"]["更省"] == "单独计税" else 0, 1)
ck("省下金额", r["年终奖对比"]["省下"], 2100)

print("\n=== 3. 年终奖临界点陷阱（验证计算灵敏度）===")
# 36000/12=3000 → 3% → 1080；  36001/12=3000.08 → 10%速扣210 → 3390.1
r1 = tools.calc_salary(10000, bonus=36000)["年终奖对比"]["单独计税"]["税额"]
r2 = tools.calc_salary(10000, bonus=36001)["年终奖对比"]["单独计税"]["税额"]
ck("36000 的税", r1, 1080)
ck("36001 的税（多1元）", r2, 3390.1)
print(f"     → 多发 1 元，多交 {r2 - r1:.1f} 元税（真实存在的临界点陷阱）")

print("\n=== 4. 五险一金基数上下限 ===")
r = tools.calc_salary(100000)      # 超上限，应按 26421 计
ck("高薪按上限基数", r["输入"]["社保基数"], 26421)
r = tools.calc_salary(3000)        # 低于下限，应按 4546 计
ck("低薪按下限基数", r["输入"]["社保基数"], 4546)

print("\n=== 5. 专项附加扣除减税 ===")
base = tools.calc_salary(15000)["工资个税"]["全年税额"]
cut = tools.calc_salary(15000, special_deduction=2000)["工资个税"]["全年税额"]
ck("2000/月专项附加节税", base - cut, 2000 * 12 * 0.10)   # 该档税率 10%

print("\n=== 6. Offer 对比 ===")
a = {"name": "A公司", "monthly": 12000, "months": 12, "commute_min": 50,
     "days_per_week": 5, "hours_per_day": 9, "housing_fund_rate": 0.05}
b = {"name": "B公司", "monthly": 14000, "months": 13, "commute_min": 15,
     "days_per_week": 5, "hours_per_day": 10, "housing_fund_rate": 0.08}
cmp = tools.compare_offers(a, b)
for row in cmp["对比"]:
    print(f"     {row['项']:<16} A={row['A公司']:<12} B={row['B公司']:<12} 更优={row['更优']}")
print(f"     结论：时薪更高 {cmp['结论']['时薪更高']}，差距 {cmp['结论']['时薪差距']} 元/小时")
ck("结论有时薪比较", 1 if cmp["结论"]["时薪更高"] in ("A公司", "B公司") else 0, 1)
ck("对比项覆盖6项", len(cmp["对比"]), 6)

print("\n=== 7. 简历隐患排查 ===")
bad = """个人简历
热爱学习，积极进取，有责任心，团队合作能力强。
2024.03-2025.06 某公司 后端开发
负责接口开发。"""

good = """张三 | 13800000000 | zhangsan@example.com
2024.03 - 2025.06  Python后端开发工程师
· 用 FastAPI 重构订单接口，P99 从 800ms 降到 120ms（提升 6.6 倍）
· 设计 Redis 缓存层，缓存命中率 92%，DB QPS 下降 60%
· 用 pytest 覆盖 200 条用例，线上事故率下降 80%"""

r_bad = tools.check_resume(bad, "简历副本final.docx")
r_good = tools.check_resume(good, "张三-Python后端-简历.pdf")
print(f"     差简历：高 {r_bad['统计']['高']} / 中 {r_bad['统计']['中']} / 低 {r_bad['统计']['低']}")
for i in r_bad["问题"][:5]:
    print(f"       [{i['级别']}] {i['类别']}: {i['问题'][:44]}")
print(f"     好简历：高 {r_good['统计']['高']} / 中 {r_good['统计']['中']} / 低 {r_good['统计']['低']}")
ck("差简历检出高优先级", r_bad["统计"]["高"] > 0, 1)
ck("好简历无高优先级", r_good["统计"]["高"], 0)
ck("差简历检出空泛词", 1 if any("空泛" in i["类别"] for i in r_bad["问题"]) else 0, 1)
ck("差简历检出文件名问题", 1 if any("文件名" in i["类别"] for i in r_bad["问题"]) else 0, 1)

print("\n=== 7b. 回归：应届简历不应误报为硬伤 ===")
# 实测教训：2025 届简历只有「2025 届 / 2025 年毕业」，没有经历起止时间，
# 原实现一律判「高优先级」，是误报。应届场景应降级为低。
fresh = """张三 | 13800000000 | zhangsan@example.com
> 2025 届软件工程本科，交付 2 个完整项目（175 条 pytest 通过）。
**某学院 · 软件工程 · 全日制本科 · 2025 年毕业**"""
r_fresh = tools.check_resume(fresh, "Resume.md")
ck("应届简历无高优先级", r_fresh["统计"]["高"], 0)
ck("应届时间线降为低", 1 if any(i["类别"] == "时间线" and i["级别"] == "低"
                                for i in r_fresh["问题"]) else 0, 1)
ck("源文件 .md 不报扩展名", 1 if not any(
    "扩展名" in i["问题"] for i in r_fresh["问题"]) else 0, 1)

# 非应届：没有经历日期就是硬伤，必须报高
senior = """李四 | 13900139000 | lisi@example.com
**某大学 · 计算机 · 本科**
在某公司做后端开发，负责接口。"""
r_senior = tools.check_resume(senior, "李四-简历.pdf")
ck("非应届无日期报高", 1 if any(i["类别"] == "时间线" and i["级别"] == "高"
                                for i in r_senior["问题"]) else 0, 1)

print("\n=== 8. JD 关键词提取 ===")
jd = """岗位职责：
1. 负责大模型应用开发，熟悉 LangChain / LangGraph 框架
2. 精通 Python，掌握 FastAPI，了解 Docker 与 Kubernetes 部署
3. 有 RAG 检索增强经验者优先
4. 使用过 Milvus 等向量数据库加分
任职要求：
1. 本科及以上学历，2-3年相关经验
2. 具备良好的团队合作精神
3. 大小周，能接受加班"""
e = tools.extract_jd(jd)
print(f"     技术栈（{e['技术栈数量']}）：{'、'.join(e['技术栈'])}")
print(f"     经验/学历：{e['基本要求']['经验']} / {e['基本要求']['学历']}")
print(f"     硬性要求 {len(e['硬性要求'])} 条 · 加分项 {len(e['加分项'])} 条")
for t in e["提示"]:
    print(f"     ⚠ {t}")
ck("提取到技术栈", e["技术栈数量"] > 5, 1)
ck("提取到经验", e["基本要求"]["经验"] != "未写明", 1)
ck("提取到本科", e["基本要求"]["学历"] == "本科", 1)
ck("识别加分项", len(e["加分项"]) >= 1, 1)
ck("识别加班提示", 1 if any("加班" in t for t in e["提示"]) else 0, 1)

print("\n=== 10. Offer 条款检查（规则 vs 劳动合同法）===")
# 违规样例：押金 + 试用期超上限 + 试用期工资 70%
bad_offer = """劳动合同
合同期限：3 年。试用期 6 个月，试用期工资为转正工资的 70%。
入职需缴纳培训费 2000 元，工牌押金 200 元。
如提前离职需支付违约金 5000 元。
竞业限制期限 3 年，未约定补偿金。"""
r = tools.check_offer(bad_offer, contract_months=36)
print(f"     结论：{r['结论']}")
for h in r["命中"]:
    print(f"       [{h['级别']}] {h['条款']}: {h['问题'][:46]}")
ck("检出押金违规", 1 if any("押金" in h["条款"] and h["级别"] == "违规" for h in r["命中"]) else 0, 1)
ck("检出试用期工资违规", 1 if any("试用期工资" in h["条款"] and h["级别"] == "违规" for h in r["命中"]) else 0, 1)
ck("检出竞业期超限", 1 if any("竞业" in h["条款"] and h["级别"] == "违规" for h in r["命中"]) else 0, 1)
ck("检出违约金注意", 1 if any("违约金" in h["条款"] for h in r["命中"]) else 0, 1)

# 试用期上限：3 年合同上限 6 个月，写 8 个月应判违规
r2 = tools.check_offer("合同期限 3 年，试用期 8 个月。", contract_months=36)
ck("试用期超 6 个月判违规", 1 if any("超过上限" in h["问题"] for h in r2["命中"]) else 0, 1)

# 1 年合同试用期 3 个月 → 上限 2 个月，应判违规
r3 = tools.check_offer("合同期限 1 年，试用期 3 个月。", contract_months=12)
ck("1年合同试用期3个月判违规", 1 if any("超过上限 2 个月" in h["问题"] for h in r3["命中"]) else 0, 1)

# 合格样例：不应报违规
good_offer = """劳动合同期限 3 年，试用期 3 个月，试用期工资为转正工资的 100%。
月薪 15000 元，每月 15 日发放。
工作地点：广州市天河区。入职即缴纳五险一金（含试用期）。"""
r4 = tools.check_offer(good_offer, contract_months=36)
ck("合格 Offer 无违规", r4["风险数"], 0)
print(f"     合格样例结论：{r4['结论']}（通过 {len(r4['通过'])} 项）")

print("\n=== 10b. 试用期上限边界（法定临界点）===")
# 劳动合同法第19条的分档临界：满 1 年进下一档、满 3 年再进档
ck("不满3个月→不得约定(None)", 1 if tools.probation_limit(2) is None else 0, 1)
ck("3个月→上限1个月", tools.probation_limit(3), 1)
ck("11个月→上限1个月", tools.probation_limit(11), 1)
ck("满12个月(1年)→上限2个月", tools.probation_limit(12), 2)
ck("35个月→上限2个月", tools.probation_limit(35), 2)
ck("满36个月(3年)→上限6个月", tools.probation_limit(36), 6)
ck("无固定期限→上限6个月", tools.probation_limit(120), 6)

ck("15薪不被误当工资(25K·15薪)", parse_salary_min("25-50K·15薪"), 25000)
ck("13薪不被误当工资(20-40K·13薪)", parse_salary_min("20-40K·13薪"), 20000)

print("\n=== 11. 清单与话术模板 ===")
cl = tools.get_checklist()
ck("清单分组数", len(cl), 5)
ck("清单总项数", sum(len(v) for v in cl.values()), 30)
pb = tools.get_playbook()
ck("话术场景数", len(pb["话术"]), 6)
ck("辞职信模板数", len(pb["辞职信"]), 3)
ck("每条话术都有别做提醒", 1 if all(p.get("别做") for p in pb["话术"]) else 0, 1)

print("\n=== 9. 边界：零输入不崩 ===")
try:
    tools.calc_salary(0)
    tools.compare_offers({}, {})
    tools.check_resume("")
    tools.extract_jd("")
    print("  ✅ 空/零输入未抛异常")
except Exception as ex:
    print(f"  ❌ 空输入崩溃：{ex!r}")
    FAIL.append("边界输入")

print("\n" + "=" * 52)
print(f"❌ {len(FAIL)} 项失败：{'、'.join(FAIL)}" if FAIL else "✅ 全部通过（税算与手算一致）")
sys.exit(1 if FAIL else 0)
