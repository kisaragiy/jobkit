"""Capability and context checks for the single execution kernel."""
from contextvars import ContextVar
from contextlib import contextmanager
import json
import sqlite3
import time
import uuid
import store


class KernelError(RuntimeError):
    def __init__(self, code, message, status=409, *, outcome_unknown=False):
        super().__init__(message)
        self.code, self.status = code, status
        self.outcome_unknown = outcome_unknown is True


_capability = ContextVar("jobkit_kernel_capability", default=None)
_action_grant = ContextVar("jobkit_action_grant", default=None)
_maintenance = ContextVar('jobkit_local_maintenance', default=False)


@contextmanager
def maintenance_access():
    """Explicit local CLI intent, never enabled by an HTTP field or environment."""
    token = _maintenance.set(True)
    try:
        yield
    finally:
        _maintenance.reset(token)


def require_maintenance():
    if not _maintenance.get():
        raise KernelError('MAINTENANCE_REQUIRED', '此操作仅限本机维护命令，不向工作区页面开放。', 403)


@contextmanager
def action_grant(doc, context):
    """由执行器在消费授权之后进入；这是外部写入执行期间的唯一凭据来源。"""
    token = _action_grant.set((doc, context["scope_id"]))
    try:
        yield
    finally:
        _action_grant.reset(token)


