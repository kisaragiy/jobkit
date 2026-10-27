"""简历结构诊断：项目四块齐不齐、项目组合有没有硬伤。

两块独立的事：
  1. 四块模板 —— 技术架构 / 项目描述 / 技术描述 / 项目成果。缺哪块，面试官就从哪块下手问。
  2. 组合诊断 —— 两个项目都是 Agent，就没人能证明你会做后端。同类堆叠 = 能力面窄。

纯规则，0 token，不碰 LLM。**只诊断，不改写**（改写要过 resume_optimize 且必须可回溯）。
"""
from __future__ import annotations

import re

# 四块：key -> (显示名, 触发词)
BLOCKS = [
    ("架构", "技术架构", ("架构", "技术选型", "系统设计", "整体设计", "分层",
                          "docker", "部署", "模块划分", "设计思路")),
    ("项目", "项目描述", ("背景", "目标", "问题", "业务", "场景", "给谁", "解决",
                          "需求", "为什么做")),
    ("技术", "技术描述", ("实现", "流程", "难点", "优化", "机制", "方案", "具体",
                          "核心", "处理")),
    ("成果", "项目成果", ("成果", "效果", "结果", "提升", "降低", "减少", "通过",
                          "实测", "达到", "%", "ms", "qps", "倍")),
]

# 项目类型关键词
KINDS = {
    "agent": ("agent", "智能体", "langgraph", "langchain", "编排", "supervisor",
              "workflow", "工具调用", "多智能体", "replan"),
    "rag": ("rag", "检索增强", "向量", "embedding", "知识库", "召回", "faiss",
            "milvus", "chroma"),
    "aigc": ("aigc", "生图", "出图", "comfyui", "sdxl", "flux", "lora", "扩散",
             "图像生成", "视频生成", "绘画"),
    "backend": ("后端", "接口", "数据库", "并发", "缓存", "消息队列", "微服务",
                "spring", "mysql", "redis", "kafka", "高可用", "限流", "熔断",
                "分布式", "部署上线"),
    "data": ("爬虫", "采集", "抓取", "数据清洗", "etl", "报表", "分析"),
}

_H3 = re.compile(r"^###\s+(.+)$", re.M)
_NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:%|ms|s|万|千|条|个|次|倍|qps|QPS|Qpm)")


