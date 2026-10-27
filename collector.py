#!/usr/bin/env python3
"""岗位自动收集器 —— 定时跑搜索，增量扩充岗位池。

为什么要它：池子不增长，排序/筛选再准也只是在旧数据里挑。手动 `boss search` 抓一次
就 254 个岗，几天就过期了（岗位会下架、HR 会离线）。

设计要点：
  1. **走 CLI 的搜索通道**（Bridge 真实浏览器），与投递同一条路，不用 headless
  2. **关键词 × 城市 矩阵 + 轮转游标**：每轮只跑一部分组合，游标持久化，
     多轮下来自动覆盖全部组合，不会永远只搜前几个词
  3. **upsert 去重**：job_id 是主键；已存在只更新 last_seen 和**缺失**字段
     （`store.upsert_job` 用 COALESCE(NULLIF(...)) 保留已有值），不会覆盖好数据
  4. **限速 + 熔断**：搜索间隔 25-45s 随机；连续失败 3 次停；
     命中风控信号（ACCOUNT_RISK / RATE_LIMITED）**立即停**，不重试不试探
  5. 每轮跑完打印「新增 / 更新 / 失败」并写一行到 runs 表

用法：
    python collector.py                    # 按配置跑一轮
    python collector.py --dry-run          # 只打印计划，不真搜
    python collector.py --max 5            # 本轮最多 5 次搜索
    python collector.py --keywords "AI应用开发,大模型" --cities 广州,深圳
    python collector.py --status           # 看池子现状 + 上轮记录

风控说明：
    `boss search` 是只读搜索，比打招呼安全得多（实测单次约 19s），
    但仍要限速 —— 别把只读当"随便打"。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import settings as st  # noqa: E402
import store  # noqa: E402

CLI_DIR = Path(r"C:\Users\<user>\boss-agent-cli-dev")
STATE_FILE = HERE / "collect_state.json"

DEFAULT_KEYWORDS = ["AI应用开发", "大模型", "Agent", "Python后端", "LLM", "智能体", "AI工程师", "算法应用"]
DEFAULT_CITIES = ["广州", "深圳", "东莞", "佛山", "惠州"]

DEFAULTS_COLLECT = {
    "enabled": True,
    "keywords": DEFAULT_KEYWORDS,
    "cities": DEFAULT_CITIES,
    "max_searches_per_round": 20,
    "min_interval_sec": 25,
    "max_interval_sec": 45,
}

# ── 失败分类 ──────────────────────────────────────────────────
# 为什么要分类（2026-09-20 血教训）：
#   原来「连续失败 3 次就停整轮」。实测这台机器上 BOSS 搜索会**间歇性抖动** ——
#   同一条命令、同一个城市，前一次 NETWORK_ERROR(22s)，后一次直接成功拿到 15 条。
#   3 次抖动就把整轮 20 次搜索废掉（04:14 那轮就是这样：失败 3 次 → 停，剩 17 个组合没搜）。
#   所以：真风控立即停（铁律不变），网络抖动重试 + 更高阈值，其余才按原逻辑。
RISK_SIGNALS = (
    "ACCOUNT_RISK", "RATE_LIMITED", "FORBIDDEN", "AUTH_EXPIRED",
    "GREET_LIMIT", "TOKEN_REFRESH_FAILED",
    # ⚠️ 中文措辞要按平台**实际原文**写，不能想当然。
    # 教训：原来写「账户异常」，而 BOSS 实际返回的是「您的账户**存在**异常」——
    # 子串匹配不上，真风控会被当成普通失败漏过去。实测原文来自 ACCOUNT_RISK 的 message 字段。
    "账户存在异常", "账户异常", "操作过于频繁", "访问过于频繁",
    "环境存在异常", "存在异常行为",
)
TRANSIENT_SIGNALS = (
    "NETWORK_ERROR", "TIMEOUT", "TimeoutError", "timed out",
    "Connection", "ECONNRESET", "ConnectionRefused", "SSL", "proxy",
)
MAX_TRANSIENT_FAILS = 5      # 网络抖动累计到这个数才停（不是连续）

# ══════════════════════════════════════════════════════════════
# ⛔ 频次闸门（2026-09-20 ACCOUNT_RISK 血教训）
#
# 事故经过：
#   09-20 14:03~15:16 一小时内跑了 3 轮 × 60 次 = 180 次搜索（145 次实际发出），
#   15:16:44 命中 ACCOUNT_RISK。17:52~17:56 更夸张：4 分钟内 5 轮并发。
#
# 四个雷（全是我的锅）：
#   ① UI 的「每轮搜索次数」上限我写成 max=60 → 能设到配额的 3 倍
#   ② collector **没有单实例锁** → 多进程能同时跑（后端重启后旧进程还在跑）
#   ③ 只有「每轮上限」，**没有每日总量** → 跑多少轮没人管
#   ④ 没有轮次冷却 → 可以连着跑
#
# 判断错误：我把「搜索」当成无害的只读操作，只给「投递」设了节奏闸门。
# 实际上搜 145 次和投 145 次，平台看到的特征是一样的。
#
# 下面这组闸门是【硬编码】的，配置文件改不动 —— 血的教训不该再被配置绕过。
# ══════════════════════════════════════════════════════════════
LOCK_FILE = HERE / "collector.lock"
QUOTA_FILE = HERE / "collect_quota.json"

MAX_PER_ROUND_HARD = 20            # 单轮硬上限（UI/配置都改不动）
DAILY_SEARCH_CAP = 120             # 每日搜索总次数上限（跨天重置）
MIN_GAP_BETWEEN_ROUNDS_SEC = 1800  # 两轮之间至少 30 分钟
LOCK_STALE_SEC = 3600              # 锁超过 1 小时视为陈尸，可抢占


def _read_quota() -> dict:
    """读当日搜索计数。跨天自动重置。"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        d = json.loads(QUOTA_FILE.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        d = {}
    if d.get("date") != today:
        d = {"date": today, "searches": 0}
        try:
            QUOTA_FILE.write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass
    return d


