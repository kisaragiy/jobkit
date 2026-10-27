"""定时投递槽位调度 —— 时段校验、到点判定、额度估算、重叠提示（纯逻辑，不接 UI / 不接投递）。

对标 GetJob 的 delivery-schedule.js，语义保持一致：
  · 槽位（slot）要么是「每天某个时刻」（repeat=daily），要么是「某天某个时刻」（repeat=once）
  · 到点判定用「错过窗口」：超过 5 分钟还没执行的槽位不再算待执行，
    避免开机晚了才发现、又跑一波把额度和风控都打满
  · 额度估算按单岗保守墙钟（纯文字 45 秒 / 图片简历 90 秒）+ 3 分钟缓冲，
    只用于建议时间和重叠提示，不是硬上限

时间处理：所有判定函数都走显式 now_ms（毫秒时间戳，本地时区），
模块内不取系统时间 —— 否则没法写确定性测试。
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

# ── 常量（与 delivery-schedule.js 对齐）────────────────────────────
MISS_WINDOW_MS = 5 * 60 * 1000      # 错过窗口：迟到超过 5 分钟不再待执行
MAX_SLOTS = 24                       # 一天最多几个时段
MAX_RUNS = 50                        # runs 只留最近 50 条
DEFAULT_SLOT_QUANTITY = 30           # 新建时段的默认投递数
EST_SECONDS_PER_JOB_TEXT = 45        # 纯文字单岗保守墙钟（秒）
EST_SECONDS_PER_JOB_IMAGE = 90       # 带图片简历时单岗保守墙钟（秒）
EST_BUFFER_MINUTES = 3               # 估算缓冲
EST_MIN_MINUTES = 3                  # 估算下限

REPEAT_ONCE = "once"
REPEAT_DAILY = "daily"

SCHEDULE_PATH = Path(__file__).parent / "schedule.json"

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{1,2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


# ── 时间小工具（全部本地时区，不碰 UTC）────────────────────────────
def _local_dt(ms) -> datetime:
    """毫秒时间戳 → 本地 naive datetime。"""
    return datetime.fromtimestamp(float(ms) / 1000.0)


def _dt_to_ms(dt: datetime) -> int:
    """本地 naive datetime → 毫秒时间戳（取整，避免浮点尾巴）。"""
    return int(round(dt.timestamp() * 1000))


def _date_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def parse_hhmm(raw) -> str | None:
    """清洗 '9:5' / '09:30' → '09:30'；非法返回 None（不抛）。"""
    m = _HHMM_RE.match(str(raw or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


def parse_date(raw) -> str | None:
    """清洗 YYYY-MM-DD；日期不存在（如 2026-02-30）也返回 None。"""
    m = _DATE_RE.match(str(raw or "").strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        dt = datetime(y, mo, d)
    except ValueError:
        return None
    return _date_key(dt)


def _hhmm_minutes(hhmm: str) -> int:
    h, mi = hhmm.split(":")
    return int(h) * 60 + int(mi)


def _minutes_to_hhmm(minutes: int) -> str:
    wrapped = minutes % 1440
    return f"{wrapped // 60:02d}:{wrapped % 60:02d}"


def _circular_gap_minutes(from_min: int, to_min: int) -> int:
    """一天内的环形间隔：23:50 → 00:10 算 20 分钟。"""
    d = to_min - from_min
    if d <= 0:
        d += 1440
    return d


# ── 槽位构造与清洗 ─────────────────────────────────────────────
def new_slot_id() -> str:
    """短 id，够本地文件用，不需要全局唯一。"""
    suffix = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    return f"s-{os.urandom(3).hex()}-{suffix}"


def new_slot(time: str = "09:30", quantity: int = DEFAULT_SLOT_QUANTITY,
             repeat: str = REPEAT_DAILY, date: str | None = None) -> dict:
    """新建一个槽位。repeat=once 必须给 date（YYYY-MM-DD）。

    这里是「程序内部构造」，参数非法直接抛 ValueError —— 用户输入的清洗走
    normalize_slot()，那边只返回 None，不抛。
    """
    t = parse_hhmm(time)
    if t is None:
        raise ValueError(f"时间格式必须是 HH:mm：{time!r}")
    if repeat not in (REPEAT_ONCE, REPEAT_DAILY):
        raise ValueError(f"repeat 只能是 once / daily：{repeat!r}")
    if repeat == REPEAT_ONCE:
        d = parse_date(date)
        if d is None:
            raise ValueError(f"一次性时段必须给合法日期 YYYY-MM-DD：{date!r}")
    else:
        d = parse_date(date) if date else None
    q = _clean_quantity(quantity)
    if q is None:
        raise ValueError(f"quantity 必须是 >=0 的整数：{quantity!r}")
    slot = {
        "id": new_slot_id(),
        "time": t,
        "repeat": repeat,
        "quantity": q,
        "enabled": 1,
        "image_resume": 0,
    }
    if d:
        slot["date"] = d
    return slot


def _clean_quantity(raw) -> int | None:
    """字符串数字也认；负数/非数字 → None；小数向下取整。"""
    if isinstance(raw, bool):
        return None
    try:
        n = float(str(raw).strip()) if not isinstance(raw, (int, float)) else float(raw)
    except (TypeError, ValueError):
        return None
    if n != n or n < 0:          # NaN 或负数
        return None
    return int(n)


def normalize_slot(raw) -> dict | None:
    """清洗一个用户输入的槽位；非法返回 None，绝不抛异常。

    非法判定：时间不是 HH:mm、once 缺日期/日期不存在、quantity 非数字或为负。
    暂停的槽位（enabled=0）照样返回 —— 是否参与执行由调度侧决定。
    """
    if not isinstance(raw, dict):
        return None
    t = parse_hhmm(raw.get("time"))
    if t is None:
        return None
    q = _clean_quantity(raw.get("quantity", 0))
    if q is None:
        return None
    repeat = REPEAT_DAILY if raw.get("repeat") == REPEAT_DAILY else REPEAT_ONCE
    slot: dict = {
        "id": str(raw.get("id") or "").strip() or new_slot_id(),
        "time": t,
        "repeat": repeat,
        "quantity": q,
        "enabled": 0 if raw.get("enabled") in (0, False, "0") else 1,
        "image_resume": 1 if raw.get("image_resume") in (1, True, "1") else 0,
    }
    if repeat == REPEAT_ONCE:
        d = parse_date(raw.get("date"))
        if d is None:
            return None
        slot["date"] = d
    else:
        # daily 的 date 不参与判定，留着的旧值只做兼容，别拿它当准
        d = parse_date(raw.get("date"))
        if d:
            slot["date"] = d
    last = raw.get("last_run_ms")
    if isinstance(last, (int, float)) and last > 0:
        slot["last_run_ms"] = int(last)
    return slot


def slot_when_label(slot: dict) -> str:
    """给提示文案用的「每天 09:30」「2026-10-20 09:30」。"""
    time = parse_hhmm(slot.get("time")) or "--:--"
    if slot.get("repeat") == REPEAT_DAILY:
        return f"每天 {time}"
    return f"{slot.get('date') or ''} {time}".strip()


# ── 到点判定 ─────────────────────────────────────────────────
def _is_daily(slot) -> bool:
    return bool(slot) and slot.get("repeat") == REPEAT_DAILY


def _is_enabled(slot) -> bool:
    """没这个字段的历史数据按启用处理。"""
    if not slot:
        return False
    return slot.get("enabled") not in (0, False, "0")


def slot_occurrence_ms(slot, now_ms) -> float | None:
    """该槽位「相对 now 所在这一天」的执行时刻（毫秒）。

    daily：今天（本地）的 HH:mm，可能已经过去；
    once ：所存日期当天的 HH:mm。
    时间/日期非法返回 None。
    """
    if not isinstance(slot, dict):
        return None
    t = parse_hhmm(slot.get("time"))
    if t is None:
        return None
    h, mi = (int(x) for x in t.split(":"))
    now = _local_dt(now_ms)
    if _is_daily(slot):
        return _dt_to_ms(datetime(now.year, now.month, now.day, h, mi))
    d = parse_date(slot.get("date"))
    if d is None:
        return None
    day = datetime.fromisoformat(d)
    return _dt_to_ms(datetime(day.year, day.month, day.day, h, mi))


def _next_occurrence_ms(slot, now_ms) -> float | None:
    """严格「还没过错过窗口」的下一次执行时刻。

    daily：今天时刻若还在错过窗口内（含刚到点）就用今天，否则顺延到明天；
    once ：日期时刻已过错过窗口（或从未）→ None。
    """
    base = slot_occurrence_ms(slot, now_ms)
    if base is None:
        return None
    if base >= now_ms - MISS_WINDOW_MS:
        return base
    if not _is_daily(slot):
        return None
    t = parse_hhmm(slot["time"])
    h, mi = (int(x) for x in t.split(":"))
    now = _local_dt(now_ms)
    tomorrow = now + timedelta(days=1)
    return _dt_to_ms(datetime(tomorrow.year, tomorrow.month, tomorrow.day, h, mi))


def _is_done(slot: dict, occurrence_ms: float, runs=None) -> bool:
    """该次发生是否已经执行过（槽位上的 last_run_ms 或 runs 里的一条记录）。"""
    last = slot.get("last_run_ms")
    if isinstance(last, (int, float)) and last >= occurrence_ms:
        return True
    for run in runs or []:
        if not isinstance(run, dict) or run.get("slot_id") != slot.get("id"):
            continue
        at = run.get("at_ms")
        if isinstance(at, (int, float)) and occurrence_ms <= at <= occurrence_ms + MISS_WINDOW_MS:
            return True
    return False


def next_due(slots, now_ms) -> dict | None:
    """最近一个「到点或未来」的槽位；没有则 None。

    返回 {"slot", "slot_id", "at_ms", "wait_seconds", "due"}：
    wait_seconds 恒 >= 0；刚过点还在窗口内的 due=True、wait_seconds=0。
    """
    best = None
    for slot in slots or []:
        if not _is_enabled(slot):
            continue
        occ = _next_occurrence_ms(slot, now_ms)
        if occ is None:
            continue
        if best is None or occ < best["at_ms"]:
            due = occ <= now_ms
            best = {
                "slot": slot,
                "slot_id": slot.get("id"),
                "at_ms": occ,
                "wait_seconds": 0 if due else int(round((occ - now_ms) / 1000.0)),
                "due": due,
            }
    return best


def pending_slots(slots, now_ms, runs=None) -> list[dict]:
    """已到点、还在错过窗口内、且还没执行过的槽位（按到点时间排序）。

    迟到超过 5 分钟（now - occ > MISS_WINDOW_MS）不算待执行 —— 宁可不投，
    也不在用户不知情的时候补跑一波。
    """
    out = []
    for slot in slots or []:
        if not _is_enabled(slot):
            continue
        occ = _next_occurrence_ms(slot, now_ms)
        if occ is None or occ > now_ms:
            continue
        if now_ms - occ > MISS_WINDOW_MS:
            continue
        if _is_done(slot, occ, runs):
            continue
        out.append({
            "slot": slot,
            "slot_id": slot.get("id"),
            "at_ms": occ,
            "late_seconds": int(round((now_ms - occ) / 1000.0)),
        })
    out.sort(key=lambda row: (row["at_ms"], str(row["slot_id"])))
    return out


# ── 额度估算与重叠提示 ─────────────────────────────────────────
def estimate_minutes(quantity, image_resume: bool = False) -> int:
    """一波大约要跑多久（分钟，偏慢估计）。

    ceil(岗位数 × 单岗秒数 / 60) + 3 分钟缓冲，下限 3 分钟。
    quantity=0 也返回 3 —— 缓冲和启动开销是固定的。
    """
    q = _clean_quantity(quantity)
    if q is None:
        q = 0
    per_job = EST_SECONDS_PER_JOB_IMAGE if image_resume else EST_SECONDS_PER_JOB_TEXT
    raw = -(-(q * per_job) // 60) + EST_BUFFER_MINUTES   # 整数向上取整
    return max(EST_MIN_MINUTES, raw)


def _slot_duration_minutes(slot: dict) -> int:
    return estimate_minutes(slot.get("quantity", 0), bool(slot.get("image_resume")))


def _pair_gap_minutes(a: dict, b: dict, now_ms) -> float:
    """两个时段的间隔（分钟）。

    都是 once → 按绝对时间比；只要有一方 daily → 按一天内的时刻环形比
    （每天重复的槽位跨日期比较没有意义）。
    """
    if not _is_daily(a) and not _is_daily(b):
        ma = slot_occurrence_ms(a, now_ms)
        mb = slot_occurrence_ms(b, now_ms)
        if ma is None or mb is None:
            return float("inf")
        return abs(ma - mb) / 60000.0
    ta, tb = parse_hhmm(a.get("time")), parse_hhmm(b.get("time"))
    if ta is None or tb is None:
        return float("inf")
    ma, mb = _hhmm_minutes(ta), _hhmm_minutes(tb)
    d = abs(ma - mb)
    return float(min(d, 1440 - d))


def _order_pair(a: dict, b: dict, now_ms) -> tuple[dict, dict]:
    """返回 (先发生的, 后发生的)。"""
    if not _is_daily(a) and not _is_daily(b):
        ma = slot_occurrence_ms(a, now_ms)
        mb = slot_occurrence_ms(b, now_ms)
        if ma is None:
            return b, a
        if mb is None:
            return a, b
        return (a, b) if ma <= mb else (b, a)
    ta, tb = parse_hhmm(a.get("time")), parse_hhmm(b.get("time"))
    if ta is None:
        return b, a
    if tb is None:
        return a, b
    ma, mb = _hhmm_minutes(ta), _hhmm_minutes(tb)
    return (a, b) if _circular_gap_minutes(ma, mb) <= _circular_gap_minutes(mb, ma) else (b, a)


def _participates_in_overlap(slot: dict, now_ms) -> bool:
    """仅一次且预估窗口已结束的时段不再参与重叠提示（已经跑完了）。"""
    if not _is_enabled(slot) or parse_hhmm(slot.get("time")) is None:
        return False
    if _is_daily(slot):
        return True
    start = slot_occurrence_ms(slot, now_ms)
    if start is None:
        return False
    return start + _slot_duration_minutes(slot) * 60000 > now_ms


def overlap_warning(slots, now_ms=None) -> list[str]:
    """时段重叠提示：上一波按保守估计还没跑完，下一波就到点了。

    只挂在较晚的那一波上（谁被打断提示谁），同一波最多一条。没有重叠返回 []。
    now_ms 只用于判断「已跑完的 once 槽位」是否还参与，默认取当前时间。
    """
    if now_ms is None:
        now_ms = _dt_to_ms(datetime.now())
    active = [s for s in (slots or []) if _participates_in_overlap(s, now_ms)]
    warnings: dict[str, dict] = {}
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if _pair_gap_minutes(a, b, now_ms) <= 0:
                continue          # 同一时刻由 validate 挡，不算「重叠」
            earlier, later = _order_pair(a, b, now_ms)
            gap = _pair_gap_minutes(earlier, later, now_ms)
            dur = _slot_duration_minutes(earlier)
            if gap >= dur:
                continue
            key = str(later.get("id"))
            if key not in warnings or gap < warnings[key]["gap"]:
                warnings[key] = {"later": later, "earlier": earlier, "gap": gap, "dur": dur}

    out = []
    for row in warnings.values():
        earlier, later = row["earlier"], row["later"]
        qty = max(0, int(earlier.get("quantity") or 0))
        gap = max(1, int(round(row["gap"])))
        if _is_daily(earlier) and _is_daily(later):
            out.append(f"「每天 {later['time']}」与上一波间隔 {gap} 分钟，"
                       f"上一波 {qty} 岗按估算约需 {row['dur']} 分钟，到点会打断上一波")
        else:
            out.append(f"「{slot_when_label(later)}」距上一波 {gap} 分钟，"
                       f"按 {qty} 岗估算约需 {row['dur']} 分钟，到点可能打断上一波")
    return out


# ── 持久化 ───────────────────────────────────────────────────
def _empty() -> dict:
    return {"slots": [], "runs": []}


def empty_schedule() -> dict:
    """空结构（给调用方做初值，避免各写一份字面量）。"""
    return _empty()


def load(path=SCHEDULE_PATH) -> dict:
    """读槽位文件。文件不存在/坏 JSON/结构不对 → 空结构，不抛。

    单个槽位清洗失败只丢那一条，不连坐整个文件。
    """
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    slots = []
    for item in raw.get("slots") or []:
        slot = normalize_slot(item)
        if slot:
            slots.append(slot)
    runs = [r for r in (raw.get("runs") or []) if isinstance(r, dict)][-MAX_RUNS:]
    return {"slots": slots, "runs": runs}


def save(path, data) -> dict:
    """原子写（.tmp + os.replace），避免半截文件。

    写入前顺手清洗槽位、裁掉多余 runs；返回真正落盘的结构。
    """
    p = Path(path)
    src = data if isinstance(data, dict) else {}
    slots = []
    for item in src.get("slots") or []:
        slot = normalize_slot(item)
        if slot:
            slots.append(slot)
    runs = [r for r in (src.get("runs") or []) if isinstance(r, dict)][-MAX_RUNS:]
    payload = {"slots": slots, "runs": runs}

    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return payload


def mark_run(slot_id, now_ms, sent, note="", data=None, path=SCHEDULE_PATH) -> dict:
    """记录一次执行。

    data 给了就原地追加（并回写该槽位的 last_run_ms），没给就读 path、写回 path。
    runs 只留最近 50 条。返回写入的那条记录。
    """
    run = {
        "slot_id": str(slot_id or ""),
        "at_ms": int(now_ms),
        "sent": 1 if sent else 0,
        "note": str(note or ""),
    }
    owns = data is None
    work = load(path) if owns else data
    runs = [r for r in (work.get("runs") or []) if isinstance(r, dict)]
    runs.append(run)
    work["runs"] = runs[-MAX_RUNS:]
    for slot in work.get("slots") or []:
        if slot.get("id") == run["slot_id"]:
            slot["last_run_ms"] = run["at_ms"]
    if owns:
        save(path, work)
    return run
