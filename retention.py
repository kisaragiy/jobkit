"""Bounded retention for operational traces; evidence and task state are never deleted.

Only two tables are ever pruned: `runtime_events` (append-only operational trace) and
terminal `runtime_model_calls` rows. Tasks, documents, send attempts, model reviews and
imports are evidence — they are never removed here, however old they are.
"""
import os
import time

import store
from sandbox import KernelError, require_maintenance

DEFAULT_DAYS = 30
MIN_DAYS, MAX_DAYS = 1, 365
ENVIRONMENT = 'JOBKIT_RETENTION_DAYS'
TERMINAL_MODEL_STATES = ('not_sent', 'response_error', 'response_received')


def days(requested=None):
    """Explicit argument wins over the environment; both are bounded."""
    if requested is None:
        raw = os.environ.get(ENVIRONMENT, '').strip()
        if not raw:
            return DEFAULT_DAYS
        try:
            requested = int(raw)
        except ValueError:
            raise KernelError('INVALID_RETENTION_DAYS', '留存天数配置不是整数，未执行任何清理。', 400) from None
    if type(requested) is not int or not MIN_DAYS <= requested <= MAX_DAYS:
        raise KernelError('INVALID_RETENTION_DAYS', f'留存天数必须是 {MIN_DAYS}–{MAX_DAYS} 之间的整数。', 400)
    return requested


def source(requested=None):
    if requested is not None:
        return 'explicit'
    return 'environment:' + ENVIRONMENT if os.environ.get(ENVIRONMENT, '').strip() else 'default'


def cutoff(value, now=None):
    return (time.time() if now is None else now) - value * 86400


def _marks(states):
    return ','.join('?' * len(states))


def _counts(c, floor):
    marks = _marks(TERMINAL_MODEL_STATES)
    deletable = {
        'runtime_events': c.execute('SELECT count(*) FROM runtime_events WHERE occurred_at < ?', (floor,)).fetchone()[0],
        'runtime_model_calls': c.execute(
            f'SELECT count(*) FROM runtime_model_calls WHERE created_at < ? AND state IN ({marks})',
            (floor, *TERMINAL_MODEL_STATES)).fetchone()[0]}
    unfinished = c.execute(
        f'SELECT count(*) FROM runtime_model_calls WHERE created_at < ? AND state NOT IN ({marks})',
        (floor, *TERMINAL_MODEL_STATES)).fetchone()[0]
    return deletable, unfinished


def inspect(requested=None):
    """Read-only plan: what would be deleted, and what is deliberately kept."""
    require_maintenance()
    value = days(requested)
    floor = cutoff(value)
    with store.runtime_db() as c:
        deletable, unfinished = _counts(c, floor)
        audit = store.runtime_table_audit(c)
    return {'days': value, 'policy_source': source(requested), 'cutoff': floor,
            'deletable': deletable, 'total_deletable': sum(deletable.values()),
            'kept_unfinished_model_calls': unfinished,
            'never_pruned': ['runtime_tasks', 'runtime_documents', 'runtime_send_attempts',
                             'runtime_model_reviews', 'runtime_workspace_imports', 'runtime_draft_edits'],
            'backup_coverage': audit,
            'irreversible': True, 'applied': False,
            'recommendation': '先执行完整备份（runtime.database.backup），再执行清理。'}


def apply(requested, context):
    """Delete only what inspect() said it would; the receipt records the exact counts."""
    from execution_engine import check_context, record_commit
    require_maintenance()
    value = days(requested)
    floor = cutoff(value)
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_context(c, context['context_token'])
        planned, unfinished = _counts(c, floor)
        marks = _marks(TERMINAL_MODEL_STATES)
        deleted = {'runtime_events': c.execute('DELETE FROM runtime_events WHERE occurred_at < ?', (floor,)).rowcount}
        deleted['runtime_model_calls'] = c.execute(
            f'DELETE FROM runtime_model_calls WHERE created_at < ? AND state IN ({marks})',
            (floor, *TERMINAL_MODEL_STATES)).rowcount
        if deleted != planned:
            raise KernelError('RETENTION_COUNT_MISMATCH', '删除行数与计划不一致，已回滚。')
        return record_commit(c, context, 'runtime.maintenance.retention.apply',
                             {'days': value, 'policy_source': source(requested), 'cutoff': floor,
                              'deleted': deleted, 'total_deleted': sum(deleted.values()),
                              'kept_unfinished_model_calls': unfinished,
                              'irreversible': True, 'applied': True})
