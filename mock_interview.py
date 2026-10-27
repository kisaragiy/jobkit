"""模拟面试答题卡：拿到真题后，给你「考官想听什么 / 怎么讲 / 会被追问什么」。

和 interview_kit 的分工：
    interview_kit  → 生成题目（我没有题的时候用）
    mock_interview → 拿到真题后准备答题卡（HR 真问了你，要用它）

**不编内容。** 这里只给结构：题型判定、骨架、必被追问的问题、评分红线。
具体事实（你做的项目、你的数字）必须你自己填 —— 编一个数字，面试官追问两层就崩。
"""
from __future__ import annotations

import re

# 题型 → (考察点, 骨架, 必被追问, 红线)
# 骨架是方法论，不是事实，可以固化；内容永远来自用户自己。
KINDS = {
    "aigc": {
        "label": "AIGC / 生成内容实操",
        "looks": ("aigc", "生图", "出图", "视频", "comfyui", "sdxl", "lora", "工作流",
                  "画面", "镜头", "分镜"),
        "checks": ["你真做过吗（细节能对上）", "出问题时你怎么定位", "有没有量化结果"],
        "frame": [
            "一句话交代项目：给谁做、做什么、交付了什么",
            "讲执行链路：模型 → 参数 → 后处理 → 质检，每一步为什么这么选",
            "讲一次翻车：现象 → 排查 → 改了什么 → 效果怎么确认",
            "给数字：出图量、成功率、返修率、耗时",
        ],
        "followups": [
            "这条片子/这张图，你自己不满意的地方是哪？",
            "同样的 prompt 换个模型，结果会怎样？为什么？",
            "人物不一致你怎么保证的（参考图？固定 seed？LoRA？）",
            "如果客户说「不像」，你第一步改什么？",
        ],
        "redline": "只讲工具名词、讲不出翻车和修复过程 = 没过",
    },
    "project": {
        "label": "项目深挖",
        "looks": ("项目", "做过什么", "介绍一下", "技术难点", "架构", "为什么这么设计"),
        "checks": ["你负责哪一块（不是团队做了什么）", "为什么这么选", "踩过什么坑"],
        "frame": [
            "背景一句：解决什么问题、给谁用",
            "你负责的部分（明确边界，别把团队的算自己的）",
            "关键技术选型 + 为什么（说 2 个备选和取舍）",
            "结果 + 数字（QPS / 延迟 / 覆盖率 / 缺陷率）",
        ],
        "followups": [
            "这块如果流量翻十倍，先崩在哪？",
            "当时为什么不用更简单的方案？",
            "现在回头看，哪里会重写？",
        ],
        "redline": "把团队成果说成自己的，追问两层就露",
    },
    "backend": {
        "label": "后端 / 工程能力",
        "looks": ("并发", "数据库", "索引", "缓存", "接口", "性能", "部署", "docker",
                  "redis", "mysql", "事务", "队列"),
        "checks": ["基础是否扎实", "有没有线上排障经验", "边界条件想不想得到"],
        "frame": [
            "先给结论/方案，再讲理由",
            "讲清数据流：请求从进来到落库经过哪些环节",
            "讲一致性和失败处理（超时、重试、幂等）",
            "给观测手段：怎么发现出问题了",
        ],
        "followups": [
            "这个方案在什么情况下会失效？",
            "怎么防止重复提交 / 消息重复消费？",
            "线上突然变慢，你的排查顺序是什么？",
        ],
        "redline": "只背概念、说不出线上现象 = 没过",
    },
    "behavior": {
        "label": "行为 / 软素质",
        "looks": ("为什么离职", "职业规划", "优缺点", "怎么处理冲突", "加班", "压力",
                  "为什么选我们", "团队"),
        "checks": ["稳定性", "自我认知", "沟通是否坦诚"],
        "frame": [
            "STAR：情境一句 → 你的任务 → 你的动作 → 结果",
            "别评价前公司/同事，只说事实和自己的选择",
            "结尾落到「所以我想来这里做什么」",
        ],
        "followups": [
            "如果当时领导不同意你的做法，你会怎么处理？",
            "这件事你最大的收获是什么？",
        ],
        "redline": "抱怨前东家 / 空话套话 = 减分",
    },
    "unknown": {
        "label": "未归类",
        "looks": (),
        "checks": ["先判断这道题在考什么，再决定怎么答"],
        "frame": [
            "先用一句话确认理解：「您是问……对吗？」",
            "再按「结论 → 依据 → 例子」回答",
        ],
        "followups": [],
        "redline": "没听懂就硬答 = 最亏",
    },
}

_ZW = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_NUM_RE = re.compile(
    r"(?:^|[\s。；;，,？?!！])\s*([0-9]{1,2}[.、)）](?![0-9])|[（(][0-9]{1,2}[)）]|问[:：])")


