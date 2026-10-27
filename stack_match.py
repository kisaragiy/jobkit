"""技术栈匹配 —— 投递前判断「这个岗的栈你够不够」。

来自真实痛点（牛客 28 届实录）：JD 写 langchain，拿 Java 的 agent 去砸，
筛那关就过不去。投递量大但转化极低时，第一个该修的是「别投不匹配的」，
而不是「把招呼语写得更好」。

设计原则：
  1. **只用 JD 原文**，不做推测；JD 没了就明说「看不出」，不硬给分。
  2. **区分「硬缺」和「可补」**：主语言不对是硬缺，缺某个框架是可补。
  3. **识别「或」语义**：「Python 或 Java 任一门」不算不匹配 —— 误杀比漏判贵。
  4. 画像来自配置文件（用户可改），不猜用户会什么。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parent / "stack_profile.json"

# 默认画像：只写用户明确说过的（AI 应用开发 / Python 后端 / LLM Agent / AIGC），
# 不确定的一律留空 —— 空表示「没写」，不是「不会」。
DEFAULT_PROFILE = {
    "_说明": "skills=你会的；weak=会一点但别当卖点；never=明确不会（用来判硬缺）",
    "skills": ["python", "fastapi", "flask", "django", "llm", "rag", "agent",
               "langchain", "prompt", "大模型", "comfyui", "mysql", "redis"],
    "weak": ["vue", "react", "javascript", "linux"],
    "never": ["php", "c#", "ruby"],
    "core_lang": ["python"],          # 你的主语言：JD 只要别的语言就算硬缺
    "acceptable_lang": ["java", "go", "golang"],  # 能读能改，但不是卖点
}

# ── 词表：全部带词边界，避免 javascript 命中 java、yes 命中 es ──────
LANG_PATTERNS = {
    "python": r"(?<![a-z])python(?![a-z])|(?<![a-z])py(?![a-z])thon",
    "java": r"(?<![a-z])java(?![a-z])(?!script)",
    "golang": r"(?<![a-z])golang(?![a-z])|(?<![a-z])go\s*语言",
    "c++": r"(?<![a-z])c\+\+",
    "javascript": r"(?<![a-z])javascript(?![a-z])|(?<![a-z])js(?![a-z])",
    "typescript": r"(?<![a-z])typescript(?![a-z])",
    "php": r"(?<![a-z])php(?![a-z])",
    "node": r"(?<![a-z])node(?:\.?js)?(?![a-z])",
}

FRAMEWORK_PATTERNS = {
    "spring": r"(?<![a-z])spring(?:boot|cloud)?(?![a-z])",
    "mybatis": r"(?<![a-z])mybatis(?![a-z])",
    "fastapi": r"(?<![a-z])fastapi(?![a-z])",
    "flask": r"(?<![a-z])flask(?![a-z])",
    "django": r"(?<![a-z])django(?![a-z])",
    "vue": r"(?<![a-z])vue(?:\.?js)?(?![a-z])",
    "react": r"(?<![a-z])react(?![a-z])",
    "langchain": r"(?<![a-z])langchain(?![a-z])",
    "langgraph": r"(?<![a-z])langgraph(?![a-z])",
    "dify": r"(?<![a-z])dify(?![a-z])",
    "coze": r"(?<![a-z])coze(?![a-z])",
    "pytorch": r"(?<![a-z])pytorch(?![a-z])",
    "tensorflow": r"(?<![a-z])tensorflow(?![a-z])",
    "transformers": r"(?<![a-z])transformers?(?![a-z])",
    "vllm": r"(?<![a-z])vllm(?![a-z])",
    "milvus": r"(?<![a-z])milvus(?![a-z])",
    "faiss": r"(?<![a-z])faiss(?![a-z])",
    "elasticsearch": r"(?<![a-z])elasticsearch(?![a-z])|(?<![a-z])ES(?![a-z])",
}

AI_PATTERNS = {
    "llm": r"(?<![a-z])llm(?![a-z])|大模型|语言模型",
    "rag": r"(?<![a-z])rag(?![a-z])|检索增强",
    "agent": r"(?<![a-z])agent(?![a-z])|智能体",
    "prompt": r"(?<![a-z])prompt(?![a-z])|提示词",
    "finetune": r"微调|(?<![a-z])finetun|lora(?![a-z])",
    "cv": r"计算机视觉|图像识别|(?<![a-z])cv(?![a-z])",
}

# 「或」类语义：出现这些词说明语言要求不是排他的
OR_HINTS = ("任一门", "任一", "或者", "或", "熟悉其中", "至少一种", "之一", "其一")


def load_profile(path: Path | None = None) -> dict:
    """读画像；文件不存在就用默认值（并在返回值里标明来源）。"""
    p = Path(path) if path else PROFILE_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = dict(DEFAULT_PROFILE)
                out.update({k: v for k, v in data.items() if not k.startswith("_")})
                out["_来源"] = str(p)
                return out
        except (ValueError, OSError):
            pass
    out = dict(DEFAULT_PROFILE)
    out["_来源"] = "内置默认（建议跑 `python jobkit.py stack --init` 生成后自己改）"
    return out


def save_profile(path: Path | None = None) -> Path:
    p = Path(path) if path else PROFILE_PATH
    payload = {k: v for k, v in DEFAULT_PROFILE.items()}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _hits(text: str, patterns: dict) -> list[str]:
    low = (text or "").lower()
    if not low:
        return []
    # ES 是大小写敏感的短词，单独在原文上匹配
    found = []
    for name, pat in patterns.items():
        target = (text or "") if name == "elasticsearch" else low
        if re.search(pat, target, flags=re.IGNORECASE if name != "elasticsearch" else 0):
            found.append(name)
    return found


def extract_stacks(jd: str) -> dict:
    """从 JD 原文抽技术栈；抽不到就是抽不到，不脑补。"""
    text = jd or ""
    return {
        "langs": _hits(text, LANG_PATTERNS),
        "frameworks": _hits(text, FRAMEWORK_PATTERNS),
        "ai": _hits(text, AI_PATTERNS),
        "has_or_hint": any(h in text for h in OR_HINTS),
        "chars": len(text.strip()),
    }


def _judge_lang(stacks: dict, profile: dict) -> tuple[str, str]:
    """主语言判定。返回 (verdict, why)。

    verdict:
      match        JD 要求里有你的主语言
      acceptable   主语言在，另外还要求别的语言（那些是加分项，不是门槛）
      mismatch     主语言完全用不上 —— 投了就是浪费一次机会
      unknown      看不出语言要求

    「Python 或 Java 任一门」这种写法天然落在 match：langs 同时含两者。
    """
    langs = set(stacks["langs"])
    core = set(profile.get("core_lang") or [])

    if not langs:
        return "unknown", "JD 里没看出明确语言要求"

    if langs & core:
        extra = langs - core
        if extra:
            return "acceptable", (f"要求 {'/'.join(sorted(extra))}，"
                                   f"但 {'/'.join(sorted(langs & core))} 也在可选范围内")
        return "match", f"命中主语言 {'/'.join(sorted(langs & core))}"

    return "mismatch", f"只要 {'/'.join(sorted(langs))}，你主语言" \
                       f"（{'/'.join(sorted(core)) or '未设置'}）用不上"


def match(job: dict, profile: dict | None = None) -> dict:
    """给单个岗位出技术栈结论。

    返回 {verdict, delta, why, hits, gaps, stacks}
      verdict: strong / ok / weak / mismatch / unknown
      delta:   建议的优先级加减分（mismatch 给强负分，unknown 给 0）
    """
    prof = profile or load_profile()
    jd = (job.get("job_description") or "").strip()
    if not jd:
        return {"verdict": "unknown", "delta": 0, "why": "没有 JD，看不出技术栈",
                "hits": [], "gaps": [], "stacks": {}}

    stacks = extract_stacks(jd)
    skills = {s.lower() for s in (prof.get("skills") or [])}
    never = {s.lower() for s in (prof.get("never") or [])}
    weak = {s.lower() for s in (prof.get("weak") or [])}

    found = set(stacks["langs"]) | set(stacks["frameworks"]) | set(stacks["ai"])
    hits = sorted(found & skills)
    gaps = sorted(found - skills)
    conflicts = sorted(found & never)          # JD 要求你明确不会的
    weak_hits = sorted(found & weak)

    lang_verdict, lang_why = _judge_lang(stacks, prof)

    if lang_verdict == "mismatch":
        return {"verdict": "mismatch", "delta": -30,
                "why": f"技术栈硬缺：{lang_why}",
                "hits": hits, "gaps": gaps, "stacks": stacks}

    if conflicts:
        return {"verdict": "mismatch", "delta": -25,
                "why": f"JD 要求你明确不会的：{'/'.join(conflicts)}",
                "hits": hits, "gaps": gaps, "stacks": stacks}

    score = 0
    reasons = [lang_why]
    if hits:
        score += min(12, 3 * len(hits))
        reasons.append(f"命中 {len(hits)} 项：{'/'.join(hits[:6])}")
    if weak_hits:
        reasons.append(f"只沾边：{'/'.join(weak_hits)}")
    if len(gaps) >= 4 and not hits:
        score -= 8
        reasons.append(f"要求 {len(gaps)} 项你都没有")

    if score >= 9:
        verdict = "strong"
    elif score >= 3:
        verdict = "ok"
    elif lang_verdict == "acceptable" or score == 0:
        verdict = "weak"
    else:
        verdict = "weak"

    return {"verdict": verdict, "delta": score, "why": "；".join(reasons),
            "hits": hits, "gaps": gaps, "stacks": stacks}


VERDICT_LABEL = {
    "strong": "✅ 强匹配",
    "ok": "🟢 可投",
    "weak": "🟡 勉强",
    "mismatch": "⛔ 别投",
    "unknown": "⬜ 看不出",
}


def render(rows: list[dict]) -> str:
    """把批量结果渲染成人看的清单。"""
    if not rows:
        return "（没有可评估的岗位）"
    lines = [f"技术栈匹配 · {len(rows)} 个岗位", "=" * 56, ""]
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r["verdict"], []).append(r)
    for key in ("mismatch", "weak", "ok", "strong", "unknown"):
        group = buckets.get(key) or []
        if not group:
            continue
        lines.append(f"{VERDICT_LABEL[key]}（{len(group)}）")
        for r in group[:8]:
            title = (r.get("title") or "?")[:24]
            company = (r.get("company") or "?")[:14]
            lines.append(f"  · {title} @ {company}")
            lines.append(f"    {r['why']}")
        if len(group) > 8:
            lines.append(f"  …还有 {len(group) - 8} 个")
        lines.append("")
    return "\n".join(lines)
