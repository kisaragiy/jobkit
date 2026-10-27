"""发送台账：把「点过发送」与「核对过结果」分开记账。

为什么需要它：`real_submit` 只发一次鼠标点击就返回 `submit_requested`，不等回显——
这是对的（等响应会超时误判）。但如果没有一本账记住「这一条还没核对」，
下一次登记授权时系统就完全不知道 17 秒前已经点过一次，于是重发。

本模块是纯逻辑（无 IO、无浏览器），只回答两个问题：
1. 这个对象现在还能不能再发？（未知未核对 = 不能）
2. 这条点击的结果归到哪个状态？

底线：
- `submitting` / `submitted_unverified` / `outcome_unknown` / `conflict` 都是**未闭环**，
  未闭环期间同一对象不得再次发送，且整个 scope 的外部写入都要人工核对后才能继续。
- 只有「观察到回显」或「人工核对确认已发出」才算已联系证据；后者必须留人工签名。
- 台账永不因为跨天自动放行：昨天没核对清楚的，今天照样拦。

容量策略（证据 / 流水分离）：
- **保护集** = 所有阻塞态（submitting / submitted_unverified / outcome_unknown / conflict）
  + 已联系证据（observed_reply / resolved_delivered）。这些是闸门的唯一依据，
  任何容量淘汰都不许碰 —— 丢了就等于放行重发。
- **可淘汰集** = `resolved_not_delivered`（人工确认没发出去，对象可以重新使用）：
  它既不阻塞也不证明已联系，属于流水，可以按 `MAX_ITEMS_KEPT` 有界保留。
- 保护集自身超过 `MAX_PROTECTED_KEPT` 时**不静默丢弃**：全部保留，并在返回值上带
  `overflow=True` / `overflow_count`，由上游报警或要求人工归档。
  旧实现用 `kept[-limit:]` 统一截断末尾，150 招呼/日下第 4 天就会把最早的
  observed_reply / resolved_delivered 挤出去，「永久不再发」只剩约 500 条窗口。
- 状态缺失或无法识别（未来的新状态）按保护集处理（fail-closed）：宁多留，不误删。

归档出口（保护集用满之后的唯一出路）：
- 已联系证据可以从活跃台账搬进**同库**的另一份文档（`applicant_send_ledger_archive`，
  同样按 scope 存），活跃文档因此腾出额度，5000 上限不再是死胡同。
- 「搬走」不等于「忘掉」：所有判断「这个对象联系过没有 / 还没闭环」的查询必须读
  活跃 ∪ 归档（`runtime_tools._ledger_read_all`，沙箱层同理）。只搬不查等于归档一次
  就把证据擦掉、同一对象立刻能再发一次，比不归档更糟。
- 归档只收已联系证据（`observed_reply` / `resolved_delivered`）。阻塞态搬走就是把闸门
  关掉，一律拒绝（`submitting` → `SEND_LEDGER_IN_FLIGHT`，其余阻塞态 → `SEND_LEDGER_UNRESOLVED`）。
- 归档不改状态、不改字段，条目原样搬；同一 `ledger_id` 幂等，不会产生第二条。
"""
import hashlib
import time

from applicant_greeting import day_key

LEDGER_KIND = 'applicant_send_ledger'
# 归档文档：同库、同 scope 维度的一份伴生文档，承接活跃台账搬出来的已联系证据
ARCHIVE_KIND = 'applicant_send_ledger_archive'
# 可淘汰集（流水）保留条数；沿用旧名字保持调用方兼容
MAX_ITEMS_KEPT = 500
# 保护集（证据）上限：达到只报警不删除
MAX_PROTECTED_KEPT = 5000

# 执行中 / 已点击未核对 / 结果未知 / 证据冲突 —— 四种都拦
STATE_SUBMITTING = 'submitting'
STATE_SUBMITTED = 'submitted_unverified'
STATE_OBSERVED = 'observed_reply'
STATE_UNKNOWN = 'outcome_unknown'
STATE_CONFLICT = 'conflict'
STATE_NOT_DELIVERED = 'resolved_not_delivered'
STATE_DELIVERED = 'resolved_delivered'

