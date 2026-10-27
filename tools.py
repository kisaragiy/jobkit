#!/usr/bin/env python3
"""jobkit 本地工具集 —— 纯计算 / 规则判断。

设计约束（2026-09-19 风控事故后确立）：
  · **零平台调用**：这里一个网络请求都不发
  · **零 token**：不做 AI 调用，纯本地规则与算术
  · 所有口径参数可覆盖，默认值标注来源与年份 —— 政策会变，别把默认值当真理

包含：
  calc_salary      税后到手（含年终奖两种计税对比）
  compare_offers   两份 Offer 的性价比对比
  check_resume     简历隐患排查
  extract_jd       JD 关键词 / 要求提取
"""
from __future__ import annotations

import re
from collections import Counter

# ══════════════════════════════════════════════════════════
# 一、薪资计算
# ══════════════════════════════════════════════════════════

# 综合所得年度税率表（累计预扣预缴，全年换算）
# (上限, 税率, 速算扣除数)
TAX_YEAR = [
    (36_000,    0.03, 0),
    (144_000,   0.10, 2_520),
    (300_000,   0.20, 16_920),
    (420_000,   0.25, 31_920),
    (660_000,   0.30, 52_920),
    (960_000,   0.35, 85_920),
    (float("inf"), 0.45, 181_920),
]

# 年终奖单独计税用的「按月换算」税率表
TAX_BONUS_MONTH = [
    (3_000,     0.03, 0),
    (12_000,    0.10, 210),
    (25_000,    0.20, 1_410),
    (35_000,    0.25, 2_660),
    (55_000,    0.30, 4_410),
    (80_000,    0.35, 7_160),
    (float("inf"), 0.45, 15_160),
]

# 五险一金个人缴纳比例（广州口径，2024 版，可覆盖）
SOCIAL_RATES = {
    "养老保险": 0.08,
    "医疗保险": 0.02,
    "失业保险": 0.002,
    # 工伤、生育：个人不缴
}
# 缴费基数上下限（广州 2024，元/月）—— 政策每年调整，务必按需覆盖
SOCIAL_BASE_LIMITS = {"下限": 4_546, "上限": 26_421}

THRESHOLD_MONTHLY = 5_000     # 个税起征点
THRESHOLD_YEARLY = 60_000


def _rate_of(taxable: float, table) -> tuple[float, float]:
    """按金额查税率表，返回 (税率, 速算扣除数)。

    拆出这一步是因为年终奖单独计税的算法特殊：
    **用「年终奖÷12」查税率档位，但税按年终奖全额计算** ——
    两者混在一起算会低估税负（实测把 30000 年终奖的税算成 75 元，正确是 900 元）。
    """
    taxable = max(0.0, taxable)
    for cap, rate, deduct in table:
        if taxable <= cap:
            return rate, deduct
    return table[-1][1], table[-1][2]


def _tax_of(taxable: float, table) -> tuple[float, float, float]:
    """返回 (税额, 税率, 速算扣除数)。"""
    taxable = max(0.0, taxable)
    rate, deduct = _rate_of(taxable, table)
    return max(0.0, taxable * rate - deduct), rate, deduct


def calc_salary(monthly_gross: float,
                months: int = 12,
                bonus: float = 0.0,
                housing_fund_rate: float = 0.05,
                special_deduction: float = 0.0,
                social_base: float | None = None,
                city_limits: dict | None = None) -> dict:
    """算税后到手，并给出年终奖两种计税方式的对比。

    参数
      monthly_gross      月薪（税前）
      months             发薪月数
      bonus              年终奖金额
      housing_fund_rate  公积金个人比例（0.05 ~ 0.12）
      special_deduction  每月专项附加扣除合计（子女教育/房贷/赡养老人等）
      social_base        社保缴费基数；None = 按实际月薪，并受上下限约束
      city_limits        覆盖默认基数上下限，如 {"下限":4546,"上限":26421}

    口径说明：按全年累计预扣预缴换算（固定月薪下与逐月累计结果一致）。
    """
    limits = city_limits or SOCIAL_BASE_LIMITS
    base = monthly_gross if social_base is None else social_base
    base = min(max(base, limits["下限"]), limits["上限"])

    social = {name: round(base * rate, 2) for name, rate in SOCIAL_RATES.items()}
    social_total = round(sum(social.values()), 2)
    fund = round(base * housing_fund_rate, 2)
    deduct_monthly = round(social_total + fund, 2)

    salary_year = monthly_gross * months

    # ── 工资部分 ──
    taxable_salary = max(0.0, salary_year - THRESHOLD_YEARLY
                         - deduct_monthly * months
                         - special_deduction * months)
    tax_salary, rate_s, deduct_s = _tax_of(taxable_salary, TAX_YEAR)

    # ── 年终奖：方式 A 单独计税 ──
    # 注意：bonus/12 只用于查税率档位，税额按 bonus 全额算
    if bonus > 0:
        rate_b, deduct_b = _rate_of(bonus / 12, TAX_BONUS_MONTH)
        tax_bonus_sep = max(0.0, bonus * rate_b - deduct_b)
    else:
        tax_bonus_sep, rate_b, deduct_b = 0.0, 0.0, 0.0

    # ── 年终奖：方式 B 并入综合所得 ──
    taxable_all = max(0.0, salary_year + bonus - THRESHOLD_YEARLY
                      - deduct_monthly * months - special_deduction * months)
    tax_all, rate_all, _ = _tax_of(taxable_all, TAX_YEAR)
    tax_bonus_combined = max(0.0, tax_all - tax_salary)

    total_tax_sep = round(tax_salary + tax_bonus_sep, 2)
    total_tax_cmb = round(tax_salary + tax_bonus_combined, 2)
    better = "单独计税" if total_tax_sep <= total_tax_cmb else "并入综合所得"
    diff = round(abs(total_tax_sep - total_tax_cmb), 2)

    net_year = round(salary_year + bonus - deduct_monthly * months
                     - total_tax_sep - special_deduction * 0, 2)
    net_month = round((salary_year - deduct_monthly * months - tax_salary) / months, 2)

    return {
        "输入": {
            "月薪": monthly_gross, "发薪月数": months, "年终奖": bonus,
            "公积金比例": housing_fund_rate, "专项附加扣除/月": special_deduction,
            "社保基数": round(base, 2),
        },
        "五险一金": {**social, "公积金": fund, "合计": deduct_monthly},
        "工资个税": {
            "年应纳税所得额": round(taxable_salary, 2),
            "税率": rate_s, "速算扣除": deduct_s,
            "全年税额": round(tax_salary, 2),
        },
        "年终奖对比": {
            "单独计税": {"税额": round(tax_bonus_sep, 2), "税率": rate_b},
            "并入综合所得": {"税额": round(tax_bonus_combined, 2)},
            "更省": better, "省下": diff,
        },
        "到手": {
            "月均到手": net_month,
            "全年到手（含年终奖，按更省方式）": round(
                salary_year + bonus - deduct_monthly * months
                - min(total_tax_sep, total_tax_cmb), 2),
            "全年税前总包": round(salary_year + bonus, 2),
        },
    }


