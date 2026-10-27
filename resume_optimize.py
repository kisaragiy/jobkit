#!/usr/bin/env python3
"""简历对齐 JD 优化 —— 对标 GetJob「AI 简历优化」的输出结构，提示词自研。

输出三段（对齐对方交互设计，代码与提示词均自行实现）：
  gaps     岗位要求逐条对照简历的覆盖度：已覆盖 / 弱覆盖 / 未覆盖
  rewrites 逐条改写（仅改写简历已有内容，禁止新增经历与数字）
  warnings 需要人工核对的风险点

成本：DeepSeek flash，单次约 ¥0.01~0.05（实测见 --dry-run 输出）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "deepseek-chat"
ENV_PATH = Path(os.path.expandvars(r"%LOCALAPPDATA%\hermes\.env"))

SYSTEM_PROMPT = """你是资深技术招聘官兼简历顾问，服务对象是中文技术岗求职者。

你的任务：把候选人简历中**已有的**经历，改写成对齐目标岗位 JD 的表述，并指出证据缺口。

铁律（违反即任务失败）：
1. 禁止编造。不得新增简历中不存在的经历、项目、技术栈、公司、时间、数字。
2. 不得为凑关键词而虚构经历。JD 要求但简历没有的（如某框架、某学历、某年限），
   只能标为「未覆盖」或「弱覆盖」，绝不写进改写结果。
3. 改写只做三件事：换用 JD 的术语、把模糊表述具体化、把已有成果量化
   （量化只能用简历里已有的数字；简历没给数字时，用「可量化占位」标注而不是编一个数）。
4. 改写必须可被面试官当面追问而不穿帮——每一条都要能对应回简历原文。
5. 输出简体中文。语气专业、克制，不要营销腔（不用「精通/极大提升/赋能」这类词）。

覆盖度判定标准：
- 已覆盖：简历里有明确的对应证据（项目、技术栈、成果）
- 弱覆盖：提到了但证据不足（只有名词罗列、没有落地描述）
- 未覆盖：简历里完全没有

只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块围栏。格式：
{
  "job_title": "岗位名称（从 JD 推断，没有则填「未标明」）",
  "requirements": [
    {"item": "JD 中的一条要求（保留原意，压缩到 20 字内）",
     "status": "已覆盖|弱覆盖|未覆盖",
     "evidence": "简历中的对应证据原文片段；无则填空字符串",
     "reason": "判定理由，一句话"}
  ],
  "rewrites": [
    {"section": "所属板块，如 项目经历/灵枢",
     "original": "简历原文（必须逐字摘自简历）",
     "rewritten": "改写后表述",
     "why": "改动理由，指向上面的哪条要求"}
  ],
  "warnings": ["需要人工核对或可能被追问的风险点"]
}

requirements 覆盖 JD 的全部硬性要求（技能、经验、学历、职责关键词），一般 6-15 条。
rewrites 只包含真正值得改的条目，没有就返回空数组。宁可少改，不要硬凑。"""

USER_TEMPLATE = """# 目标岗位
{job_title}

# 职位描述（JD）
{jd}

# 候选人简历全文
{resume}