def _add_quota(n: int) -> None:
    d = _read_quota()
    d["searches"] = int(d.get("searches") or 0) + int(n)
    try:
        QUOTA_FILE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def acquire_lock() -> tuple[bool, str]:
    """单实例锁。防止多进程并发收集。

    ⛔ 这是风控事故的直接成因之一：原来是「4 分钟内 5 轮并发」。
    实现方式：独占创建锁文件（O_EXCL）。陈尸锁（>1h）自动抢占。
    """
    now = time.time()
    if LOCK_FILE.exists():
        try:
            info = json.loads(LOCK_FILE.read_text(encoding="utf-8")) or {}
            age = now - float(info.get("at") or 0)
            pid = info.get("pid")
            if age < LOCK_STALE_SEC:
                alive = _pid_alive(pid)
                if alive:
                    return False, f"已有收集进程在跑（pid={pid}，{age / 60:.0f} 分钟前启动）"
                # pid 已死 → 陈尸，清掉
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({"pid": os.getpid(), "at": now}).encode())
        os.close(fd)
        return True, ""
    except OSError:
        return False, "拿不到收集锁（可能刚好被另一个进程抢占）"


def release_lock() -> None:
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError:
        pass


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{'1'}} else {{'0'}}"],
            capture_output=True, text=True, timeout=10)
        return "1" in (r.stdout or "")
    except Exception:
        return True   # 查不出来就当成活的（保守）


def check_frequency_gates(n: int) -> tuple[bool, str]:
    """频次闸门：日总量 + 轮次冷却。返回 (可跑, 拒绝原因)。"""
    q = _read_quota()
    used = int(q.get("searches") or 0)
    if used >= DAILY_SEARCH_CAP:
        return False, (f"今日搜索已达上限 {used}/{DAILY_SEARCH_CAP} —— 停止。"
                       f"（平台看到的「搜 N 次」和「投 N 次」特征一样，别过线）")
    if used + n > DAILY_SEARCH_CAP:
        return False, (f"本轮要搜 {n} 次，但今日只剩 {DAILY_SEARCH_CAP - used} 次额度 —— "
                       f"请把每轮次数调到 ≤{DAILY_SEARCH_CAP - used}，或明天再来")

    # 轮次冷却
    st = load_state()
    last = st.get("last_run_ts")
    if last:
        gap = time.time() - float(last)
        if 0 <= gap < MIN_GAP_BETWEEN_ROUNDS_SEC:
            left = (MIN_GAP_BETWEEN_ROUNDS_SEC - gap) / 60
            return False, (f"距上一轮只过了 {gap / 60:.0f} 分钟，"
                           f"再等 {left:.0f} 分钟就能跑 —— "
                           f"两轮之间至少间隔 {MIN_GAP_BETWEEN_ROUNDS_SEC // 60} 分钟（防风控）")
    return True, ""