def compare_offers(a: dict, b: dict) -> dict:
    """两份 Offer 性价比对比。

    每份 Offer 字段：
      name 名称 / monthly 月薪 / months 发薪月数 / bonus 年终奖
      commute_min 单程通勤分钟 / days_per_week 每周工作天数
      hours_per_day 每天工时 / housing_fund_rate 公积金比例
      special_deduction 专项附加扣除
    """
    def prep(o: dict) -> dict:
        s = calc_salary(
            monthly_gross=float(o.get("monthly") or 0),
            months=int(o.get("months") or 12),
            bonus=float(o.get("bonus") or 0),
            housing_fund_rate=float(o.get("housing_fund_rate") or 0.05),
            special_deduction=float(o.get("special_deduction") or 0),
        )
        net_year = s["到手"]["全年到手（含年终奖，按更省方式）"]
        days = float(o.get("days_per_week") or 5)
        hours = float(o.get("hours_per_day") or 8)
        commute = float(o.get("commute_min") or 0)
        # 年度投入工时 = 工作日 × 每天工时 + 通勤时间
        work_days = days * 52 - 11          # 扣法定节假日约 11 天
        total_hours = work_days * (hours + commute * 2 / 60)
        return {
            "name": o.get("name") or "Offer",
            "年包(税前)": s["到手"]["全年税前总包"],
            "全年到手": net_year,
            "月均到手": s["到手"]["月均到手"],
            "公积金/月(个人)": s["五险一金"]["公积金"],
            "年投入工时": round(total_hours, 1),
            "时薪(到手/工时)": round(net_year / total_hours, 2) if total_hours else 0,
            "每周工作天数": days, "每天工时": hours, "单程通勤(分)": commute,
            "_detail": s,
        }

    pa, pb = prep(a), prep(b)
    keys = ["年包(税前)", "全年到手", "月均到手", "公积金/月(个人)",
            "年投入工时", "时薪(到手/工时)"]
    rows = []
    for k in keys:
        va, vb = pa[k], pb[k]
        # 工时是越少越好，其余越多越好
        lower_better = k == "年投入工时"
        if va == vb:
            win = "打平"
        elif (va < vb) == lower_better:
            win = pa["name"]
        else:
            win = pb["name"]
        rows.append({"项": k, pa["name"]: va, pb["name"]: vb, "更优": win})

    gain_a = pa["时薪(到手/工时)"]
    gain_b = pb["时薪(到手/工时)"]
    return {
        "对比": rows,
        "结论": {
            "时薪更高": pa["name"] if gain_a >= gain_b else pb["name"],
            "时薪差距": round(abs(gain_a - gain_b), 2),
            "全年到手差距": round(abs(pa["全年到手"] - pb["全年到手"]), 2),
            "口径": "时薪 = 全年到手 ÷ 年投入工时（含双程通勤）；工时按 52 周 - 11 天法定假",
        },
        "明细": {pa["name"]: pa["_detail"], pb["name"]: pb["_detail"]},
    }


# ══════════════════════════════════════════════════════════
# 二、简历隐患排查
# ══════════════════════════════════════════════════════════

