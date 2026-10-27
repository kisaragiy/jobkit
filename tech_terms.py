"""技术词表（打招呼语评分、简历诊断共用）。

为什么要单独一个模块
--------------------
一开始词表写在 greeting_kit 里，简历诊断又要用同一套。复制一份必然漂移
（改一处忘另一处），所以抽出来做单点。

注意：匹配一律大小写不敏感；中文词按子串匹配即可（「多 Agent」「多agent」
都要能命中，所以同时存两种写法没必要，统一 lower 后比较）。
"""
from __future__ import annotations

import re

# 分类词表：key 是类别名，value 是词列表。
# 加词原则：**JD 里真的会出现的词**。太泛的（"编程""开发"）不加，会把覆盖率
# 算虚高，反而掩盖真实缺口。
TERMS: dict[str, tuple[str, ...]] = {
    "语言": (
        "python", "java", "golang", "go", "c++", "c#", "php", "rust",
        "javascript", "typescript", "sql", "shell", "scala", "kotlin",
    ),
    "后端框架": (
        "fastapi", "django", "flask", "tornado", "spring", "springboot",
        "spring boot", "mybatis", "express", "nestjs", "grpc", "restful",
    ),
    "AI/LLM": (
        "langchain", "langgraph", "llamaindex", "rag", "agent", "multi-agent",
        "多agent", "多 agent", "llm", "prompt", "提示词", "embedding", "向量",
        "rerank", "微调", "fine-tune", "lora", "qlora", "peft", "vllm",
        "ollama", "llama", "qwen", "gpt", "transformer", "transformers",
        "pytorch", "tensorflow", "模型部署", "推理加速", "量化", "知识库",
    ),
    "AIGC": (
        "comfyui", "sdxl", "stable diffusion", "flux", "midjourney",
        "diffusion", "controlnet", "lora训练", "文生图", "文生视频",
        "图生视频", "ai绘画", "aigc",
    ),
    "数据存储": (
        "mysql", "postgresql", "postgres", "redis", "mongodb", "sqlite",
        "elasticsearch", "es", "kafka", "rabbitmq", "clickhouse", "hbase",
    ),
    "运维/工程": (
        "docker", "kubernetes", "k8s", "nginx", "linux", "git", "ci/cd",
        "jenkins", "prometheus", "grafana", "日志", "监控", "单元测试",
        "pytest", "unittest",
    ),
    "前端": (
        "react", "vue", "angular", "html", "css", "webpack", "vite",
        "小程序", "flutter", "electron",
    ),
    "架构/软技能": (
        "微服务", "分布式", "高并发", "消息队列", "缓存", "性能优化",
        "爬虫", "scrapy", "数据分析", "数据清洗", "etl", "api设计",
        "系统设计", "代码review", "敏捷",
    ),
}

CATEGORIES = tuple(TERMS.keys())

# 扁平化后的全量词表（长的在前，避免短词先命中造成误判）
ALL_TERMS: tuple[str, ...] = tuple(sorted(
    {t for terms in TERMS.values() for t in terms}, key=len, reverse=True))


def _has(low: str, term: str) -> bool:
    """term 是否出现在 low 里。

    所有英文词都用**字母边界**匹配，不是只对短词：
    - `java` ⊂ `javascript` → 后面接字母，不算命中
    - `sql` ⊂ `mysql` → 前面接字母，不算命中
    - `go` ⊂ `golang` → 不算（`golang` 自己会命中）
    - `es` ⊂ `express` → 不算

    但**数字不算边界**：`python3`、`go1.20`、`mysql8` 里前面的词要能命中。
    所以用 `(?<![a-z])…(?![a-z])` 而不是 `\\b` 或 `[a-z0-9]`。

    中文词没有词边界概念，仍按子串匹配（「多Agent」「多agent」都要命中）。
    """
    if not term.isascii():
        return term in low
    pat = _BOUND.get(term)
    if pat is None:
        pat = re.compile(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])")
        _BOUND[term] = pat
    return bool(pat.search(low))


_BOUND: dict[str, re.Pattern] = {}


def find_terms(text: str) -> list[str]:
    """返回 text 里命中的技术词（已 lower，去重，保持词表顺序稳定）。"""
    low = (text or "").lower()
    return [t for t in ALL_TERMS if _has(low, t)]


def by_category(text: str) -> dict[str, list[str]]:
    """按类别分组命中。用于报告「你缺 AI 类」这种结论。"""
    low = (text or "").lower()
    out = {}
    for cat, terms in TERMS.items():
        got = [t for t in terms if _has(low, t)]
        if got:
            out[cat] = got
    return out