def classify_failure(raw, want_signal: bool = False):
    """把失败按性质分类。

    返回 'risk' | 'transient' | 'deterministic'；
    want_signal=True 时返回命中的信号词（没命中返回 ''）。
    """
    s = str(raw or "")
    for sig in RISK_SIGNALS:
        if sig in s:
            return sig if want_signal else "risk"
    for sig in TRANSIENT_SIGNALS:
        if sig in s:
            return sig if want_signal else "transient"
    return "" if want_signal else "deterministic"


# ── 环境与调用 ────────────────────────────────────────────────
def clean_env() -> dict:
    """去掉代理变量。

    本机 Clash 会设 http_proxy，httpx 连 localhost 会被代理拦成 502（实测）。
    """
    import os
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = "localhost,127.0.0.1"
    return env


def boss_cmd() -> list[str]:
    """优先用 dev 目录的 uv run（带 IPv4 补丁 + bridge 依赖）。"""
    import shutil
    if (CLI_DIR / "pyproject.toml").exists() and shutil.which("uv"):
        return ["uv", "run", "--directory", str(CLI_DIR), "boss"]
    return ["boss"]


def run_search(keyword: str, city: str, timeout: int = 120) -> tuple[bool, object, str]:
    """跑一次搜索。返回 (成功, 岗位列表或None, 原始输出)。

    服务端筛选（收集时就把垃圾岗挡在门外）：
    - 全职：排除实习/兼职
    - 10-15K：靠谱AI应用岗集中区（5-10K档实测全是杂岗）；本地 rules.py 继续卡12K上限
    - 1年以内/1-3年：排除3-5年/5-10年够不着的岗
    """
    cmd = boss_cmd() + [
        "--json", "search", keyword,
        "--city", city,
        "--job-type", "全职",
        "--salary", "10-15K",
        "--experience", "1年以内,1-3年",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=clean_env(), cwd=str(CLI_DIR), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, None, "TIMEOUT"
    except OSError as e:
        return False, None, f"OSError: {e}"

    out = (p.stdout or "") + (p.stderr or "")
    i = out.find("{")
    if i < 0:
        return False, None, out[:300]
    try:
        d = json.loads(out[i:out.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError) as e:
        return False, None, f"JSON 解析失败: {e}"

    if not d.get("ok"):
        err = (d.get("error") or {}).get("code") or "UNKNOWN"
        return False, None, f"CLI 报错 {err}"
    return True, d.get("data") or [], out


# ── 第二通道：DOM（GetJob 2.3.1 选择器层迁移）────────────────
SOURCE_CLI, SOURCE_DOM, SOURCE_AUTO = "cli", "dom", "auto"
SOURCE_ZHILIAN = "zhilian"
# zhilian 不进 auto：auto 的语义是「BOSS 的两条通道互相兜底」，
# 跨平台回退会把「这个平台没岗」伪装成「另一个平台的岗」，语义就脏了。
SOURCES = (SOURCE_CLI, SOURCE_DOM, SOURCE_AUTO, SOURCE_ZHILIAN)

# DOM 失败里「通道本身不可用」的几类 → auto 模式才允许回落 CLI。
# restricted（风控）刻意不在其中：平台已给负反馈，回落换通道继续请求等于绕过风控。
_DOM_FALLBACK_REASONS = ("page-error", "not-logged-in", "selector-miss")


def _fill_score(job: dict) -> dict:
    """采集时就地打分 —— 没有分数的岗位在用户眼里等于没被推荐过。

    智联通道返回的 job 不带 match_score，不补的话那批岗在看板上显示「—」，
    用户无法判断该不该投，产品价值就断了。
    """
    try:
        import rules
        if not job.get("match_score"):
            job["match_score"] = rules.job_score(job)
    except Exception:                            # noqa: BLE001  打分失败不该丢岗位
        pass
    return job


def _ensure_browser() -> tuple[bool, str]:
    """浏览器没开就自动开。用户不该为了「端口没起」去看文档。"""
    try:
        import dom_probe
        return dom_probe.ensure_browser()
    except Exception as e:                       # noqa: BLE001
        return False, f"浏览器准备失败：{type(e).__name__}: {e}"


def _dom_search(keyword: str, city: str, limit: int = 30) -> tuple[bool, object, str]:
    """DOM 通道：连已登录的浏览器读列表页。返回 (成功, 岗位列表或None, 原始输出)。

    原始输出仍是字符串，复用 CLI 通道的 classify_failure 分类逻辑。
    """
    ok_b, why = _ensure_browser()
    if not ok_b:
        return False, None, f"DOM page-error {why}"

    try:
        import dom_channel
    except Exception as e:                       # 模块坏了不能拖垮主链路
        return False, None, f"DOM page-error import: {e}"

    try:
        ok, jobs, reason = dom_channel.fetch_jobs(keyword, city, limit=limit)
    except Exception as e:                       # 兜底：任何意外都按通道失败，不抛给上层
        return False, None, f"DOM page-error exception: {e}"

    if not ok:
        # 风控必须落进既有的风险信号词，否则会被当成普通失败继续搜
        if reason == "restricted":
            return False, None, "ACCOUNT_RISK DOM restricted"
        return False, None, f"DOM {reason}"

    out = []
    for j in jobs or []:
        d = dict(j)
        # DOM 侧叫 company_scale，jobs 表叫 scale
        scale = d.pop("company_scale", "")
        if scale and not d.get("scale"):
            d["scale"] = scale
        d["source_channel"] = "dom"
        out.append(d)
    return True, out, f"DOM ok({len(out)})"


def _zhilian_search(keyword: str, city: str, limit: int = 30):
    """智联通道：自造搜索 URL → CDP 取 __INITIAL_STATE__。

    与 BOSS 通道的关键差别：智联的列表页 state 里就带着 JD 全文与真实薪资，
    不需要为每个岗位再开详情页（省时间，也少一次风控暴露面）。
    """
    try:
        import zhilian_channel as ZC
        from dom import zhilian_urls as ZU
    except ImportError as e:                      # 依赖缺失要说清楚，不能静默
        return False, None, f"zhilian 通道不可用: {e}"

    ok_b, why = _ensure_browser()
    if not ok_b:
        return False, None, f"zhilian {why}"

    try:
        url = ZU.build_search_url(keyword, city)
    except ZU.UnknownCityError as e:
        # 城市名本身就不认识 → 跳过这个城市，文案交给 zhilian_urls 说人话
        return False, None, f"zhilian {e}"

    ok, jobs, reason = ZC.fetch_jobs(url, limit=limit)
    if ok:
        return True, jobs, f"zhilian ok({len(jobs)})"
    return False, None, f"zhilian {reason}"


def search_jobs(keyword: str, city: str, source: str = SOURCE_CLI,
                limit: int = 30) -> tuple[bool, object, str]:
    """按通道取岗位。

    cli      → boss-agent-cli（原有行为，默认）
    dom      → 只用 BOSS DOM，失败就失败（用来单独观察选择器层效果）
    auto     → BOSS DOM 优先，通道不可用时回落 CLI 并打印原因
    zhilian  → 智联招聘（独立通道，不参与 auto 的跨平台回落）
    """
    if source not in SOURCES:
        raise ValueError(f"未知通道 {source!r}（只支持 {'/'.join(SOURCES)}）")

    if source == SOURCE_CLI:
        return run_search(keyword, city)
    if source == SOURCE_DOM:
        return _dom_search(keyword, city, limit)
    if source == SOURCE_ZHILIAN:
        return _zhilian_search(keyword, city, limit)

    ok, jobs, raw = _dom_search(keyword, city, limit)
    if ok:
        return True, jobs, raw
    why = str(raw)[4:].split(" ")[0] if str(raw).startswith("DOM ") else ""
    if why in _DOM_FALLBACK_REASONS:
        print(f"  ↓ DOM 通道不可用（{why}）—— 回落 CLI")
        return run_search(keyword, city)
    return False, None, raw


# ── 状态（轮转游标）──────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            pass
    return {"cursor": 0, "rounds": 0, "last_run": None}


def save_state(s: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── 主流程 ───────────────────────────────────────────────────
def collect(dry_run: bool = False, max_searches: int | None = None,
            keywords: list[str] | None = None, cities: list[str] | None = None,
            on_progress=None, source: str = SOURCE_CLI) -> dict:
    """收集一轮 —— **带频次闸门的外壳**。

    真正的实现是 _collect_inner。这里只做三件事：
      ① 把单轮次数钳到硬上限（配置/UI 都改不动）
      ② 检查日总量 + 轮次冷却
      ③ 拿单实例锁，跑完释放

    ⛔ 为什么必须有这三道（2026-09-20 ACCOUNT_RISK 事故）：
    原来只有「每轮上限 20」这一道软约束，而 UI 能把它设到 60、
    没有并发锁、没有日总量、没有冷却 —— 结果一小时内 180 次搜索、145 次实际发出、
    4 分钟内 5 轮并发，直接 ACCOUNT_RISK。
    """
    cfg = st.load().get("collect") or {}
    raw = max_searches if max_searches is not None else (
        cfg.get("max_searches_per_round") or DEFAULTS_COLLECT["max_searches_per_round"])
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULTS_COLLECT["max_searches_per_round"]

    # ① 硬上限（配置和 UI 都绕不过去）
    if n > MAX_PER_ROUND_HARD:
        print(f"  ⚠️ 请求 {n} 次/轮超过硬上限，已钳到 {MAX_PER_ROUND_HARD}（防风控）")
        if on_progress:
            try:
                on_progress("clamped", {"requested": n, "used": MAX_PER_ROUND_HARD})
            except Exception:
                pass
        n = MAX_PER_ROUND_HARD

    # ② 频次闸门
    okf, why = check_frequency_gates(n)
    if not okf:
        print(f"\n🛑 已拒绝本轮收集：{why}")
        if on_progress:
            try:
                on_progress("blocked", {"reason": why})
            except Exception:
                pass
        return {"blocked": why, "searches": 0}

    # ③ 单实例锁
    okl, lock_msg = acquire_lock()
    if not okl:
        print(f"\n🛑 已拒绝本轮收集：{lock_msg}")
        if on_progress:
            try:
                on_progress("blocked", {"reason": lock_msg})
            except Exception:
                pass
        return {"blocked": lock_msg, "searches": 0}

    try:
        r = _collect_inner(dry_run=dry_run, max_searches=n, keywords=keywords,
                           cities=cities, on_progress=on_progress, source=source)
        # 计额度 + 记本轮时间（冷却用）
        did = int(r.get("attempted") or r.get("searches") or 0)
        if not dry_run and did:
            _add_quota(did)
            stt = load_state()
            stt["last_run_ts"] = time.time()
            save_state(stt)
        return r
    finally:
        release_lock()


def _collect_inner(dry_run: bool = False, max_searches: int | None = None,
                   keywords: list[str] | None = None, cities: list[str] | None = None,
                   on_progress=None, source: str = SOURCE_CLI) -> dict:
    """跑一轮收集。

    on_progress(stage, payload) 会在每个关键节点被调用，供 UI 实时显示：
        ('start',    {total, cursor, combos})
        ('waiting',  {index, wait_sec})
        ('search',   {index, keyword, city})
        ('got',      {index, keyword, city, count, new, cache_hit, sec})
        ('failed',   {index, keyword, city, reason})
        ('risk',     {signal})
        ('done',     {inserted, updated, failed, cache_hits, risk})
    """
    def emit(stage: str, payload: dict) -> None:
        if on_progress is None:
            return
        try:
            on_progress(stage, payload)
        except Exception:
            pass

    cfg = st.load().get("collect") or {}
    kws = keywords or cfg.get("keywords") or DEFAULT_KEYWORDS
    cts = cities or cfg.get("cities") or DEFAULT_CITIES
    limit = int(max_searches if max_searches is not None else
                (cfg.get("max_searches_per_round") or DEFAULTS_COLLECT["max_searches_per_round"]))
    lo = int(cfg.get("min_interval_sec") or DEFAULTS_COLLECT["min_interval_sec"])
    hi = int(cfg.get("max_interval_sec") or DEFAULTS_COLLECT["max_interval_sec"])

    combos = [(k, c) for k in kws for c in cts]
    if not combos:
        return {"error": "关键词或城市为空"}

    state = load_state()
    start = int(state.get("cursor") or 0) % len(combos)
    # 从游标处起，轮转取 limit 个（跑满一圈自动回到起点）
    picked = [combos[(start + i) % len(combos)] for i in range(min(limit, len(combos)))]

    print("═" * 62)
    print(f"岗位自动收集 · {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("═" * 62)
    print(f"  矩阵: {len(kws)} 关键词 × {len(cts)} 城市 = {len(combos)} 组合")
    print(f"  本轮: {len(picked)} 次搜索（游标 {start} → {(start + len(picked)) % len(combos)}）")
    print(f"  间隔: {lo}-{hi}s 随机")
    print(f"  组合: " + ", ".join(f"{k}@{c}" for k, c in picked))

    if dry_run:
        print("\n（--dry-run，未执行搜索）")
        emit("dry_run", {"planned": len(picked)})
        return {"dry_run": True, "planned": len(picked)}

    emit("start", {"total": len(picked), "cursor": start, "combos": len(combos)})

    conn = store.connect()
    now_base = time.time()
    inserted = updated = failed = 0
    cache_hits = 0
    consecutive_fails = 0     # 只统计「确定性失败」（配置/参数错）——它不是链路问题
    transient_fails = 0       # 网络抖动累计
    risk_hit = ""
    attempted = 0             # 实际搜过的组合数（游标按这个前进，不是按计划数）
    log_lines: list[str] = []

    def _try_search(kw_: str, city_: str, idx_: int):
        """跑一次，对网络抖动自动重试一次。

        返回 (ok, jobs, raw, sec, fatal)；
        fatal 非空 = 必须停止整轮（'risk:XXX' 或 'transient'）。
        """
        t0 = time.time()
        okk, jobs_, raw_ = search_jobs(kw_, city_, source)
        dt_ = time.time() - t0
        if okk:
            return True, jobs_, raw_, dt_, ""

        kind = classify_failure(raw_)
        if kind == "risk":
            return False, None, raw_, dt_, "risk:" + classify_failure(raw_, want_signal=True)

        if kind == "transient":
            print(f"  ↻ {kw_}@{city_} 网络抖动（{str(raw_)[:36]}）—— 重试一次")
            log_lines.append(f"[retry] {kw_}@{city_}: {str(raw_)[:80]}")
            emit("retry", {"index": idx_, "keyword": kw_, "city": city_, "reason": str(raw_)[:80]})
            t1 = time.time()
            ok2, jobs2, raw2 = search_jobs(kw_, city_, source)
            dt2 = time.time() - t1
            if ok2:
                return True, jobs2, raw2, dt2, ""
            return False, None, raw2, dt2, "transient"

        return False, None, raw_, dt_, "deterministic"

    for idx, (kw, city) in enumerate(picked):
        if idx > 0:
            wait = random.randint(lo, hi)
            print(f"\n  ⏳ 等待 {wait}s …")
            emit("waiting", {"index": idx, "total": len(picked), "wait_sec": wait})
            time.sleep(wait)

        emit("search", {"index": idx, "total": len(picked), "keyword": kw, "city": city})
        ok, jobs, raw, dt, fatal = _try_search(kw, city, idx)
        attempted = idx + 1

        if fatal.startswith("risk:"):
            risk_hit = fatal[5:]
            print(f"\n  🛑 命中风控信号 {risk_hit} —— 立即停止，不重试")
            log_lines.append(f"[risk] {risk_hit}")
            emit("risk", {"signal": risk_hit})
            break

        if fatal == "transient":
            transient_fails += 1
            failed += 1
            print(f"  ❌ {kw}@{city} 重试仍失败（抖动 {transient_fails}/{MAX_TRANSIENT_FAILS}）: {str(raw)[:60]}")
            log_lines.append(f"[fail] {kw}@{city} transient: {str(raw)[:100]}")
            emit("failed", {"index": idx, "total": len(picked), "keyword": kw, "city": city,
                            "reason": str(raw)[:160]})
            if transient_fails >= MAX_TRANSIENT_FAILS:
                print(f"\n  🛑 网络抖动累计 {transient_fails} 次 —— 停止本轮（大概率是链路问题）")
                emit("stop", {"reason": "transient-fails", "count": transient_fails})
                break
            continue

        if fatal == "deterministic":
            failed += 1
            consecutive_fails += 1
            print(f"  ❌ {kw}@{city}  失败({dt:.0f}s): {str(raw)[:90]}")
            log_lines.append(f"[fail] {kw}@{city}: {str(raw)[:120]}")
            emit("failed", {"index": idx, "total": len(picked), "keyword": kw, "city": city,
                            "reason": str(raw)[:160]})
            if consecutive_fails >= 3:
                print(f"\n  🛑 连续失败 {consecutive_fails} 次 —— 停止本轮")
                log_lines.append(f"[stop] consecutive-fails={consecutive_fails}")
                emit("stop", {"reason": "consecutive-fails", "count": consecutive_fails})
                break
            continue

        consecutive_fails = 0
        # ⚠️ 缓存命中伪装成成功：CLI 有 search_cache，命中时不到 1s 就返回。
        # 实测踩过：广州那次 1s"成功"，其实一次真实请求都没发，让人误以为收集正常。
        # 所以把 <3s 的响应当「缓存命中」，单独计数并明确提示。
        is_cache_hit = dt < 3.0
        new_in_this = 0
        for job in jobs:
            try:
                _fill_score(job)          # 采集与打分闭环：没分的当场补上
                is_new = store.upsert_job(conn, job, f"search:{kw}:{city}", now_base)
                if is_new:
                    inserted += 1
                    new_in_this += 1
                else:
                    updated += 1
            except Exception as e:
                log_lines.append(f"[err] upsert {job.get('job_id')}: {e}")
        conn.commit()
        if is_cache_hit:
            cache_hits += 1
            print(f"  ⚠️ {kw}@{city}  {len(jobs)} 条（新增 {new_in_this}）  {dt:.1f}s  "
                  f"← 太快了，是缓存命中，不是真实搜索")
            log_lines.append(f"[cache] {kw}@{city} got={len(jobs)} new={new_in_this} {dt:.1f}s")
        else:
            print(f"  ✅ {kw}@{city}  {len(jobs)} 条（新增 {new_in_this}）  {dt:.0f}s")
            log_lines.append(f"[ok] {kw}@{city} got={len(jobs)} new={new_in_this}")
        emit("got", {"index": idx, "total": len(picked), "keyword": kw, "city": city,
                     "count": len(jobs), "new": new_in_this,
                     "cache_hit": is_cache_hit, "sec": round(dt, 1)})

    # 写 runs 记录（表结构：started_at/ended_at/sent/failed/stopped，
    #                    收集场景复用：sent=新增岗位数，stopped=状态说明）
    try:
        conn.execute(
            "INSERT INTO runs (started_at, ended_at, sent, failed, stopped) VALUES (?,?,?,?,?)",
            (now_base, time.time(), inserted, failed,
             (f"collect risk={risk_hit}" if risk_hit else f"collect searches={len(picked)}")),
        )
        conn.commit()
    except Exception:
        pass
    conn.close()

    # 游标按【实际搜过】的组合数前进。
    # 血教训：原来按 len(picked)（计划数）前进 —— 熔断提前退出时会**跳过没搜过的组合**。
    # 04:14 那轮实际只搜了 3 个，游标却从 6 跳到 26，中间 17 个组合被永久跳过。
    state["cursor"] = (start + max(attempted, 0)) % len(combos)
    state["rounds"] = int(state.get("rounds") or 0) + 1
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)

    print("\n" + "─" * 62)
    print(f"  完成：新增 {inserted} · 更新 {updated} · 失败 {failed}"
          + (f" · ⚠️缓存命中 {cache_hits}" if cache_hits else "")
          + (f" · 🛑风控 {risk_hit}" if risk_hit else ""))
    if cache_hits and cache_hits == len(picked) - failed:
        print("  ⚠️ 全部命中缓存 = 本轮【没有发送任何真实搜索请求】")
        print("     说明 session/stoken 可能已失效，或 CLI 认为结果还新鲜。")
    print(f"  游标 {state['cursor']}/{len(combos)} · 第 {state['rounds']} 轮")
    print("─" * 62)
    result = {"inserted": inserted, "updated": updated, "failed": failed,
              "cache_hits": cache_hits, "risk": risk_hit,
              "transient_fails": transient_fails, "attempted": attempted,
              "searches": len(picked), "cursor": state["cursor"]}
    emit("done", result)
    return result


def show_status() -> None:
    conn = store.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        print(f"岗位池总数: {total}")
        rows = conn.execute(
            "SELECT city, COUNT(*) FROM jobs GROUP BY city ORDER BY 2 DESC"
        ).fetchall()
        print("  城市:", " | ".join(f"{r[0] or '(空)'}:{r[1]}" for r in rows))
        src = conn.execute(
            "SELECT sources, COUNT(*) FROM jobs GROUP BY sources ORDER BY 2 DESC LIMIT 8"
        ).fetchall()
        print("  来源 Top8:")
        for s, n in src:
            print(f"    {n:>4}  {(s or '(空)')[:70]}")
        fresh = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE last_seen > ?", (time.time() - 86400,)
        ).fetchone()[0]
        print(f"  近 24h 见过的: {fresh}")
    finally:
        conn.close()

    s = load_state()
    print(f"\n收集游标: {s.get('cursor')} · 已跑 {s.get('rounds')} 轮 · 上次 {s.get('last_run')}")
    c = st.load().get("collect") or {}
    print("  配置关键词:", c.get("keywords") or DEFAULT_KEYWORDS)
    print("  配置城市  :", c.get("cities") or DEFAULT_CITIES)
    print("  每轮上限  :", c.get("max_searches_per_round") or DEFAULTS_COLLECT["max_searches_per_round"])


def main() -> int:
    ap = argparse.ArgumentParser(description="岗位自动收集器")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不真搜")
    ap.add_argument("--max", type=int, default=None, help="本轮最多搜索几次")
    ap.add_argument("--keywords", default="", help="逗号分隔，覆盖配置")
    ap.add_argument("--cities", default="", help="逗号分隔，覆盖配置")
    ap.add_argument("--status", action="store_true", help="只看池子现状，不收集")
    ap.add_argument("--source", choices=SOURCES, default=SOURCE_CLI,
                    help="采集通道：cli=固定走 boss-agent-cli（默认）；"
                         "dom=只走已登录浏览器的 DOM；auto=DOM 优先、不可用时回落 CLI")
    args = ap.parse_args()

    if args.status:
        show_status()
        return 0

    kws = [s.strip() for s in args.keywords.split(",") if s.strip()] or None
    cts = [s.strip() for s in args.cities.split(",") if s.strip()] or None
    r = collect(dry_run=args.dry_run, max_searches=args.max, keywords=kws, cities=cts,
                source=args.source)
    if r.get("error"):
        print("❌", r["error"])
        return 2
    if r.get("risk"):
        return 3     # 风控退出码，便于 cron 识别
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
    except Exception as e:                       # noqa: BLE001
        # 不把 traceback 甩给使用者：这不是他要解决的问题，是我们要修的
        print(f"\n❌ 出错了：{type(e).__name__}: {e}")
        print("   这是 jobkit 内部的问题，不需要你改代码。")
        print("   把上面这行发给维护者即可。")
        if os.environ.get("JOBKIT_DEBUG"):
            raise
        sys.exit(1)
