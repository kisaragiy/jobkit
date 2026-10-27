"""Single persistent task executor. UI/CLI submit; they never drive execution loops."""
import json
import hashlib
import logging
import time
import uuid
import llm_draft
import store
import applicant_salary_review
import applicant_binding
import applicant_reply
import applicant_reply_window
import applicant_reply_windows
import applicant_send
import applicant_send_ledger
import run_state
from sandbox import KernelError, authorize, capability, record_refusal
from task_lease import task_lease

LOG = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "blocked", "cancelled", "outcome_unknown"}
RECEIPTED_CONTROLS = frozenset({"runtime.switch", "runtime.document.save", "runtime.task.submit",
                               "runtime.task.execute", "runtime.task.cancel", "runtime.task.recover",
                               "runtime.task.batch.submit", "runtime.task.batch.execute", "runtime.task.review", "runtime.task.replace", "runtime.task.edit", "runtime.workspace.restore.commit", "runtime.model.review", "runtime.applicant.plan.save", "runtime.applicant.greeting.prepare", "runtime.applicant.salary.confirm", "runtime.applicant.browser.binding.confirm", "runtime.applicant.reply.prepare", "runtime.applicant.reply.window.start", "runtime.applicant.reply.window.observe", "runtime.applicant.send.recipient.confirm", "runtime.applicant.send.reserve", "runtime.applicant.send.release", "runtime.applicant.send.handoff", "runtime.applicant.send.manual.claim", "runtime.applicant.send.readback.commit", "runtime.maintenance.retention.apply", "runtime.applicant.reply.forget", "runtime.applicant.send.queue.handoff", "runtime.applicant.jobs.search.step", "runtime.applicant.jobs.search.reset", "runtime.applicant.action.authorize", "runtime.applicant.chat.authorize", "runtime.applicant.chat.outcome.resolve", "runtime.applicant.chat.outcome.archive", "runtime.applicant.greeting.plan", "runtime.applicant.jobs.search.stop", "runtime.run.pause", "runtime.run.resume"})


def batch_outcome(task_ids, tasks, stopped_code):
    return {"tasks": tasks, "remaining_task_ids": task_ids[len(tasks):], "stopped_code": stopped_code,
            "drafted": sum(t["status"] == "completed" and bool((t["result"] or {}).get("text")) for t in tasks),
            "needs_review": sum(t["status"] == "completed" and not (t["result"] or {}).get("text") for t in tasks),
            "failed": sum(t["status"] != "completed" for t in tasks)}


def check_manual_draft(c, row):
    revision, text = row["manual_revision"], row["manual_text"]
    valid = type(revision) is int and revision >= 0
    if valid and revision == 0:
        valid = text is None
    elif valid:
        edit = c.execute("SELECT text FROM runtime_draft_edits WHERE task_id=? AND scope_id=? AND revision=?",
                         (row["task_id"], row["scope_id"], revision)).fetchone()
        valid = isinstance(text, str) and bool(text.strip()) and edit is not None and edit["text"] == text
    if not valid:
        raise KernelError("INVALID_MANUAL_DRAFT_STATE", "人工稿记录无法核对，请人工检查，暂不能修改或复制。")


def check_conversation_key(row, value):
    """人工核对只能发生在草稿自己的会话里。

    拿 A 会话的草稿去 B 会话确认必须失败，而且要说清是「串会话」而不是「记录不存在」。
    不传键的调用保持旧行为（升级前的页面只认 task_id），传了就必须对上。
    """
    if value is None:
        return applicant_reply.task_key(row)
    try:
        key = applicant_reply_windows.checked_key(value)
    except ValueError:
        raise KernelError('INVALID_CONVERSATION_KEY', '会话键必须是 1..128 个不含空白的可见字符；不要用显示名，同名 HR 会串。', 400) from None
    if key != applicant_reply.task_key(row):
        raise KernelError('CONVERSATION_KEY_MISMATCH', '这份草稿属于另一个会话，不能在当前会话里修改、复制或确认。')
    return key


def commit_owner(c, context, tool_name):
    """Validate ownership without starting another database connection."""
    if not c.in_transaction or tool_name not in RECEIPTED_CONTROLS:
        raise KernelError("INVALID_COMMIT_OWNER", "控制操作提交凭据不匹配。", 500)
    row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (context.get("task_id"),)).fetchone()
    if (row is None or row["tool"] != tool_name or row["scope_id"] != context["scope_id"]
            or row["context_token"] != context["context_token"] or row["kind"] != "control"
            or row["effect"] != "state" or row["status"] != "executing" or row["commit_receipt"] is not None):
        raise KernelError("INVALID_COMMIT_OWNER", "控制操作提交凭据不匹配。", 500)
    return row


def record_commit(c, context, tool_name, result):
    """Write a local control receipt inside the caller's mutation transaction."""
    row = commit_owner(c, context, tool_name)
    receipt = json.dumps({"version": 1, "task_id": row["task_id"], "tool": tool_name, "scope_id": row["scope_id"],
                          "context_token": row["context_token"], "result": result}, ensure_ascii=False, allow_nan=False)
    c.execute("UPDATE runtime_tasks SET commit_receipt=? WHERE task_id=?", (receipt, row["task_id"]))
    store.runtime_event(c, row["task_id"], row["scope_id"], tool_name, "committed")
    return result


def ledger_document_items(c, scope_id, kind):
    """读一份台账类文档的条目（回执核对用：从库重读，不信回执自述）。"""
    document = store.runtime_document(c, scope_id, kind)
    content = document["content"] if isinstance(document["content"], dict) else {}
    return [item for item in (content.get("items") or ()) if isinstance(item, dict)]