def ready_authorization(scope_id):
    """读本工作区的渠道授权；只有 ready 且未过期、未用尽的才算可用。

    用只读连接：store.connect() 每次都会跑迁移并取写锁，在 dispatch 已持有连接时
    再开写连接会 database is locked —— 那会把「读不到」误报成「没有授权」。
    """
    try:
        c = sqlite3.connect('file:%s?mode=ro' % store.DB_PATH, uri=True)
    except sqlite3.Error:
        return None
    try:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT content FROM runtime_documents WHERE scope_id=? AND kind='channel_authorization'",
            (scope_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    if row is None:
        return None
    content = row["content"]
    doc = json.loads(content) if isinstance(content, str) else content
    if not isinstance(doc, dict) or doc.get("kind") != "channel_authorization":
        return None
    if doc.get("status") == "exhausted" or int(doc.get("used", 0)) >= int(doc.get("max_uses", 0)):
        raise KernelError("CHANNEL_AUTHORIZATION_EXHAUSTED", "这份授权已经用过一次，要再动作必须重新登记并重新核对目标。")
    if doc.get("status") != "ready":
        return None
    if time.time() >= float(doc.get("expires_at", 0)):
        raise KernelError("CHANNEL_AUTHORIZATION_EXPIRED", "授权已过期，请重新登记。")
    return doc


def ready_conversation_authorization(scope_id):
    """B 方案的一次性授权（真实会话）。格式与 channel_authorization 不同，单独读。"""
    try:
        c = sqlite3.connect('file:%s?mode=ro' % store.DB_PATH, uri=True)
    except sqlite3.Error:
        return None
    try:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT content FROM runtime_documents WHERE scope_id=? AND kind='conversation_authorization'",
            (scope_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    if row is None:
        return None
    content = row['content']
    doc = json.loads(content) if isinstance(content, str) else content
    if not isinstance(doc, dict) or doc.get('kind') != 'conversation_authorization':
        return None
    if doc.get('status') == 'exhausted' or int(doc.get('used', 0)) >= int(doc.get('max_uses', 0)):
        raise KernelError('CONVERSATION_AUTHORIZATION_EXHAUSTED',
                          '这份授权已经用过一次，要再动作必须重新登记并重新核对目标。')
    if doc.get('status') != 'ready':
        return None
    if time.time() >= float(doc.get('expires_at', 0)):
        raise KernelError('CONVERSATION_AUTHORIZATION_EXPIRED', '授权已过期，请重新登记。')
    return doc


def unresolved_send_blocker(scope_id=None):
    """有「未核对的发送结果」时返回那条记录，否则 None。

    这是真人事故事件（outcome_unknown 之后 17 秒重发同一对象）在沙箱层的闸门：
    不能只靠 handler 自查，否则任何新注册的 external_write 都会绕过去。
    只读连接，不写库。

    跨 scope 聚合：台账按工作区各存一份，换一个账号/组织就是新工作区、台账为空，
    于是同一真人能被再发一次。方案验收矩阵写的是「跨日/跨 scope 不绕过」，所以这里
    读全库不过滤 scope。scope_id 参数保留仅为兼容调用方，不再参与判断。
    """
    from applicant_send_ledger import LEDGER_KIND, ARCHIVE_KIND, BLOCKING_STATES  # 延迟导入避免循环依赖
    try:
        c = sqlite3.connect('file:%s?mode=ro' % store.DB_PATH, uri=True)
    except sqlite3.Error:
        return None
    try:
        c.row_factory = sqlite3.Row
        # 归档文档一起查：归档只收已联系证据，里面出现阻塞态就是数据坏了——
        # 坏数据也必须拦（fail-closed），不能因为「本该在活跃文档里」就放过。
        rows = c.execute("SELECT content FROM runtime_documents WHERE kind IN (?,?)",
                         (LEDGER_KIND, ARCHIVE_KIND)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    for row in rows:
        content = row['content']
        doc = json.loads(content) if isinstance(content, str) else content
        items = (doc.get('items') if isinstance(doc, dict) else None) or ()
        for item in items:
            if isinstance(item, dict) and item.get('state') in BLOCKING_STATES:
                return item
    return None


def paused_actions_now():
    """跨工作区汇总当前暂停的动作类型；读不到按「自动回复已暂停」处理。

    只读连接，理由同 ready_authorization：dispatch 已持有连接，再开写连接会
    database is locked，那会把「读不到」误报成「没暂停」。
    """
    import run_state
    fallback = list(run_state.DEFAULT_ACTIONS)
    try:
        c = sqlite3.connect('file:%s?mode=ro' % store.DB_PATH, uri=True)
    except sqlite3.Error:
        return fallback
    try:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT content FROM runtime_documents WHERE kind=?",
                         (run_state.KIND,)).fetchall()
    except sqlite3.Error:
        return fallback
    finally:
        c.close()
    documents = []
    for (content,) in rows:
        documents.append(json.loads(content) if isinstance(content, str) else content)
    return run_state.paused_actions(*documents)


CONTROLLED_CHANNEL_PREFIX = 'runtime.applicant.action.'


def require_channel_authorization(context, tool_name=None):
    """外部写入不「一行放行」：受控通道或真实会话，任一有可用一次性授权才放行。

    真实通道的写入必须先过发送台账：受控通道的渠道授权存在，不代表真实通道的
    上一条结果已经核对清楚——两者是两条独立的腿，不能互相借道。
    """
    # 默认 fail-closed：只有明确的受控通道工具才豁免台账检查，其余一律先查
    if not str(tool_name or '').startswith(CONTROLLED_CHANNEL_PREFIX):
        blocker = unresolved_send_blocker(context["scope_id"])
        if blocker is not None:
            raise KernelError("SEND_OUTCOME_UNRESOLVED",
                              "上一条发送（%s）的结果还没核对，先核对完再发。"
                              % str(blocker.get('ledger_id') or '')[:20], 409)
    channel_error = None
    try:
        doc = ready_authorization(context["scope_id"])
    except KernelError as exc:      # 受控通道的授权过期/用尽，不该挡住真实通道
        doc, channel_error = None, exc
    if doc is not None:
        if doc.get("action") not in ("greeting.submit",):
            raise KernelError("CHANNEL_NOT_AUTHORIZED", "这份授权不覆盖当前动作类型。")
        return
    # 真实通道：上一条结果没核对清楚之前，一律不发（含未知、已点击未核对、冲突、执行中）
    blocker = unresolved_send_blocker(context["scope_id"])
    if blocker is not None:
        raise KernelError("SEND_OUTCOME_UNRESOLVED",
                          "上一条发送（%s）的结果还没核对，先核对完再发。"
                          % str(blocker.get('ledger_id') or '')[:20], 409)
    # B 方案：真实会话授权（目标/正文的细校验由 handler 做，这里只确认存在且未用尽）
    try:
        conversation = ready_conversation_authorization(context["scope_id"])
    except KernelError:
        raise
    if conversation is not None:
        # 暂停只停自动回复，招呼照发（2026-10-27 定）；暂停集跨工作区汇总。
        action = conversation.get('action') if isinstance(conversation, dict) else None
        if not isinstance(action, str) or not action:
            action = 'reply'
        if action in paused_actions_now():
            import run_state
            raise KernelError("RUN_PAUSED", run_state.CODES["RUN_PAUSED"], 409)
        return
    if channel_error is not None:
        raise channel_error
    raise KernelError("CHANNEL_NOT_AUTHORIZED", "渠道身份与接入条件尚未验证，当前仅提供草稿。")


def record_refusal(tool_name, error):
    """Event-only trace for an operation refused before a task can be created."""
    trace_id = uuid.uuid4().hex
    with store.runtime_db() as c:
        context = store.runtime_context(c)
        store.runtime_event(c, trace_id, context["scope_id"], tool_name, "blocked", error.code)
    error.trace_id = trace_id
    return error


def authorize(tool, context, expected_token):
    if tool.context_required and expected_token != context["context_token"]:
        raise KernelError("STALE_CONTEXT", "当前身份已切换，请重新加载工作区。")
    if tool.maintenance_only:
        require_maintenance()
    if tool.effect == "external_write":
        require_channel_authorization(context, tool.name)


@contextmanager
def capability(tool, context):
    token = _capability.set((tool.name, context["scope_id"], context["context_token"], context.get('task_id')))
    try:
        yield
    finally:
        _capability.reset(token)


def require_registered(tool_name):
    """Legacy irreversible primitives cannot become a second execution entrance."""
    cap = _capability.get()
    with store.runtime_db() as c:
        context = store.runtime_context(c)
        if cap is not None and cap[:3] == (tool_name, context["scope_id"], context["context_token"]):
            return
    raise record_refusal(tool_name, KernelError("KERNEL_REQUIRED", "旧执行入口已停用；请使用统一工作区。"))


def current_execution(tool_name):
    require_registered(tool_name)
    _, scope, token, task_id = _capability.get()
    if not task_id:
        raise KernelError('KERNEL_REQUIRED', '模型调用缺少内核任务关联。')
    return {'scope_id': scope, 'context_token': token, 'task_id': task_id}


def require_legacy_workspace():
    with store.runtime_db() as c:
        context = store.runtime_context(c)
    if (context["role"], context["organization_id"], context["account_id"]) != ("applicant", "local", "legacy"):
        raise KernelError("LEGACY_SCOPE_DENIED", "历史求职者资料仅能在原求职者工作区查看。")