STATES = (STATE_SUBMITTING, STATE_SUBMITTED, STATE_OBSERVED,
          STATE_UNKNOWN, STATE_CONFLICT, STATE_NOT_DELIVERED, STATE_DELIVERED)

BLOCKING_STATES = (STATE_SUBMITTING, STATE_SUBMITTED, STATE_UNKNOWN, STATE_CONFLICT)
CONTACTED_STATES = (STATE_OBSERVED, STATE_DELIVERED)
# 未闭环但仍在进行（不算"未知"，报错文案要区分）
IN_FLIGHT_STATES = (STATE_SUBMITTING,)

# 保护集：丢了就等于放行，永不因容量淘汰
PROTECTED_STATES = BLOCKING_STATES + CONTACTED_STATES
# 可淘汰集：人工确认没发出去，既不阻塞也不证明已联系
EVICTABLE_STATES = (STATE_NOT_DELIVERED,)
# 可归档集：只有已联系证据能搬出活跃台账 —— 阻塞态搬走等于把闸门关掉
ARCHIVABLE_STATES = CONTACTED_STATES
# 点名归档清单的默认上限：面板响应必须有界，超出的只报总数（界面写「只列最近 N 条」）
ARCHIVABLE_VIEW_LIMIT = 50

RESOLUTIONS = ('delivered', 'not_delivered', 'duplicate_confirmed')

CODES = {
    'SEND_OUTCOME_UNRESOLVED': '上一条发送的结果还没核对，先核对清楚再发下一条。',
    'SEND_TARGET_ALREADY_CONTACTED': '这个对象已经有联系记录，不再重复发送。',
    'SEND_TARGET_UNRESOLVED': '这个对象上次发送结果未核对，先人工核对。',
    'SEND_LEDGER_ENTRY_NOT_FOUND': '没有找到这条发送记录。',
    'SEND_LEDGER_ALREADY_CLOSED': '这条发送记录已经核对过了。',
    'SEND_RESOLUTION_INVALID': '核对结论必须是 delivered / not_delivered / duplicate_confirmed。',
    'SEND_RESOLUTION_BY_REQUIRED': '人工核对必须留下核对人。',
    'SEND_LEDGER_IN_FLIGHT': '这条发送正在执行中（还没点下去或还没点完），不能提前给结论。',
    'SEND_STABLE_TARGET_REQUIRED': '这个对象还没有可证明的稳定标识（平台会话 ID / 岗位 ID），按方案只提供草稿，请人工在平台上发送。',
    'SEND_LEDGER_OVERFLOW': '发送台账的保护证据已超过保留上限，系统没有丢弃任何一条；请人工归档后再继续。',
    'SEND_LEDGER_NOT_ARCHIVABLE': '只有已联系证据（observed_reply / resolved_delivered）可以归档；其余状态留在活跃台账里继续拦。',
    'SEND_LEDGER_UNRESOLVED': '这条发送的结果还没核对，搬走它等于关掉闸门；先人工核对再归档。',
    'SEND_LEDGER_ARCHIVE_INVALID': '归档请求无效：需要操作人，并指定台账条目或目标指纹（或两者都不指定表示整个工作区）。',
}


def entry_id(target_sha256, body_sha256, ts):
    """台账条目标识：同对象同正文同一次点击，可用于幂等去重。"""
    payload = '%s|%s|%.6f' % (target_sha256 or '', body_sha256 or '', float(ts))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]


def new_entry(*, scope_id, target_sha256, body_sha256, action, ts=None, note=None):
    """登记一次「即将点击」。此时还不知道结果，所以状态是 submitting。"""
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError('SCOPE_REQUIRED')
    if not isinstance(target_sha256, str) or len(target_sha256) != 64:
        raise ValueError('TARGET_REQUIRED')
    if action not in ('greeting', 'reply'):
        raise ValueError('ACTION_INVALID')
    stamp = time.time() if ts is None else float(ts)
    return {
        'ledger_id': entry_id(target_sha256, body_sha256, stamp),
        'scope_id': scope_id.strip(),
        'target_sha256': target_sha256,
        'body_sha256': body_sha256,
        'action': action,
        'state': STATE_SUBMITTING,
        'ts': stamp,
        'day': day_key(stamp),
        'note': note,
        'resolved_at': None,
        'resolved_by': None,
        'resolution': None,
    }


