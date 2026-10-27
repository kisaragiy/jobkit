"""jobkit 本地台账库 —— 把 boss-agent 散落的缓存归一到一处，避免每次重解析。

数据来源（只读，不改动 boss-agent 的原库）：
  · ~/.boss-agent/cache/boss_agent.db  → search_cache(搜索结果) / greet_records(投递记录)
  · ~/.boss-agent/cache/index_cache.json → 最近一次搜索的岗位目录
  · hermes/scripts/.boss_greeted.txt    → 旧版去重文件（历史遗留，一并导入）

用法：
    python store.py sync      # 从各来源同步进 jobkit.db
    python store.py stats     # 打印库存概况
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import hashlib
import math
import uuid
from contextlib import contextmanager
from pathlib import Path

JOBKIT_DIR = Path(__file__).parent
#: `JOBKIT_DB` 可覆盖库路径：多 worktree 并行开发 / 拿真库跑只读校验时用，
#: 否则每个 worktree 都会在自己的目录里新建一个空库、看着像「数据全丢了」。
DB_PATH = Path(os.environ.get("JOBKIT_DB") or (JOBKIT_DIR / "jobkit.db"))
BOSS_CACHE = Path(r"C:\Users\<user>\.boss-agent\cache")
BOSS_DB = BOSS_CACHE / "boss_agent.db"
INDEX_CACHE = BOSS_CACHE / "index_cache.json"
GREETED_TXT = Path(r"C:\Users\<user>\AppData\Local\hermes\scripts\.boss_greeted.txt")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    security_id  TEXT,
    title        TEXT,
    company      TEXT,
    salary       TEXT,
    city         TEXT,
    district     TEXT,
    experience   TEXT,
    education    TEXT,
    skills       TEXT,
    industry     TEXT,
    scale        TEXT,
    boss_name    TEXT,
    boss_title   TEXT,
    boss_active  TEXT,
    match_score  REAL,
    first_seen   REAL,
    last_seen    REAL,
    sources      TEXT,
    platform     TEXT,
    url          TEXT,
    job_description TEXT
);
CREATE TABLE IF NOT EXISTS greetings (
    job_id      TEXT PRIMARY KEY,
    security_id TEXT,
    greeted_at  REAL,
    message     TEXT,
    status      TEXT,
    error       TEXT,
    job_snapshot TEXT,
    attempted_at REAL
);
CREATE TABLE IF NOT EXISTS resumes (
    resume_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    direction   TEXT NOT NULL,
    path        TEXT NOT NULL,
    active      INTEGER DEFAULT 0,
    created_at  REAL,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    ended_at  REAL,
    sent      INTEGER DEFAULT 0,
    failed    INTEGER DEFAULT 0,
    stopped   TEXT
);
CREATE TABLE IF NOT EXISTS interviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT NOT NULL,
    position    TEXT DEFAULT '',
    stage       TEXT DEFAULT '',
    happened_at REAL,
    result      TEXT DEFAULT 'pending',
    note        TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS interview_questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL,
    question     TEXT NOT NULL,
    my_answer    TEXT DEFAULT '',
    self_score   INTEGER DEFAULT 0,
    better       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS conversations (
    conv_key      TEXT PRIMARY KEY,
    hr_name       TEXT,
    company       TEXT,
    position      TEXT,
    last_msg      TEXT,
    msg_hash      TEXT,
    category      TEXT,
    from_me       INTEGER DEFAULT 0,
    last_time     TEXT,
    avatar        TEXT,
    first_seen    REAL,
    updated_at    REAL,
    pending_since REAL,
    handled       INTEGER DEFAULT 0,
    hr_read       INTEGER DEFAULT 0,
    hr_read_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_greet_time ON greetings(greeted_at);
CREATE INDEX IF NOT EXISTS idx_conv_pending ON conversations(from_me, handled);
CREATE TABLE IF NOT EXISTS runtime_scopes (
    scope_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    UNIQUE(role, organization_id, account_id)
);
CREATE TABLE IF NOT EXISTS runtime_control (
    id INTEGER PRIMARY KEY CHECK(id=1),
    scope_id TEXT NOT NULL,
    context_token TEXT NOT NULL,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_documents (
    scope_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(scope_id,kind)
);
CREATE TABLE IF NOT EXISTS runtime_tasks (
    task_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    context_token TEXT NOT NULL,
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,
    effect TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    error_code TEXT,
    parent_task_id TEXT,
    commit_receipt TEXT,
    conversation_note TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'pending',
    review_revision INTEGER NOT NULL DEFAULT 0,
    supersedes_task_id TEXT,
    superseded_by_task_id TEXT,
    manual_text TEXT,
    manual_revision INTEGER NOT NULL DEFAULT 0,
    conversation_key TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(scope_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS runtime_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    scope_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,
    code TEXT,
    occurred_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_draft_edits (
    task_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    document_revision INTEGER NOT NULL,
    text TEXT NOT NULL,
    control_task_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(task_id,revision)
);
CREATE TABLE IF NOT EXISTS runtime_manual_batches (
    scope_id TEXT NOT NULL,
    batch_key_digest TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(scope_id,batch_key_digest)
);
CREATE TABLE IF NOT EXISTS runtime_workspace_imports (
    import_id TEXT PRIMARY KEY,
    owner_scope_id TEXT NOT NULL,
    request_key_digest TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    target_scope_id TEXT NOT NULL UNIQUE,
    source_snapshot TEXT NOT NULL,
    task_id_map TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(owner_scope_id,request_key_digest)
);
CREATE TABLE IF NOT EXISTS runtime_model_calls (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    scope_id TEXT NOT NULL,
    context_token TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    config_binding TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    reserved_units INTEGER NOT NULL CHECK(typeof(reserved_units)='integer' AND reserved_units > 0),
    state TEXT NOT NULL CHECK(state IN ('reserved','sending','not_sent','outcome_unknown','response_error','response_received')),
    http_status INTEGER,
    error_code TEXT,
    provider_request_id TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_calls_day ON runtime_model_calls(budget_day);
CREATE INDEX IF NOT EXISTS idx_model_calls_scope ON runtime_model_calls(scope_id,created_at,attempt_id);
CREATE TABLE IF NOT EXISTS runtime_model_reviews (
    attempt_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    control_task_id TEXT NOT NULL,
    observed_state TEXT NOT NULL CHECK(observed_state IN ('reserved','sending','outcome_unknown')),
    note TEXT NOT NULL,
    reviewed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runtime_tasks_scope ON runtime_tasks(scope_id,created_at);
CREATE INDEX IF NOT EXISTS idx_runtime_events_task ON runtime_events(task_id,event_id);
CREATE TABLE IF NOT EXISTS runtime_send_attempts (
    attempt_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    draft_task_id TEXT NOT NULL,
    recipient_binding_id TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','released','not_sent','outcome_unknown')),
    message_sha256 TEXT NOT NULL,
    reserved_units INTEGER NOT NULL DEFAULT 1 CHECK(reserved_units = 1),
    window_start_task_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(scope_id,intent_id)
);
CREATE INDEX IF NOT EXISTS idx_send_attempts_day ON runtime_send_attempts(scope_id,budget_day);
"""

