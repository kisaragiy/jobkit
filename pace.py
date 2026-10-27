"""投递节奏与熔断引擎（纯逻辑，stdlib only）。

对标 GetJob 2.3.1 的 pace-guard / anti-detect-config 策略层（逻辑自研重写，只搬机制不搬代码）：

  1) 令牌桶    —— 每分钟最多点开 N 个岗位详情（默认 10），超了按剩余时间等待
  2) 访问窗口  —— 滚动 60 分钟内最多访问 M 个岗位（默认 200，含过滤/失败/已沟通）
  3) 分层随机  —— T1 返回列表后 → 点下一条；T2 点沟通前；T3 发完消息后
  4) 熔断      —— 一次连点 N 个详情（默认 80，且 15 分钟内）或连续 DESC_MISS 次抓不到 JD
                 → 暂停 fuse_wait(n) 分钟，指数退避 5/8/13/21/34
  5) 受限页    —— 页面出现「访问受限 / 账户异常行为 / 操作过于频繁」→ 立即停，不重试

设计原则：
  - 全部纯函数 + 一个可持久化的小状态类（PaceState），不碰网络、不碰浏览器，
    这样可以用固定时间戳做单元测试，也不会让投递脚本因为逻辑错误静默跑飞。
  - 所有阈值都可 overrides 覆盖，默认值保守（比 BOSS 实际能承受的上限更低）。

用法：
    p = PaceState.load("pace_state.json")
    while p.window_remaining(now) > 0:
        p.wait_token(now)          # 令牌桶限速
        ...
        p.record("delivered")      # 或 filtered / failed / chatted
        p.save("pace_state.json")
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------- 默认参数

DEFAULT_PACE: dict = {
    # 令牌桶
    "DETAIL_PER_MINUTE": 10,
    "TOKEN_WINDOW_SECONDS": 60,
    # 全局熔断
    "DETAIL_PER_ROUND": 80,
    "DETAIL_FUSE_WITHIN_MINUTES": 15,
    "DESC_MISS_STREAK": 50,
    "FUSE_WAIT_MINUTES": [5, 8, 13, 21, 34],
    # 访问窗口
    "VISIT_WINDOW_MINUTES": 60,
    "VISIT_WINDOW_MAX": 200,
    # 分层随机等待（秒）
    "T1": (1.0, 6.0),          # 回到列表 → 点下一条
    "T2": (0.5, 2.0),          # 点「立即沟通」前
    "T3": (2.0, 3.5),          # 发完招呼语 → 返回
    "T3A": (1.0, 2.0),         # 发图前
    "T4": (1.0, 2.5),          # 返回列表 → 等列表刷新
    "LIST_FILTER_DELAY": (0.3, 0.8),    # 列表内过滤（没点开详情）
    "DETAIL_FILTER_DELAY": (3.0, 8.0),  # 读过 JD 再过滤（模拟看完）
    "PAGE_DELAY": (3.0, 8.0),           # 本页有过滤/跳过 → 翻页
    "PAGE_DELAY_SHORT": (1.0, 2.0),     # 本页干净 → 翻页
    # 受限页文案（命中即停）
    "RESTRICTED_PAGE_TEXT": [
        "访问受限",
        "您的账户存在异常行为",
        "操作过于频繁",
        "请勿频繁提交刷新请求",
        "账号存在风险",
    ],
}

# 访问记录的种类：过滤也算一次「看」，因为页面确实被打开了
VISIT_KINDS = ("filtered", "delivered", "failed", "chatted")
KIND_FILTERED = "filtered"
KIND_DELIVERED = "delivered"
KIND_FAILED = "failed"
KIND_CHATTED = "chatted"


def get_pace(overrides: dict | None = None) -> dict:
    """合并默认参数与覆盖值。非法值回落到默认，不抛异常。"""
    pace = dict(DEFAULT_PACE)
    if not overrides:
        return pace
    for key, value in overrides.items():
        if key not in DEFAULT_PACE:
            pace[key] = value  # 允许扩展字段，交给调用方
            continue
        default = DEFAULT_PACE[key]
        try:
            if isinstance(default, list) and key == "FUSE_WAIT_MINUTES":
                vals = [max(1, int(v)) for v in value]
                pace[key] = vals or list(default)
            elif isinstance(default, tuple) and isinstance(value, (list, tuple)) and len(value) == 2:
                lo, hi = float(value[0]), float(value[1])
                pace[key] = (min(lo, hi), max(lo, hi))
            elif isinstance(default, int):
                pace[key] = max(0, int(value))
            else:
                pace[key] = value
        except (TypeError, ValueError):
            pace[key] = default
    return pace


# ---------------------------------------------------------------- 基础工具

def jitter_seconds(range_: Iterable[float], rng: random.Random | None = None) -> float:
    """在 [min, max] 间取随机秒数。"""
    lo, hi = float(range_[0]), float(range_[1])
    if hi < lo:
        lo, hi = hi, lo
    r = rng or random
    return round(lo + r.random() * (hi - lo), 3)


def fmt_seconds(sec: float) -> str:
    sec = max(0.0, float(sec))
    if sec < 60:
        return f"{sec:.1f}s"
    total = int(round(sec))
    return f"{total // 60}分{total % 60}秒"


def text_hits_restricted(text: str, pace: dict | None = None) -> str | None:
    """页面文本命中受限文案时返回命中的那句，否则 None。"""
    if not text:
        return None
    p = pace or DEFAULT_PACE
    for phrase in p.get("RESTRICTED_PAGE_TEXT", []):
        if phrase and phrase in text:
            return phrase
    return None


# ---------------------------------------------------------------- 纯判定函数

def token_wait_ms(clicks: list[float], now_ms: float, pace: dict | None = None) -> float:
    """令牌桶还要等多少毫秒才能再点一个详情。0 表示可以立刻点。

    clicks: 最近点开详情的时间戳（ms，升序或乱序都行）
    """
    p = pace or DEFAULT_PACE
    window_ms = max(1, int(p["TOKEN_WINDOW_SECONDS"])) * 1000
    limit = max(1, int(p["DETAIL_PER_MINUTE"]))
    recent = sorted(ts for ts in clicks if ts and (now_ms - ts) < window_ms)
    if len(recent) < limit:
        return 0.0
    oldest = recent[0]
    return max(1.0, window_ms - (now_ms - oldest))


def should_fuse(
    clicks: int,
    desc_miss_streak: int,
    started_at_ms: float | None,
    now_ms: float,
    pace: dict | None = None,
) -> dict:
    """是否需要熔断。

    返回 {"fuse": bool, "kind": "desc"|"clicks"|None, "wait_minutes": int}
      - desc:   连续 desc_miss_streak 次没抓到 JD（可能被限流/风控）
      - clicks: 一轮内点开详情过多且仍在 fuse 时间窗内
    """
    p = pace or DEFAULT_PACE
    clicked = max(0, int(clicks))
    streak = max(0, int(desc_miss_streak))
    desc_fuse = streak >= int(p["DESC_MISS_STREAK"])

    click_fuse = False
    if clicked >= int(p["DETAIL_PER_ROUND"]) and started_at_ms is not None:
        window_ms = int(p["DETAIL_FUSE_WITHIN_MINUTES"]) * 60 * 1000
        click_fuse = (now_ms - float(started_at_ms)) < window_ms

    if desc_fuse:
        return {"fuse": True, "kind": "desc", "wait_minutes": fuse_wait_minutes(1, p)}
    if click_fuse:
        return {"fuse": True, "kind": "clicks", "wait_minutes": fuse_wait_minutes(1, p)}
    return {"fuse": False, "kind": None, "wait_minutes": 0}


def fuse_wait_minutes(n: int, pace: dict | None = None) -> int:
    """第 n 次连续熔断等待几分钟：5 → 8 → 13 → 21 → 34，之后停在最后一级。"""
    p = pace or DEFAULT_PACE
    waits = p.get("FUSE_WAIT_MINUTES") or DEFAULT_PACE["FUSE_WAIT_MINUTES"]
    idx = min(max(int(n), 1), len(waits)) - 1
    return int(waits[idx])


def prune_visits(visits: list[dict], now_ms: float, pace: dict | None = None) -> list[dict]:
    """丢掉滚动窗口之外的访问记录。"""
    p = pace or DEFAULT_PACE
    window_ms = max(1, int(p["VISIT_WINDOW_MINUTES"])) * 60 * 1000
    return [v for v in visits if (now_ms - float(v.get("ts", 0) or 0)) <= window_ms]


def window_stats(visits: list[dict], now_ms: float, pace: dict | None = None) -> dict:
    """滚动窗口内的访问统计 + 下一个可投时刻。"""
    p = pace or DEFAULT_PACE
    window_ms = max(1, int(p["VISIT_WINDOW_MINUTES"])) * 60 * 1000
    recent = prune_visits(visits, now_ms, p)
    counts = {kind: 0 for kind in VISIT_KINDS}
    for v in recent:
        kind = v.get("kind")
        if kind in counts:
            counts[kind] += 1
        else:
            counts[KIND_FILTERED] += 1
    limit = max(1, int(p["VISIT_WINDOW_MAX"]))
    total = len(recent)
    next_at = now_ms
    if total >= limit and recent:
        oldest = min(float(v.get("ts", 0) or 0) for v in recent)
        next_at = oldest + window_ms
    return {
        "window_minutes": int(p["VISIT_WINDOW_MINUTES"]),
        "limit": limit,
        "total": total,
        "remaining": max(0, limit - total),
        "counts": counts,
        "next_at_ms": next_at,
        "wait_ms": max(0.0, next_at - now_ms),
    }


# ---------------------------------------------------------------- 状态类

@dataclass
class PaceState:
    """可持久化的节奏状态。投递脚本每投一条 record() 一次，结束 save()。"""

    pace: dict = field(default_factory=get_pace)
    visits: list[dict] = field(default_factory=list)      # [{ts, kind}]
    clicks: list[float] = field(default_factory=list)     # 详情点击时间戳（令牌桶）
    desc_miss_streak: int = 0
    round_clicks: int = 0
    round_started_at: float | None = None
    fuse_count: int = 0
    fuse_until_ms: float = 0.0

    # -- 查询 --

    def window(self, now_ms: float | None = None) -> dict:
        return window_stats(self.visits, self._now(now_ms), self.pace)

    def window_remaining(self, now_ms: float | None = None) -> int:
        return self.window(now_ms)["remaining"]

    def token_wait(self, now_ms: float | None = None) -> float:
        return token_wait_ms(self.clicks, self._now(now_ms), self.pace)

    def fuse_remaining_ms(self, now_ms: float | None = None) -> float:
        return max(0.0, self.fuse_until_ms - self._now(now_ms))

    def is_blocked(self, now_ms: float | None = None) -> bool:
        """窗口满 或 熔断中 → 现在不该投。"""
        now = self._now(now_ms)
        return self.window_remaining(now) <= 0 or self.fuse_remaining_ms(now) > 0

    def next_action_at(self, now_ms: float | None = None) -> float:
        """下一次可以动手的最早时间戳（ms）。"""
        now = self._now(now_ms)
        w = self.window(now)
        return max(self.fuse_until_ms, w["next_at_ms"], now)

    # -- 变更 --

    def begin_round(self, now_ms: float | None = None) -> None:
        self.round_clicks = 0
        self.round_started_at = self._now(now_ms)

    def record_click(self, now_ms: float | None = None, desc_ok: bool = True) -> None:
        """点开一个详情。desc_ok=False 记一次 JD 抓取失败。"""
        now = self._now(now_ms)
        self.clicks.append(now)
        self.round_clicks += 1
        if self.round_started_at is None:
            self.round_started_at = now
        if desc_ok:
            self.desc_miss_streak = 0
        else:
            self.desc_miss_streak += 1
        self._prune_clicks(now)

    def record(self, kind: str, now_ms: float | None = None) -> None:
        """记一次访问结果：filtered / delivered / failed / chatted。"""
        if kind not in VISIT_KINDS:
            kind = KIND_FILTERED
        now = self._now(now_ms)
        self.visits.append({"ts": now, "kind": kind})
        self._prune_visits(now)

    def apply_fuse(self, now_ms: float | None = None) -> dict:
        """检查并触发熔断。返回判定结果；触发时清空本轮计数。"""
        now = self._now(now_ms)
        verdict = should_fuse(
            self.round_clicks, self.desc_miss_streak, self.round_started_at, now, self.pace
        )
        if verdict["fuse"]:
            self.fuse_count += 1
            wait_ms = fuse_wait_minutes(self.fuse_count, self.pace) * 60 * 1000
            self.fuse_until_ms = now + wait_ms
            self.round_clicks = 0
            self.round_started_at = None
            self.desc_miss_streak = 0
            verdict["wait_minutes"] = fuse_wait_minutes(self.fuse_count, self.pace)
            verdict["until_ms"] = self.fuse_until_ms
        return verdict

    def reset_fuse_counter(self) -> None:
        """一轮顺利跑完后手动清零，避免下次偶发熔断直接跳到 34 分钟。"""
        self.fuse_count = 0

    # -- 持久化 --

    def to_dict(self) -> dict:
        return {
            "visits": self.visits,
            "clicks": self.clicks,
            "desc_miss_streak": self.desc_miss_streak,
            "round_clicks": self.round_clicks,
            "round_started_at": self.round_started_at,
            "fuse_count": self.fuse_count,
            "fuse_until_ms": self.fuse_until_ms,
        }

    def save(self, path: str) -> None:
        """原子写：先写 .tmp 再替换，避免半截 JSON 让下次启动直接崩。"""
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str, overrides: dict | None = None) -> "PaceState":
        state = cls(pace=get_pace(overrides))
        if not os.path.exists(path):
            return state
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return state  # 坏文件当空状态，不让投递任务起不来
        state.visits = [v for v in raw.get("visits", []) if isinstance(v, dict)]
        state.clicks = [float(x) for x in raw.get("clicks", []) if isinstance(x, (int, float))]
        state.desc_miss_streak = int(raw.get("desc_miss_streak", 0) or 0)
        state.round_clicks = int(raw.get("round_clicks", 0) or 0)
        state.round_started_at = raw.get("round_started_at")
        state.fuse_count = int(raw.get("fuse_count", 0) or 0)
        state.fuse_until_ms = float(raw.get("fuse_until_ms", 0) or 0)
        return state

    # -- 内部 --

    @staticmethod
    def _now(now_ms: float | None) -> float:
        return float(now_ms) if now_ms is not None else time.time() * 1000

    def _prune_clicks(self, now_ms: float) -> None:
        window_ms = max(1, int(self.pace["TOKEN_WINDOW_SECONDS"])) * 1000
        self.clicks = [ts for ts in self.clicks if (now_ms - ts) < window_ms]

    def _prune_visits(self, now_ms: float) -> None:
        self.visits = prune_visits(self.visits, now_ms, self.pace)