def committed_result(row, c):
    try:
        receipt = json.loads(row["commit_receipt"])
        if (row["tool"] not in RECEIPTED_CONTROLS or row["kind"] != "control" or row["effect"] != "state"
                or not isinstance(receipt, dict) or set(receipt) != {"version", "task_id", "tool", "scope_id", "context_token", "result"}
                or type(receipt["version"]) is not int or receipt["version"] != 1
                or receipt["task_id"] != row["task_id"] or receipt["tool"] != row["tool"] or receipt["scope_id"] != row["scope_id"]
                or receipt["context_token"] != row["context_token"] or not isinstance(receipt["result"], dict)):
            raise ValueError("invalid receipt")
        json.dumps(receipt["result"], allow_nan=False)
        result = receipt["result"]
        payload = json.loads(row["payload"])
        if row['tool'] == 'runtime.model.review':
            saved = store.runtime_model_review_get(c, row['scope_id'], payload['attempt_id'])
            valid = (payload.get('confirmed') is True and saved is not None and result == saved
                     and result.get('note') == payload['note'].strip()
                     and result.get('observed_state') == payload['expected_state']
                     and result.get('disposition') == 'acknowledged_unknown'
                     and result.get('network_outcome_verified') is False
                     and result.get('reservation_refunded') is False
                     and result.get('automatic_execution') is False)
        elif row['tool'] == 'runtime.workspace.restore.commit':
            source = json.loads(payload['snapshot_text'])
            target = result.get('target_workspace', {})
            valid = (set(result) == {'import_id', 'owner_scope_id', 'content_sha256', 'target_workspace', 'counts', 'document_revision', 'automatic_execution', 'history_execution_verified'}
                     and payload.get('confirmed') is True and result.get('owner_scope_id') == row['scope_id']
                     and result.get('content_sha256') == payload['expected_content_sha256']
                     and isinstance(result.get('import_id'), str) and len(result['import_id']) == 32
                     and all(ch in '0123456789abcdef' for ch in result['import_id'])
                     and target.get('role') == source['workspace']['role']
                     and target.get('organization_id') == payload['target']['organization_id'].strip()
                     and target.get('account_id') == payload['target']['account_id'].strip()
                     and target.get('scope_id') == store.runtime_scope_id(target['role'], target['organization_id'], target['account_id'])
                     and result.get('counts') == source['counts'] and all(type(v) is int for v in result['counts'].values())
                     and type(result.get('document_revision')) is int and result['document_revision'] == source['document']['revision']
                     and result.get('automatic_execution') is False and result.get('history_execution_verified') is False)
        elif row['tool'] in {'runtime.applicant.reply.window.start','runtime.applicant.reply.window.observe'}:
            try:
                content=applicant_reply_window.checked({'revision':result['revision'],'content':result['content']},row['scope_id'])
            except KernelError:
                raise ValueError('invalid reply window receipt') from None
            valid=(set(result)=={'kind','revision','content'} and result['kind']==applicant_reply_window.KIND
                   and result['revision']==payload['expected_revision']+1 and content['context_token']==row['context_token']
                   and content['last_observation_task_id']==payload['message_observation_task_id']
                   and content['profile_revision']==payload['expected_profile_revision'] and content['binding_revision']==payload['expected_binding_revision']
                   and (row['tool']!='runtime.applicant.reply.window.start' or content['start_observation_task_id']==payload['message_observation_task_id']))
        elif row['tool']=='runtime.applicant.reply.prepare':
            try:
                saved,task=applicant_reply.read(c,row['scope_id'],result['intent']['intent_id'])
            except KernelError:
                raise ValueError('invalid reply provenance') from None
            valid=(set(result)=={'intent','task','fresh_observation_task_id'} and saved==result['intent']
                   and result['task'].get('task_id')==saved['draft_task_id'] and result['task'].get('scope_id')==row['scope_id']
                   and result['task'].get('tool')=='draft.reply'
                   and result['task'].get('status') in TERMINAL|{'queued','executing'}
                   and type(result['task'].get('document_revision')) is int and result['task']['document_revision']==saved['profile_revision']
                   and result['task'].get('conversation_note')==saved['party']['display_name']
                   and result['task'].get('message')=='\n\n'.join(m['text'] for m in saved['messages']).strip()
                   and result['fresh_observation_task_id']==payload['message_observation_task_id']
                   and [m['message_id'] for m in saved['messages']]==payload['message_ids']
                   and saved['profile_revision']==payload['expected_profile_revision']
                   and saved['binding_revision']==payload['expected_binding_revision'])
        elif row['tool']=='runtime.applicant.send.recipient.confirm':
            try:
                saved=applicant_send.checked_recipient(
                    store.runtime_document(c,row['scope_id'],applicant_send.recipient_kind(payload['intent_id'])),
                    row['scope_id'],payload['intent_id'])
            except (KernelError,KeyError,TypeError):
                raise ValueError('invalid recipient receipt') from None
            valid=(set(result)=={'recipient','revision','created','message_age_seconds','sending_enabled'}
                   and saved==result['recipient']
                   and result['revision']==1 and isinstance(result['created'],bool)
                   and payload.get('confirmed') is True
                   and result['recipient']['context_token']==row['context_token']
                   and result['recipient']['profile_revision']==payload['expected_profile_revision']
                   and result['recipient']['binding_revision']==payload['expected_binding_revision']
                   and result['recipient']['platform_account_id_verified'] is False
                   and result['recipient']['sending_enabled'] is False
                   and result['sending_enabled'] is False)
        elif row['tool']=='runtime.applicant.send.reserve':
            try:
                saved=applicant_send.attempt(c,row['scope_id'],result['attempt']['attempt_id'])
            except (KernelError,KeyError,TypeError):
                raise ValueError('invalid reservation receipt') from None
            valid=(set(result)=={'attempt','budget','created','message_age_seconds','sending_enabled'}
                   and applicant_send.public_attempt(saved)==result['attempt']
                   and result['created'] is True and result['sending_enabled'] is False
                   and saved['intent_id']==payload['intent_id']
                   and saved['recipient_binding_id']==payload['recipient_binding_id']
                   and saved['state']=='reserved'
                   and isinstance(result['budget'],dict) and result['budget'].get('budget_day')==saved['budget_day'])
        elif row['tool']=='runtime.applicant.send.release':
            try:
                saved=applicant_send.attempt(c,row['scope_id'],result['attempt']['attempt_id'])
            except (KernelError,KeyError,TypeError):
                raise ValueError('invalid release receipt') from None
            valid=(set(result)=={'attempt','budget','reason','sending_enabled'}
                   and applicant_send.public_attempt(saved)==result['attempt']
                   and saved['state']=='released' and result['reason']==payload['reason']
                   and result['sending_enabled'] is False)
        elif row['tool']=='runtime.applicant.send.handoff':
            try:
                saved=applicant_send.checked_handoff(
                    store.runtime_document(c,row['scope_id'],applicant_send.handoff_kind(payload['attempt_id'])),
                    row['scope_id'],payload['attempt_id'])
            except (KernelError,KeyError,TypeError):
                raise ValueError('invalid handoff receipt') from None
            valid=(set(result)=={'handoff','created','text_expires_at','expires_in_seconds',
                                'manual_handoff_only','delivery_confirmed','sending_enabled'}
                   and saved==result['handoff'] and isinstance(result['created'],bool)
                   and result['manual_handoff_only'] is True
                   and result['delivery_confirmed'] is False and result['sending_enabled'] is False)
        elif row['tool']=='runtime.applicant.send.manual.claim':
            try:
                saved=applicant_send.checked_claim(
                    store.runtime_document(c,row['scope_id'],applicant_send.claim_kind(payload['attempt_id'])),
                    row['scope_id'],payload['attempt_id'])
            except (KernelError,KeyError,TypeError):
                raise ValueError('invalid claim receipt') from None
            valid=(set(result)=={'attempt','claim','created','delivery_confirmed','sending_enabled','note'}
                   and saved==result['claim'] and isinstance(result['created'],bool)
                   and result['delivery_confirmed'] is False and result['sending_enabled'] is False)
        elif row['tool']=='runtime.applicant.send.readback.commit':
            try:
                saved=applicant_send.checked_readback(
                    store.runtime_document(c,row['scope_id'],applicant_send.readback_kind(payload['attempt_id'])),
                    row['scope_id'],payload['attempt_id'])
            except (KernelError,KeyError,TypeError):
                raise ValueError('invalid readback receipt') from None
            valid=(set(result)=={'readback','created','party_match','delivery_confirmed','sending_enabled'}
                   and saved==result['readback'] and isinstance(result['created'],bool)
                   and isinstance(result['party_match'],bool)
                   and result['delivery_confirmed'] is False and result['sending_enabled'] is False)
        elif row['tool']=='runtime.applicant.send.queue.handoff':
            cards=result.get('cards') or []
            skipped=result.get('skipped') or []
            valid=(set(result)=={'cards','skipped','ok_count','skipped_count','created_count','partial',
                                'queue_limit','manual_handoff_only','delivery_confirmed','sending_enabled'}
                   and type(result['ok_count']) is int and result['ok_count']==len(cards)
                   and type(result['skipped_count']) is int and result['skipped_count']==len(skipped)
                   and type(result['created_count']) is int
                   and result['created_count']==sum(1 for card in cards if card.get('created') is True)
                   and result['partial']==(len(skipped)>0)
                   and result['manual_handoff_only'] is True
                   and result['delivery_confirmed'] is False and result['sending_enabled'] is False
                   and all(card.get('attempt_id') in payload['attempt_ids'] for card in cards)
                   and all(isinstance(item.get('code'),str) for item in skipped))
        elif row['tool']=='runtime.maintenance.retention.apply':
            deleted=result.get('deleted') or {}
            valid=(set(result)=={'days','policy_source','cutoff','deleted','total_deleted',
                                'kept_unfinished_model_calls','irreversible','applied'}
                   and payload.get('confirmed') is True
                   and result['applied'] is True and result['irreversible'] is True
                   and isinstance(deleted,dict) and set(deleted)=={'runtime_events','runtime_model_calls'}
                   and all(type(value) is int and value>=0 for value in deleted.values())
                   and result['total_deleted']==sum(deleted.values()))
        elif row['tool']=='runtime.applicant.reply.forget':
            intents=result.get('intents') or []
            valid=(set(result)=={'intents','deleted_documents','redacted_tasks','redacted_observations',
                                'irreversible','applied','note'}
                   and payload.get('confirmed') is True
                   and result['applied'] is True and result['irreversible'] is True
                   and isinstance(intents,list) and bool(intents)
                   and all(isinstance(value,str) and len(value)==32 for value in intents)
                   and type(result['deleted_documents']) is int and result['deleted_documents']>=0
                   and all(c.execute('SELECT 1 FROM runtime_documents WHERE scope_id=? AND kind=?',
                                     (row['scope_id'],'applicant_reply_intent:'+value)).fetchone() is None
                           for value in intents))
        elif row['tool'] == 'runtime.applicant.browser.binding.confirm':
            try:
                content = applicant_binding.checked({'revision': result['revision'], 'content': result['content']}, row['scope_id'])
            except KernelError:
                raise ValueError('invalid browser binding receipt') from None
            valid = (set(result) == {'kind', 'revision', 'content'} and result['kind'] == 'applicant_browser_binding'
                     and type(result['revision']) is int and result['revision'] == payload['expected_revision'] + 1
                     and payload.get('confirmed') is True
                     and content['identity_observation_task_id'] == payload['identity_observation_task_id']
                     and content['display_name'] == payload['expected_display_name']
                     and content['browser_session_fingerprint'] == payload['expected_browser_session_fingerprint'])
        elif row['tool'] == 'runtime.applicant.salary.confirm':
            try:
                saved = applicant_salary_review.checked(store.runtime_document(c, row['scope_id'], 'applicant_salary:' + result['confirmation_id']), row['scope_id'])
            except KernelError:
                raise ValueError('invalid salary receipt') from None
            valid = (saved == result and payload.get('confirmed') is True
                     and result['job_observation_task_id'] == payload['job_observation_task_id']
                     and result['salary_observation_task_id'] == payload['salary_observation_task_id']
                     and result['job']['job_id'] == payload['job_id']
                     and result['salary_text'] == payload['expected_salary_text']
                     and result['profile_revision'] == payload['expected_profile_revision']
                     and result['plan_revision'] == payload['expected_plan_revision'])
        elif row['tool'] == 'runtime.applicant.greeting.prepare':
            saved = store.runtime_document(c, row['scope_id'], 'applicant_greeting:' + payload['job_id'])
            valid = (saved['revision'] == 1 and saved['content'] == result
                     and result.get('scope_id') == row['scope_id']
                     and result.get('job', {}).get('job_id') == payload['job_id']
                     and result.get('profile_revision') == payload['expected_profile_revision']
                     and result.get('plan_revision') == payload['expected_plan_revision']
                     and result.get('state') == 'awaiting_channel_verification'
                     and all(result.get(k) is False for k in ('sent', 'sending_enabled', 'account_verified', 'quota_consumed')))
        elif row['tool'] == 'runtime.applicant.plan.save':
            valid = (payload.get('confirmed') is True and set(result) == {'kind', 'revision', 'content'} and result['kind'] == 'applicant_plan'
                     and type(result['revision']) is int and result['revision'] == payload['expected_revision'] + 1
                     and isinstance(result['content'], dict) and result['content'] == payload['content'])
        elif row["tool"] == "runtime.document.save":
            valid = (set(result) == {"kind", "revision", "content"} and result["kind"] in {"facts", "profile"}
                     and type(result["revision"]) is int and result["revision"] == payload["expected_revision"] + 1
                     and isinstance(result["content"], dict) and result["content"] == payload["content"])
        elif row["tool"] == "runtime.switch":
            valid = (result.get("role") == payload["role"] and result.get("organization_id") == payload["organization_id"].strip()
                     and result.get("account_id") == payload["account_id"].strip()
                     and result.get("scope_id") == store.runtime_scope_id(result["role"], result["organization_id"], result["account_id"])
                     and isinstance(result.get("context_token"), str) and len(result["context_token"]) == 32
                     and type(result.get("generation")) is int and result["generation"] > 0)
        elif row["tool"] == "runtime.task.batch.submit":
            tasks = result.get("tasks")
            items = payload["items"]
            valid = (isinstance(tasks, list) and len(tasks) == len(items)
                     and type(result.get("created")) is int and type(result.get("reused")) is int
                     and result["created"] + result["reused"] == len(items)
                     and all(t.get("scope_id") == row["scope_id"] and t.get("tool") == "draft.reply"
                             and t.get("message") == item["message"].strip()
                             and t.get("conversation_note") == item["conversation_note"].strip()
                             for t, item in zip(tasks, items)))
        elif row["tool"] == "runtime.task.batch.execute":
            tasks, ids = result.get("tasks"), payload["task_ids"]
            valid = (isinstance(tasks, list) and len(tasks) <= len(ids)
                     and (result.get("stopped_code") is None or isinstance(result.get("stopped_code"), str))
                     and (result.get("stopped_code") is not None or (len(tasks) == len(ids) and all(t.get("status") == "completed" for t in tasks)))
                     and all(t.get("task_id") == ids[index] and t.get("scope_id") == row["scope_id"]
                             and t.get("tool") == "draft.reply" and t.get("status") in TERMINAL for index, t in enumerate(tasks))
                     and result == batch_outcome(ids, tasks, result.get("stopped_code")))
        elif row["tool"] == "runtime.task.replace":
            valid = (result.get("scope_id") == row["scope_id"] and result.get("tool") == "draft.reply"
                     and isinstance(result.get("task_id"), str) and result["task_id"] != payload["task_id"]
                     and result.get("supersedes_task_id") == payload["task_id"]
                     and result.get("document_revision") == payload["expected_document_revision"])
        elif row["tool"] == "runtime.task.edit":
            valid = (result.get("task_id") == payload["task_id"] and result.get("scope_id") == row["scope_id"]
                     and result.get("tool") == "draft.reply" and result.get("status") == "completed"
                     and result.get("manual_revision") == payload["expected_manual_revision"] + 1
                     and result.get("manual_text") == payload["text"].strip()
                     and result.get("document_revision") == payload["expected_document_revision"])
        elif row["tool"] == "runtime.applicant.chat.authorize":
            # 从库重读授权文档比对，不信回执自述（A01 口径：JSON 能解析不等于事实）
            doc = store.runtime_document(c, row["scope_id"], "conversation_authorization")
            content = doc["content"] if isinstance(doc["content"], dict) else {}
            expected_body = hashlib.sha256(payload["text"].strip().encode("utf-8")).hexdigest()
            valid = (result.get("status") == "authorized"
                     and content.get("target_sha256") == result.get("target_sha256")
                     and content.get("body_sha256") == expected_body == result.get("body_sha256")
                     and content.get("action", "reply") == payload.get("action", "reply") == result.get("action")
                     and content.get("target") == result.get("target")
                     and content.get("max_uses") == 1
                     and payload.get("confirmed_by") == result.get("confirmation_by"))
        elif row["tool"] == "runtime.applicant.chat.outcome.resolve":
            doc = store.runtime_document(c, row["scope_id"], applicant_send_ledger.LEDGER_KIND)
            content = doc["content"] if isinstance(doc["content"], dict) else {}
            saved = next((i for i in (content.get("items") or ())
                          if isinstance(i, dict) and i.get("ledger_id") == result.get("ledger_id")), None)
            valid = (saved is not None and saved == result
                     and payload.get("confirmed_by") == result.get("resolved_by")
                     and result.get("state") in (applicant_send_ledger.STATE_DELIVERED,
                                                 applicant_send_ledger.STATE_NOT_DELIVERED))
        elif row["tool"] == "runtime.applicant.chat.outcome.archive":
            # 归档回执：两个文档都从库重读核对 —— 回执说「搬走了」不等于真搬走了
            active = ledger_document_items(c, row["scope_id"], applicant_send_ledger.LEDGER_KIND)
            archive = ledger_document_items(c, row["scope_id"], applicant_send_ledger.ARCHIVE_KIND)
            moved = result.get("archived")
            already = result.get("already_archived")
            active_ids = {item.get("ledger_id") for item in active}
            archived_by_id = {item.get("ledger_id"): item for item in archive}
            valid = (set(result) == {"status", "archived", "archived_count", "already_archived",
                                     "active_count", "archive_total", "capacity", "archive",
                                     "archived_by", "sending_enabled", "delivery_confirmed"}
                     and result["status"] in ("archived", "already_archived", "nothing_to_archive")
                     and isinstance(moved, list) and isinstance(already, list)
                     and type(result["archived_count"]) is int and result["archived_count"] == len(moved)
                     and (result["status"] == "archived") == bool(moved)
                     and all(isinstance(i, str) and len(i) == 20 for i in moved)
                     and all(isinstance(i, str) for i in already)
                     # 真搬了：归档里有这条且是已联系证据，活跃文档里已经没有它
                     and all(i in archived_by_id and applicant_send_ledger.is_contacted(archived_by_id[i])
                             for i in moved)
                     and all(i not in active_ids for i in moved)
                     and all(i in archived_by_id for i in already)
                     and type(result["active_count"]) is int
                     and type(result["archive_total"]) is int
                     and result["archive_total"] >= len(moved)
                     and isinstance(result["capacity"], dict) and isinstance(result["archive"], dict)
                     and payload.get("confirmed_by") == result["archived_by"]
                     and result["sending_enabled"] is False and result["delivery_confirmed"] is False)
        elif row["tool"] in ("runtime.run.pause", "runtime.run.resume"):
            doc = store.runtime_document(c, row["scope_id"], run_state.KIND)
            content = doc["content"] if isinstance(doc["content"], dict) else {}
            valid = (content.get("state") == result.get("state")
                     and content.get("actions") == result.get("actions")
                     and content.get("generation") == result.get("generation")
                     and content.get("paused_by") == result.get("paused_by"))
        else:
            valid = (isinstance(result.get("task_id"), str) and result.get("scope_id") == row["scope_id"]
                     and result.get("status") in TERMINAL | {"queued", "executing"}
                     and (result.get("tool") == payload.get("tool") if row["tool"] == "runtime.task.submit"
                          else result["task_id"] == payload.get("task_id")))
            if row["tool"] == "runtime.task.execute":
                valid = valid and result.get("status") in TERMINAL
            elif row["tool"] == "runtime.task.review":
                valid = (valid and result.get("review_status") == ("handled" if payload["handled"] else "pending")
                         and result.get("review_revision") == payload["expected_review_revision"] + 1)
        if not valid:
            raise ValueError("receipt result does not match operation")
        return receipt["result"]
    except (TypeError, ValueError, RecursionError, KeyError, AttributeError):
        raise KernelError("INVALID_COMMIT_RECEIPT", "提交凭据无法核对，请人工检查该操作。") from None


