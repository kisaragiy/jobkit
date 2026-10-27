"""Registered local tools. Brain remains pure; the kernel owns all task state."""
import json
import hashlib
import re
import time
import uuid
import os
from datetime import datetime, timezone
import store
import reply_draft
import recruiter_reply
import llm_draft
import llm_transport
import database_backup
import run_state
import retention
import applicant_forget
import applicant_jobs
import applicant_action
import applicant_conversation
import applicant_greeting
import applicant_send_ledger
import sandbox
import applicant_job_search
import applicant_browser
import applicant_salary_review
import applicant_binding
import applicant_reply
import applicant_reply_window
import applicant_reply_windows
import applicant_send
import math
from copy import deepcopy
from execution_engine import check_context, check_manual_draft, public_task, record_commit
from sandbox import KernelError
from tool_router import Tool
from workspace_records import validate_record_graph, MAX_BYTES as EXPORT_MAX_BYTES, MAX_TASKS as EXPORT_MAX_TASKS, MAX_EDITS as EXPORT_MAX_EDITS
from restore_preflight import validate_snapshot
from workspace_bundle import archived_projection, combine_snapshot
from task_lease import task_lease

FACT_FIELDS = {"title", "location", "salary", "hours", "duties", "process", "source"}
PROFILE_FIELDS = set(reply_draft.PROFILE_FIELDS)
QUEUE_STAGES = ("queued", "drafted", "needs_review", "attention", "running", "history")


def queue_stage(payload, result, status, manual_revision, manual_text, successor, review, document_revision):
    """Pure projection of existing facts, not a second persisted task status."""
    if successor is not None or review == "superseded":
        return "history"
    try:
        source = json.loads(payload)
        if (not isinstance(source, dict) or not isinstance(source.get("message"), str)
                or not source["message"].strip() or type(source.get("document_revision")) is not int
                or document_revision < 1 or source["document_revision"] != document_revision):
            return "attention"
        if status == "executing":
            return "running"
        if status == "queued":
            return "queued"
        if status != "completed":
            return "attention"
        output = json.loads(result)
        if (not isinstance(output, dict) or type(output.get("document_revision")) is not int
                or output["document_revision"] != document_revision or not isinstance(output.get("text"), str)
                or type(manual_revision) is not int or manual_revision < 0
                or (manual_revision == 0 and manual_text is not None)
                or (manual_revision > 0 and (not isinstance(manual_text, str) or not manual_text.strip()))):
            return "attention"
        if output["text"].strip():
            return "drafted"
        return "needs_review" if isinstance(output.get("reason"), str) and output["reason"].strip() else "attention"
    except (TypeError, ValueError, RecursionError):
        return "attention"


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise KernelError("INVALID_IDENTITY", "请填写不超过 128 字的企业和账号标识。", 400)
    return value.strip()


def context_get(payload, context):
    return {key: value for key, value in context.items() if key != "task_id"}


def context_switch(payload, context):
    if set(payload) - {"role", "organization_id", "account_id"}:
        raise KernelError("INVALID_INPUT", "身份切换参数不受支持。", 400)
    role = payload.get("role")
    if role not in {"recruiter", "applicant"}:
        raise KernelError("INVALID_ROLE", "请选择招聘者或求职者。", 400)
    organization = _identity(payload.get("organization_id"))
    account = _identity(payload.get("account_id"))
    scope_id = store.runtime_scope_id(role, organization, account)
    with store.runtime_db() as c:
        c.execute("BEGIN IMMEDIATE")
        check_context(c, context["context_token"])
        busy = c.execute("SELECT 1 FROM runtime_tasks WHERE scope_id=? AND effect='external_write' "
                         "AND status IN ('executing','outcome_unknown') LIMIT 1", (context["scope_id"],)).fetchone()
        if busy:
            raise KernelError("UNRESOLVED_EXTERNAL_ACTION", "仍有外部动作待核对，暂不能切换身份。")
        c.execute("INSERT OR IGNORE INTO runtime_scopes VALUES (?,?,?,?)", (scope_id, role, organization, account))
        stale = c.execute("SELECT task_id,tool FROM runtime_tasks WHERE scope_id=? AND kind='work' AND status IN ('queued','executing') "
                          "AND task_id<>?", (context["scope_id"], context["task_id"])).fetchall()
        for row in stale:
            c.execute("UPDATE runtime_tasks SET status='cancelled',error_code='CONTEXT_SWITCHED',updated_at=? WHERE task_id=?",
                      (time.time(), row["task_id"]))
            store.runtime_event(c, row["task_id"], context["scope_id"], row["tool"], "cancelled", "CONTEXT_SWITCHED")
        c.execute("UPDATE runtime_control SET scope_id=?,context_token=?,generation=generation+1 WHERE id=1",
                  (scope_id, uuid.uuid4().hex))
        return record_commit(c, context, "runtime.switch", store.runtime_context(c))


def document_get(payload, context):
    kind = "facts" if context["role"] == "recruiter" else "profile"
    with store.runtime_db() as c:
        check_context(c, context["context_token"])
        return {"kind": kind, **store.runtime_document(c, context["scope_id"], kind)}


def document_save(payload, context):
    values = payload.get("content")
    revision = payload.get("expected_revision")
    allowed = FACT_FIELDS if context["role"] == "recruiter" else PROFILE_FIELDS
    if (set(payload) - {"content", "expected_revision", "confirmed"}
            or not isinstance(values, dict) or set(values) - allowed or type(revision) is not int
            or payload.get("confirmed") is not True
            or any(not isinstance(value, str) or len(value) > 2000 for value in values.values())):
        raise KernelError("INVALID_DOCUMENT", "请核对资料字段并确认内容准确。", 400)
    if context["role"] == "recruiter" and not values.get("source", "").strip():
        raise KernelError("FACT_SOURCE_REQUIRED", "请填写岗位资料来源。", 400)
    kind = "facts" if context["role"] == "recruiter" else "profile"
    with store.runtime_db() as c:
        c.execute("BEGIN IMMEDIATE")
        check_context(c, context["context_token"])
        current = store.runtime_document(c, context["scope_id"], kind)
        if current["revision"] != revision:
            raise KernelError("STALE_DOCUMENT", "资料已更新，请重新加载后再保存。")
        c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
                  "DO UPDATE SET revision=excluded.revision,content=excluded.content",
                  (context["scope_id"], kind, revision + 1, json.dumps(values, ensure_ascii=False)))
        return record_commit(c, context, "runtime.document.save", {"kind": kind, "revision": revision + 1, "content": values})


def draft_reply(payload, context):
    config = llm_draft.configuration()
    if payload.get('draft_backend', 'rules') != config.backend:
        raise KernelError('DRAFT_BACKEND_CHANGED', '草稿后端配置已变更，请核对后明确新建任务。')
    if config.backend == 'llm' and payload.get('llm_binding') != llm_draft.transport_binding():
        raise KernelError('LLM_CONFIG_CHANGED', '模型服务或外发配置已变更，请核对后明确新建任务。')
    with store.runtime_db() as c:
        check_context(c, context["context_token"])
        document = store.runtime_document(c, context["scope_id"], payload["document_kind"])
    if context["role"] == "recruiter":
        result = recruiter_reply.draft(payload["message"], document["content"])
    else:
        draft = reply_draft.draft(payload["message"], document["content"])
        result = {"state": "drafted" if draft["text"] else "needs_review", **draft}
    if config.backend == 'llm':
        result = llm_draft.compose(result, role=context['role'], message=payload['message'],
                                   content=document['content'], config=config)
        if payload['llm_binding'] != llm_draft.transport_binding():
            raise KernelError('LLM_CONFIG_CHANGED', '模型配置已变更，未采用迟到响应。')
    return {**result, "document_revision": document["revision"], "sent": False}


def workspace_export(payload, context):
    if payload:
        raise KernelError("INVALID_EXPORT", "导出仅限当前工作区，不接受额外范围或路径。", 400)
    with store.runtime_db() as c:
        c.execute("BEGIN")
        check_context(c, context["context_token"])
        scope = context["scope_id"]
        imported = c.execute('SELECT * FROM runtime_workspace_imports WHERE target_scope_id=?', (scope,)).fetchone()
        kind = "facts" if context["role"] == "recruiter" else "profile"
        try:
            document = store.runtime_document(c, scope, kind)
            if not isinstance(document["content"], dict):
                raise ValueError("invalid document")
        except (TypeError, ValueError, RecursionError):
            raise KernelError("INVALID_EXPORT_STATE", "资料无法完整读取，导出已停止，请先核对记录。") from None
        count, size = c.execute("SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))+coalesce(length(cast(result AS BLOB)),0)+length(cast(conversation_note AS BLOB))+coalesce(length(cast(manual_text AS BLOB)),0)),0) FROM runtime_tasks WHERE scope_id=? AND kind='work' AND tool='draft.reply'", (scope,)).fetchone()
        edit_count, edit_size = c.execute("SELECT count(*),coalesce(sum(length(cast(text AS BLOB))),0) FROM runtime_draft_edits WHERE scope_id=?", (scope,)).fetchone()
        if count > EXPORT_MAX_TASKS or edit_count > EXPORT_MAX_EDITS or size + edit_size > EXPORT_MAX_BYTES:
            raise KernelError("EXPORT_TOO_LARGE", "记录超过当前导出容量，未生成截断文件。请保留原数据库，联系维护人员分批导出。", 413)
        rows = c.execute("SELECT * FROM runtime_tasks WHERE scope_id=? AND kind='work' AND tool='draft.reply' ORDER BY created_at,task_id", (scope,)).fetchall()
        tasks = []
        for row in rows:
            task = public_task(row)
            if task.get("input_state") != "readable" or task.get("result_state") == "unreadable":
                raise KernelError("INVALID_EXPORT_STATE", "存在无法完整读取的消息或草稿，导出已停止，请先核对记录。")
            check_manual_draft(c, row)
            tasks.append({key: value for key, value in task.items() if key not in {"scope_id", "parent_task_id", "tool"}})
        edits = [dict(row) for row in c.execute("SELECT task_id,revision,document_revision,text,created_at FROM runtime_draft_edits WHERE scope_id=? ORDER BY task_id,revision", (scope,))]
        validate_record_graph(tasks, edits)
        snapshot = {"format": "jobkit.workspace-records", "format_version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "workspace": {key: context[key] for key in ("scope_id", "role", "organization_id", "account_id")},
                    "document": {"kind": kind, **document}, "tasks": tasks, "draft_edits": edits,
                    "counts": {"tasks": len(tasks), "draft_edits": len(edits)},
                    "restore_supported": False, "platform_send_evidence": False}
        if imported is not None:
            source, mapping, import_result = checked_import(imported)
            snapshot = combine_snapshot(snapshot, source, mapping, import_result['import_id'])
            if snapshot['counts']['tasks'] > EXPORT_MAX_TASKS or snapshot['counts']['draft_edits'] > EXPORT_MAX_EDITS:
                raise KernelError('EXPORT_TOO_LARGE', '合并记录超过导出容量，未生成截断文件。', 413)
        try:
            validate_snapshot(json.dumps(snapshot, ensure_ascii=False, allow_nan=False), scope_id=store.runtime_scope_id, fact_fields=FACT_FIELDS, profile_fields=PROFILE_FIELDS)
        except KernelError as error:
            if error.code == 'RESTORE_TOO_LARGE':
                raise KernelError('EXPORT_TOO_LARGE', '记录超过导出容量，未生成截断文件。', 413) from None
            raise KernelError('INVALID_EXPORT_STATE', '记录或来源关系无法完整核对，未生成文件。') from None
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise KernelError('INVALID_EXPORT_STATE', '记录无法完整序列化，未生成文件。') from None
        try:
            size = len(json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"))
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise KernelError("INVALID_EXPORT_STATE", "记录无法完整序列化，导出已停止，请先核对。") from None
        if size > EXPORT_MAX_BYTES:
            raise KernelError("EXPORT_TOO_LARGE", "记录超过当前导出容量，未生成截断文件。", 413)
        return snapshot


def restore_target_exists(c, scope):
    return (any(c.execute('SELECT 1 FROM ' + table + ' WHERE scope_id=? LIMIT 1', (scope,)).fetchone()
                for table in ('runtime_scopes', 'runtime_documents', 'runtime_tasks', 'runtime_draft_edits'))
            or c.execute('SELECT 1 FROM runtime_workspace_imports WHERE target_scope_id=?', (scope,)).fetchone() is not None)


def checked_import(row):
    try:
        data, digest = validate_snapshot(row['source_snapshot'], scope_id=store.runtime_scope_id, fact_fields=FACT_FIELDS, profile_fields=PROFILE_FIELDS)
        mapping, result = json.loads(row['task_id_map']), json.loads(row['result'])
        expected_mapping = {task['task_id']: uuid.uuid5(uuid.UUID(row['import_id']), task['task_id']).hex for task in data['tasks']}
        target = result['target_workspace']
        if (mapping != expected_mapping or set(mapping.values()) & set(mapping)
                or digest != row['content_sha256'] or result['content_sha256'] != digest
                or result['import_id'] != row['import_id'] or result['owner_scope_id'] != row['owner_scope_id']
                or result['counts'] != data['counts'] or result['document_revision'] != data['document']['revision']
                or result['automatic_execution'] is not False or result['history_execution_verified'] is not False
                or target['role'] != data['workspace']['role'] or target['scope_id'] != row['target_scope_id']
                or target['scope_id'] != store.runtime_scope_id(target['role'], target['organization_id'], target['account_id'])
                or row['fingerprint'] != hashlib.sha256(json.dumps([digest, row['target_scope_id']]).encode()).hexdigest()):
            raise ValueError('invalid import state')
        return data, mapping, result
    except (KernelError, TypeError, ValueError, KeyError, AttributeError, RecursionError):
        raise KernelError('INVALID_RESTORED_ARCHIVE', '恢复档案或映射无法核对，请人工检查。') from None


def restore_preflight(payload, context):
    if set(payload) != {'snapshot_text', 'target'} or not isinstance(payload['target'], dict) or set(payload['target']) != {'organization_id', 'account_id'}:
        raise KernelError('INVALID_RESTORE_REQUEST', '请提供导出文件正文及新的目标工作区标识，不接受文件路径或执行参数。', 400)
    target = payload['target']
    organization, account = _identity(target['organization_id']), _identity(target['account_id'])
    data, digest = validate_snapshot(payload['snapshot_text'], scope_id=store.runtime_scope_id, fact_fields=FACT_FIELDS, profile_fields=PROFILE_FIELDS)
    role = data['workspace']['role']
    target_scope = store.runtime_scope_id(role, organization, account)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        exists = restore_target_exists(c, target_scope)
    issues = [{'code': 'TARGET_EXISTS', 'message': '目标工作区已存在，请使用新的标识；不会覆盖现有数据。'}] if exists else []
    tasks = data['tasks']
    return {'preflight_passed': not issues, 'restore_available': True, 'restore_mode': 'new_workspace_with_archived_records', 'automatic_execution': False,
            'content_sha256': digest, 'source_workspace': data['workspace'],
            'target_workspace': {'scope_id': target_scope, 'role': role, 'organization_id': organization, 'account_id': account},
            'counts': data['counts'], 'document_revision': data['document']['revision'], 'issues': issues,
            'review_required': {state: sum(t['status'] == state for t in tasks) for state in ('queued', 'executing', 'outcome_unknown')},
            'warnings': [{'code': 'UNVERIFIED_HISTORY', 'message': '文件不是平台发送或本机原始执行证明；历史任务不会自动执行。'},
                         {'code': 'CURRENT_DOCUMENT_ONLY', 'message': '文件仅包含各来源快照中的确认资料，不保证具备所有较早版本的完整内容。'}]}


def restore_commit(payload, context):
    if (set(payload) != {'snapshot_text', 'target', 'expected_content_sha256', 'idempotency_key', 'confirmed'}
            or payload['confirmed'] is not True or not isinstance(payload['idempotency_key'], str)
            or not payload['idempotency_key'].strip() or len(payload['idempotency_key']) > 128
            or not isinstance(payload['expected_content_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', payload['expected_content_sha256'])
            or not isinstance(payload['target'], dict) or set(payload['target']) != {'organization_id', 'account_id'}):
        raise KernelError('INVALID_RESTORE_REQUEST', '请确认恢复范围、预检内容摘要和请求标识。', 400)
    organization = _identity(payload['target']['organization_id'])
    account = _identity(payload['target']['account_id'])
    data, digest = validate_snapshot(payload['snapshot_text'], scope_id=store.runtime_scope_id, fact_fields=FACT_FIELDS, profile_fields=PROFILE_FIELDS)
    if digest != payload['expected_content_sha256']:
        raise KernelError('RESTORE_CONTENT_CHANGED', '文件与预检摘要不一致，未恢复数据。')
    role = data['workspace']['role']
    scope = store.runtime_scope_id(role, organization, account)
    key = hashlib.sha256(payload['idempotency_key'].encode('utf-8')).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([digest, scope]).encode()).hexdigest()
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        existing = c.execute('SELECT * FROM runtime_workspace_imports WHERE owner_scope_id=? AND request_key_digest=?', (context['scope_id'], key)).fetchone()
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise KernelError('IDEMPOTENCY_CONFLICT', '相同恢复请求标识不能用于不同文件或目标。')
            _data, _mapping, result = checked_import(existing)
            return record_commit(c, context, 'runtime.workspace.restore.commit', result)
        if restore_target_exists(c, scope):
            raise KernelError('RESTORE_TARGET_EXISTS', '目标工作区已存在，未覆盖任何数据。')
        import_id, now = uuid.uuid4().hex, time.time()
        mapping = {task['task_id']: uuid.uuid5(uuid.UUID(import_id), task['task_id']).hex for task in data['tasks']}
        if (len(set(mapping.values())) != len(mapping) or set(mapping.values()) & set(mapping)
                or any(origin['origin_id'] == import_id for origin in data.get('origins', []))):
            raise KernelError('RESTORE_ID_CONFLICT', '恢复记录标识冲突，未写入数据。')
        result = {'import_id': import_id, 'owner_scope_id': context['scope_id'], 'content_sha256': digest,
                  'target_workspace': {'scope_id': scope, 'role': role, 'organization_id': organization, 'account_id': account},
                  'counts': data['counts'], 'document_revision': data['document']['revision'],
                  'automatic_execution': False, 'history_execution_verified': False}
        c.execute('INSERT INTO runtime_scopes VALUES (?,?,?,?)', (scope, role, organization, account))
        document = data['document']
        c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)', (scope, document['kind'], document['revision'], json.dumps(document['content'], ensure_ascii=False)))
        c.execute('INSERT INTO runtime_workspace_imports VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (import_id, context['scope_id'], key, fingerprint, digest, scope, json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False),
                   json.dumps(mapping), json.dumps(result, ensure_ascii=False), now))
        return record_commit(c, context, 'runtime.workspace.restore.commit', result)


