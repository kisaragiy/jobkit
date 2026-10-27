#!/usr/bin/env python3
"""jobkit AI 工具集 —— 模拟面试 / 岗位方向匹配 / 职业行动计划。

设计来源（2026-09-19）：GetJob 的页面 JS 里能拿到**流程与分类框架**，拿不到提示词
（它的 prompt 在服务端）。所以这里借它的信息架构，提示词全部自研：

  模拟面试   题型分 5 类：岗位匹配 / 项目深挖 / 岗位技能 / 行为面试 / 情景收尾
             流程：出题 → 文字作答 → 点评（出题与点评分两次调用，可分别重试）
             约束：JD 40-2000 字，作答 120-4000 字
  岗位方向匹配  从现有经历反推「能拿出证据」的岗位方向
  职业行动计划  按 JD 差距生成 4-12 周准备计划

反编造纪律与 resume_optimize 一致：只允许引用简历里真实存在的经历与数字。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from resume_optimize import (  # noqa: E402
    DEFAULT_MODEL, call_llm, estimate_cost, extract_json,
)

# ── 题型（对标 GetJob tools-mock-interview.js 的 TYPE_LABELS）──
QUESTION_TYPES = {
    "fit": "岗位匹配",
    "project": "项目深挖",
    "skill": "岗位技能",
    "behavioral": "行为面试",
    "close": "情景收尾",
}

JD_MIN, JD_MAX = 40, 2000
ANS_MIN, ANS_MAX = 120, 4000

# ══════════════════════════════════════════════════════════
# 一、AI 模拟面试
# ══════════════════════════════════════════════════════════

INTERVIEW_GEN_SYSTEM = """你是资深技术面试官，正在为一位候选人准备针对性的模拟面试。

出题依据只有两份材料：候选人的真实简历、目标岗位的 JD。**不得引入材料外的信息。**

出题要求：
- 按题目配比覆盖这 5 类，用给定的 type 值：
  fit（岗位匹配，问为什么适合这个岗）、project（项目深挖，钻简历里某个项目的技术决策）、
  skill（岗位技能，问 JD 里点名的技术）、behavioral（行为面试，协作/冲突/失败复盘）、
  close（情景收尾，给一个岗位相关的实际场景让他设计方案）
- **project 类必须锚定简历里真实存在的项目名**，问具体的技术取舍，不泛泛问「介绍下你的项目」
- skill 类只问 JD 里真的写了的技术；JD 没写的不要问
- 每题给一句 why（这题想考察什么），供候选人自评
- 出题要能区分「背过」和「做过」——优先问决策依据（为什么这么选、当时有什么备选、踩了什么坑）

只输出 JSON，不要 markdown 围栏。格式：
{
  "questions": [
    {"id": 1, "type": "project", "question": "题目原文", "why": "考察点", "hint": "答题方向提示，不给答案"}
  ]
}"""

INTERVIEW_GEN_USER = """# 目标岗位
{job_title}

# 岗位 JD
{jd}

# 候选人简历
{resume}

请出 {n} 道题，type 分布尽量覆盖 5 类。"""


INTV_GRADE_SYSTEM = """你是资深技术面试官，正在点评候选人刚才的模拟面试作答。

点评原则：
- 依据只有：题目、候选人的简历、他的实际作答。**不要替他编造他没写过的经历。**
- 区分三件事：① 答得实（有具体做法和取舍）② 答得虚（正确的废话、只有结论没有过程）③ 答错/答偏
- 逐题点评，给可执行的改进点，不要"回答得不错，可以再具体一些"这种空话
- 明显短板不要留情面，但指出问题时要给替代说法（怎么答更好）

只输出 JSON，不要 markdown 围栏。格式：
{
  "total_score": 0-100 的整数,
  "summary": "整体评价，2-3 句，直说强弱",
  "items": [
    {"id": 1, "score": 0-100, "verdict": "扎实|偏虚|答偏|答错",
     "comment": "具体点评",
     "better": "更好的答法示范（可以引用简历里真实的东西）"}
  ],
  "weakest": ["最该补的 2-3 个点"],
  "next_steps": ["接下来可执行的练习建议，2-4 条"]
}"""

INTERVIEW_GRADE_USER = """# 目标岗位
{job_title}

# 岗位 JD
{jd}

# 候选人简历
{resume}

# 题目与作答
{qa}

