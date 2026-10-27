"""投递后跟进引擎（纯逻辑，stdlib only）。

对标 GetJob 2.3.1 的 followup-logic / followup-engine 判定层（机制自研重写，不搬代码）：

  1) 跟进节奏 —— 投递满 3 天轻提醒（gentle），4-6 天礼貌追问（polite），
                 7 天及以上直接问结果（direct）；超过 TTL（默认 14 天）不再打扰
  2) 多轮跟进 —— 同一岗位最多跟 max_rounds 轮（默认 4），轮与轮之间按
                 冷却阶梯 12h → 24h → 48h → 72h 逐轮变长；跟满即停
  3) 发送时段 —— 只在本地 09:00-21:00 发，夜间攒着第二天再发
  4) 每日上限 —— 默认每天 20 条，超了截断（按投递时间最早的先发）
  5) 幂等落盘 —— 状态 JSON 原子写（tmp + replace），坏文件回退空结构不抛

多轮口径的出处（GetJob 2.3.1，只迁机制不抄代码）
    - 冷却阶梯取值：platforms/boss/followup-logic.js:11
      `FOLLOWUP_COOLDOWN_HOURS = [12, 24, 48, 72]`。★ 来源里它是**用户选择**的
      单一冷却值（`:28-31 normalizeCooldownHours` 只接受这四个值，非法回落 24），
      并不是按轮次自动递进；本模块把同一组常量当作**逐轮阶梯**使用，这是 jobkit
      的扩展，不是 GetJob 的原行为。
    - TTL：platforms/boss/followup-logic.js:6 `FOLLOWUP_TTL_DAYS = 14`，
      判定在 `:64-76 isFollowupInWindow` / `:423-429 followupLookupDecision`：
      **从投递时间（processed_at）算**，超过 14 天整岗不再跟进。本模块保持同一锚点
      （greeted_at = 投递时间），不改成「距上次跟进」。
    - 冷却锚点：来源 `:68-73` 用 processed_at 算 age；因来源每个岗位只跟一次
      （`:67 if (followupCountOf(row) > 0) return false`），冷却与 TTL 同锚点。
      多轮之后「上一轮发完到今天」才是下一轮该等的间隔，所以第 2 轮起冷却锚点
      改为上次跟进时间 last_ts；第 1 轮仍锚在投递时间。这是为多轮补的语义，
      已在 docs 与测试里锁死。
    - 轮次计数：来源 `:47-50 followupCountOf` 读 `followup_count`，但落库只写 1
      （data-service.js:1940/1952 `SET followup_count = 1`，查询条件
      `IFNULL(followup_count,0) = 0`），实际是「跟过没有」的布尔位。本模块把它
      升级为**真实轮次计数**（state.sent 里每个 job_id 的 round 最大值）。
    - 轮次上限：来源没有（单轮），本模块取 `max_rounds = len(冷却阶梯) = 4`，
      让四个阶梯值各用一次后停止，避免 14 天 TTL 内无限骚扰。

设计原则：
  - 全部纯函数 + 一个可持久化的小状态 dict，不碰网络、不碰浏览器、不读数据库，
    时间一律由调用方传 now_ms，便于用固定时间戳做单元测试。
  - 文案是确定性模板，只基于「投了简历」这一真实动作，不编造没发生过的事。
  - 不在这里调用 LLM；build_message 留了 llm 钩子，默认传 None 走模板。

用法：
    state = load_state("followup_state.json")
    ok, why = check_send_window(now_ms)
    todo = due_followups(rows, now_ms, state)      # rows = greetings 表行
    for item in todo:
        send(item["job_id"], item["message"])
        mark_sent(state, item["job_id"], now_ms, item["stage"], item["round"])
    save_state("followup_state.json", state)
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

DAY_MS = 24 * 3600 * 1000
HOUR_MS = 3600 * 1000

# ---------------------------------------------------------------- 默认参数

# 逐轮冷却阶梯（小时）。取值出处：GetJob followup-logic.js:11。
# 用法差异见模块 docstring：来源是单选值，这里是逐轮阶梯。
COOLDOWN_LADDER_HOURS = (12, 24, 48, 72)

DEFAULT_CFG: dict = {
    "gentle_days": 3,        # 投递满 3 天 → 轻提醒
    "polite_days": 4,        # 4 天起 → 礼貌追问
    "direct_days": 7,        # 7 天起 → 直接问结果
    "ttl_days": 14,          # 超过 14 天不再跟进（GetJob followup-logic.js:6 同款上限）
    "cooldown_ladder": COOLDOWN_LADDER_HOURS,   # 第 1..N 轮的冷却间隔（小时，递增）
    "max_rounds": len(COOLDOWN_LADDER_HOURS),   # 每岗最多跟进几轮
    "daily_limit": 20,       # 每日跟进条数上限
    "send_start_hour": 9,    # 发送时段（本地时间，含）
    "send_end_hour": 21,     # 发送时段（本地时间，不含）
    "max_state_entries": 200,
    "max_message_chars": 100,
}

STAGES = ("gentle", "polite", "direct")

# 平台容易判骚扰的词。模板本身不含，这里兜底拦住 LLM 钩子的输出
BANNED_WORDS = ("工资", "薪资", "薪水", "月薪", "年薪", "待遇", "多少钱", "涨薪", "底薪")

_TEMPLATES = {
    "gentle": "您好，我是之前投递「{title}」的候选人。想确认下简历您是否方便看看，需要补充材料我随时提供，谢谢。",
    "polite": "您好，再跟您确认一下「{title}」这个岗位的进展。如果还需要我补充信息，我随时配合，感谢您的时间。",
    "direct": "您好，想直接问一下「{title}」目前还有机会吗？如果已招满也麻烦告知，我好安排后续，谢谢您。",
}

_SAFE_FALLBACK = "您好，想跟您确认一下这个岗位的进展，方便的话请回复一下，谢谢。"


def _normalize_ladder(value) -> tuple[int, ...]:
    """冷却阶梯归一：只收正整数、必须逐轮不减；任何一条不合法就整体回落默认。

    对应来源 normalizeCooldownHours（followup-logic.js:28-31）的「不认识就回落」
    态度，但不要求取值必须等于 12/24/48/72 —— jobkit 允许配置更细的梯度。
    """
    if not isinstance(value, (list, tuple)):
        return COOLDOWN_LADDER_HOURS
    hours: list[int] = []
    for raw in value:
        try:
            h = int(raw)
        except (TypeError, ValueError):
            return COOLDOWN_LADDER_HOURS
        if h <= 0:
            return COOLDOWN_LADDER_HOURS
        if hours and h < hours[-1]:
            return COOLDOWN_LADDER_HOURS
        hours.append(h)
    return tuple(hours) if hours else COOLDOWN_LADDER_HOURS


def get_cfg(overrides: dict | None = None) -> dict:
    """合并默认参数与覆盖值。非法值回落到默认，不抛异常。"""
    cfg = dict(DEFAULT_CFG)
    if not overrides:
        return cfg
    for key, value in overrides.items():
        if key not in DEFAULT_CFG:
            cfg[key] = value          # 允许扩展字段，交给调用方
            continue
        default = DEFAULT_CFG[key]
        try:
            if isinstance(default, int):
                cfg[key] = max(0, int(value))
            else:
                cfg[key] = value
        except (TypeError, ValueError):
            cfg[key] = default
    # 逻辑阈值至少 1 天，否则跟进窗口自动塌成 0
    for key in ("gentle_days", "polite_days", "direct_days", "ttl_days"):
        cfg[key] = max(1, cfg[key])
    if cfg["polite_days"] < cfg["gentle_days"]:
        cfg["polite_days"] = cfg["gentle_days"]
    if cfg["direct_days"] < cfg["polite_days"]:
        cfg["direct_days"] = cfg["polite_days"]
    cfg["cooldown_ladder"] = _normalize_ladder(cfg.get("cooldown_ladder"))
    # 轮次上限不能超过阶梯长度：否则后面的轮次没有冷却值可用，会退化成连发
    cfg["max_rounds"] = max(1, min(cfg["max_rounds"], len(cfg["cooldown_ladder"])))
    cfg["max_state_entries"] = max(1, cfg["max_state_entries"])
    cfg["max_message_chars"] = max(20, cfg["max_message_chars"])
    cfg["send_start_hour"] = min(23, cfg["send_start_hour"])
    cfg["send_end_hour"] = min(24, cfg["send_end_hour"])
    return cfg


def cooldown_hours_for_round(round_no, cfg: dict | None = None) -> int:
    """第 round_no 轮（1 起）之前要冷却多少小时。

    阶梯用完后停在最后一档（72h），不会再变短。第 1 轮 = 12h，但在默认
    gentle_days=3 的首轮门槛之下不产生实际约束。
    """
    conf = get_cfg(cfg)
    ladder = conf["cooldown_ladder"]
    try:
        n = int(round_no)
    except (TypeError, ValueError):
        n = 1
    idx = min(max(1, n), len(ladder)) - 1
    return ladder[idx]


# ---------------------------------------------------------------- 时间工具

def _to_ms(value) -> int:
    """把 greetings.greeted_at 归一成毫秒。

    store.py 写库用 time.time()（秒），其它来源可能是毫秒，这里都认。
    """
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    if ts < 1e11:          # 秒（1e11 秒 ≈ 5138 年）
        ts *= 1000.0
    elif ts > 1e14:        # 微秒
        ts /= 1000.0
    return int(ts)


def _day_key(now_ms: int) -> str:
    """本地日期 YYYY-MM-DD（用于每日额度跨天重置）。"""
    return time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000.0))


def _hm(now_ms: int) -> str:
    return time.strftime("%H:%M", time.localtime(now_ms / 1000.0))


# ---------------------------------------------------------------- 分档

def pick_stage(greeted_at_ms, now_ms, cfg: dict | None = None) -> str:
    """按投递时长分档：<3 天 ''；3 天 gentle；4-6 天 polite；>=7 天 direct。

    cfg 可覆盖各档阈值（与 due_followups 用同一份配置，避免两处口径打架）。
    """
    conf = get_cfg(cfg)
    greeted = _to_ms(greeted_at_ms)
    try:
        now = int(now_ms)
    except (TypeError, ValueError):
        return ""
    if greeted <= 0 or now <= greeted:
        return ""
    days = (now - greeted) // DAY_MS
    if days >= conf["direct_days"]:
        return "direct"
    if days >= conf["polite_days"]:
        return "polite"
    if days >= conf["gentle_days"]:
        return "gentle"
    return ""


# ---------------------------------------------------------------- 文案

def _clean_text(value, limit: int = 24) -> str:
    """岗位名去空白、截断，避免模板被撑爆。"""
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


def has_banned_word(text: str) -> bool:
    raw = str(text or "")
    return any(word in raw for word in BANNED_WORDS)


def build_message(job: dict, stage: str, cfg: dict | None = None, llm=None,
                  round_no: int = 0) -> str:
    """确定性跟进话术（中文，≤ max_message_chars）。

    llm 是可选的 callable(job, stage) -> str；只在显式传入时调用，
    返回空/带骚扰词/超长时一律回落到模板。默认不碰任何模型，不花钱。
    round_no / cfg 目前只用于签名演进与将来的分轮文案，不影响模板选择。
    """
    conf = get_cfg(cfg)
    stage = stage if stage in STAGES else "polite"
    job = job if isinstance(job, dict) else {}
    title = _clean_text(job.get("title") or job.get("job_name") or "该岗位") or "该岗位"

    text = ""
    if callable(llm):
        try:
            text = str(llm(job, stage) or "").strip()
        except Exception:
            text = ""
    if not text:
        text = _TEMPLATES[stage].format(title=title)
    if has_banned_word(text):
        text = _SAFE_FALLBACK
        if title and title != "该岗位":
            text = f"您好，想确认一下「{title}」这个岗位的进展，方便的话请回复一下，谢谢。"
    return text[: conf["max_message_chars"]].strip()


# ---------------------------------------------------------------- 发送时段 / 额度

def check_send_window(now_ms, cfg: dict | None = None) -> tuple[bool, str]:
    """默认只允许本地 09:00-21:00 发送（左闭右开）。"""
    conf = get_cfg(cfg)
    try:
        now = int(now_ms)
    except (TypeError, ValueError):
        return False, "时间戳无效"
    clock = time.localtime(now / 1000.0)
    minutes = clock.tm_hour * 60 + clock.tm_min
    start = conf["send_start_hour"] * 60
    end = conf["send_end_hour"] * 60
    span = f"{conf['send_start_hour']:02d}:00-{conf['send_end_hour']:02d}:00"
    if start <= minutes < end:
        return True, f"在发送时段内（{span}）"
    return False, f"当前 {_hm(now)} 不在发送时段 {span}"


def daily_count(state: dict, now_ms) -> int:
    """今天已跟进条数；跨天或状态坏了都返回 0。"""
    if not isinstance(state, dict):
        return 0
    if str(state.get("day") or "") != _day_key(int(now_ms)):
        return 0
    try:
        count = int(state.get("count") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def remaining_quota(state: dict, now_ms, cfg: dict | None = None) -> int:
    conf = get_cfg(cfg)
    return max(0, conf["daily_limit"] - daily_count(state, now_ms))


# ---------------------------------------------------------------- 选人

def _sent_ids(state) -> set[str]:
    if not isinstance(state, dict):
        return set()
    ids: set[str] = set()
    for item in state.get("sent") or []:
        if isinstance(item, dict):
            job_id = str(item.get("job_id") or "").strip()
        else:
            job_id = str(item or "").strip()
        if job_id:
            ids.add(job_id)
    return ids


def _round_of(record) -> int:
    """状态记录 → 轮次；老状态文件没有 round 字段，按第 1 轮算。"""
    if not isinstance(record, dict):
        return 0
    try:
        n = int(record.get("round") or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def followup_history(state) -> dict[str, dict]:
    """状态 → 每个岗位的跟进进度（轮次 + 最近一次时间）。

    返回 {job_id: {"rounds": 已跟轮数, "last_ts": 最近一次跟进毫秒}}。
    轮数取该 job_id 下最大的 round，兼容「同岗多条记录」和手工改过的状态。
    """
    out: dict[str, dict] = {}
    if not isinstance(state, dict):
        return out
    for item in state.get("sent") or []:
        if isinstance(item, dict):
            job_id = str(item.get("job_id") or "").strip()
            if not job_id:
                continue
            ts = _to_ms(item.get("ts"))
            rounds = _round_of(item)
        else:
            job_id = str(item or "").strip()
            if not job_id:
                continue
            ts, rounds = 0, 1
        slot = out.setdefault(job_id, {"rounds": 0, "last_ts": 0})
        slot["rounds"] = max(slot["rounds"], rounds)
        if ts > slot["last_ts"]:
            slot["last_ts"] = ts
    return out


def split_by_reply(greeted_rows, replied_companies, matcher=None) -> dict:
    """把「对方已经回过话」的投递行从跟进候选里摘出来。

    为什么需要：followup 只看 greetings 表，不知道聊天里 HR 早就回过了 ——
    不摘就会对着已经回复的人继续催，这是最伤人的一种自动化失误。

    参数
    ----
    greeted_rows       greetings 表行（list[dict]）
    replied_companies  有 `from_me=0` 会话的公司名（list[str]）
    matcher            (job_company, replied_company) -> bool；
                       默认去掉空白后等值比较，调用方应传 `store.companies_match`
                       （BOSS 会话里的公司名比岗位库短，等值比会漏）

    返回
    ----
    {"active": [...原行...],
     "skipped": [{"job_id", "company", "title", "matched"}]}

    设计取舍：**缺 company 的行留在 active**。无法判断时不静默吞掉一条该跟进的投递。
    """
    replies = [str(x).strip() for x in (replied_companies or []) if str(x or "").strip()]
    match = matcher or (lambda a, b: str(a).strip() == str(b).strip())

    active: list = []
    skipped: list[dict] = []
    for row in greeted_rows or []:
        if not isinstance(row, dict):
            active.append(row)          # 脏行不参与判定，交回调用方
            continue
        company = str(row.get("company") or "").strip()
        hit = None
        if company and replies:
            for rc in replies:
                try:
                    if match(company, rc):
                        hit = rc
                        break
                except Exception:       # noqa: BLE001 —— matcher 是外部注入，不能连坐
                    continue
        if hit is None:
            active.append(row)
            continue
        skipped.append({
            "job_id": str(row.get("job_id") or ""),
            "company": company,
            "title": str(row.get("title") or ""),
            "matched": hit,
        })
    return {"active": active, "skipped": skipped}


def due_followups(greeted_rows, now_ms, state: dict | None = None,
                  cfg: dict | None = None) -> list[dict]:
    """挑出该跟进的投递，按投递时间从早到晚排序并按每日额度截断。

    greeted_rows 是 greetings 表行（list[dict]）：只认 status=sent、投递满
    gentle_days 天、未超 ttl_days（TTL 锚在投递时间，来源 followup-logic.js:74）、
    未跟满 max_rounds 轮、且距上一轮已过本轮冷却的岗位。

    返回项多了两个字段：round（本次是第几轮，1 起）与 rounds_done（已跟轮数），
    调用方 mark_sent 时原样回传即可。
    """
    conf = get_cfg(cfg)
    if not greeted_rows:
        return []
    try:
        now = int(now_ms)
    except (TypeError, ValueError):
        return []

    history = followup_history(state)
    quota = remaining_quota(state, now, conf)
    if quota <= 0:
        return []

    picked: list[dict] = []
    seen: set[str] = set()
    for row in greeted_rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status != "sent":
            continue
        job_id = str(row.get("job_id") or "").strip()
        if not job_id or job_id in seen:
            continue
        greeted = _to_ms(row.get("greeted_at"))
        if not greeted:
            continue
        age = now - greeted
        if age < conf["gentle_days"] * DAY_MS:
            continue
        # TTL 锚点是投递时间，不是上次跟进时间（来源 followup-logic.js:64-76）
        if age > conf["ttl_days"] * DAY_MS:
            continue

        prog = history.get(job_id) or {"rounds": 0, "last_ts": 0}
        rounds_done = prog["rounds"]
        if rounds_done >= conf["max_rounds"]:
            continue                        # 轮次用完 → 停止跟进
        next_round = rounds_done + 1
        anchor = prog["last_ts"] or greeted  # 第 2 轮起锚在上一轮
        if now - anchor < cooldown_hours_for_round(next_round, conf) * HOUR_MS:
            continue

        stage = pick_stage(greeted, now, conf)
        if not stage:
            continue
        seen.add(job_id)
        item = dict(row)
        item.update({
            "job_id": job_id,
            "greeted_at_ms": greeted,
            "age_days": age // DAY_MS,
            "round": next_round,
            "rounds_done": rounds_done,
            "stage": stage,
            "message": build_message(row, stage, conf, round_no=next_round),
        })
        picked.append(item)

    picked.sort(key=lambda it: (it["greeted_at_ms"], it["job_id"]))
    return picked[:quota]


# ---------------------------------------------------------------- 状态持久化

def _empty_state() -> dict:
    return {"sent": [], "day": "", "count": 0}


def _trim_sent(sent: list[dict], limit: int) -> list[dict]:
    """只留最近 limit 条（按 ts 升序取尾部），防止状态文件无限长。

    注意：按条数裁剪对「多轮」是安全的 —— 同岗的旧轮次被裁掉只会让轮次
    计数低估，due_followups 仍会受 TTL 限制，不会造成超发。
    """
    if len(sent) <= limit:
        return sent
    ordered = sorted(sent, key=lambda it: it.get("ts") or 0)
    return ordered[-limit:]


def load_state(path, *, strict: bool = False) -> dict:
    """兼容读取；执行器使用 strict=True，损坏/异常格式必须停止而非当作空历史。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError):
        if strict:
            raise
        return _empty_state()
    if not isinstance(data, dict):
        if strict:
            raise ValueError("跟进状态格式不正确")
        return _empty_state()
    if strict:
        raw_sent = data.get("sent", [])
        if (not isinstance(raw_sent, list)
                or any(not isinstance(item, dict) or not item.get("job_id") for item in raw_sent)
                or not isinstance(data.get("day", ""), str)
                or type(data.get("count", 0)) is not int or data.get("count", 0) < 0):
            raise ValueError("跟进历史格式不正确，需核对后迁移")
        pending = data.get("pending", {})
        if (not isinstance(pending, dict)
                or any(not key or not isinstance(item, dict)
                       or item.get("state") not in {"executing", "outcome_unknown"}
                       for key, item in pending.items())):
            raise ValueError("待核对状态格式不正确")

    sent: list[dict] = []
    for item in data.get("sent") or []:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("job_id") or "").strip()
        if not job_id:
            continue
        sent.append({
            "job_id": job_id,
            "ts": _to_ms(item.get("ts")),
            "stage": str(item.get("stage") or ""),
            "round": _round_of(item),
        })
    sent = _trim_sent(sent, DEFAULT_CFG["max_state_entries"])

    try:
        count = max(0, int(data.get("count") or 0))
    except (TypeError, ValueError):
        count = 0
    result = {
        "sent": sent,
        "day": str(data.get("day") or ""),
        "count": count,
    }
    pending = data.get("pending") or {}
    if not isinstance(pending, dict):
        if strict:
            raise ValueError("待核对状态格式不正确")
    elif pending:
        result["pending"] = pending
    return result