JOB_FIELDS = ["job_id", "security_id", "title", "company", "salary", "city", "district",
              "experience", "education", "skills", "industry", "scale",
              "boss_name", "boss_title", "boss_active", "match_score",
              "platform", "url", "job_description"]

# 老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
_MIGRATE_COLUMNS = [
    ("platform", "TEXT"),
    ("url", "TEXT"),
    ("job_description", "TEXT"),
]

# 投递记录也要补列：早期版本只存 job_id，岗位一下架/被新搜索覆盖，
# 台账里就只剩「岗位信息缺失」。从这版起投递时把岗位快照一起存下来。
_GREET_MIGRATE_COLUMNS = [
    ("job_snapshot", "TEXT"),
    # 记录这次投递用的是哪份简历（效果追踪的前提）。0/空 = 早期投递，未知。
    ("resume_id", "INTEGER DEFAULT 0"),
    # 每次尝试的时刻，**不论成功失败**。
    # 为什么不复用 greeted_at：greeted_at 语义是「打招呼成功的时间」，
    # 失败时为 NULL（NULL 在按时间过滤/排序里会被整条排除）。
    # 结果就是「今天失败了 3 条」在库里查不到 —— 只能靠翻日志。
    ("attempted_at", "REAL"),
]


def _migrate(c: sqlite3.Connection) -> None:
    c.execute("BEGIN IMMEDIATE")
    have = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    for name, decl in _MIGRATE_COLUMNS:
        if name not in have:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    have_g = {r[1] for r in c.execute("PRAGMA table_info(greetings)")}
    for name, decl in _GREET_MIGRATE_COLUMNS:
        if name not in have_g:
            c.execute(f"ALTER TABLE greetings ADD COLUMN {name} {decl}")
    # conversations：已读状态（B2「已读未回」分档的基础）
    have_c = {r[1] for r in c.execute("PRAGMA table_info(conversations)")}
    if "hr_read" not in have_c:
        c.execute("ALTER TABLE conversations ADD COLUMN hr_read INTEGER DEFAULT 0")
    if "hr_read_at" not in have_c:
        # 第一次观测到「对方已读」的时刻。updated_at 是「最后一次看到这条会话」，
        # 每次抓取都会刷新 → 拿它算「读了多久没回」是假的。
        c.execute("ALTER TABLE conversations ADD COLUMN hr_read_at REAL")
    runtime_columns = {row[1] for row in c.execute("PRAGMA table_info(runtime_tasks)")}
    if runtime_columns and "parent_task_id" not in runtime_columns:
        c.execute("ALTER TABLE runtime_tasks ADD COLUMN parent_task_id TEXT")
    if runtime_columns and "commit_receipt" not in runtime_columns:
        c.execute("ALTER TABLE runtime_tasks ADD COLUMN commit_receipt TEXT")
    for column, declaration in (("conversation_note", "TEXT NOT NULL DEFAULT ''"),
                                ("review_status", "TEXT NOT NULL DEFAULT 'pending'"),
                                ("review_revision", "INTEGER NOT NULL DEFAULT 0"),
                                ("supersedes_task_id", "TEXT"), ("superseded_by_task_id", "TEXT"),
                                ("manual_text", "TEXT"), ("manual_revision", "INTEGER NOT NULL DEFAULT 0"),
                                # 空串 = 升级前的记录，按默认会话键（legacy）读。
                                ("conversation_key", "TEXT NOT NULL DEFAULT ''")):
        if runtime_columns and column not in runtime_columns:
            c.execute(f"ALTER TABLE runtime_tasks ADD COLUMN {column} {declaration}")
    # 预占必须固定观察代次：重建观察起点后，旧预占既不能续期，也不能再取卡。
    send_columns = {row[1] for row in c.execute("PRAGMA table_info(runtime_send_attempts)")}
    if send_columns and "window_start_task_id" not in send_columns:
        c.execute("ALTER TABLE runtime_send_attempts ADD COLUMN window_start_task_id TEXT NOT NULL DEFAULT ''")
    c.commit()


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        _migrate(c)
    except Exception:
        c.close()
        raise
    return c


