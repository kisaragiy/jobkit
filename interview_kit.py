"""面试题库生成 —— 出题，但**不替你编答案**。

为什么只出题不写答案
--------------------
模型不知道你项目的真实细节。它写出来的「答题要点」会是你没做过的事，
面试官追问两句就露馅 —— 那比没有答案更糟。

所以这里只做两件事：
1. 按 JD 生成「你最可能被问到什么」（这个模型很擅长，且错了也不致命）
2. 给每类题**答题结构提示**（讲什么、别讲什么）—— 静态知识，不烧 token

答案必须用你自己的真实项目填。填充时可以先跑 `jobkit show N` 回忆岗位细节。

小模型约束（2026-09-21 教训）
------------------------------
本地 qwen 出不了稳定 JSON，会吐半截。所以这里只要**纯文本**：
用【技术】【项目】【行为】分段 + 编号行，正则解析，解析不出来就明确报错，
绝不把半截文本当题目列表返回。
"""
from __future__ import annotations

import re

DEFAULT_MODEL = ""          # 交给 llm_local 自己挑

SYSTEM_PROMPT = (
    "你是一位在中国互联网公司做了十年的技术面试官。"
    "你出题的特点是具体、贴岗位、不空洞，喜欢问「你怎么做的」而不是「什么是」。"
)

USER_TEMPLATE = """你正在面试一位应聘「{title}」岗位的候选人，公司是{company}。

岗位要求：
{jd}

候选人的技术背景：
{resume}

请生成 10 个这个岗位最可能被问到的问题，严格按下面三组输出：

【技术】
1. 问题
2. 问题
...（6 个）

【项目】
7. 问题
...（3 个）

【行为】
10. 问题
...（1 个）

要求：
- 全部用中文，每行一个编号问题，不要输出答案
- 技术题要贴着 JD 里出现的技术栈问，不要问这个岗位用不到的东西
- 项目题要能引导候选人讲自己的真实经历
- 不要出现「请介绍你自己」「你的优缺点」这种通用题
"""

# 每组题的答题结构提示（静态，不花 token）
ANSWER_GUIDE = {
    "技术": [
        "先说方案为什么这么选（对比过什么、放弃的选项是什么）",
        "再说遇到的具体问题（报错/性能/边界），不要只说「很好用」",
        "最后说怎么验证的（测试/指标/线上数据）",
        "被追问「还有别的办法吗」时，要能给出至少一个替代方案和它的缺点",
    ],
    "项目": [
        "一句话说清这个项目解决什么问题、给谁用",
        "明确「哪些是我做的」——团队项目最容易在这里翻车",
        "讲最难的一个技术决策，以及当时的取舍",
        "准备一个「如果重做会怎么改」的答案，这题几乎必问",
    ],
    "行为": [
        "给具体例子，不要形容词（不说「我抗压能力强」，说「那次连轴转三天我做了什么」）",
        "结尾回到「所以我现在想找一个能长期做的方向」",
    ],
}

_SECTION_RE = re.compile(r"【(技术|项目|行为)】")
_ITEM_RE = re.compile(r"^\s*(\d{1,2})[.、)]\s*(.+?)\s*$")


class QuestionParseError(ValueError):
    """模型输出没能解析成题目 —— 必须暴露，不能静默返回空列表。"""


def _parse(text: str) -> dict[str, list[str]]:
    """把纯文本解析成 {类别: [题目...]}。"""
    out: dict[str, list[str]] = {"技术": [], "项目": [], "行为": []}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION_RE.search(line)
        if m:
            current = m.group(1)
            continue
        m = _ITEM_RE.match(line)
        if m and current:
            # 先剥掉模型自带的各种句末标点，再加统一的「？」，避免「。？」这种双标点
            q = m.group(2).strip().rstrip("？?。.！!；;").strip()
            if q:
                out[current].append(q + "？")
    return out


def generate(jd: str, resume: str, *, company: str = "（未提供）",
             title: str = "（未提供）", model: str = DEFAULT_MODEL,
             temperature: float = 0.6) -> dict:
    """生成面试题。返回 {"技术": [...], "项目": [...], "行为": [...], "source": ...}。

    解析失败抛 QuestionParseError —— 上层要能区分「没生成出来」和「生成了但没解析到」。
    """
    import llm_local

    prompt = USER_TEMPLATE.format(
        title=title, company=company, jd=(jd or "")[:3000], resume=(resume or "")[:2000],
    )
    content, meta = llm_local.chat(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
        model=model, temperature=temperature,
    )
    if not isinstance(content, str) or not content.strip():
        raise QuestionParseError(
            f"模型没有返回内容（model={meta.get('model')}，"
            f"thinking={meta.get('thinking_len')} 字，耗时 {meta.get('elapsed_sec')}s）"
        )

    groups = _parse(content)
    total = sum(len(v) for v in groups.values())
    if total == 0:
        raise QuestionParseError(
            f"模型输出了 {len(content)} 字，但没解析出任何题目。原始输出前 200 字：{content[:200]!r}"
        )
    groups["source"] = "local" if model else "auto"
    groups["raw"] = content
    return groups


def guide_for(category: str) -> list[str]:
    return ANSWER_GUIDE.get(category, [])


def render(groups: dict) -> str:
    """把题库渲染成可复制的文本（方便贴到笔记里逐条填答案）。"""
    lines = []
    total = sum(len(groups.get(k) or []) for k in ("技术", "项目", "行为"))
    lines.append(f"共 {total} 题")
    for cat in ("技术", "项目", "行为"):
        items = groups.get(cat) or []
        if not items:
            continue
        lines.append(f"\n【{cat}】{len(items)} 题")
        for i, q in enumerate(items, 1):
            lines.append(f"  {i}. {q}")
        lines.append("  答题结构：")
        for g in guide_for(cat):
            lines.append(f"    · {g}")
    lines.append(
        "\n⚠ 项目题里的技术细节是模型根据你的背景**推测**的，"
        "按你真实做过的内容改，别照着编。"
    )
    return "\n".join(lines)