请逐题点评。"""


def interview_questions(resume: str, jd: str, title: str = "", n: int = 6,
                        api_key: str = "", base_url: str = "",
                        model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    """出题。返回 (结果, usage)。"""
    jd = (jd or "")[:JD_MAX]
    n = max(3, min(12, n))
    content, usage = call_llm(
        [{"role": "system", "content": INTERVIEW_GEN_SYSTEM},
         {"role": "user", "content": INTERVIEW_GEN_USER.format(
             job_title=title or "（未提供，请从 JD 推断）", jd=jd, resume=resume, n=n)}],
        model=model, api_key=api_key, base_url=base_url, max_tokens=6000)
    return extract_json(content), usage


def interview_grade(resume: str, jd: str, title: str, qa: list[dict],
                    api_key: str = "", base_url: str = "",
                    model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    """点评。qa = [{"id":1,"type":"project","question":"...","answer":"..."}]"""
    blocks = []
    for it in qa:
        blocks.append(
            f"## 第 {it.get('id')} 题（{QUESTION_TYPES.get(it.get('type'), it.get('type') or '未分类')}）\n"
            f"题目：{it.get('question', '')}\n"
            f"作答：{(it.get('answer') or '（未作答）')[:ANS_MAX]}")
    content, usage = call_llm(
        [{"role": "system", "content": INTV_GRADE_SYSTEM},
         {"role": "user", "content": INTERVIEW_GRADE_USER.format(
             job_title=title or "（未提供）", jd=(jd or "")[:JD_MAX],
             resume=resume, qa="\n\n".join(blocks))}],
        model=model, api_key=api_key, base_url=base_url, max_tokens=8000)
    return extract_json(content), usage


# ══════════════════════════════════════════════════════════
# 二、岗位方向匹配
# ══════════════════════════════════════════════════════════

ROLE_FIT_SYSTEM = """你是求职策略顾问。任务：从候选人的真实简历出发，找出他**能拿出证据**的岗位方向。

铁律：
- 每个方向都必须能从简历里指出**具体证据**（项目名、技术栈、数字）。指不出来就不许写。
- 不要给"也可以试试 AI 产品经理"这种没有证据支撑的方向。
- 不要建议他去学一个简历里完全没有的东西（那是行动计划的事，不是方向匹配）。

方向数量 3-6 个，按匹配度降序。

只输出 JSON，不要 markdown 围栏。格式：
{
  "directions": [
    {"title": "岗位方向名，如 Python 后端 / LLM 应用开发",
     "fit_score": 0-100 的整数,
     "evidence": ["简历里的具体证据，逐条"],
     "needs_gap": ["这个方向还差什么（能补的）"],
     "search_keywords": ["BOSS 上搜索用的关键词，3-6 个"]
    }
  ],
  "best_bet": "最推荐哪一个方向，一句话说为什么",
  "avoid": ["简历支撑不足、不建议投的方向及原因"]
}"""

ROLE_FIT_USER = """# 候选人简历
{resume}

请给出岗位方向建议。"""


def role_fit(resume: str, api_key: str = "", base_url: str = "",
             model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    content, usage = call_llm(
        [{"role": "system", "content": ROLE_FIT_SYSTEM},
         {"role": "user", "content": ROLE_FIT_USER.format(resume=resume)}],
        model=model, api_key=api_key, base_url=base_url, max_tokens=6000)
    return extract_json(content), usage


# ══════════════════════════════════════════════════════════
# 三、职业行动计划
# ══════════════════════════════════════════════════════════

CAREER_PLAN_SYSTEM = """你是求职教练。任务：拿候选人的真实简历对照目标 JD，找出差距，排一个 4-12 周的准备计划。

铁律：
- 计划里的每一项都要能**落到可验证的产出**（"读完某文档"不算，"用某技术做一个能跑的小项目并写 README"才算）
- 按周排，每周有明确目标和可验收的产出
- 差距要分清：① 硬缺口（JD 点名但简历完全没有）② 弱证据（会但没写/没产出）③ 非技术项（表达、简历结构）
- 时间要现实：一个全职找工作的人每周能投入的时间有限，别排满

只输出 JSON，不要 markdown 围栏。格式：
{
  "gap_analysis": {
    "hard_gaps": [{"item": "缺什么", "why": "JD 哪里要求", "severity": "高|中|低"}],
    "weak_evidence": ["会但没拿得出手的产出"],
    "non_technical": ["表达 / 简历 / 面试技巧层面"]
  },
  "weeks": [
    {"week": "第 1-2 周", "goal": "这阶段要达成什么",
     "tasks": ["具体任务"], "deliverable": "可验收产出"}
  ],
  "priority_note": "如果时间不够，最先保哪两件事"
}"""

CAREER_PLAN_USER = """# 目标岗位
{job_title}

# 岗位 JD
{jd}

# 候选人简历
{resume}

请排 {weeks} 周的准备计划。"""


def career_plan(resume: str, jd: str, title: str = "", weeks: int = 6,
                api_key: str = "", base_url: str = "",
                model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    weeks = max(4, min(12, weeks))
    content, usage = call_llm(
        [{"role": "system", "content": CAREER_PLAN_SYSTEM},
         {"role": "user", "content": CAREER_PLAN_USER.format(
             job_title=title or "（未提供，请从 JD 推断）",
             jd=(jd or "")[:JD_MAX], resume=resume, weeks=weeks)}],
        model=model, api_key=api_key, base_url=base_url, max_tokens=8000)
    return extract_json(content), usage