@contextmanager
def runtime_db():
    c = connect()
    try:
        with c:
            yield c
    finally:
        c.close()


def runtime_context(c: sqlite3.Connection) -> dict:
    """One local owner, explicit role/account namespaces; no account verification claim."""
    scope_id = runtime_scope_id("recruiter", "我的企业", "主账号")
    c.execute("INSERT OR IGNORE INTO runtime_scopes VALUES (?,?,?,?)",
              (scope_id, "recruiter", "我的企业", "主账号"))
    c.execute("INSERT OR IGNORE INTO runtime_control VALUES (1,?,?,1)",
              (scope_id, uuid.uuid4().hex))
    row = c.execute("SELECT s.*,r.context_token,r.generation FROM runtime_control r "
                    "JOIN runtime_scopes s ON s.scope_id=r.scope_id WHERE r.id=1").fetchone()
    if row is None:
        raise RuntimeError("Runtime context is missing")
    return {**dict(row), "account_verified": False, "execution_mode": "assist"}


def runtime_scope_id(role: str, organization_id: str, account_id: str) -> str:
    return hashlib.sha256(json.dumps([role, organization_id, account_id],
                                    ensure_ascii=False).encode()).hexdigest()


def runtime_event(c, task_id, scope_id, tool, status, code=None):
    # Deliberately exclude message bodies, documents, model reasoning and credentials.
    c.execute("INSERT INTO runtime_events(task_id,scope_id,tool,status,code,occurred_at) "
              "VALUES (?,?,?,?,?,?)", (task_id, scope_id, tool, status, code, time.time()))


BACKUP_RUNTIME_TABLES = frozenset({'runtime_scopes', 'runtime_control', 'runtime_documents', 'runtime_tasks',
    'runtime_events', 'runtime_draft_edits', 'runtime_manual_batches', 'runtime_workspace_imports',
    'runtime_model_calls', 'runtime_model_reviews', 'runtime_send_attempts'})

RUNTIME_TABLE_PREFIX = 'runtime_'


def runtime_table_audit(c):
    """Compare the live schema with the backup allow-list; a new table must never be silently dropped."""
    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    runtime_tables = {name for name in tables if name.startswith(RUNTIME_TABLE_PREFIX)}
    return {'covered': sorted(runtime_tables & BACKUP_RUNTIME_TABLES),
            'uncovered': sorted(runtime_tables - BACKUP_RUNTIME_TABLES),
            'missing': sorted(BACKUP_RUNTIME_TABLES - runtime_tables)}


def runtime_database_snapshot(destination, *, max_bytes, deadline):
    """SQLite's online backup includes committed WAL pages; no raw file copying."""
    from sandbox import KernelError
    source_path, destination = Path(DB_PATH).resolve(), Path(destination)
    if source_path == destination.resolve():
        raise KernelError('INVALID_BACKUP_PATH', '不能将当前数据库作为备份目标。', 400)
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True, timeout=0.1)
    try:
        pages = source.execute('PRAGMA page_count').fetchone()[0]
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        if pages * page_size > max_bytes:
            raise KernelError('BACKUP_TOO_LARGE', '数据库超过当前备份容量，未生成截断副本。', 413)
        uncovered = runtime_table_audit(source)['uncovered']
        if uncovered:
            raise KernelError('BACKUP_TABLE_COVERAGE_GAP',
                              '备份清单未覆盖运行时表：' + ','.join(uncovered) + '；未生成不完整的备份。', 409)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        target = sqlite3.connect(destination)
        try:
            def progress(status, remaining, total):
                if time.monotonic() >= deadline:
                    raise TimeoutError('backup deadline')
                if total * page_size > max_bytes:
                    raise RuntimeError('backup capacity changed')
            source.backup(target, pages=128, progress=progress, sleep=0.01)
            # A published copy is standalone, without relying on WAL sidecars.
            mode = target.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
            if mode.lower() != 'delete':
                raise RuntimeError('backup journal not standalone')
        finally:
            target.close()
    finally:
        source.close()
    with destination.open('r+b') as stream:
        os.fsync(stream.fileno())


def runtime_database_inspect(path, *, deadline, trace_id):
    """Physical/schema verification via independent read-only connection, no migration."""
    from sandbox import KernelError
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)
    try:
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise KernelError('INVALID_DATABASE_BACKUP', '备份数据库完整性检查未通过。')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not BACKUP_RUNTIME_TABLES <= tables:
            raise KernelError('INVALID_DATABASE_BACKUP', '备份缺少当前运行时的必要表。')
        witness = connection.execute('SELECT tool,kind,effect,status FROM runtime_tasks WHERE task_id=?', (trace_id,)).fetchone()
        if witness != ('runtime.database.backup', 'control', 'local_file', 'executing'):
            raise KernelError('INVALID_DATABASE_BACKUP', '备份与创建任务的关联不一致。')
        if time.monotonic() >= deadline:
            raise KernelError('BACKUP_TIMEOUT', '备份校验超时，未确认完成。')
    except sqlite3.Error:
        code = 'BACKUP_TIMEOUT' if time.monotonic() >= deadline else 'INVALID_DATABASE_BACKUP'
        raise KernelError(code, '备份数据库校验未通过，未确认完成。') from None
    finally:
        connection.close()


def runtime_model_day():
    return time.strftime('%Y-%m-%d', time.gmtime())