请按要求输出 JSON。"""


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """极简 .env 读取，不依赖第三方库。"""
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _call_cloud(messages: list[dict], *, model: str, api_key: str, base_url: str,
                temperature: float = 0.3, max_tokens: int = 8192,
                timeout: int = 180) -> tuple[str, dict]:
    """OpenAI 兼容接口，纯 stdlib。返回 (content, usage)。"""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_err = None
    for attempt in range(3):
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"], body.get("usage", {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                last_err = f"HTTP {e.code}: {detail}"
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"API 错误 HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = repr(e)
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"网络错误（已重试 3 次）: {last_err}") from e
    raise RuntimeError(f"请求失败: {last_err}")


def call_llm(messages: list[dict], *, model: str, api_key: str, base_url: str,
             temperature: float = 0.3, max_tokens: int = 8192,
             timeout: int = 180, allow_local_fallback: bool = True,
             local_messages: list[dict] | None = None) -> tuple[str, dict]:
    """**唯一的 LLM 入口** —— 云端优先，失败自动回退本地 ollama。

    为什么兜底放这里：greet_generator / ai_tools / resume_optimize 全都调这个函数，
    一处加兜底全线受益。

    为什么需要兜底：DeepSeek 余额说没就没 —— 实测 2026-09-17 只剩 ¥1.71、
    2026-09-20 又见 HTTP 402 Insufficient Balance，当天招呼语 5 条全挂。

    ：local_messages  可选。**本地兜底时用的另一套 prompt**。
    为什么需要它（2026-09-21 血教训）：本地小模型（qwen3.5:9b）遵循不了
    "输出严格 JSON" 这种复杂格式约束 —— 实测它吐半截 JSON、还把
    「您的 JD 要求…」这种生硬措辞写进招呼语。让它只输出一句话，质量好得多。
    调用方可以把「云端用的 JSON prompt」和「本地用的纯文本 prompt」分开传。

    返回的 usage 里 `provider` 是 `cloud` 或 `local`。
    """
    cloud_err = ""
    if api_key:
        try:
            content, usage = _call_cloud(messages, model=model, api_key=api_key,
                                         base_url=base_url, temperature=temperature,
                                         max_tokens=max_tokens, timeout=timeout)
            if content.strip():
                usage = usage or {}
                usage.setdefault("provider", "cloud")
                return content, usage
            cloud_err = "云端返回空内容"
        except Exception as e:
            cloud_err = str(e)[:160]
    else:
        cloud_err = "没配 API key"

    if not allow_local_fallback:
        raise RuntimeError(f"云端调用失败：{cloud_err}")

    # ── 本地 ollama 兜底（0 成本）──
    try:
        import llm_local as L
    except Exception as e:
        raise RuntimeError(f"云端失败（{cloud_err}）；且 llm_local 不可用（{e}）") from e

    ok, msg = L.ensure_started()
    if not ok:
        raise RuntimeError(f"云端失败（{cloud_err}）；本地模型也起不来（{msg}）")

    base = L.base_url()
    mdl = L.pick_model(L.list_models(base))
    use_msgs = local_messages if local_messages else messages
    content, meta = L.chat(use_msgs, mdl, temperature=temperature,
                           num_predict=min(max_tokens, 2000))
    if not content.strip():
        raise RuntimeError(
            f"云端失败（{cloud_err}）；本地 {mdl} 返回空 —— "
            f"检查是否漏了 think:false（思考型模型会吃光预算）"
        )
    out = dict(meta)
    out["provider"] = "local"
    out["fallback_reason"] = cloud_err
    out["used_local_prompt"] = bool(local_messages)
    return content, out


def extract_json(text: str) -> dict:
    """容错解析：去掉代码块围栏，截取首个平衡的 JSON 对象。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("模型输出里找不到 JSON 对象")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("模型输出 JSON 不完整")


def estimate_cost(usage: dict, model: str = DEFAULT_MODEL) -> str:
    """按 DeepSeek 官方价（峰值）估算，仅作参考。"""
    if not usage:
        return "未知"
    pin, pout = 0.30, 1.20  # $ / 1M tokens, 峰值
    cost = usage.get("prompt_tokens", 0) / 1e6 * pin + usage.get("completion_tokens", 0) / 1e6 * pout
    return f"≈${cost:.4f}（≈¥{cost * 7.1:.3f}）"


STATUS_ORDER = {"未覆盖": 0, "弱覆盖": 1, "已覆盖": 2}


