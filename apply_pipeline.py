#!/usr/bin/env python3
"""批量投递流程 —— 搜索 → 过滤 → 去重 → 定制招呼语 → 按安全节奏发送。

安全策略（来自实测：8-20s 间隔连发 12 个即触发 24h 封禁）：
  · 每个招呼间隔 60-120 秒（随机，模拟真人）
  · 每 5 个休息 10-15 分钟
  · 每日上限 130
  · 熔断：命中 RATE_LIMITED / ACCOUNT_RISK / AUTH_EXPIRED / FORBIDDEN / GREET_LIMIT
    或连续 5 次失败 → 立即停止，不重试

用法：
    python apply_pipeline.py --dry-run --limit 5    # 预览（不发送，不需要登录）
    python apply_pipeline.py --limit 10             # 真实发送（需要登录态）
    python apply_pipeline.py --limit 10 --interval 90 110
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import filters as F  # noqa: E402
import greet_generator as gg  # noqa: E402
import pace as P  # noqa: E402
import rules  # noqa: E402
import settings as settings_mod  # noqa: E402
import store  # noqa: E402

CLI_DIR = Path(r"C:\Users\<user>\boss-agent-cli-dev")
DEFAULT_RESUME = r"C:\面试\Resume.md"

# 熔断信号：出现任一即停手（不重试）
BREAKER_CODES = ("RATE_LIMITED", "ACCOUNT_RISK", "AUTH_EXPIRED", "FORBIDDEN", "GREET_LIMIT",
                 "COMPLIANCE_BLOCKED", "OUTCOME_UNKNOWN", "KERNEL_REQUIRED")
RISK_HINTS = ("风控拦截", "账号异常", "登录已过期", "操作过于频繁", "请求过于频繁")

# 写操作遇到这些错误时无法判断远端是否接收；保留分类接口，但不据此自动重发。
TRANSIENT_CODES = ("NETWORK_ERROR", "TIMEOUT", "ECONNRESET", "HTTP_5", "SERVER_ERROR")
RETRY_DELAY = 8          # 秒；退避一次就够，多了反而像脚本在刷

# 用岗位摘要（非完整 JD）生成时，招呼语的硬上限。提示词里会要求模型压到这个数，
# 但模型经常超（实测出现过 209/215 字），所以再加一道确定性截断兜底。
MAX_BRIEF_MESSAGE_CHARS = 150


def shorten_message(msg: str, limit: int = 190) -> str:
    """确定性截断：超长时保留首句（匹配证据）+ 末句（行动号召），丢掉中间。

    提示词约束不可靠（模型会超），这层是硬保证 —— 不依赖模型听话。
    """
    msg = (msg or "").strip()
    if len(msg) <= limit:
        return msg
    parts = [p.strip() for p in re.split(r"(?<=[。！？])", msg) if p.strip()]
    if len(parts) < 3:
        return msg[:limit]
    head, closing = parts[0], parts[-1]
    if len(head) + len(closing) > limit:
        return msg[:limit]
    out = head
    for p in parts[1:-1]:          # 中间句按重要性未知，能塞就塞
        if len(out) + len(p) + len(closing) <= limit:
            out += p
        else:
            break
    return out + closing


def boss_cmd() -> list[str]:
    if (CLI_DIR / "pyproject.toml").exists() and shutil.which("uv"):
        return ["uv", "run", "--directory", str(CLI_DIR), "boss"]
    exe = shutil.which("boss")
    if exe:
        return [exe]
    raise RuntimeError("找不到 boss 命令")


def clean_env() -> dict:
    import os
    env = os.environ.copy()
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = "localhost,127.0.0.1"
    return env


def run_boss(args: list[str], timeout: int = 120) -> tuple[bool, str]:
    try:
        p = subprocess.run(boss_cmd() + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=clean_env(), cwd=str(CLI_DIR))
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT: boss {' '.join(args)}"


BRIDGE_STATUS_URL = "http://127.0.0.1:19826/status"
CDP_VERSION_URL = "http://127.0.0.1:9222/json/version"


def browser_channel() -> tuple[str, str]:
    """判断当前会走哪条浏览器通道，返回 (通道, 说明)。

    背景（2026-09-19 账户风控事故）：boss-agent-cli 的降级链是
        Bridge（扩展+daemon）→ CDP 连用户浏览器 → headless patchright 兜底
    前两条走**真实浏览器上下文**；兜底那条是 headless Chromium + 伪造 macOS UA，
    是最容易被识别的自动化特征。账户风控就是这么触发的。

    结论：投递这类写操作**只允许**走真实浏览器通道，落到 headless 一律拒绝。
    这条限制在代码层，不依赖人的自觉。
    """
    import json as _json
    import urllib.request as _u

    try:
        with _u.urlopen(BRIDGE_STATUS_URL, timeout=5) as r:
            d = _json.loads(r.read().decode("utf-8"))
            if d.get("ok") and d.get("extensionConnected"):
                return "bridge", f"Bridge 扩展已连接（v{d.get('extensionVersion') or '?'}）"
            if d.get("ok"):
                return "bridge_no_ext", "Bridge daemon 在跑，但浏览器扩展未连接"
    except Exception:
        pass

    try:
        with _u.urlopen(CDP_VERSION_URL, timeout=3) as r:
            _json.loads(r.read().decode("utf-8"))
            return "cdp", "CDP 真实浏览器可用"
    except Exception:
        pass

    return "headless", "Bridge 与 CDP 均不可用 → 会降级到 headless"


def delivery_channel_ok() -> tuple[bool, str, str]:
    """投递前的通道门禁。返回 (是否放行, 通道, 说明)。"""
    ch, why = browser_channel()
    return ch in ("bridge", "cdp"), ch, why


def login_ready() -> tuple[bool, str]:
    """投递前置检查。返回 (是否放行, 说明)。

    两级校验：
      ① 标志位：认证状态必须健康，且**硬性缺失**（fail/error/missing）一票否决
      ② 真实请求：boss status --live 必须在超时内返回，超时/报错判为不可用

    ⚠️ warn 不能一票否决 —— 这是本函数最大的坑，踩过一次：
    `stoken_freshness` 的 status 是 warn（detail 写"session 文件约 284.9 小时
    前更新；stoken **可能**已过期"），那是**推测**不是实证。旧代码把任何 warn
    都算缺失，于是界面顶着红色「未登录/受限」，而采集任务明明在正常跑
    （633 个岗位在库里）—— 用户看到的状态和软件的实际能力相反，还不知道该
    信哪个。

    正确做法：推测交给 ② 的实证去判。真实请求过了就是能用，把 warn 附在
    说明里当提示；真实请求挂了，warn 才有诊断价值。
    """
    _, out = run_boss(["status"], timeout=60)
    i = out.find("{")
    if i < 0:
        return False, "无法读取登录态"
    try:
        d = json.loads(out[i:out.rfind("}") + 1])
    except json.JSONDecodeError:
        return False, "登录态输出解析失败"
    data = d.get("data", {})
    checks = {c.get("name"): c for c in (data.get("checks") or [])}
    hard = [k for k, v in checks.items()
            if v.get("status") in ("fail", "error", "missing")]
    soft = [k for k, v in checks.items() if v.get("status") == "warn"]
    st = data.get("auth_state", "unknown")
    # 实测 boss-agent-cli 1.15 的健康状态值是 "complete"（配 auth_summary: healthy），
    # 不是 "ok"/"full" —— 白名单必须覆盖真实取值，否则会误判
    if st not in ("ok", "full", "complete", "healthy") or hard:
        return False, f"登录态 {st}，缺失 {', '.join(hard) or '—'}（先跑 python boss_login.py）"

    # ② 真实请求验证：标志位全绿不代表能发请求
    ok, live = run_boss(["status", "--live"], timeout=75)
    if not ok or "TIMEOUT" in live[:20]:
        why = ("标志位正常但真实请求无响应（HTTP 通道卡死）——"
               "浏览器通道没有可用会话，请重新登录")
        if soft:
            why += f"（另：{', '.join(soft)}）"
        return False, why
    if '"ok": false' in live.replace(" ", ""):
        return False, f"真实请求返回失败：{live.strip()[:160]}"

    # 通过。推测性提示带出去，让界面能说"能用，但快该换 token 了"
    if soft:
        return True, "可用（提示：" + ", ".join(soft) + "）"
    return True, "ok"


def today_sent_count() -> int:
    c = store.connect()
    n = c.execute(
        "SELECT COUNT(*) FROM greetings WHERE status='sent' AND date(greeted_at,'unixepoch','localtime') "
        "= date('now','localtime')"
    ).fetchone()[0]
    c.close()
    return n


def build_brief(job: dict) -> tuple[str, str]:
    """把岗位字段拼成一段"类 JD"文本，供招呼语生成使用。"""
    parts = [f"公司：{job.get('company') or '未知'}", f"岗位：{job.get('title') or '未知'}"]
    for label, key in (("城市", "city"), ("薪资", "salary"), ("学历", "education"),
                       ("经验", "experience"), ("行业", "industry"), ("规模", "scale")):
        if job.get(key):
            parts.append(f"{label}：{job[key]}")
    skills = (job.get("skills") or "").strip()
    if skills:
        parts.append(f"技能要求：{skills}")
    parts.append(
        "\n（注意：以上是岗位的摘要信息，不是完整 JD 正文；只依据上面的信息写，不要臆测岗位职责。"
        "信息量有限时**不要罗列多个项目**，只挑 1 个最相关的，"
        f"招呼语控制在 {MAX_BRIEF_MESSAGE_CHARS} 字以内。）"
    )
    return "\n".join(parts), (job.get("title") or "")


def extract_message(result: dict) -> str:
    return (result.get("primary") or "").strip()


def _enrich_security_id(job_id: str) -> str:
    """缺 security_id 时进详情页把它补回来。

    为什么需要：boss-agent-cli 的 greet 签名要求 securityId，但列表页返回的
    卡片链接不一定带（DOM 通道尤其）。详情页 URL 本身带 ?securityId=，
    所以进一次详情页就能补上 —— 这是 jobkit 必须自造的能力。
    GetJob 没有这个需求（它在 SPA 内点按钮，不拼 token），所以不能拿它当参照。

    失败一律返回空串（由调用方计数告警），绝不猜、不编 token。
    """
    jid = str(job_id or "").strip()
    if not jid:
        return ""
    try:
        import dom_channel as DC      # 延迟导入：不走 enrich 的路径不付这个代价
    except Exception:
        return ""
    url = f"https://www.zhipin.com/job_detail/{jid}.html"
    try:
        ok, detail, _reason = DC.fetch_detail(url)
    except Exception:
        return ""
    if not ok or not detail:
        return ""
    return str(detail.get("security_id") or "").strip()


def send_greet(security_id: str, job_id: str, message: str) -> tuple[bool, str]:
    from sandbox import require_registered
    require_registered("boss.greet")
    ok, out = run_boss(["greet", security_id, job_id, "--message", message], timeout=150)
    # 判定兼容：boss 输出是 JSON（{"ok": true}），先查带空格原样，再查删空格后的无空格形式。
    # 教训：旧写法 '"ok": true' in out.replace(" ", "") 把空格删了却检查带空格的子串，
    # 导致所有成功响应（ok:true）被误判失败——5 连败熔断、重复投递风险。
    if ok and ('"ok": true' in out or '"ok":true' in out.replace(" ", "")):
        return True, ""
    # 截取错误信息
    codes = [c for c in BREAKER_CODES if c in out]
    hints = [h for h in RISK_HINTS if h in out]
    err = (codes + hints) or []
    snippet = re.sub(r"\s+", " ", out)[:220]
    return False, (f"[{','.join(err)}] " if err else "") + snippet


def should_break(err: str) -> bool:
    return any(c in err for c in BREAKER_CODES) or any(h in err for h in RISK_HINTS)


def is_boss_platform(platform) -> bool:
    """是否是 BOSS 直聘的岗位。

    为什么必须分：外部 feed（51job / 智联 / 猎聘 / 网页抓取）来的岗位没有
    security_id，投递 CLI 根本用不了。留在队列里有两个害处：
      ① 让「候选队列 89 条」这个数字虚高 —— 实际能投的没那么多
      ② 每次都被计进「缺 security_id」制造噪音，看起来很吓人
    2026-10-21 实测：657 条里 47 条是外部平台，100% 没有 security_id。

    历史数据的 platform 是 NULL（那时候还没记这个字段），那些是 BOSS 的，要保留。
    """
    p = str(platform or "").strip().lower()
    return p in ("", "boss")


def load_apply_jobs(c) -> tuple[list[dict], int]:
    """取可投递的岗位 + 被排除的外部岗位数。"""
    rows = [dict(r) for r in c.execute("SELECT * FROM jobs")]
    jobs = [j for j in rows if is_boss_platform(j.get("platform"))]
    return jobs, len(rows) - len(jobs)


def is_transient(err: str) -> bool:
    """瞬时错误（网络抖动）—— 值得重试一次。"""
    return any(c in str(err or "") for c in TRANSIENT_CODES)


# 这些 security_id 形态投了必然失败 —— 不是「岗不好」，是数据本身不可用。
_UNUSABLE_SID_PREFIXES = ("cpc_job_list_chat_",)


def unusable_security_id(sid: str) -> bool:
    """security_id 是否根本用不了（不该浪费一次投递尝试）。

    三种形态：
    · 以 "-" 开头 —— 会被 boss CLI 当命令行选项解析，报 "No such option: -k"。
      实测：卡姿兰的 security_id 是 `-kurpN4f0DFlu-...`，每次投递都 INVALID_PARAM。
    · `cpc_job_list_chat_` 前缀 —— 聊天页采集生成的占位符，不是真 ID。
    · 空 —— 这里的调用方已单独统计「缺字段」，不要把两种情况混成一个数
      （缺字段是采集短板，坏值是数据脏）。
    """
    s = str(sid or "").strip()
    if not s:
        return True
    return s.startswith("-") or s.startswith(_UNUSABLE_SID_PREFIXES)


def send_with_retry(security_id: str, job_id: str, message: str, *,
                    sender=None, retry_delay: float = RETRY_DELAY,
                    sleep=None, log=print) -> tuple[bool, str, int]:
    """兼容旧调用名；写操作一旦提交就不因网络错误自动重发。

    超时/断连/5xx 无法证明远端没收到，必须标记结果未知并核对。
    返回 (ok, err, attempts)，保留旧注入参数以兼容既有 runner。
    """
    send = sender if sender is not None else send_greet
    try:
        ok, err = send(security_id, job_id, message)
    except Exception as exc:
        from sandbox import KernelError
        if isinstance(exc, KernelError):
            return False, f"[{exc.code}] {exc}", 0
        return False, f"[OUTCOME_UNKNOWN] 发送结果待核对：{type(exc).__name__}", 1
    if not ok and is_transient(err) and not should_break(err):
        return False, f"[OUTCOME_UNKNOWN] 发送结果待核对，未自动重发；{err}", 1
    return ok, err, 1


def resume_hint(done: int, total: int, sent: int, failed: int, err: str = "") -> str:
    """熔断/中断后告诉用户怎么接着投。

    为什么值得写清楚：2026-10-21 真机跑 70 条的配额，第 14 条登录态失效熔断，
    剩下 56 条一条没发。用户看到「🛑 立即停止」只会以为今天到此为止 ——
    实际上重新扫码、重跑同一条命令就能接着投（已投成功的不在队列里了，
    失败的会自动重排进来）。不说这句话，那 56 条就真的浪费了。
    """
    left = total - done
    if left <= 0:
        return ""
    lines = ["", f"📌 还剩 {left} 条没投（本轮 {sent} 条成功 · {failed} 条失败）。"]
    if "OUTCOME_UNKNOWN" in (err or ""):
        lines.append("   当前条发送结果待核对，已暂停；请在原平台核对，不能直接重发。")
        lines.append("   执行中和待核对记录已从自动发送队列排除。")
        return "\n".join(lines)
    if "AUTH" in (err or "") or "登录" in (err or ""):
        lines.append("   ① 先重新登录： python boss_login.py")
        lines.append("   ② 再重跑刚才那条命令 —— 参数不用改")
    else:
        lines.append("   重跑刚才那条命令即可接着投 —— 参数不用改")
    lines.append("   已发送、执行中和待核对记录不再自动执行；明确失败的记录可重新排队。")
    return "\n".join(lines)


def _hhmm(ts_ms: float) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M")


def pace_gate(pace, now_ms: float | None = None) -> tuple[float, str]:
    """动手前问一次节奏护栏：还要等几秒、为什么等。

    熔断优先于窗口——熔断是平台已经给了负反馈，窗口只是我们自己设的保守上限。
    """
    if pace is None:
        return 0.0, ""
    now = float(now_ms) if now_ms is not None else time.time() * 1000
    fuse_ms = pace.fuse_remaining_ms(now)
    if fuse_ms > 0:
        return fuse_ms / 1000, f"熔断冷却中，{P.fmt_seconds(fuse_ms / 1000)} 后继续（连续熔断第 {pace.fuse_count} 次）"
    if pace.window_remaining(now) <= 0:
        w = pace.window(now)
        return w["wait_ms"] / 1000, (f"近 {w['window_minutes']} 分钟已访问 {w['total']} 个岗位，"
                                     f"到 {_hhmm(w['next_at_ms'])} 再继续")
    return 0.0, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="jobkit 批量投递流程")
    ap.add_argument("--limit", type=int, default=5, help="本轮最多投几条，默认 5")
    ap.add_argument("--dry-run", action="store_true", help="只生成并预览，不发送")
    ap.add_argument("--resume", default=DEFAULT_RESUME)
    ap.add_argument("--interval", type=int, nargs=2, default=[60, 120],
                    metavar=("MIN", "MAX"), help="招呼间隔秒数区间")
    ap.add_argument("--rest-every", type=int, default=5, help="每投几条休息一次")
    ap.add_argument("--rest", type=int, nargs=2, default=[600, 900],
                    metavar=("MIN", "MAX"), help="休息秒数区间")
    ap.add_argument("--daily-cap", type=int, default=130, help="每日投递上限")
    ap.add_argument("--max-fail", type=int, default=5, help="连续失败几次后熔断")
    ap.add_argument("--online-only", action="store_true",
                    help="只投在线的 Boss（离线岗位打招呼照样送达，默认不跳过）")
    ap.add_argument("--enrich-detail", action="store_true",
                    help="缺 security_id 的岗位先进详情页回填（需要 DOM 通道可用，每条约 5-10s）")
    ap.add_argument("--enrich-detail-limit", type=int, default=10,
                    help="本轮最多回填几条，避免把浏览器打爆（默认 10）")
    ap.add_argument("--cities", default="广州,深圳",
                    help="只投这些城市，逗号分隔；传空字符串表示不限（默认 广州,深圳）")
    ap.add_argument("--no-pace", action="store_true",
                    help="关闭节奏护栏（窗口/令牌桶/熔断），退回只看 --interval")
    ap.add_argument("--pace-state", default="pace_state.json",
                    help="节奏状态文件，跨进程累计访问窗口（默认 pace_state.json）")
    ap.add_argument("--no-filter", action="store_true",
                    help="忽略 settings 里的二次过滤（仅调试用）")
    args = ap.parse_args()
    if not args.dry_run:
        from main import dispatch
        from sandbox import KernelError
        try:
            result = dispatch("boss.apply", {"limit": args.limit})
            return 0 if result.get("ok") else 1
        except KernelError as exc:
            print(f"{exc.code}：{exc}")
            return 3
    from sandbox import require_legacy_workspace
    require_legacy_workspace()

    resume = Path(args.resume).read_text(encoding="utf-8", errors="replace")
    c = store.connect()
    greeted = store.delivery_blocked_jobs(c)
    jobs, n_external = load_apply_jobs(c)
    c.close()

    # 队列：过滤通过 + 有 security_id + 未投过 + 城市匹配
    cities = [x.strip() for x in (args.cities or "").split(",") if x.strip()]
    queue = []
    skipped_offline = 0
    skipped_city = 0
    skipped_no_sec = 0
    skipped_dash_sec = 0
    enriched = 0
    enrich_left = max(0, int(getattr(args, "enrich_detail_limit", 0) or 0))
    for j in jobs:
        ok, _ = rules.job_ok(j)
        if not ok or j["job_id"] in greeted:
            continue
        # 缺 security_id 是采集侧的已知短板：投递 CLI 必须带它。
        # 先试着进详情页回填（--enrich-detail 且额度未用尽），补到了就入队并写回库；
        # 补不到才计数告警 —— 绝不静默丢弃（静默会表现成「今天没岗」，实际是数据缺字段）。
        if not j.get("security_id"):
            if getattr(args, "enrich_detail", False) and enrich_left > 0:
                enrich_left -= 1
                sid = _enrich_security_id(j["job_id"])
                if sid:
                    j["security_id"] = sid
                    enriched += 1
                    try:
                        store.upsert_job(c, j, "enrich:detail", time.time())
                    except Exception:
                        pass          # 写回失败不影响本轮投递
            if not j.get("security_id"):
                skipped_no_sec += 1
                continue
        # security_id 形态不可用的，投了必然失败，直接跳过不浪费尝试
        # （"-" 开头会被 CLI 当命令行选项；cpc_job_list_chat_ 是采集占位符）
        if unusable_security_id(j.get("security_id")):
            skipped_dash_sec += 1
            continue
        if cities and (j.get("city") or "").strip() not in cities:
            skipped_city += 1
            continue
        if args.online_only and (j.get("boss_active") or "") == "离线":
            skipped_offline += 1
            continue
        queue.append(j)

    ONLINE = ("在线", "刚刚活跃", "今日活跃")
    # 排序：在线的排前面，其余按匹配分；离线的排最后但不剔除（打招呼照样会送到）
    def rank(j):
        act = j.get("boss_active") or ""
        if act in ONLINE:
            tier = 0
        elif act == "离线":
            tier = 2
        else:
            tier = 1
        return (tier, -(j.get("match_score") or 0))
    queue.sort(key=rank)

    # ── 二次过滤：settings 驱动的档位过滤（默认全关，打开才生效）──
    filter_cfg = {} if args.no_filter else F.from_settings(settings_mod.load())
    filter_on = any(filter_cfg.get(k) for k in (
        "redline_guard", "hr_activity_filter", "job_filter_enabled",
        "filter_double_rest", "filter_work_hours"))
    if filter_on:
        kept, dropped = [], {}
        for j in queue:
            ok, reason = F.check_job(j, filter_cfg)
            if ok:
                kept.append(j)
            else:
                dropped[reason] = dropped.get(reason, 0) + 1
        if dropped:
            detail = "，".join(f"{k} {v}" for k, v in sorted(dropped.items(), key=lambda x: -x[1]))
            print(f"二次过滤：剔除 {sum(dropped.values())} 条（{detail}）")
        queue = kept

    pace_state = None if args.no_pace else P.PaceState.load(args.pace_state)
    if pace_state:
        pace_state.begin_round()
        w = pace_state.window()
        print(f"节奏护栏：{w['window_minutes']} 分钟窗口已用 {w['total']}/{w['limit']} · "
              f"令牌桶 {pace_state.pace['DETAIL_PER_MINUTE']}/分钟 · "
              f"状态文件 {args.pace_state}")

    print("═" * 62)
    print(f"jobkit 投递流程{'（试运行，不发送）' if args.dry_run else ''}")
    print("═" * 62)
    online_n = sum(1 for j in queue if (j.get("boss_active") or "") in ONLINE)
    print(f"候选队列 {len(queue)} 条（在线 {online_n} · 离线/未知 {len(queue) - online_n}），"
          f"本轮取 {min(args.limit, len(queue))} 条")
    if n_external:
        print(f"（已排除 {n_external} 条非 BOSS 岗位 —— 51job/智联/猎聘/网页抓取，"
              f"这些没有 security_id，投递通道用不了）")
    print(f"城市筛选：{'不限' if not cities else ' / '.join(cities)}"
          + (f"（已跳过 {skipped_city} 条其他城市）" if skipped_city else ""))
    if skipped_offline:
        print(f"（--online-only 已跳过 {skipped_offline} 条离线岗位）")
    if enriched:
        print(f"✓ 已进详情页回填 {enriched} 条 security_id（并写回台账）")
    if skipped_no_sec:
        print(f"⚠️  另有 {skipped_no_sec} 条岗位缺 security_id，无法投递（DOM 通道常见）；"
              f"这些不是「没岗」，是数据缺字段。加 --enrich-detail 可尝试进详情页补全。")
    if skipped_dash_sec:
        print(f"⚠️  另有 {skipped_dash_sec} 条岗位的 security_id 用不了（以 \"-\" 开头会被当命令行选项，"
              f"或 \"cpc_job_list_chat_\" 占位符），已跳过；这类岗位需在 BOSS 网页手动打招呼。")

    # ── 通道门禁：写操作只允许走真实浏览器 ──────────────────
    ch_ok, ch, ch_why = delivery_channel_ok()
    print(f"浏览器通道：{'✅' if ch_ok else '❌'} {ch_why}")

    if not args.dry_run:
        if not ch_ok:
            # 文案原则：先说「你该怎么做」，再说「为什么」，技术细节放最后。
            # 血教训：原来开门就是「cd 到某目录跑 python 模块 + edge://extensions 加载扩展」，
            # 对只会用 app 的人是劝退级 —— 而且现在根本不需要这么做（壳子自带浏览器）。
            print("\n🛑 这条通道走不通，已拒绝投递（没有发送任何消息）。")
            print("")
            print("  ── 推荐做法：什么都不用配 ─────────────────────────")
            print("  直接在 jobkit 窗口里点右上角的【开始投递】。")
            print("  它用软件自带的浏览器投递，登录态就是你在窗口里登的那个，")
            print("  不需要 Bridge、不需要装浏览器扩展、不需要敲命令。")
            print("")
            print("  ── 你为什么会看到这条 ──────────────────────────────")
            print("  命令行版（apply_pipeline.py）只能借用【外部】浏览器，顺序是：")
            print("     Bridge（扩展+后台服务）→ CDP → headless 兜底")
            print("  前面两条现在都不通，只剩 headless —— 而 headless 浏览器 + 伪造 UA")
            print("  正是平台识别自动化的主要特征（2026-09-19 的账户风控就是这么触发的），")
            print("  所以这里宁可停下来，也不偷偷降级。")
            print("")
            print("  ── 确实要用命令行版的话（进阶）────────────────────")
            print("     1) python -m boss_agent_cli.bridge.daemon --serve")
            print("     2) 浏览器打开 edge://extensions → 开发者模式 → 加载解压缩的扩展")
            print("        → 选 boss-agent-cli-dev/extension")
            print("     3) 重跑本命令；详见 jobkit/README.md")
            print("")
            print("  ── 只想试跑不发送 ──────────────────────────────────")
            print("     python apply_pipeline.py --dry-run")
            return 3

        ready, why = login_ready()
        sent_today = today_sent_count()
        print(f"登录态：{'✅ ok' if ready else '❌ ' + why}")
        print(f"今日已投：{sent_today} / 上限 {args.daily_cap}")
        if not ready:
            print("\n❌ 登录态不可用于投递，先运行 python boss_login.py")
            return 2
        if sent_today >= args.daily_cap:
            print(f"\n🛑 已达每日上限 {args.daily_cap}，今天不再投递。")
            return 0
    else:
        print("登录态：跳过检查（试运行不需要）")

    api_key, base_url = gg.resolve_key()
    if not api_key:
        print("❌ 未找到 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    picked = queue[:args.limit]
    if not picked:
        print("\n（队列为空：先跑 boss search 抓岗，或所有岗都已投过）")
        return 0

    results = []
    consecutive_fail = 0
    cc = store.connect()
    started = time.time()

    for idx, job in enumerate(picked, 1):
        title = (job.get("title") or "")[:30]
        company = (job.get("company") or "")[:18]

        # 节奏护栏：窗口满 / 熔断中就先等，等完再动手
        wait_s, why = pace_gate(pace_state)
        if wait_s > 0:
            print(f"\n⏸  {why}")
            try:
                time.sleep(wait_s)
            except KeyboardInterrupt:
                print("\n⏹ 手动中断，未发送任何消息。")
                break
            if pace_state:
                pace_state.begin_round()

        print(f"\n[{idx}/{len(picked)}] {title} @ {company}  {job.get('salary','')} {job.get('city','')}")

        # 1) 生成定制招呼语
        brief, _ = build_brief(job)
        try:
            res, usage = gg.generate(brief, resume, company=job.get("company") or "",
                                     title=job.get("title") or "", count=1,
                                     api_key=api_key, base_url=base_url, temperature=0.7)
            message = extract_message(res)
        except Exception as e:
            print(f"     ⚠️ 招呼语生成失败：{str(e)[:110]}")
            message = ""

        # 提示词约束不可靠，加一道确定性截断（保留首句匹配证据 + 末句行动号召）
        raw_len = len(message)
        message = shorten_message(message)
        if message:
            note = f"（原 {raw_len} 字，已截断）" if raw_len != len(message) else ""
            print(f"     · 招呼语 {len(message)} 字{note}")
            print(f"       {message[:100]}{'…' if len(message) > 100 else ''}")

        if not message:
            fallback = f"您好，我在 {job.get('title','')} 这个岗位上看到贵司的需求，和我的项目经历比较匹配，方便发份简历给您看看吗？"
            message = fallback
            print(f"     · 用回退模板（生成失败）")
        if args.dry_run:
            results.append((job, "preview", ""))
            continue

        # 2) 发送（先占一个令牌桶位；发送结果决定窗口里的归类）
        if pace_state:
            pace_state.record_click(desc_ok=True)
        # 在任何发送之前落盘；崩溃后的 executing 记录也不能自动重发。
        if not store.claim_greet(cc, job["job_id"], job["security_id"], message, job=job):
            print("     ⏸ 已发送、执行中或结果待核对，跳过")
            continue
        ok, err, attempts = send_with_retry(job["security_id"], job["job_id"], message)
        if ok:
            print(f"     ✅ 已发送{'（重试后成功）' if attempts > 1 else ''}")
            store.record_greet(cc, job["job_id"], job["security_id"], message, "sent", job=job)
            consecutive_fail = 0
            results.append((job, "sent", ""))
            if pace_state:
                pace_state.record("delivered")
        else:
            print(f"     ❌ 失败：{err[:130]}")
            outcome = "outcome_unknown" if "OUTCOME_UNKNOWN" in err else "failed"
            store.record_greet(cc, job["job_id"], job["security_id"], message, outcome, err, job=job)
            consecutive_fail += 1
            results.append((job, "failed", err))
            if pace_state:
                pace_state.record("failed")
            if should_break(err):
                print(f"\n🛑 命中熔断信号，立即停止（不重试）。")
                n_sent = sum(1 for r in results if r[1] == "sent")
                n_fail = sum(1 for r in results if r[1] == "failed")
                print(resume_hint(idx, len(picked), n_sent, n_fail, err))
                break
            if consecutive_fail >= args.max_fail:
                print(f"\n🛑 连续失败 {consecutive_fail} 次，停止。")
                n_sent = sum(1 for r in results if r[1] == "sent")
                n_fail = sum(1 for r in results if r[1] == "failed")
                print(resume_hint(idx, len(picked), n_sent, n_fail, err))
                break
            continue

        # 3) 节奏控制：基础间隔与令牌桶取大者，宁慢勿快
        if idx < len(picked):
            if idx % args.rest_every == 0:
                wait = random.randint(*args.rest)
                print(f"     ☕ 已投 {idx} 条，休息 {wait//60} 分钟")
            else:
                wait = random.randint(*args.interval)
                token_s = pace_state.token_wait() / 1000 if pace_state else 0
                if token_s > wait:
                    print(f"     ⏳ 令牌桶限速：等待 {int(token_s)} 秒")
                    wait = int(token_s)
                else:
                    print(f"     ⏳ 等待 {wait} 秒")
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                print("\n⏹ 手动中断，已投记录已落库。")
                break

    if pace_state:
        pace_state.save(args.pace_state)
    cc.close()
    elapsed = time.time() - started

    sent = sum(1 for _, s, _ in results if s == "sent")
    print("\n" + "═" * 62)
    print(f"本轮结束：发送 {sent} · 失败 {sum(1 for _,s,_ in results if s=='failed')} · "
          f"试运行 {sum(1 for _,s,_ in results if s=='preview')} · 耗时 {elapsed/60:.1f} 分钟")
    if sent:
        print(f"今日累计：{today_sent_count()} 条")
    if args.dry_run:
        print("\n试运行不发送。去掉 --dry-run 即真实投递（需要登录态）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