def split_projects(text: str) -> list[dict]:
    """按 `### 标题` 切出项目块。没有 ### 就整篇当一个项目。"""
    t = str(text or "")
    marks = [(m.start(), m.group(1).strip()) for m in _H3.finditer(t)]
    if not marks:
        body = t.strip()
        return [{"title": "（整篇）", "body": body}] if body else []
    out = []
    for i, (start, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(t)
        body = t[start:end]
        body = body.split("\n", 1)[1] if "\n" in body else ""
        out.append({"title": title, "body": body.strip()})
    return out


def four_blocks(body: str) -> dict:
    """四块齐全度。成果块额外要求有数字 —— 「性能大幅提升」等于没说。"""
    low = str(body or "").lower()
    got = {}
    for key, label, words in BLOCKS:
        got[key] = any(w in low for w in words)
    # 有成果词但没数字 = 假成果，按缺算
    if got["成果"] and not _NUM.search(str(body or "")):
        got["成果"] = False
        got["成果_有词无数字"] = True
    missing = [label for key, label, _ in BLOCKS if not got.get(key)]
    return {"blocks": got, "missing": missing, "complete": not missing}


def classify(title: str, body: str) -> list[str]:
    """项目主旨按**标题**定，正文只作补充。

    为什么不能正文加权：任何工程项目的正文都会出现「部署 / 接口 / 缓存」，
    按正文算的话每个项目都成了后端项目，「缺后端」的提示永远不会触发 ——
    那诊断就只剩下「都没问题」这个假结论。
    """
    tl = str(title or "").lower()
    strong = [k for k, words in KINDS.items() if any(w in tl for w in words)]
    if strong:
        return strong
    # 标题没线索时看正文，但要求至少命中 2 个词才算
    bl = str(body or "").lower()
    weak = [k for k, words in KINDS.items() if sum(1 for w in words if w in bl) >= 2]
    return weak


AI_KINDS = ("agent", "rag", "aigc")


def portfolio(projects: list[dict]) -> dict:
    """项目组合诊断：AI 方向堆叠 / 缺后端。"""
    rows, counts = [], {}
    for p in projects:
        kinds = classify(p.get("title", ""), p.get("body", ""))
        rows.append({"title": p.get("title", ""), "kinds": kinds})
        for k in kinds:
            counts[k] = counts.get(k, 0) + 1

    tips = []
    ai_n = sum(counts.get(k, 0) for k in AI_KINDS)
    if ai_n >= 2 and not counts.get("backend"):
        tips.append(
            f"{ai_n} 个项目都是 AI 应用方向（{'、'.join(_kind_label(k) for k in AI_KINDS if counts.get(k))}），"
            f"缺一个能把「后端工程能力」证明出来的项目。面试官会怀疑你只会调 API、"
            f"扛不住并发和线上排障。不用新写——把现有项目里服务端那部分单独讲透也算。")
    elif not counts.get("backend") and projects:
        tips.append("没有一个项目能证明后端工程能力（并发、数据库、部署、线上排障）。")
    elif ai_n >= 2:
        tips.append(f"{ai_n} 个项目都在 AI 方向，但后端项目也在，能力面够了。")
    if not tips:
        tips.append("组合看起来没有明显硬伤（至少覆盖了两个不同能力面）。")

    return {"projects": rows, "counts": counts, "tips": tips}


def _kind_label(k: str) -> str:
    return {"agent": "Agent 编排", "rag": "RAG 检索", "aigc": "AIGC",
            "backend": "后端", "data": "数据"}.get(k, k)


def diagnose(resume_text: str) -> dict:
    projects = split_projects(resume_text)
    details = []
    for p in projects:
        fb = four_blocks(p["body"])
        details.append({"title": p["title"], "kinds": classify(p["title"], p["body"]),
                        "missing_blocks": fb["missing"],
                        "fake_result": fb["blocks"].get("成果_有词无数字", False)})
    return {"projects": details, "portfolio": portfolio(projects)}


def render(res: dict) -> str:
    lines = []
    projs = res.get("projects") or []
    lines.append(f"📄 项目结构诊断（{len(projs)} 个项目）")
    for p in projs:
        kinds = "、".join(_kind_label(k) for k in p["kinds"]) or "未归类"
        lines.append("")
        lines.append(f"【{p['title']}】{kinds}")
        if p["missing_blocks"]:
            lines.append(f"  缺这几块：{'、'.join(p['missing_blocks'])}")
            for b in p["missing_blocks"]:
                lines.append(f"    · {b} —— {_block_hint(b)}")
        else:
            lines.append("  四块齐全")
        if p.get("fake_result"):
            lines.append("  ⚠️ 写了效果但没有数字 —— 「性能大幅提升」等于没说，要有实测值")
    pf = res.get("portfolio") or {}
    if pf.get("tips"):
        lines.append("")
        lines.append("🎯 组合诊断：")
        for t in pf["tips"]:
            lines.append(f"  · {t}")
    return "\n".join(lines)


def _block_hint(label: str) -> str:
    return {
        "技术架构": "整体怎么搭的：模块划分 + 关键选型 + 部署形态",
        "项目描述": "解决谁的什么问题，一句话说清业务价值",
        "技术描述": "核心链路怎么实现的，难点在哪",
        "项目成果": "必须有实测数字（延迟/成功率/缺陷数/规模）",
    }.get(label, "")


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="简历结构诊断（四块 + 项目组合）")
    ap.add_argument("path", nargs="?", default=r"C:\面试\Resume.md", help="简历 Markdown 路径")
    args = ap.parse_args(argv)

    try:
        text = open(args.path, "r", encoding="utf-8").read()
    except OSError as exc:
        print(f"❌ 读不到 {args.path}：{exc}")
        return 2
    print(render(diagnose(text)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