def _model_execution(c, context, binding):
    from sandbox import KernelError
    active = runtime_context(c)
    if active['context_token'] != context['context_token'] or active['scope_id'] != context['scope_id']:
        raise KernelError('STALE_CONTEXT', '身份已切换，未发送模型请求。')
    task = c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',
                     (context['task_id'], context['scope_id'])).fetchone()
    if (task is None or task['tool'] != 'draft.reply' or task['kind'] != 'work'
            or task['context_token'] != context['context_token']):
        raise KernelError('MODEL_TASK_NOT_EXECUTING', '模型请求缺少正在执行的草稿任务。')
    if task['status'] == 'cancelled':
        raise KernelError('CANCELLED', '草稿任务已取消，未继续发送模型请求。')
    if task['status'] != 'executing':
        raise KernelError('MODEL_TASK_NOT_EXECUTING', '模型请求缺少正在执行的草稿任务。')
    payload = json.loads(task['payload'])
    if payload.get('draft_backend') != 'llm' or payload.get('llm_binding') != binding:
        raise KernelError('LLM_CONFIG_CHANGED', '任务的模型外发配置不匹配，未发送。')
    document = runtime_document(c, context['scope_id'], payload['document_kind'])
    if document['revision'] != payload['document_revision']:
        raise KernelError('STALE_FACTS', '资料已更新，未发送旧资料的模型请求。')


def runtime_model_reserve(context, model, binding, units, request_limit, unit_limit):
    from sandbox import KernelError
    with runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        _model_execution(c, context, binding)
        if c.execute('SELECT 1 FROM runtime_model_calls WHERE task_id=?', (context['task_id'],)).fetchone():
            raise KernelError('LLM_ALREADY_ATTEMPTED', '该任务已有模型请求记录，不能再次调用。')
        day = runtime_model_day()
        used = c.execute('SELECT count(*),coalesce(sum(reserved_units),0) FROM runtime_model_calls WHERE budget_day=?', (day,)).fetchone()
        if used[0] >= request_limit or used[1] + units > unit_limit:
            raise KernelError('LLM_BUDGET_EXCEEDED', '本机当日模型调用额度不足，未发送请求。')
        attempt_id, now = uuid.uuid4().hex, time.time()
        c.execute('INSERT INTO runtime_model_calls(attempt_id,task_id,scope_id,context_token,provider,model,config_binding,budget_day,reserved_units,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                  (attempt_id, context['task_id'], context['scope_id'], context['context_token'], 'deepseek', model, binding, day, units, 'reserved', now, now))
        runtime_event(c, context['task_id'], context['scope_id'], 'draft.reply', 'model_reserved')
        return attempt_id


def runtime_model_interrupt(context, database=None):
    """Bounded read-only signal probe; never initializes or mutates the database."""
    path = Path(database if database is not None else DB_PATH).resolve()
    try:
        c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.02)
        try:
            c.execute('BEGIN')
            active = c.execute('SELECT scope_id,context_token FROM runtime_control WHERE id=1').fetchone()
            task = c.execute('SELECT scope_id,context_token,tool,kind,status FROM runtime_tasks WHERE task_id=?',
                             (context['task_id'],)).fetchone()
        finally:
            c.close()
    except (sqlite3.Error, OSError):
        return 'MODEL_MONITOR_FAILED'
    if active is None or task is None:
        return 'MODEL_MONITOR_FAILED'
    if active != (context['scope_id'], context['context_token']):
        return 'STALE_CONTEXT'
    if task[:4] != (context['scope_id'], context['context_token'], 'draft.reply', 'work'):
        return 'MODEL_MONITOR_FAILED'
    if task[4] == 'cancelled':
        return 'CANCELLED'
    return None if task[4] == 'executing' else 'MODEL_TASK_NOT_EXECUTING'


def runtime_model_sending(context, attempt_id, binding):
    from sandbox import KernelError
    with runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        _model_execution(c, context, binding)
        changed = c.execute("UPDATE runtime_model_calls SET state='sending',updated_at=? WHERE attempt_id=? AND task_id=? AND scope_id=? AND context_token=? AND config_binding=? AND state='reserved'",
                            (time.time(), attempt_id, context['task_id'], context['scope_id'], context['context_token'], binding)).rowcount
        if changed != 1:
            raise KernelError('INVALID_MODEL_CALL_STATE', '模型请求状态无法核对，未发送。')
        runtime_event(c, context['task_id'], context['scope_id'], 'draft.reply', 'model_sending')


def runtime_model_finish(context, attempt_id, *, state, error_code=None, http_status=None, metadata=None):
    from sandbox import KernelError
    if state not in {'not_sent', 'outcome_unknown', 'response_error', 'response_received'}:
        raise KernelError('INVALID_MODEL_CALL_STATE', '模型请求终态无效。')
    metadata = metadata or {}
    with runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        # Settle the original scope even if the active workspace changed in flight.
        changed = c.execute("UPDATE runtime_model_calls SET state=?,error_code=?,http_status=?,provider_request_id=?,prompt_tokens=?,completion_tokens=?,total_tokens=?,updated_at=? WHERE attempt_id=? AND task_id=? AND scope_id=? AND context_token=? AND state IN ('reserved','sending')",
             (state, error_code, http_status, metadata.get('provider_request_id'), metadata.get('prompt_tokens'),
              metadata.get('completion_tokens'), metadata.get('total_tokens'), time.time(), attempt_id,
              context['task_id'], context['scope_id'], context['context_token'])).rowcount
        if changed != 1:
            raise KernelError('INVALID_MODEL_CALL_STATE', '模型请求记录无法完成核对。')
        runtime_event(c, context['task_id'], context['scope_id'], 'draft.reply', 'model_' + state, error_code)


