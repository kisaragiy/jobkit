#!/usr/bin/env python3
"""按 JD 定制打招呼语 —— 只引用简历里真实存在的经历。

为什么不用模板：群发模板是「500+ 沟通 0 面试」的主因。HR 只看第一句，
第一句必须说明"你为什么匹配这个岗"，而不是"我很感兴趣"。

硬约束（与 resume_optimize 一致的反编造纪律）：
  · 只能引用简历中真实存在的项目、技术栈、数字
  · 禁止"热爱学习/积极进取/希望能有机会"这类空话
  · 每条招呼语必须能让面试官追问而不穿帮

同时输出 matches（匹配依据），方便你逐条核对后再发。

用法：
    python greet_generator.py --jd jd.txt
    python greet_generator.py --jd jd.txt --company "某科技" --title "AI应用开发工程师"
    python greet_generator.py --jd jd.txt --count 3 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from resume_optimize import (  # noqa: E402
    DEFAULT_MODEL, call_llm, estimate_cost, extract_json, load_env,
)

SYSTEM_PROMPT = """你是求职者的私人求职顾问，帮他写发往 BOSS 直聘的打招呼消息。

写之前先在心里做三件事：
1. 从 JD 里挑出 2-3 个最核心、最区分度高的技术要求（不是公司福利、不是泛泛的职责）
2. 在候选人简历里找能对应上的**真实**项目证据
3. 用一条消息把"证据 → 岗位需求"这条线说清楚

消息规则：
- 第一句直接给匹配点。禁止任何寒暄开场（"您好，看到贵公司在招聘…"这类直接算失败）。
- 必须出现具体技术名词（如 LangGraph / MCP / FastAPI / LoRA），不要写"AI 应用开发"这种泛称。
- 长度 100-150 字。**这是硬限制**，超出会被压缩：HR 不会读完长消息。一段话，不要分点、不要 emoji、不要礼貌套话堆砌。
- 结尾给一个明确的下一步动作（如"方便发份简历给您看看吗"）。
- 语气：同行对同行的平实口吻。不要"贵司/鄙人"这种过度客气，也不要自来熟。

铁律（违反即失败）：
1. 只允许引用简历里真实存在的经历、技术栈、数字。简历里没有的技术一个字都不许写。
2. 简历里没有数字的地方，不要编造数字。
3. 不知道公司做什么就不要装作知道，不要编公司业务。

只输出 JSON，不要解释文字、不要 markdown 围栏。格式：
{
  "primary": "主推的那条招呼语",
  "alternates": ["备选招呼语1", "备选招呼语2"],
  "matches": [
    {"jd_need": "JD 里的一条核心要求",
     "your_evidence": "简历里对应的真实证据（逐字或紧邻概括）",
     "used_in_message": true}
  ],
  "avoid": ["这条 JD 你简历撑不住、消息里绝对不要提的点"]
}

matches 按重要性排序，3-6 条。alternates 数量按要求给。"""

USER_TEMPLATE = """# 目标岗位
公司：{company}
岗位：{title}

# 职位描述（JD）
{jd}

# 候选人简历全文
{resume}

请写 {count} 条打招呼语（primary 1 条 + alternates {alt_count} 条），并给出匹配依据。"""


LOCAL_SIMPLE_SYSTEM = (
    "你是求职者的朋友，帮他在招聘 App 上给 HR 发一条打招呼的消息。"
    "说话要像真人聊天，不要像写作文或背简历。"
)

LOCAL_SIMPLE_TEMPLATE = """帮我把下面这条打招呼的消息写出来。

【绝对禁止出现的词】（出现就作废）：
贵司 / 贵公司 / 您们 / 尊敬的 / 您的JD / 您的要求 / 快发 / 赶紧发 / 能否入职

必须做到：
1. 只输出这一句话本身 —— 不要 JSON、不要引号包裹、不要标题、不要编号、不要任何解释
2. 100 到 150 个字
3. 开头用「您好」，紧接着就说清楚我为什么匹配这个岗位
4. 只能提我简历里真实写过的东西，不许编造经历、年限、学历、公司
5. 像真人聊天。要称呼对方就直接说「您」，别用「贵司」这种老派书面语
6. 结尾问一句能不能发简历（用「方便发份简历给您看看吗」这类自然说法）

岗位信息：
{jd}

我的简历：
{resume}