# 空泛表述（HR 共识：这些词等于没写）
VAGUE_WORDS = [
    "热爱学习", "积极进取", "有责任心", "团队合作", "善于沟通", "吃苦耐劳",
    "性格开朗", "勤恳踏实", "抗压能力强", "有良好的", "较强的", "熟练掌握",
    "深入了解", "精通各种", "熟悉各种",
]
PHONE_RE = re.compile(r"1[3-9]\d{9}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# 日期：2023.01 / 2023-01 / 2023年1月 / 2023/01
DATE_RE = re.compile(r"(20\d{2})\s*[.\-/年]\s*(\d{1,2})")
NUM_RE = re.compile(r"\d+\s*(%|万|千|个|次|倍|ms|s|人|条|QPS|w)")


def check_resume(text: str, filename: str = "") -> dict:
    """简历隐患排查：联系方式 / 排版ATS / 空泛表述 / 日期 / 量化。"""
    issues: list[dict] = []

    def add(level: str, cat: str, msg: str, hint: str = "") -> None:
        issues.append({"级别": level, "类别": cat, "问题": msg, "建议": hint})

    # 1) 联系方式
    if not PHONE_RE.search(text):
        add("高", "联系方式", "没找到 11 位手机号", "HR 约面第一件事就是打电话，必须写")
    if not EMAIL_RE.search(text):
        add("高", "联系方式", "没找到邮箱", "补一个常用邮箱")
    else:
        m = EMAIL_RE.search(text)
        if m and m.group(0).split("@")[-1].lower() in ("qq.com", "163.com", "126.com"):
            add("低", "联系方式", f"邮箱是 {m.group(0).split('@')[-1]}",
                "能用，但用 Gmail/Outlook 更显专业（非硬伤）")

    # 2) 空泛表述
    hits = [w for w in VAGUE_WORDS if w in text]
    if hits:
        add("中", "空泛表述", f"命中 {len(hits)} 个空话词：{'、'.join(hits[:8])}",
            "换成可验证的事实：具体做了什么、用了什么技术、拿到什么结果")

    # 3) 量化
    nums = NUM_RE.findall(text)
    if len(nums) < 3:
        add("中", "量化", f"全篇只有 {len(nums)} 处数字化成果",
            "成果尽量带数字（性能提升 X%、覆盖 Y 条用例、支撑 Z QPS）")

    # 4) 日期
    dates = DATE_RE.findall(text)
    # 应届生简历只有毕业年份、没有经历起止时间属正常情况，
    # 不能一律判成高优先级硬伤（实测在 2025 届简历上误报过）
    is_fresh = bool(re.search(r"(应届|20\d{2}\s*届|年毕业|在校)", text))
    if not dates:
        if is_fresh:
            add("低", "时间线", "只有毕业年份，没有经历起止年月",
                "应届简历属正常；若有实习 / 项目，补上起止年月更有说服力")
        else:
            add("高", "时间线", "没找到经历起止时间",
                "每段经历都要有起止年月（如 2024.03 - 2025.06）")
    else:
        years = sorted({int(y) for y, _ in dates})
        if len(dates) < 3:
            add("中", "时间线", f"只解析到 {len(dates)} 个日期",
                "经历时间建议统一格式（如 2024.03 - 2025.06）")
        if years and min(years) < 2000:
            add("低", "时间线", f"出现异常年份 {min(years)}", "检查是否笔误")

    # 5) 排版 / ATS
    if "|" in text and text.count("|") > 12:
        add("低", "排版", "大量表格竖线", "部分 ATS 解析表格会错行，经历段建议纯文本")
    if "　" in text:
        add("低", "排版", "含全角空格", "ATS 可能识别为乱码，换成半角空格")
    weird = set(re.findall(r"[•▪◆●■★☆▶]", text))
    if weird:
        add("低", "排版", f"特殊项目符号 {''.join(sorted(weird))}",
            "部分系统解析不了，改用 - 或数字编号")

    # 6) 文件名
    # .md/.txt 是源文件格式，不是投出去的成品，不按投递标准要求
    is_source = bool(re.search(r"\.(md|markdown|txt)$", filename, re.I))
    if filename and not is_source:
        if not re.search(r"[\u4e00-\u9fa5a-zA-Z]", filename):
            add("中", "文件名", "文件名看不出是谁", "建议「姓名-岗位-简历.pdf」")
        if re.search(r"(副本|copy|final|最终|new|新建)", filename, re.I):
            add("中", "文件名", f"文件名含临时词：{filename}",
                "招聘方看得到文件名，去掉「副本/final/最终」")
        if not re.search(r"\.(pdf|docx?)$", filename, re.I):
            add("低", "文件名", "扩展名不是 PDF/DOCX", "优先投 PDF，排版不乱")

    # 7) 长度
    n = len(re.findall(r"[\u4e00-\u9fa5]", text))
    if n < 400:
        add("中", "篇幅", f"正文约 {n} 个中文字", "偏短，可能信息量不足")
    elif n > 3000:
        add("中", "篇幅", f"正文约 {n} 个中文字", "偏长，建议压到 2 页内")

    high = sum(1 for i in issues if i["级别"] == "高")
    return {
        "问题": issues,
        "统计": {
            "高": high,
            "中": sum(1 for i in issues if i["级别"] == "中"),
            "低": sum(1 for i in issues if i["级别"] == "低"),
            "中文字数": n,
        },
        "结论": "有硬伤，先修高优先级" if high else
                ("基本可用，建议按中优先级优化" if issues else "没发现问题"),
    }


# ══════════════════════════════════════════════════════════
# 三、JD 关键词 / 要求提取
# ══════════════════════════════════════════════════════════

# 硬性要求引导词（后面跟的通常是必须项）
REQUIRE_LEAD = ["必须", "要求", "至少", "需具备", "熟悉", "精通", "掌握", "了解", "具备"]
# 加分项引导词
BONUS_LEAD = ["加分", "优先", "有.*经验者优先", "最好", "更佳", "有.*者优先"]

TECH_VOCAB = [
    # 语言
    "Python", "Java", "Go", "Golang", "C++", "C#", "JavaScript", "TypeScript", "Rust", "PHP", "SQL",
    # 后端
    "FastAPI", "Django", "Flask", "Spring", "SpringBoot", "Spring Boot", "MyBatis", "Laravel",
    "Node.js", "Express", "gRPC", "REST", "GraphQL", "微服务", "高并发", "分布式",
    # 数据 / 存储
    "MySQL", "PostgreSQL", "Redis", "MongoDB", "Elasticsearch", "Kafka", "RabbitMQ", "ClickHouse",
    # AI / LLM
    "LLM", "大模型", "RAG", "Agent", "LangChain", "LangGraph", "MCP", "Prompt", "提示词",
    "Fine-tune", "微调", "LoRA", "QLoRA", "Transformer", "PyTorch", "TensorFlow", "Diffusion",
    "Stable Diffusion", "ComfyUI", "OpenAI", "GPT", "Claude", "Qwen", "DeepSeek", "Ollama",
    "向量数据库", "Milvus", "Faiss", "Embedding", "多模态", "VLM", "Whisper", "TTS",
    # 工程 / 运维
    "Docker", "Kubernetes", "K8s", "CI/CD", "Jenkins", "Git", "Linux", "Nginx", "AWS", "阿里云",
    "Celery", "asyncio", "多线程", "多进程", "性能优化", "压测", "监控",
    # 前端
    "React", "Vue", "Next.js", "Webpack", "Vite", "HTML", "CSS", "Tailwind",
    # 其他
    "爬虫", "Scrapy", "Playwright", "Selenium", "Pandas", "NumPy", "数据挖掘", "机器学习",
]


def extract_jd(text: str) -> dict:
    """从 JD 提取：技术栈 / 硬性要求 / 加分项 / 经验学历 / 可疑信号。"""
    if not text:
        return {"技术栈": [], "硬性要求": [], "加分项": [], "基本要求": {}, "提示": []}

    # 1) 技术栈（词库匹配，保序去重）
    found: list[str] = []
    low = text.lower()
    for w in TECH_VOCAB:
        if w.lower() in low and w not in found:
            found.append(w)

    # 2) 按行拆分，挑带引导词的句子
    lines = [l.strip() for l in re.split(r"[\n。；;]", text) if l.strip()]
    reqs, bonus = [], []
    for l in lines:
        for bl in BONUS_LEAD:
            if re.search(bl, l):
                bonus.append(l[:100])
                break
        else:
            for rl in REQUIRE_LEAD:
                if rl in l:
                    reqs.append(l[:100])
                    break

    # 3) 经验 / 学历
    exp = re.search(r"(\d+\s*[-~至]\s*\d+\s*年|\d+\s*年以上|应届|经验不限)", text)
    edu = re.search(r"(博士|硕士|本科|大专|学历不限)", text)

    # 4) 可疑信号
    tips = []
    if re.search(r"(包吃住|月入过万|无经验可做|轻松|日结|高提成)", text):
        tips.append("出现「包吃住/月入过万/无经验可做/日结」等话术，警惕非技术岗")
    if re.search(r"(外包|驻场|派遣|人力)", text):
        tips.append("出现外包/驻场/派遣字样，确认合同主体")
    if text.count("加班") or re.search(r"(996|大小周|单休)", text):
        tips.append("提及加班/996/大小周，面试时确认作息")
    if len(text) < 120:
        tips.append("JD 过短，信息量少，建议先向 HR 要完整版")
    if not exp and not edu:
        tips.append("没写明经验与学历要求，可能筛选不严或信息不全")

    return {
        "技术栈": found,
        "技术栈数量": len(found),
        "硬性要求": reqs[:15],
        "加分项": bonus[:8],
        "基本要求": {
            "经验": exp.group(0) if exp else "未写明",
            "学历": edu.group(0) if edu else "未写明",
            "字数": len(text),
        },
        "提示": tips,
    }


# ══════════════════════════════════════════════════════════
# 四、Offer 条款检查（规则即时检查，零 token）
# ══════════════════════════════════════════════════════════

# 法律依据均出自《劳动合同法》。规则只做提示，不构成法律意见。
OFFER_RULES = [
    {
        "id": "probation-length",
        "名称": "试用期时长",
        "依据": "劳动合同法第19条",
        "检查": lambda t, o: _check_probation(t, o),
    },
    {
        "id": "probation-pay",
        "名称": "试用期工资",
        "依据": "劳动合同法第20条：不得低于本单位相同岗位最低档工资或约定工资的80%，且不低于当地最低工资",
        "检查": lambda t, o: _check_probation_pay(t, o),
    },
    {
        "id": "deposit",
        "名称": "押金 / 收费 / 扣证件",
        "依据": "劳动合同法第9条：禁止收取押金、扣押证件",
        "检查": lambda t, o: (
            "违规", "出现押金 / 培训费 / 扣押证件等字样 —— 法律明确禁止",
            "已付的先要回，签约前要求删除该条"
        ) if re.search(r"(押金|保证金|培训费|扣押|抵押|服装费|工牌费)", t) else None,
    },
    {
        "id": "penalty",
        "名称": "违约金条款",
        "依据": "劳动合同法第25条：除服务期与竞业限制外，不得约定由劳动者承担违约金",
        "检查": lambda t, o: (
            "注意", "约定了违约金 —— 只有「服务期（专项培训）」和「竞业限制」两种情形才合法",
            "问清属于哪种；不属于这两种的条款无效，可要求删除"
        ) if re.search(r"违约金", t) else None,
    },
    {
        "id": "noncompete",
        "名称": "竞业限制",
        "依据": "劳动合同法第23、24条：竞业限制期限不得超过2年，且须按月支付补偿",
        "检查": lambda t, o: _check_noncompete(t),
    },
    {
        "id": "social",
        "名称": "社保公积金",
        "依据": "社会保险法：用人单位应自用工之日起30日内办理社保登记",
        "检查": lambda t, o: (
            "注意", "没提到社保 / 五险一金",
            "入职即应缴纳（含试用期）；「转正后补缴」不合规，务必书面确认起缴时间"
        ) if not re.search(r"(五险一金|社保|社会保险|公积金)", t) else None,
    },
    {
        "id": "salary",
        "名称": "薪资约定",
        "依据": "劳动合同法第17条：劳动报酬是劳动合同必备条款",
        "检查": lambda t, o: (
            "注意", "没写明具体薪资数额或发放日",
            "要求书面写明月薪构成与发放日；口头承诺难维权"
        ) if not re.search(r"(\d+\s*元|\d+K|\d+k|月薪|发放日|发薪)", t) else None,
    },
    {
        "id": "location",
        "名称": "工作地点",
        "依据": "劳动合同法第17条：工作地点为必备条款",
        "检查": lambda t, o: (
            "注意", "没写明工作地点",
            "写清具体办公地点；「公司可调整」这类弹性表述要问清范围"
        ) if not re.search(r"(工作地点|办公地点|地址|base|Base|BASE)", t) else None,
    },
    {
        "id": "furlough",
        "名称": "待岗 / 降薪条款",
        "依据": "劳动合同法第35条：变更劳动合同需双方书面协商一致",
        "检查": lambda t, o: (
            "注意", "出现待岗 / 调岗降薪 / 公司可单方调整等表述",
            "单方调薪调岗不合法，需协商一致并书面确认"
        ) if re.search(r"(待岗|降薪|单方|有权调整|无条件服从)", t) else None,
    },
]

# 试用期法定上限见 probation_limit()（边界见该函数注释）


def _parse_months(text: str) -> float | None:
    m = re.search(r"(\d+)\s*年", text)
    if m:
        return float(m.group(1)) * 12
    m = re.search(r"(\d+)\s*个月", text)
    if m:
        return float(m.group(1))
    return None


def probation_limit(contract_months: float | None) -> float | None:
    """试用期法定上限（月）。None = 依法不得约定试用期。

    劳动合同法第19条：
      3 个月以上不满 1 年 → ≤1 个月
      1 年以上不满 3 年   → ≤2 个月
      3 年以上 / 无固定期限 → ≤6 个月
    ⚠️ 边界必须用「满 1 年就进入下一档」，用 <= 比较会把刚好 1 年的合同
       错判成「不满 1 年」档，导致合法条款被误报违规（实测踩过）。
    """
    if contract_months is None:
        return None
    if contract_months < 3:
        return None          # 不满 3 个月：不得约定试用期
    if contract_months < 12:
        return 1
    if contract_months < 36:
        return 2
    return 6


def _check_probation(t: str, o: dict):
    if not re.search(r"试用期", t):
        return None
    # 只在「试用期」之后的片段里找时长。
    # 实测教训：「合同期限 1 年，试用期 3 个月」若整句解析，
    # 正则先命中「1 年」把合同期限当成试用期 —— 合法条款被判违规。
    seg = _sentence_with(t, "试用期") or ""
    after = seg.split("试用期", 1)[1] if "试用期" in seg else ""
    pm = _parse_months(after)
    if pm is None:
        return ("提示", "提到试用期但没写明时长", "要求写清试用期起止与时长")
    cm = o.get("contract_months")
    if cm is None:
        return ("提示", f"试用期 {pm:g} 个月（未提供合同期限，无法核对上限）",
                "合同 1~3 年时试用期不得超过 2 个月；3 年以上不超过 6 个月")
    limit = probation_limit(cm)
    if limit is None:
        return ("违规", f"合同 {cm:g} 个月（不满 3 个月），依法不得约定试用期",
                "法律依据：劳动合同法第19条")
    if pm > limit:
        return ("违规", f"试用期 {pm:g} 个月超过上限 {limit} 个月（合同 {cm:g} 个月）",
                "要求改为合法时长；超期部分应按转正工资支付")
    return None


def _first_money(seg: str) -> float | None:
    """从一段话里取第一个金额（元）。支持 3000 / 3,000 / 3.5 千 / 1.2 万。"""
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([千万])?\s*元", seg)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    unit = m.group(2)
    if unit == "千":
        v *= 1000
    elif unit == "万":
        v *= 10000
    return v


def _check_probation_pay(t: str, o: dict):
    if not re.search(r"试用期", t):
        return None
    if not re.search(r"(试用期.{0,20}(工资|薪资|薪))", t):
        return ("注意", "没写试用期工资标准",
                "试用期工资不得低于转正工资的 80%，且不低于当地最低工资")
    # ① 直接写了百分比
    m = re.search(r"试用期.{0,24}?(\d+)\s*%", t)
    if m:
        if float(m.group(1)) < 80:
            return ("违规", f"试用期工资为 {m.group(1)}%，低于法定 80% 下限",
                    "劳动合同法第20条：不得低于本单位相同岗位最低档工资或约定工资的 80%")
        return None
    # ② 写的是绝对金额 —— 试用期 X 元 vs 转正 Y 元，自己算比例
    seg_try = re.search(r"试用期[^。；;\n]{0,60}", t)
    seg_full = re.search(r"(转正|正式|期满后|试用期满)[^。；;\n]{0,60}", t)
    if seg_try and seg_full:
        a = _first_money(seg_try.group(0))
        b = _first_money(seg_full.group(0))
        if a and b and b > 0 and a / b < 0.8:
            return ("违规",
                    f"试用期 {a:g} 元，低于转正 {b:g} 元的 80%（应不低于 {b * 0.8:g} 元）",
                    "劳动合同法第20条：试用期工资不得低于约定工资的 80%，且不低于当地最低工资")
    return None


def _check_noncompete(t: str):
    if not re.search(r"竞业", t):
        return None
    months = _parse_months(_sentence_with(t, "竞业") or "")
    if months and months > 24:
        return ("违规", f"竞业限制 {months:g} 个月，超过法定上限 24 个月", "劳动合同法第24条")
    if not re.search(r"(补偿|补偿金|按月支付)", t):
        return ("注意", "有竞业限制但没提补偿金",
                "竞业限制必须按月支付补偿，否则条款可能不被支持；签前确认金额与期限")
    return None


def _sentence_with(text: str, kw: str) -> str | None:
    for s in re.split(r"[。；;\n]", text):
        if kw in s:
            return s
    return None


def check_offer(text: str, contract_months: float | None = None) -> dict:
    """按劳动合同法做 Offer 条款即时检查（规则匹配，零 token）。"""
    if not text.strip():
        return {"结论": "无内容", "命中": [], "通过": [], "风险数": 0}

    hits, passed = [], []
    for rule in OFFER_RULES:
        r = rule["检查"](text, {"contract_months": contract_months})
        if r:
            level, msg, hint = r
            hits.append({"级别": level, "条款": rule["名称"],
                         "问题": msg, "建议": hint, "依据": rule["依据"]})
        else:
            passed.append(rule["名称"])

    order = {"违规": 0, "注意": 1, "提示": 2}
    hits.sort(key=lambda x: order.get(x["级别"], 9))
    n_bad = sum(1 for h in hits if h["级别"] == "违规")
    n_warn = sum(1 for h in hits if h["级别"] == "注意")
    return {
        "命中": hits,
        "通过": passed,
        "风险数": n_bad,
        "提示数": n_warn,
        "结论": (f"发现 {n_bad} 处可能违规条款，签约前务必核实" if n_bad
                 else (f"{n_warn} 处需注意，建议问清" if n_warn else "规则检查未发现明显问题")),
        "免责": "以上为规则匹配结果，仅供参考，不构成法律意见。",
    }


# ══════════════════════════════════════════════════════════
# 五、话术模板库 / 辞职信（静态模板，零 token）
# ══════════════════════════════════════════════════════════

PLAYBOOK = [
    {
        "场景": "问期望薪资",
        "何时用": "HR 在电话里第一次问薪资时",
        "原则": "先反问范围，再给区间；区间下限 = 你的心理底线，上限 = 目标薪资 + 15%",
        "话术": [
            "在聊薪资之前，想先了解下这个岗位的薪资区间和结构，方便说的话我再看怎么对齐。",
            "我目前拿到/期望的范围是 {下限}-{上限}K。具体我会结合岗位职责、发展空间和整体包来看。",
            "想确认下薪资结构：月薪是几薪？年终奖怎么算？公积金比例是多少？",
        ],
        "别做": "别先报具体数字（容易被压到下限）；别说「都可以」「看公司」（显得没底）",
    },
    {
        "场景": "面试被追问不会的技术",
        "何时用": "面试官问到你没实际用过的技术",
        "原则": "承认边界 + 给出相邻经验 + 说明学习路径；绝不硬编造",
        "话术": [
            "{技术} 我还没有在生产环境用过。不过我在 {相邻技术} 上有类似经验：{具体例子}，底层思路是相通的。",
            "这块我了解原理但没落地过，如果岗位需要，我可以说明我的学习计划和时间预估。",
            "我不想编一个答案误导您。我实际做过的是 {真实经历}，和这个问题的关联点在于 {关联}。",
        ],
        "别做": "别不懂装懂（面试官一追问就穿帮，比不会更致命）",
    },
    {
        "场景": "被问离职原因",
        "何时用": "HR 或面试官问「为什么离开上一家」",
        "原则": "对事不对人：说发展方向不匹配，不说前公司/领导坏话",
        "话术": [
            "上一份工作我主要负责 {职责}，做下来发现我更想深耕 {方向}，那边的业务重心在 {别的方向}，所以想找个更匹配的岗位。",
            "团队和业务都比较稳定，我希望能接触更有挑战的项目，{目标岗位} 正好是我一直想做的方向。",
        ],
        "别做": "别吐槽前公司/领导/同事（HR 会立刻给你打上「难相处」标签）",
    },
    {
        "场景": "面试后感谢信",
        "何时用": "面试结束当天 24 小时内",
        "原则": "短、提到具体聊过的点、别复述简历",
        "话术": [
            "X 老师您好，感谢今天抽时间面试。您提到的 {具体问题/业务点} 让我对岗位理解更清楚了，结合我 {相关经验}，我觉得能在 {具体方向} 上快速上手。后续有需要补充的材料我随时提供。",
        ],
        "别做": "别写长篇大论复述简历；别催结果",
    },
    {
        "场景": "催面试进度",
        "何时用": "面试后超过约定时间还没消息",
        "原则": "给台阶、不施压、留退路；一次就够，别连催",
        "话术": [
            "X 老师您好，之前 XX 号面的 {岗位}，想跟您了解下后续流程有没有更新。如果还在评估中我就不打扰了，有需要补充的信息随时联系我。",
            "X 老师您好，我这边收到了另一份 offer 需要尽快答复，想问下咱们这边的进度，方便的话给个大致时间就行。感谢！",
        ],
        "别做": "别连环催（隔 3-5 天一次够了）；别一上来就要结果",
    },
    {
        "场景": "拒绝 offer / 谈崩后体面收场",
        "何时用": "决定不去，或薪资谈不拢",
        "原则": "留一线；行业圈子小",
        "话术": [
            "X 老师您好，非常感谢您的认可和这次机会。综合考虑后我决定选择另一个更匹配的方向，很遗憾这次没能合作，希望以后有机会再联系。",
            "感谢沟通。薪资方面和我目前的预期还有一点差距，如果后续有调整空间，随时欢迎再联系我。",
        ],
        "别做": "别玩消失（不回消息最伤口碑）",
    },
]

RESIGN_LETTERS = [
    {
        "风格": "标准版",
        "字数": "约 120 字",
        "正文": """尊敬的 {领导}：

您好！

因个人职业规划原因，我决定辞去 {公司} {岗位} 一职，最后工作日为 {日期}。

感谢在职期间 {公司} 给予的机会与培养，也感谢团队同事的支持。离职前我会完成手头工作的交接，并整理好相关文档。

祝公司业务蒸蒸日上，祝各位同事一切顺利。

{姓名}
{日期}""",
    },
    {
        "风格": "极简版",
        "字数": "约 50 字",
        "正文": """{领导}您好：

因个人原因，我申请辞去 {岗位} 一职，最后工作日 {日期}。我会做好交接，感谢这段时间的照顾。

{姓名}""",
    },
    {
        "风格": "交接承诺版（推荐，避免扯皮）",
        "字数": "约 150 字",
        "正文": """尊敬的 {领导}：

您好！

因个人职业规划原因，我申请辞去 {公司} {岗位} 一职，计划最后工作日为 {日期}。

离职前我将完成以下交接：
1. 手头项目 {项目} 的进度与文档整理
2. 与 {接手同事} 完成对接并留出答疑时间
3. 归还公司资产 {设备清单}

感谢这段经历的收获。祝好。

{姓名}
{日期}""",
    },
]

RESIGN_TIPS = [
    "提前 30 天以书面形式通知（试用期提前 3 天）—— 法律要求，也是体面",
    "邮件 + 纸质各留一份，并保留送达证据（邮件回执 / 签收）",
    "不要删电脑里的文件、不要带走公司资料 —— 可能被追究",
    "离职证明必须索要（下家入职要用），公司有法定义务出具",
    "确认最后一个月工资、未休年假折算、报销是否结清",
    "竞业限制如果签了，确认补偿金怎么发；没补偿的条款可主张无效",
]

# 面试 / 入职清单
CHECKLIST = {
    "面试前（1-2 天）": [
        "查公司工商信息（成立时间/融资/规模）",
        "查岗位 JD 里提到的技术，确认自己讲得出",
        "准备 2-3 个能讲透的项目（背景→我的方案→结果数字）",
        "准备「你有什么想问的」3 个问题",
        "确认时间、地点/会议链接、联系人电话",
        "打印 2 份纸质简历（现场面）",
        "线上会议：提前测摄像头麦克风与网络",
        "看一眼公司产品/官网，准备一句真实的使用感受",
    ],
    "面试当天": [
        "提前 10-15 分钟到（线上面试提前 5 分钟进）",
        "衣着整洁（互联网岗商务休闲即可）",
        "手机静音",
        "带身份证（部分写字楼需登记）",
        "记录面试官问到但没答好的问题",
    ],
    "面试后 24h 内": [
        "发感谢信/微信（提到具体聊过的点）",
        "复盘记录：被问到的题、答得差的题",
        "把新问到的技术问题补进复习清单",
    ],
    "入职材料": [
        "身份证原件 + 复印件",
        "学历/学位证书 + 复印件（部分需学信网备案表）",
        "离职证明（上家）",
        "银行卡（工资卡，确认开户行）",
        "一寸/两寸证件照若干",
        "体检报告（部分公司指定机构）",
        "社保/公积金转移所需材料",
        "简历上写的证书原件（如有）",
    ],
    "入职当天": [
        "签劳动合同：**逐条看**薪资/试用期/工作地点/期限（用工具箱的 Offer 条款检查）",
        "确认合同期限与试用期是否合规（1-3 年合同试用期≤2 个月）",
        "确认社保公积金起缴月份（试用期也要缴）",
        "要一份合同原件或复印件（双方各执一份）",
        "问清发薪日、工资卡、考勤方式",
        "领取设备并登记，记下资产编号",
    ],
}


def get_checklist() -> dict:
    return CHECKLIST


def get_playbook() -> dict:
    return {"话术": PLAYBOOK, "辞职信": RESIGN_LETTERS, "辞职提醒": RESIGN_TIPS}


# ══════════════════════════════════════════════════════════════
# 三、黑名单关键词建议（0 token，纯统计）
# ══════════════════════════════════════════════════════════════

# 明显是通用词，不该进黑名单（屏蔽会误杀一大片）
_GENERIC_WORDS = {
    "有限公司", "有限责任", "科技", "技术", "信息", "网络", "智能", "数据", "服务",
    "招聘", "岗位", "职位", "公司", "集团", "股份", "中心", "事业部", "工作室",
    "工程师", "开发", "产品", "项目", "高级", "中级", "初级", "资深", "专员",
    "深圳", "广州", "北京", "上海", "东莞", "佛山", "惠州", "珠海", "广东",
    "周末", "双休", "五险一金", "年假", "补贴", "绩效", "奖金", "经验",
}


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def blacklist_report(jobs: list, blocked_company: list, blocked_title: list,
                     check_fn=None) -> dict:
    """黑名单体检报告 —— 回答「我的黑名单在干什么、还缺什么」。

    为什么不做「n-gram 关键词建议」（2026-09-20 实测否决）：
      滑窗切出的是「开发实 / 用开发实 / 应用实」这种跨词边界的碎片；
      按分隔符切又太粗（只剩 1 条）。
      而且实测拒绝原因分布是「经验57 / 黑名单56 / 薪资27 / 标题23」——
      大头要么已被黑名单覆盖、要么是维度问题（经验/薪资**不该**用词挡）。
      所以「挖新词」这条路收益很低，真问题在别处。真正的价值是下面三块。

    ：jobs            岗位 dict 列表
    ：blocked_company 公司黑名单
    ：blocked_title   岗位黑名单
    ：check_fn        可选。`job -> (ok: bool, why: str)`，用于算拒绝原因分布
    """
    def nk(s):
        return re.sub(r"\s+", "", str(s or "")).lower()

    bl_co = [w for w in blocked_company if w]
    bl_ti = [w for w in blocked_title if w]
    hit_co, hit_ti = Counter(), Counter()
    co_rej = Counter()
    co_rej_ex: dict = {}
    co_rej_why: dict = {}
    reason_top = Counter()

    for j in jobs:
        title, comp = j.get("title") or "", j.get("company") or ""
        t, co = nk(title), nk(comp)
        for w in bl_co:
            if nk(w) in co:
                hit_co[w] += 1
        for w in bl_ti:
            if nk(w) in t:
                hit_ti[w] += 1

        if check_fn is not None:
            try:
                ok, why = check_fn(j)
            except Exception:
                ok, why = True, ""
            if not ok:
                kind = (why.split()[0] if why else "未知")
                reason_top[kind] += 1
                co_rej[comp or "(空)"] += 1
                if len(co_rej_ex.setdefault(comp or "(空)", [])) < 2:
                    co_rej_ex[comp or "(空)"].append(title[:26])
                co_rej_why.setdefault(comp or "(空)", Counter())[kind] += 1

    # ① 词效力榜 —— 哪些词真在干活
    effort = sorted(
        [(n, w, "公司") for w, n in hit_co.items() if n] +
        [(n, w, "岗位") for w, n in hit_ti.items() if n],
        reverse=True,
    )
    top_words = [{"word": w, "field": f, "blocked_jobs": n} for n, w, f in effort[:20]]

    # ② 反复出现但全被拒的公司 —— **带上拒绝原因**，否则会误导：
    #    「凡岛」4 个岗全被拒，但原因是薪资超上限（15-20K），不是公司有问题；
    #    哪天调高上限它就变可投了。所以必须让人看到原因再判断。
    cands = []
    for co, n in co_rej.most_common(120):
        if n < 2:
            break
        if co == "(空)" or any(nk(w) in nk(co) for w in bl_co):
            continue
        why_c = co_rej_why.get(co) or Counter()
        cands.append({
            "company": co, "rejected_jobs": n,
            "reasons": [f"{k}{v}个" for k, v in why_c.most_common(3)],
            "examples": co_rej_ex.get(co, []),
            # 只有因「公司/岗位词」被拒的，屏蔽才是对症的
            "blocking_would_help": any(
                k in ("黑名单公司", "黑名单岗位", "标题命中排除词") for k in why_c),
        })

    # ③ 闲置词 —— 仅作参考，**不建议删**
    idle = ([w for w in bl_ti if hit_ti[w] == 0],
            [w for w in bl_co if hit_co[w] == 0])

    return {
        "counts": {
            "jobs": len(jobs),
            "blocked_company_words": len(bl_co),
            "blocked_title_words": len(bl_ti),
            "top_word_hits": sum(n for n, _, _ in effort),
        },
        "reject_reasons": dict(reason_top.most_common(8)),
        "top_words": top_words,
        "company_candidates": cands[:15],
        "idle": {"title": idle[0], "company": idle[1]},
        "idle_note": "闲置不等于没用 —— 岗位池会变，现在没出现的词以后可能出现。"
                     "只有你确定某个类别再也不考虑时才删。",
    }


# ══════════════════════════════════════════════════════════════
# 四、公司背调（本地规则层，0 token）
# ══════════════════════════════════════════════════════════════

# 公司名里的「非真雇主」信号（中介/外包/人力）
COMPANY_RISK_PATTERNS = [
    (r"人力资源|劳务|派遣|人力服务|人才服务|职业介绍|人才市场", "中介/人力服务公司", "high"),
    (r"外包|众包|驻场|外派|服务外包", "外包/驻场性质", "high"),
    (r"信息技术服务|信息科技服务", "疑似外包承接方", "mid"),
    (r"猎头", "猎头公司", "high"),
]

# JD / 岗位名里的危险措辞
# 口径来源：小红书真实经历《在BOSS直聘找了三个月工作，我被骗了6800》(2026-07-14)
# 六种套路的共同点 = 入职前让你掏钱。规则按「名目」覆盖，最后再兜一层「要钱动作」。
JD_RISK_PATTERNS = [
    (r"培训后上岗|先培训|培训费|包就业|包分配|保offer|保录用", "培训贷/包就业话术", "high"),
    (r"入职前.{0,6}(缴费|收费|押金)|需先交|自费|购机|买设备|押证件", "入职前收费/押证", "high"),
    # —— 六种实骗套路，逐条对照 ——
    (r"包进|内推费|内推服务费|付费内推|包入职|包上岗|包过", "付费内推/保录用话术", "high"),
    (r"刷单|刷信誉|(充值|先充|垫付).{0,12}(返|佣金)", "刷单/返利诈骗", "high"),
    (r"租车费|以租代购|合作司机|车辆押金|油费押金", "套路运（租车/合作司机）", "high"),
    (r"发展下线|拉人头|下线.{0,6}提成|团队裂变|发展.{0,3}代理", "传销式招聘", "high"),
    (r"学费|岗前培训|培训.{0,6}(扣|抵扣)|先培训.{0,8}上岗", "岗前培训收费", "high"),
    (r"体检.{0,8}(自理|自付|收费|费用.{0,6}\d)|指定医院体检", "体检费套路", "high"),
    (r"账号.{0,4}冻结|已冻结|账号异常|涉嫌违规", "招聘账号异常/冻结", "high"),
    # 兜底：不管什么名目，出现「要你交钱」这个动作就 high
    (r"(需|须|请)(先)?(缴纳|支付|预付|收取).{0,8}(费|金)", "入职前收费（兜底）", "high"),
    (r"服装费|工牌.{0,4}押金|保证金", "各类名目收费", "high"),
    (r"无责底薪\s*\d|无责任底薪", "无责底薪（常见销售话术）", "mid"),
    (r"底薪加提成|提成不上限|高提成|多劳多得", "强提成制（偏销售）", "mid"),
    (r"接受小白|无需经验|零基础|无经验可做|门槛低|接受转行", "门槛异常低", "mid"),
    (r"外派|派驻|驻场|甲方现场", "外派驻场", "mid"),
    (r"日结|当天结|周结", "日结/周结计薪", "mid"),
    (r"按单结算|跑单|接单多得", "按单结算（非正式雇佣）", "mid"),
    (r"包吃住|包吃包住|提供住宿", "包吃住（常见于普工）", "low"),
]

_RISK_ORDER = {"high": 3, "mid": 2, "low": 1, "ok": 0}


def check_company(company: str, title: str = "", jd: str = "",
                  known_blocked: list | None = None) -> dict:
    """本地规则背调：0 token，纯正则。

    只做「能确定的事」——模式匹配。不下「这公司一定是骗子」的结论，
    输出的是**信号清单**，判断权归人。

    ：known_blocked  你已屏蔽的词（公司黑名单）。**必须传** ——
    品牌名抓不到模式：实测「外企德科数字」是人力外包，但名字里没有
    「人力资源/外包/劳务」任何一个，纯正则判它 ok。把黑名单并进来才不漏。
    """
    company = str(company or "")
    text = f"{title or ''} {jd or ''}"
    hits: list = []

    # ① 先对用户黑名单（覆盖品牌名，模式抓不到的那类）
    co_key = _norm_key(company)
    for w in (known_blocked or []):
        k = _norm_key(w)
        if k and len(k) >= 2 and k in co_key:
            hits.append({"level": "high", "where": "公司名",
                         "label": f"命中你已屏蔽的词「{w}」",
                         "evidence": company[:40]})
            break

    # ② 公司名模式
    for pat, label, lv in COMPANY_RISK_PATTERNS:
        if re.search(pat, company, re.I):
            hits.append({"level": lv, "where": "公司名", "label": label,
                         "evidence": company[:40]})
    # ③ JD / 标题措辞
    for pat, label, lv in JD_RISK_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            hits.append({"level": lv, "where": "岗位描述", "label": label,
                         "evidence": m.group(0)[:40]})

    worst = "ok"
    for h in hits:
        if _RISK_ORDER[h["level"]] > _RISK_ORDER[worst]:
            worst = h["level"]

    return {
        "company": company,
        "level": worst,
        "hits": hits,
        "high_count": sum(1 for h in hits if h["level"] == "high"),
        "verdict": {
            "high": "⚠️ 有高风险信号 —— 投之前先查工商信息/看准网，绝不先交任何费用或证件",
            "mid": "🔶 有可疑信号 —— 值得多看一眼 JD 和公司背景",
            "low": "🔹 有轻微信号（未必是问题）",
            "ok": "✅ 本地规则未发现明显风险信号（不等于没风险）",
        }[worst],
        "note": "本地规则只看措辞模式 + 你的黑名单，不查工商/司法数据。"
                "要更准需要联网查（企查查/天眼查/看准网）。",
    }