def runtime_model_review_get(c, scope_id, attempt_id):
    row = c.execute('SELECT * FROM runtime_model_reviews WHERE scope_id=? AND attempt_id=?', (scope_id, attempt_id)).fetchone()
    if row is None:
        return None
    from sandbox import KernelError
    source = c.execute('SELECT task_id,state FROM runtime_model_calls WHERE scope_id=? AND attempt_id=?', (scope_id, attempt_id)).fetchone()
    owner = c.execute('SELECT tool,kind,effect FROM runtime_tasks WHERE scope_id=? AND task_id=?', (scope_id, row['control_task_id'])).fetchone()
    task = c.execute('SELECT tool,kind FROM runtime_tasks WHERE scope_id=? AND task_id=?', (scope_id, row['task_id'])).fetchone()
    if (source is None or source['task_id'] != row['task_id'] or source['state'] != row['observed_state']
            or row['observed_state'] not in {'reserved', 'sending', 'outcome_unknown'}
            or owner is None or (owner['tool'], owner['kind'], owner['effect']) != ('runtime.model.review', 'control', 'state')
            or task is None or (task['tool'], task['kind']) != ('draft.reply', 'work')
            or any(not isinstance(row[key], str) or not re.fullmatch(r'[0-9a-f]{32}', row[key])
                   for key in ('attempt_id', 'review_id', 'task_id', 'control_task_id'))
            or not isinstance(row['note'], str) or not row['note'].strip() or row['note'] != row['note'].strip() or len(row['note']) > 500
            or type(row['reviewed_at']) not in (int, float) or not math.isfinite(row['reviewed_at'])):
        raise KernelError('INVALID_MODEL_REVIEW_STATE', '模型核对记录或其原始关联无法核对。')
    return {key: row[key] for key in ('review_id', 'attempt_id', 'task_id', 'observed_state', 'note', 'reviewed_at')} | {
        'disposition': 'acknowledged_unknown', 'reservation_refunded': False,
        'network_outcome_verified': False, 'automatic_execution': False}


def runtime_model_review_put(c, context, attempt, note):
    from sandbox import KernelError
    existing = runtime_model_review_get(c, context['scope_id'], attempt['attempt_id'])
    if existing is not None:
        if existing['note'] != note or existing['observed_state'] != attempt['state'] or existing['task_id'] != attempt['task_id']:
            raise KernelError('MODEL_REVIEW_CONFLICT', '该调用已有不同的核对记录，不能覆盖。')
        return existing
    c.execute('INSERT INTO runtime_model_reviews(attempt_id,review_id,task_id,scope_id,control_task_id,observed_state,note,reviewed_at) VALUES (?,?,?,?,?,?,?,?)',
              (attempt['attempt_id'], uuid.uuid4().hex, attempt['task_id'], context['scope_id'], context['task_id'],
               attempt['state'], note, time.time()))
    runtime_event(c, attempt['task_id'], context['scope_id'], 'draft.reply', 'model_review_acknowledged')
    return runtime_model_review_get(c, context['scope_id'], attempt['attempt_id'])


def runtime_model_usage(c, scope_id, cursor=None):
    from sandbox import KernelError
    day = runtime_model_day()
    stats = c.execute('SELECT count(*),coalesce(sum(reserved_units),0),coalesce(sum(total_tokens),0),count(total_tokens) FROM runtime_model_calls WHERE scope_id=? AND budget_day=?', (scope_id, day)).fetchone()
    where, args = 'scope_id=?', [scope_id]
    if cursor is not None:
        boundary = c.execute('SELECT created_at FROM runtime_model_calls WHERE attempt_id=? AND scope_id=?', (cursor, scope_id)).fetchone()
        if boundary is None:
            raise KernelError('INVALID_MODEL_CURSOR', '模型记录分页位置无效。', 400)
        where += ' AND (created_at<? OR (created_at=? AND attempt_id<?))'
        args += [boundary[0], boundary[0], cursor]
    fields = 'attempt_id,task_id,provider,model,budget_day,reserved_units,state,http_status,error_code,provider_request_id,prompt_tokens,completion_tokens,total_tokens,created_at,updated_at'
    rows = c.execute('SELECT ' + fields + ' FROM runtime_model_calls WHERE ' + where + ' ORDER BY created_at DESC,attempt_id DESC LIMIT 51', args).fetchall()
    items = [{**dict(row), 'usage_known': row['total_tokens'] is not None,
              'pending_or_unknown': row['state'] in {'reserved', 'sending', 'outcome_unknown'}} for row in rows[:50]]
    for item in items:
        item['review'] = runtime_model_review_get(c, scope_id, item['attempt_id'])
        item['review_required'] = item['pending_or_unknown'] and item['review'] is None
    return {'budget_day_utc': day, 'scope_daily': {'attempts': stats[0], 'reserved_units': stats[1],
            'reported_tokens': stats[2], 'usage_known_attempts': stats[3], 'usage_unknown_attempts': stats[0] - stats[3]},
            'items': items, 'next_cursor': items[-1]['attempt_id'] if len(rows) > 50 else None}


def runtime_document(c, scope_id, kind):
    row = c.execute("SELECT revision,content FROM runtime_documents WHERE scope_id=? AND kind=?",
                    (scope_id, kind)).fetchone()
    return {"revision": row[0], "content": json.loads(row[1])} if row else {"revision": 0, "content": {}}