def public_task(row):
    if row is None:
        raise KernelError("TASK_NOT_FOUND", "未找到当前身份下的任务。", 404)
    result = {key: row[key] for key in ("task_id", "tool", "status", "scope_id", "error_code", "parent_task_id", "created_at", "updated_at")}
    if row["kind"] == "work" and row["tool"] == "draft.reply":
        try:
            output = json.loads(row["result"]) if row["result"] else None
            if output is not None and not isinstance(output, dict):
                raise ValueError("invalid stored draft result")
            result.update(result=output, result_state="readable" if output is not None else "empty")
        except (TypeError, ValueError, RecursionError):
            result.update(result=None, result_state="unreadable")
    else:
        result["result"] = json.loads(row["result"]) if row["result"] else None
    if row["kind"] == "work" and row["tool"] == "draft.reply":
        result.update(conversation_note=row["conversation_note"], review_status=row["review_status"], review_revision=row["review_revision"],
                      manual_text=row["manual_text"], manual_revision=row["manual_revision"],
                      supersedes_task_id=row["supersedes_task_id"], superseded_by_task_id=row["superseded_by_task_id"])
        try:
            payload = json.loads(row["payload"])
            if (not isinstance(payload, dict) or not isinstance(payload.get("message"), str)
                    or type(payload.get("document_revision")) is not int):
                raise ValueError("invalid input")
            result.update(message=payload["message"], document_revision=payload["document_revision"], input_state="readable", source_type="manual")
        except (TypeError, ValueError, RecursionError):
            result.update(message=None, document_revision=None, input_state="unreadable", source_type="manual")
    return result