def save_state(path, state: dict) -> bool:
    """原子写状态（同目录 tmp + os.replace）。失败只返回 False，不抛。"""
    if not isinstance(state, dict):
        return False
    payload = {
        "sent": _trim_sent([
            {
                "job_id": str(it.get("job_id") or "").strip(),
                "ts": int(it.get("ts") or 0),
                "stage": str(it.get("stage") or ""),
                "round": _round_of(it),
            }
            for it in (state.get("sent") or [])
            if isinstance(it, dict) and str(it.get("job_id") or "").strip()
        ], DEFAULT_CFG["max_state_entries"]),
        "day": str(state.get("day") or ""),
        "count": max(0, int(state.get("count") or 0)),
    }
    if state.get("pending"):
        payload["pending"] = state["pending"]
    target = Path(path)
    tmp_path = None
    try:
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent or ".")
        )
        tmp_path = tmp_name
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
        return True
    except (OSError, TypeError, ValueError):
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def mark_sent(state: dict, job_id, now_ms, stage: str = "polite",
              round_no=None, cfg: dict | None = None) -> bool:
    """登记一轮已跟进，返回 True 表示真的记了一笔。

    幂等 / 防超发的三条门：
      1. 参数无效、轮次已满（>= max_rounds）→ False；
      2. 该轮次已经记过（round_no <= 已跟轮数）→ False；
      3. 距上次跟进还不够本轮冷却 → False（防止补偿重试把同一轮发两遍）。
    不传 round_no 时自动取「已跟轮数 + 1」。
    """
    conf = get_cfg(cfg)
    if not isinstance(state, dict):
        return False
    job_id = str(job_id or "").strip()
    if not job_id:
        return False
    try:
        now = int(now_ms)
    except (TypeError, ValueError):
        return False

    state.setdefault("sent", [])
    prog = followup_history(state).get(job_id) or {"rounds": 0, "last_ts": 0}
    rounds_done = prog["rounds"]
    if rounds_done >= conf["max_rounds"]:
        return False
    try:
        next_round = int(round_no) if round_no is not None else rounds_done + 1
    except (TypeError, ValueError):
        return False
    if next_round <= rounds_done:
        return False
    if prog["last_ts"] and now - prog["last_ts"] < cooldown_hours_for_round(
            next_round, conf) * HOUR_MS:
        return False

    day = _day_key(now)
    if str(state.get("day") or "") != day:
        state["day"] = day
        state["count"] = 0
    try:
        state["count"] = int(state.get("count") or 0) + 1
    except (TypeError, ValueError):
        state["count"] = 1

    state["sent"].append({
        "job_id": job_id,
        "ts": now,
        "stage": stage if stage in STAGES else "polite",
        "round": next_round,
    })
    state["sent"] = _trim_sent(state["sent"], DEFAULT_CFG["max_state_entries"])
    return True