def render(result: dict) -> str:
    out: list[str] = []
    out.append("═" * 62)
    out.append(f"岗位：{result.get('job_title', '未标明')}")
    out.append("═" * 62)

    reqs = result.get("requirements") or []
    if reqs:
        counts: dict[str, int] = {}
        for r in reqs:
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: STATUS_ORDER.get(x[0], 9)))
        out.append(f"\n■ 覆盖度总览（{len(reqs)} 条要求）：{summary}\n")
        icon = {"已覆盖": "✅", "弱覆盖": "⚠️", "未覆盖": "❌"}
        for r in sorted(reqs, key=lambda x: STATUS_ORDER.get(x.get("status", ""), 9)):
            out.append(f"{icon.get(r.get('status'), '•')} [{r.get('status')}] {r.get('item')}")
            if r.get("reason"):
                out.append(f"     └ {r['reason']}")
            if r.get("evidence"):
                out.append(f"       证据：{r['evidence'][:110]}")
    else:
        out.append("\n⚠️ 模型未给出分类缺口，请对照 JD 自行取舍。")

    rw = result.get("rewrites") or []
    out.append(f"\n■ 逐条改写（{len(rw)} 条）")
    if not rw:
        out.append("  （无：简历已足够对齐，或缺口属于不可改写项）")
    for i, r in enumerate(rw, 1):
        out.append(f"\n  [{i}] {r.get('section', '')}")
        out.append(f"      原文：{r.get('original', '')[:160]}")
        out.append(f"      改写：{r.get('rewritten', '')[:220]}")
        out.append(f"      理由：{r.get('why', '')}")

    warns = result.get("warnings") or []
    if warns:
        out.append(f"\n■ 人工核对提醒（{len(warns)} 条）")
        for w in warns:
            out.append(f"  ⚠️ {w}")
    out.append("\n※ 请逐条人工核对：改写必须能对应回你的真实经历，编造内容会在面试当场穿帮。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="简历对齐 JD 优化（自建，非 GetJob）")
    ap.add_argument("--jd", help="JD 文件路径")
    ap.add_argument("--jd-text", help="JD 正文（短文本直接传）")
    ap.add_argument("--resume", default=r"C:\面试\Resume.md", help="简历文件路径")
    ap.add_argument("--title", help="目标岗位名称（选填，不给则从 JD 推断）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument("--dry-run", action="store_true", help="只打印提示词长度与预估，不调用 API")
    args = ap.parse_args()

    if args.jd_text:
        jd = args.jd_text
    elif args.jd:
        jd = Path(args.jd).read_text(encoding="utf-8", errors="replace")
    else:
        ap.error("必须提供 --jd 或 --jd-text")

    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"❌ 简历文件不存在: {resume_path}", file=sys.stderr)
        return 2
    resume = resume_path.read_text(encoding="utf-8", errors="replace")

    if len(resume) < 120:
        print(f"❌ 简历太短（{len(resume)} 字），至少 120 字", file=sys.stderr)
        return 2
    if len(jd) < 40:
        print(f"❌ JD 太短（{len(jd)} 字），至少 40 字", file=sys.stderr)
        return 2

    prompt = USER_TEMPLATE.format(job_title=args.title or "（未提供，请从 JD 推断）", jd=jd, resume=resume)

    if args.dry_run:
        print(f"简历 {len(resume)} 字 / JD {len(jd)} 字 / 提示词 {len(prompt) + len(SYSTEM_PROMPT)} 字")
        print(f"模型 {args.model}，预估单次成本 < ¥0.05")
        return 0

    env = load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY")
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    if not api_key:
        print("❌ 未找到 DEEPSEEK_API_KEY（检查 hermes/.env）", file=sys.stderr)
        return 2

    t0 = time.time()
    content, usage = call_llm(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
        model=args.model, api_key=api_key, base_url=base_url,
    )
    elapsed = time.time() - t0

    try:
        result = extract_json(content)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"❌ 解析模型输出失败: {e}", file=sys.stderr)
        print("--- 原始输出 ---", file=sys.stderr)
        print(content[:1500], file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
        print(f"\n[耗时 {elapsed:.1f}s · tokens {usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')} · 成本 {estimate_cost(usage)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
