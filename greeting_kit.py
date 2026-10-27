"""招呼语质量检查（纯规则，零 token）。

为什么先做检查而不是效果追踪
----------------------------
效果追踪需要「发出去的原话 + 对方是否回复」，但库里 28 条 sent 记录的
message 全是空的（早期版本没存），只有 4 条有内容。87% 缺失的情况下
做「哪句话有效」的统计，出来的结论全是噪音。

所以先做**不依赖历史**的部分：一句话好不好，规则就能判个八九不离十。
等 message 稳定记录、样本够了，再接效果追踪（接口已经留好）。

评分思路
--------
不是给「文采分」，是给**回复概率分**。HR 每天看几十条，决定回不回你的
只有三件事：① 这条是不是群发（空洞开场 = 一眼假）② 有没有干货（技术栈/
项目/成果）③ 有没有理由回你（问句/明确诉求）。

所以六项：开场、长度、信息密度、证据、岗位匹配、结尾诉求。
每项都有明确扣分理由，不做黑箱打分。
"""
from __future__ import annotations

import re

# 一眼就是群发的开场 —— 这类开头 HR 直接跳过
EMPTY_OPENINGS = [
    "你好，还招人吗", "还招人吗", "在吗", "你好在吗", "hi", "hello",
    "请问还招人吗", "招人吗", "您好，还招人吗", "有人吗",
]
# 有称呼说明看过岗位，不是群发
POLITE_OPENINGS = ["您好", "你好", "HR", "老师"]

TECH_HINTS = (
    "python", "java", "go", "rust", "fastapi", "django", "flask", "langchain",
    "langgraph", "rag", "agent", "llm", "llama", "qwen", "ollama", "docker",
    "k8s", "kubernetes", "mysql", "redis", "postgres", "mongodb", "kafka",
    "微服务", "多agent", "多 agent", "提示词", "微调", "lora", "量化", "向量",
    "comfyui", "sdxl", "diffusion", "pytorch", "transformers", "vllm",
)
# 有数字/成果的句子比形容词可信
EVIDENCE_RE = re.compile(
    r"(\d+\s*(万|k|qps|并发|条|个|%|倍|小时|秒|天|人))|"
    r"(上线|交付|部署|落地|重构|优化到|从.+?到|降低|提升|节省)", re.I)
# 结尾有没有「给对方一个回你的理由」
ASK_RE = re.compile(r"(方便.{0,6}(聊|说|沟通|看)|可以.{0,6}(聊|沟通)|"
                    r"是否|能否|有没有机会|期待|想了解|请教)", re.I)

MIN_LEN, MAX_LEN = 40, 220


def _hit_tech(text: str) -> list[str]:
    low = text.lower()
    return [k for k in TECH_HINTS if k in low]


def check(text: str, jd: str = "") -> dict:
    """给招呼语打分。返回 score(0-100) / grade / issues / strengths。"""
    t = (text or "").strip()
    issues: list[str] = []
    strengths: list[str] = []
    score = 100

    if not t:
        return {"score": 0, "grade": "F", "issues": ["内容为空"],
                "strengths": [], "tech": [], "length": 0}

    # ① 空洞开场：直接重扣。这是最致命的一类，HR 一眼判定群发。
    low = t.lower()
    if any(low.startswith(o.lower()) or low == o.lower() for o in EMPTY_OPENINGS):
        score -= 45
        issues.append("开场是「还招人吗」这类空话 —— HR 每天看几十条，这种直接划走")
    elif not any(t.startswith(p) for p in POLITE_OPENINGS):
        score -= 8
        issues.append("没有称呼（您好/HR 老师）—— 不写称呼显得群发")

    # ② 长度：太短没信息，太长 HR 不看
    n = len(t)
    if n < MIN_LEN:
        score -= 30
        issues.append(f"只有 {n} 字，太短 —— 说不出你能干什么（建议 {MIN_LEN}-{MAX_LEN} 字）")
    elif n > MAX_LEN:
        score -= 18
        issues.append(f"{n} 字太长 —— BOSS 上超过 4 行大概率被略过，砍到 {MAX_LEN} 字内")

    # ③ 技术信息密度
    tech = _hit_tech(t)
    if not tech:
        score -= 25
        issues.append("没提到任何具体技术栈 —— 全是大词，HR 无法判断你会什么")
    elif len(tech) >= 3:
        strengths.append(f"提到 {len(tech)} 个具体技术：{'、'.join(tech[:5])}")
    else:
        strengths.append(f"提到技术：{'、'.join(tech)}")

    # ④ 证据：数字/成果 > 形容词
    if EVIDENCE_RE.search(t):
        strengths.append("有具体成果/数字，比「精通/熟悉」可信")
    else:
        score -= 15
        issues.append("没有可验证的成果 —— 「精通/熟悉」这类词 HR 默认不信，给数字或项目名")

    # ⑤ 和岗位的匹配（有 JD 时才算，没 JD 不算扣分）
    if jd:
        jd_low = jd.lower()
        overlap = [k for k in tech if k in jd_low]
        if overlap:
            strengths.append(f"命中 JD 关键词：{'、'.join(overlap[:5])}")
        elif tech:
            score -= 12
            issues.append("提到的技术 JD 里一个都没有 —— 说明没对着岗位写，是通用模板")

    # ⑥ 结尾有没有给对方回你的理由
    if ASK_RE.search(t):
        strengths.append("结尾有明确诉求/问句，给了对方回复的抓手")
    else:
        score -= 10
        issues.append("结尾没抛问题 —— 加一句「方便聊 3 分钟吗」比空收尾回复率高")

    score = max(0, min(100, score))
    return {
        "score": score,
        "grade": _grade(score),
        "issues": issues,
        "strengths": strengths,
        "tech": tech,
        "length": n,
    }


def _grade(s: int) -> str:
    if s >= 85:
        return "A"
    if s >= 70:
        return "B"
    if s >= 50:
        return "C"
    return "D"


def render(r: dict, text: str = "") -> str:
    lines = [f"招呼语评分：{r['score']}/100（{r['grade']}）  {r['length']} 字"]
    if r["strengths"]:
        lines.append("")
        lines.append("✅ 做对的地方：")
        lines.extend(f"  · {s}" for s in r["strengths"])
    if r["issues"]:
        lines.append("")
        lines.append("⚠ 要改的地方：")
        lines.extend(f"  · {s}" for s in r["issues"])
    if not r["issues"]:
        lines.append("")
        lines.append("没有发现明显问题。")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        txt = sys.argv[1]
    else:
        txt = "你好，还招人吗？"
    print(render(check(txt)))
