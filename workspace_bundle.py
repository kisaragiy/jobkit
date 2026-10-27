"""Pure, flat provenance projection for combined backups; no execution or IO."""
from copy import deepcopy
from sandbox import KernelError

ORIGIN_FIELDS = frozenset({'record_origin', 'origin_id', 'source_task_id'})
MAX_ORIGINS = 100


def archived_projection(source, mapping, import_id):
    origins = deepcopy(source.get('origins', []))
    if any(origin['origin_id'] == import_id for origin in origins):
        raise KernelError('RESTORE_ID_CONFLICT', '恢复来源标识冲突，未继续处理。')
    origins.append({'origin_id': import_id, 'workspace': deepcopy(source['workspace']), 'document': deepcopy(source['document'])})
    tasks = []
    for original in source['tasks']:
        task = deepcopy(original)
        historical = original.get('record_origin') == 'restored_history'
        task.update(task_id=mapping[original['task_id']], record_origin='restored_history',
                    origin_id=original['origin_id'] if historical else import_id,
                    source_task_id=original['source_task_id'] if historical else original['task_id'])
        for field in ('supersedes_task_id', 'superseded_by_task_id'):
            task[field] = mapping[original[field]] if original[field] is not None else None
        tasks.append(task)
    edits = [{**edit, 'task_id': mapping[edit['task_id']]} for edit in source['draft_edits']]
    return tasks, edits, origins


def combine_snapshot(local, source, mapping, import_id):
    tasks, edits, origins = archived_projection(source, mapping, import_id)
    native = [{**task, 'record_origin': 'local', 'origin_id': None, 'source_task_id': None} for task in local['tasks']]
    return {**local, 'format_version': 2, 'tasks': native + tasks,
            'draft_edits': local['draft_edits'] + edits, 'origins': origins,
            'counts': {'tasks': len(native) + len(tasks), 'draft_edits': len(local['draft_edits']) + len(edits)}}