def is_blocking(entry):
    """这条记录是否阻塞后续发送。"""
    return isinstance(entry, dict) and entry.get('state') in BLOCKING_STATES


def is_contacted(entry):
    return isinstance(entry, dict) and entry.get('state') in CONTACTED_STATES


def is_protected(entry):
    """是否属于「不能因容量丢弃」的证据。

    只有明确是 `resolved_not_delivered` 的才算流水；其余（含缺 state、未知状态、
    非 dict 脏数据）一律按保护处理 —— fail-closed，误留一条只会多占空间，
    误删一条会直接放行重复招呼。
    """
    if not isinstance(entry, dict):
        return True
    return entry.get('state') not in EVICTABLE_STATES


def blocking_items(items, scope_id=None):
    result = []
    for item in items or ():
        if not is_blocking(item):
            continue
        if scope_id is not None and item.get('scope_id') != scope_id:
            continue
        result.append(item)
    return result


def target_evidence(items, target_sha256, scope_id=None):
    """这个对象的既有证据。返回 (kind, entry)，kind ∈ {'contacted','unresolved',None}。

    已联系优先于未闭环：只要发过且确认收到，就不再重复；未闭环其次。
    """
    contacted = unresolved = None
    for item in items or ():
        if not isinstance(item, dict) or item.get('target_sha256') != target_sha256:
            continue
        if scope_id is not None and item.get('scope_id') != scope_id:
            continue
        if is_contacted(item) and contacted is None:
            contacted = item
        elif is_blocking(item) and unresolved is None:
            unresolved = item
    if contacted is not None:
        return 'contacted', contacted
    if unresolved is not None:
        return 'unresolved', unresolved
    return None, None


def observed_state(*, before_marks=None, after_marks=None, error=False):
    """用一次只读观察决定这条点击的结果。

    只做「有新增回显 / 无新增回显 / 读不到」三档，不推断送达与已读。
    """
    if error:
        return STATE_UNKNOWN, 'observation_failed'
    if before_marks is None or after_marks is None:
        return STATE_SUBMITTED, 'no_baseline'
    if after_marks > before_marks:
        return STATE_OBSERVED, 'new_mark_observed'
    if after_marks == before_marks:
        return STATE_SUBMITTED, 'no_new_mark'
    return STATE_CONFLICT, 'marks_went_backwards'


def close(entry, state, *, by=None, note=None, ts=None):
    """把一条记录推进到新状态。人工核对必须留 `by`。

    `submitted_unverified` 不是终态（还等着核对），所以不写 resolved_at。
    """
    if not isinstance(entry, dict):
        raise ValueError('INVALID_ENTRY')
    if entry.get('state') not in BLOCKING_STATES:
        raise ValueError('SEND_LEDGER_ALREADY_CLOSED')
    if state not in STATES or state in (STATE_SUBMITTING,):
        raise ValueError('INVALID_OUTCOME_STATE')
    if state in (STATE_NOT_DELIVERED, STATE_DELIVERED):
        if not isinstance(by, str) or not by.strip():
            raise ValueError('SEND_RESOLUTION_BY_REQUIRED')
    stamp = time.time() if ts is None else float(ts)
    terminal = state != STATE_SUBMITTED
    updated = dict(entry)
    updated['state'] = state
    updated['resolved_at'] = stamp if terminal else None
    updated['resolved_by'] = (by.strip() if isinstance(by, str) and by.strip() else None)
    updated['resolution'] = note
    return updated