def _norm_skills(v) -> str:
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v or "")


def upsert_job(c: sqlite3.Connection, job: dict, source: str, now: float) -> bool:
    """写岗位；已存在则只更新 last_seen 和缺失字段。返回是否新插入。"""
    jid = job.get("job_id")
    if not jid:
        return False
    row = c.execute("SELECT sources, first_seen FROM jobs WHERE job_id=?", (jid,)).fetchone()
    vals = {
        "job_id": jid,
        "security_id": job.get("security_id") or "",
        "title": job.get("title") or "",
        "company": job.get("company") or "",
        "salary": job.get("salary") or "",
        "city": job.get("city") or "",
        "district": job.get("district") or "",
        "experience": job.get("experience") or "",
        "education": job.get("education") or "",
        "skills": _norm_skills(job.get("skills")),
        "industry": job.get("industry") or "",
        "scale": job.get("scale") or "",
        "boss_name": job.get("boss_name") or "",
        "boss_title": job.get("boss_title") or "",
        "boss_active": job.get("boss_active") or "",
        "match_score": job.get("match_score"),
        "platform": job.get("platform") or "",
        "url": job.get("url") or "",
        "job_description": job.get("job_description") or "",
    }
    if row:
        srcs = set((row["sources"] or "").split(",")) - {""}
        srcs.add(source)
        sets = ", ".join(f"{f}=COALESCE(NULLIF(?,''),{f})" for f in JOB_FIELDS if f != "job_id")
        params = [vals[f] for f in JOB_FIELDS if f != "job_id"]
        c.execute(
            f"UPDATE jobs SET {sets}, last_seen=?, sources=? WHERE job_id=?",
            (*params, now, ",".join(sorted(srcs)), jid),
        )
        return False
    c.execute(
        f"INSERT INTO jobs ({', '.join(JOB_FIELDS)}, first_seen, last_seen, sources) "
        f"VALUES ({', '.join('?' * len(JOB_FIELDS))}, ?, ?, ?)",
        (*[vals[f] for f in JOB_FIELDS], now, now, source),
    )
    return True


def import_search_cache(c: sqlite3.Connection, now: float) -> tuple[int, int]:
    if not BOSS_DB.exists():
        return 0, 0
    src = sqlite3.connect(f"file:{BOSS_DB}?mode=ro", uri=True)
    new = seen = 0
    for cache_key, response, created in src.execute(
        "SELECT cache_key, response, created_at FROM search_cache"
    ):
        try:
            payload = json.loads(response)
        except (TypeError, json.JSONDecodeError):
            continue
        for job in payload.get("data") or []:
            seen += 1
            if upsert_job(c, job, "search_cache", float(created or now)):
                new += 1
    src.close()
    return new, seen


