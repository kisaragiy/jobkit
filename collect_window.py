"""采集时间窗口控制 —— 只在晚上抓 JD。

为什么只在晚上抓
----------------
1. **岗位质量**：白天刷出来的多是热门岗位的重复曝光（一堆公司在同一个热词上竞价），
   晚上新上架的岗位里，中小公司 / 新开的 HC 比例更高，不那么千篇一律。
2. **成本**：远程模型在 9:00-18:00 是高峰价，同样的活晚上跑更便宜。

设计约束
--------
- 纯逻辑，不碰网络 / DB / 系统时间（now 显式传入），才能写确定性测试。
- **跨午夜**要正确处理：22:00→07:00 这个窗口横跨两天。
- 配置写错要抛错，不能静默回退到「全天都抓」——那会让夜间策略形同虚设。
"""
from __future__ import annotations

import re
from datetime import datetime

DEFAULT_START = "22:00"
DEFAULT_END = "07:00"

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(raw: str) -> int:
    """'22:00' → 1320（当天第几分钟）。非法输入抛 ValueError。"""
    m = _HHMM_RE.match(str(raw or "").strip())
    if not m:
        raise ValueError(f"时间格式应为 HH:MM，收到 {raw!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"时间超出范围：{raw!r}")
    return h * 60 + mi


def _minute_of_day(now: datetime) -> int:
    return now.hour * 60 + now.minute


def in_window(now: datetime | None = None,
              start: str = DEFAULT_START, end: str = DEFAULT_END) -> bool:
    """当前时刻是否落在采集窗口内。

    start <= end  → 当天内的区间（如 08:00-12:00）
    start >  end  → 跨午夜（如 22:00-07:00），这是本项目的默认值
    """
    now = now or datetime.now()
    s, e = parse_hhmm(start), parse_hhmm(end)
    cur = _minute_of_day(now)
    if s == e:
        return True          # 起止相同视为全天开放，避免「配错了就永远不跑」
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


def describe(start: str = DEFAULT_START, end: str = DEFAULT_END) -> str:
    """人话描述当前窗口，用于日志和界面提示。"""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return "全天"
    span = (e - s) % (24 * 60)
    return f"{start} → {end}（跨天 {span // 60}h{span % 60:02d}m）" if s > e \
        else f"{start} → {end}（{span // 60}h{span % 60:02d}m）"


def next_open(now: datetime | None = None,
              start: str = DEFAULT_START) -> datetime:
    """下次窗口开启的时间（用于「现在不抓，今晚 22:00 再跑」这种提示）。"""
    now = now or datetime.now()
    s = parse_hhmm(start)
    target = now.replace(hour=s // 60, minute=s % 60, second=0, microsecond=0)
    if target <= now:
        from datetime import timedelta
        target += timedelta(days=1)
    return target


def check_or_reason(now: datetime | None = None,
                    start: str = DEFAULT_START, end: str = DEFAULT_END) -> tuple[bool, str]:
    """返回 (是否可跑, 原因)。原因文案直接给用户看，别写成英文错误码。"""
    now = now or datetime.now()
    if in_window(now, start, end):
        return True, f"在采集窗口内（{describe(start, end)}）"
    nxt = next_open(now, start)
    return False, (f"现在是 {now:%H:%M}，不在采集窗口（{describe(start, end)}）；"
                   f"下次开启 {nxt:%m-%d %H:%M}。要立即抓加 --force")