def resolve(entry, resolution, *, by, note=None, ts=None):
    """人工核对结论 → 终态。duplicate_confirmed 表示确认发生重复，按已联系处理。"""
    if resolution not in RESOLUTIONS:
        raise ValueError('SEND_RESOLUTION_INVALID')
    if not isinstance(by, str) or not by.strip():
        raise ValueError('SEND_RESOLUTION_BY_REQUIRED')
    # 正在点击的那一次还没落地，此时给结论会提前解除阻塞 → 另一条路立刻能再发一次
    if isinstance(entry, dict) and entry.get('state') in IN_FLIGHT_STATES:
        raise ValueError('SEND_LEDGER_IN_FLIGHT')
    mapped = {'delivered': STATE_DELIVERED,
              'not_delivered': STATE_NOT_DELIVERED,
              'duplicate_confirmed': STATE_DELIVERED}[resolution]
    text = resolution if not note else '%s: %s' % (resolution, note)
    return close(entry, mapped, by=by, note=text, ts=ts)


def _sort_key(index, item):
    """淘汰顺序：先按 ts，再按原位置（缺 ts 的当最旧，只有流水才会被淘汰）。"""
    stamp = item.get('ts') if isinstance(item, dict) else None
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        stamp = 0.0
    return (float(stamp), index)


class MergeResult(list):
    """`merge()` 的返回值。

    仍然是 `list`（可迭代 / 切片 / json.dumps / 与旧代码互换），额外带容量报告：
    `overflow`、`overflow_count`、`evicted`、`evicted_count`、`report()`。
    上层据此报警或要求人工归档，而不是等闸门静默失效。
    """

    def __init__(self, items=(), *, evicted=(), overflow_count=0, limit=MAX_ITEMS_KEPT,
                 protected_limit=MAX_PROTECTED_KEPT):
        super().__init__(items)
        self.evicted = list(evicted)
        self.evicted_count = len(self.evicted)
        self.overflow_count = int(overflow_count)
        self.overflow = self.overflow_count > 0
        self.limit = limit
        self.protected_limit = protected_limit

    @property
    def protected_count(self):
        return len([item for item in self if is_protected(item)])

    @property
    def evictable_count(self):
        return len([item for item in self if not is_protected(item)])

    def report(self):
        """可落进文档 meta / 返回给上层的容量报告（纯 dict，可直接 json 序列化）。"""
        return {
            'kept': len(self),
            'protected': self.protected_count,
            'evictable': self.evictable_count,
            'limit': self.limit,
            'protected_limit': self.protected_limit,
            'evicted': self.evicted_count,
            'overflow': self.overflow,
            'overflow_count': self.overflow_count,
            'alarm': CODES['SEND_LEDGER_OVERFLOW'] if self.overflow else None,
        }


def capacity(items, *, limit=MAX_ITEMS_KEPT, protected_limit=MAX_PROTECTED_KEPT):
    """只读体检：当前台账里保护集/可淘汰集各多少、是否已经超限（不修改任何东西）。"""
    items = list(items or ())
    protected = len([item for item in items if is_protected(item)])
    return {
        'total': len(items),
        'protected': protected,
        'evictable': len(items) - protected,
        'blocking': len([item for item in items if is_blocking(item)]),
        'contacted': len([item for item in items if is_contacted(item)]),
        'limit': limit,
        'protected_limit': protected_limit,
        'overflow': protected > protected_limit,
        'overflow_count': max(0, protected - protected_limit),
        'alarm': CODES['SEND_LEDGER_OVERFLOW'] if protected > protected_limit else None,
    }


