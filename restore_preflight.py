"""Strict, pure v1 snapshot validation. No file IO, restoration, or task execution."""
import hashlib
import json
import math
import re
from datetime import datetime
from sandbox import KernelError
from workspace_records import MAX_BYTES, MAX_TASKS, MAX_EDITS, validate_record_graph
from workspace_bundle import MAX_ORIGINS, ORIGIN_FIELDS
TASK_FIELDS = frozenset('task_id status error_code created_at updated_at result result_state conversation_note review_status review_revision manual_text manual_revision supersedes_task_id superseded_by_task_id message document_revision input_state source_type'.split())
STATES = {'queued', 'executing', 'completed', 'failed', 'blocked', 'cancelled', 'outcome_unknown'}


def invalid():
    raise KernelError('INVALID_RESTORE_FILE', '导出文件格式、内容或记录关系无法完整核对；预检已停止，未恢复数据。', 400)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result
    def constant(_value):
        raise ValueError('nonfinite constant')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, ValueError, RecursionError):
        invalid()


def integer(value):
    return type(value) is int and value >= 0


def timestamp(value):
    return type(value) in {int, float} and 0 <= value <= 1e15 and math.isfinite(value)


def identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip():
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return True


def validate_snapshot(text, *, scope_id, fact_fields, profile_fields):
    if not isinstance(text, str):
        invalid()
    try:
        size = len(text.encode('utf-8'))
    except UnicodeError:
        invalid()
    if size > MAX_BYTES:
        raise KernelError('RESTORE_TOO_LARGE', '文件超过当前 20 MiB 预检容量，未恢复数据。', 413)
    data = strict_json(text)
    if isinstance(data, dict) and type(data.get('format_version')) is int and data['format_version'] == 2:
        return validate_v2(data, scope_id=scope_id, fact_fields=fact_fields, profile_fields=profile_fields)
    fields = {'format', 'format_version', 'created_at', 'workspace', 'document', 'tasks', 'draft_edits', 'counts', 'restore_supported', 'platform_send_evidence'}
    if (not isinstance(data, dict) or set(data) != fields or data['format'] != 'jobkit.workspace-records'
            or type(data['format_version']) is not int or data['format_version'] != 1
            or data['restore_supported'] is not False or data['platform_send_evidence'] is not False):
        invalid()
    try:
        if not isinstance(data['created_at'], str) or len(data['created_at']) > 64 or datetime.fromisoformat(data['created_at']).utcoffset() is None:
            invalid()
    except (ValueError, OverflowError):
        invalid()
    workspace, document, tasks, edits, counts = (data[k] for k in ('workspace', 'document', 'tasks', 'draft_edits', 'counts'))
    if (not isinstance(workspace, dict) or set(workspace) != {'scope_id', 'role', 'organization_id', 'account_id'}
            or not isinstance(workspace['role'], str) or workspace['role'] not in {'recruiter', 'applicant'}
            or not identity(workspace['organization_id']) or not identity(workspace['account_id'])
            or workspace['scope_id'] != scope_id(workspace['role'], workspace['organization_id'], workspace['account_id'])):
        invalid()
    kind = 'facts' if workspace['role'] == 'recruiter' else 'profile'
    allowed = fact_fields if kind == 'facts' else profile_fields
    if (not isinstance(document, dict) or set(document) != {'kind', 'revision', 'content'} or document['kind'] != kind
            or not integer(document['revision']) or not isinstance(document['content'], dict)
            or set(document['content']) - allowed
            or any(not isinstance(v, str) or len(v) > 2000 for v in document['content'].values())
            or (document['revision'] == 0 and document['content'])
            or (kind == 'facts' and document['revision'] > 0 and not document['content'].get('source', '').strip())):
        invalid()
    if not isinstance(tasks, list) or not isinstance(edits, list):
        invalid()
    if len(tasks) > MAX_TASKS or len(edits) > MAX_EDITS:
        raise KernelError('RESTORE_TOO_LARGE', '任务或修订数量超过当前预检容量，未恢复数据。', 413)
    if (not isinstance(counts, dict) or set(counts) != {'tasks', 'draft_edits'}
            or not integer(counts['tasks']) or not integer(counts['draft_edits'])
            or counts['tasks'] != len(tasks) or counts['draft_edits'] != len(edits)):
        invalid()
    for task in tasks:
        if (not isinstance(task, dict) or set(task) != TASK_FIELDS
                or not isinstance(task['status'], str) or task['status'] not in STATES
                or not isinstance(task['review_status'], str) or task['review_status'] not in {'pending', 'handled', 'superseded'}
                or not integer(task['review_revision']) or not integer(task['document_revision'])
                or (task['review_status'] != 'pending' and task['review_revision'] < 1)
                or (task['review_status'] == 'handled' and task['status'] in {'queued', 'executing'})
                or (task['review_status'] == 'superseded' and task['status'] in {'queued', 'executing', 'outcome_unknown'})
                or (type(task['manual_revision']) is int and task['manual_revision'] > 0 and task['status'] != 'completed')
                or task['document_revision'] > document['revision']
                or not timestamp(task['created_at']) or not timestamp(task['updated_at'])
                or not isinstance(task['message'], str) or not task['message'].strip() or len(task['message']) > 8000
                or not isinstance(task['conversation_note'], str) or len(task['conversation_note']) > 120
                or task['input_state'] != 'readable' or task['source_type'] != 'manual'
                or (task['error_code'] is not None and (not isinstance(task['error_code'], str) or len(task['error_code']) > 120))
                or any(v is not None and (not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{32}', v)) for v in (task['supersedes_task_id'], task['superseded_by_task_id']))):
            invalid()
        result = task['result']
        if result is None:
            if task['result_state'] != 'empty' or task['status'] == 'completed':
                invalid()
        else:
            allowed_result = {'state', 'text', 'citations', 'reason', 'needs', 'hits', 'skip', 'document_revision', 'sent'}
            if (not isinstance(result, dict) or set(result) - allowed_result
                    or not {'state', 'text', 'document_revision', 'sent'} <= set(result)
                    or result['state'] not in ('drafted', 'needs_review') or not isinstance(result['text'], str)
                    or bool(result['text'].strip()) != (result['state'] == 'drafted')
                    or result['sent'] is not False or not integer(result['document_revision'])
                    or result['document_revision'] != task['document_revision'] or task['result_state'] != 'readable'
                    or task['status'] in {'queued', 'executing'}
                    or ('reason' in result and not isinstance(result['reason'], str))
                    or ('skip' in result and type(result['skip']) is not bool)):
                invalid()
            for key in ('citations', 'needs', 'hits'):
                if key in result and (not isinstance(result[key], list) or len(result[key]) > 50 or any(not isinstance(v, str) or len(v) > 2000 for v in result[key])):
                    invalid()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'task_id', 'revision', 'document_revision', 'text', 'created_at'}:
            invalid()
    try:
        validate_record_graph(tasks, edits)
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (KernelError, TypeError, ValueError, RecursionError, OverflowError):
        invalid()
    if len(canonical) > MAX_BYTES:
        raise KernelError('RESTORE_TOO_LARGE', '规范化后的文件超过预检容量，未恢复数据。', 413)
    return data, hashlib.sha256(canonical).hexdigest()


def validate_v2(data, *, scope_id, fact_fields, profile_fields):
    fields = {'format', 'format_version', 'created_at', 'workspace', 'document', 'tasks', 'draft_edits', 'counts', 'restore_supported', 'platform_send_evidence', 'origins'}
    if (set(data) != fields or not isinstance(data['tasks'], list) or not isinstance(data['draft_edits'], list)
            or not isinstance(data['origins'], list)):
        invalid()
    if len(data['tasks']) > MAX_TASKS or len(data['draft_edits']) > MAX_EDITS or len(data['origins']) > MAX_ORIGINS:
        raise KernelError('RESTORE_TOO_LARGE', '记录、修订或来源数量超过预检容量。', 413)
    counts = data['counts']
    if (not isinstance(counts, dict) or set(counts) != {'tasks', 'draft_edits'}
            or not integer(counts['tasks']) or not integer(counts['draft_edits'])
            or counts != {'tasks': len(data['tasks']), 'draft_edits': len(data['draft_edits'])}):
        invalid()
    groups = {None: {'workspace': data['workspace'], 'document': data['document'], 'tasks': [], 'edits': []}}
    for origin in data['origins']:
        if (not isinstance(origin, dict) or set(origin) != {'origin_id', 'workspace', 'document'}
                or not isinstance(origin['origin_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', origin['origin_id'])
                or origin['origin_id'] in groups):
            invalid()
        groups[origin['origin_id']] = {**origin, 'tasks': [], 'edits': []}
    ids, source_ids = {}, set()
    for task in data['tasks']:
        if (not isinstance(task, dict) or set(task) != TASK_FIELDS | ORIGIN_FIELDS
                or not isinstance(task.get('task_id'), str) or task['task_id'] in ids):
            invalid()
        if task['record_origin'] == 'local':
            if task['origin_id'] is not None or task['source_task_id'] is not None:
                invalid()
            group = None
        elif task['record_origin'] == 'restored_history':
            group = task['origin_id']
            if (not isinstance(group, str) or group not in groups
                    or not isinstance(task['source_task_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', task['source_task_id'])
                    or (group, task['source_task_id']) in source_ids):
                invalid()
            source_ids.add((group, task['source_task_id']))
        else:
            invalid()
        ids[task['task_id']] = group
        groups[group]['tasks'].append({key: value for key, value in task.items() if key not in ORIGIN_FIELDS})
    for edit in data['draft_edits']:
        if not isinstance(edit, dict) or not isinstance(edit.get('task_id'), str) or edit['task_id'] not in ids:
            invalid()
        groups[ids[edit['task_id']]]['edits'].append(edit)
    # Each provenance group is validated through the v1 contract. No nested v2 bundles.
    for group in groups.values():
        unit = {key: value for key, value in data.items() if key not in {'origins', 'format_version', 'tasks', 'draft_edits', 'counts', 'workspace', 'document'}}
        unit.update(format_version=1, workspace=group['workspace'], document=group['document'],
                    tasks=group['tasks'], draft_edits=group['edits'],
                    counts={'tasks': len(group['tasks']), 'draft_edits': len(group['edits'])})
        try:
            encoded = json.dumps(unit, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            invalid()
        checked, _digest = validate_snapshot(encoded, scope_id=scope_id, fact_fields=fact_fields, profile_fields=profile_fields)
        if checked['workspace']['role'] != data['workspace']['role']:
            invalid()
    try:
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError):
        invalid()
    if len(canonical) > MAX_BYTES:
        raise KernelError('RESTORE_TOO_LARGE', '规范化后的文件超过预检容量。', 413)
    return data, hashlib.sha256(canonical).hexdigest()