def restored_get(payload, context):
    if set(payload) - {'view'} or payload.get('view', 'records') not in ('records', 'source'):
        raise KernelError('INVALID_RESTORE_REQUEST', '恢复档案仅支持 records 或 source 视图。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        row = c.execute('SELECT * FROM runtime_workspace_imports WHERE target_scope_id=?', (context['scope_id'],)).fetchone()
        if row is None:
            raise KernelError('RESTORED_ARCHIVE_NOT_FOUND', '当前工作区没有恢复档案。', 404)
        data, mapping, result = checked_import(row)
        if payload.get('view') == 'source':
            return {'import': result, 'source_snapshot': data}
        records, edits, origins = archived_projection(data, mapping, result['import_id'])
        inverse = {mapped: original for original, mapped in mapping.items()}
        records = [{**record, 'source_file_task_id': inverse[record['task_id']], 'source_type': 'restored_history',
                    'execution_origin': 'restored_history', 'executed_here': False} for record in records]
        return {'import': result, 'records': records, 'draft_edits': edits, 'origins': origins, 'source_workspace': data['workspace']}


def model_usage(payload, context):
    cursor = payload.get('cursor')
    if (set(payload) - {'cursor'} or (cursor is not None and
            (not isinstance(cursor, str) or len(cursor) != 32 or any(ch not in '0123456789abcdef' for ch in cursor)))):
        raise KernelError('INVALID_MODEL_CURSOR', '模型记录分页参数无效。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        report = store.runtime_model_usage(c, context['scope_id'], cursor)
    return {**report, 'limits': llm_transport.limits(), 'reservation_refunds': False,
            'reported_usage_is_billing_proof': False}


def model_readiness(payload, context):
    if payload:
        raise KernelError('INVALID_MODEL_READINESS', '配置检查不接受密钥、地址或执行参数。', 400)
    snapshot = {}
    for name in ('JOBKIT_DRAFT_BACKEND', 'JOBKIT_LLM_TIMEOUT_SECONDS', 'JOBKIT_LLM_PROVIDER',
                 'JOBKIT_LLM_MODEL', 'JOBKIT_LLM_API_KEY', 'JOBKIT_LLM_EXTERNAL_ALLOWED',
                 'JOBKIT_LLM_DAILY_REQUEST_LIMIT', 'JOBKIT_LLM_DAILY_RESERVATION_LIMIT'):
        value = os.environ.get(name)
        if value is not None:
            snapshot[name] = value
    issues, config, policy = [], None, None
    try:
        config = llm_draft.configuration(snapshot)
    except KernelError as exc:
        issues.append(exc.code)
    try:
        llm_transport.settings(snapshot)
        provider_valid = True
    except KernelError as exc:
        provider_valid = False
        issues.append(exc.code)
    try:
        policy = llm_transport.limits(snapshot)
    except KernelError as exc:
        issues.append(exc.code)
    if config is not None and config.backend != 'llm':
        issues.append('RULES_MODE_SELECTED')
    model = snapshot.get('JOBKIT_LLM_MODEL', '')
    return {
        'configuration_ready': config is not None and config.backend == 'llm' and provider_valid and policy is not None,
        'backend': config.backend if config else None,
        'timeout_seconds': config.timeout_seconds if config else None,
        'provider': 'deepseek' if snapshot.get('JOBKIT_LLM_PROVIDER') == 'deepseek' else None,
        'model': model if model in llm_transport.MODELS else None,
        'credential_configured': llm_transport.credential_configured(snapshot),
        'external_permission_configured': snapshot.get('JOBKIT_LLM_EXTERNAL_ALLOWED') == 'yes',
        'limits': policy, 'issues': sorted(set(issues)),
        'network_checked': False, 'account_verified': False,
        'quota_checked': False, 'concurrency_checked': False, 'automatic_execution': False,
    }


def model_review(payload, context):
    if (set(payload) != {'attempt_id', 'expected_state', 'note', 'confirmed'} or payload.get('confirmed') is not True
            or not isinstance(payload.get('attempt_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', payload['attempt_id'])
            or not isinstance(payload.get('expected_state'), str) or payload['expected_state'] not in {'reserved', 'sending', 'outcome_unknown'}
            or not isinstance(payload.get('note'), str) or not payload['note'].strip() or len(payload['note']) > 500):
        raise KernelError('INVALID_MODEL_REVIEW', '请确认调用标识、原状态和不超过 500 字的人工核对备注。', 400)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        attempt = c.execute('SELECT * FROM runtime_model_calls WHERE attempt_id=? AND scope_id=?',
                            (payload['attempt_id'], context['scope_id'])).fetchone()
        if attempt is None:
            raise KernelError('MODEL_CALL_NOT_FOUND', '当前工作区没有该模型调用记录。', 404)
        source_task = attempt['task_id']
    with task_lease(source_task), store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        attempt = c.execute('SELECT * FROM runtime_model_calls WHERE attempt_id=? AND scope_id=?',
                            (payload['attempt_id'], context['scope_id'])).fetchone()
        if attempt is None or attempt['task_id'] != source_task:
            raise KernelError('MODEL_CALL_NOT_FOUND', '模型调用关联无法核对。', 404)
        if attempt['state'] != payload['expected_state']:
            raise KernelError('STALE_MODEL_CALL', '模型调用状态已变化，请重新读取后核对。')
        task = c.execute('SELECT tool,kind,status FROM runtime_tasks WHERE task_id=? AND scope_id=?',
                         (source_task, context['scope_id'])).fetchone()
        if task is None or task['tool'] != 'draft.reply' or task['kind'] != 'work':
            raise KernelError('INVALID_MODEL_CALL_STATE', '原草稿任务关联无法核对。')
        if task['status'] not in {'completed', 'failed', 'blocked', 'cancelled', 'outcome_unknown'}:
            raise KernelError('MODEL_TASK_NOT_TERMINAL', '请先通过原任务恢复入口核对中断状态，再确认模型调用。')
        result = store.runtime_model_review_put(c, context, attempt, payload['note'].strip())
        return record_commit(c, context, 'runtime.model.review', result)


def applicant_only(context):
    if context['role'] != 'applicant':
        raise KernelError('APPLICANT_ONLY', '请先切换到求职者工作区。', 400)


def applicant_plan_get(payload, context):
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_APPLICANT_PLAN', '仅能读取当前求职者工作区的方案。', 400)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        document = store.runtime_document(c, context['scope_id'], 'applicant_plan')
    return {'kind': 'applicant_plan', **document, 'suggested_preset': deepcopy(applicant_jobs.PRESET) if document['revision'] == 0 else None,
            'platform_automation_enabled': False}


def applicant_plan_save(payload, context):
    applicant_only(context)
    if (set(payload) != {'content', 'expected_revision', 'confirmed'} or payload.get('confirmed') is not True
            or type(payload.get('expected_revision')) is not int or payload['expected_revision'] < 0):
        raise KernelError('INVALID_APPLICANT_PLAN', '请核对并确认求职方案及当前版本。', 400)
    content = applicant_jobs.validate_plan(payload['content'])
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        document = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        if document['revision'] != payload['expected_revision']:
            raise KernelError('STALE_APPLICANT_PLAN', '求职方案已更新，请重新读取后保存。')
        revision = document['revision'] + 1
        c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,content=excluded.content",
                  (context['scope_id'], 'applicant_plan', revision, json.dumps(content, ensure_ascii=False)))
        return record_commit(c, context, 'runtime.applicant.plan.save', {'kind': 'applicant_plan', 'revision': revision, 'content': content})


def applicant_jobs_preview(payload, context):
    applicant_only(context)
    if set(payload) != {'jobs'}:
        raise KernelError('INVALID_JOB_FEED', '请提供待核对的岗位记录。', 400)
    return applicant_preview_records(payload['jobs'], context)


def applicant_preview_records(jobs, context):
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        profile = store.runtime_document(c, context['scope_id'], 'profile')
    if not plan['revision'] or not profile['revision']:
        raise KernelError('APPLICANT_SETUP_REQUIRED', '请先确认保存本人资料和求职方案。')
    if (not isinstance(profile['content'], dict) or set(profile['content']) - PROFILE_FIELDS
            or any(not isinstance(value, str) or len(value) > 2000 for value in profile['content'].values())):
        raise KernelError('INVALID_DOCUMENT_STATE', '本人资料无法读取，请先核对。')
    result = applicant_jobs.preview(plan['content'], profile['content'], jobs)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        if store.runtime_document(c, context['scope_id'], 'applicant_plan')['revision'] != plan['revision']:
            raise KernelError('STALE_APPLICANT_PLAN', '求职方案已变化，未采用旧方案结果。')
        if store.runtime_document(c, context['scope_id'], 'profile')['revision'] != profile['revision']:
            raise KernelError('STALE_FACTS', '本人资料已变化，未采用旧资料结果。')
    return {**result, 'plan_revision': plan['revision'], 'profile_revision': profile['revision']}


def applicant_jobs_read(payload, context):
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_REQUEST', '岗位读取不接受地址或执行脚本。', 400)
    observed = applicant_browser.read_current_jobs()
    if observed['status'] != 'observed':
        return {**observed, 'matching_ready': False, 'automatic_execution': False}
    applicant_jobs.validate_jobs(observed['jobs'])
    try:
        result = applicant_preview_records(observed['jobs'], context)
    except KernelError as error:
        if error.code != 'APPLICANT_SETUP_REQUIRED':
            raise
        return {'status': 'observed', 'observed_count': len(observed['jobs']), 'matching_ready': False,
                'setup_required': True, 'account_verified': False, 'sending_enabled': False, 'automatic_execution': False}
    return {**result, 'status': 'observed', 'matching_ready': True, 'source_type': 'observed_browser_dom',
            'account_verified': False, 'sending_enabled': False}


def applicant_observation(c, context, task_id, tool):
    if not isinstance(task_id, str) or not re.fullmatch(r'[0-9a-f]{32}', task_id):
        raise KernelError('INVALID_SALARY_REVIEW', '请提供有效的真实读取任务标识。', 400)
    row = c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?', (task_id, context['scope_id'])).fetchone()
    if (row is None or row['tool'] != tool or row['status'] != 'completed' or row['context_token'] != context['context_token']):
        raise KernelError('OBSERVATION_REQUIRED', '必须使用当前工作区和身份下已完成的真实读取。')
    if not 0 <= time.time() - row['updated_at'] <= 300:
        raise KernelError('STALE_OBSERVATION', '真实读取已过期，请重新读取后核对。')
    try:
        result = json.loads(row['result'])
        if not isinstance(result, dict) or result.get('status') != 'observed':
            raise ValueError('invalid observation')
        return result
    except (TypeError, ValueError):
        raise KernelError('INVALID_OBSERVATION', '读取记录无法完整核对。') from None


def salary_review_sources(c, context, payload):
    jobs = applicant_observation(c, context, payload['job_observation_task_id'], 'runtime.applicant.jobs.read')
    ocr = applicant_observation(c, context, payload['salary_observation_task_id'], 'runtime.applicant.salary.read')
    try:
        if (jobs['source_type'] != 'observed_browser_dom' or jobs['matching_ready'] is not True
                or any(type(jobs.get(k)) is not int or jobs[k] < 1 for k in ('profile_revision', 'plan_revision'))
                or ocr['source_type'] != 'rendered_salary_ocr_unconfirmed' or ocr['matching_updated'] is not False
                or ocr['sending_enabled'] is not False or not isinstance(ocr['salaries'], list) or len(ocr['salaries']) > 8):
            raise ValueError('invalid sources')
        records = [applicant_jobs.record(item) for item in jobs['candidates']]
        applicant_jobs.validate_jobs(records)
        selected = [job for job in records if job['job_id'] == payload['job_id']]
        salaries = [item for item in ocr['salaries'] if item['job_id'] == payload['job_id']]
        if len(selected) != 1 or len(salaries) != 1:
            raise ValueError('missing or ambiguous job')
        job, evidence = selected[0], salaries[0]
        monthly = applicant_jobs.monthly_salary(evidence['recognized_text'])
        if (evidence['raw_text'] != job['salary'] or evidence['status'] != 'needs_confirmation'
                or evidence['confirmed'] is not False or monthly is None or list(monthly) != evidence['monthly_salary']
                or type(evidence['confidence']) not in (int, float) or not math.isfinite(evidence['confidence'])
                or not 0.98 <= evidence['confidence'] <= 1
                or not isinstance(evidence['image_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', evidence['image_sha256'])):
            raise ValueError('salary evidence mismatch')
    except (TypeError, KeyError, ValueError, KernelError, AttributeError):
        raise KernelError('SALARY_EVIDENCE_MISMATCH', '薪资证据与岗位或识别结果无法对应，未确认金额。') from None
    profile = store.runtime_document(c, context['scope_id'], 'profile')
    plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
    if (profile['revision'] != payload['expected_profile_revision'] or profile['revision'] != jobs.get('profile_revision')
            or plan['revision'] != payload['expected_plan_revision'] or plan['revision'] != jobs.get('plan_revision')):
        raise KernelError('STALE_SALARY_FACTS', '本人资料或求职方案已变化，请重新读取后核对。')
    return job, evidence, profile, plan


def applicant_salary_confirm(payload, context):
    applicant_only(context)
    required = {'job_observation_task_id', 'salary_observation_task_id', 'job_id',
                'expected_profile_revision', 'expected_plan_revision', 'expected_salary_text', 'confirmed'}
    if (set(payload) != required or payload.get('confirmed') is not True
            or not isinstance(payload.get('job_id'), str) or not re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', payload['job_id'])
            or not isinstance(payload.get('expected_salary_text'), str) or not 1 <= len(payload['expected_salary_text']) <= 80
            or any(type(payload.get(k)) is not int or payload[k] < 1 for k in ('expected_profile_revision', 'expected_plan_revision'))):
        raise KernelError('INVALID_SALARY_REVIEW', '请核对岗位、识别金额与资料版本，并明确确认。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        job, evidence, profile, plan = salary_review_sources(c, context, payload)
        if evidence['recognized_text'] != payload['expected_salary_text']:
            raise KernelError('SALARY_TEXT_CHANGED', '确认金额与本次识别结果不同，请重新核对。')
        binding = {'scope_id': context['scope_id'], 'job_observation_task_id': payload['job_observation_task_id'],
                   'salary_observation_task_id': payload['salary_observation_task_id'], 'job': job,
                   'salary_text': evidence['recognized_text'], 'monthly_salary': evidence['monthly_salary'],
                   'image_sha256': evidence['image_sha256'], 'profile_revision': profile['revision'], 'plan_revision': plan['revision']}
        identity = applicant_salary_review.identity(binding)
        kind = 'applicant_salary:' + identity
        doc = store.runtime_document(c, context['scope_id'], kind)
        if doc['revision']:
            result = applicant_salary_review.checked(doc, context['scope_id'])
            if result['confirmation_id'] != identity or any(result.get(key) != value for key, value in binding.items()):
                raise KernelError('INVALID_SALARY_CONFIRMATION', '已有确认记录与本次核对依据不同，未替换记录。')
        else:
            result = {**binding, 'confirmation_id': identity, 'confirmed': True, 'sending_enabled': False}
            c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)', (context['scope_id'], kind, 1, json.dumps(result, ensure_ascii=False)))
        return record_commit(c, context, 'runtime.applicant.salary.confirm', result)


def confirmed_salary_matching(c, context, payload):
    required = {'job_observation_task_id', 'confirmation_ids', 'expected_profile_revision', 'expected_plan_revision'}
    if (set(payload) != required or not isinstance(payload.get('confirmation_ids'), list) or not 1 <= len(payload['confirmation_ids']) <= 8
            or any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{64}', x) for x in payload['confirmation_ids'])
            or len(set(payload['confirmation_ids'])) != len(payload['confirmation_ids'])
            or any(type(payload.get(k)) is not int or payload[k] < 1 for k in ('expected_profile_revision', 'expected_plan_revision'))):
        raise KernelError('INVALID_SALARY_REVIEW', '请提供已确认的薪资记录与当前资料版本。', 400)
    observed = applicant_observation(c, context, payload['job_observation_task_id'], 'runtime.applicant.jobs.read')
    verified, profile, plan = {}, None, None
    for identity in payload['confirmation_ids']:
        record = applicant_salary_review.checked(store.runtime_document(c, context['scope_id'], 'applicant_salary:' + identity), context['scope_id'])
        if record['confirmation_id'] != identity or record['job_observation_task_id'] != payload['job_observation_task_id']:
            raise KernelError('SALARY_EVIDENCE_MISMATCH', '薪资确认不属于本次岗位读取。')
        fields = {**payload, 'salary_observation_task_id': record['salary_observation_task_id'], 'job_id': record['job']['job_id']}
        job, evidence, profile, plan = salary_review_sources(c, context, fields)
        if (job != record['job'] or evidence['recognized_text'] != record['salary_text'] or evidence['image_sha256'] != record['image_sha256']
                or record['profile_revision'] != profile['revision'] or record['plan_revision'] != plan['revision'] or job['job_id'] in verified):
            raise KernelError('SALARY_EVIDENCE_MISMATCH', '薪资确认的依据已经变化或重复，未用于匹配。')
        verified[job['job_id']] = record
    records = [applicant_jobs.record(item) for item in observed['candidates']]
    for job in records:
        if job['job_id'] in verified:
            job['salary'] = verified[job['job_id']]['salary_text']
    result = applicant_jobs.preview(plan['content'], profile['content'], records)
    for candidate in result['candidates']:
        record = verified.get(candidate['job_id'])
        candidate['salary_confirmation_id'] = record['confirmation_id'] if record else None
        candidate['salary_source'] = 'explicitly_confirmed_ocr' if record else 'original_dom'
    return {**result, 'source_type': 'observed_browser_dom_with_confirmed_salary', 'status': 'observed', 'matching_ready': True,
            'job_observation_task_id': payload['job_observation_task_id'], 'confirmation_ids': payload['confirmation_ids'],
            'profile_revision': profile['revision'], 'plan_revision': plan['revision'], 'sending_enabled': False, 'account_verified': False}


def applicant_confirmed_preview(payload, context):
    applicant_only(context)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        return confirmed_salary_matching(c, context, payload)


def greeting_intent_document(document, scope_id):
    if not document['revision']:
        return None
    result = document['content']
    try:
        binding = {key: result[key] for key in ('job', 'text', 'profile_revision', 'plan_revision')}
        fingerprint = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        valid = (document['revision'] == 1 and set(result) == set(binding) | {
            'intent_id', 'scope_id', 'observation_task_id', 'fingerprint', 'state',
            'sent', 'sending_enabled', 'account_verified', 'quota_consumed'}
            and result['scope_id'] == scope_id and result['state'] == 'awaiting_channel_verification'
            and result['fingerprint'] == fingerprint
            and all(result[k] is False for k in ('sent', 'sending_enabled', 'account_verified', 'quota_consumed')))
    except (TypeError, KeyError, ValueError):
        valid = False
    if not valid:
        raise KernelError('INVALID_GREETING_INTENT_STATE', '已有招呼记录无法核对，未覆盖或重新准备。')
    return result


def applicant_greeting_prepare(payload, context):
    """Freeze an eligible observed greeting; this never dispatches a platform action."""
    applicant_only(context)
    required = {'observation_task_id', 'job_id', 'expected_profile_revision', 'expected_plan_revision'}
    if (set(payload) != required
            or not isinstance(payload.get('observation_task_id'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', payload['observation_task_id'])
            or not isinstance(payload.get('job_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', payload['job_id'])
            or any(type(payload.get(k)) is not int or payload[k] < 1
                   for k in ('expected_profile_revision', 'expected_plan_revision'))):
        raise KernelError('INVALID_GREETING_INTENT', '请提供刚读取的岗位、任务标识和资料版本。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        source = c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',
                           (payload['observation_task_id'], context['scope_id'])).fetchone()
        if (source is None or source['tool'] not in {'runtime.applicant.jobs.read', 'runtime.applicant.jobs.confirmed.preview'} or source['status'] != 'completed'
                or source['context_token'] != context['context_token']):
            raise KernelError('OBSERVED_JOBS_REQUIRED', '必须使用当前工作区已完成的真实岗位读取记录。')
        if not 0 <= time.time() - source['updated_at'] <= 300:
            raise KernelError('STALE_JOB_OBSERVATION', '岗位读取已过期，请重新读取后准备招呼。')
        try:
            if source['tool'] == 'runtime.applicant.jobs.confirmed.preview':
                observed = confirmed_salary_matching(c, context, json.loads(source['payload']))
                expected_type = 'observed_browser_dom_with_confirmed_salary'
            else:
                observed = json.loads(source['result'])
                expected_type = 'observed_browser_dom'
            if (observed['source_type'] != expected_type or observed['status'] != 'observed'
                    or observed['matching_ready'] is not True):
                raise ValueError('invalid observation')
            jobs = observed['candidates']
            if (type(observed['profile_revision']) is not int or type(observed['plan_revision']) is not int):
                raise ValueError('invalid revisions')
            applicant_jobs.validate_jobs([applicant_jobs.record(job) for job in jobs])
            selected = [job for job in jobs if job['job_id'] == payload['job_id']]
        except (TypeError, ValueError, KeyError):
            raise KernelError('INVALID_JOB_OBSERVATION', '岗位读取记录无法完整核对。') from None
        profile = store.runtime_document(c, context['scope_id'], 'profile')
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        if (profile['revision'] != payload['expected_profile_revision'] or profile['revision'] != observed['profile_revision']
                or plan['revision'] != payload['expected_plan_revision'] or plan['revision'] != observed['plan_revision']):
            raise KernelError('STALE_GREETING_FACTS', '本人资料或方案已变化，请重新读取岗位。')
        if len(selected) != 1:
            raise KernelError('OBSERVED_JOB_NOT_FOUND', '该岗位不在本次读取记录中。', 404)
        job = applicant_jobs.record(selected[0])
        candidate = applicant_jobs.preview(plan['content'], profile['content'], [job])['candidates'][0]
        if candidate['decision'] != 'eligible' or not candidate['greeting']:
            raise KernelError('JOB_NOT_ELIGIBLE', '岗位仍需核对或不符合条件，未准备招呼。')
        binding = {'job': job, 'text': candidate['greeting'], 'profile_revision': profile['revision'], 'plan_revision': plan['revision']}
        fingerprint = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        kind = 'applicant_greeting:' + job['job_id']
        existing = store.runtime_document(c, context['scope_id'], kind)
        if existing['revision']:
            result = greeting_intent_document(existing, context['scope_id'])
            if result.get('fingerprint') != fingerprint:
                raise KernelError('GREETING_INTENT_CONFLICT', '此岗位已有不同版本的招呼记录，请人工核对，不能覆盖或重复准备。')
        else:
            result = {**binding, 'intent_id': uuid.uuid4().hex, 'scope_id': context['scope_id'],
                      'observation_task_id': source['task_id'], 'fingerprint': fingerprint,
                      'state': 'awaiting_channel_verification', 'sent': False, 'sending_enabled': False,
                      'account_verified': False, 'quota_consumed': False}
            c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)',
                      (context['scope_id'], kind, 1, json.dumps(result, ensure_ascii=False)))
        return record_commit(c, context, 'runtime.applicant.greeting.prepare', result)


def applicant_greeting_get(payload, context):
    applicant_only(context)
    if (set(payload) != {'job_id'} or not isinstance(payload.get('job_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', payload['job_id'])):
        raise KernelError('INVALID_GREETING_INTENT', '请提供有效岗位标识。', 400)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        doc = store.runtime_document(c, context['scope_id'], 'applicant_greeting:' + payload['job_id'])
    return {'found': bool(doc['revision']), 'intent': greeting_intent_document(doc, context['scope_id']),
            'sending_enabled': False}


def applicant_binding_get(payload, context):
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_BINDING', '只可读取当前求职者工作区的会话确认。', 400)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        doc = store.runtime_document(c, context['scope_id'], 'applicant_browser_binding')
        content = applicant_binding.checked(doc, context['scope_id']) if doc['revision'] else None
    return {'revision': doc['revision'], 'content': content, 'live_session_checked': False, 'account_verified': False, 'sending_enabled': False}


def applicant_binding_confirm(payload, context):
    applicant_only(context)
    optional = {'job_id', 'greeting_template_id'}
    if (set(payload) - optional != {'identity_observation_task_id', 'expected_display_name', 'expected_browser_session_fingerprint', 'expected_revision', 'confirmed'}
            or any(not isinstance(payload.get(key), str) for key in optional & set(payload))
            or ('job_id' in payload and not re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', payload['job_id']))
            or ('greeting_template_id' in payload and not re.fullmatch(r'[A-Za-z0-9_~:\-]{1,64}', payload['greeting_template_id']))
            or payload.get('confirmed') is not True or type(payload.get('expected_revision')) is not int or payload['expected_revision'] < 0
            or not isinstance(payload.get('expected_display_name'), str) or not 1 <= len(payload['expected_display_name']) <= 40
            or not isinstance(payload.get('expected_browser_session_fingerprint'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', payload['expected_browser_session_fingerprint'])):
        raise KernelError('INVALID_BROWSER_BINDING', '请核对显示姓名和专用浏览器会话，并明确确认。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        if not isinstance(payload.get('identity_observation_task_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', payload['identity_observation_task_id']):
            raise KernelError('INVALID_BROWSER_BINDING', '请提供本次账号读取标识。', 400)
        row = c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?', (payload['identity_observation_task_id'], context['scope_id'])).fetchone()
        if (row is None or row['tool'] != 'runtime.applicant.identity.read' or row['status'] != 'completed'
                or row['context_token'] != context['context_token'] or not 0 <= time.time() - row['updated_at'] <= 120):
            raise KernelError('FRESH_IDENTITY_REQUIRED', '请先重新读取当前账号显示信息，再确认绑定。')
        try:
            observed = json.loads(row['result'])
            valid = (observed['status'] == 'display_name_observed' and observed['role_evidence'] == 'applicant_page'
                     and observed['display_name'] == payload['expected_display_name']
                     and observed['browser_session_fingerprint'] == payload['expected_browser_session_fingerprint']
                     and observed['account_verified'] is False and observed['sending_enabled'] is False)
        except (TypeError, ValueError, KeyError):
            valid = False
        if not valid:
            raise KernelError('BROWSER_BINDING_MISMATCH', '确认信息与当前读取的账号或浏览器会话不一致。')
        doc = store.runtime_document(c, context['scope_id'], 'applicant_browser_binding')
        if doc['revision'] != payload['expected_revision']:
            raise KernelError('STALE_BROWSER_BINDING', '浏览器确认已更新，请重新读取。')
        others = c.execute("SELECT scope_id,revision,content FROM runtime_documents WHERE kind='applicant_browser_binding' AND scope_id<>? LIMIT 1001", (context['scope_id'],)).fetchall()
        if len(others) > 1000:
            raise KernelError('BROWSER_BINDING_CAPACITY', '工作区数量超出当前会话绑定核对容量。')
        for other in others:
            try:
                saved = applicant_binding.checked({'revision': other['revision'], 'content': json.loads(other['content'])}, other['scope_id'])
            except (TypeError, ValueError):
                raise KernelError('INVALID_BROWSER_BINDING', '已有浏览器确认记录无法完整读取。') from None
            if saved['browser_session_fingerprint'] == observed['browser_session_fingerprint']:
                raise KernelError('BROWSER_SESSION_ALREADY_BOUND', '此专用浏览器会话已关联其他工作区，不能重复绑定。')
        # 可空：会话级绑定没有岗位；模板标识由当前方案模板文本派生，作为版本证据而非平台 ID。
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        template = plan['content'].get('greeting_template') if isinstance(plan['content'], dict) else None
        content = {'scope_id': context['scope_id'], 'display_name': observed['display_name'],
                   'browser_session_fingerprint': observed['browser_session_fingerprint'],
                   'identity_observation_task_id': row['task_id'], 'confirmed': True,
                   'platform_account_id_verified': False, 'sending_enabled': False,
                   'job_id': payload.get('job_id'), 'greeting_template_id': payload.get('greeting_template_id')
                   or ('tpl-' + hashlib.sha256(template.encode('utf-8')).hexdigest()[:24] if isinstance(template, str) and template else None)}
        revision = doc['revision'] + 1
        c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,content=excluded.content',
                  (context['scope_id'], 'applicant_browser_binding', revision, json.dumps(content, ensure_ascii=False)))
        return record_commit(c, context, 'runtime.applicant.browser.binding.confirm', {'kind': 'applicant_browser_binding', 'revision': revision, 'content': content})


def applicant_binding_check(payload, context):
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_BINDING', '会话检查不接受浏览器地址或账号凭据。', 400)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        doc = store.runtime_document(c, context['scope_id'], 'applicant_browser_binding')
        saved = applicant_binding.checked(doc, context['scope_id']) if doc['revision'] else None
    result = {'binding_revision': doc['revision'], 'session_matches_confirmation': False, 'account_verified': False, 'sending_enabled': False}
    if saved is None:
        return {**result, 'status': 'binding_required'}
    observed = applicant_browser.read_current_jobs(binding_check=True)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        if store.runtime_document(c, context['scope_id'], 'applicant_browser_binding') != doc:
            raise KernelError('STALE_BROWSER_BINDING', '检查期间浏览器确认已变化，请重新检查。')
    if observed.get('status') != 'display_name_observed':
        return {**result, 'status': observed.get('status', 'identity_unavailable')}
    matches = observed.get('browser_session_fingerprint') == saved['browser_session_fingerprint'] and observed.get('display_name') == saved['display_name']
    return {**result, 'status': 'session_matches_confirmation' if matches else 'session_changed', 'session_matches_confirmation': matches}


def applicant_conversations_read(payload, context, *, messages=False):
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_REQUEST', '会话读取不接受地址、账号凭据或脚本。', 400)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
        if not doc['revision']:
            return {'status':'binding_required','automation_ready':False,'account_verified':False,'sending_enabled':False,
                    **({'messages':[],'party':None,'messages_truncated':False,'history_complete':False,'recipient_verified':False}
                       if messages else {'rows':[],'list_truncated':False})}
        binding=applicant_binding.checked(doc,context['scope_id'])
    result=applicant_browser.read_chat(binding=binding,**({'messages':True} if messages else {}))
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        if store.runtime_document(c,context['scope_id'],'applicant_browser_binding')!=doc:
            raise KernelError('STALE_BROWSER_BINDING','读取期间会话绑定已变化，请重新读取。')
    return {**result,'binding_revision':doc['revision'],'source_type':'boss_selected_message_dom' if messages else 'boss_visible_inbox_previews',
            'account_verified':False,'sending_enabled':False}


def applicant_conversation_open(payload,context):
    applicant_only(context)
    if (set(payload)!={'inbox_observation_task_id','row_index','confirmed'} or payload.get('confirmed') is not True
            or not isinstance(payload.get('inbox_observation_task_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['inbox_observation_task_id'])
            or type(payload.get('row_index')) is not int or not 0<=payload['row_index']<30):
        raise KernelError('INVALID_CONVERSATION_REFERENCE','请明确本次读取的会话引用；此操作只打开会话，不发送。',400)
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
        binding=applicant_binding.checked(doc,context['scope_id'])
        row=c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',(payload['inbox_observation_task_id'],context['scope_id'])).fetchone()
        if (row is None or row['tool']!='runtime.applicant.conversations.read' or row['status']!='completed'
                or row['context_token']!=context['context_token'] or not 0<=time.time()-row['updated_at']<=30):
            raise KernelError('FRESH_INBOX_REQUIRED','会话引用已过期或不属于当前身份，请重新读取列表。')
        try:
            observed=json.loads(row['result'])
            snapshot={key:observed[key] for key in ('status','display_name','rows','automation_ready','list_truncated')}
            import applicant_inbox
            applicant_inbox.checked(snapshot,binding['display_name'])
            valid=(observed['source_type']=='boss_visible_inbox_previews' and observed['binding_revision']==doc['revision']
                   and snapshot['status']=='observed_previews' and payload['row_index']<len(snapshot['rows']))
        except (TypeError,KeyError,ValueError,KernelError):
            valid=False
        if not valid:
            raise KernelError('INVALID_CONVERSATION_REFERENCE','本次会话引用与账号或绑定无法对应，未打开会话。')
    result=applicant_browser.read_chat(binding=binding,navigation={'snapshot':snapshot,'row_index':payload['row_index']})
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        if store.runtime_document(c,context['scope_id'],'applicant_browser_binding')!=doc:
            raise KernelError('STALE_BROWSER_BINDING','打开期间绑定变化，请重新核对当前会话；不会自动重试。',outcome_unknown=True)
    return result


def observed_reply_source(c,context,payload):
    applicant_only(context)
    if (set(payload)!={'message_observation_task_id','message_ids','expected_profile_revision','expected_binding_revision'}
            or not isinstance(payload.get('message_observation_task_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['message_observation_task_id'])
            or not isinstance(payload.get('message_ids'),list) or not 1<=len(payload['message_ids'])<=10
            or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_~.\-]{1,128}',x) for x in payload['message_ids'])
            or len(set(payload['message_ids']))!=len(payload['message_ids'])
            or any(type(payload.get(k)) is not int or payload[k]<1 for k in ('expected_profile_revision','expected_binding_revision'))):
        raise KernelError('INVALID_OBSERVED_REPLY','请提供本次消息读取、末尾问题标识与当前资料版本。',400)
    check_context(c,context['context_token'])
    source=c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',(payload['message_observation_task_id'],context['scope_id'])).fetchone()
    if (source is None or source['tool']!='runtime.applicant.messages.read' or source['status']!='completed'
            or source['context_token']!=context['context_token'] or not 0<=time.time()-source['updated_at']<=300):
        raise KernelError('FRESH_MESSAGES_REQUIRED','请先重新读取当前会话消息，不能使用预览或过期消息准备回复。')
    doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
    binding=applicant_binding.checked(doc,context['scope_id'])
    profile=store.runtime_document(c,context['scope_id'],'profile')
    if (profile['revision']!=payload['expected_profile_revision'] or doc['revision']!=payload['expected_binding_revision']):
        raise KernelError('STALE_REPLY_FACTS','本人资料或浏览器绑定已变化，请重新核对后读取消息。')
    try:
        observed=json.loads(source['result'])
        if (observed['source_type']!='boss_selected_message_dom' or observed['binding_revision']!=doc['revision']
                or observed['account_verified'] is not False or observed['sending_enabled'] is not False):
            raise ValueError('invalid observation')
        snapshot={key:observed[key] for key in ('status','party','messages','messages_truncated','history_complete','recipient_verified','automation_ready')}
    except (TypeError,ValueError,KeyError):
        raise KernelError('INVALID_MESSAGE_OBSERVATION','消息读取依据无法完整核对。') from None
    messages,text,boundary=applicant_reply.block(snapshot)
    if [message['message_id'] for message in messages]!=payload['message_ids']:
        raise KernelError('REPLY_BLOCK_MISMATCH','请选择末尾连续的全部对方纯文本消息，不能遗漏、重排或回复已被本人消息覆盖的问题。')
    if (not isinstance(profile['content'],dict) or set(profile['content'])-PROFILE_FIELDS
            or any(not isinstance(value,str) or len(value)>2000 for value in profile['content'].values())):
        raise KernelError('INVALID_DOCUMENT_STATE','本人资料无法完整读取，请先核对。')
    return {'scope_id':context['scope_id'],'context_token':context['context_token'],'party':snapshot['party'],'messages':messages,
            'profile_revision':profile['revision'],'binding_revision':doc['revision'],
            'browser_session_fingerprint':binding['browser_session_fingerprint'],'block_boundary_observed':boundary,
            'draft_backend':'rules'},text


def applicant_reply_get(payload,context):
    applicant_only(context)
    if set(payload)!={'intent_id'} or not isinstance(payload.get('intent_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['intent_id']):
        raise KernelError('INVALID_REPLY_INTENT','请提供有效的回复意图标识。',400)
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        intent,task=applicant_reply.read(c,context['scope_id'],payload['intent_id'])
        profile=store.runtime_document(c,context['scope_id'],'profile')
        binding=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
        if binding['revision']:
            applicant_binding.checked(binding,context['scope_id'])
        source=c.execute('SELECT updated_at FROM runtime_tasks WHERE task_id=? AND scope_id=?',(intent['source_observation_task_id'],context['scope_id'])).fetchone()
        return {'intent':intent,'task':public_task(task),'profile_stale':profile['revision']!=intent['profile_revision'],
                'binding_stale':binding['revision']!=intent['binding_revision'] or binding['content'].get('browser_session_fingerprint')!=intent['browser_session_fingerprint'],
                'context_stale':intent['context_token']!=context['context_token'],
                'original_observation_fresh':bool(source is not None and 0<=time.time()-source['updated_at']<=300),
                'sending_enabled':False}


def window_key(payload):
    """Optional conversation key; absent means the legacy global window (older clients unchanged)."""
    if 'conversation_key' not in payload:
        return applicant_reply_window.DEFAULT_CONVERSATION_KEY
    try:
        return applicant_reply_windows.checked_key(payload['conversation_key'])
    except ValueError:
        raise KernelError('INVALID_CONVERSATION_KEY','会话键必须是 1..128 个不含空白的可见字符；不要用显示名，同名 HR 会串。',400) from None


def reply_window_observation(c,context,payload):
    required={'message_observation_task_id','expected_revision','expected_profile_revision','expected_binding_revision'}
    if (set(payload)-{'conversation_key'}!=required or not isinstance(payload.get('message_observation_task_id'),str)
            or not re.fullmatch(r'[0-9a-f]{32}',payload['message_observation_task_id'])
            or type(payload.get('expected_revision')) is not int or payload['expected_revision']<0
            or any(type(payload.get(k)) is not int or payload[k]<1 for k in ('expected_profile_revision','expected_binding_revision'))):
        raise KernelError('INVALID_REPLY_WINDOW','请提供刚读取的消息和当前观察、资料、会话版本。',400)
    source=c.execute('SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?',(payload['message_observation_task_id'],context['scope_id'])).fetchone()
    if (source is None or source['tool']!='runtime.applicant.messages.read' or source['status']!='completed'
            or source['context_token']!=context['context_token'] or not 0<=time.time()-source['updated_at']<=10):
        raise KernelError('FRESH_WINDOW_MESSAGES_REQUIRED','请先重新读取当前会话，观察边界只接受十秒内的真实读取。')
    profile=store.runtime_document(c,context['scope_id'],'profile')
    binding_doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
    binding=applicant_binding.checked(binding_doc,context['scope_id'])
    if profile['revision']!=payload['expected_profile_revision'] or binding_doc['revision']!=payload['expected_binding_revision']:
        raise KernelError('STALE_REPLY_FACTS','本人资料或会话关联已变化，请重新核对。')
    try:
        observed=json.loads(source['result'])
        if observed['source_type']!='boss_selected_message_dom' or observed['binding_revision']!=binding_doc['revision']:
            raise ValueError('invalid source')
        selected=applicant_reply_window.snapshot(observed)
    except (TypeError,ValueError,KeyError):
        raise KernelError('INVALID_MESSAGE_OBSERVATION','观察边界的消息来源无法完整核对。') from None
    return source,selected,profile,binding_doc,binding


def applicant_reply_window_update(payload,context,*,advance=False):
    applicant_only(context)
    tool='runtime.applicant.reply.window.observe' if advance else 'runtime.applicant.reply.window.start'
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        source,selected,profile,binding_doc,binding=reply_window_observation(c,context,payload)
        key=window_key(payload)
        document,previous=applicant_reply_window.read(c,context['scope_id'],key)
        if document['revision']!=payload['expected_revision']:
            raise KernelError('STALE_REPLY_WINDOW','观察边界已更新，请重新读取。')
        if advance:
            if previous is None:raise KernelError('REPLY_WINDOW_REQUIRED','请先明确建立消息观察起点。')
            if (previous['context_token']!=context['context_token'] or previous['profile_revision']!=profile['revision']
                    or previous['binding_revision']!=binding_doc['revision'] or previous['browser_session_fingerprint']!=binding['browser_session_fingerprint']):
                raise KernelError('STALE_REPLY_WINDOW','身份、资料或浏览器会话已变化，请重新建立观察起点。')
            if not 0<=time.time()-previous['last_observed_at']<=applicant_reply_window.GAP_SECONDS or not 0<=source['updated_at']-previous['last_observed_at']<=applicant_reply_window.GAP_SECONDS:
                raise KernelError('WINDOW_GAP_EXCEEDED','消息读取间隔超过两分钟，不能把离线期间的消息当作刚收到。请重新建立观察起点。')
            seen=applicant_reply_window.advance(previous,selected,source['updated_at'])
            content={**previous,'seen':seen,'last_observed_at':source['updated_at'],
                     'last_observation_task_id':source['task_id'],'tail_message_id':selected['messages'][-1]['message_id']}
        else:
            seen={m['message_id']:{'digest':applicant_reply_window.message_digest(m),'first_seen_at':source['updated_at'],'new_after_start':False}
                  for m in selected['messages'] if applicant_reply_window.usable_id(m['message_id'])}
            content={'scope_id':context['scope_id'],'context_token':context['context_token'],'profile_revision':profile['revision'],
                     'binding_revision':binding_doc['revision'],'browser_session_fingerprint':binding['browser_session_fingerprint'],'party':selected['party'],
                     'started_at':source['updated_at'],'last_observed_at':source['updated_at'],
                     'start_observation_task_id':source['task_id'],'last_observation_task_id':source['task_id'],
                     'tail_message_id':selected['messages'][-1]['message_id'],'seen':seen,'sending_enabled':False,'automatic_polling':False}
        try:
            container=(applicant_reply_windows.advance(document['container'],key,content,now=source['updated_at'])
                       if advance else applicant_reply_windows.rebuild(document['container'],key,content,now=source['updated_at']))
        except applicant_reply_windows.ExpiredError:
            raise KernelError('WINDOW_GAP_EXCEEDED','消息观察窗口已过期，不能接着推进；请重新建立观察起点。') from None
        except applicant_reply_windows.CapacityError:
            raise KernelError('WINDOW_KEY_CAPACITY_REACHED','同时观察的会话已达上限，请先结束或等待旧会话过期；未过期的会话不会被丢弃。') from None
        except applicant_reply_windows.WindowStoreError:
            raise KernelError('STALE_REPLY_WINDOW','观察起点已变化或记录损坏，请重新读取后重建观察起点。') from None
        revision=applicant_reply_windows.get(container,key)['revision']
        applicant_reply_window.checked({'revision':revision,'content':content},context['scope_id'])
        c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,content=excluded.content',
                  (context['scope_id'],applicant_reply_window.KIND,document['document_revision']+1,json.dumps(container,ensure_ascii=False)))
        return record_commit(c,context,tool,{'kind':applicant_reply_window.KIND,'revision':revision,'content':content})


def applicant_reply_window_get(payload,context):
    applicant_only(context)
    if set(payload)-{'conversation_key'}:raise KernelError('INVALID_REPLY_WINDOW','只能读取当前工作区的消息观察边界。',400)
    key=window_key(payload)
    with store.runtime_db() as c:
        check_context(c,context['context_token']);document,content=applicant_reply_window.read(c,context['scope_id'],key)
        now=time.time()
        windows=[applicant_reply_windows.public(item,now) for item in applicant_reply_windows.items(document['container'])]
    return {'revision':document['revision'],'content':content,'conversation_key':key,'document_revision':document['document_revision'],
            'windows':windows,'observation_gap_ok':bool(content and 0<=now-content['last_observed_at']<=120),
            'automatic_polling':False,'sending_enabled':False}


def applicant_reply_preflight(payload,context):
    applicant_only(context)
    if (set(payload)!={'intent_id','expected_manual_revision'} or not isinstance(payload.get('intent_id'),str)
            or not re.fullmatch(r'[0-9a-f]{32}',payload['intent_id']) or type(payload.get('expected_manual_revision')) is not int or payload['expected_manual_revision']<0):
        raise KernelError('INVALID_REPLY_PREFLIGHT','请提供回复意图和当前草稿修订版本。',400)
    with store.runtime_db() as c:
        c.execute('BEGIN');check_context(c,context['context_token'])
        intent,task=applicant_reply.read(c,context['scope_id'],payload['intent_id']);task=dict(task)
        check_manual_draft(c,task)
        if task['manual_revision']!=payload['expected_manual_revision']:raise KernelError('STALE_MANUAL_DRAFT','草稿修订已变化，请重新核对。')
        profile=store.runtime_document(c,context['scope_id'],'profile')
        binding_doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
        binding=applicant_binding.checked(binding_doc,context['scope_id'])
        window_doc,window=applicant_reply_window.read(c,context['scope_id'])
    blockers=[]
    # 方案第 177 行「每步检查」：暂停后不能再往下走，也不该把可发项摆到用户面前
    import sandbox as _sandbox_state
    if 'reply' in _sandbox_state.paused_actions_now():
        blockers.append('RUN_PAUSED')
    if intent['context_token']!=context['context_token']:blockers.append('STALE_REPLY_CONTEXT')
    if intent['profile_revision']!=profile['revision']:blockers.append('STALE_REPLY_PROFILE')
    if intent['binding_revision']!=binding_doc['revision'] or intent['browser_session_fingerprint']!=binding['browser_session_fingerprint']:blockers.append('STALE_REPLY_BINDING')
    projection=public_task(task);result=projection.get('result') or {}
    draft_text=task['manual_text'] if task['manual_revision'] else result.get('text','')
    draft_ready=(task['status']=='completed' and isinstance(draft_text,str) and bool(draft_text.strip()) and len(draft_text)<=4000
                 and task['review_status']=='pending' and task['superseded_by_task_id'] is None)
    if not draft_ready:blockers.append('DRAFT_NOT_READY')
    live=applicant_browser.read_chat(binding=binding,messages=True,preflight=True)
    recipient_match=False;recency_match=False;live_ids=[]
    try:
        pending,_,_=applicant_reply.block(live)
        live_ids=[m['message_id'] for m in pending]
        recipient_match=(live['party']==intent['party'] and live_ids==[m['message_id'] for m in intent['messages']]
                         and all(applicant_reply_window.message_digest(a)==applicant_reply_window.message_digest(b) for a,b in zip(pending,intent['messages'])))
    except (KeyError,TypeError,KernelError):
        pending=[]
    if not recipient_match:blockers.append('LIVE_MESSAGE_ANCHOR_CHANGED')
    if window is None:blockers.append('REPLY_WINDOW_REQUIRED')
    elif (window['context_token']!=context['context_token'] or window['profile_revision']!=profile['revision']
          or window['binding_revision']!=binding_doc['revision'] or window['browser_session_fingerprint']!=binding['browser_session_fingerprint']
          or window['party']!=intent['party']):blockers.append('STALE_REPLY_WINDOW')
    elif not 0<=time.time()-window['last_observed_at']<=120:blockers.append('WINDOW_GAP_EXCEEDED')
    else:
        recency_match=bool(pending) and all(
            window['seen'].get(m['message_id'],{}).get('new_after_start') is True
            and window['seen'][m['message_id']]['digest']==applicant_reply_window.message_digest(m)
            and 0<=time.time()-window['seen'][m['message_id']]['first_seen_at']<=120 for m in pending)
        if not recency_match:blockers.append('MESSAGE_NOT_NEW_AFTER_WINDOW')
    with store.runtime_db() as c:
        c.execute('BEGIN');check_context(c,context['context_token'])
        _,again=applicant_reply.read(c,context['scope_id'],payload['intent_id'])
        if (any(again[k]!=task[k] for k in ('status','result','manual_revision','manual_text','review_status','review_revision','superseded_by_task_id'))
                or store.runtime_document(c,context['scope_id'],'profile')!=profile
                or store.runtime_document(c,context['scope_id'],'applicant_browser_binding')!=binding_doc
                or store.runtime_document(c,context['scope_id'],applicant_reply_window.KIND)['revision']!=window_doc['document_revision']):
            raise KernelError('PREFLIGHT_STATE_CHANGED','检查期间草稿、资料、会话或观察边界变化，请重新检查。')
    now=time.time()
    return {'intent_id':intent['intent_id'],'draft_task_id':task['task_id'],'manual_revision':task['manual_revision'],
            'draft_text_sha256':hashlib.sha256(draft_text.encode()).hexdigest() if isinstance(draft_text,str) and draft_text else None,
            'profile_revision':profile['revision'],'binding_revision':binding_doc['revision'],'window_revision':window_doc['revision'],
            'live_message_ids':live_ids,'draft_ready':draft_ready,'recipient_fence_passed':recipient_match,'recency_fence_passed':recency_match,
            'recency_basis':'first_observed_after_window_anchor','checked_at':now,'expires_at':now+10,
            'blockers':blockers,'preflight_passed':not blockers,'platform_account_id_verified':False,'recipient_verified':False,
            'sending_enabled':False,'quota_consumed':False,'automatic_polling':False}


def _send_draft(task):
    projection=public_task(task);result=projection.get('result') or {}
    text=task['manual_text'] if task['manual_revision'] else result.get('text','')
    ready=(task['status']=='completed' and isinstance(text,str) and bool(text.strip()) and len(text)<=4000
           and task['review_status']=='pending' and task['superseded_by_task_id'] is None)
    return (text if ready else None),ready


def _used_evidence(c,scope_id,attempt_id):
    """A self-report or a readback record means this reservation has already been used."""
    for kind in (applicant_send.claim_kind(attempt_id),applicant_send.readback_kind(attempt_id)):
        if c.execute('SELECT 1 FROM runtime_documents WHERE scope_id=? AND kind=?',(scope_id,kind)).fetchone():
            return True
    return False


def _send_readiness(c,context,attempt,*,ignore_usage=False):
    """One no-IO readiness verdict shared by the queue view, single handoff and batch handoff.

    Returns (facts, blockers). Blockers are (code, message) pairs in priority order; callers
    raise the first one or report it as the queue reason. Nothing here writes to the database.
    """
    attempt_id=attempt['attempt_id'];blockers=[]
    # 暂停自动回复期间，队列不该再摆出「可交接」的姿态（方案第 177 行每步检查）
    import sandbox as _sandbox_state
    if 'reply' in _sandbox_state.paused_actions_now():
        blockers.append(('RUN_PAUSED', '自动回复已暂停，先恢复再继续；暂停不影响核对发送结果。'))
    if attempt['state']!='reserved':
        blockers.append(('INVALID_SEND_ATTEMPT_STATE','只有未使用的预占记录可以交接；已释放或结果未知的记录不再交给人手发送。'))
    if not ignore_usage and _used_evidence(c,context['scope_id'],attempt_id):
        blockers.append(('SEND_ALREADY_USED','这条预占已有自报或回读证据，不能释放，也不会再次成为待发项。'))
    recipient=None
    try:
        recipient=applicant_send.checked_recipient(
            store.runtime_document(c,context['scope_id'],applicant_send.recipient_kind(attempt['intent_id'])),
            context['scope_id'],attempt['intent_id'])
    except KernelError:
        blockers.append(('SEND_RECIPIENT_REQUIRED','发送对象绑定缺失或无法核对，请重新核对发送对象。'))
    intent=task=text=None
    try:
        intent,task=applicant_reply.read(c,context['scope_id'],attempt['intent_id']);task=dict(task)
    except KernelError:
        blockers.append(('DRAFT_NOT_READY','回复意图或草稿依据无法核对，请重新准备。'))
    if task is not None:
        text,ready=_send_draft(task)
        if not ready:
            blockers.append(('DRAFT_NOT_READY','草稿已变化或不可用。'))
        elif hashlib.sha256(text.encode()).hexdigest()!=attempt['message_sha256']:
            blockers.append(('DRAFT_CHANGED_AFTER_RESERVATION','预占之后草稿被修改，不能继续交接。'))
    if recipient is not None and recipient['recipient_fingerprint']!=attempt['recipient_binding_id']:
        blockers.append(('SEND_RECIPIENT_CHANGED','预占记录与发送对象绑定不一致。'))
    if recipient is not None:
        # A04：资料或浏览器会话在绑定之后变化，旧事实不能继续作为待发内容。
        profile=store.runtime_document(c,context['scope_id'],'profile')
        binding_doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
        try:
            binding=applicant_binding.checked(binding_doc,context['scope_id'])
        except KernelError:
            binding=None
        if (binding is None or profile['revision']!=recipient['profile_revision']
                or binding_doc['revision']!=recipient['binding_revision']
                or binding['browser_session_fingerprint']!=recipient['browser_session_fingerprint']):
            blockers.append(('STALE_SEND_FACTS','资料或浏览器会话在绑定之后已变化，请重新核对发送对象与草稿。'))
    _,window=applicant_reply_window.read(c,context['scope_id'])
    expires=None
    if window is None:
        blockers.append(('REPLY_WINDOW_REQUIRED','请先建立当前会话的消息观察起点。'))
    elif window['start_observation_task_id']!=attempt['window_start_task_id']:
        # A05：观察起点被重建后，旧预占既不能续期，也不能再取卡。
        blockers.append(('WINDOW_REBUILT','消息观察起点已重建，旧预占不能再使用，请重新准备。'))
    elif intent is not None:
        expires=applicant_send.text_expiry(window,intent['messages'])
        if time.time()>expires:
            blockers.append(('HANDOFF_EXPIRED','这条消息已超出两分钟时效，请重新读取会话并重新核对发送对象。'))
    return ({'attempt':attempt,'recipient':recipient,'intent':intent,'task':task,'text':text,
             'window':window,'expires':expires},blockers)


def _send_facts(c,context,intent,payload):
    """Re-read persisted facts; never trust a client-supplied party, message body or revision."""
    profile=store.runtime_document(c,context['scope_id'],'profile')
    binding_doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
    binding=applicant_binding.checked(binding_doc,context['scope_id'])
    window_doc,window=applicant_reply_window.read(c,context['scope_id'])
    if profile['revision']!=payload['expected_profile_revision']:
        raise KernelError('STALE_SEND_FACTS','本人资料已变化，请重新核对发送对象。')
    if binding_doc['revision']!=payload['expected_binding_revision']:
        raise KernelError('STALE_SEND_FACTS','浏览器会话绑定已变化，请重新核对发送对象。')
    if (intent['context_token']!=context['context_token'] or intent['profile_revision']!=profile['revision']
            or intent['binding_revision']!=binding_doc['revision']
            or intent['browser_session_fingerprint']!=binding['browser_session_fingerprint']):
        raise KernelError('STALE_SEND_FACTS','回复意图与当前身份、资料或浏览器会话不一致。')
    if window_doc['revision']!=payload['expected_window_revision']:
        raise KernelError('STALE_REPLY_WINDOW','消息观察边界已更新，请重新核对发送对象。')
    if window is None:
        raise KernelError('REPLY_WINDOW_REQUIRED','请先建立当前会话的消息观察起点。')
    if (window['context_token']!=context['context_token'] or window['profile_revision']!=profile['revision']
            or window['binding_revision']!=binding_doc['revision'] or window['browser_session_fingerprint']!=binding['browser_session_fingerprint']
            or window['party']!=intent['party']):
        raise KernelError('STALE_REPLY_WINDOW','观察边界与当前发送对象不一致。')
    applicant_send.window_age(window)
    age=applicant_send.message_age(window,intent['messages'])
    return profile,binding_doc,binding,window_doc,window,age


def send_recipient_confirm(payload,context):
    applicant_only(context)
    required={'intent_id','expected_manual_revision','expected_profile_revision','expected_binding_revision','expected_window_revision','confirmed'}
    if (set(payload)!=required or payload.get('confirmed') is not True
            or not isinstance(payload.get('intent_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['intent_id'])
            or type(payload.get('expected_manual_revision')) is not int or payload['expected_manual_revision']<0
            or any(type(payload.get(k)) is not int or payload[k]<1 for k in ('expected_profile_revision','expected_binding_revision','expected_window_revision'))):
        raise KernelError('INVALID_SEND_RECIPIENT','请明确核对发送对象、当前草稿和消息观察边界。',400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        intent,task=applicant_reply.read(c,context['scope_id'],payload['intent_id']);task=dict(task)
        check_manual_draft(c,task)
        if task['manual_revision']!=payload['expected_manual_revision']:
            raise KernelError('STALE_MANUAL_DRAFT','草稿修订已变化，请重新核对。')
        text,ready=_send_draft(task)
        if not ready:
            raise KernelError('DRAFT_NOT_READY','草稿尚未完成或已被替换，不能绑定发送对象。')
        profile,binding_doc,binding,_,_,age=_send_facts(c,context,intent,payload)
        content={'scope_id':context['scope_id'],'intent_id':intent['intent_id'],'draft_task_id':task['task_id'],
                 'context_token':context['context_token'],'party':intent['party'],
                 'anchor_message_id':intent['messages'][-1]['message_id'],
                 'message_ids':[message['message_id'] for message in intent['messages']],
                 'profile_revision':profile['revision'],'binding_revision':binding_doc['revision'],
                 'browser_session_fingerprint':binding['browser_session_fingerprint'],
                 'recipient_fingerprint':'','created_at':time.time(),
                 'platform_account_id_verified':False,'sending_enabled':False}
        content['recipient_fingerprint']=applicant_send.fingerprint(content)
        kind=applicant_send.recipient_kind(intent['intent_id'])
        document=store.runtime_document(c,context['scope_id'],kind)
        if document['revision']:
            existing=applicant_send.checked_recipient(document,context['scope_id'],intent['intent_id'])
            if not applicant_send.same_recipient(existing,content):
                raise KernelError('SEND_RECIPIENT_CHANGED','发送对象、消息依据、资料或会话已经变化，请重新核对；不会覆盖原绑定。')
            return record_commit(c,context,'runtime.applicant.send.recipient.confirm',
                                 {'recipient':existing,'revision':1,'created':False,'message_age_seconds':age,'sending_enabled':False})
        c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)',(context['scope_id'],kind,1,json.dumps(content,ensure_ascii=False)))
        return record_commit(c,context,'runtime.applicant.send.recipient.confirm',
                             {'recipient':content,'revision':1,'created':True,'message_age_seconds':age,'sending_enabled':False})


def send_recipient_get(payload,context):
    applicant_only(context)
    if set(payload)!={'intent_id'} or not isinstance(payload.get('intent_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['intent_id']):
        raise KernelError('INVALID_SEND_RECIPIENT','请提供有效的回复意图标识。',400)
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        document=store.runtime_document(c,context['scope_id'],applicant_send.recipient_kind(payload['intent_id']))
        if not document['revision']:
            return {'recipient':None,'revision':0,'profile_stale':True,'binding_stale':True,'sending_enabled':False}
        recipient=applicant_send.checked_recipient(document,context['scope_id'],payload['intent_id'])
        profile=store.runtime_document(c,context['scope_id'],'profile')
        binding=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
    return {'recipient':recipient,'revision':document['revision'],
            'profile_stale':profile['revision']!=recipient['profile_revision'],
            'binding_stale':binding['revision']!=recipient['binding_revision'],
            'sending_enabled':False}


def send_reserve(payload,context):
    applicant_only(context)
    required={'intent_id','recipient_binding_id','expected_manual_revision','expected_profile_revision','expected_binding_revision','expected_window_revision','confirmed'}
    if (set(payload)!=required or payload.get('confirmed') is not True
            or not isinstance(payload.get('intent_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['intent_id'])
            or not isinstance(payload.get('recipient_binding_id'),str) or not re.fullmatch(r'[0-9a-f]{64}',payload['recipient_binding_id'])
            or type(payload.get('expected_manual_revision')) is not int or payload['expected_manual_revision']<0
            or any(type(payload.get(k)) is not int or payload[k]<1 for k in ('expected_profile_revision','expected_binding_revision','expected_window_revision'))):
        raise KernelError('INVALID_SEND_RESERVATION','请明确核对发送对象、草稿和当前观察边界。',400)
    # 暂停自动回复期间不该吃掉当日额度：在开写事务之前就拒
    import sandbox as _sandbox
    if 'reply' in _sandbox.paused_actions_now():
        raise KernelError('RUN_PAUSED', run_state.CODES['RUN_PAUSED'], 409)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        intent,task=applicant_reply.read(c,context['scope_id'],payload['intent_id']);task=dict(task)
        check_manual_draft(c,task)
        if task['manual_revision']!=payload['expected_manual_revision']:
            raise KernelError('STALE_MANUAL_DRAFT','草稿修订已变化，请重新核对。')
        text,ready=_send_draft(task)
        if not ready:
            raise KernelError('DRAFT_NOT_READY','草稿尚未完成或已被替换，不能预占发送额度。')
        document=store.runtime_document(c,context['scope_id'],applicant_send.recipient_kind(intent['intent_id']))
        if not document['revision']:
            raise KernelError('SEND_RECIPIENT_REQUIRED','请先核对并绑定发送对象。')
        recipient=applicant_send.checked_recipient(document,context['scope_id'],intent['intent_id'])
        if (recipient['recipient_fingerprint']!=payload['recipient_binding_id'] or recipient['draft_task_id']!=task['task_id']
                or recipient['context_token']!=context['context_token']):
            raise KernelError('SEND_RECIPIENT_CHANGED','发送对象绑定与当前草稿或工作区不一致。')
        profile,binding_doc,binding,_,_,age=_send_facts(c,context,intent,payload)
        if (recipient['profile_revision']!=profile['revision'] or recipient['binding_revision']!=binding_doc['revision']
                or recipient['browser_session_fingerprint']!=binding['browser_session_fingerprint']):
            raise KernelError('SEND_RECIPIENT_CHANGED','绑定建立后资料或浏览器会话已变化，请重新核对发送对象。')
        row=c.execute('SELECT * FROM runtime_send_attempts WHERE scope_id=? AND intent_id=?',(context['scope_id'],intent['intent_id'])).fetchone()
        if row is not None:
            existing=applicant_send.attempt(c,context['scope_id'],row['attempt_id'])
            if existing['recipient_binding_id']!=payload['recipient_binding_id']:
                raise KernelError('SEND_ATTEMPT_CONFLICT','同一回复意图已有不同发送对象的预占记录。')
            return {'attempt':applicant_send.public_attempt(existing),'budget':applicant_send.budget_view(c,context['scope_id']),
                    'created':False,'message_age_seconds':age,'sending_enabled':False}
        budget=applicant_send.budget_view(c,context['scope_id'])
        if budget['remaining']<1:
            raise KernelError('SEND_DAILY_LIMIT_REACHED','本机当日发送预占额度已用完，未创建新的预占记录。')
        attempt_id,now=uuid.uuid4().hex,time.time()
        _,window=applicant_reply_window.read(c,context['scope_id'])
        if window is None:
            raise KernelError('REPLY_WINDOW_REQUIRED','请先建立当前会话的消息观察起点。')
        c.execute('INSERT INTO runtime_send_attempts(attempt_id,scope_id,intent_id,draft_task_id,recipient_binding_id,budget_day,state,'
                  'message_sha256,reserved_units,window_start_task_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                  (attempt_id,context['scope_id'],intent['intent_id'],task['task_id'],recipient['recipient_fingerprint'],budget['budget_day'],
                   'reserved',hashlib.sha256(text.encode()).hexdigest(),1,window['start_observation_task_id'],now,now))
        saved=applicant_send.attempt(c,context['scope_id'],attempt_id)
        return record_commit(c,context,'runtime.applicant.send.reserve',
                              {'attempt':applicant_send.public_attempt(saved),'budget':applicant_send.budget_view(c,context['scope_id']),
                               'created':True,'message_age_seconds':age,'sending_enabled':False})


def send_release(payload,context):
    applicant_only(context)
    if (set(payload)!={'attempt_id','reason','confirmed'} or payload.get('confirmed') is not True
            or payload.get('reason') not in {'user_cancelled','preflight_drift','draft_changed'}
            or not isinstance(payload.get('attempt_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['attempt_id'])):
        raise KernelError('INVALID_SEND_RELEASE','请明确要释放的预占记录和原因。',400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        existing=applicant_send.attempt(c,context['scope_id'],payload['attempt_id'])
        if existing['state']!='reserved':
            raise KernelError('INVALID_SEND_ATTEMPT_STATE','只有尚未使用的预占记录可以释放；已送达或结果未知的记录不能回退。')
        if _used_evidence(c,context['scope_id'],existing['attempt_id']):
            raise KernelError('SEND_ALREADY_USED','这条预占已有自报或回读证据，不能释放额度；请人工核对实际结果。')
        now=time.time()
        c.execute('UPDATE runtime_send_attempts SET state=?,updated_at=? WHERE attempt_id=? AND scope_id=?',
                  ('released',now,existing['attempt_id'],context['scope_id']))
        released=applicant_send.attempt(c,context['scope_id'],existing['attempt_id'])
        return record_commit(c,context,'runtime.applicant.send.release',
                              {'attempt':applicant_send.public_attempt(released),'budget':applicant_send.budget_view(c,context['scope_id'],now),
                               'reason':payload['reason'],'sending_enabled':False})


def send_get(payload,context):
    applicant_only(context)
    if set(payload)!={'attempt_id'} or not isinstance(payload.get('attempt_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['attempt_id']):
        raise KernelError('INVALID_SEND_RESERVATION','请提供有效的发送预占标识。',400)
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        existing=applicant_send.attempt(c,context['scope_id'],payload['attempt_id'])
        return {'attempt':applicant_send.public_attempt(existing),'budget':applicant_send.budget_view(c,context['scope_id']),
                'sending_enabled':False}


def _readback_prepare(c,context,payload):
    """Re-read everything the readback evidence must be tied to, inside the caller's transaction."""
    existing=applicant_send.attempt(c,context['scope_id'],payload['attempt_id'])
    recipient=applicant_send.checked_recipient(
        store.runtime_document(c,context['scope_id'],applicant_send.recipient_kind(existing['intent_id'])),context['scope_id'],existing['intent_id'])
    _,task=applicant_reply.read(c,context['scope_id'],existing['intent_id']);task=dict(task)
    text,ready=_send_draft(task)
    if not ready:
        raise KernelError('DRAFT_NOT_READY','草稿已变化或不可用，无法核对回读证据。')
    if hashlib.sha256(text.encode()).hexdigest()!=existing['message_sha256']:
        raise KernelError('DRAFT_CHANGED_AFTER_RESERVATION','预占之后草稿被修改，回读不能作为本次预占的证据。')
    binding_doc=store.runtime_document(c,context['scope_id'],'applicant_browser_binding')
    binding=applicant_binding.checked(binding_doc,context['scope_id'])
    if binding_doc['revision']!=recipient['binding_revision'] or binding['browser_session_fingerprint']!=recipient['browser_session_fingerprint']:
        raise KernelError('SEND_RECIPIENT_CHANGED','浏览器会话绑定已变化，无法回读本次发送。')
    return existing,recipient,binding,text


def _readback_evidence(existing,recipient,binding,text):
    live=applicant_browser.read_chat(binding=binding,messages=True,preflight=True)
    party_match=isinstance(live,dict) and live.get('party')==recipient['party']
    evidence=(applicant_send.readback_evidence(live,text,recipient['message_ids']) if party_match
              else {'observed':False,'ambiguous':False,'reason':'conversation_changed','message_id':None,'message_sha256':None})
    return party_match,evidence


def send_readback(payload,context):
    applicant_only(context)
    if set(payload)!={'attempt_id'} or not isinstance(payload.get('attempt_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['attempt_id']):
        raise KernelError('INVALID_SEND_READBACK','请提供有效的发送预占标识。',400)
    with store.runtime_db() as c:
        c.execute('BEGIN');check_context(c,context['context_token'])
        existing,recipient,binding,text=_readback_prepare(c,context,payload)
    party_match,evidence=_readback_evidence(existing,recipient,binding,text)
    return {'attempt_id':existing['attempt_id'],'intent_id':existing['intent_id'],'party_match':party_match,
            'readback_observed':evidence['observed'],'evidence':evidence,'delivered_confirmed':False,
            'sending_enabled':False,'note':'本地回读证据，不等于平台送达确认。'}


def send_readback_commit(payload,context):
    """Persist one live readback observation; the conversation is read, never written."""
    applicant_only(context)
    if set(payload)!={'attempt_id'} or not isinstance(payload.get('attempt_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['attempt_id']):
        raise KernelError('INVALID_SEND_READBACK','请提供有效的发送预占标识。',400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        existing,recipient,binding,text=_readback_prepare(c,context,payload)
    party_match,evidence=_readback_evidence(existing,recipient,binding,text)
    content={'scope_id':context['scope_id'],'attempt_id':existing['attempt_id'],'observed':evidence['observed'],
             'ambiguous':evidence['ambiguous'],'reason':evidence['reason'],
             'match_count':int(evidence.get('match_count') or 0),'message_id':evidence['message_id'],
             'message_sha256':evidence['message_sha256'],'observed_at':time.time(),'party_match':party_match,
             'delivery_confirmed':False,'sending_enabled':False}
    kind=applicant_send.readback_kind(existing['attempt_id'])
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        # A08：浏览器读取期间状态可能已变化，写证据前在同一事务内用同一套判据重读一次。
        facts,blockers=_send_readiness(c,context,applicant_send.attempt(c,context['scope_id'],existing['attempt_id']),
                                       ignore_usage=True)
        if blockers:
            code,message=blockers[0]
            raise KernelError(code,message)
        if not facts['text'] or hashlib.sha256(facts['text'].encode()).hexdigest()!=existing['message_sha256']:
            raise KernelError('DRAFT_CHANGED_AFTER_RESERVATION','回读期间草稿或发送对象已变化，本次证据未写入。')
        document=store.runtime_document(c,context['scope_id'],kind)
        previous=(applicant_send.checked_readback(document,context['scope_id'],existing['attempt_id'])
                  if document['revision'] else None)
        changed=applicant_send.supersedes(previous,content)
        if previous is not None and not changed:
            record,created=previous,False
        else:
            if previous is None:
                c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)',
                          (context['scope_id'],kind,1,json.dumps(content,ensure_ascii=False)))
            else:
                c.execute('UPDATE runtime_documents SET content=? WHERE scope_id=? AND kind=?',
                          (json.dumps(content,ensure_ascii=False),context['scope_id'],kind))
            record,created=content,True
        return record_commit(c,context,'runtime.applicant.send.readback.commit',
                             {'readback':record,'created':created,'party_match':party_match,
                              'delivery_confirmed':False,'sending_enabled':False})


def _rate(hits,base):
    return round(hits/base,3) if base else None


def send_stats(payload,context):
    """Local ledger of what followed each reservation; it is not a platform reply-rate report."""
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_SEND_STATS','统计不需要参数。',400)
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        ids=[row[0] for row in c.execute('SELECT attempt_id FROM runtime_send_attempts WHERE scope_id=? ORDER BY created_at',
                                         (context['scope_id'],)).fetchall()]
        attempts=[applicant_send.attempt(c,context['scope_id'],attempt_id) for attempt_id in ids]
        rows=c.execute("SELECT kind,revision,content FROM runtime_documents WHERE scope_id=? AND "
                       "(kind LIKE 'applicant_send_handoff:%' OR kind LIKE 'applicant_send_claim:%' "
                       "OR kind LIKE 'applicant_send_readback:%')",(context['scope_id'],)).fetchall()
    documents={};invalid=[]
    for kind,revision,raw in rows:
        prefix,_,attempt_id=kind.partition(':')
        try:
            content=json.loads(raw)
        except ValueError:
            invalid.append(kind);continue
        documents.setdefault(attempt_id,{})[prefix]={'revision':revision,'content':content}
    items=[];states={};days={};drafts={}
    for attempt in attempts:
        attempt_id=attempt['attempt_id'];bundle=documents.get(attempt_id,{})
        try:
            handoff=(applicant_send.checked_handoff(bundle['applicant_send_handoff'],context['scope_id'],attempt_id)
                     if 'applicant_send_handoff' in bundle else None)
            claim=(applicant_send.checked_claim(bundle['applicant_send_claim'],context['scope_id'],attempt_id)
                   if 'applicant_send_claim' in bundle else None)
            readback=(applicant_send.checked_readback(bundle['applicant_send_readback'],context['scope_id'],attempt_id)
                      if 'applicant_send_readback' in bundle else None)
        except KernelError:
            invalid.append('attempt:'+attempt_id);continue
        hit=bool(readback and readback['observed'])
        item={'attempt_id':attempt_id,'intent_id':attempt['intent_id'],'state':attempt['state'],
              'budget_day':attempt['budget_day'],'draft_text_sha256':attempt['message_sha256'],
              'handoff':handoff is not None,'claimed':claim is not None,'readback_observed':hit,
              'readback_ambiguous':bool(readback and readback['ambiguous'])}
        items.append(item)
        states[attempt['state']]=states.get(attempt['state'],0)+1
        for bucket,key in ((days,attempt['budget_day']),(drafts,attempt['message_sha256'])):
            entry=bucket.setdefault(key,{'attempts':0,'handoffs':0,'claims':0,'hits':0,'ambiguous':0})
            entry['attempts']+=1;entry['handoffs']+=item['handoff'];entry['claims']+=item['claimed']
            entry['hits']+=hit;entry['ambiguous']+=item['readback_ambiguous']
    handoffs=sum(1 for item in items if item['handoff'])
    claims=sum(1 for item in items if item['claimed'])
    hits=sum(1 for item in items if item['readback_observed'])
    ambiguous=sum(1 for item in items if item['readback_ambiguous'])
    by_draft=[{'draft_text_sha256':key,**value} for key,value in drafts.items()]
    by_draft.sort(key=lambda entry:(-entry['attempts'],entry['draft_text_sha256']))
    return {'totals':{'attempts':len(items),'by_state':states,'handoffs':handoffs,'claims':claims,
                      'readback_hits':hits,'readback_ambiguous':ambiguous,
                      'handoff_to_readback_rate':_rate(hits,handoffs),'claim_to_readback_rate':_rate(hits,claims)},
            'by_day':[{'budget_day':key,**value} for key,value in sorted(days.items())],
            'by_draft':by_draft,'items':items[-50:],'truncated':max(0,len(items)-50),
            'invalid_documents':invalid,
            'limits':{'readback_rate_basis':'manual handoff cards that also have a persisted readback observation',
                      'not_platform_reply_rate':True,'delivery_not_confirmed':True,
                      'sample_warning':(None if handoffs>=10 else '交接样本不足 10 条，比率只作方向参考')},
            'delivery_confirmed':False,'sending_enabled':False}


def _send_prepare(c,context,payload):
    """Re-read the attempt, its immutable recipient binding, draft and observation window."""
    attempt=applicant_send.attempt(c,context['scope_id'],payload['attempt_id'])
    recipient=applicant_send.checked_recipient(
        store.runtime_document(c,context['scope_id'],applicant_send.recipient_kind(attempt['intent_id'])),context['scope_id'],attempt['intent_id'])
    intent,task=applicant_reply.read(c,context['scope_id'],attempt['intent_id']);task=dict(task)
    text,ready=_send_draft(task)
    if not ready:
        raise KernelError('DRAFT_NOT_READY','草稿已变化或不可用。')
    if hashlib.sha256(text.encode()).hexdigest()!=attempt['message_sha256']:
        raise KernelError('DRAFT_CHANGED_AFTER_RESERVATION','预占之后草稿被修改，不能继续交接。')
    if recipient['recipient_fingerprint']!=attempt['recipient_binding_id']:
        raise KernelError('SEND_RECIPIENT_CHANGED','预占记录与发送对象绑定不一致。')
    _,window=applicant_reply_window.read(c,context['scope_id'])
    if window is None:
        raise KernelError('REPLY_WINDOW_REQUIRED','请先建立当前会话的消息观察起点。')
    return attempt,recipient,intent,task,text,window


def _handoff_card(c,context,attempt_id):
    """Single and batch handoff share one readiness verdict with the queue view."""
    attempt=applicant_send.attempt(c,context['scope_id'],attempt_id)
    facts,blockers=_send_readiness(c,context,attempt)
    if blockers:
        code,message=blockers[0]
        raise KernelError(code,message)
    recipient,intent,task,text,window=facts['recipient'],facts['intent'],facts['task'],facts['text'],facts['window']
    expires=facts['expires'];now=time.time()
    content={'scope_id':context['scope_id'],'attempt_id':attempt['attempt_id'],'intent_id':attempt['intent_id'],
             'draft_task_id':attempt['draft_task_id'],'recipient_fingerprint':attempt['recipient_binding_id'],
             'party_display_name':recipient['party']['display_name'],'anchor_message_id':recipient['anchor_message_id'],
             'message_ids':list(recipient['message_ids']),'draft_text_sha256':attempt['message_sha256'],
             'text_expires_at':expires,'created_at':now,'manual_handoff_only':True,'sending_enabled':False}
    kind=applicant_send.handoff_kind(attempt['attempt_id'])
    document=store.runtime_document(c,context['scope_id'],kind)
    if document['revision']:
        existing=applicant_send.checked_handoff(document,context['scope_id'],attempt['attempt_id'])
        if any(existing[key]!=content[key] for key in ('intent_id','draft_task_id','recipient_fingerprint','party_display_name',
                                                      'anchor_message_id','message_ids','draft_text_sha256','text_expires_at')):
            raise KernelError('SEND_HANDOFF_CHANGED','交接卡与实际发送对象或草稿不一致，请重新核对。')
        handoff,created=existing,False
    else:
        c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)',(context['scope_id'],kind,1,json.dumps(content,ensure_ascii=False)))
        handoff,created=content,True
    return {'handoff':handoff,'text':text,'created':created,'text_expires_at':expires,
            'expires_in_seconds':max(0,expires-now),'manual_handoff_only':True,
            'delivery_confirmed':False,'sending_enabled':False}


def send_handoff(payload,context):
    """Produce a copy-ready card for a person to send by hand; the platform is never touched."""
    applicant_only(context)
    if (set(payload)!={'attempt_id','confirmed'} or payload.get('confirmed') is not True
            or not isinstance(payload.get('attempt_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['attempt_id'])):
        raise KernelError('INVALID_SEND_HANDOFF','请明确要交接的预占记录。',400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        card=_handoff_card(c,context,payload['attempt_id'])
        committed=record_commit(c,context,'runtime.applicant.send.handoff',
                                {key:value for key,value in card.items() if key!='text'})
    # The draft body is returned to the caller but never written into the control receipt.
    return {**committed,'text':card['text']}


QUEUE_LIMIT=10


def send_queue_get(payload,context):
    """Read-only view of what could be handed off right now, including expiry."""
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_SEND_QUEUE','队列查询不接受参数。',400)
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        rows=c.execute("SELECT attempt_id FROM runtime_send_attempts WHERE scope_id=? AND state='reserved' ORDER BY created_at",
                       (context['scope_id'],)).fetchall()
        now=time.time();items=[]
        for row in rows:
            attempt_id=row[0]
            entry={'attempt_id':attempt_id,'ready':False,'reason':None,'detail':None,'has_handoff':False,
                   'claimed':False,'expires_at':None,'expires_in_seconds':None,'expired':None}
            try:
                attempt=applicant_send.attempt(c,context['scope_id'],attempt_id)
                facts,blockers=_send_readiness(c,context,attempt)
                handoff_doc=store.runtime_document(c,context['scope_id'],applicant_send.handoff_kind(attempt_id))
                claim_doc=store.runtime_document(c,context['scope_id'],applicant_send.claim_kind(attempt_id))
                expires=facts['expires']
                entry.update({'intent_id':attempt['intent_id'],
                              'party_display_name':(facts['recipient']['party']['display_name'] if facts['recipient'] else None),
                              'has_handoff':bool(handoff_doc['revision']),'claimed':bool(claim_doc['revision']),
                              'expires_at':expires,'expires_in_seconds':(max(0,expires-now) if expires else None),
                              'expired':(now>expires if expires else None),'ready':not blockers})
                if blockers:
                    entry.update({'reason':blockers[0][0],'detail':blockers[0][1]})
            except KernelError as error:
                entry.update({'reason':error.code,'detail':str(error)})
            items.append(entry)
    return {'items':items,'count':len(items),'queue_limit':QUEUE_LIMIT,
            'sending_enabled':False,'delivery_confirmed':False}


def send_queue_handoff(payload,context):
    """Materialise several cards at once; one failing card never blocks the others."""
    applicant_only(context)
    ids=payload.get('attempt_ids')
    if (set(payload)!={'attempt_ids','confirmed'} or payload.get('confirmed') is not True
            or not isinstance(ids,list) or not 1<=len(ids)<=QUEUE_LIMIT
            or any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{32}',value) for value in ids)):
        raise KernelError('INVALID_SEND_QUEUE',f'请提供 1–{QUEUE_LIMIT} 个有效的预占标识。',400)
    ordered=list(dict.fromkeys(ids))
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        cards=[];skipped=[]
        for attempt_id in ordered:
            try:
                card=_handoff_card(c,context,attempt_id)
                cards.append({'attempt_id':attempt_id,
                              **{key:value for key,value in card.items() if key!='text'},'text':card['text']})
            except KernelError as error:
                skipped.append({'attempt_id':attempt_id,'code':error.code,'message':str(error)})
        if not cards:
            raise KernelError(skipped[0]['code'],'批量交接全部失败，第一条原因：'+skipped[0]['message'])
        result={'cards':[{key:value for key,value in card.items() if key!='text'} for card in cards],
                'skipped':skipped,'ok_count':len(cards),'skipped_count':len(skipped),
                'created_count':sum(1 for card in cards if card['created']),
                'partial':bool(skipped),'queue_limit':QUEUE_LIMIT,'manual_handoff_only':True,
                'delivery_confirmed':False,'sending_enabled':False}
        committed=record_commit(c,context,'runtime.applicant.send.queue.handoff',result)
    return {**committed,'cards':cards,
            'note':'批量交接不是原子的：cards 里是已写入的记录，skipped 里没有任何改动。'}


def send_handoff_get(payload,context):
    applicant_only(context)
    if set(payload)!={'attempt_id'} or not isinstance(payload.get('attempt_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['attempt_id']):
        raise KernelError('INVALID_SEND_HANDOFF','请提供有效的发送预占标识。',400)
    with store.runtime_db() as c:
        check_context(c,context['context_token'])
        attempt=applicant_send.attempt(c,context['scope_id'],payload['attempt_id'])
        document=store.runtime_document(c,context['scope_id'],applicant_send.handoff_kind(attempt['attempt_id']))
        handoff=(applicant_send.checked_handoff(document,context['scope_id'],attempt['attempt_id']) if document['revision'] else None)
        claim_doc=store.runtime_document(c,context['scope_id'],applicant_send.claim_kind(attempt['attempt_id']))
        claim=(applicant_send.checked_claim(claim_doc,context['scope_id'],attempt['attempt_id']) if claim_doc['revision'] else None)
    expired=bool(handoff and time.time()>handoff['text_expires_at'])
    return {'attempt':applicant_send.public_attempt(attempt),'handoff':handoff,'claim':claim,
            'expired':expired,'delivery_confirmed':False,'sending_enabled':False}


def send_manual_claim(payload,context):
    """A person reports they sent it by hand; still not a delivery confirmation."""
    applicant_only(context)
    if (set(payload)!={'attempt_id','confirmed'} or payload.get('confirmed') is not True
            or not isinstance(payload.get('attempt_id'),str) or not re.fullmatch(r'[0-9a-f]{32}',payload['attempt_id'])):
        raise KernelError('INVALID_SEND_CLAIM','请明确报告已手动发送的预占记录。',400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE');check_context(c,context['context_token'])
        attempt=applicant_send.attempt(c,context['scope_id'],payload['attempt_id'])
        document=store.runtime_document(c,context['scope_id'],applicant_send.handoff_kind(attempt['attempt_id']))
        if not document['revision']:
            raise KernelError('SEND_HANDOFF_REQUIRED','请先生成待发卡片，再报告已手动发送。')
        handoff=applicant_send.checked_handoff(document,context['scope_id'],attempt['attempt_id'])
        now=time.time()
        if now>handoff['text_expires_at']:
            raise KernelError('HANDOFF_EXPIRED','交接卡已过时效；如确实已发送，请重新读取会话后核对送达。')
        if handoff['draft_text_sha256']!=attempt['message_sha256']:
            raise KernelError('SEND_HANDOFF_CHANGED','交接卡与预占记录的草稿不一致。')
        content={'scope_id':context['scope_id'],'attempt_id':attempt['attempt_id'],'handoff_revision':1,
                 'claimed_at':now,'self_reported':True,'delivery_confirmed':False,'sending_enabled':False}
        kind=applicant_send.claim_kind(attempt['attempt_id'])
        claim_doc=store.runtime_document(c,context['scope_id'],kind)
        if claim_doc['revision']:
            claim,created=applicant_send.checked_claim(claim_doc,context['scope_id'],attempt['attempt_id']),False
        else:
            c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)',(context['scope_id'],kind,1,json.dumps(content,ensure_ascii=False)))
            claim,created=content,True
        return record_commit(c,context,'runtime.applicant.send.manual.claim',
                             {'attempt':applicant_send.public_attempt(attempt),'claim':claim,'created':created,
                              'delivery_confirmed':False,'sending_enabled':False,
                              'note':'用户自报已手动发送，不是平台确认；送达仍以回读证据为准。'})


JOB_SEARCH_KIND = 'applicant_job_search'
# 批量补读详情：一次 1..5 条、总时长 40 秒、每条之间重读会话。
DETAILS_TOOL = 'runtime.applicant.jobs.search.details'
DETAILS_MAX = 5
DETAILS_DEFAULT = 3
DETAILS_SECONDS = 40.0
DETAILS_RENDER_SECONDS = 1.5
DETAILS_OK = frozenset({'stored', 'updated', 'unchanged'})


def _job_search_state(c, context):
    document = store.runtime_document(c, context['scope_id'], JOB_SEARCH_KIND)
    if not document['revision']:
        return None, document
    try:
        content = document['content']
        if isinstance(content, str):
            content = json.loads(content)
        return applicant_job_search.validate(content), document
    except (TypeError, ValueError):
        raise KernelError('INVALID_JOB_SEARCH_STATE', '搜索记录无法核对，请重新开始。') from None


def _job_search_save(c, context, state):
    current = store.runtime_document(c, context['scope_id'], JOB_SEARCH_KIND)
    c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
              "DO UPDATE SET revision=excluded.revision,content=excluded.content",
              (context['scope_id'], JOB_SEARCH_KIND, current['revision'] + 1,
               json.dumps(state, ensure_ascii=False)))
    return store.runtime_document(c, context['scope_id'], JOB_SEARCH_KIND)


def applicant_search_open(payload, context):
    """Navigate to one planned city×keyword search page (maintenance, no sending)."""
    applicant_only(context)
    if set(payload) != {'keyword', 'city'}:
        raise KernelError('INVALID_JOB_SEARCH', '请提供方案内的关键词与城市。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        if not plan['revision']:
            raise KernelError('APPLICANT_SETUP_REQUIRED', '请先确认保存本人资料和求职方案。')
        content = applicant_jobs.validate_plan(plan['content'])
        keywords = [item.strip() for item in content['keywords']]
        cities = [applicant_jobs.city(item) for item in content['primary_cities'] + content['secondary_cities']]
        keyword = payload['keyword'].strip() if isinstance(payload['keyword'], str) else ''
        city = applicant_jobs.city(payload['city']) if isinstance(payload['city'], str) else ''
        if keyword not in keywords or city not in cities:
            raise KernelError('JOB_SEARCH_OUT_OF_PLAN',
                              '关键词或城市不在已保存的求职方案内，未开始搜索。', 400)
        state, document = _job_search_state(c, context)
        now = time.time()
        if state is None:
            state = applicant_job_search.new(content, plan['revision'], now)
            document = _job_search_save(c, context, state)
        elif state['plan_revision'] != plan['revision']:
            raise KernelError('STALE_JOB_SEARCH', '求职方案已变化，请重新开始搜索。')
        elif state['status'] != 'running':
            raise KernelError('INVALID_JOB_SEARCH_STATE', '上次搜索已结束，请重新开始。')
        combo = applicant_job_search.current(state)
        if combo is None:
            raise KernelError('INVALID_JOB_SEARCH_STATE', '本轮组合已读完，请重新开始。')
        if [combo['city'], combo['keyword']] != [city, keyword]:
            return {'status': 'out_of_order', 'expected': {'city': combo['city'], 'keyword': combo['keyword']},
                    'state': applicant_job_search.public(state), 'sending_enabled': False}
        revision = document['revision']
    navigation = applicant_browser.search_open(keyword, city)
    if navigation['status'] != 'navigation_requested':
        return {**navigation, 'state': applicant_job_search.public(state), 'automatic_execution': False}
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        if store.runtime_document(c, context['scope_id'], 'applicant_plan')['revision'] != plan['revision']:
            raise KernelError('STALE_APPLICANT_PLAN', '求职方案已变化，未采用本次导航结果。')
        state, document = _job_search_state(c, context)
        if state is None or document['revision'] != revision:
            raise KernelError('STALE_JOB_SEARCH', '搜索记录已变化，请重新读取。')
        mark = applicant_job_search.mark_navigated(state, time.time())
        _job_search_save(c, context, state)
        public = applicant_job_search.public(state)
    # local_ui 动作没有控制提交凭据；已落库的只是"已导航到该组合"的观测状态。
    return {'navigation': navigation, 'navigated': mark, 'state': public,
            'account_verified': False, 'sending_enabled': False, 'automatic_execution': False}


def applicant_search_step(payload, context):
    """Read one page of the current combination and fold it into the session."""
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_REQUEST', '搜索读取不接受地址或执行脚本。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        state, document = _job_search_state(c, context)
        if state is None:
            raise KernelError('JOB_SEARCH_REQUIRED', '请先开始一次有界搜索。', 400)
        if state['plan_revision'] != plan['revision']:
            raise KernelError('STALE_JOB_SEARCH', '求职方案已变化，请重新开始搜索。')
        if state['status'] != 'running':
            return {'status': 'finished', 'state': applicant_job_search.public(state),
                    'sending_enabled': False}
        if applicant_job_search.needs_navigation(state):
            combo = applicant_job_search.current(state)
            return {'status': 'navigation_required',
                    'next': None if combo is None else {'city': combo['city'], 'keyword': combo['keyword']},
                    'state': applicant_job_search.public(state), 'sending_enabled': False}
        combo = applicant_job_search.current(state)
        if combo is None:
            return {'status': 'finished', 'state': applicant_job_search.public(state),
                    'sending_enabled': False}
        revision = document['revision']
        pages_read = combo['pages']
    # 浏览器 I/O 必须留在写事务之外（方案第 94 行「浏览器 IO 不能占用长写锁」）：
    # scroll_jobs / read_current_jobs 内部会 current_execution → require_registered →
    # 再开一个 store.runtime_db() → connect() 的 _migrate() 无条件 BEGIN IMMEDIATE，
    # 外层写事务未提交时就是自己锁自己，5 秒后 database is locked（BUG-6，2026-10-28 实测）。
    scroll = None
    if pages_read > 0:
        scroll = applicant_browser.scroll_jobs()
    if scroll is not None and scroll['status'] != 'scrolled':
        observation = {'status': 'unavailable', 'jobs': []}
    else:
        if scroll is not None:
            time.sleep(1.2)
        observation = applicant_browser.read_current_jobs(
            execution='runtime.applicant.jobs.search.step')
        # 刚导航完立刻读，页面可能还没渲染出岗位卡片 → 会误判 schema_changed。
        # 2026-10-27 live 实测：同一页面再读一次就是 observed/15 条。
        # 有界重试（最多 2 次、每次 0.8s）；仍然读不到才保留 schema_changed，
        # 不放宽「结构变化」这个判据，也不是把失败折叠成空列表。
        if observation['status'] == 'schema_changed' and pages_read == 0:
            for _ in range(2):
                time.sleep(0.8)
                observation = applicant_browser.read_current_jobs(
                    execution='runtime.applicant.jobs.search.step')
                if observation['status'] != 'schema_changed':
                    break
    if observation['status'] not in {'observed', 'empty', 'restricted', 'auth_required', 'schema_changed'}:
        observation = {'status': 'unavailable', 'jobs': []}
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        state, document = _job_search_state(c, context)
        if state is None or document['revision'] != revision:
            raise KernelError('STALE_JOB_SEARCH', '搜索记录已变化，请重新读取。')
        event = applicant_job_search.apply_page(state, observation, time.time())
        _job_search_save(c, context, state)
        # 只读提示：还剩多少条缺 JD，调用方自己决定要不要继续补（这里不自动读详情）。
        return record_commit(c, context, 'runtime.applicant.jobs.search.step',
                             {'status': 'page_read', 'event': event,
                              'state': applicant_job_search.public(state),
                              'pending_details': applicant_job_search.pending_count(state),
                              'scroll': scroll, 'account_verified': False,
                              'sending_enabled': False, 'automatic_execution': False})


def applicant_job_detail(payload, context):
    """Open one job-detail page and fold its JD text into the current search session.

    只读正文，不点击列表卡、不填任何输入框。导航由 applicant_browser.detail_open
    用 job_id 拼固定地址完成；读取后在同一写事务里重新核对会话 revision，
    读取期间会话被改（重置/停止/换方案）就不写，避免把旧证据挂到新会话上。
    """
    applicant_only(context)
    if not isinstance(payload, dict) or set(payload) != {'job_id'}:
        raise KernelError('INVALID_JOB_DETAIL', '请只提供 job_id 一个字段。', 400)
    job_id = payload['job_id']
    if not applicant_browser.job_id_ok(job_id):
        raise KernelError('INVALID_JOB_DETAIL', '岗位标识不合法。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        state, document = _job_search_state(c, context)
        if state is None:
            raise KernelError('JOB_SEARCH_REQUIRED', '请先开始一次有界搜索。', 400)
        if state['plan_revision'] != plan['revision']:
            raise KernelError('STALE_JOB_SEARCH', '求职方案已变化，请重新开始搜索。')
        if job_id not in state['jobs']:
            raise KernelError('OBSERVED_JOB_NOT_FOUND', '该岗位不在本次读取记录中。', 404)
        revision = document['revision']
    opened = applicant_browser.detail_open(job_id)
    if opened['status'] != 'navigation_requested':
        return {'status': opened['status'], 'job_id': job_id,
                'account_verified': False, 'sending_enabled': False,
                'automatic_execution': False, 'delivery_confirmed': False}
    try:
        time.sleep(1.5)
        observed = applicant_browser.real_job_detail(expected_job_id=job_id)
    finally:
        # BUG-5：详情页在自己的 tab 里打开，读完必须关掉，否则残留 tab 会让下次读取
        # 变成 page_selection_required。关不掉不重要（下次 open 会清理）。
        applicant_browser.detail_close(opened.get('target_id'))
    status = observed['status']
    if status not in {'observed', 'jd_missing'}:
        return {'status': status, 'job_id': job_id,
                'account_verified': False, 'sending_enabled': False,
                'automatic_execution': False, 'delivery_confirmed': False}
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        state, document = _job_search_state(c, context)
        if state is None or document['revision'] != revision:
            raise KernelError('STALE_JOB_SEARCH', '搜索记录已变化，职责描述未写入。')
        if status == 'jd_missing':
            detail = {'status': 'jd_missing', 'job_id': job_id,
                      'title': observed.get('title') or '', 'company': observed.get('company') or ''}
        else:
            detail = applicant_job_search.enrich_jd(
                state, job_id, observed['jd'], time.time(), truncated=observed['truncated'])
            _job_search_save(c, context, state)
        # local_ui 动作没有控制提交凭据（同 jobs.search.open）：已落库的只是岗位正文证据。
        return {'status': detail['status'], 'job_id': job_id, 'detail': detail,
                'account_verified': False, 'sending_enabled': False,
                'automatic_execution': False, 'delivery_confirmed': False,
                'read_only': True}


def applicant_search_details(payload, context):
    """Fill JD text for jobs already collected in the current search session.

    有界：一次 1..5 条（缺省 3），整批总时长 DETAILS_SECONDS。每条之间重读会话，
    revision 变了立即停（不把刚读到的正文写进别人的会话）；单条失败只记一条，
    不拖垮整批。返回 results / filled / failed / remaining / pending_total。
    """
    applicant_only(context)
    if not isinstance(payload, dict) or set(payload) - {'limit'}:
        raise KernelError('INVALID_JOB_SEARCH', '批量补读职责描述只接受 limit 一个字段。', 400)
    limit = payload.get('limit', DETAILS_DEFAULT)
    if type(limit) is not int or not 1 <= limit <= DETAILS_MAX:
        raise KernelError('INVALID_JOB_SEARCH',
                          '每次最多补 %d 条职责描述。' % DETAILS_MAX, 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        state, document = _job_search_state(c, context)
        if state is None:
            raise KernelError('JOB_SEARCH_REQUIRED', '请先开始一次有界搜索。', 400)
        if state['plan_revision'] != plan['revision']:
            raise KernelError('STALE_JOB_SEARCH', '求职方案已变化，请重新开始搜索。')
        revision = document['revision']
        pending_total = applicant_job_search.pending_count(state)
        job_ids = applicant_job_search.pending_details(state, limit)
    started = time.monotonic()
    deadline = started + DETAILS_SECONDS
    results, filled = [], 0
    stopped = None
    for job_id in job_ids:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            stopped = 'LIMIT_SECONDS'
            break
        try:
            opened = applicant_browser.detail_open(job_id, execution=DETAILS_TOOL)
            if opened.get('status') != 'navigation_requested':
                results.append({'job_id': job_id, 'status': opened.get('status') or 'unavailable',
                                'reason': 'navigation_refused'})
                continue
            try:
                time.sleep(min(DETAILS_RENDER_SECONDS, max(0.0, deadline - time.monotonic())))
                observed = applicant_browser.real_job_detail(expected_job_id=job_id)
            finally:
                applicant_browser.detail_close(opened.get('target_id'))
        except KernelError as error:
            # 单条核不上（读错对象/页面结构变化）只废这一条，不影响后面几条。
            results.append({'job_id': job_id, 'status': 'error', 'reason': error.code})
            continue
        status = observed.get('status')
        if status != 'observed':
            results.append({'job_id': job_id, 'status': status or 'unavailable',
                            'reason': 'jd_missing' if status == 'jd_missing' else 'detail_read_refused'})
            continue
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            check_context(c, context['context_token'])
            state, document = _job_search_state(c, context)
            if state is None or document['revision'] != revision:
                # 读详情期间会话被改（重置/停止/再读页）：这条不写，立即停。
                stopped = 'STALE_JOB_SEARCH'
                break
            try:
                detail = applicant_job_search.enrich_jd(
                    state, job_id, observed['jd'], time.time(), truncated=observed['truncated'])
            except KernelError as error:
                results.append({'job_id': job_id, 'status': 'error', 'reason': error.code})
                continue
            revision = _job_search_save(c, context, state)['revision']
        results.append({'job_id': job_id, 'status': detail['status'], 'reason': None,
                        'jd_chars': detail['jd_chars'], 'truncated': detail['truncated']})
        filled += 1
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        state, _ = _job_search_state(c, context)
    remaining = applicant_job_search.pending_count(state) if state is not None else 0
    failed = len(results) - filled
    return {'status': 'stopped' if stopped else 'batch_done', 'stopped': stopped,
            'results': results, 'filled': filled, 'failed': failed,
            'remaining': remaining, 'pending_total': pending_total, 'limit': limit,
            'elapsed_seconds': round(max(0.0, time.monotonic() - started), 3),
            'account_verified': False, 'sending_enabled': False,
            'automatic_execution': False, 'read_only': True,
            'note': '批量补读只补职责正文；单条失败已逐条记录，未读到的仍需人工核对。'}


def applicant_search_get(payload, context):
    """Read the current bounded-search session and its explained candidates."""
    applicant_only(context)
    if set(payload) - {'include_candidates'} or not isinstance(payload.get('include_candidates', True), bool):
        raise KernelError('INVALID_JOB_SEARCH', '读取参数无法核对。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        state, _ = _job_search_state(c, context)
    if state is None:
        return {'status': 'not_started', 'state': None, 'candidates': [],
                'sending_enabled': False, 'automatic_execution': False}
    result = {'status': 'ok', 'state': applicant_job_search.public(state),
              'stale': state['plan_revision'] != plan['revision'], 'candidates': [],
              'account_verified': False, 'sending_enabled': False, 'automatic_execution': False}
    if payload.get('include_candidates', True) and state['jobs']:
        records = applicant_job_search.job_records(state)
        candidates, counts = [], {'eligible': 0, 'needs_review': 0, 'rejected': 0}
        # applicant_jobs.validate_jobs 单次最多 50 条；采集上限 60，按 50 分批匹配后合并。
        for start in range(0, len(records), 50):
            try:
                preview = applicant_preview_records(records[start:start + 50], context)
            except KernelError as error:
                if error.code != 'APPLICANT_SETUP_REQUIRED':
                    raise
                result['setup_required'] = True
                result['candidates'] = []
                return result
            candidates.extend(preview['candidates'])
            for key, value in preview['counts'].items():
                counts[key] = counts.get(key, 0) + value
            result['plan_revision'] = preview['plan_revision']
            result['profile_revision'] = preview['profile_revision']
        order = {'eligible': 0, 'needs_review': 1, 'rejected': 2}
        candidates.sort(key=lambda item: (order[item['decision']],
                                          item['city_priority'] if item['city_priority'] is not None else 999,
                                          item['job_id']))
        # 方案第 119 行：每条都要有来源与观察时间，不能只给会话级聚合值
        observed = state.get('observed_at') if isinstance(state.get('observed_at'), dict) else {}
        for item in candidates:
            item['source'] = item.get('source') if isinstance(item.get('source'), str) and item['source'].strip() else 'bounded_browser_search'
            stamp = observed.get(item.get('job_id'), item.get('observed_at_epoch'))
            item['observed_at'] = applicant_jobs.observed_iso(stamp)
            item['observed_at_epoch'] = stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None
        result.update({'candidates': candidates, 'decision_counts': counts,
                       'source_type': 'bounded_browser_search'})
    return result


def applicant_search_stop(payload, context):
    """Stop the current session; collected evidence stays readable."""
    applicant_only(context)
    if set(payload) != {'confirmed'} or payload.get('confirmed') is not True:
        raise KernelError('INVALID_JOB_SEARCH', '请明确确认停止本次搜索。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        state, _ = _job_search_state(c, context)
        if state is None:
            raise KernelError('JOB_SEARCH_REQUIRED', '当前没有进行中的搜索。', 400)
        view = applicant_job_search.stop(state, 'USER_STOPPED', time.time())
        _job_search_save(c, context, state)
        return record_commit(c, context, 'runtime.applicant.jobs.search.stop',
                             {'state': view, 'sending_enabled': False})


def applicant_search_reset(payload, context):
    """Drop the stored search session so a new one can start. Maintenance only."""
    applicant_only(context)
    if set(payload) != {'confirmed'} or payload.get('confirmed') is not True:
        raise KernelError('INVALID_JOB_SEARCH', '请明确确认清除本次搜索记录。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        row = c.execute('SELECT 1 FROM runtime_documents WHERE scope_id=? AND kind=?',
                        (context['scope_id'], JOB_SEARCH_KIND)).fetchone()
        if row is None:
            raise KernelError('JOB_SEARCH_REQUIRED', '当前没有搜索记录。', 400)
        c.execute('DELETE FROM runtime_documents WHERE scope_id=? AND kind=?',
                  (context['scope_id'], JOB_SEARCH_KIND))
        return record_commit(c, context, 'runtime.applicant.jobs.search.reset',
                             {'status': 'reset', 'sending_enabled': False,
                              'note': '搜索记录已清除；下一次开始搜索会重新建立会话。'})

# ---------------------------------------------------------------- G3 单次受限动作
# 一次动作 = 一份只能用一次的授权 + 一次填入/提交 + 一次只读观察 + 六态结果。
# 数量上限硬编码为 1（applicant_action.MAX_USES），不存在批量授权这条路。
ACTION_AUTH_KIND = applicant_action.AUTHORIZATION_KIND
CONTROLLED_TARGET = {'job_id': 'controlled-job-1', 'company': '受控公司'}
OBSERVE_SECONDS = 3.0
OBSERVE_INTERVAL_SECONDS = 0.2
ACTION_RESULT_KIND = 'applicant_action'
ACTION_REFUSAL = {
    'CHANNEL_NOT_AUTHORIZED': '没有可用的渠道授权，当前只提供草稿。',
    'CHANNEL_AUTHORIZATION_EXHAUSTED': '这份授权已经用过一次，要再动作必须重新登记并重新核对目标。',
    'CHANNEL_AUTHORIZATION_EXPIRED': '授权已过期，请重新登记。',
    'CHANNEL_TARGET_CHANGED': '目标已变化，这份授权不能用于新的对象。',
    'CHANNEL_PROFILE_CHANGED': '本人资料已变化，请重新登记授权。',
    'CHANNEL_PLAN_CHANGED': '求职方案已变化，请重新登记授权。',
}


def _action_doc(c, context, kind):
    return store.runtime_document(c, context['scope_id'], kind)


def _action_write(c, context, kind, payload):
    current = store.runtime_document(c, context['scope_id'], kind)
    c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
              "DO UPDATE SET revision=excluded.revision,content=excluded.content",
              (context['scope_id'], kind, current['revision'] + 1,
               json.dumps(payload, ensure_ascii=False)))
    return store.runtime_document(c, context['scope_id'], kind)


def _action_claim(c, context, expected_revision, payload):
    """乐观锁：只有 revision 没被别人推进过，才算抢到这份授权。

    不做这层的话，两个进程可以同时读到同一份 ready 授权、各执行一次，
    一次性就变成了两次 —— 这正是「同会话写操作必须串行」的落点。
    """
    cursor = c.execute(
        "UPDATE runtime_documents SET revision=revision+1, content=? "
        "WHERE scope_id=? AND kind=? AND revision=?",
        (json.dumps(payload, ensure_ascii=False), context['scope_id'],
         ACTION_AUTH_KIND, expected_revision))
    return cursor.rowcount == 1


GREETING_ATTEMPTS_KIND = 'applicant_greeting_attempts'
GREETING_SETTINGS_KIND = 'applicant_greeting_settings'
MAX_ATTEMPTS_KEPT = 500
PLATFORM_LIMIT_TTL_SECONDS = 24 * 3600


def _limit_state(c, context):
    doc = store.runtime_document(c, context['scope_id'], GREETING_SETTINGS_KIND)
    content = doc['content'] if isinstance(doc['content'], dict) else {}
    return content


def _used_today(c, context):
    """当日已用额度：跨工作区汇总（换 scope 不能刷新 150 上限）。"""
    return applicant_greeting.usage(_attempts_read_all(c), day=applicant_greeting.day_key())


def reserve_quota(c, context, job_id, company):
    """在同一事务里「检查 + 占位」，否则两个进程会各自读到 used=N 双双放行。

    必须由调用方在 BEGIN IMMEDIATE 事务里调用；占位记录 state='reserved'，
    执行结束后由真实结果原地替换，崩在半路则保留（保守：算用掉）。
    """
    settings = _limit_state(c, context)
    limit = applicant_greeting.effective_limit(
        settings.get('configured', applicant_greeting.DAILY_LIMIT),
        settings.get('platform_limit'), settings.get('platform_expires_at'))
    counted = _used_today(c, context)
    if counted['used'] >= limit:
        raise KernelError('DAILY_LIMIT_REACHED',
                          '今天的招呼额度已用完，正常停止。', 429)
    _attempts_append(c, context, applicant_greeting.attempt(
        account_id=context['scope_id'], job_id=job_id, company=company,
        state=applicant_greeting.RESERVED_STATE))
    return limit - counted['used'] - 1


def _attempts_read(c, context):
    doc = store.runtime_document(c, context['scope_id'], GREETING_ATTEMPTS_KIND)
    content = doc['content']
    items = content.get('items') if isinstance(content, dict) else None
    return [item for item in (items or ()) if isinstance(item, dict)], doc['revision']


def _attempts_append(c, context, record, replace_reserved=False):
    """每条动作结果都要留痕：G4 的额度与去重全靠这份历史。

    replace_reserved=True 时把同一条已占位记录换成真实结果，避免占位与结果双计额度。
    """
    items, revision = _attempts_read(c, context)
    if replace_reserved:
        for index, item in enumerate(items):
            if (item.get('state') == applicant_greeting.RESERVED_STATE
                    and item.get('job_id') == record['job_id']
                    and item.get('account_id') == record['account_id']
                    and item.get('day') == record['day']):
                items[index] = record
                break
        else:
            items.append(record)
    else:
        items.append(record)
    items = items[-MAX_ATTEMPTS_KEPT:]
    c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
              "DO UPDATE SET revision=excluded.revision,content=excluded.content",
              (context['scope_id'], GREETING_ATTEMPTS_KIND, revision + 1,
               json.dumps({'items': items}, ensure_ascii=False)))


def applicant_action_authorize(payload, context):
    """登记一份一次性渠道授权；正文只存摘要，不存原文。"""
    applicant_only(context)
    if set(payload) - {'action', 'account_evidence', 'job_id', 'company', 'conversation_key',
                       'text', 'ttl_seconds'}:
        raise KernelError('INVALID_ACTION_REQUEST', '授权参数不受支持。', 400)
    action_kind = payload.get('action')
    text = payload.get('text')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= applicant_browser.MAX_ACTION_TEXT:
        raise KernelError('INVALID_ACTION_TEXT', '待发正文需为 1-500 字。', 400)
    try:
        target = applicant_action.target_key(job_id=payload.get('job_id'),
                                             company=payload.get('company'),
                                             conversation_key=payload.get('conversation_key'))
    except ValueError:
        raise KernelError('INVALID_ACTION_REQUEST', '目标必须包含岗位与公司。', 400) from None
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        profile = store.runtime_document(c, context['scope_id'], 'profile')
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
    if not profile['revision'] or not plan['revision']:
        raise KernelError('APPLICANT_SETUP_REQUIRED', '请先确认保存本人资料和求职方案。', 409)
    try:
        doc = applicant_action.authorization(
            scope_id=context['scope_id'], action=action_kind,
            account_evidence=payload.get('account_evidence'), target=target,
            profile_revision=profile['revision'], plan_revision=plan['revision'],
            ttl_seconds=payload.get('ttl_seconds', applicant_action.AUTHORIZATION_TTL_SECONDS))
    except ValueError as exc:
        raise KernelError(str(exc), '授权参数不合法。', 400) from None
    doc['body_sha256'] = hashlib.sha256(text.strip().encode('utf-8')).hexdigest()
    doc['body_length'] = len(text.strip())
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        saved = _action_write(c, context, ACTION_AUTH_KIND, doc)
        return record_commit(c, context, 'runtime.applicant.action.authorize', {
            'status': 'authorized', 'authorization': applicant_action.public(saved['content']),
            'sending_enabled': False, 'delivery_confirmed': False,
            'note': '这份授权只能用一次，且只对已核对的目标和资料版本有效。',
        })


def applicant_action_execute(payload, context):
    """执行一次填入选提交并只读观察；结果落在六态之一，不做自动重试。"""
    applicant_only(context)
    if set(payload) != {'action', 'text', 'confirmed'} or payload.get('confirmed') is not True:
        raise KernelError('INVALID_ACTION_REQUEST', '需要明确确认后才执行一次动作。', 400)
    text = payload.get('text')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= applicant_browser.MAX_ACTION_TEXT:
        raise KernelError('INVALID_ACTION_TEXT', '待发正文需为 1-500 字。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        auth = _action_doc(c, context, ACTION_AUTH_KIND)
        profile = store.runtime_document(c, context['scope_id'], 'profile')
        plan = store.runtime_document(c, context['scope_id'], 'applicant_plan')
    if not auth['revision'] or not isinstance(auth['content'], dict):
        raise KernelError('CHANNEL_NOT_AUTHORIZED', ACTION_REFUSAL['CHANNEL_NOT_AUTHORIZED'], 403)
    doc = auth['content']
    target = applicant_action.target_key(job_id=CONTROLLED_TARGET['job_id'],
                                         company=CONTROLLED_TARGET['company'])
    code = applicant_action.check(doc, action=payload['action'], target=target,
                                  profile_revision=profile['revision'],
                                  plan_revision=plan['revision'])
    if code:
        raise KernelError(code, ACTION_REFUSAL.get(code, '渠道授权不可用。'), 403)
    if hashlib.sha256(text.strip().encode('utf-8')).hexdigest() != doc.get('body_sha256'):
        raise KernelError('ACTION_BODY_CHANGED', '正文与登记授权时不一致，请重新登记。', 409)
    # 提交前最后一次核对：页面上的目标必须仍是授权里的那个
    page = applicant_browser.controlled_target()
    if (page.get('job_id') != CONTROLLED_TARGET['job_id']
            or page.get('company') != CONTROLLED_TARGET['company']):
        raise KernelError('CHANNEL_TARGET_CHANGED',
                          '页面上的目标与授权不一致，未执行。', 409)
    consumed = applicant_action.consume(doc)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        reserve_quota(c, context, CONTROLLED_TARGET['job_id'], CONTROLLED_TARGET['company'])
        if not _action_claim(c, context, auth['revision'], consumed):
            raise KernelError('CHANNEL_AUTHORIZATION_TAKEN',
                              '这份授权已被另一个动作领走，未执行。', 409)

    attempted = filled = submitted = observed = page_lost = False
    observed_text = None
    try:
        with sandbox.action_grant(consumed, context):
            applicant_browser.controlled_open()
            baseline = applicant_browser.controlled_observe()
            base_count = int(baseline.get('count') or 0)
            filled = applicant_browser.controlled_fill(text).get('status') == 'filled'
            submitted = applicant_browser.controlled_submit().get('status') == 'submitted'
            # 有界轮询：回显是异步追加的，单次读会误判成「已提交无回显」
            seen = baseline
            deadline = time.monotonic() + OBSERVE_SECONDS
            while time.monotonic() < deadline:
                seen = applicant_browser.controlled_observe()
                page_lost = seen.get('crashed') is True
                if page_lost or int(seen.get('count') or 0) > base_count:
                    break
                time.sleep(OBSERVE_INTERVAL_SECONDS)
            observed_text = seen.get('last')
            observed = int(seen.get('count') or 0) > base_count and observed_text == text.strip()
    except sandbox.KernelError as exc:
        if not exc.outcome_unknown:
            raise
        result = applicant_action.outcome(attempted=True, filled=filled, submitted=submitted,
                                          observed_change=False)
        result.update({'state': 'outcome_unknown', 'reason': exc.code, 'needs_human': True,
                       'retryable': False, 'consumes_budget': True})
    else:
        if page_lost and submitted:
            result = applicant_action.outcome(attempted=True, filled=filled, submitted=True,
                                              observed_change=False)
            result.update({'state': 'outcome_unknown', 'reason': 'page_lost_after_submit',
                           'needs_human': True, 'retryable': False, 'consumes_budget': True})
        else:
            result = applicant_action.outcome(attempted=True, filled=filled, submitted=submitted,
                                              observed_change=observed, expected_text=text.strip(),
                                              observed_text=observed_text)
    result['action'] = payload['action']
    result['authorization_used'] = consumed.get('used')
    result['note'] = '受控页面通道；新回显不等于平台送达，更不等于对方已读。'
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        _action_write(c, context, ACTION_RESULT_KIND, result)
        _attempts_append(c, context, applicant_greeting.attempt(
            account_id=context['scope_id'], job_id=CONTROLLED_TARGET['job_id'],
            company=CONTROLLED_TARGET['company'], state=result['state']),
            replace_reserved=True)
    return result


def applicant_action_get(payload, context):
    """只读：最近一次动作结果与授权轮廓。"""
    applicant_only(context)
    if set(payload) - {'include_authorization'}:
        raise KernelError('INVALID_ACTION_REQUEST', '读取参数不受支持。', 400)
    with store.runtime_db() as c:
        check_context(c, context['context_token'])
        auth = _action_doc(c, context, ACTION_AUTH_KIND)
        result = _action_doc(c, context, ACTION_RESULT_KIND)
    view = {
        'status': 'not_started' if not result['revision'] else 'reported',
        'result': result['content'] if result['revision'] else None,
        'sending_enabled': False, 'delivery_confirmed': False,
        'channel': 'controlled',
    }
    if payload.get('include_authorization'):
        view['authorization'] = applicant_action.public(auth['content']) if auth['revision'] else None
    return view


def applicant_greeting_plan(payload, context):
    """把有界搜索的候选过一次配额/去重闸门，只输出「可以进入执行协议」的前缀。"""
    applicant_only(context)
    if set(payload) - {'configured_limit', 'platform_limit'}:
        raise KernelError('INVALID_GREETING_PLAN', '规划参数不受支持。', 400)
    configured = payload.get('configured_limit', applicant_greeting.DAILY_LIMIT)
    platform_limit = payload.get('platform_limit')
    for value in (configured, platform_limit):
        if value is not None and (type(value) is not int or value < 0):
            raise KernelError('INVALID_GREETING_PLAN', '额度必须是 0 或正整数。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        profile = store.runtime_document(c, context['scope_id'], 'profile')
        plan_doc = store.runtime_document(c, context['scope_id'], 'applicant_plan')
        search = store.runtime_document(c, context['scope_id'], JOB_SEARCH_KIND)
        attempts, _ = _attempts_read(c, context)
    if not profile['revision'] or not plan_doc['revision']:
        raise KernelError('APPLICANT_SETUP_REQUIRED', '请先确认保存本人资料和求职方案。', 409)
    content = search['content'] if isinstance(search['content'], dict) else {}
    jobs = [job for job in (content.get('jobs') or ()) if isinstance(job, dict)]
    candidates = [{'job_id': job.get('job_id'), 'company': job.get('company'),
                   'job_open': job.get('job_open') is not False, 'page_restricted': False,
                   'profile_revision': content.get('profile_revision'),
                   'plan_revision': content.get('plan_revision')} for job in jobs]
    now = time.time()
    settings = {'configured': configured, 'platform_limit': platform_limit,
                'platform_limit_source': 'manual' if platform_limit is not None else None,
                'platform_limit_expires_at': (now + PLATFORM_LIMIT_TTL_SECONDS)
                if platform_limit is not None else None,
                'updated_at': now}
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        existing = store.runtime_document(c, context['scope_id'], GREETING_SETTINGS_KIND)
        c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
                  "DO UPDATE SET revision=excluded.revision, content=excluded.content",
                  (context['scope_id'], GREETING_SETTINGS_KIND, existing['revision'] + 1,
                   json.dumps(settings, ensure_ascii=False)))
    day = applicant_greeting.day_key()
    result = applicant_greeting.plan(
        candidates, records=attempts, day=day, account_id=context['scope_id'],
        configured=configured, platform_limit=platform_limit,
        profile_revision=profile['revision'], plan_revision=plan_doc['revision'])
    result['candidates_total'] = len(candidates)
    result['source'] = 'bounded_browser_search' if candidates else 'no_candidates'
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        return record_commit(c, context, 'runtime.applicant.greeting.plan', result)


def applicant_chat_observe(payload, context):
    """只读观察当前真实对话面板：输入框是否就绪、发送是否可用、送达/已读标记。

    这是**读**，不是发。返回的 delivered_marks/read_marks 只陈述「平台回显存在该标记」，
    不推导投递成功——delivery_confirmed 恒 false。
    """
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_REQUEST', '读取对话面板不接受参数。', 400)
    result = applicant_browser.real_observe()
    result['source'] = 'real_chat_page'
    return result


def applicant_chat_target(payload, context):
    """只读：读当前打开对话的对方身份（名字/公司/职位）。不点击、不输入。"""
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_REQUEST', '读取对话身份不接受参数。', 400)
    result = applicant_browser.real_target()
    result['source'] = 'real_chat_page'
    return result


def applicant_chat_read(payload, context):
    """只读：读输入框内容与发送按钮状态。"""
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_REQUEST', '读取输入框不接受参数。', 400)
    return applicant_browser.real_read_composer()


def applicant_chat_fill(payload, context):
    """把正文填进真实输入框。**未发送**，所以是可回撤的本地动作。"""
    applicant_only(context)
    if set(payload) != {'text'}:
        raise KernelError('INVALID_ACTION_REQUEST', '需要且只需要待发正文。', 400)
    return applicant_browser.real_fill(payload['text'])


CONVERSATION_AUTH_KIND = 'conversation_authorization'


def _document_items(c, scope_id, kind):
    """读一份 runtime_document 的条目与版本（文档不存在时返回 `([], 0)`）。"""
    doc = store.runtime_document(c, scope_id, kind)
    content = doc['content'] if isinstance(doc['content'], dict) else {}
    items = [item for item in (content.get('items') or []) if isinstance(item, dict)]
    return items, doc['revision']


def _ledger_read(c, context):
    """读当前工作区的**活跃**发送台账（写入用；归档文档不参与写入）。"""
    return _document_items(c, context['scope_id'], applicant_send_ledger.LEDGER_KIND)


def _ledger_archive_read(c, context):
    """读当前工作区的**归档**文档（写入用；与活跃台账分开存、同一张 runtime_documents 表）。"""
    return _document_items(c, context['scope_id'], applicant_send_ledger.ARCHIVE_KIND)


def _ledger_items_by_kind(c, kinds):
    """按 kind 汇总条目（只读判断用）。"""
    rows = c.execute("SELECT content FROM runtime_documents WHERE kind IN (%s)"
                     % ','.join('?' * len(kinds)), tuple(kinds)).fetchall()
    items = []
    for (content,) in rows:
        doc = json.loads(content) if isinstance(content, str) else content
        for item in (doc.get('items') or []) if isinstance(doc, dict) else ():
            if isinstance(item, dict):
                items.append(item)
    return items


def _ledger_read_active(c):
    """跨工作区的活跃台账条目（容量告警看这一份：上限只管活跃文档）。"""
    return _ledger_items_by_kind(c, (applicant_send_ledger.LEDGER_KIND,))


def _ledger_read_archive(c):
    """跨工作区的归档条目（已联系证据的落地处）。"""
    return _ledger_items_by_kind(c, (applicant_send_ledger.ARCHIVE_KIND,))


def _ledger_read_all(c):
    """跨工作区汇总**活跃台账 + 归档**：所有「联系过没有 / 还没闭环」的判断只看这个。

    换一个账号/组织就是新工作区、台账为空，同一真人能被再发一次。
    方案验收矩阵要求「跨日/跨 scope 不绕过」，所以已联系证据与未闭环判断都看全库。

    归档同理：已联系证据搬进 `applicant_send_ledger_archive` 之后，这里必须照样读出来。
    只读活跃文档 = 归档一次就把「这个对象联系过」擦掉，同一对象立刻能再发一次 ——
    那比不归档更糟。两个文档按 `ledger_id` 去重，同 id 以先出现的（活跃）那条为准。
    """
    items, seen = [], set()
    for item in _ledger_items_by_kind(c, (applicant_send_ledger.LEDGER_KIND,
                                          applicant_send_ledger.ARCHIVE_KIND)):
        key = item.get('ledger_id')
        if isinstance(key, str):
            if key in seen:
                continue
            seen.add(key)
        items.append(item)
    return items


def _attempts_read_all(c):
    """跨工作区汇总招呼尝试：额度按人计，不按工作区计。"""
    rows = c.execute("SELECT content FROM runtime_documents WHERE kind=?",
                     (GREETING_ATTEMPTS_KIND,)).fetchall()
    items = []
    for (content,) in rows:
        doc = json.loads(content) if isinstance(content, str) else content
        for item in (doc.get('items') or []) if isinstance(doc, dict) else ():
            if isinstance(item, dict):
                items.append(item)
    return items


def _ledger_write(c, context, kind, items, revision, meta):
    """写一份台账类文档（活跃台账 / 归档文档共用），乐观版本 +1。调用方须已在写事务内。"""
    c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
              "DO UPDATE SET revision=excluded.revision,content=excluded.content",
              (context['scope_id'], kind, revision + 1,
               json.dumps({'items': list(items), 'meta': meta}, ensure_ascii=False)))


def _ledger_merge(c, context, entry):
    """把一条台账记录写回（同 ledger_id 原地替换）。调用方须已在写事务内。

    容量策略在 applicant_send_ledger.merge 里：阻塞/已联系证据永不淘汰，只有
    resolved_not_delivered 这类流水按上限淘汰。保护集超限时不删，把 overflow
    标记写进文档 meta，让上层能报警/要求人工归档 —— 否则闸门会随容量悄悄失效。
    归档只搬「已联系证据」、只搬出活跃文档，所以这里读写仍是活跃文档，不会把
    已归档的条目又拉回来。
    """
    items, revision = _ledger_read(c, context)
    merged = applicant_send_ledger.merge(items, entry)
    _ledger_write(c, context, applicant_send_ledger.LEDGER_KIND, list(merged),
                  revision, merged.report())
    return merged


def _greeting_job_key(target, target_sha256):
    """招呼额度里的对象键：优先真实 job_id，读不到就用目标指纹，保证同一对象同一键。"""
    job_id = (target or {}).get('job_id')
    if isinstance(job_id, str) and job_id.strip():
        return job_id.strip()
    return 'target:' + (target_sha256 or '')[:24]


# 台账状态 → 招呼额度记录状态（两套名字不同但语义一致；not_delivered 不占额度）
LEDGER_TO_GREETING = {
    applicant_send_ledger.STATE_SUBMITTED: 'submitted_unverified',
    applicant_send_ledger.STATE_OBSERVED: 'observed_reply',
    applicant_send_ledger.STATE_UNKNOWN: 'outcome_unknown',
    applicant_send_ledger.STATE_CONFLICT: 'conflict',
    applicant_send_ledger.STATE_NOT_DELIVERED: 'not_executed',
}


def _replace_greeting_attempt(c, context, job_key, ledger_state, day):
    """把当天该对象的占位记录换成真实结果，避免占位与结果双计额度。"""
    state = LEDGER_TO_GREETING.get(ledger_state)
    if state is None:
        return False
    items, revision = _attempts_read(c, context)
    for index, item in enumerate(items):
        if (item.get('job_id') == job_key and item.get('day') == day
                and item.get('state') == applicant_greeting.RESERVED_STATE):
            items[index] = dict(item, state=state)
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
                      "DO UPDATE SET revision=excluded.revision,content=excluded.content",
                      (context['scope_id'], GREETING_ATTEMPTS_KIND, revision + 1,
                       json.dumps({'items': items}, ensure_ascii=False)))
            return True
    return False


def _ledger_close_entry(context, entry, state, *, job_key=None, by=None, note=None):
    """写台账结果；有对应招呼占位时同事务换掉。点击已发出，写失败不能静默吞掉。"""
    closed = applicant_send_ledger.close(entry, state, by=by, note=note)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        _ledger_merge(c, context, closed)
        if job_key:
            _replace_greeting_attempt(c, context, job_key, state, closed.get('day'))
    return closed



def _conversation_claim(c, context, revision, consumed):
    # 乐观锁：两份并发都读到 ready 时，只有一个 rowcount==1
    cur = c.execute(
        "UPDATE runtime_documents SET revision=revision+1, content=? "
        "WHERE scope_id=? AND kind=? AND revision=?",
        (json.dumps(consumed, ensure_ascii=False), context['scope_id'],
         CONVERSATION_AUTH_KIND, revision))
    return cur.rowcount == 1


def applicant_chat_submit(payload, context):
    # 真发。细校验全在这里：目标重读、正文比对、资料版本、一次性领取。
    applicant_only(context)
    if set(payload) != {'point'}:
        raise KernelError('INVALID_ACTION_REQUEST', '需要发送按钮位置。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        document = store.runtime_document(c, context['scope_id'], CONVERSATION_AUTH_KIND)
        profile_revision = store.runtime_document(c, context['scope_id'], 'profile')['revision']
        plan_revision = store.runtime_document(c, context['scope_id'], 'applicant_plan')['revision']
    if not document['revision'] or not isinstance(document['content'], dict):
        raise KernelError('CONVERSATION_NOT_AUTHORIZED',
                          applicant_conversation.CODES['CONVERSATION_NOT_AUTHORIZED'], 403)
    authorized = document['content']
    # 方案第131行：没有可证明的稳定对象和动作结果证据时，保留人工发送。
    # 这是证据门而不是死开关 —— DOM 能读到平台会话 ID / 岗位 ID 就自动放行。
    target_doc = authorized.get('target') if isinstance(authorized.get('target'), dict) else {}
    stable_id = (target_doc.get('job_id') or authorized.get('conversation_id')
                 or authorized.get('session_id') or '')
    if not (isinstance(stable_id, str) and stable_id.strip()):
        raise KernelError('SEND_STABLE_TARGET_REQUIRED',
                          applicant_send_ledger.CODES['SEND_STABLE_TARGET_REQUIRED'], 409)
    # 正文以输入框真实内容为准，不信调用方传参
    composer = applicant_browser.real_read_composer()
    text = (composer.get('text') or '').strip() if isinstance(composer, dict) else ''
    live = applicant_browser.real_target()
    if not isinstance(live, dict) or live.get('status') != 'target_read':
        code = 'CONVERSATION_TARGET_UNREADABLE'
        raise KernelError(code, applicant_conversation.CODES[code], 409)
    snapshot = applicant_conversation.target_snapshot(
        name=live['name'], company=live['company'], position=live.get('position'),
        job_id=live.get('job_id'))
    code = applicant_conversation.check(authorized, target=snapshot, text=text,
                                       profile_revision=profile_revision,
                                       plan_revision=plan_revision)
    if code:
        raise KernelError(code, applicant_conversation.CODES.get(code, '授权核对未通过。'), 403)
    consumed = applicant_conversation.consume(authorized)
    action = authorized.get('action', 'reply')
    target_sha = authorized.get('target_sha256')
    body_sha = authorized.get('body_sha256')
    job_key = _greeting_job_key(authorized.get('target'), target_sha)
    # 点击前基线：读不到就留空，后续核对会停在「已提交未核对」，不冒充已送达
    try:
        panel = applicant_browser.real_observe('runtime.applicant.chat.submit')
        baseline = panel.get('delivered_marks') if isinstance(panel, dict) else None
    except Exception:
        baseline = None
    entry = applicant_send_ledger.new_entry(
        scope_id=context['scope_id'], target_sha256=target_sha, body_sha256=body_sha,
        action=action)
    entry['marks_before'] = baseline
    entry['job_key'] = job_key
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        if not _conversation_claim(c, context, document['revision'], consumed):
            raise KernelError('CONVERSATION_CONFIRMATION_TAKEN',
                              '这份授权已被另一个动作领走，未执行。', 409)
        # 纵深防御：这条路不经过 authorize，自己也要挡一遍已联系/未闭环的同对象
        evidence_kind, _ = applicant_send_ledger.target_evidence(
            _ledger_read_all(c), target_sha)
        if evidence_kind == 'contacted':
            raise KernelError('SEND_TARGET_ALREADY_CONTACTED',
                              applicant_send_ledger.CODES['SEND_TARGET_ALREADY_CONTACTED'], 409)
        if evidence_kind == 'unresolved':
            raise KernelError('SEND_TARGET_UNRESOLVED',
                              applicant_send_ledger.CODES['SEND_TARGET_UNRESOLVED'], 409)
        # 招呼额度在同一事务里占位：否则并发下两个进程各自读到 used=149 会双双放行
        if action == 'greeting':
            reserve_quota(c, context, job_key,
                          (authorized.get('target') or {}).get('company') or '')
        _ledger_merge(c, context, entry)
    try:
        result = applicant_browser.real_submit(payload['point'])
    except Exception:
        # 点击可能已发出：先记账再抛，绝不能让这次尝试变成「没发生过」
        _ledger_close_entry(context, entry, applicant_send_ledger.STATE_UNKNOWN, job_key=job_key)
        raise
    status = result.get('status') if isinstance(result, dict) else None
    if status != 'submit_requested':
        # 页面没连上/按钮没点下去：一次点击都没发生，不能记成「已提交未核对」去假阻塞
        _ledger_close_entry(context, entry, applicant_send_ledger.STATE_NOT_DELIVERED,
                            job_key=job_key, by='real_submit:%s' % (status or 'no_status'))
        return dict(result if isinstance(result, dict) else {}, ledger_id=entry['ledger_id'],
                    action=action, ledger_state=applicant_send_ledger.STATE_NOT_DELIVERED,
                    needs_outcome_check=False)
    _ledger_close_entry(context, entry, applicant_send_ledger.STATE_SUBMITTED, job_key=job_key)
    return dict(result, ledger_id=entry['ledger_id'], action=action,
                ledger_state=applicant_send_ledger.STATE_SUBMITTED,
                needs_outcome_check=True)


def applicant_chat_authorize(payload, context):
    # 登记一次性发送授权：读真实身份 -> 人工确认 -> 授权落库。此步不发送。
    applicant_only(context)
    if set(payload) - {'text', 'confirmed_by', 'action'} or not {'text', 'confirmed_by'} <= set(payload):
        raise KernelError('INVALID_ACTION_REQUEST', '需要待发正文与确认人，动作类型可选。', 400)
    action = payload.get('action', 'reply')
    if action not in ('greeting', 'reply'):
        raise KernelError('INVALID_ACTION_REQUEST', '动作类型只能是 greeting 或 reply。', 400)
    text = payload['text']
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= applicant_conversation.BODY_MAX:
        raise KernelError('INVALID_ACTION_TEXT', '待发正文需为 1-500 字。', 400)
    live = applicant_browser.real_target()
    if not isinstance(live, dict) or live.get('status') != 'target_read':
        code = 'CONVERSATION_TARGET_UNREADABLE'
        raise KernelError(code, applicant_conversation.CODES[code], 409)
    snapshot = applicant_conversation.target_snapshot(
        name=live['name'], company=live['company'], position=live.get('position'),
        job_id=live.get('job_id'))
    try:
        confirmation = applicant_conversation.confirmation_required(
            snapshot, by=payload['confirmed_by'])
    except ValueError as exc:
        raise KernelError('INVALID_ACTION_REQUEST', '确认人无效。', 400) from None
    target_sha = applicant_conversation.fingerprint(snapshot)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        profile_revision = store.runtime_document(c, context['scope_id'], 'profile')['revision']
        plan_revision = store.runtime_document(c, context['scope_id'], 'applicant_plan')['revision']
        ledger_items = _ledger_read_all(c)
    if not profile_revision or not plan_revision:
        raise KernelError('APPLICANT_SETUP_REQUIRED', '请先确认保存本人资料和求职方案。', 409)
    # 同一对象不发第二次：已联系是永久证据；未闭环先人工核对（跨天也不放行）
    evidence, _found = applicant_send_ledger.target_evidence(ledger_items, target_sha)
    if evidence == 'contacted':
        raise KernelError('SEND_TARGET_ALREADY_CONTACTED',
                          applicant_send_ledger.CODES['SEND_TARGET_ALREADY_CONTACTED'], 409)
    if evidence == 'unresolved':
        raise KernelError('SEND_TARGET_UNRESOLVED',
                          applicant_send_ledger.CODES['SEND_TARGET_UNRESOLVED'], 409)
    try:
        document = applicant_conversation.authorization(
            scope_id=context['scope_id'], target=snapshot, confirmation=confirmation,
            text=text, profile_revision=profile_revision, plan_revision=plan_revision)
    except ValueError as exc:
        code = str(exc)
        raise KernelError(code if code in applicant_conversation.CODES else
                          'CONVERSATION_CONFIRMATION_INVALID',
                          applicant_conversation.CODES.get(code, '确认凭证无效。'), 403) from None
    document['action'] = action
    result = {'status': 'authorized', 'target': snapshot, 'action': action,
              'target_sha256': document['target_sha256'],
              'body_sha256': document['body_sha256'],
              'confirmation_by': document['confirmation_by'],
              'expires_in': applicant_conversation.AUTHORIZATION_TTL_SECONDS,
              'max_uses': document['max_uses'],
              'sending_enabled': False, 'delivery_confirmed': False}
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        existing = store.runtime_document(c, context['scope_id'], CONVERSATION_AUTH_KIND)
        c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                  "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision, "
                  "content=excluded.content",
                  (context['scope_id'], CONVERSATION_AUTH_KIND, existing['revision'] + 1,
                   json.dumps(document, ensure_ascii=False)))
        return record_commit(c, context, 'runtime.applicant.chat.authorize', result)


def applicant_chat_outcome_get(payload, context):
    """看当前还没核对清楚的发送记录（只读）。

    `capacity` 只算**活跃台账**（5000 上限管的就是这份文档）；归档条数单独给在
    `archive` 里。`blocking` 仍然来自活跃 ∪ 归档 —— 归档里万一混进阻塞态也必须看得见。
    """
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_BROWSER_REQUEST', '读取发送台账不接受参数。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        items = _ledger_read_all(c)
        active = _ledger_read_active(c)
        archived = _ledger_read_archive(c)
    blocking = applicant_send_ledger.blocking_items(items)
    return {'blocking': blocking, 'blockers_count': len(blocking),
            'summary': applicant_send_ledger.summary(items, scope_id=context['scope_id']),
            'capacity': applicant_send_ledger.capacity(active),
            'archive': applicant_send_ledger.archive_report(archived),
            'sending_enabled': False, 'delivery_confirmed': False}


def applicant_chat_outcome_archive(payload, context):
    """把已联系证据搬进同库的归档文档，给活跃台账腾出保护集额度。不删除任何证据。

    三条底线：
    1. **搬走不等于忘掉**：`_ledger_read_all` 读活跃 ∪ 归档，所以归档后
       `chat.authorize` / `chat.submit` 照样报 `SEND_TARGET_ALREADY_CONTACTED`。
    2. **只搬已联系证据**：未闭环（含 `submitting`）一律拒绝并给错误码 —— 搬走阻塞态
       等于把闸门关掉，比不归档更糟。
    3. **幂等 + 可恢复**：同一 `ledger_id` 反复归档只有一条；动作本身走
       `RECEIPTED_CONTROLS` 提交回执，崩溃恢复时从两个文档重读核对，不重放。
    """
    applicant_only(context)
    allowed = {'ledger_ids', 'target_sha256', 'confirmed_by'}
    if set(payload) - allowed or 'confirmed_by' not in payload:
        raise KernelError('INVALID_ACTION_REQUEST',
                          applicant_send_ledger.CODES['SEND_LEDGER_ARCHIVE_INVALID'], 400)
    by = payload['confirmed_by']
    if not isinstance(by, str) or not by.strip():
        raise KernelError('INVALID_ACTION_REQUEST', '归档必须留下操作人。', 400)
    ledger_ids = payload.get('ledger_ids')
    if ledger_ids is not None and (not isinstance(ledger_ids, list) or not ledger_ids
                                   or not all(isinstance(i, str) and i.strip() for i in ledger_ids)):
        raise KernelError('INVALID_ACTION_REQUEST',
                          applicant_send_ledger.CODES['SEND_LEDGER_ARCHIVE_INVALID'], 400)
    target_sha = payload.get('target_sha256')
    if target_sha is not None and (not isinstance(target_sha, str) or len(target_sha) != 64):
        raise KernelError('INVALID_ACTION_REQUEST',
                          applicant_send_ledger.CODES['SEND_LEDGER_ARCHIVE_INVALID'], 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        active, active_revision = _ledger_read(c, context)
        archived, archive_revision = _ledger_archive_read(c, context)
        archived_ids = {item.get('ledger_id') for item in archived}
        if ledger_ids is not None:
            unknown = set(ledger_ids) - {item.get('ledger_id') for item in active} - archived_ids
            if unknown:
                raise KernelError('SEND_LEDGER_ENTRY_NOT_FOUND',
                                  applicant_send_ledger.CODES['SEND_LEDGER_ENTRY_NOT_FOUND'], 404)
        try:
            moved, kept = applicant_send_ledger.plan_archive(
                active, ledger_ids=ledger_ids, target_sha256=target_sha,
                scope_id=context['scope_id'])
        except ValueError as exc:
            code = str(exc)
            raise KernelError(code if code in applicant_send_ledger.CODES else 'INVALID_ACTION_REQUEST',
                              applicant_send_ledger.CODES.get(code, '归档请求无效。'), 409) from None
        if moved:
            merged = applicant_send_ledger.merge_archive(archived, moved)
            remaining = applicant_send_ledger.MergeResult(kept)
            _ledger_write(c, context, applicant_send_ledger.LEDGER_KIND, kept,
                          active_revision, remaining.report())
            _ledger_write(c, context, applicant_send_ledger.ARCHIVE_KIND, merged,
                          archive_revision, applicant_send_ledger.archive_report(merged))
            already = []
        else:
            merged, kept = archived, active
            wanted = set(ledger_ids) if ledger_ids is not None else None
            already = []
            for item in archived:
                if wanted is not None:
                    hit = item.get('ledger_id') in wanted
                else:
                    hit = target_sha is not None and item.get('target_sha256') == target_sha
                if hit:
                    already.append(item.get('ledger_id'))
        result = {'status': 'archived' if moved else ('already_archived' if already
                                                      else 'nothing_to_archive'),
                  'archived': [item.get('ledger_id') for item in moved],
                  'archived_count': len(moved),
                  'already_archived': [i for i in already if isinstance(i, str)],
                  'active_count': len(kept),
                  'archive_total': len(merged),
                  'capacity': applicant_send_ledger.capacity(kept),
                  'archive': applicant_send_ledger.archive_report(merged),
                  'archived_by': by.strip(),
                  'sending_enabled': False, 'delivery_confirmed': False}
        return record_commit(c, context, 'runtime.applicant.chat.outcome.archive', result)


def applicant_chat_outcome_verify(payload, context):
    """只读核对：用页面当前回显与点击前基线比对，推进台账状态。

    只分三档：有新增回显 / 无新增回显 / 读不到。不推断送达，不推断已读。
    """
    applicant_only(context)
    if set(payload) - {'ledger_id'}:
        raise KernelError('INVALID_BROWSER_REQUEST', '核对参数不受支持。', 400)
    panel = applicant_browser.real_observe('runtime.applicant.chat.outcome.verify')
    marks_after = panel.get('delivered_marks') if isinstance(panel, dict) else None
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        items = _ledger_read_all(c)
    blocking = applicant_send_ledger.blocking_items(items)
    if payload.get('ledger_id'):
        blocking = [i for i in blocking if i.get('ledger_id') == payload['ledger_id']]
    if not blocking:
        raise KernelError('SEND_LEDGER_ENTRY_NOT_FOUND',
                          applicant_send_ledger.CODES['SEND_LEDGER_ENTRY_NOT_FOUND'], 404)
    entry = blocking[0]
    # 面板必须还停在这条记录的对象上：否则别人的回显会被记成这一条的，误判成已联系
    if entry.get('target_sha256'):
        try:
            live = applicant_browser.real_target()
        except Exception:
            live = None
        if not isinstance(live, dict) or live.get('status') != 'target_read':
            raise KernelError('CONVERSATION_TARGET_UNREADABLE',
                              applicant_conversation.CODES['CONVERSATION_TARGET_UNREADABLE'], 409)
        current = applicant_conversation.target_snapshot(
            name=live['name'], company=live['company'], position=live.get('position'),
            job_id=live.get('job_id'))
        if applicant_conversation.fingerprint(current) != entry['target_sha256']:
            raise KernelError('SEND_VERIFY_TARGET_MISMATCH',
                              '当前打开的对话不是这条记录的对象，先切回那个对话再核对。', 409)
    state, why = applicant_send_ledger.observed_state(
        before_marks=entry.get('marks_before'), after_marks=marks_after,
        error=not isinstance(panel, dict) or panel.get('status') != 'observed')
    closed = _ledger_close_entry(context, entry, state, job_key=entry.get('job_key'))
    return {'ledger_id': closed['ledger_id'], 'state': closed['state'], 'reason': why,
            'marks_before': entry.get('marks_before'), 'marks_after': marks_after,
            'needs_human': state in applicant_send_ledger.BLOCKING_STATES,
            'sending_enabled': False, 'delivery_confirmed': False}


def applicant_chat_outcome_resolve(payload, context):
    """人工核对结论落库：delivered / not_delivered / duplicate_confirmed。

    这是「未知结果」的唯一解除路径；必须留核对人，且不重放任何提交。
    """
    applicant_only(context)
    allowed = {'ledger_id', 'resolution', 'confirmed_by', 'note'}
    if set(payload) - allowed or not {'ledger_id', 'resolution', 'confirmed_by'} <= set(payload):
        raise KernelError('INVALID_ACTION_REQUEST', '需要台账条目、核对结论与核对人。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        items = _ledger_read_all(c)
        entry = next((i for i in items if i.get('ledger_id') == payload['ledger_id']), None)
        if entry is None:
            raise KernelError('SEND_LEDGER_ENTRY_NOT_FOUND',
                              applicant_send_ledger.CODES['SEND_LEDGER_ENTRY_NOT_FOUND'], 404)
        try:
            closed = applicant_send_ledger.resolve(
                entry, payload['resolution'], by=payload['confirmed_by'],
                note=payload.get('note'))
        except ValueError as exc:
            code = str(exc)
            raise KernelError(code if code in applicant_send_ledger.CODES else 'INVALID_ACTION_REQUEST',
                              applicant_send_ledger.CODES.get(code, '核对结论无效。'), 400) from None
        _ledger_merge(c, context, closed)
        if closed.get('job_key'):
            _replace_greeting_attempt(c, context, closed['job_key'],
                                      closed['state'], closed.get('day'))
        return record_commit(c, context, 'runtime.applicant.chat.outcome.resolve', closed)


def _run_state_documents(c):
    """跨工作区读运行开关（只读判断用：换账号不能绕过暂停）。"""
    rows = c.execute("SELECT content FROM runtime_documents WHERE kind=?",
                     (run_state.KIND,)).fetchall()
    documents = []
    for (content,) in rows:
        documents.append(json.loads(content) if isinstance(content, str) else content)
    return documents


def _run_state_write(c, context, document):
    doc = store.runtime_document(c, context['scope_id'], run_state.KIND)
    c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) ON CONFLICT(scope_id,kind) "
              "DO UPDATE SET revision=excluded.revision,content=excluded.content",
              (context['scope_id'], run_state.KIND, doc['revision'] + 1,
               json.dumps(document, ensure_ascii=False)))
    return document


def applicant_run_get(payload, context):
    """看运行开关与待核对条数（只读）。"""
    applicant_only(context)
    if payload:
        raise KernelError('INVALID_ACTION_REQUEST', '读取运行开关不接受参数。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN')
        check_context(c, context['context_token'])
        documents = _run_state_documents(c)
        blockers = applicant_send_ledger.blocking_items(_ledger_read_all(c))
    paused = run_state.paused_actions(*documents)
    return {'paused_actions': paused, 'paused': bool(paused),
            'operations_paused': 'reply' in paused,
            'greeting_allowed': 'greeting' not in paused,
            'unresolved_count': len(blockers),
            'sending_enabled': False, 'automatic_execution': False}


def runtime_run_pause(payload, context):
    """暂停自动动作；默认只暂停自动回复，招呼照发。"""
    applicant_only(context)
    allowed = {'confirmed', 'reason', 'actions', 'paused_by'}
    if (set(payload) - allowed or payload.get('confirmed') is not True
            or not isinstance(payload.get('paused_by'), str) or not payload['paused_by'].strip()):
        raise KernelError('INVALID_ACTION_REQUEST', '暂停需要确认与操作人。', 400)
    actions = payload.get('actions') or list(run_state.DEFAULT_ACTIONS)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        doc = store.runtime_document(c, context['scope_id'], run_state.KIND)
        current = doc['content'] if isinstance(doc['content'], dict) and doc['revision'] else None
        try:
            updated = run_state.pause(current, by=payload['paused_by'],
                                      reason=payload.get('reason'), actions=actions)
        except ValueError as exc:
            raise KernelError(str(exc), run_state.CODES.get(str(exc), '暂停参数无效。'), 400) from None
        _run_state_write(c, context, updated)
        blockers = applicant_send_ledger.blocking_items(_ledger_read_all(c))
        result = dict(updated, unresolved_count=len(blockers),
                      sending_enabled=False, automatic_execution=False)
        return record_commit(c, context, 'runtime.run.pause', result)


def runtime_run_resume(payload, context):
    """恢复自动动作；不给 action 表示全部恢复。"""
    applicant_only(context)
    allowed = {'confirmed', 'action', 'paused_by', 'expected_generation'}
    if (set(payload) - allowed or payload.get('confirmed') is not True
            or not isinstance(payload.get('paused_by'), str) or not payload['paused_by'].strip()):
        raise KernelError('INVALID_ACTION_REQUEST', '恢复需要确认与操作人。', 400)
    expected = payload.get('expected_generation')
    if expected is not None and (type(expected) is not int or expected < 0):
        raise KernelError('INVALID_ACTION_REQUEST', '运行版本号无效。', 400)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        doc = store.runtime_document(c, context['scope_id'], run_state.KIND)
        current = doc['content'] if isinstance(doc['content'], dict) and doc['revision'] else None
        try:
            updated = run_state.resume(current, by=payload['paused_by'],
                                       action=payload.get('action'),
                                       expected_generation=expected)
        except ValueError as exc:
            raise KernelError(str(exc), run_state.CODES.get(str(exc), '恢复参数无效。'), 400) from None
        _run_state_write(c, context, updated)
        blockers = applicant_send_ledger.blocking_items(_ledger_read_all(c))
        result = dict(updated, unresolved_count=len(blockers),
                      sending_enabled=False, automatic_execution=False)
        return record_commit(c, context, 'runtime.run.resume', result)


def register(router, engine):
    router.register(Tool('runtime.applicant.jobs.search.open', applicant_search_open,
                         effect='local_ui', maintenance_only=True))
    router.register(Tool('runtime.applicant.jobs.search.step', applicant_search_step,
                         maintenance_only=True))
    router.register(Tool('runtime.applicant.jobs.search.get', applicant_search_get, effect='none'))
    router.register(Tool('runtime.applicant.jobs.search.stop', applicant_search_stop,
                         maintenance_only=True))
    router.register(Tool('runtime.applicant.jobs.search.reset', applicant_search_reset,
                         maintenance_only=True))
    # 详情页读取：导航 + 读正文，两步都在内核里；导航改变页面状态所以是 local_ui。
    router.register(Tool('runtime.applicant.jobs.detail', applicant_job_detail,
                         effect='local_ui', maintenance_only=True))
    # 批量补读详情：一次有界条数 + 总时长上限，导航副作用同 jobs.detail。
    router.register(Tool(DETAILS_TOOL, applicant_search_details,
                         effect='local_ui', maintenance_only=True))
    router.register(Tool('runtime.applicant.action.authorize', applicant_action_authorize,
                         maintenance_only=True))
    router.register(Tool('runtime.applicant.action.execute', applicant_action_execute,
                         effect='external_write', maintenance_only=True))
    router.register(Tool('runtime.applicant.action.get', applicant_action_get, effect='none'))
    router.register(Tool('runtime.applicant.greeting.plan', applicant_greeting_plan,
                         maintenance_only=True))
    router.register(Tool('runtime.applicant.chat.observe', applicant_chat_observe,
                         effect='none'))
    router.register(Tool('runtime.applicant.chat.target', applicant_chat_target,
                         effect='none'))
    router.register(Tool('runtime.applicant.chat.read', applicant_chat_read,
                         effect='none'))
    # 填入只在浏览器里落文本、没发出去，属可回撤的本地动作（同 jobs.search.open 口径）
    router.register(Tool('runtime.applicant.chat.fill', applicant_chat_fill,
                         effect='local_ui', maintenance_only=True))
    # 真发：不可逆外部动作，走 external_write 授权门
    router.register(Tool('runtime.applicant.chat.submit', applicant_chat_submit,
                         effect='external_write'))
    router.register(Tool('runtime.applicant.chat.authorize', applicant_chat_authorize))
    router.register(Tool('runtime.applicant.chat.outcome.get', applicant_chat_outcome_get,
                         effect='none'))
    # 只读核对：读页面回显推状态，不发任何东西
    router.register(Tool('runtime.applicant.chat.outcome.verify', applicant_chat_outcome_verify,
                         effect='none'))
    # 人工核对结论：唯一的未知结果解除路径，必须留核对人
    router.register(Tool('runtime.applicant.chat.outcome.resolve', applicant_chat_outcome_resolve))
    # 归档：把已联系证据搬进同库归档文档（不删、仍拦得住、未闭环拒收、走提交回执）
    router.register(Tool('runtime.applicant.chat.outcome.archive', applicant_chat_outcome_archive))
    router.register(Tool('runtime.run.get', applicant_run_get, effect='none'))
    router.register(Tool('runtime.run.pause', runtime_run_pause))
    router.register(Tool('runtime.run.resume', runtime_run_resume))
    router.register(Tool('runtime.applicant.send.handoff',send_handoff))
    router.register(Tool('runtime.applicant.send.handoff.get',send_handoff_get,effect='none'))
    router.register(Tool('runtime.applicant.send.manual.claim',send_manual_claim))
    router.register(Tool('runtime.applicant.send.queue.get',send_queue_get,effect='none'))
    router.register(Tool('runtime.applicant.send.queue.handoff',send_queue_handoff))
    router.register(Tool('runtime.applicant.send.recipient.confirm',send_recipient_confirm))
    router.register(Tool('runtime.applicant.send.recipient.get',send_recipient_get,effect='none'))
    router.register(Tool('runtime.applicant.send.reserve',send_reserve))
    router.register(Tool('runtime.applicant.send.release',send_release))
    router.register(Tool('runtime.applicant.send.get',send_get,effect='none'))
    router.register(Tool('runtime.applicant.send.readback',send_readback,effect='none'))
    router.register(Tool('runtime.applicant.send.readback.commit',send_readback_commit))
    router.register(Tool('runtime.applicant.send.stats',send_stats,effect='none'))
    router.register(Tool('runtime.applicant.reply.window.get',applicant_reply_window_get,effect='none'))
    router.register(Tool('runtime.applicant.reply.window.start',lambda p,c:applicant_reply_window_update(p,c)))
    router.register(Tool('runtime.applicant.reply.window.observe',lambda p,c:applicant_reply_window_update(p,c,advance=True)))
    router.register(Tool('runtime.applicant.reply.preflight',applicant_reply_preflight,effect='none'))
    router.register(Tool('runtime.applicant.reply.prepare',lambda payload,context:engine.submit_observed_reply(payload,context,observed_reply_source)))
    router.register(Tool('runtime.applicant.reply.get',applicant_reply_get,effect='none'))
    router.register(Tool('runtime.applicant.messages.read',lambda payload,context:applicant_conversations_read(payload,context,messages=True),effect='none'))
    router.register(Tool('runtime.applicant.conversation.open',applicant_conversation_open,effect='local_ui',maintenance_only=True))
    router.register(Tool('runtime.applicant.conversations.read',applicant_conversations_read,effect='none'))
    def applicant_chat_open(payload, context):
        applicant_only(context)
        if set(payload) != {'confirmed'} or payload.get('confirmed') is not True:
            raise KernelError('BROWSER_CONFIRMATION_REQUIRED', '请明确打开专用求职浏览器的沟通页面。', 400)
        return applicant_browser.open_chat()

    def applicant_chat_inspect(payload, context):
        applicant_only(context)
        if payload:
            raise KernelError('INVALID_BROWSER_REQUEST', '沟通页检查不接受地址或脚本。', 400)
        return applicant_browser.inspect_chat()

    router.register(Tool('runtime.applicant.chat.open', applicant_chat_open, effect='local_ui', maintenance_only=True))
    router.register(Tool('runtime.applicant.chat.inspect', applicant_chat_inspect, effect='none', maintenance_only=True))
    router.register(Tool('runtime.applicant.browser.binding.get', applicant_binding_get, effect='none'))
    router.register(Tool('runtime.applicant.browser.binding.confirm', applicant_binding_confirm))
    router.register(Tool('runtime.applicant.browser.binding.check', applicant_binding_check, effect='none'))
    router.register(Tool('runtime.applicant.salary.confirm', applicant_salary_confirm))
    router.register(Tool('runtime.applicant.jobs.confirmed.preview', applicant_confirmed_preview, effect='none'))
    def applicant_viewport_reset(payload, context):
        applicant_only(context)
        if set(payload) != {'confirmed'} or payload.get('confirmed') is not True:
            raise KernelError('BROWSER_CONFIRMATION_REQUIRED', '请明确确认恢复专用浏览器的异常显示尺寸。', 400)
        return applicant_browser.read_current_jobs(reset_viewport=True)

    router.register(Tool('runtime.applicant.browser.viewport.reset', applicant_viewport_reset, effect='local_ui', maintenance_only=True))
    def applicant_salary_read(payload, context):
        applicant_only(context)
        if payload:
            raise KernelError('INVALID_BROWSER_REQUEST', '薪资读取不接受截图地址、区域或脚本。', 400)
        return applicant_browser.read_current_jobs(salary=True)

    router.register(Tool('runtime.applicant.salary.read', applicant_salary_read, effect='none'))
    def applicant_identity_read(payload, context):
        applicant_only(context)
        if payload:
            raise KernelError('INVALID_BROWSER_REQUEST', '账号检查不接受账号凭据、地址或脚本。', 400)
        return applicant_browser.read_current_jobs(identity=True)

    router.register(Tool('runtime.applicant.identity.read', applicant_identity_read, effect='none'))
    router.register(Tool('runtime.applicant.greeting.prepare', applicant_greeting_prepare))
    router.register(Tool('runtime.applicant.greeting.get', applicant_greeting_get, effect='none'))
    def applicant_browser_inspect(payload, context):
        applicant_only(context)
        if payload:
            raise KernelError('INVALID_BROWSER_REQUEST', '诊断不接受地址或脚本。', 400)
        return applicant_browser.read_current_jobs(diagnostics=True)

    router.register(Tool('runtime.applicant.browser.inspect', applicant_browser_inspect, effect='none', maintenance_only=True))
    router.register(Tool('runtime.applicant.jobs.read', applicant_jobs_read, effect='none'))
    def applicant_browser_open(payload, context):
        applicant_only(context)
        if set(payload) != {'confirmed'} or payload.get('confirmed') is not True:
            raise KernelError('BROWSER_CONFIRMATION_REQUIRED', '请通过本机命令明确打开独立求职浏览器。', 400)
        return applicant_browser.open_session(context['scope_id'])

    def applicant_channel_check(payload, context):
        applicant_only(context)
        if payload:
            raise KernelError('INVALID_BROWSER_REQUEST', '连接检查不接受地址或登录凭据。', 400)
        return applicant_browser.check_connection()

    router.register(Tool('runtime.applicant.browser.open', applicant_browser_open, effect='local_file', maintenance_only=True))
    router.register(Tool('runtime.applicant.channel.check', applicant_channel_check, effect='none'))
    router.register(Tool('runtime.applicant.plan.get', applicant_plan_get, effect='none'))
    router.register(Tool('runtime.applicant.plan.save', applicant_plan_save))
    router.register(Tool('runtime.applicant.jobs.preview', applicant_jobs_preview, effect='none'))
    def backup(payload, context):
        if set(payload) != {'confirmed_global'} or payload.get('confirmed_global') is not True:
            raise KernelError('GLOBAL_BACKUP_CONFIRMATION_REQUIRED', '完整备份包含全部工作区，请通过维护命令明确确认。', 400)
        return database_backup.create()

    def backup_check(payload, context):
        if (set(payload) != {'backup_id'} or not isinstance(payload.get('backup_id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', payload['backup_id'])):
            raise KernelError('INVALID_BACKUP_ID', '请提供固定备份目录中的有效备份标识。', 400)
        return database_backup.verify(payload['backup_id'])

    router.register(Tool('runtime.database.backup', backup, effect='local_file', maintenance_only=True))
    router.register(Tool('runtime.database.backup.check', backup_check, effect='none', maintenance_only=True))

    def database_coverage(payload, context):
        if payload:
            raise KernelError('INVALID_COVERAGE_REQUEST', '备份覆盖检查不接受参数。', 400)
        with store.runtime_db() as c:
            return store.runtime_table_audit(c)

    def retention_plan(payload, context):
        if set(payload) - {'days'}:
            raise KernelError('INVALID_RETENTION_DAYS', '只接受可选的天数参数。', 400)
        return retention.inspect(payload.get('days'))

    def retention_apply(payload, context):
        if set(payload) - {'days', 'confirmed'} or payload.get('confirmed') is not True:
            raise KernelError('RETENTION_CONFIRMATION_REQUIRED', '清理不可逆，请明确确认后再执行。', 400)
        return retention.apply(payload.get('days'), context)

    router.register(Tool('runtime.database.coverage', database_coverage, effect='none', maintenance_only=True))
    router.register(Tool('runtime.maintenance.retention.plan', retention_plan, effect='none', maintenance_only=True))
    router.register(Tool('runtime.maintenance.retention.apply', retention_apply, maintenance_only=True))

    def reply_forget_plan(payload, context):
        applicant_only(context)
        if set(payload) - {'intent_id', 'party_display_name'}:
            raise KernelError('INVALID_FORGET_SELECTOR', '只接受回复意图标识或会话名称。', 400)
        with store.runtime_db() as c:
            check_context(c, context['context_token'])
            return applicant_forget.review(c, context['scope_id'], payload)

    def reply_forget(payload, context):
        applicant_only(context)
        if set(payload) - {'intent_id', 'party_display_name', 'confirmed'}:
            raise KernelError('INVALID_FORGET_SELECTOR', '只接受回复意图标识或会话名称。', 400)
        if payload.get('confirmed') is not True:
            raise KernelError('FORGET_CONFIRMATION_REQUIRED', '删除对话正文不可逆，请明确确认。', 400)
        selector = {key: value for key, value in payload.items() if key != 'confirmed'}
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE');check_context(c, context['context_token'])
            return record_commit(c, context, 'runtime.applicant.reply.forget',
                                 applicant_forget.forget(c, context['scope_id'], selector))

    router.register(Tool('runtime.applicant.reply.forget.plan', reply_forget_plan, effect='none', maintenance_only=True))
    router.register(Tool('runtime.applicant.reply.forget', reply_forget, maintenance_only=True))
    router.register(Tool('runtime.model.readiness.get', model_readiness, effect='none'))
    router.register(Tool('runtime.model.review', model_review))
    router.register(Tool('runtime.model.usage.get', model_usage, effect='none'))
    router.register(Tool("runtime.context", context_get, effect="none", context_required=False))
    router.register(Tool("runtime.switch", context_switch, changes_context=True))
    router.register(Tool("runtime.document.get", document_get, effect="none"))
    router.register(Tool("runtime.document.save", document_save))
    router.register(Tool("runtime.workspace.export", workspace_export, effect="none"))
    router.register(Tool('runtime.workspace.restore.preflight', restore_preflight, effect='none'))
    router.register(Tool('runtime.workspace.restore.commit', restore_commit))
    router.register(Tool('runtime.workspace.restored.get', restored_get, effect='none'))
    router.register(Tool("draft.reply", draft_reply, kind="work", effect="none"))

    def submit(payload, context):
        if set(payload) - {"tool", "input", "idempotency_key"} or not isinstance(payload.get("input"), dict):
            raise KernelError("INVALID_INPUT", "任务输入格式不正确。", 400)
        return engine.submit(payload.get("tool"), payload["input"], payload.get("idempotency_key"), context,
                             parent_task_id=context["task_id"])

    def task_list(payload, context):
        review = payload.get("review", "all")
        if set(payload) - {"review"} or not isinstance(review, str) or review not in {"all", "pending", "handled", "superseded"}:
            raise KernelError("INVALID_REVIEW", "请选择待处理、已人工处理或全部记录。", 400)
        with store.runtime_db() as c:
            check_context(c, context["context_token"])
            rows = c.execute("SELECT * FROM runtime_tasks WHERE scope_id=? AND kind='work' "
                             "AND (?='all' OR review_status=?) ORDER BY created_at DESC LIMIT 50",
                             (context["scope_id"], review, review)).fetchall()
            kind = "facts" if context["role"] == "recruiter" else "profile"
            document = store.runtime_document(c, context["scope_id"], kind)
            tasks = [public_task(row) for row in rows]
            for task in tasks:
                task["current_document_revision"] = document["revision"]
                task["draft_stale"] = task.get("document_revision") != document["revision"]
            return tasks

    def task_search(payload, context):
        query, review, cursor = payload.get("query", ""), payload.get("review", "pending"), payload.get("cursor")
        stage = payload.get("stage", "all")
        if (set(payload) - {"query", "review", "cursor", "stage"} or not isinstance(query, str) or len(query) > 120
                or not isinstance(stage, str) or stage not in ("all",) + QUEUE_STAGES
                or not isinstance(review, str) or review not in {"all", "pending", "handled", "superseded"}
                or (cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r"[0-9a-f]{32}", cursor)))):
            raise KernelError("INVALID_SEARCH", "请使用不超过 120 字的关键词及有效的记录筛选。", 400)
        with store.runtime_db() as c:
            c.execute("BEGIN")
            check_context(c, context["context_token"])
            document = store.runtime_document(c, context["scope_id"], "facts" if context["role"] == "recruiter" else "profile")
            c.create_function("jobkit_queue_stage", 7, lambda *args: queue_stage(*args, document["revision"]), deterministic=True)
            where = "scope_id=? AND kind='work' AND tool='draft.reply' AND (?='all' OR review_status=?)"
            args = [context["scope_id"], review, review]
            query = query.strip()
            if query:
                where += " AND (instr(lower(conversation_note),lower(?))>0 OR instr(lower(CASE WHEN json_valid(payload) THEN coalesce(json_extract(payload,'$.message'),'') ELSE '' END),lower(?))>0)"
                args += [query, query]
            matching = "WITH matching AS (SELECT *,jobkit_queue_stage(payload,result,status,manual_revision,manual_text,superseded_by_task_id,review_status) AS queue_stage FROM runtime_tasks WHERE " + where + ") "
            counts = {key: 0 for key in QUEUE_STAGES}
            counts.update({row["queue_stage"]: row["n"] for row in c.execute(matching + "SELECT queue_stage,count(*) AS n FROM matching GROUP BY queue_stage", args)})
            total = sum(counts.values()) if stage == "all" else counts[stage]
            page_where, page_args = "(?='all' OR queue_stage=?)", args + [stage, stage]
            if cursor is not None:
                boundary = c.execute("SELECT created_at FROM runtime_tasks WHERE task_id=? AND scope_id=? AND kind='work' AND tool='draft.reply'", (cursor, context["scope_id"])).fetchone()
                if boundary is None:
                    raise KernelError("INVALID_SEARCH_CURSOR", "分页位置无法核对，请重新搜索。", 400)
                page_where += " AND (created_at<? OR (created_at=? AND task_id<?))"
                page_args += [boundary["created_at"], boundary["created_at"], cursor]
            rows = c.execute(matching + "SELECT * FROM matching WHERE " + page_where + " ORDER BY created_at DESC,task_id DESC LIMIT 51", page_args).fetchall()
            tasks = [public_task(row) for row in rows[:50]]
            for task, row in zip(tasks, rows):
                task["queue_stage"] = row["queue_stage"]
                task["current_document_revision"] = document["revision"]
                task["draft_stale"] = task.get("document_revision") != document["revision"]
            return {"tasks": tasks, "total": total, "stage_counts": counts, "next_cursor": tasks[-1]["task_id"] if len(rows) > 50 else None}

    def events(payload, context):
        trace_id = payload.get("trace_id")
        if (set(payload) - {"trace_id"} or (trace_id is not None and
                (not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id)))):
            raise KernelError("INVALID_TRACE_ID", "执行记录标识格式不正确。", 400)
        with store.runtime_db() as c:
            check_context(c, context["context_token"])
            rows = c.execute("SELECT e.event_id,e.task_id,e.tool,e.status,e.code,e.occurred_at,t.parent_task_id "
                             "FROM runtime_events e LEFT JOIN runtime_tasks t ON t.task_id=e.task_id "
                             "WHERE e.scope_id=? AND (? IS NULL OR e.task_id=?) "
                             "ORDER BY e.event_id DESC LIMIT 100", (context["scope_id"], trace_id, trace_id)).fetchall()
            return [dict(row) for row in rows]

    def recovery_list(payload, context):
        if payload:
            raise KernelError("INVALID_INPUT", "核对列表不接受额外参数。", 400)
        with store.runtime_db() as c:
            check_context(c, context["context_token"])
            rows = c.execute("SELECT task_id,tool,status,error_code,created_at FROM runtime_tasks "
                             "WHERE scope_id=? AND kind='control' AND status IN ('queued','executing','outcome_unknown') "
                             "AND task_id<>? ORDER BY created_at DESC LIMIT 50",
                             (context["scope_id"], context["task_id"])).fetchall()
            return [dict(row) for row in rows]

    router.register(Tool("runtime.task.submit", submit))
    router.register(Tool("runtime.task.batch.submit", lambda p, ctx: engine.submit_batch(p, ctx)))
    router.register(Tool("runtime.task.batch.execute", lambda p, ctx: engine.execute_batch(p, ctx)))
    router.register(Tool("runtime.task.review", lambda p, ctx: engine.review(p, ctx)))
    router.register(Tool("runtime.task.replace", lambda p, ctx: engine.replace_draft(p, ctx)))
    router.register(Tool("runtime.task.edit", lambda p, ctx: engine.edit_draft(p, ctx)))
    router.register(Tool("runtime.task.edits.get", lambda p, ctx: engine.draft_edits(p.get("task_id"), ctx), effect="none"))
    router.register(Tool("runtime.task.copy.get", lambda p, ctx: engine.copy_draft(p.get("task_id"), ctx), effect="none"))
    router.register(Tool("runtime.task.execute", lambda p, ctx: engine.execute(p.get("task_id"), ctx["context_token"], work_only=True, receipt_owner=ctx)))
    router.register(Tool("runtime.task.get", lambda p, ctx: engine.read(p.get("task_id"), ctx), effect="none"))
    router.register(Tool("runtime.task.cancel", lambda p, ctx: engine.cancel(p.get("task_id"), ctx)))
    router.register(Tool("runtime.task.recover", lambda p, ctx: engine.recover(p.get("task_id"), ctx)))
    router.register(Tool("runtime.task.list", task_list, effect="none"))
    router.register(Tool("runtime.task.search", task_search, effect="none"))
    router.register(Tool("runtime.events", events, effect="none"))
    router.register(Tool("runtime.recovery.list", recovery_list, effect="none"))