def merge(items, entry, *, limit=MAX_ITEMS_KEPT, protected_limit=MAX_PROTECTED_KEPT):
    """按 ledger_id 原地替换或追加，只对可淘汰集做容量淘汰；返回 `MergeResult`（list 子类）。

    - 幂等：同 `ledger_id` 重复写入只替换那一格，不产生重复条目。
    - 保护集（阻塞态 + 已联系证据）一条都不删，即使超过 `protected_limit`
      也照留，只在返回值上置 `overflow=True` 并给出 `overflow_count`。
    - 可淘汰集（`resolved_not_delivered`）按 ts 保留最新 `limit` 条，
      被淘汰的原样放在 `evicted` 里备查。
    - 不改入参。
    """
    if not isinstance(entry, dict):
        raise ValueError('INVALID_ENTRY')
    kept = [dict(item) if isinstance(item, dict) else item for item in (items or ())]
    payload = dict(entry)
    for index, item in enumerate(kept):
        if isinstance(item, dict) and item.get('ledger_id') == payload.get('ledger_id'):
            kept[index] = payload
            break
    else:
        kept.append(payload)

    evictable = [(index, item) for index, item in enumerate(kept) if not is_protected(item)]
    drop = set()
    if limit is not None and len(evictable) > max(int(limit), 0):
        keep_count = max(int(limit), 0)
        ordered = sorted(evictable, key=lambda pair: _sort_key(*pair))
        drop = {index for index, _ in ordered[:len(ordered) - keep_count]}
    evicted = [item for index, item in evictable if index in drop]
    result = [item for index, item in enumerate(kept) if index not in drop]
    protected_count = len(kept) - len(evictable)
    overflow_count = 0
    if protected_limit is not None:
        overflow_count = max(0, protected_count - max(int(protected_limit), 0))
    return MergeResult(result, evicted=evicted, overflow_count=overflow_count,
                       limit=limit, protected_limit=protected_limit)


def archive_eligible(entry):
    """这条记录是否属于「可以搬出活跃台账」的已联系证据。"""
    return isinstance(entry, dict) and entry.get('state') in ARCHIVABLE_STATES


def archive_refusal(entry):
    """归档不合格时的错误码；合格返回 `None`。

    分三档，为的是让操作者知道下一步该做什么：
    - `submitting` → 那次点击还在路上，任何写都可能还没落地（`SEND_LEDGER_IN_FLIGHT`）
    - 其它阻塞态 → 先人工核对（`SEND_LEDGER_UNRESOLVED`）
    - 状态缺失 / 未知 / 流水 → 本来就不是证据（`SEND_LEDGER_NOT_ARCHIVABLE`）
    fail-closed：拿不准就不许搬。
    """
    if archive_eligible(entry):
        return None
    state = entry.get('state') if isinstance(entry, dict) else None
    if state in IN_FLIGHT_STATES:
        return 'SEND_LEDGER_IN_FLIGHT'
    if state in BLOCKING_STATES:
        return 'SEND_LEDGER_UNRESOLVED'
    return 'SEND_LEDGER_NOT_ARCHIVABLE'


def plan_archive(items, *, ledger_ids=None, target_sha256=None, scope_id=None):
    """挑出可以搬去归档文档的已联系证据，返回 `(moved, kept)`；不改入参。

    - 点名归档（给了 `ledger_ids` 或 `target_sha256`）：被点到的条目里只要有一条不合格
      就整批拒绝（`ValueError` 带错误码）。半批归档会让「搬走了什么」和「还剩什么」
      两边同时残缺，事后对不上账。
    - 整区归档（两个都不给）：只挑已联系证据，未闭环/流水原地留下继续拦 —— 它们不是
      「被拒绝归档」，是根本不该归档。
    - 幂等：`moved` 里同一 `ledger_id` 只出现一次。
    - 非 dict 脏数据按保护处理，永不搬动（fail-closed）。
    """
    wanted = None if ledger_ids is None else set(ledger_ids)
    named = wanted is not None or target_sha256 is not None
    moved, kept, seen = [], [], set()
    for item in items or ():
        if not isinstance(item, dict):
            kept.append(item)
            continue
        if scope_id is not None and item.get('scope_id') != scope_id:
            kept.append(item)
            continue
        selected = wanted is None or item.get('ledger_id') in wanted
        if selected and target_sha256 is not None:
            selected = item.get('target_sha256') == target_sha256
        if not selected:
            kept.append(item)
            continue
        code = archive_refusal(item)
        if code:
            if named and (wanted is None or item.get('ledger_id') in wanted):
                raise ValueError(code)
            kept.append(item)
            continue
        key = item.get('ledger_id')
        if isinstance(key, str):
            if key in seen:
                continue
            seen.add(key)
        moved.append(item)
    return moved, kept


