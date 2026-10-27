"""jobkit 投递配置 —— 黑白名单 + 投递选项，存 JSON，改完立即生效。

对标 GetJob「投递设置」的信息架构，配置项自研：
  blacklist.company / title / jd   命中即不投（三个字段独立维护）
  whitelist.title                  岗位名必须包含任一关键词才投（空=不过滤）
  options.*                        投递行为开关
  limits.daily_cap                 每日投递上限

设计原则：筛选规则只有一份，rules.py 从这里读 —— 避免界面改了规则不生效。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

HERE = Path(__file__).parent
PATH = HERE / "settings.json"

DEFAULTS: dict = {
    "blacklist": {
        "company": [],   # 公司名关键词
        "title": [],     # 岗位名关键词
        "jd": [],        # 职位描述关键词
    },
    "whitelist": {
        "title": [],     # 岗位名必须包含（任一）；空 = 不做白名单过滤
    },
    "options": {
        "custom_greeting": True,     # 发送自定义招呼语（关闭则用默认招呼）
        "only_active_hr": False,     # 只投 Boss 活跃的岗位
        "requirement_filter": True,  # 启用学历/经验/薪资过滤
        "log_float": True,           # 界面显示实时运行日志
        "exclude_weekend_work": False,  # 只投双休（数据源无此字段，暂不生效）
    },
    "limits": {
        "daily_cap": 130,
        # 薪资上限（元/月）：超过这个数的岗位基本够不着，直接挡掉省投递额度。
        # 目标区间 7-10K，留足上浮空间。
        "salary_max": 12000,
    },
    # ── 筛选条件（投递设置页可改）──────────────────────────────
    # 空列表/0 = 不限。这些是"投不投"的硬闸门，在 rules.job_ok 里生效。
    "filters": {
        "cities": [],              # 显式城市白名单；填了就只投这些（覆盖下面的分级逻辑）
        "core_cities": ["广州", "深圳"],  # 无条件投的城市；其余城市要有线上/远程信号
        "edu_ceiling": "",         # 只投「学历要求 ≤ 此值」的岗；空 = 不限
        "exp_ceiling": "",         # 只投「经验要求 ≤ 此值」的岗；空 = 不限
        "salary_min": 0,           # 月薪下限（元）
        "scales": [],              # 公司规模；空 = 不限
        "industries": [],          # 行业；空 = 不限
        "exclude_daily_pay": True, # 排除按天/按小时计薪的岗（日薪岗不是正式全职）
        # 「兼职」在黑名单里，但可远程的技术兼职放行（用户规则 2026-09-19）
        "allow_remote_parttime": True,
    },
    # ── 优先级排序（投递队列的顺序）────────────────────────────
    # mode=single 用 sort.single；mode=mixed 用 sort.keys 按权重加权。
    # 用户要求「按单个条件或混合条件排序」，所以两种都支持。
    "sort": {
        "mode": "mixed",
        "single": "match",
        "keys": [
            # 降维投递：岗位学历门槛越低，对本科生越容易，越该优先
            {"by": "edu_low_first", "weight": 30},
            {"by": "match", "weight": 40},
            {"by": "salary", "weight": 20},
            {"by": "boss_active", "weight": 10},
        ],
    },
    # ── 岗位自动收集 ────────────────────────────────────────────
    # 池子不增长，排序再准也只是在旧数据里挑。由 cron 定时跑 collector.py。
    "collect": {
        "enabled": True,
        "keywords": ["AI应用开发", "大模型", "Agent", "Python后端", "LLM", "智能体", "AI工程师", "算法应用"],
        "cities": ["广州", "深圳", "东莞", "佛山", "惠州"],
        "max_searches_per_round": 20,   # 每轮最多搜几次（单次约 19s + 间隔 25-45s）
        "min_interval_sec": 25,
        "max_interval_sec": 45,
    },
}

_lock = threading.Lock()
_cache: dict | None = None


def _merge(base: dict, over: dict) -> dict:
    """深合并：以 DEFAULTS 为骨架，补上用户配置里存在的键。

    这样配置文件少字段也能跑（老配置不会因新增选项而炸）。
    """
    out = {}
    for k, v in base.items():
        if isinstance(v, dict):
            out[k] = _merge(v, over.get(k) or {})
        else:
            out[k] = over.get(k, v)
    for k, v in over.items():          # 允许配置里出现骨架外的键
        if k not in out:
            out[k] = v
    return out


def load(force: bool = False) -> dict:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        over = {}
        if PATH.exists():
            try:
                over = json.loads(PATH.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                over = {}               # 配置损坏则回退默认，不阻塞流程
        _cache = _merge(DEFAULTS, over)
        return _cache


def save(cfg: dict) -> dict:
    global _cache
    with _lock:
        merged = _merge(DEFAULTS, cfg or {})
        PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        _cache = merged
        return merged


def reset(only_missing: bool = True) -> dict:
    """恢复默认。

    ⚠️ 血教训（2026-09-19）：原来直接 save(DEFAULTS) —— 我把它当"补全新键"用，
    结果把用户攒的黑名单/白名单**整个抹掉了**（验证脚本里调了两次，清空 67+15 条）。
    现在默认 only_missing=True：只补配置里【缺失】的键，绝不覆盖已有值。
    真要恢复出厂用 reset(only_missing=False)。
    """
    if not only_missing:
        return save(DEFAULTS)
    cur = load(force=True)
    merged = _merge(DEFAULTS, cur)      # 以 DEFAULTS 为骨架，已有值优先
    return save(merged)


def as_text_rules() -> tuple[list[str], list[str], list[str], list[str]]:
    """给 rules.py 用的快捷方式：(黑名单公司, 黑名单岗位, 黑名单JD, 白名单岗位)。"""
    c = load()
    bl, wl = c.get("blacklist", {}), c.get("whitelist", {})
    return (bl.get("company") or [], bl.get("title") or [], bl.get("jd") or [],
            wl.get("title") or [])