def split_questions(text: str) -> list[str]:
    """把 HR 一条长消息切成若干道题。

    真库原话：「1. 展示一条…执行过程。2. AIGC视频出现产品变形…你如何…」
    没有编号就整条当一题（宁可少切，也不要把一句话劈成两半）。

    注意：BOSS 的文本里会夹零宽字符（「过程。」和「2.」之间就有一个 U+200B），
    不清掉的话编号匹配不到 —— 这类隐形字符是切分失败的头号原因。
    """
    t = _ZW.sub("", str(text or "")).strip()
    if not t:
        return []
    marks = [m.start(1) for m in _NUM_RE.finditer(t)]
    if not marks:
        return [t]
    # 只有一处编号也可能要切：「…解决？3. 你独立搭建过…」= 前文 + 编号题
    if marks[0] > 0:
        marks = [0] + marks
    parts = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(t)
        seg = t[start:end].strip().strip("。；;，,？?！!")
        seg = re.sub(r"^[0-9]{1,2}[.、)）]\s*|^[（(][0-9]{1,2}[)）]\s*|^问[:：]\s*", "", seg)
        if len(seg) >= 4:                 # 太短的多半是误切
            parts.append(seg.strip())
    return parts or [t]


def classify(question: str) -> str:
    q = str(question or "").lower()
    best, best_hits = "unknown", 0
    for kind, spec in KINDS.items():
        hits = sum(1 for k in spec["looks"] if k in q)
        if hits > best_hits:
            best, best_hits = kind, hits
    return best


def card(question: str) -> dict:
    """一道题的答题卡。不含具体事实，只有结构和追问。"""
    kind = classify(question)
    spec = KINDS[kind]
    return {
        "question": str(question or "").strip(),
        "kind": kind,
        "label": spec["label"],
        "checks": list(spec["checks"]),
        "frame": list(spec["frame"]),
        "followups": list(spec["followups"]),
        "redline": spec["redline"],
        "need_from_you": [],   # 由 prepare 填
    }


# 答不好的常见原因（自评用，不涉及事实）
WEAK_SIGNS = {
    "只说工具名": "没有讲「为什么选它」和「出问题怎么定位」，听着像看过教程",
    "没有数字": "没有量化结果，面试官无法判断你的实际贡献",
    "全程说「我们」": "分不清哪部分是你做的，容易被追穿",
    "没有翻车": "真实项目一定翻过车；一点不讲反而不可信",
}


def prepare(text: str) -> dict:
    """一条 HR 消息 → 若干张答题卡 + 需要你补的素材。"""
    qs = split_questions(text)
    cards = [card(q) for q in qs]
    need = []
    for c in cards:
        if c["kind"] in ("aigc", "project", "backend"):
            need.append("这个项目的真实数字（规模/耗时/成功率/缺陷数，任选两个）")
        if c["kind"] == "aigc":
            need.append("一次真实的翻车 + 你怎么定位修复的")
    # 去重保序
    seen, dedup = set(), []
    for n in need:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    return {"questions": qs, "cards": cards, "need_from_you": dedup}


def render(pack: dict) -> str:
    lines = []
    cards = pack.get("cards") or []
    lines.append(f"🎤 这道题拆成 {len(cards)} 问，逐题准备：")
    for i, c in enumerate(cards, 1):
        lines.append("")
        lines.append(f"[{i}] {c['question'][:60]}")
        lines.append(f"    类型：{c['label']}")
        lines.append(f"    考官想听：{'；'.join(c['checks'])}")
        lines.append("    怎么讲：")
        for step in c["frame"]:
            lines.append(f"      · {step}")
        if c["followups"]:
            lines.append("    必被追问（提前想好）：")
            for f in c["followups"]:
                lines.append(f"      ? {f}")
        lines.append(f"    ⛔ 红线：{c['redline']}")
    need = pack.get("need_from_you") or []
    if need:
        lines.append("")
        lines.append("📌 答题前先补这些素材（我编不了，编了会被追问穿）：")
        for n in need:
            lines.append(f"    · {n}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="拿到真题后准备答题卡")
    ap.add_argument("text", nargs="?", default="", help="HR 的消息原文（可以直接贴）")
    ap.add_argument("--from-chat", metavar="公司名", help="从库里取这家的最后一条消息")
    args = ap.parse_args(argv)

    text = args.text
    if args.from_chat:
        import store
        conn = store.connect()
        try:
            rows = store.list_conversations(conn, pending_only=False, limit=200)
        finally:
            conn.close()
        hit = next((r for r in rows if args.from_chat in (r.get("company") or "")), None)
        if not hit:
            print(f"❌ 找不到「{args.from_chat}」的会话")
            return 2
        text = hit.get("last_msg") or ""

    if not text:
        ap.print_help()
        return 1

    print(render(prepare(text)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