def merge_archive(items, new_items):
    """把已联系证据并入归档文档：按 `ledger_id` 原地替换或追加，绝不产生重复条目。

    不做容量淘汰：归档是保护集的出口，再在这里套一层上限，等于把「谁都没被删」
    换成「删在另一个文档里」，闸门照样会随容量失效。条数只报告（见 `archive_report`），
    超量由人工决定怎么处理。
    """
    kept = [dict(item) if isinstance(item, dict) else item for item in (items or ())]
    index = {}
    for position, item in enumerate(kept):
        if isinstance(item, dict) and isinstance(item.get('ledger_id'), str):
            index.setdefault(item['ledger_id'], position)
    for item in new_items or ():
        if not isinstance(item, dict):
            raise ValueError('INVALID_ENTRY')
        payload = dict(item)
        key = item.get('ledger_id')
        if isinstance(key, str) and key in index:
            kept[index[key]] = payload
            continue
        if isinstance(key, str):
            index[key] = len(kept)
        kept.append(payload)
    return kept


def _view_ts(item):
    """排序用的时间戳：缺 ts / 非数值一律当最旧（0），别让坏数据冲到列表最前面。"""
    stamp = item.get('ts') if isinstance(item, dict) else None
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return 0.0
    return float(stamp)


def _view_stamp(item):
    """回给界面的 ts：坏的直接给 None，不给 0（0 会被渲染成 1970 年，看着像真数据）。"""
    stamp = item.get('ts') if isinstance(item, dict) else None
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    return float(stamp)


def _target_digest(item):
    """对象摘要：台账只存指纹、不存名字，给前 12 位让操作者能和别处对上号。"""
    target = item.get('target_sha256') if isinstance(item, dict) else None
    if isinstance(target, str) and target:
        return target[:12]
    note = item.get('note') if isinstance(item, dict) else None
    if isinstance(note, str) and note.strip():
        return note.strip()[:40]
    return ''


def archivable_view(items, *, limit=ARCHIVABLE_VIEW_LIMIT):
    """可归档条目的**只读清单**：按 ts 倒序，最多 `limit` 条 —— 给工作台挂勾选框用。

    只收 `archive_eligible` 认可的已联系证据：未核对 / 流水本来就搬不走，列出来只会让人
    点一个必然失败的按钮（勾上再被 `SEND_LEDGER_UNRESOLVED` 拒绝）。
    超出上限不静默丢：`total` 报全量条数，`truncated` 让界面说清「只列最近 N 条」。
    """
    picked = [item for item in (items or ()) if archive_eligible(item)]
    ordered = sorted(picked, key=_view_ts, reverse=True)   # 稳定排序：同 ts 保持原顺序
    bound = max(int(limit), 0)
    view = [{'ledger_id': item.get('ledger_id'),
             'state': item.get('state'),
             'target': _target_digest(item),
             'action': item.get('action'),
             'ts': _view_stamp(item),
             'body_sha256_short': (item['body_sha256'][:12]
                                   if isinstance(item.get('body_sha256'), str) else '')}
            for item in ordered[:bound]]
    return {'archivable': view, 'archivable_total': len(ordered),
            'archivable_truncated': len(ordered) > len(view)}


def archive_report(items):
    """归档文档的只读体检：搬进去多少、覆盖多少对象、里面有没有不该出现的阻塞态。"""
    scoped = [item for item in (items or ()) if isinstance(item, dict)]
    return {'archived': len(scoped),
            'contacted': len([item for item in scoped if is_contacted(item)]),
            'targets': len({item.get('target_sha256') for item in scoped
                            if isinstance(item.get('target_sha256'), str)}),
            'blocking': len([item for item in scoped if is_blocking(item)])}


def summary(items, scope_id=None):
    """给用户看的一行账：还有几条没核对、总共发过几条。"""
    scoped = [i for i in (items or ()) if scope_id is None or i.get('scope_id') == scope_id]
    return {'total': len(scoped),
            'blocking': len(blocking_items(scoped)),
            'contacted': len([i for i in scoped if is_contacted(i)]),
            'by_state': {state: len([i for i in scoped if i.get('state') == state])
                         for state in STATES}}