def prepare_draft(payload, context, c):
    if not isinstance(payload, dict) or set(payload) - {"message", "conversation_note"}:
        raise KernelError("INVALID_INPUT", "草稿任务不能指定身份或资料快照。", 400)
    message, note = payload.get("message"), payload.get("conversation_note", "")
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise KernelError("INVALID_MESSAGE", "请输入不超过 8000 字的消息。", 400)
    if not isinstance(note, str) or len(note) > 120:
        raise KernelError("INVALID_CONVERSATION_NOTE", "会话备注不能超过 120 字。", 400)
    kind = "facts" if context["role"] == "recruiter" else "profile"
    try:
        document = store.runtime_document(c, context["scope_id"], kind)
        if not isinstance(document["content"], dict):
            raise ValueError("invalid document")
    except (TypeError, ValueError, RecursionError):
        raise KernelError("INVALID_DOCUMENT_STATE", "已保存资料无法读取，请人工核对资料。") from None
    prepared = {"message": message.strip(), "document_revision": document["revision"], "document_kind": kind}
    # Legacy snapshots without this field always mean rules. Bind explicit model
    # intent at submission so a later process configuration cannot change it.
    if llm_draft.configuration().backend == 'llm':
        prepared['draft_backend'] = 'llm'
        prepared['llm_binding'] = llm_draft.transport_binding()
    return prepared, note.strip()


def check_context(c, expected):
    current = store.runtime_context(c)
    if current["context_token"] != expected:
        raise KernelError("STALE_CONTEXT", "当前身份已切换，请重新加载工作区。")
    return current