def import_index_cache(c: sqlite3.Connection, now: float) -> tuple[int, int]:
    if not INDEX_CACHE.exists():
        return 0, 0
    try:
        d = json.loads(INDEX_CACHE.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return 0, 0
    jobs = d.get("jobs") or []
    saved = float(d.get("saved_at") or now)
    new = 0
    for job in jobs:
        if upsert_job(c, job, "index_cache", saved):
            new += 1
    return new, len(jobs)


def import_greets(c: sqlite3.Connection) -> int:
    """导入投递记录。注意：原库只有 job_id，公司/岗位名通过 jobs 表 join 补全。"""
    n = 0
    if BOSS_DB.exists():
        src = sqlite3.connect(f"file:{BOSS_DB}?mode=ro", uri=True)
        try:
            rows = list(src.execute("SELECT security_id, job_id, greeted_at FROM greet_records"))
        except sqlite3.OperationalError:
            rows = []
        src.close()
        for sid, jid, ts in rows:
            if _upsert_greet(c, jid, sid, ts, None, "sent", None):
                n += 1
    # 旧版纯文本去重文件（无时间戳）
    if GREETED_TXT.exists():
        for line in GREETED_TXT.read_text(encoding="utf-8", errors="replace").splitlines():
            jid = line.strip()
            if jid and _upsert_greet(c, jid, None, None, None, "sent", "来源: .boss_greeted.txt"):
                n += 1
    return n


def _upsert_greet(c, job_id, security_id, ts, message, status, error) -> bool:
    if not job_id:
        return False
    if c.execute("SELECT 1 FROM greetings WHERE job_id=?", (job_id,)).fetchone():
        return False
    c.execute(
        "INSERT INTO greetings (job_id, security_id, greeted_at, message, status, error) "
        "VALUES (?,?,?,?,?,?)",
        (job_id, security_id, ts, message, status, error),
    )
    return True


# 投递时存哪些字段：够台账展示 + 够点回原岗位，但不存整个 JD（体积大、
# 台账也不看）。
_SNAP_FIELDS = ("title", "company", "salary", "city", "district", "url",
                "platform", "match_score", "education", "experience",
                "boss_name", "boss_active")


def _snapshot(job: dict | None) -> str | None:
    if not job:
        return None
    snap = {k: job[k] for k in _SNAP_FIELDS if job.get(k) not in (None, "")}
    return json.dumps(snap, ensure_ascii=False) if snap else None


def _active_resume_id(c: sqlite3.Connection) -> int:
    """当前启用简历的 id；没版本或出任何问题都返回 0（未知），不抛。"""
    try:
        row = c.execute(
            "SELECT resume_id FROM resumes WHERE active=1 LIMIT 1").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def record_greet(c: sqlite3.Connection, job_id: str, security_id: str,
                 message: str, status: str, error: str | None = None,
                 job: dict | None = None) -> None:
    """投递流程写完一条就落库（幂等：已存在则更新状态）。

    `job` 传岗位 dict：会把展示用字段快照存进 greetings.job_snapshot。
    为什么必须快照：岗位会被新搜索覆盖、也可能下架，jobs 表里查不到时
    台账就只剩一个 job_id —— 早期 28 条投递全成了「岗位信息缺失」，只能
    看着一堆占位符。

    resume_id 自动取当前启用版本 —— 调用方不用管，也避免漏记。
    """
    c.execute(
        "INSERT INTO greetings (job_id, security_id, greeted_at, message, status, error, job_snapshot, resume_id, attempted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET greeted_at=excluded.greeted_at, "
        "status=excluded.status, error=excluded.error, message=excluded.message, "
        "job_snapshot=COALESCE(excluded.job_snapshot, greetings.job_snapshot), "
        "attempted_at=excluded.attempted_at, "
        # 已有有效版本号时不被 0 冲掉（重试/改状态不应丢归因）
        "resume_id=COALESCE(NULLIF(excluded.resume_id,0), greetings.resume_id)",
        (job_id, security_id, time.time() if status == "sent" else None,
         message, status, error, _snapshot(job), _active_resume_id(c), time.time()),
    )
    c.commit()


CONV_COLS = ["hr_name", "company", "position", "last_msg", "msg_hash",
             "category", "from_me", "last_time", "avatar", "hr_read", "hr_read_at"]


def upsert_conversations(c: sqlite3.Connection, rows: list[dict],
                         now: float | None = None) -> dict:
    """把一次聊天页抓取结果写库。

    返回 {"added", "changed", "unchanged", "pending", "new_replies"}。

    - 新会话：插入；只有待处理的才记 pending_since（用来算「放了多久没回」）
    - 老会话最后一条变了：更新内容，并把 handled 归零（有新消息，得重新处理）
    - 没变：只刷新 updated_at，**不动 handled**——不能把用户标记过的「已处理」冲掉
    """
    now = now if now is not None else time.time()
    added = changed = unchanged = new_replies = 0
    for r in rows:
        key = r["conv_key"]
        h = r.get("msg_hash") or r.get("hash") or ""
        from_me = 1 if r.get("from_me") else 0
        vals = [
            r.get("hr", r.get("hr_name")) or "",
            r.get("company") or "",
            r.get("position") or "",
            r.get("last_msg") or "",
            h,
            r.get("category") or "",
            from_me,
            r.get("time", r.get("last_time")) or "",
            r.get("avatar") or "",
            1 if r.get("hr_read") else 0,
            now if r.get("hr_read") else None,   # 首次入库就已读：以入库时刻为近似的已读时刻
        ]
        old = c.execute(
            "SELECT msg_hash, from_me FROM conversations WHERE conv_key=?", (key,)
        ).fetchone()
        if old is None:
            c.execute(
                f"INSERT INTO conversations (conv_key, {', '.join(CONV_COLS)}, "
                "first_seen, updated_at, pending_since, handled) "
                f"VALUES (?, {', '.join('?' * len(CONV_COLS))}, ?, ?, ?, 0)",
                (key, *vals, now, now, None if from_me else now),
            )
            added += 1
        elif old["msg_hash"] != h:
            sets = ", ".join(f"{col}=?" for col in CONV_COLS)
            c.execute(
                f"UPDATE conversations SET {sets}, updated_at=?, handled=0, "
                # 直接用新值，不用 COALESCE：对方又发消息 = 新的待处理开始，
                # 旧 pending_since 会让「放了多久没回」越算越离谱。
                "pending_since=? WHERE conv_key=?",
                (*vals, now, None if from_me else now, key),
            )
            changed += 1
            if not from_me:
                new_replies += 1
        else:
            # hash 没变 ≠ 没变化：对方「已读」不会改消息内容，只改 hr_read。
            # 只刷新 updated_at 的话，已读状态永远进不了库 ——
            # 而「已读未回」正是靠这个字段判的。所以这一列必须单独写。
            # hr_read_at 只在 0→1 的那一刻记一次，后续抓取不覆盖（否则时间总被刷成现在）。
            c.execute(
                "UPDATE conversations SET hr_read=?, "
                "hr_read_at=CASE WHEN ?=1 AND (COALESCE(hr_read,0)=0 OR hr_read_at IS NULL) "
                "THEN ? ELSE hr_read_at END "
                "WHERE conv_key=?",
                (1 if r.get("hr_read") else 0,
                 1 if r.get("hr_read") else 0,
                 now, key),
            )
            unchanged += 1
    c.commit()
    pending = c.execute(
        "SELECT COUNT(*) FROM conversations WHERE from_me=0 AND handled=0"
    ).fetchone()[0]
    return {"added": added, "changed": changed, "unchanged": unchanged,
            "pending": pending, "new_replies": new_replies}


def list_conversations(c: sqlite3.Connection, pending_only: bool = False,
                       limit: int = 200) -> list[dict]:
    """按「待处理优先、时间新的在前」返回会话。

    附带 age_hours：待处理已放了多久（小时）。注意这是**我们第一次看到它**
    到现在的时间，不是 HR 发消息到现在——BOSS 页面只给「昨天/刚刚」这种
    相对时间，拿不到确切时刻。所以界面文案要说「已挂起」，不能说「HR 等了」。
    """
    sql = ("SELECT * FROM conversations"
           + (" WHERE from_me=0 AND handled=0" if pending_only else "")
           + " ORDER BY (from_me=0 AND handled=0) DESC, updated_at DESC LIMIT ?")
    now = time.time()
    rows = []
    for r in c.execute(sql, (limit,)):
        d = dict(r)
        ps = d.get("pending_since")
        d["age_hours"] = round((now - ps) / 3600, 1) if ps else None
        rows.append(d)
    return rows


# ── 公司名归一化（跨表匹配）──────────────────────────────────────
# 血教训：会话表里 BOSS 给的公司名比岗位表短（「上海兴岩信息科技」 vs
# 「上海兴岩信息科技有限公司」），直接等值比较 → 交集 0，漏斗永远是断的。
_COMPANY_NOISE_RE = re.compile(
    r"(股份有限公司|有限责任公司|有限公司|集团|控股|"
    r"信息科技|信息技术|网络科技|智能科技|数字科技|科技|技术|信息|服务|"
    r"[（(].*?[)）])")
_REGION_PREFIX_RE = re.compile(r"^(广州市|深圳市|东莞市|佛山市|惠州市|珠海市|广东省|广东|广州|深圳)")


def company_key(name: str) -> str:
    """把公司名压成可比较的 key（去掉法律后缀、行业词、地域前缀）。"""
    s = re.sub(r"\s+", "", str(name or ""))
    if not s:
        return ""
    s = _COMPANY_NOISE_RE.sub("", s)
    s = _REGION_PREFIX_RE.sub("", s)
    return s


def companies_match(a: str, b: str) -> bool:
    """两家公司是不是同一家。

    保守策略：**宁可不匹配，也不错配**。短的包含在长的里、且短名至少 3 个字
    才算同一家 —— 否则「华为」会把「华为云」也吞进来。
    """
    ka, kb = company_key(a), company_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    return len(short) >= 3 and short in long_


def link_conversations(c: sqlite3.Connection) -> list[dict]:
    """给每个会话找到对应岗位（按公司名匹配，取该公司匹配度最高的岗位）。

    找不到时 job_id 为空字符串 —— 明确表示「没对上」，不要塞一个瞎猜的岗位。
    """
    jobs = [dict(r) for r in c.execute(
        "SELECT job_id, title, company, salary, match_score FROM jobs")]
    convs = [dict(r) for r in c.execute(
        "SELECT conv_key, company, hr_name, position, last_msg, category, "
        "from_me, last_time, handled FROM conversations")]

    out = []
    for cv in convs:
        best = None
        for j in jobs:
            if not companies_match(cv.get("company") or "", j.get("company") or ""):
                continue
            if best is None or (j.get("match_score") or 0) > (best.get("match_score") or 0):
                best = j
        out.append({
            **cv,
            "job_id": (best or {}).get("job_id", ""),
            "job_title": (best or {}).get("title", ""),
            "job_salary": (best or {}).get("salary", ""),
        })
    return out


def already_greeted(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute(
        "SELECT job_id FROM greetings WHERE status='sent'"
    )}


def delivery_blocked_jobs(c: sqlite3.Connection) -> set[str]:
    """Keep confirmed, in-flight and uncertain writes out of automatic replay."""
    return {r[0] for r in c.execute(
        "SELECT job_id FROM greetings WHERE status IN ('sent','executing','outcome_unknown')"
    )}


def claim_greet(c: sqlite3.Connection, job_id: str, security_id: str,
                message: str, job: dict | None = None) -> bool:
    """Atomically persist intent before sending; only one process can claim it."""
    cur = c.execute(
        "INSERT INTO greetings (job_id,security_id,message,status,job_snapshot,resume_id,attempted_at) "
        "VALUES (?,?,?,'executing',?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET security_id=excluded.security_id, "
        "message=excluded.message,status='executing',error=NULL,greeted_at=NULL, "
        "job_snapshot=COALESCE(excluded.job_snapshot,greetings.job_snapshot), "
        "resume_id=COALESCE(NULLIF(excluded.resume_id,0),greetings.resume_id), "
        "attempted_at=excluded.attempted_at "
        "WHERE greetings.status NOT IN ('sent','executing','outcome_unknown')",
        (job_id, security_id, message, _snapshot(job), _active_resume_id(c), time.time()),
    )
    claimed = cur.rowcount == 1
    c.commit()
    return claimed


def sync() -> dict:
    c = connect()
    now = time.time()
    n1, s1 = import_search_cache(c, now)
    n2, s2 = import_index_cache(c, now)
    n3 = import_greets(c)
    c.commit()
    stats = {
        "jobs_total": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "jobs_new": n1 + n2,
        "search_cache_entries": s1,
        "index_cache_entries": s2,
        "greetings_total": c.execute("SELECT COUNT(*) FROM greetings").fetchone()[0],
        "greetings_new": n3,
    }
    c.close()
    return stats


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "sync":
        s = sync()
        print(f"✅ 同步完成")
        print(f"   岗位库 {s['jobs_total']} 条（本次新增 {s['jobs_new']}，"
              f"来自 search_cache {s['search_cache_entries']} 条 / index_cache {s['index_cache_entries']} 条）")
        print(f"   投递记录 {s['greetings_total']} 条（本次新增 {s['greetings_new']}）")
        return 0
    if cmd == "stats":
        c = connect()
        print(f"岗位: {c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}")
        print(f"投递: {c.execute('SELECT COUNT(*) FROM greetings').fetchone()[0]}")
        for r in c.execute("SELECT company, COUNT(*) n FROM jobs GROUP BY company ORDER BY n DESC LIMIT 5"):
            print(f"  {r['company']}: {r['n']}")
        c.close()
        return 0
    print(f"未知命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