现在直接输出那一句话："""


def generate(jd: str, resume: str, *, company: str = "（未提供）",
             title: str = "（未提供，从 JD 推断）", count: int = 3,
             model: str = DEFAULT_MODEL, api_key: str = "", base_url: str = "",
             temperature: float = 0.7, allow_local_fallback: bool = True
             ) -> tuple[dict, dict]:
    """生成招呼语。返回 (result, usage)。供 CLI 与投递流程复用。

    **两条 prompt 路径**（2026-09-21 血教训）：
      云端：要 JSON（含 alternates/matches），DeepSeek 能稳定遵循
      本地：**只要一句话**。小模型遵循不了复杂格式约束 ——
            实测让它出 JSON 会吐半截、措辞失控（写「您的 JD 要求…」）。
    本地/云端兜底交给 `call_llm`（唯一 LLM 入口）。
    """
    prompt = USER_TEMPLATE.format(
        company=company, title=title, jd=jd, resume=resume,
        count=max(1, count), alt_count=max(0, count - 1),
    )
    local_prompt = LOCAL_SIMPLE_TEMPLATE.format(
        company=company, title=title, jd=jd[:2000], resume=resume[:3000],
    )

    content, usage = call_llm(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
        model=model, api_key=api_key, base_url=base_url,
        temperature=temperature, allow_local_fallback=allow_local_fallback,
        local_messages=[{"role": "system", "content": LOCAL_SIMPLE_SYSTEM},
                        {"role": "user", "content": local_prompt}],
    )

    if usage.get("used_local_prompt"):
        # 本地路径输出的是**纯文本**（不是 JSON），直接当正文
        result = {"primary": content.strip(), "alternates": [], "matches": [],
                  "source": "local-plaintext"}
    else:
        # ⛔ 解析失败必须视为失败，**绝不把原文当招呼语**。
        # 血教训（2026-09-21）：原实现把 content[:190] 当 primary ——
        # 实测本地小模型吐半截 JSON，整段 '{"primary": "...", "alternates": [' 差点发出去。
        try:
            result = extract_json(content)
        except Exception as e:
            raise RuntimeError(
                f"模型输出不是合法 JSON，已拒绝（不发原文）：{type(e).__name__}: {e}｜"
                f"输出前 80 字：{content.strip()[:80]!r}"
            ) from e

    # ── 发送前质量门禁：不合格就不发 ──
    # 先做安全替换（贵司→公司），再做硬门禁 —— 能改的不整条扔
    primary = soft_fix(str(result.get("primary") or "").strip())
    ok, why = validate_greeting(primary)

    alts_raw = result.get("alternates") or []
    alts = [soft_fix(str(a).strip()) for a in alts_raw]
    alts = [a for a in alts if validate_greeting(a)[0]]

    if not ok:
        if alts:
            # 主推不合格但有合格备选 → 备选顶上
            result["primary"] = alts[0]
            result["alternates"] = alts[1:]
            usage["primary_replaced"] = f"备选顶上（主推不合格：{why}）"
        else:
            raise RuntimeError(
                f"招呼语质检不通过，已拒绝发送（宁可这条不发也不发垃圾）：{why}｜"
                f"正文：{primary[:90]!r}"
            )
    else:
        result["alternates"] = alts
    return result, usage


# ══════════════════════════════════════════════════════════════
# 招呼语质量门禁（发送前必须过）
# ══════════════════════════════════════════════════════════════
#
# ⛔ 为什么要这道闸（2026-09-21 用户当场制止）：
# 本地小模型生成的招呼语出现过这些问题 ——
#   · 「您的 JD 要求…」   —— 把 HR 的 JD 说成「您的 JD」，像在考核对方，极生硬
#   · 「快发简历看看能否入职？」 —— 「快」像命令 HR；未面试就问入职，轻浮
#   · 整段 JSON 残留（见 generate 的解析失败处理）
# 发出去是直接社死。所以**宁可这条不发，也不能发垃圾**。
_BANNED_PATTERNS = [
    (r"您的\s*(JD|岗位|职位|要求|需求)", "「您的 JD/要求」——像在考核 HR"),
    (r"贵公司|贵司|尊敬的", "对 BOSS 直聘场景过于书面，不自然"),
    (r"快发|速发|赶紧发", "命令语气"),
    (r"能否入职|可以入职|什么时候入职", "还没面试就谈入职，轻浮"),
    (r"您们", "「您们」是错的（您已含复数敬称）"),
    (r"[{[\]}]\s*[\"']?(primary|alternates|matches|jd_need)", "JSON 结构残留"),
    (r"^\s*[{\[]", "以 JSON/数组开头"),
    (r"```", "markdown 代码围栏残留"),
    (r"作为(一个)?(AI|人工智能|语言模型)", "暴露 AI 身份"),
]

# 能安全改的就改，别整条扔掉。
# 「贵司」→「公司」语义完全不变，但整条丢弃等于浪费一次投递额度，
# 还退回到质量更差的回退模板（2026-10-21 实测：14 条投递里 5 条被整条拒掉）。
_SOFT_FIXES = (
    (re.compile(r"贵公司|贵司"), "公司"),
)

# 第一人称，或中文里省略主语的自述动词（「做过/用过/搭过」= 「我做过」）
_SELF_REF = re.compile(
    r"我|自己|(?:做过|写过|搭过|实现过|落地过|负责过|主导过|参与过|开发过|部署过|跑过|调过|用过)")


def soft_fix(text: str) -> str:
    """发送前把可安全替换的措辞改掉（不做整条拒绝）。"""
    t = str(text or "")
    for pat, rep in _SOFT_FIXES:
        t = pat.sub(rep, t)
    return t


def validate_greeting(text: str) -> tuple[bool, str]:
    """招呼语发送前质检。返回 (是否合格, 原因)。

    只做能机械判定的检查 —— 不评「写得好不好」（那是审美），
    只拦「发出去会出事」的（禁词/结构残留/长度失控/没有问候）。
    """
    t = str(text or "").strip()
    if not t:
        return False, "空内容"
    if len(t) < 25:
        return False, f"太短（{len(t)} 字），像没写完"
    if len(t) > 220:
        return False, f"太长（{len(t)} 字），HR 不会读完"

    for pat, why in _BANNED_PATTERNS:
        m = re.search(pat, t, re.I | re.M)
        if m:
            return False, f"{why}（命中 {m.group(0)[:20]!r}）"

    # 得是求职者本人说的话。
    # ⚠️ 不能要求"必须有您好" —— SYSTEM_PROMPT 明确禁止寒暄开场（要求第一句直接给匹配点），
    # 两类 prompt 规则打架会让合格输出被误杀。
    # ⚠️ 也不能只认「我」—— 中文口语省略主语是常态：「用 LangGraph 做过…」=「我用 LangGraph 做过…」。
    # 只认「我」会把最自然的一类好话术全部误杀（2026-10-21 实测：14 条里 4 条被这条规则拒掉，
    # 退回到没有任何技术证据的回退模板）。所以判据 = 第一人称 **或** 已完成体自述动词。
    if not _SELF_REF.search(t):
        return False, "没有人称 —— 读起来不像求职者本人说的话"

    # 不能是纯符号/纯英文。
    # ⚠️ 用「中文字符总数」而不是「连续 10 个中文」——技术话术必然中英混排
    # （LangGraph / Supervisor / pytest / SSE / fail-closed），连续段会被术语切断。
    if len(re.findall(r"[\u4e00-\u9fa5]", t)) < 12:
        return False, "中文太少"

    return True, ""


def resolve_key(model: str = DEFAULT_MODEL) -> tuple[str, str]:
    env = load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY") or ""
    base_url = (os.environ.get("DEEPSEEK_BASE_URL") or env.get("DEEPSEEK_BASE_URL")
                or "https://api.deepseek.com")
    return api_key, base_url


def render(result: dict) -> str:
    out = ["═" * 62, "打招呼语（可直接复制发送）", "═" * 62]
    primary = result.get("primary", "").strip()
    out.append(f"\n★ 主推（{len(primary)} 字）\n")
    out.append(f"  {primary}")

    alts = result.get("alternates") or []
    if alts:
        out.append(f"\n■ 备选（{len(alts)} 条）")
        for i, a in enumerate(alts, 1):
            out.append(f"\n  [{i}] {len(a.strip())} 字")
            out.append(f"      {a.strip()}")

    matches = result.get("matches") or []
    if matches:
        out.append(f"\n■ 匹配依据（发之前逐条核对）")
        for m in matches:
            flag = "✓" if m.get("used_in_message") else "·"
            out.append(f"  {flag} JD：{m.get('jd_need', '')}")
            out.append(f"      你：{m.get('your_evidence', '')}")

    avoid = result.get("avoid") or []
    if avoid:
        out.append(f"\n■ 千万不要提（简历撑不住，提了就被追问穿帮）")
        for a in avoid:
            out.append(f"  ❌ {a}")

    out.append("\n※ 发之前人工核对：消息里每一个技术名词都要能在你简历里找到出处。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="按 JD 定制打招呼语（自建，非 GetJob）")
    ap.add_argument("--jd", help="JD 文件路径")
    ap.add_argument("--jd-text", help="JD 正文")
    ap.add_argument("--resume", default=r"C:\面试\Resume.md")
    ap.add_argument("--company", default="（未提供）")
    ap.add_argument("--title", default="（未提供，从 JD 推断）")
    ap.add_argument("--count", type=int, default=3, help="总共要几条，默认 3")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.jd_text:
        jd = args.jd_text
    elif args.jd:
        jd = Path(args.jd).read_text(encoding="utf-8", errors="replace")
    else:
        ap.error("必须提供 --jd 或 --jd-text")

    rp = Path(args.resume)
    if not rp.exists():
        print(f"❌ 简历文件不存在: {rp}", file=sys.stderr)
        return 2
    resume = rp.read_text(encoding="utf-8", errors="replace")

    env = load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY")
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    if not api_key:
        print("❌ 未找到 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    prompt = USER_TEMPLATE.format(
        company=args.company, title=args.title, jd=jd, resume=resume,
        count=max(1, args.count), alt_count=max(0, args.count - 1),
    )

    t0 = time.time()
    content, usage = call_llm(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
        model=args.model, api_key=api_key, base_url=base_url, temperature=0.7,
    )
    elapsed = time.time() - t0

    try:
        result = extract_json(content)
    except Exception as e:
        print(f"❌ 解析失败: {e}", file=sys.stderr)
        print(content[:1200], file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
        print(f"\n[耗时 {elapsed:.1f}s · 成本 {estimate_cost(usage)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
