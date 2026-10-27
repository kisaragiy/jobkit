"""Pure consistency checks for local business records; no IO or execution path."""
import math
import re
from sandbox import KernelError

MAX_BYTES = 20 * 1024 * 1024
MAX_TASKS = 10000
MAX_EDITS = 50000


def validate_record_graph(tasks, edits):
    def invalid():
        raise KernelError("INVALID_EXPORT_STATE", "任务关系或人工修改历史不一致，导出已停止，请先核对记录。")

    def integer(value):
        return type(value) is int and value >= 0

    index, history = {}, {}
    for task in tasks:
        task_id = task.get("task_id")
        if (not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id) or task_id in index
                or not integer(task.get("document_revision")) or not integer(task.get("manual_revision"))):
            invalid()
        index[task_id] = task
        history[task_id] = {}
    for edit in edits:
        task_id, revision = edit.get("task_id"), edit.get("revision")
        if (not isinstance(task_id, str) or task_id not in index or not integer(revision) or revision < 1
                or revision in history[task_id] or not integer(edit.get("document_revision"))
                or edit["document_revision"] != index[task_id]["document_revision"]
                or not isinstance(edit.get("text"), str) or not edit["text"].strip() or len(edit["text"]) > 8000
                or type(edit.get("created_at")) not in {int, float}
                or not math.isfinite(edit["created_at"]) or edit["created_at"] < 0):
            invalid()
        history[task_id][revision] = edit["text"]
    for task_id, task in index.items():
        previous, following = task.get("supersedes_task_id"), task.get("superseded_by_task_id")
        if (task.get("review_status") == "superseded") != (following is not None):
            invalid()
        if following is not None:
            if (not isinstance(following, str) or following not in index or following == task_id
                    or index[following].get("supersedes_task_id") != task_id
                    or index[following]["document_revision"] <= task["document_revision"]):
                invalid()
        if previous is not None:
            if (not isinstance(previous, str) or previous not in index or previous == task_id
                    or index[previous].get("superseded_by_task_id") != task_id):
                invalid()
        revision, versions = task["manual_revision"], history[task_id]
        # Compare count and sequence without allocating a range of an untrusted size.
        if len(versions) != revision or (versions and sorted(versions) != list(range(1, len(versions) + 1))):
            invalid()
        if revision:
            result = task.get("result")
            if (versions[revision] != task.get("manual_text") or not isinstance(result, dict)
                    or not isinstance(result.get("text"), str) or not result["text"].strip()
                    or type(result.get("document_revision")) is not int
                    or result["document_revision"] != task["document_revision"]):
                invalid()
        elif task.get("manual_text") is not None:
            invalid()