class ExecutionEngine:
    def __init__(self, router):
        self.router = router

    def dispatch(self, name, payload=None, context_token=None):
        """Control and work both take the same registry → engine → sandbox path."""
        tool = None
        try:
            tool = self.router.resolve(name)
            if payload is not None and not isinstance(payload, dict):
                raise KernelError("INVALID_INPUT", "操作参数格式不正确。", 400)
            with store.runtime_db() as c:
                context = store.runtime_context(c)
                authorize(tool, context, context_token)
            task = self.submit(name, payload or {}, uuid.uuid4().hex, context, internal=True)
            result = self.execute(task["task_id"], context["context_token"])
            if result["status"] != "completed":
                error = KernelError(result["error_code"] or "OPERATION_NOT_COMPLETED", "请求未完成，请核对当前工作区状态。")
                error.trace_id = result["task_id"]
                raise error
            return result
        except KernelError as exc:
            if not getattr(exc, "trace_id", None):
                # Unknown names are untrusted input; never echo their content
                # into the audit stream (they may contain credentials).
                record_refusal(tool.name if tool else "<unregistered>", exc)
            raise

    def submit(self, name, payload, key, context, *, internal=False, parent_task_id=None):
        tool = self.router.resolve(name)
        if not internal and tool.kind != "work":
            raise KernelError("INVALID_TOOL_KIND", "只能提交已注册的业务任务。", 400)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise KernelError("INVALID_IDEMPOTENCY_KEY", "缺少有效的任务标识。", 400)
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            note = ""
            if name == "draft.reply":
                payload, note = prepare_draft(payload, context, c)
            result, _ = self._queue(c, tool, payload, key, context, parent_task_id, note)
            if parent_task_id is not None:
                record_commit(c, context, "runtime.task.submit", result)
            return result

    def _queue(self, c, tool, payload, key, context, parent_task_id, note="", conversation_key=""):
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
            encoded.encode('utf-8')
            key.encode('utf-8')
            note.encode('utf-8')
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise KernelError("INVALID_INPUT", "操作参数无法保存，请核对后重试。", 400) from None
        existing = c.execute("SELECT * FROM runtime_tasks WHERE scope_id=? AND idempotency_key=?",
                             (context["scope_id"], key)).fetchone()
        if existing:
            # 会话键也参与幂等比对：同一段消息在两个会话里是两份草稿，不能被当成同一次重试。
            if (existing["tool"] != tool.name or existing["payload"] != encoded or existing["conversation_note"] != note
                    or (existing["conversation_key"] or "") != conversation_key):
                raise KernelError("IDEMPOTENCY_CONFLICT", "同一任务标识不能用于不同内容。")
            return public_task(existing), False
        task_id, now = uuid.uuid4().hex, time.time()
        c.execute("INSERT INTO runtime_tasks(task_id,scope_id,context_token,tool,kind,effect,status,"
                  "idempotency_key,payload,result,error_code,created_at,updated_at,parent_task_id,conversation_note,conversation_key) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (task_id, context["scope_id"], context["context_token"], tool.name, tool.kind,
                   tool.effect, "queued", key, encoded, None, None, now, now, parent_task_id, note, conversation_key))
        store.runtime_event(c, task_id, context["scope_id"], tool.name, "queued")
        return public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone()), True

    def submit_observed_reply(self,payload,context,resolve_source):
        tool=self.router.resolve('draft.reply')
        with store.runtime_db() as c:
            c.execute('BEGIN IMMEDIATE')
            check_context(c,context['context_token'])
            commit_owner(c,context,'runtime.applicant.reply.prepare')
            conversation_key=payload.get('conversation_key',applicant_reply_windows.DEFAULT_KEY)
            if not applicant_reply_windows.valid_key(conversation_key):
                raise KernelError('INVALID_CONVERSATION_KEY','会话键必须是 1..128 个不含空白的可见字符；不要用显示名，同名 HR 会串。',400)
            binding,text=resolve_source(c,context,payload)
            if llm_draft.configuration().backend!='rules':
                raise KernelError('OBSERVED_REPLY_RULES_REQUIRED','当前消息接入仅支持本地规则草稿，请明确配置本地规则模式；不会外发 HR 消息给模型。')
            prepared,note=prepare_draft({'message':text,'conversation_note':binding['party']['display_name']},context,c)
            if prepared.get('draft_backend','rules')!='rules':
                raise KernelError('OBSERVED_REPLY_RULES_REQUIRED','草稿后端已变化，未准备平台消息草稿。')
            references=[applicant_reply.reference(c,context['scope_id'],m['message_id'],conversation_key) for m in binding['messages']]
            digest=applicant_reply.fingerprint(binding)
            if any(references):
                if any(ref is None for ref in references) or len(set(references))!=1:
                    raise KernelError('REPLY_MESSAGE_OVERLAP','这批消息与已有回复意图部分重叠，请先人工核对已有草稿，不能重复准备。')
                intent,row=applicant_reply.read(c,context['scope_id'],references[0])
                if intent['fingerprint']!=digest or intent['conversation_key']!=conversation_key:
                    raise KernelError('REPLY_INTENT_CONFLICT','同一消息已有不同资料、会话或内容依据的回复意图，请人工核对，不会覆盖或重复生成。')
                task=public_task(row)
            else:
                # 旧默认会话的种子保持原样，升级前已排队的草稿仍按同一标识复用；
                # 其他会话把键掺进种子，于是同一条消息在两个会话里各得一份草稿。
                seed=([context['scope_id'],[m['message_id'] for m in binding['messages']]]
                      if conversation_key==applicant_reply_windows.DEFAULT_KEY
                      else [context['scope_id'],conversation_key,[m['message_id'] for m in binding['messages']]])
                key='observed-reply:'+hashlib.sha256(json.dumps(seed).encode()).hexdigest()
                task,created=self._queue(c,tool,prepared,key,context,context['task_id'],note,conversation_key)
                if not created:
                    raise KernelError('INVALID_REPLY_INTENT_STATE','已有草稿缺少完整消息关联，未重复准备。')
                identity=uuid.uuid4().hex
                intent={**binding,'intent_id':identity,'draft_task_id':task['task_id'],
                        'source_observation_task_id':payload['message_observation_task_id'],'fingerprint':digest,'state':'prepared_unsent',
                        'conversation_key':conversation_key,
                        'recipient_verified':False,'recency_verified':False,'automation_ready':False,'sent':False,'sending_enabled':False,'quota_consumed':False}
                c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)',(context['scope_id'],'applicant_reply_intent:'+identity,1,json.dumps(intent,ensure_ascii=False)))
                for message in binding['messages']:
                    reference={'intent_id':identity,'message_id':message['message_id'],'conversation_key':conversation_key}
                    c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)',(context['scope_id'],applicant_reply.message_key(message['message_id'],conversation_key),1,json.dumps(reference,ensure_ascii=False)))
            return record_commit(c,context,'runtime.applicant.reply.prepare',{'intent':intent,'task':task,'fresh_observation_task_id':payload['message_observation_task_id']})

    def submit_batch(self, payload, context):
        items, key = payload.get("items"), payload.get("idempotency_key")
        if (set(payload) - {"items", "idempotency_key"} or not isinstance(items, list) or not 1 <= len(items) <= 50
                or not isinstance(key, str) or not key.strip() or len(key) > 128):
            raise KernelError("INVALID_BATCH", "每批须包含 1 到 50 条消息和有效的批次标识。", 400)
        if any(not isinstance(item, dict) or set(item) != {"message", "conversation_note"}
               or not isinstance(item["conversation_note"], str) or not item["conversation_note"].strip() for item in items):
            raise KernelError("INVALID_BATCH", "每条消息必须填写会话备注和对方原话。", 400)
        tool = self.router.resolve("draft.reply")
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            prepared = [prepare_draft(item, context, c) for item in items]
            digest = hashlib.sha256(key.encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps(prepared, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            manifest = c.execute("SELECT fingerprint FROM runtime_manual_batches WHERE scope_id=? AND batch_key_digest=?",
                                 (context["scope_id"], digest)).fetchone()
            if manifest and manifest[0] != fingerprint:
                raise KernelError("IDEMPOTENCY_CONFLICT", "该批次内容或资料版本已变化，请核对后使用新批次。")
            c.execute("INSERT OR IGNORE INTO runtime_manual_batches VALUES (?,?,?)", (context["scope_id"], digest, fingerprint))
            prefix = "manual-batch:" + digest
            tasks, created = [], 0
            for index, (draft, note) in enumerate(prepared):
                task, is_new = self._queue(c, tool, draft, prefix + ":" + str(index), context, context["task_id"], note)
                tasks.append(task)
                created += int(is_new)
            result = {"tasks": tasks, "created": created, "reused": len(tasks) - created}
            return record_commit(c, context, "runtime.task.batch.submit", result)

    def _batch_document(self, c, context, expected):
        check_context(c, context["context_token"])
        kind = "facts" if context["role"] == "recruiter" else "profile"
        try:
            document = store.runtime_document(c, context["scope_id"], kind)
            if not isinstance(document["content"], dict):
                raise ValueError("invalid document")
        except (TypeError, ValueError, RecursionError):
            raise KernelError("INVALID_DOCUMENT_STATE", "已保存资料无法读取，请人工核对资料。") from None
        if document["revision"] < 1:
            raise KernelError("DOCUMENT_REQUIRED", "请先保存已确认资料，再批量生成草稿。")
        if document["revision"] != expected:
            raise KernelError("STALE_FACTS", "资料已更新，批量处理已停止，请刷新核对。")

    def _batch_queued(self, c, task_id, context, expected):
        row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?", (task_id, context["scope_id"])).fetchone()
        task = public_task(row)
        if (row["kind"] != "work" or row["tool"] != "draft.reply" or row["effect"] != "none"
                or row["status"] != "queued" or row["review_status"] != "pending"
                or row["superseded_by_task_id"] is not None or task.get("input_state") != "readable"
                or task.get("document_revision") != expected):
            raise KernelError("BATCH_NOT_READY", "选中的记录已变化或不能批量起草，请刷新清单。")

    def execute_batch(self, payload, context):
        ids, expected = payload.get("task_ids"), payload.get("expected_document_revision")
        if (set(payload) != {"task_ids", "expected_document_revision"} or not isinstance(ids, list)
                or not 1 <= len(ids) <= 10 or type(expected) is not int
                or any(not isinstance(task_id, str) or len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id) for task_id in ids)
                or len(set(ids)) != len(ids)):
            raise KernelError("INVALID_BATCH", "请选择 1 到 10 条不同的待起草消息，并核对资料版本。", 400)
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._batch_document(c, context, expected)
            for task_id in ids:
                self._batch_queued(c, task_id, context, expected)
            store.runtime_event(c, context["task_id"], context["scope_id"], "runtime.task.batch.execute", "planned")
        tasks, stopped = [], None
        for task_id in ids:
            task = None
            try:
                with store.runtime_db() as c:
                    self._batch_document(c, context, expected)
                    self._batch_queued(c, task_id, context, expected)
                task = self.execute(task_id, context["context_token"], work_only=True)
            except KernelError as error:
                stopped = error.code
                with store.runtime_db() as c:
                    row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?", (task_id, context["scope_id"])).fetchone()
                    if row is not None and row["status"] in TERMINAL:
                        task = public_task(row)
            if task is not None and task["status"] in TERMINAL:
                tasks.append(task)
                if task["status"] != "completed":
                    stopped = stopped or task["error_code"] or "BATCH_STOPPED"
            if stopped:
                break
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                self._batch_document(c, context, expected)
            except KernelError as error:
                stopped = stopped or error.code
            result = batch_outcome(ids, tasks, stopped)
            return record_commit(c, context, "runtime.task.batch.execute", result)

    def review(self, payload, context):
        if (set(payload) - {"conversation_key"} != {"task_id", "expected_review_revision", "handled"}
                or not isinstance(payload["task_id"], str) or len(payload["task_id"]) > 128
                or type(payload["expected_review_revision"]) is not int or type(payload["handled"]) is not bool):
            raise KernelError("INVALID_REVIEW", "处理标记参数不正确。", 400)
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?",
                            (payload["task_id"], context["scope_id"])).fetchone()
            public_task(row)
            check_conversation_key(row, payload.get("conversation_key"))
            if row["kind"] != "work" or row["tool"] != "draft.reply" or row["status"] not in TERMINAL:
                raise KernelError("REVIEW_NOT_READY", "请先生成草稿或取消任务，再标记人工处理进度。")
            if row["superseded_by_task_id"] is not None or row["review_status"] == "superseded":
                raise KernelError("TASK_SUPERSEDED", "此记录已有替代草稿，请处理新的记录。")
            if row["review_revision"] != payload["expected_review_revision"]:
                raise KernelError("STALE_REVIEW", "处理标记已变化，请重新载入记录。")
            c.execute("UPDATE runtime_tasks SET review_status=?,review_revision=review_revision+1,updated_at=? WHERE task_id=?",
                      ("handled" if payload["handled"] else "pending", time.time(), row["task_id"]))
            store.runtime_event(c, row["task_id"], context["scope_id"], row["tool"], "reviewed", "MANUALLY_HANDLED" if payload["handled"] else "REOPENED")
            task = public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (row["task_id"],)).fetchone())
            return record_commit(c, context, "runtime.task.review", task)

    def replace_draft(self, payload, context):
        if (set(payload) - {"conversation_key"} != {"task_id", "expected_review_revision", "expected_document_revision"}
                or type(payload["expected_review_revision"]) is not int or type(payload["expected_document_revision"]) is not int):
            raise KernelError("INVALID_REPLACEMENT", "请核对记录和当前资料版本后重建草稿。", 400)
        with task_lease(payload["task_id"]), store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?", (payload["task_id"], context["scope_id"])).fetchone()
            source = public_task(row)
            conversation_key = check_conversation_key(row, payload.get("conversation_key"))
            if row["kind"] != "work" or row["tool"] != "draft.reply" or row["effect"] != "none" or source.get("input_state") != "readable":
                raise KernelError("INVALID_REPLACEMENT", "只能重建当前工作区内原话完整的本地草稿任务。")
            draft, note = prepare_draft({"message": source["message"], "conversation_note": row["conversation_note"]}, context, c)
            if draft["document_revision"] != payload["expected_document_revision"]:
                raise KernelError("STALE_DOCUMENT", "资料版本已变化，请刷新清单后重建。")
            if row["superseded_by_task_id"] is not None:
                successor = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?", (row["superseded_by_task_id"], context["scope_id"])).fetchone()
                task = public_task(successor)
                if (row["review_status"] != "superseded" or row["review_revision"] != payload["expected_review_revision"] + 1
                        or task.get("supersedes_task_id") != row["task_id"] or task.get("document_revision") != draft["document_revision"]):
                    raise KernelError("STALE_REVIEW", "记录已被更新，请刷新清单后查看替代任务。")
                return record_commit(c, context, "runtime.task.replace", task)
            if row["review_revision"] != payload["expected_review_revision"] or row["review_status"] != "pending":
                raise KernelError("STALE_REVIEW", "记录已处理或已变化，请刷新清单。")
            if row["status"] not in {"queued", "completed", "failed", "blocked", "cancelled"}:
                raise KernelError("TASK_BUSY", "请先核对正在执行或结果未知的任务，不能直接重建。")
            key = f"replacement:{row['task_id']}:{row['review_revision']}:{draft['document_revision']}"
            # 替代草稿继承原会话键：重建不能把 A 会话的草稿悄悄变成默认会话的。
            task, created = self._queue(c, self.router.resolve("draft.reply"), draft, key, context, context["task_id"], note,
                                        "" if conversation_key == applicant_reply_windows.DEFAULT_KEY else conversation_key)
            if not created:
                raise KernelError("REPLACEMENT_CONFLICT", "替代任务标识冲突，请人工核对记录。")
            c.execute("UPDATE runtime_tasks SET supersedes_task_id=? WHERE task_id=?", (row["task_id"], task["task_id"]))
            c.execute("UPDATE runtime_tasks SET review_status='superseded',review_revision=review_revision+1,superseded_by_task_id=?,updated_at=? WHERE task_id=?",
                      (task["task_id"], time.time(), row["task_id"]))
            if row["status"] == "queued":
                c.execute("UPDATE runtime_tasks SET status='cancelled',error_code='SUPERSEDED' WHERE task_id=?", (row["task_id"],))
                store.runtime_event(c, row["task_id"], context["scope_id"], row["tool"], "cancelled", "SUPERSEDED")
            store.runtime_event(c, row["task_id"], context["scope_id"], row["tool"], "superseded", "REPLACED")
            task = public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task["task_id"],)).fetchone())
            return record_commit(c, context, "runtime.task.replace", task)

    def edit_draft(self, payload, context):
        if (set(payload) - {"conversation_key"} != {"task_id", "text", "confirmed", "expected_manual_revision", "expected_document_revision"}
                or not isinstance(payload["task_id"], str) or not isinstance(payload["text"], str)
                or not payload["text"].strip() or len(payload["text"]) > 8000 or payload["confirmed"] is not True
                or type(payload["expected_manual_revision"]) is not int or payload["expected_manual_revision"] < 0
                or type(payload["expected_document_revision"]) is not int):
            raise KernelError("INVALID_MANUAL_DRAFT", "请填写不超过 8000 字的人工稿，核对并确认后保存。", 400)
        with task_lease(payload["task_id"]), store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?", (payload["task_id"], context["scope_id"])).fetchone()
            task = public_task(row)
            check_conversation_key(row, payload.get("conversation_key"))
            if (row["kind"] != "work" or row["tool"] != "draft.reply" or row["status"] != "completed"
                    or row["review_status"] != "pending" or row["superseded_by_task_id"] is not None
                    or not isinstance(task["result"], dict) or not isinstance(task["result"].get("text"), str)
                    or not task["result"]["text"].strip() or type(task["result"].get("document_revision")) is not int):
                raise KernelError("EDIT_NOT_AVAILABLE", "仅可修改待处理、已完成且未被替换的文字草稿。")
            self._batch_document(c, context, payload["expected_document_revision"])
            if (task.get("document_revision") != payload["expected_document_revision"]
                    or task["result"].get("document_revision") != payload["expected_document_revision"]):
                raise KernelError("STALE_FACTS", "资料已更新，请先重建草稿后修改。")
            text, expected = payload["text"].strip(), payload["expected_manual_revision"]
            check_manual_draft(c, row)
            if row["manual_revision"] == expected + 1 and row["manual_text"] == text:
                return record_commit(c, context, "runtime.task.edit", task)
            if row["manual_revision"] != expected:
                raise KernelError("STALE_MANUAL_DRAFT", "人工稿已被更新，请刷新记录后核对，不能覆盖。")
            c.execute("INSERT INTO runtime_draft_edits VALUES (?,?,?,?,?,?,?)", (row["task_id"], context["scope_id"], expected + 1,
                      payload["expected_document_revision"], text, context["task_id"], time.time()))
            c.execute("UPDATE runtime_tasks SET manual_text=?,manual_revision=?,updated_at=? WHERE task_id=?", (text, expected + 1, time.time(), row["task_id"]))
            store.runtime_event(c, row["task_id"], context["scope_id"], row["tool"], "edited", "MANUALLY_CONFIRMED")
            task = public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (row["task_id"],)).fetchone())
            return record_commit(c, context, "runtime.task.edit", task)

    def copy_draft(self, task_id, context, conversation_key=None):
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?", (task_id, context["scope_id"])).fetchone()
            task = public_task(row)
            check_conversation_key(row, conversation_key)
            if row["superseded_by_task_id"] is not None or row["review_status"] == "superseded":
                raise KernelError("TASK_SUPERSEDED", "此草稿已有替代记录，请使用新的草稿。")
            if (row["kind"] != "work" or row["tool"] != "draft.reply" or row["status"] != "completed"
                    or not isinstance(task["result"], dict) or not isinstance(task["result"].get("text"), str) or not task["result"]["text"]):
                raise KernelError("COPY_NOT_AVAILABLE", "此记录还没有可复制的完成草稿。")
            kind = "facts" if context["role"] == "recruiter" else "profile"
            document = store.runtime_document(c, context["scope_id"], kind)
            if not isinstance(document["content"], dict):
                raise KernelError("INVALID_DOCUMENT_STATE", "已保存资料无法读取，请人工核对资料。")
            if type(task["result"].get("document_revision")) is not int or task["result"].get("document_revision") != document["revision"]:
                raise KernelError("STALE_FACTS", "资料已更新，请重建草稿后复制。")
            check_manual_draft(c, row)
            return {"task_id": row["task_id"], "text": row["manual_text"] if row["manual_revision"] else task["result"]["text"],
                    "manual_revision": row["manual_revision"], "document_revision": document["revision"]}

    def draft_edits(self, task_id, context, conversation_key=None):
        with store.runtime_db() as c:
            check_context(c, context["context_token"])
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?", (task_id, context["scope_id"])).fetchone()
            task = public_task(row)
            check_conversation_key(row, conversation_key)
            if row["tool"] != "draft.reply" or row["kind"] != "work":
                raise KernelError("EDIT_NOT_AVAILABLE", "此记录不属于文字草稿。")
            rows = c.execute("SELECT revision,document_revision,text,created_at FROM runtime_draft_edits WHERE task_id=? AND scope_id=? ORDER BY revision DESC LIMIT 20", (task_id, context["scope_id"])).fetchall()
            total = c.execute("SELECT count(*) FROM runtime_draft_edits WHERE task_id=? AND scope_id=?", (task_id, context["scope_id"])).fetchone()[0]
            return {"task_id": task["task_id"], "total": total, "edits": [dict(edit) for edit in rows]}

    def execute(self, task_id, context_token, *, work_only=False, receipt_owner=None):
        with task_lease(task_id):
            return self._execute(task_id, context_token, work_only=work_only, receipt_owner=receipt_owner)

    def _execute(self, task_id, context_token, *, work_only=False, receipt_owner=None):
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            context = check_context(c, context_token)
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?",
                            (task_id, context["scope_id"])).fetchone()
            task = public_task(row)
            if work_only and row["kind"] != "work":
                raise KernelError("CONTROL_NOT_EXECUTABLE", "设置操作不能通过业务任务入口重新执行，请核对提交结果。")
            if receipt_owner is not None:
                owner = commit_owner(c, receipt_owner, "runtime.task.execute")
                try:
                    valid = (work_only and receipt_owner["scope_id"] == context["scope_id"]
                             and receipt_owner["context_token"] == context_token
                             and json.loads(owner["payload"]).get("task_id") == task_id)
                except (TypeError, ValueError, AttributeError):
                    valid = False
                if not valid:
                    raise KernelError("INVALID_COMMIT_OWNER", "执行请求与目标任务不匹配，未开始执行。", 500)
            if task["status"] in TERMINAL:
                if receipt_owner is not None:
                    record_commit(c, receipt_owner, "runtime.task.execute", task)
                return task
            if task["status"] != "queued":
                raise KernelError("TASK_BUSY", "任务正在处理，不能重复领取。")
            if row["context_token"] != context_token:
                raise KernelError("STALE_TASK", "该任务属于旧的执行上下文。")
            try:
                tool = self.router.resolve(row["tool"])
                authorize(tool, context, context_token)
                try:
                    payload = json.loads(row["payload"])
                    if not isinstance(payload, dict):
                        raise ValueError("not an object")
                    if tool.name == "draft.reply":
                        expected_kind = "facts" if context["role"] == "recruiter" else "profile"
                        if (set(payload) not in ({"message", "document_revision", "document_kind"},
                                                 {"message", "document_revision", "document_kind", "draft_backend"},
                                                 {"message", "document_revision", "document_kind", "draft_backend", "llm_binding"})
                                or ('draft_backend' in payload and payload['draft_backend'] != 'llm')
                                or ('llm_binding' in payload and (not isinstance(payload['llm_binding'], str)
                                    or len(payload['llm_binding']) != 64
                                    or any(ch not in '0123456789abcdef' for ch in payload['llm_binding'])))
                                or payload["document_kind"] != expected_kind
                                or type(payload["document_revision"]) is not int
                                or not isinstance(payload["message"], str)
                                or not payload["message"].strip() or len(payload["message"]) > 8000):
                            raise ValueError("invalid draft snapshot")
                except (TypeError, ValueError, RecursionError):
                    raise KernelError("INVALID_TASK_PAYLOAD", "任务保存的数据无法使用，请核对后新建任务。") from None
                if tool.name == "draft.reply":
                    try:
                        document = store.runtime_document(c, context["scope_id"], payload["document_kind"])
                        if not isinstance(document["content"], dict):
                            raise ValueError("invalid document")
                    except (TypeError, ValueError, RecursionError):
                        raise KernelError("INVALID_DOCUMENT_STATE", "已保存资料无法读取，请人工核对资料。") from None
            except KernelError as exc:
                c.execute("UPDATE runtime_tasks SET status='blocked',error_code=?,updated_at=? WHERE task_id=?",
                          (exc.code, time.time(), task_id))
                store.runtime_event(c, task_id, context["scope_id"], row["tool"], "blocked", exc.code)
                c.commit()
                exc.trace_id = task_id
                raise
            if tool.name == "draft.reply":
                if document["revision"] != payload["document_revision"]:
                    c.execute("UPDATE runtime_tasks SET status='blocked',error_code='STALE_FACTS',updated_at=? WHERE task_id=?",
                              (time.time(), task_id))
                    store.runtime_event(c, task_id, context["scope_id"], tool.name, "blocked", "STALE_FACTS")
                    task = public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone())
                    if receipt_owner is not None:
                        record_commit(c, receipt_owner, "runtime.task.execute", task)
                    return task
            c.execute("UPDATE runtime_tasks SET status='executing',updated_at=? WHERE task_id=?", (time.time(), task_id))
            store.runtime_event(c, task_id, context["scope_id"], tool.name, "executing")
        context = {**context, "task_id": task_id}
        error = None
        try:
            with capability(tool, context):
                result = tool.handler(payload, context)
            try:
                serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
                serialized.encode('utf-8')
                result = json.loads(serialized)
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise KernelError("INVALID_TOOL_RESULT", "任务返回的数据无法保存，请人工核对处理结果。", 500) from None
            status, code = "completed", None
        except KernelError as exc:
            status = "blocked"
            if exc.outcome_unknown and tool.effect in {'local_ui','external_write'}:
                status = 'outcome_unknown'
            if exc.code == "INVALID_TOOL_RESULT":
                status = "failed" if tool.effect == "none" else "outcome_unknown"
            error, result, code = exc, None, exc.code
        except Exception as exc:
            # Exception text/traceback may contain private tool inputs. The
            # persistent task trace supplies correlation without logging them.
            LOG.error("Kernel tool failed: trace_id=%s tool=%s exception_type=%s", task_id, tool.name, type(exc).__name__)
            error = KernelError("TOOL_FAILED", "任务处理异常，请人工核对后再决定下一步。", 500)
            result, status, code = None, ("failed" if tool.effect == "none" else "outcome_unknown"), error.code
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            current = store.runtime_context(c)
            current_status = c.execute("SELECT status FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            if current_status == "cancelled":
                status, result, code = "cancelled", None, "CANCELLED"
                if error is not None and error.code == 'CANCELLED':
                    error = None  # Expected cooperative stop, not a tool failure.
                elif error is not None:
                    store.runtime_event(c, task_id, context['scope_id'], tool.name, 'cancellation_error', error.code)
            if current["context_token"] != context_token and not tool.changes_context:
                # A committed control change remains a completed fact in its old
                # namespace; only the response is fenced. Pure work is cancelled.
                if not (tool.kind == "control" and tool.effect in {"state", "local_file", "local_ui"} and status in {"completed", "outcome_unknown"}):
                    status, result, code = "cancelled", None, "STALE_CONTEXT"
                error = KernelError("STALE_CONTEXT", "身份已切换，旧工作区的结果不会显示。")
            if tool.name == "draft.reply" and status != 'cancelled':
                try:
                    document = store.runtime_document(c, context["scope_id"], payload["document_kind"])
                    if not isinstance(document["content"], dict):
                        raise ValueError("invalid document")
                except (TypeError, ValueError, RecursionError):
                    status, result, code = "blocked", None, "INVALID_DOCUMENT_STATE"
                    error = KernelError(code, "已保存资料无法读取，请人工核对资料。")
                else:
                    if document["revision"] != payload["document_revision"]:
                        status, result, code = "blocked", None, "STALE_FACTS"
            c.execute("UPDATE runtime_tasks SET status=?,result=?,error_code=?,updated_at=? WHERE task_id=?",
                      (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                       code, time.time(), task_id))
            store.runtime_event(c, task_id, context["scope_id"], tool.name, status, code)
            task = public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone())
            if receipt_owner is not None and error is None:
                record_commit(c, receipt_owner, "runtime.task.execute", task)
        if error:
            error.trace_id = task_id
            raise error
        return task

    def read(self, task_id, context):
        with store.runtime_db() as c:
            check_context(c, context["context_token"])
            return public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?",
                                         (task_id, context["scope_id"])).fetchone())

    def cancel(self, task_id, context):
        with store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?",
                            (task_id, context["scope_id"])).fetchone()
            task = public_task(row)
            if row["kind"] != "work":
                raise KernelError("CONTROL_NOT_CANCELLABLE", "控制操作不能作为业务任务取消。")
            if row["effect"] == "external_write" and task["status"] == "executing":
                raise KernelError("EXECUTION_IN_FLIGHT", "外部动作正在执行，需等待核对结果。")
            if task["status"] not in TERMINAL:
                c.execute("UPDATE runtime_tasks SET status='cancelled',error_code='CANCELLED',updated_at=? WHERE task_id=?", (time.time(), task_id))
                store.runtime_event(c, task_id, context["scope_id"], row["tool"], "cancelled", 'CANCELLED')
            result = public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone())
            if context.get("task_id"):
                record_commit(c, context, "runtime.task.cancel", result)
            return result

    def recover(self, task_id, context):
        """Reconcile abandoned tasks from atomic receipts; never replay handlers."""
        with task_lease(task_id), store.runtime_db() as c:
            c.execute("BEGIN IMMEDIATE")
            check_context(c, context["context_token"])
            row = c.execute("SELECT * FROM runtime_tasks WHERE task_id=? AND scope_id=?",
                            (task_id, context["scope_id"])).fetchone()
            task = public_task(row)
            queued_control = row["kind"] == "control" and task["status"] == "queued"
            recoverable = queued_control or task["status"] == "executing" or (row["kind"] == "control" and task["status"] == "outcome_unknown")
            if recoverable:
                result = None
                if queued_control:
                    status, code = ("cancelled", "NOT_STARTED") if row["commit_receipt"] is None else ("outcome_unknown", "INVALID_TASK_STATE")
                elif row["kind"] == "control" and row["effect"] == "state" and row["commit_receipt"] is not None:
                    try:
                        result = committed_result(row, c)
                        status, code = "completed", "RECOVERED_COMMIT"
                    except KernelError as exc:
                        status, code = "outcome_unknown", exc.code
                else:
                    uncertain = row["effect"] != "none"
                    status, code = ("outcome_unknown", "OUTCOME_UNKNOWN") if uncertain else ("failed", "INTERRUPTED")
                if (status, code) != (row["status"], row["error_code"]):
                    c.execute("UPDATE runtime_tasks SET status=?,result=?,error_code=?,updated_at=? WHERE task_id=?",
                              (status, json.dumps(result, ensure_ascii=False) if result is not None else None, code, time.time(), task_id))
                    store.runtime_event(c, task_id, context["scope_id"], row["tool"], status, code)
                task = public_task(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone())
            if context.get("task_id"):
                record_commit(c, context, "runtime.task.recover", task)
            return task
