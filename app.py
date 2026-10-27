#!/usr/bin/env python3
"""jobkit 本地 HTTP 展示与控制适配器；业务执行委托唯一内核。

启动：
    python main.py serve                # http://127.0.0.1:18770
    python main.py serve --port 9000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import apply_pipeline as ap  # noqa: E402
import boss_session  # noqa: E402  # 壳子登录态 → session.enc 的桥
import greet_generator as gg  # noqa: E402
import ledger  # noqa: E402
import rules  # noqa: E402  # 投递白名单筛选（/api/delivery/plan）
import settings  # noqa: E402  # 风险体检要读限额
import store  # noqa: E402
import resume_optimize as ro  # noqa: E402
from store import connect  # noqa: E402

HERE = Path(__file__).parent
WEB = HERE / "web" / "index.html"
WORKSPACE_WEB = HERE / "web" / "workspace.html"
CONTROLLED_CHAT_WEB = HERE / "web" / "controlled_chat.html"
DEFAULT_RESUME = Path(r"C:\面试\Resume.md")   # 老的硬编码路径，仅作兼容导入用

# ── 运行版本身份（G6）────────────────────────────────────────
# 目的：回答「运行中的进程加载的是哪份代码」。磁盘上的代码测试通过 ≠ 运行版本正确。
APP_VERSION = "0.1.0"   # 与 shell/package.json 保持一致；真正的运行版本以 build_fingerprint 为准
# 参与指纹的文件集固定；改动这份列表本身就是一次指纹变化（app.py 在集内）。
FINGERPRINT_FILES = (
    "main.py", "app.py", "runtime_tools.py", "execution_engine.py",
    "sandbox.py", "tool_router.py", "store.py", "web/workspace.html",
)
try:
    _LOCAL_TZ = timezone(timedelta(hours=8), "UTC+08:00")   # 口径固定 UTC+8，不跟主机时区漂移
except ValueError:   # pragma: no cover - 纯防御，timezone 不会因参数失败
    _LOCAL_TZ = timezone(timedelta(hours=8))
_STARTED_AT = datetime.now(_LOCAL_TZ)


def _file_digest(path: Path) -> str | None:
    """读不到就当 missing：版本接口绝不因为一个文件缺失而崩掉。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def compute_build_fingerprint(root: Path | str | None = None) -> tuple[str, dict[str, str]]:
    """按固定文件集汇总指纹，返回 (sha256 全文, {文件名: 前 12 位})。

    指纹材料按 FINGERPRINT_FILES 的固定顺序拼接（"名字:全文哈希"），
    与文件所在路径无关，因此拷贝到别处算出的值可直接对比。
    """
    base = Path(root) if root is not None else HERE
    digests = {name: _file_digest(base / name) for name in FINGERPRINT_FILES}
    material = "\n".join(f"{name}:{digests[name] or 'missing'}" for name in FINGERPRINT_FILES)
    short = {name: (digests[name][:12] if digests[name] else "missing") for name in FINGERPRINT_FILES}
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), short


def _read_git_commit() -> str | None:
    """git 不可用（没装 git / 不是工作区 / 超时）一律返回 None，绝不抛异常。"""
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(HERE), capture_output=True,
                              text=True, errors="replace", timeout=5, check=False,
                              env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    value = (done.stdout or "").strip()
    return value if done.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40}([0-9a-fA-F]{24})?", value) else None


def _compose_identity(root: Path | str | None = None) -> dict:
    fingerprint, files = compute_build_fingerprint(root)
    return {
        "app_version": APP_VERSION,
        "started_at": _STARTED_AT.isoformat(timespec="seconds"),
        "build_fingerprint": fingerprint,
        "files": files,
        "git_commit": _read_git_commit(),
        "python_version": platform.python_version(),
    }


# 进程启动时算一次并缓存；/version 只读缓存，不每次请求重算全盘。
_BUILD_IDENTITY = _compose_identity()


def build_identity_snapshot() -> dict:
    """当前进程的版本身份快照（深拷贝，调用方改不动缓存）。"""
    identity = dict(_BUILD_IDENTITY)
    identity["files"] = dict(identity["files"])
    return identity

# ── 简历存储（面向一般用户：粘贴/上传，不要求用户把文件放到某个固定路径）──
RESUME_DIR = HERE / "data"
RESUME_FILE = RESUME_DIR / "resume.txt"
RESUME_META = RESUME_DIR / "resume.json"


def _resume_meta() -> dict:
    if RESUME_META.exists():
        try:
            return json.loads(RESUME_META.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def load_resume() -> str:
    """读当前简历内容。没设置返回空串。

    优先级：用户设置的 > 旧的硬编码路径（兼容老用户）。
    ⚠️ 但用户如果显式点过「清除」，就不再回退到旧路径 ——
    否则那个按钮看起来毫无作用（实测踩过：清完 source 立刻变回 legacy）。
    刻意不做缓存：用户改完立刻生效，不用重启。
    """
    for p in (RESUME_FILE, DEFAULT_RESUME):
        if p is DEFAULT_RESUME and _resume_meta().get("cleared"):
            continue
        try:
            if p.exists():
                t = p.read_text(encoding="utf-8", errors="replace")
                if t.strip():
                    return t
        except OSError:
            continue
    return ""

app = FastAPI(title="jobkit")

# 桌面和工具页均由本服务提供；API 不再允许 file:// 或外部网页跨源访问。
from local_access import LocalAccessMiddleware  # noqa: E402
app.add_middleware(LocalAccessMiddleware)

from sandbox import KernelError  # noqa: E402


@app.exception_handler(KernelError)
async def kernel_error(request, error):
    return JSONResponse({"ok": False, "code": error.code, "error": str(error),
                         "trace_id": getattr(error, "trace_id", None)}, status_code=error.status)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request, error):
    from sandbox import record_refusal
    refusal = record_refusal("runtime.http.validation", KernelError("INVALID_INPUT", "请求格式不正确，请核对后重试。", 400))
    return await kernel_error(request, refusal)


def runtime_call(request, tool, payload=None):
    from main import dispatch
    return dispatch(tool, payload, request.headers.get("x-jobkit-context"))


@app.get("/api/runtime/context")
def runtime_context_api(request: Request):
    return runtime_call(request, "runtime.context")


@app.post("/api/runtime/context")
def runtime_switch_api(request: Request, payload: dict):
    return runtime_call(request, "runtime.switch", payload)


@app.get("/api/runtime/document")
def runtime_document_api(request: Request):
    return runtime_call(request, "runtime.document.get")


@app.post("/api/runtime/document")
def runtime_document_save_api(request: Request, payload: dict):
    return runtime_call(request, "runtime.document.save", payload)


@app.post("/api/runtime/workspace/export")
def runtime_export_api(request: Request, payload: dict):
    return runtime_call(request, "runtime.workspace.export", payload)


from workspace_records import MAX_BYTES as RECORD_FILE_MAX_BYTES
RESTORE_HTTP_MAX_BYTES = 3 * RECORD_FILE_MAX_BYTES + 4096


async def runtime_restore_request(request: Request, tool: str):
    from sandbox import record_refusal
    from restore_preflight import strict_json
    from starlette.concurrency import run_in_threadpool
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > RESTORE_HTTP_MAX_BYTES:
            raise record_refusal(tool, KernelError('RESTORE_TOO_LARGE', '恢复请求超过容量，未写入数据。', 413))
        chunks.append(chunk)
    raw = b''.join(chunks)
    def check_and_dispatch():
        try:
            payload = strict_json(raw.decode('utf-8'))
        except (KernelError, UnicodeError):
            raise record_refusal(tool, KernelError('INVALID_RESTORE_REQUEST', '恢复请求格式无法核对，未写入数据。', 400)) from None
        return runtime_call(request, tool, payload)
    return await run_in_threadpool(check_and_dispatch)


@app.post('/api/runtime/workspace/restore/preflight')
async def runtime_restore_preflight_api(request: Request):
    return await runtime_restore_request(request, 'runtime.workspace.restore.preflight')


@app.post('/api/runtime/workspace/restore/commit')
async def runtime_restore_commit_api(request: Request):
    return await runtime_restore_request(request, 'runtime.workspace.restore.commit')


@app.get('/api/runtime/workspace/restored')
def runtime_restored_api(request: Request, view: str = 'records'):
    return runtime_call(request, 'runtime.workspace.restored.get', {'view': view})


@app.get("/api/runtime/tasks")
def runtime_tasks_api(request: Request, review: str = "all"):
    return runtime_call(request, "runtime.task.list", {"review": review})


@app.post("/api/runtime/tasks")
def runtime_submit_api(request: Request, payload: dict):
    return runtime_call(request, "runtime.task.submit", payload)


@app.post("/api/runtime/tasks/batch")
def runtime_batch_api(request: Request, payload: dict):
    return runtime_call(request, "runtime.task.batch.submit", payload)


@app.post("/api/runtime/tasks/search")
def runtime_search_api(request: Request, payload: dict):
    return runtime_call(request, "runtime.task.search", payload)


@app.post("/api/runtime/tasks/batch/execute")
def runtime_execute_batch_api(request: Request, payload: dict):
    return runtime_call(request, "runtime.task.batch.execute", payload)


@app.post("/api/runtime/tasks/{task_id}/review")
def runtime_review_api(request: Request, task_id: str, payload: dict):
    return runtime_call(request, "runtime.task.review", {**payload, "task_id": task_id})


@app.post("/api/runtime/tasks/{task_id}/replace")
def runtime_replace_api(request: Request, task_id: str, payload: dict):
    return runtime_call(request, "runtime.task.replace", {**payload, "task_id": task_id})


@app.get("/api/runtime/tasks/{task_id}/copy")
def runtime_copy_api(request: Request, task_id: str):
    return runtime_call(request, "runtime.task.copy.get", {"task_id": task_id})


@app.post("/api/runtime/tasks/{task_id}/edit")
def runtime_edit_api(request: Request, task_id: str, payload: dict):
    return runtime_call(request, "runtime.task.edit", {**payload, "task_id": task_id})


@app.get("/api/runtime/tasks/{task_id}/edits")
def runtime_edits_api(request: Request, task_id: str):
    return runtime_call(request, "runtime.task.edits.get", {"task_id": task_id})


@app.get("/api/runtime/tasks/{task_id}")
def runtime_task_api(request: Request, task_id: str):
    return runtime_call(request, "runtime.task.get", {"task_id": task_id})


@app.post("/api/runtime/tasks/{task_id}/execute")
def runtime_execute_api(request: Request, task_id: str):
    return runtime_call(request, "runtime.task.execute", {"task_id": task_id})


@app.post("/api/runtime/tasks/{task_id}/cancel")
def runtime_cancel_api(request: Request, task_id: str):
    return runtime_call(request, "runtime.task.cancel", {"task_id": task_id})


@app.post("/api/runtime/tasks/{task_id}/recover")
def runtime_recover_api(request: Request, task_id: str):
    return runtime_call(request, "runtime.task.recover", {"task_id": task_id})


@app.get("/api/runtime/events")
def runtime_events_api(request: Request, trace_id: str | None = None):
    return runtime_call(request, "runtime.events", {"trace_id": trace_id} if trace_id is not None else {})


@app.get('/api/runtime/model/usage')
def runtime_model_usage_api(request: Request, cursor: str | None = None):
    return runtime_call(request, 'runtime.model.usage.get', {'cursor': cursor} if cursor is not None else {})


@app.get('/api/runtime/model/readiness')
def runtime_model_readiness_api(request: Request):
    return runtime_call(request, 'runtime.model.readiness.get', {})


@app.get('/api/runtime/applicant/plan')
def runtime_applicant_plan_api(request: Request):
    return runtime_call(request, 'runtime.applicant.plan.get', {})


@app.get('/api/runtime/applicant/channel')
def runtime_applicant_channel_api(request: Request):
    return runtime_call(request, 'runtime.applicant.channel.check', {})


@app.post('/api/runtime/applicant/plan')
def runtime_applicant_plan_save_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.plan.save', payload)


@app.post('/api/runtime/applicant/jobs/preview')
def runtime_applicant_jobs_preview_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.jobs.preview', payload)


@app.post('/api/runtime/applicant/jobs/read')
def runtime_applicant_jobs_read_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.jobs.read', payload)


@app.post('/api/runtime/applicant/jobs/search/get')
def runtime_applicant_jobs_search_get_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.jobs.search.get', payload)


@app.post('/api/runtime/applicant/action/get')
def runtime_applicant_action_get_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.action.get', payload)


@app.post('/api/runtime/applicant/greetings/prepare')
def runtime_applicant_greeting_prepare_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.greeting.prepare', payload)


@app.post('/api/runtime/applicant/identity/read')
def runtime_applicant_identity_read_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.identity.read', payload)


@app.get('/api/runtime/applicant/browser/binding')
def runtime_applicant_binding_get_api(request: Request):
    return runtime_call(request, 'runtime.applicant.browser.binding.get', {})


@app.post('/api/runtime/applicant/browser/binding')
def runtime_applicant_binding_confirm_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.browser.binding.confirm', payload)


@app.post('/api/runtime/applicant/browser/binding/check')
def runtime_applicant_binding_check_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.browser.binding.check', payload)


@app.post('/api/runtime/applicant/conversations/read')
def runtime_applicant_conversations_read_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.conversations.read', payload)


@app.post('/api/runtime/applicant/messages/read')
def runtime_applicant_messages_read_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.messages.read', payload)


@app.post('/api/runtime/applicant/replies/prepare')
def runtime_applicant_reply_prepare_api(request: Request,payload:dict):
    return runtime_call(request,'runtime.applicant.reply.prepare',payload)


@app.get('/api/runtime/applicant/replies/{intent_id}')
def runtime_applicant_reply_get_api(request:Request,intent_id:str):
    return runtime_call(request,'runtime.applicant.reply.get',{'intent_id':intent_id})


@app.get('/api/runtime/applicant/reply-window')
def runtime_reply_window_get_api(request:Request,conversation_key:str|None=None):
    payload={'conversation_key':conversation_key} if conversation_key else {}
    return runtime_call(request,'runtime.applicant.reply.window.get',payload)


@app.post('/api/runtime/applicant/reply-window/start')
def runtime_reply_window_start_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.reply.window.start',payload)


@app.post('/api/runtime/applicant/reply-window/observe')
def runtime_reply_window_observe_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.reply.window.observe',payload)


@app.get('/api/runtime/applicant/chat/outcome')
def runtime_chat_outcome_get_api(request:Request):
    return runtime_call(request,'runtime.applicant.chat.outcome.get',{})


@app.post('/api/runtime/applicant/chat/outcome/verify')
def runtime_chat_outcome_verify_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.chat.outcome.verify',payload)


@app.post('/api/runtime/applicant/chat/outcome/resolve')
def runtime_chat_outcome_resolve_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.chat.outcome.resolve',payload)


@app.post('/api/runtime/applicant/chat/outcome/archive')
def runtime_chat_outcome_archive_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.chat.outcome.archive',payload)


@app.get('/api/runtime/run')
def runtime_run_get_api(request:Request):
    return runtime_call(request,'runtime.run.get',{})


@app.post('/api/runtime/run/pause')
def runtime_run_pause_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.run.pause',payload)


@app.post('/api/runtime/run/resume')
def runtime_run_resume_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.run.resume',payload)


@app.post('/api/runtime/applicant/replies/{intent_id}/preflight')
def runtime_reply_preflight_api(request:Request,intent_id:str,payload:dict):
    if set(payload)!={'expected_manual_revision'}:
        from sandbox import KernelError
        raise KernelError('INVALID_REPLY_PREFLIGHT','请核对当前草稿修订版本。',400)
    return runtime_call(request,'runtime.applicant.reply.preflight',{'intent_id':intent_id,**payload})


@app.post('/api/runtime/applicant/send/recipient/confirm')
def runtime_send_recipient_confirm_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.recipient.confirm',payload)


@app.get('/api/runtime/applicant/send/recipient/{intent_id}')
def runtime_send_recipient_get_api(request:Request,intent_id:str):
    return runtime_call(request,'runtime.applicant.send.recipient.get',{'intent_id':intent_id})


@app.post('/api/runtime/applicant/send/reserve')
def runtime_send_reserve_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.reserve',payload)


@app.post('/api/runtime/applicant/send/release')
def runtime_send_release_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.release',payload)


@app.post('/api/runtime/applicant/send/readback')
def runtime_send_readback_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.readback',payload)


@app.get('/api/runtime/applicant/send/stats')
def runtime_send_stats_api(request:Request):
    return runtime_call(request,'runtime.applicant.send.stats',{})


@app.get('/api/runtime/applicant/send/queue')
def runtime_send_queue_api(request:Request):
    return runtime_call(request,'runtime.applicant.send.queue.get',{})


@app.post('/api/runtime/applicant/send/queue/handoff')
def runtime_send_queue_handoff_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.queue.handoff',payload)


@app.get('/api/runtime/applicant/send/{attempt_id}')
def runtime_send_get_api(request:Request,attempt_id:str):
    return runtime_call(request,'runtime.applicant.send.get',{'attempt_id':attempt_id})


@app.post('/api/runtime/applicant/send/handoff')
def runtime_send_handoff_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.handoff',payload)


@app.get('/api/runtime/applicant/send/handoff/{attempt_id}')
def runtime_send_handoff_get_api(request:Request,attempt_id:str):
    return runtime_call(request,'runtime.applicant.send.handoff.get',{'attempt_id':attempt_id})


@app.post('/api/runtime/applicant/send/manual/claim')
def runtime_send_manual_claim_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.manual.claim',payload)


@app.post('/api/runtime/applicant/send/readback/commit')
def runtime_send_readback_commit_api(request:Request,payload:dict):
    return runtime_call(request,'runtime.applicant.send.readback.commit',payload)


@app.post('/api/runtime/applicant/salaries/read')
def runtime_applicant_salary_read_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.salary.read', payload)


@app.post('/api/runtime/applicant/salaries/confirm')
def runtime_applicant_salary_confirm_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.salary.confirm', payload)


@app.post('/api/runtime/applicant/jobs/confirmed-preview')
def runtime_applicant_confirmed_preview_api(request: Request, payload: dict):
    return runtime_call(request, 'runtime.applicant.jobs.confirmed.preview', payload)


@app.get('/api/runtime/applicant/greetings/{job_id}')
def runtime_applicant_greeting_get_api(request: Request, job_id: str):
    return runtime_call(request, 'runtime.applicant.greeting.get', {'job_id': job_id})


@app.post('/api/runtime/model/calls/{attempt_id}/review')
def runtime_model_review_api(request: Request, attempt_id: str, payload: dict):
    combined = {**payload, 'attempt_id': attempt_id}
    if 'attempt_id' in payload:
        combined['_duplicate_path_id'] = True  # Reject through the kernel, with a trace.
    return runtime_call(request, 'runtime.model.review', combined)


@app.get("/api/runtime/recovery")
def runtime_recovery_list_api(request: Request):
    return runtime_call(request, "runtime.recovery.list")

# ── 投递任务状态（单实例） ────────────────────────────────
_apply_lock = threading.Lock()




@app.get("/", response_class=HTMLResponse)
@app.get("/controlled-chat", response_class=HTMLResponse)
def controlled_chat_page():
    """受控沟通页：本机验收「填入-提交-观察」执行协议，不含平台数据，也不接受任何写操作参数。"""
    return CONTROLLED_CHAT_WEB.read_text(encoding="utf-8")


@app.get("/workspace", response_class=HTMLResponse)
def index():
    return WORKSPACE_WEB.read_text(encoding="utf-8")


@app.get("/legacy", response_class=HTMLResponse)
def legacy_index():
    from sandbox import require_registered
    require_registered("legacy.ui")


@app.get("/health")
def health():
    """Public process identity only: no jobs, configuration or account state."""
    return {"service": "jobkit", "ok": True}


@app.get("/version")
def version():
    """只读运行版本身份：版本号、启动时刻、构建指纹、git commit、Python 版本。

    只暴露文件名 + 12 位短哈希 + 版本号，不含绝对路径、用户名、简历正文或凭据。
    指纹在进程启动时算一次并缓存；拿它和磁盘代码算出来的值对比，
    就能判断运行中的进程是否加载了最新代码（方案 G6）。
    """
    return {"service": "jobkit", "ok": True, **build_identity_snapshot()}


@app.get("/desktop", response_class=HTMLResponse)
def desktop():
    return (HERE / "shell" / "renderer" / "index.html").read_text(encoding="utf-8")


ONLINE_STATES = ("在线", "刚刚活跃", "今日活跃")


def _reason_stats(jobs) -> list[dict]:
    """被过滤掉的原因分布（按条数降序）—— 让用户看清"为什么没投"。

    job_ok 返回的 reject_reason 形如 "经验 5-10年" / "学历 本科"，归并到前缀维度。
    """
    from collections import Counter
    c = Counter()
    for j in jobs:
        if j.get("pass"):
            continue
        r = (j.get("reject_reason") or "其他").strip()
        c[r.split()[0] if r.split() else "其他"] += 1
    return [{"reason": k, "n": v} for k, v in c.most_common(8)]


@app.get("/api/job/{job_id}")
def job_detail(job_id: str, geo: bool = False):
    """岗位详情：一个岗位的六件事放一屏。

    六个子功能各算各的，任何一块失败都只影响自己 —— 通勤要联网，
    不能因为 Nominatim 挂了就让整个详情页 500。

    `geo=false`（默认）只用地理缓存，不联网。前端点「算通勤」再带 geo=true，
    这样打开岗位是瞬时的。
    """
    c = connect()
    try:
        row = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "没有这个岗位"}
        job = dict(row)
        greeted = c.execute(
            "SELECT greeted_at, message FROM greetings WHERE job_id = ?",
            (job_id,)).fetchone()
    finally:
        c.close()

    panels: dict = {}

    def panel(name, fn):
        try:
            panels[name] = fn()
        except Exception as e:
            panels[name] = {"ok": False,
                            "error": f"{type(e).__name__}: {e}"}

    # 1. 红线（坑岗预警）
    panel("redline", lambda: _p_redline(job))
    # 2. 薪资（到手 + 是否落目标区间）
    panel("salary", lambda: _p_salary(job))
    # 3. 公司画像
    panel("company", lambda: _p_company(job))
    # 4. 通勤
    panel("commute", lambda: _p_commute(job, geo))
    # 4.5 生活成本（按岗位区镇套预置档位）
    panel("living", lambda: _p_living(job))
    # 5. 投递优先级理由
    panel("priority", lambda: _p_priority(job))
    # 6. 投递记录
    panels["applied"] = ({"ok": True, "greeted_at": greeted[0],
                          "message": greeted[1]} if greeted
                         else {"ok": True, "greeted_at": None, "message": ""})

    return {"ok": True, "data": {"job": job, "panels": panels}}


def _p_redline(job: dict) -> dict:
    import redline as RL
    r = RL.check({"title": job.get("title") or "",
                  "company": job.get("company") or "",
                  "salary": job.get("salary") or "",
                  "job_description": job.get("job_description") or ""})
    return {"ok": True, "verdict": r["verdict"],
            "reds": r["reds"], "yellows": r["yellows"],
            "warnings": r["warnings"]}


def _p_salary(job: dict) -> dict:
    """薪资：解析区间 + 按城市目标判落点 + 到手估算。"""
    import filters as F
    import priority as P
    k = F.parse_salary_k(job.get("salary") or "")
    if not k:
        return {"ok": False, "reason": "薪资面议或解析不出，无法估算"}
    t_lo, t_hi = P._target_for(job.get("city") or "")
    mid = (k[0] + k[1]) / 2
    delta, why = P._salary_score(job.get("salary") or "", job.get("city") or "")

    out = {"ok": True, "min_k": k[0], "max_k": k[1], "mid_k": round(mid, 1),
           "target": [t_lo, t_hi], "delta": delta, "why": why}
    # 按区间下限估到手 —— 用下限而不是中位，避免把期望当承诺
    try:
        import living_cost as LC
        th = LC.take_home(k[0] * 1000, job.get("city") or "")
        out["take_home_lo"] = round(th["net"])
        out["social_lo"] = round(th["social"])
    except Exception:
        pass
    return out


def _p_company(job: dict) -> dict:
    import company_profile as CP
    c = connect()
    try:
        return CP.profile(c, job.get("company") or "")
    finally:
        c.close()


def _p_commute(job: dict, geo: bool) -> dict:
    import commute as C
    return C.estimate(None, None, job.get("district") or "",
                      job.get("city") or "", allow_fetch=geo)


def _p_living(job: dict) -> dict:
    """生活成本：岗位城市+区镇 → 预置档位（省着过/正常）+ 按薪资下限算结余。

    房租有平台来源，水电/餐饮/交通/日杂是估算项 —— 输出里明确标出来。
    """
    import filters as F
    import living_cost as LC

    city = (job.get("city") or "").strip()
    dist = (job.get("district") or "").strip()
    if not city:
        return {"ok": False, "reason": "岗位没城市，无法套用生活成本数据"}

    k = F.parse_salary_k(job.get("salary") or "")
    pretax = k[0] * 1000 if k else 0     # 按区间下限，不把期望当承诺
    import re as _re
    daily_pay = bool(_re.search(r"/\s*(?:[天日]|小时|时)",
                                job.get("salary") or ""))

    # 岗位区镇没预置数据时（如东莞大朗镇）降级到全城中位，但必须标出来 ——
    # 不能拿虎门的数冒充大朗的，也不能什么都不给。
    fallback = ""
    probe = LC.plan_cost(city, dist or None, "normal")
    if not probe.get("ok") and probe.get("unmatched_district"):
        fallback = (f"岗位区镇「{dist}」没有预置数据"
                    f"（覆盖：{'、'.join(LC.districts(city))}），下面按全城中位估 —— "
                    f"参考，不是那个镇的数")
        dist = ""
    elif not probe.get("ok"):
        return {"ok": False, "reason": probe.get("reason", ""),
                "district": dist, "city": city}

    tiers = []
    plan0 = None
    for tier in ("frugal", "normal"):
        p = LC.plan_cost(city, dist or None, tier)
        if not p.get("ok"):
            return {"ok": False, "reason": p.get("reason", ""),
                    "district": dist, "city": city}
        plan0 = plan0 or p
        row = {"tier": tier, "label": p["tier_label"], "total": p["total"],
               "range": p.get("total_range")}
        if pretax:
            b = LC.balance(pretax, city, district=dist or None, tier=tier)
            if b.get("ok"):
                row["left"] = round(b["left"])
                row["save_rate"] = b["save_rate"]
        tiers.append(row)

    out = {
        "ok": True,
        "city": city,
        "district": plan0.get("district") or "",   # 数据里的规范区名（可能是城市中位为空）
        "district_input": (job.get("district") or "").strip(),
        "matched": bool(dist and plan0.get("district")),
        "fallback": bool(fallback),
        "basis": plan0.get("basis"),
        "tiers": tiers,
        "pretax_lo": pretax,
        "daily_pay": daily_pay,
        "estimated": True,
        "collected_at": plan0.get("collected_at", ""),
        "note": (fallback + "；" if fallback else "") + (plan0.get("note") or ""),
        "rent_source": plan0.get("rent_source") or {},
        "job_hint": plan0.get("job_hint", ""),
    }

    # 东莞：家在虎门威远岛 → 顺手给住家档
    if city == "东莞":
        hd = plan0.get("district") or "虎门"
        h = LC.plan_cost(city, hd, "normal", "home")
        if h.get("ok"):
            row = {"total": h["total"], "district": h["district"],
                   "note": h["note"]}
            if pretax:
                b = LC.balance(pretax, city, district=hd,
                               tier="normal", scene="home")
                if b.get("ok"):
                    row["left"] = round(b["left"])
                    row["save_rate"] = b["save_rate"]
            out["home"] = row
    return out


def _p_priority(job: dict) -> dict:
    import priority as P
    s = P.score(job)
    return {"ok": True, **s}


@app.get("/api/jobs")
def jobs_ep(q: str = "", city: str = "", only_passed: bool = False,
            online: bool = False, limit: int = 400):
    """岗位库。筛选在前端做最省事，这里只裁剪出字段 + 标记。"""
    try:
        c = connect()
        d = ledger.collect(c)
        greeted = store.already_greeted(c)
        c.close()
        out = []
        for j in d["jobs"]:
            if only_passed and not j["pass"]:
                continue
            if city and (j.get("city") or "") != city:
                continue
            if online and (j.get("boss_active") or "") not in ONLINE_STATES:
                continue
            if q:
                hay = f"{j.get('title','')} {j.get('company','')} {j.get('skills','')}".lower()
                if q.lower() not in hay:
                    continue
            out.append({
                "job_id": j["job_id"], "title": j.get("title"), "company": j.get("company"),
                "salary": j.get("salary"), "city": j.get("city"), "district": j.get("district"),
                "education": j.get("education"), "experience": j.get("experience"),
                "boss_active": j.get("boss_active"), "skills": j.get("skills"),
                "online": (j.get("boss_active") or "") in ONLINE_STATES,
                "passed": bool(j["pass"]), "reject_reason": j.get("reject_reason"),
                "greeted": j["job_id"] in greeted,
                "has_security": bool(j.get("security_id")),
            })
        cities = sorted({r["city"] for r in out if r["city"]})
        return {"ok": True, "data": {"rows": out[:limit], "total": len(out), "cities": cities}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/settings")
def settings_get():
    try:
        import settings as st
        return {"ok": True, "data": st.load(force=True)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class SettingsReq(BaseModel):
    config: dict


@app.post("/api/settings")
def settings_save(req: SettingsReq):
    try:
        import settings as st
        return {"ok": True, "data": st.save(req.config)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/settings/reset")
def settings_reset():
    try:
        import settings as st
        return {"ok": True, "data": st.reset()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/analysis")
def analysis():
    """投递分析：漏斗 + 按天 + 按城市 + 过滤原因明细。"""
    try:
        c = connect()
        d = ledger.collect(c)
        greets = [dict(r) for r in c.execute("SELECT * FROM greetings")]
        # 完整漏斗 + 数据完整性体检 —— 分析页最容易骗人的地方就是把
        # 「数据缺失」画成「转化率低」，所以两个一起给。
        import funnel as _funnel
        fs = _funnel.snapshot(c)
        finteg = _funnel.integrity(fs)
        flayers = _funnel.layers(fs)
        c.close()

        from collections import Counter
        by_day = Counter()
        for g in greets:
            if g.get("status") == "sent" and g.get("greeted_at"):
                by_day[ledger.day_key(g["greeted_at"])] += 1
        by_city = Counter(j.get("city") or "未知" for j in d["applied"])
        by_status = Counter(g.get("status") or "unknown" for g in greets)

        # 过滤漏斗：每个阶段被挡掉多少
        funnel = [{"stage": "抓取入库", "n": len(d["jobs"])}]
        blocked = Counter()
        for j in d["jobs"]:
            if not j.get("pass"):
                r = (j.get("reject_reason") or "其他").strip()
                blocked[r.split()[0] if r.split() else "其他"] += 1
        funnel.append({"stage": "通过筛选", "n": len(d["passed"])})
        funnel.append({"stage": "已投递", "n": len(d["applied"])})

        return {"ok": True, "data": {
            "funnel": funnel,
            "funnel_layers": flayers,
            "integrity": finteg,
            "blocked": [{"reason": k, "n": v} for k, v in blocked.most_common(10)],
            "by_day": [{"day": k, "n": by_day[k]} for k in sorted(by_day)[-14:]],
            "by_city": [{"city": k, "n": v} for k, v in by_city.most_common(10)],
            "by_status": [{"status": k, "n": v} for k, v in by_status.most_common()],
            "total_greets": len(greets),
        }}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class SalaryReq(BaseModel):
    monthly: float
    months: int = 12
    bonus: float = 0.0
    housing_fund_rate: float = 0.05
    special_deduction: float = 0.0
    social_base: float | None = None


@app.post("/api/tools/salary")
def tools_salary(req: SalaryReq):
    """税后到手计算。纯本地，零平台调用、零 token。"""
    try:
        import tools as T
        return {"ok": True, "data": T.calc_salary(
            monthly_gross=req.monthly, months=req.months, bonus=req.bonus,
            housing_fund_rate=req.housing_fund_rate,
            special_deduction=req.special_deduction, social_base=req.social_base)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class OffersReq(BaseModel):
    a: dict
    b: dict


@app.post("/api/tools/offers")
def tools_offers(req: OffersReq):
    try:
        import tools as T
        return {"ok": True, "data": T.compare_offers(req.a, req.b)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/tools/resume-check")
def tools_resume_check():
    """对 C:\\面试\\Resume.md（只读）做隐患排查。"""
    try:
        import tools as T
        text = load_resume()
        if not text.strip():
            return {"ok": False, "error": "还没设置简历 —— 请到「简历优化」页粘贴或上传一份"}
        return {"ok": True, "data": T.check_resume(text, RESUME_FILE.name)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class TextReq(BaseModel):
    text: str
    filename: str = ""


@app.post("/api/tools/resume-check")
def tools_resume_check_text(req: TextReq):
    try:
        import tools as T
        return {"ok": True, "data": T.check_resume(req.text, req.filename)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/tools/jd-extract")
def tools_jd_extract(req: TextReq):
    try:
        import tools as T
        return {"ok": True, "data": T.extract_jd(req.text)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class OfferCheckReq(BaseModel):
    text: str
    contract_months: float | None = None


@app.post("/api/tools/offer-check")
def tools_offer_check(req: OfferCheckReq):
    try:
        import tools as T
        return {"ok": True, "data": T.check_offer(req.text, req.contract_months)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/tools/checklist")
def tools_checklist():
    try:
        import tools as T
        return {"ok": True, "data": T.get_checklist()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/tools/playbook")
def tools_playbook():
    try:
        import tools as T
        return {"ok": True, "data": T.get_playbook()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class InterviewGenReq(BaseModel):
    jd_text: str
    title: str = ""
    n: int = 6


@app.post("/api/ai/interview/generate")
def ai_interview_generate(req: InterviewGenReq):
    """AI 模拟面试出题（要花 token）。"""
    try:
        if len(req.jd_text.strip()) < 40:
            return {"ok": False, "error": "JD 太短（至少 40 字）"}
        resume = load_resume()
        if not resume:
            return {"ok": False, "error": "还没设置简历 —— 请到「简历优化」页粘贴或上传一份"}
        api_key, base_url = gg.resolve_key()
        # 没配 key 也继续 —— call_llm 会自动回退本地 ollama
        # （实测 2026-09-20 DeepSeek 欠费返回 402，硬性拒绝会让功能全废）
        import ai_tools as A
        t0 = time.time()
        result, usage = A.interview_questions(
            resume=resume, jd=req.jd_text, title=req.title, n=req.n,
            api_key=api_key, base_url=base_url)
        return {"ok": True, "data": {"result": result, "elapsed": round(time.time() - t0, 1),
                                     "cost": ro.estimate_cost(usage),
                                     "types": A.QUESTION_TYPES}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class QAItem(BaseModel):
    id: int = 0
    type: str = ""
    question: str = ""
    answer: str = ""


class InterviewGradeReq(BaseModel):
    jd_text: str = ""
    title: str = ""
    qa: list[QAItem] = []


@app.post("/api/ai/interview/grade")
def ai_interview_grade(req: InterviewGradeReq):
    """AI 模拟面试点评（要花 token）。"""
    try:
        if not req.qa:
            return {"ok": False, "error": "没有可点评的题目"}
        has_ans = [q for q in req.qa if (q.answer or "").strip()]
        if not has_ans:
            return {"ok": False, "error": "还没有作答，先写点内容再点评"}
        resume = load_resume()
        if not resume:
            return {"ok": False, "error": "还没设置简历 —— 请到「简历优化」页粘贴或上传一份"}
        api_key, base_url = gg.resolve_key()
        # 没配 key 也继续 —— call_llm 会自动回退本地 ollama
        # （实测 2026-09-20 DeepSeek 欠费返回 402，硬性拒绝会让功能全废）
        import ai_tools as A
        t0 = time.time()
        result, usage = A.interview_grade(
            resume=resume, jd=req.jd_text, title=req.title,
            qa=[q.model_dump() for q in req.qa],
            api_key=api_key, base_url=base_url)
        return {"ok": True, "data": {"result": result, "elapsed": round(time.time() - t0, 1),
                                     "cost": ro.estimate_cost(usage),
                                     "graded": len(has_ans), "total": len(req.qa)}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/ai/role-fit")
def ai_role_fit():
    """岗位方向匹配（要花 token）。"""
    try:
        resume = load_resume()
        if not resume:
            return {"ok": False, "error": "还没设置简历 —— 请到「简历优化」页粘贴或上传一份"}
        api_key, base_url = gg.resolve_key()
        # 没配 key 也继续 —— call_llm 会自动回退本地 ollama
        # （实测 2026-09-20 DeepSeek 欠费返回 402，硬性拒绝会让功能全废）
        import ai_tools as A
        t0 = time.time()
        result, usage = A.role_fit(resume=resume, api_key=api_key, base_url=base_url)
        return {"ok": True, "data": {"result": result, "elapsed": round(time.time() - t0, 1),
                                     "cost": ro.estimate_cost(usage)}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class CareerPlanReq(BaseModel):
    jd_text: str
    title: str = ""
    weeks: int = 6


@app.post("/api/ai/career-plan")
def ai_career_plan(req: CareerPlanReq):
    """职业行动计划（要花 token）。"""
    try:
        if len(req.jd_text.strip()) < 40:
            return {"ok": False, "error": "JD 太短（至少 40 字）"}
        resume = load_resume()
        if not resume:
            return {"ok": False, "error": "还没设置简历 —— 请到「简历优化」页粘贴或上传一份"}
        api_key, base_url = gg.resolve_key()
        # 没配 key 也继续 —— call_llm 会自动回退本地 ollama
        # （实测 2026-09-20 DeepSeek 欠费返回 402，硬性拒绝会让功能全废）
        import ai_tools as A
        t0 = time.time()
        result, usage = A.career_plan(
            resume=resume, jd=req.jd_text, title=req.title, weeks=req.weeks,
            api_key=api_key, base_url=base_url)
        return {"ok": True, "data": {"result": result, "elapsed": round(time.time() - t0, 1),
                                     "cost": ro.estimate_cost(usage)}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/paths")
def paths():
    html = (f"岗位库：<code>{store.DB_PATH}</code><br>"
            f"boss 缓存：<code>{store.BOSS_DB}</code>（只读）<br>"
            f"简历：<code>{RESUME_FILE if RESUME_FILE.exists() else DEFAULT_RESUME}</code>"
            f"（已设置：{'是' if load_resume().strip() else '否'}）<br>"
            f"CDP 浏览器 profile：<code>{ap.CLI_DIR}</code> 无关；见 boss_login.py")
    return {"ok": True, "data": {"html": html}}


@app.post("/api/sync")
def sync():
    try:
        return {"ok": True, "data": store.sync()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=200)


@app.get("/api/chat/list")
def chat_list(pending: int = 1):
    """聊天会话列表。pending=1 只看待处理（对方最后说话且未标记处理）。"""
    try:
        c = connect()
        rows = store.list_conversations(c, pending_only=bool(pending))
        # 把会话和岗位对上（公司名归一化匹配）—— 否则用户不知道这条回复
        # 对应自己投的哪个岗位，看回复等于看天书。
        linked = {x["conv_key"]: x for x in store.link_conversations(c)}
        for r in rows:
            lk = linked.get(r["conv_key"]) or {}
            r["job_title"] = lk.get("job_title", "")
            r["job_salary"] = lk.get("job_salary", "")
        total = c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        pend = c.execute(
            "SELECT COUNT(*) FROM conversations WHERE from_me=0 AND handled=0"
        ).fetchone()[0]
        by_cat: dict[str, int] = {}
        for r in rows:
            by_cat[r["category"] or "normal"] = by_cat.get(r["category"] or "normal", 0) + 1
        # 分类统计始终按「待处理」算：切到「显示全部」时不能让历史会话把数字撑大
        pending_by_cat: dict[str, int] = {}
        for (cat,) in c.execute(
                "SELECT category FROM conversations WHERE from_me=0 AND handled=0"):
            k = cat or "normal"
            pending_by_cat[k] = pending_by_cat.get(k, 0) + 1
        c.close()
        return {"ok": True, "data": {
            "total": total, "pending": pend, "by_category": by_cat,
            "pending_by_category": pending_by_cat,
            "rows": [{k: r.get(k) for k in
                          ("conv_key", "hr_name", "company", "position", "last_msg",
                           "category", "from_me", "last_time", "avatar",
                           "updated_at", "pending_since", "handled",
                           "job_title", "job_salary", "age_hours")}
                     for r in rows],
            "updated": datetime.now().strftime("%H:%M:%S"),
        }}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/chat/sync")
def chat_sync_api():
    """真去抓一次 BOSS 聊天页。抓不到要明确说清原因，不能返回空列表。"""
    import chat_sync

    try:
        r = chat_sync.sync()
        return {"ok": True, "data": {
            "added": r["added"], "changed": r["changed"],
            "unchanged": r["unchanged"], "pending": r["pending"],
            "by_category": r["summary"]["by_category"],
            "updated": datetime.now().strftime("%H:%M:%S"),
        }}
    except chat_sync.ChatUnavailable as e:
        return {"ok": False, "unavailable": True, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/chat/handled")
def chat_handled(conv_key: str = "", handled: int = 1):
    """标记会话为已处理 / 恢复待处理（只改本地，不回写 BOSS）。"""
    try:
        if not conv_key:
            return {"ok": False, "error": "缺少 conv_key"}
        c = connect()
        cur = c.execute("UPDATE conversations SET handled=? WHERE conv_key=?",
                        (1 if handled else 0, conv_key))
        c.commit()
        n = cur.rowcount
        c.close()
        if not n:
            return {"ok": False, "error": "没有这个会话"}
        return {"ok": True, "data": {"conv_key": conv_key, "handled": bool(handled)}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/overview")
def overview():
    try:
        c = connect()
        d = ledger.collect(c)
        c.close()
        ONLINE = ("在线", "刚刚活跃", "今日活跃")
        today = datetime.now().strftime("%Y-%m-%d")
        pending_rows = [{
            "title": j.get("title"), "company": j.get("company"), "salary": j.get("salary"),
            "city": j.get("city"), "boss_active": j.get("boss_active"),
            "online": (j.get("boss_active") or "") in ONLINE,
            "education": j.get("education"), "match_score": j.get("match_score"),
        } for j in d["pending"][:40]]
        applied_rows = [{
            "time": ledger.fmt_ts(a["greeted_at"]), "title": a["title"], "company": a["company"],
            "salary": a["salary"] or "—", "city": a["city"] or "—", "url": a["url"],
        } for a in d["applied"][:60]]
        # 早期投递只存了 job_id、岗位查不回来的条数 —— 单独报给前端，
        # 让界面用一句话说明，而不是刷 28 行「岗位信息缺失」。
        no_info = len(d.get("unknown_applied") or [])
        return {"ok": True, "data": {
            "jobs": len(d["jobs"]), "passed": len(d["passed"]), "pending": len(d["pending"]),
            "applied": len(d["applied"]) + no_info,
            "applied_no_info": no_info,
            "today": sum(1 for a in d["applied"] if ledger.day_key(a["greeted_at"]) == today),
            "pending_rows": pending_rows, "applied_rows": applied_rows,
            "reasons": _reason_stats(d["jobs"]),
            "updated": datetime.now().strftime("%H:%M:%S"),
        }}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── 登录态缓存 ────────────────────────────────────────
# 血教训（2026-09-19）：UI 刷新绝不能直接打平台接口。
# 之前前端每 2 分钟轮询 + 每次页面加载都调 /api/login/status，
# 而该接口会真发 boss status 请求 → 一小时内数十次 → 触发 ACCOUNT_RISK 风控。
# 现在：结果缓存 10 分钟，只有用户显式点「重新检查」才 live 请求，
# 且 live 请求本身也限流（60 秒内不重复）。
_login_cache: dict = {"t": 0.0, "data": None, "live_t": 0.0}
LOGIN_TTL = 600          # 缓存有效期（秒）
LOGIN_LIVE_MIN_GAP = 60  # 两次 live 检查的最小间隔（秒）


@app.get("/api/login/status")
def login_status(live: bool = False):
    now = time.time()
    with _apply_lock:
        cached, ct, lt = _login_cache["data"], _login_cache["t"], _login_cache["live_t"]

    if not live and cached is not None and now - ct < LOGIN_TTL:
        return {"ok": True, "data": {**cached, "cached": True, "age": int(now - ct)}}

    if live and now - lt < LOGIN_LIVE_MIN_GAP and cached is not None:
        return {"ok": True, "data": {**cached, "cached": True, "age": int(now - ct),
                                     "note": f"距上次检查不足 {LOGIN_LIVE_MIN_GAP}s，返回缓存"}}

    try:
        ok, why = ap.login_ready()
        _, out = ap.run_boss(["status"], timeout=60)
        i = out.find("{")
        d = json.loads(out[i:out.rfind("}") + 1]) if i >= 0 else {}
        data = d.get("data", {})
        checks = data.get("checks") or []
        payload = {
            "ready": ok, "state": data.get("auth_state", "unknown"),
            # 通过时用 why —— 它可能带「提示：stoken_freshness」这类软提示，
            # 让用户知道"现在能用，但快该换 token 了"；失败时用平台给的
            # auth_summary（比我们拼的更细）
            "summary": why if ok else (data.get("auth_summary", "") or why),
            "checks": [{"name": c.get("name"), "status": c.get("status"),
                        "detail": (c.get("detail") or "")[:150]} for c in checks],
            "cached": False,
            "checked_at": datetime.now().strftime("%H:%M:%S"),
        }
        # 平台返回风控信号时，必须记下来给界面高亮，不能当普通失败吞掉
        raw = out + " " + str(d.get("error") or "")
        for sig in ap.BREAKER_CODES:
            if sig in raw:
                payload["risk"] = sig
                break
        for hint in ap.RISK_HINTS:
            if hint in raw:
                payload["risk"] = hint
                payload["ready"] = False
                break
        with _apply_lock:
            _login_cache.update({"t": now, "data": payload, "live_t": now})
        return {"ok": True, "data": payload}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/login/import")
async def login_import(payload: dict):
    from main import dispatch
    return dispatch("boss.login.import", {})


def _job_from_card(card: dict) -> dict:
    """推荐页的新岗在库里没有 → 从卡片文本里抽 经验/学历 构造记录。

    卡片文本样本：
      "python -K 1年以内 本科 Docker MySQL 量化交易开发经验 华遥愿金 深圳·南山区·科技园"
    薪资数字在 DOM 里是空的（BOSS 用字体/混淆隐藏），所以薪资只能拿到原始串，
    解析不出下限就会走「薪资未知」分支（rules 里 lo is None 时不下结论）。
    """
    raw = " ".join([str(card.get("raw") or ""), str(card.get("title") or ""),
                    str(card.get("salaryRaw") or "")])
    m = re.search(r"(在校/应届|应届|经验不限|无经验|1年以内|1-3年|3-5年|5-10年|10年以上)", raw)
    exp = m.group(1) if m else ""
    m = re.search(r"(学历不限|初中及以下|中专/中技|高中|大专|本科|硕士|博士)", raw)
    edu = m.group(1) if m else ""
    return {
        "title": card.get("title") or "",
        "company": card.get("company") or "",
        "salary": card.get("salaryRaw") or "",
        "education": edu,
        "experience": exp,
        "jd": card.get("raw") or "",
        "city": card.get("city") or "",
    }


@app.post("/api/delivery/plan")
async def delivery_plan(payload: dict):
    """给投递循环产白名单：卡片 → 过筛选规则 → 哪些能投、为什么不能。

    为什么放后端：筛选规则（黑名单/学历/经验/薪资上限）只有一份，在 rules.py。
    壳子端不该有第二份实现，否则两边必然漂移。
    优先用库里的完整记录（字段最全），库里没有的新岗才退回卡片文本解析。
    """
    cards = (payload or {}).get("cards") or []
    if not cards:
        return {"ok": False, "error": "cards 为空"}

    db_map: dict = {}
    ids = [str(c.get("jobId")) for c in cards if c.get("jobId")]
    if ids:
        try:
            conn = connect()
            marks = ",".join("?" * len(ids))
            for row in conn.execute(f"SELECT * FROM jobs WHERE job_id IN ({marks})", ids):
                d = dict(row)
                db_map[str(d.get("job_id"))] = d
            conn.close()
        except Exception as e:
            return {"ok": False, "error": f"读库失败 {type(e).__name__}: {e}"}

    items = []
    for c in cards:
        jid = str(c.get("jobId") or "")
        rec = db_map.get(jid)
        source = "db" if rec else "card"
        if not rec:
            rec = _job_from_card(c)
        try:
            ok, why = rules.job_ok(rec)
        except Exception as e:
            ok, why = False, f"规则异常 {type(e).__name__}: {e}"
        try:
            score = rules.job_score(rec)
        except Exception:
            score = 0.0
        items.append({
            "jobId": jid,
            "allowed": bool(ok),
            "reason": why or "",
            "source": source,
            "score": score,
            "title": (c.get("title") or rec.get("title") or "")[:80],
            "company": (c.get("company") or rec.get("company") or "")[:40],
            "salary": rec.get("salary") or c.get("salaryRaw") or "",
        })

    allowed = [it["jobId"] for it in items if it["allowed"] and it["jobId"]]
    # ★ 按优先级排序：allowedJobIds 是「排好序」的，循环按这个顺序投
    ordered = sorted([it for it in items if it["allowed"] and it["jobId"]],
                     key=lambda x: -x["score"])
    ordered_ids = [it["jobId"] for it in ordered]
    reasons: dict = {}
    for it in items:
        if not it["allowed"] and it["reason"]:
            key = it["reason"].split()[0]
            reasons[key] = reasons.get(key, 0) + 1

    return {"ok": True, "data": {
        "total": len(items),
        "allowedCount": len(allowed),
        "allowedJobIds": ordered_ids,
        "reasons": reasons,
        "items": items,
        "sort": (rules.settings.load().get("sort") or {}),
    }}


@app.get("/api/llm/status")
def llm_status():
    """本地小模型（ollama）状态 —— UI 面板用。"""
    try:
        import llm_local as L
        base = L.base_url()
        alive = L.is_alive(base)
        models = L.list_models(base) if alive else []
        return {"ok": True, "data": {
            "alive": alive,
            "base_url": base,
            "models": models,
            "pick": L.pick_model(models) if models else "",
            "note": "本地模型跑招呼语/简历优化，0 成本；核心链路（收集/筛选/投递）本就不需要模型",
        }}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/llm/start")
def llm_start():
    """拉起本地 ollama（WSL）。慢操作，前台上限 3 分钟。"""
    try:
        import llm_local as L
        ok, msg = L.ensure_started()
        return {"ok": bool(ok), "data": {"message": msg}} if ok else {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/risk/brief")
def risk_brief():
    """跑任何碰平台的操作前的风险体检。

    UI 在「投递演练 / 开始投递 / 岗位收集」之前调这个，把风险摆出来，
    避免反复跑把自己跑进风控（这是用户明确提的需求）。
    """
    level = "ok"          # ok | warn | bad
    items: list[str] = []

    # 1) 登录态年龄（stoken 是短效令牌，比 cookie 短得多）
    try:
        sp = Path(os.path.expanduser("~")) / ".boss-agent" / "auth" / "session.enc"
        if sp.exists():
            age_h = (time.time() - sp.stat().st_mtime) / 3600
            if age_h > 5:
                level = "warn"
                items.append(f"登录态已 {age_h:.1f} 小时未刷新 —— stoken 是短效令牌，"
                             f"超过 5 小时大概率已失效（表现：搜索报「环境存在异常」）")
            elif age_h > 3:
                items.append(f"登录态 {age_h:.1f} 小时，还能用但建议重新同步一次")
        else:
            level = "bad"
            items.append("找不到 session.enc —— 投递会直接失败，先在壳子里登录并「同步登录态」")
    except OSError:
        pass

    # 2) 今日投递量
    try:
        conn = connect()
        day0 = time.time() - (time.time() % 86400)
        n = conn.execute("SELECT COUNT(*) FROM greetings WHERE greeted_at >= ?", (day0,)).fetchone()[0]
        cap = int((settings.load().get("limits") or {}).get("daily_cap") or 130)
        if n:
            items.append(f"今日已投 {n} / 上限 {cap}")
        if n >= cap:
            level = "bad"
            items.append("已达到每日上限，今天不要再投")
        conn.close()
    except Exception:
        pass

    # 3) 最近是否命中过风控
    try:
        conn = connect()
        rows = conn.execute(
            "SELECT stopped FROM runs WHERE stopped LIKE '%risk%' ORDER BY started_at DESC LIMIT 3"
        ).fetchall()
        if rows:
            level = "warn" if level == "ok" else level
            items.append(f"最近 {len(rows)} 次运行命中过风控信号（{rows[0][0]}）—— "
                         f"先等 24-48h，回官网手动确认恢复正常再谈自动化")
        conn.close()
    except Exception:
        pass

    return {"ok": True, "data": {
        "level": level,
        "items": items,
        "advice": [
            "投递间隔 60-120s 随机，每 5 个休息 10-15 分钟，日上限 130（本机实测 8-20s×12 次 = 24h 封禁）",
            "UI 刷新/自动轮询绝不直连平台接口（登录态走 10 分钟缓存，显式点按钮才 live 检查）",
            "出现 ACCOUNT_RISK / RATE_LIMITED / 环境异常 → 立即停全部自动化，不重试、不换参数试探",
            "岗位收集是只读搜索，比打招呼安全，但同样要限速（间隔 25-45s，每轮 ≤20 次）",
            "投递期间不要开调试端口 —— BOSS 页面 JS 会探测 9222",
            "搜索返回 <3s 多半是 CLI 缓存命中，不代表真实请求成功",
        ],
        "checked_at": datetime.now().strftime("%H:%M:%S"),
    }}


@app.get("/api/resume")
def resume_get():
    """当前简历状态 —— UI 用它决定显示「已设置」还是「去设置」。

    ⚠️ 必须复用 load_resume()，不要再自己抄一份判断逻辑。
    踩过：这里单独写了一份 if/elif，没读 cleared 标记，
    于是「清除」后本接口仍报 configured=True（而 AI 那边已经用不了），
    两边说法打架。
    """
    text = load_resume()
    meta = _resume_meta()
    if RESUME_FILE.exists() and text:
        src = "app"
    elif text:
        src = "legacy"
    else:
        src = "none"

    return {"ok": True, "data": {
        "configured": bool(text.strip()),
        "source": src,
        "chars": len(text),
        "filename": meta.get("filename") or ("Resume.md" if src == "legacy" else ""),
        "updated_at": meta.get("updated_at") or "",
        "preview": "\n".join(text.strip().splitlines()[:6])[:400],
        "legacy_path": str(DEFAULT_RESUME),
        "legacy_exists": DEFAULT_RESUME.exists() and not meta.get("cleared"),
        "cleared": bool(meta.get("cleared")),
        "note": "简历只存在本机 jobkit/data/，除了调用模型 API 时用作上下文，不会上传到别处。",
    }}


def _extract_text_from_upload(filename: str, raw: bytes) -> tuple[str, str]:
    """从上传的文件里抽文本。返回 (文本, 说明)。不支持/失败就返回 (\"\", 原因)。"""
    name = (filename or "").lower()
    if name.endswith((".md", ".txt", ".text")):
        for enc in ("utf-8", "utf-8-sig", "gbk"):
            try:
                return raw.decode(enc), f"文本直读（{enc}）"
            except UnicodeDecodeError:
                continue
        return "", "编码识别失败，请直接粘贴文本"
    if name.endswith(".pdf"):
        try:
            import io
            from pypdf import PdfReader
            r = PdfReader(io.BytesIO(raw))
            return "\n".join((p.extract_text() or "") for p in r.pages), f"PDF 解析（{len(r.pages)} 页）"
        except ImportError:
            return "", "缺 pypdf，无法解析 PDF —— 请直接粘贴文本"
        except Exception as e:
            return "", f"PDF 解析失败：{type(e).__name__}"
    if name.endswith(".docx"):
        try:
            import io
            import docx
            d = docx.Document(io.BytesIO(raw))
            return "\n".join(p.text for p in d.paragraphs), f"DOCX 解析（{len(d.paragraphs)} 段）"
        except ImportError:
            return "", "缺 python-docx，无法解析 DOCX —— 请在 Word 里全选复制粘贴"
        except Exception as e:
            return "", f"DOCX 解析失败：{type(e).__name__}"
    return "", f"不支持的格式 {name or '(无扩展名)'} —— 支持 md/txt/pdf/docx，或直接粘贴"


@app.post("/api/resume")
async def resume_set(payload: dict):
    """设置简历。两种传法：{text: "..."} 或 {filename, content_base64}。"""
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    filename = payload.get("filename") or ""
    how = "粘贴"

    b64 = payload.get("content_base64")
    if not text and b64:
        try:
            import base64 as _b64
            raw = _b64.b64decode(b64)
        except Exception as e:
            return {"ok": False, "error": f"文件解码失败：{type(e).__name__}"}
        if len(raw) > 8 * 1024 * 1024:
            return {"ok": False, "error": "文件超过 8MB"}
        text, how = _extract_text_from_upload(filename, raw)
        if not text:
            return {"ok": False, "error": how}

    if not text:
        return {"ok": False, "error": "简历内容为空"}
    if len(text) < 50:
        return {"ok": False, "error": f"内容只有 {len(text)} 字，太短了 —— 确认一下是不是选错了文件"}

    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_FILE.write_text(text, encoding="utf-8")
    RESUME_META.write_text(json.dumps({
        "filename": filename or "粘贴的文本",
        "how": how,
        "chars": len(text),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"ok": True, "data": {
        "chars": len(text), "how": how, "filename": filename or "粘贴的文本",
        "saved_to": str(RESUME_FILE),
        "preview": "\n".join(text.splitlines()[:5])[:300],
    }}


@app.post("/api/resume/import-legacy")
def resume_import_legacy():
    """把旧的硬编码路径 C:\\面试\\Resume.md 一键导进来（老用户平滑迁移）。"""
    if not DEFAULT_RESUME.exists():
        return {"ok": False, "error": f"找不到 {DEFAULT_RESUME}"}
    try:
        text = DEFAULT_RESUME.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": f"读取失败：{e}"}
    if not text.strip():
        return {"ok": False, "error": "那个文件是空的"}
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_FILE.write_text(text, encoding="utf-8")
    RESUME_META.write_text(json.dumps({
        "filename": DEFAULT_RESUME.name, "how": f"从 {DEFAULT_RESUME} 导入",
        "chars": len(text), "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "data": {"chars": len(text), "from": str(DEFAULT_RESUME)}}


@app.delete("/api/resume")
def resume_delete():
    """清除简历（只清 jobkit 的副本，不动磁盘上的原文件）。

    会写一个 cleared 标记，免得 load_resume 又回退到旧路径 —— 那样按钮就是摆设。
    """
    try:
        if RESUME_FILE.exists():
            RESUME_FILE.unlink()
    except OSError:
        pass
    try:
        RESUME_DIR.mkdir(parents=True, exist_ok=True)
        RESUME_META.write_text(json.dumps({
            "cleared": True,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "data": {"cleared": True, "note": "已在应用内清除（磁盘原文件未动）"}}


# ── 岗位收集（UI 入口）──────────────────────────────────────
# collector 一轮要十几分钟（20 次搜索 × 45s 间隔），不能塞进 HTTP 请求里等。
# 所以起后台线程跑，UI 轮询 /api/collect/progress 看进度。
_collect: dict = {
    "running": False, "started_at": 0.0, "finished_at": 0.0,
    "stage": "", "done": 0, "total": 0,
    "inserted": 0, "updated": 0, "failed": 0, "cache_hits": 0,
    "risk": "", "stop_reason": "", "last": "", "log": [],
}
_collect_lock = threading.Lock()


def _collect_emit(stage: str, payload: dict) -> None:
    """collector 的进度回调 —— 只写内存，UI 来取。"""
    now = time.time()
    with _collect_lock:
        _collect["stage"] = stage
        if stage == "start":
            _collect["total"] = payload.get("total", 0)
        elif stage in ("got", "failed"):
            _collect["done"] = payload.get("index", 0) + 1
        if stage == "got":
            _collect["inserted"] += payload.get("new", 0)
            if payload.get("cache_hit"):
                _collect["cache_hits"] += 1
        if stage == "failed":
            _collect["failed"] += 1
        if stage == "risk":
            _collect["risk"] = payload.get("signal", "")
            _collect["finished_at"] = now
        if stage == "done":
            _collect["inserted"] = payload.get("inserted", _collect["inserted"])
            _collect["updated"] = payload.get("updated", 0)
            _collect["cache_hits"] = payload.get("cache_hits", _collect["cache_hits"])
            _collect["risk"] = payload.get("risk", "")
            _collect["finished_at"] = now
        # 人类可读的一行
        if stage == "search":
            _collect["last"] = f"搜索 {payload.get('keyword')} @ {payload.get('city')}"
        elif stage == "waiting":
            _collect["last"] = f"等待 {payload.get('wait_sec')}s（第 {payload.get('index', 0) + 1} 次）"
        elif stage == "got":
            tag = "缓存" if payload.get("cache_hit") else "真实"
            _collect["last"] = (f"{payload.get('keyword')}@{payload.get('city')} "
                                f"{payload.get('count')} 条（新增 {payload.get('new')}，{tag}）")
        elif stage == "failed":
            _collect["last"] = f"{payload.get('keyword')}@{payload.get('city')} 失败：{payload.get('reason', '')[:60]}"
        elif stage == "done":
            _collect["last"] = "本轮结束"
        _collect["log"].append(f"[{stage}] {_collect['last']}")
        if len(_collect["log"]) > 300:
            _collect["log"] = _collect["log"][-300:]


def _collect_worker(n: int) -> None:
    try:
        import collector as C
        C.collect(max_searches=n, on_progress=_collect_emit)
    except Exception as e:
        with _collect_lock:
            _collect["stop_reason"] = f"{type(e).__name__}: {e}"
            _collect["finished_at"] = time.time()
            _collect["log"].append(f"[error] {type(e).__name__}: {e}")
    finally:
        with _collect_lock:
            _collect["running"] = False


@app.get("/api/collect/status")
def collect_status():
    """岗位收集：池子现状 + 配置 + 是否在跑 + 上次结果。"""
    import collector as C

    total = 0
    cities: dict = {}
    fresh24 = 0
    last_run = None
    try:
        conn = connect()
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        for r in conn.execute("SELECT city, COUNT(*) FROM jobs GROUP BY city ORDER BY 2 DESC"):
            cities[r[0] or "(空)"] = r[1]
        fresh24 = conn.execute("SELECT COUNT(*) FROM jobs WHERE last_seen > ?",
                               (time.time() - 86400,)).fetchone()[0]
        row = conn.execute("SELECT started_at, ended_at, sent, failed, stopped FROM runs "
                           "WHERE stopped LIKE 'collect%' ORDER BY started_at DESC LIMIT 1").fetchone()
        if row:
            last_run = {
                "started_at": datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M") if row[0] else "",
                "new_jobs": row[2], "failed": row[3], "note": row[4],
            }
        conn.close()
    except Exception:
        pass

    cfg = (settings.load().get("collect") or {})
    state = C.load_state()
    combos = len(cfg.get("keywords") or C.DEFAULT_KEYWORDS) * len(cfg.get("cities") or C.DEFAULT_CITIES)

    with _collect_lock:
        live = dict(_collect)
    live["log"] = live["log"][-60:]

    return {"ok": True, "data": {
        "pool": {"total": total, "cities": cities, "fresh24": fresh24},
        "config": {
            "keywords": cfg.get("keywords") or C.DEFAULT_KEYWORDS,
            "cities": cfg.get("cities") or C.DEFAULT_CITIES,
            "max_searches_per_round": cfg.get("max_searches_per_round") or 20,
            "min_interval_sec": cfg.get("min_interval_sec") or 25,
            "max_interval_sec": cfg.get("max_interval_sec") or 45,
            "enabled": cfg.get("enabled", True),
        },
        "cursor": {"at": state.get("cursor", 0), "combos": combos, "rounds": state.get("rounds", 0),
                   "last_run": state.get("last_run")},
        "last_run": last_run,
        "live": live,
        "note": "投递只在当前页面上操作，不会往岗位库写东西 —— 收集是独立的一条路径。",
    }}


@app.post("/api/collect/run")
def collect_run(payload: dict = None):
    """触发一轮收集（后台线程，立即返回）。"""
    with _collect_lock:
        if _collect["running"]:
            return {"ok": False, "error": "已经在跑了一轮，等它结束"}
    n = None
    if isinstance(payload, dict) and payload.get("max"):
        try:
            import collector as _C
            # ⛔ 硬上限 —— 不要再写成 60。2026-09-20 风控事故的直接成因之一
            # 就是允许 60 次/轮；单轮 60 次 × 多轮 = 一小时内 180 次搜索。
            n = max(1, min(_C.MAX_PER_ROUND_HARD, int(payload["max"])))
        except (TypeError, ValueError, ImportError):
            n = None
    if n is None:
        n = int((settings.load().get("collect") or {}).get("max_searches_per_round") or 20)

    with _collect_lock:
        _collect.update({
            "running": True, "started_at": time.time(), "finished_at": 0.0,
            "stage": "starting", "done": 0, "total": n,
            "inserted": 0, "updated": 0, "failed": 0, "cache_hits": 0,
            "risk": "", "stop_reason": "", "last": "启动中…", "log": [],
        })
    threading.Thread(target=_collect_worker, args=(n,), daemon=True).start()
    return {"ok": True, "data": {"started": True, "searches": n,
                                 "note": f"本轮 {n} 次搜索，约 {n * 40 // 60} 分钟。进度看状态接口。"}}


@app.post("/api/collect/stop")
def collect_stop():
    """请求停止（本轮会在当前这次搜索结束后退出）。"""
    with _collect_lock:
        if not _collect["running"]:
            return {"ok": True, "data": {"running": False, "note": "本来就没在跑"}}
        _collect["stop_reason"] = "user-stop"
        _collect["running"] = False
    return {"ok": True, "data": {"stopping": True}}


# ── 壳子投递回写（P0：投递走壳子自己的浏览器）────────────────
# 背景：以前投递靠 CLI 的 Bridge 通道，要用户自己起 daemon + 装扩展 —— 对一般用户是劝退级。
# 改成壳子驱动自己的 webview 后，壳子需要把「这一单投没投成」回写进台账。
@app.get("/api/delivery/quota")
def delivery_quota():
    """今日投递配额 —— 壳子开投前先问这个。"""
    try:
        used = ap.today_sent_count()
    except Exception:
        used = 0
    cap = int((settings.load().get("limits") or {}).get("daily_cap") or 130)
    return {"ok": True, "data": {
        "sent_today": used, "daily_cap": cap, "remaining": max(0, cap - used),
        "exhausted": used >= cap,
    }}


@app.post("/api/delivery/record")
async def delivery_record(payload: dict):
    """壳子投递完一单后回写台账。

    状态语义：
      sent      —— 开聊 + 招呼语已发出且读到「送达」
      failed    —— 中途失败（开聊失败/生成失败/发送失败/没读到送达）
      skipped   —— 不该投（过筛没过、已在台账里）
    幂等：job_id 冲突时更新，不重复插。
    """
    p = payload or {}
    job_id = str(p.get("jobId") or "").strip()
    if not job_id:
        return {"ok": False, "error": "缺 jobId"}
    status = str(p.get("status") or "failed").strip().lower()
    if status not in ("sent", "failed", "skipped"):
        return {"ok": False, "error": f"状态非法 {status}"}
    try:
        conn = connect()
        # 上报方可能只给 jobId，这里从库里补一份岗位快照 —— 否则岗位被新
        # 搜索结果覆盖后，台账里这条记录就只剩一个 id。
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        snap = dict(row) if row else None
        store.record_greet(conn, job_id, str(p.get("securityId") or ""),
                           str(p.get("message") or "")[:500],
                           status, (str(p.get("error") or "")[:300] or None),
                           job=snap)
        conn.close()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        used = ap.today_sent_count()
    except Exception:
        used = 0
    cap = int((settings.load().get("limits") or {}).get("daily_cap") or 130)
    return {"ok": True, "data": {"recorded": True, "status": status,
                                 "sent_today": used, "daily_cap": cap,
                                 "remaining": max(0, cap - used)}}


@app.get("/api/tools/blacklist-report")
def tools_blacklist_report():
    """黑名单体检报告（0 token，纯统计）。"""
    try:
        import tools as T
        conn = connect()
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs")]
        conn.close()
        cfg = settings.load()
        return {"ok": True, "data": T.blacklist_report(
            jobs, cfg["blacklist"]["company"], cfg["blacklist"]["title"],
            check_fn=rules.job_ok)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/tools/company-check")
async def tools_company_check(payload: dict):
    """公司背调 —— 本地规则层（0 token）。"""
    try:
        import tools as T
        p = payload or {}
        co = str(p.get("company") or "").strip()
        if not co:
            return {"ok": False, "error": "请填公司名"}
        cfg = settings.load()
        return {"ok": True, "data": T.check_company(
            co, str(p.get("title") or ""), str(p.get("jd") or "")[:3000],
            known_blocked=cfg["blacklist"]["company"])}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


_login_lock = threading.Lock()
_login_proc: dict = {"p": None, "last_start": 0.0}
LOGIN_MIN_GAP_SEC = 20    # 两次拉起之间至少隔 20 秒


@app.post("/api/login/start")
def login_start():
    """拉起 BOSS 登录窗口。

    ⛔ 并发守卫 + 冷却（2026-09-21 审计补）：
    以前无锁，连点几次「去登录」就起几个浏览器进程 —— 每个都会打开 zhipin.com，
    等于多次平台请求，而且窗口叠一堆。

    ⚠️ 只判「进程是否还活着」不够：如果登录脚本**快速退出**（比如 Edge 没起、
    CDP 端口不通），poll() 立刻返回，守卫就放行了 —— 实测连点 3 次起了 2 个。
    所以再加一道时间冷却：无论上次成功失败，20 秒内不许再拉。

    通用原则：**任何能拉起外部进程的端点都要防重入**（和收集器的并发问题同源）。
    """
    now = time.time()
    with _login_lock:
        if now - float(_login_proc.get("last_start") or 0) < LOGIN_MIN_GAP_SEC:
            left = int(LOGIN_MIN_GAP_SEC - (now - _login_proc["last_start"]))
            return {"ok": True, "data": {
                "already_running": True,
                "note": f"刚拉过一次了，请等 {left} 秒再试（连点会起多个浏览器窗口）。"}}
        p = _login_proc.get("p")
        if p is not None and p.poll() is None:
            return {"ok": True, "data": {
                "already_running": True, "pid": p.pid,
                "note": f"登录窗口已经在运行了（pid={p.pid}），别重复点。"
                        f"扫码完成后关掉它即可。"}}
        try:
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "boss_login.py"), "--wait", "0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                cwd=str(HERE),
            )
            _login_proc["p"] = proc
            _login_proc["last_start"] = now
        except Exception as e:
            return {"ok": False, "error": f"拉不起来：{type(e).__name__}: {e}"}

    return {"ok": True, "data": {
        "started": True, "pid": _login_proc["p"].pid,
        "note": "已启动。浏览器窗口即将弹出，请在其中扫码登录 BOSS 直聘。"
                "登录成功后本页「重新检查」即可看到状态变化。窗口请勿关闭。"}}


class ResumeReq(BaseModel):
    jd_text: str
    title: str = ""


@app.post("/api/resume/optimize")
def resume_optimize_ep(req: ResumeReq):
    try:
        if len(req.jd_text) < 40:
            return {"ok": False, "error": "JD 太短（至少 40 字）"}
        resume = load_resume()
        if not resume:
            return {"ok": False, "error": "还没设置简历 —— 请到「简历优化」页粘贴或上传一份"}
        api_key, base_url = gg.resolve_key()
        # 没配 key 也继续 —— call_llm 会自动回退本地 ollama
        # （实测 2026-09-20 DeepSeek 欠费返回 402，硬性拒绝会让功能全废）
        t0 = time.time()
        content, usage = ro.call_llm(
            [{"role": "system", "content": ro.SYSTEM_PROMPT},
             {"role": "user", "content": ro.USER_TEMPLATE.format(
                 job_title=req.title or "（未提供，请从 JD 推断）",
                 jd=req.jd_text, resume=resume)}],
            model=ro.DEFAULT_MODEL, api_key=api_key, base_url=base_url)
        result = ro.extract_json(content)
        return {"ok": True, "data": {"result": result, "elapsed": round(time.time() - t0, 1),
                                     "cost": ro.estimate_cost(usage)}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class GreetReq(BaseModel):
    jd_text: str
    company: str = ""
    title: str = ""
    count: int = 3


@app.post("/api/greet/generate")
def greet_generate_ep(req: GreetReq):
    try:
        if len(req.jd_text) < 40:
            return {"ok": False, "error": "JD 太短（至少 40 字）"}
        resume = load_resume()
        if not resume:
            return {"ok": False, "error": "还没设置简历 —— 请到「简历优化」页粘贴或上传一份"}
        api_key, base_url = gg.resolve_key()
        # 没配 key 也继续 —— call_llm 会自动回退本地 ollama
        # （实测 2026-09-20 DeepSeek 欠费返回 402，硬性拒绝会让功能全废）
        t0 = time.time()
        result, usage = gg.generate(req.jd_text, resume, company=req.company or "（未提供）",
                                    title=req.title or "（未提供，从 JD 推断）",
                                    count=max(1, min(5, req.count)),
                                    api_key=api_key, base_url=base_url)
        return {"ok": True, "data": {"result": result, "elapsed": round(time.time() - t0, 1),
                                     "cost": ro.estimate_cost(usage)}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class ApplyReq(BaseModel):
    limit: int = 5
    dry_run: bool = True
    interval: list[int] = [60, 120]
    rest_every: int = 5
    daily_cap: int = 130


# 旧投递写入口只返回内核拒绝；不再维护第二套内存队列或执行线程。

@app.post("/api/delivery/request")
def delivery_request(payload: dict = None):
    from main import dispatch
    return dispatch("boss.apply", payload or {})


@app.get("/api/delivery/pending")
def delivery_pending():
    return {"ok": False, "code": "KERNEL_REQUIRED", "data": {"pending": False},
            "error": "旧投递队列已停用，请使用统一工作区。"}


@app.post("/api/apply/start")
def apply_start(req: ApplyReq):
    from main import dispatch
    return dispatch("boss.apply", {"limit": req.limit})


@app.get("/api/apply/status")
def apply_status():
    return {"ok": False, "code": "KERNEL_REQUIRED", "error": "请在统一任务列表查看状态。"}


@app.post("/api/apply/stop")
def apply_stop():
    return {"ok": False, "code": "KERNEL_REQUIRED", "error": "请在统一任务列表取消任务。"}


def _port_state(host: str, port: int) -> str:
    """端口状态：free / busy（真有程序在听）/ reserved（被 Windows 锁死）。

    血教训：Windows 会把成段端口保留给系统（本机实测 8720-8819、7973-8472 等），
    bind 抛 WinError 10013。旧代码把它和「被别的程序占用」混为一谈，
    于是用户看到「端口被其它程序占用」—— 可 netstat 里一个进程都没有，
    用户永远查不出真相，只能用 netsh 看保留段（那不是用户该干的事）。
    10048 才是真的被占用。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.bind((host, port))
        return "free"
    except OSError as e:
        winerr = getattr(e, "winerror", None) or (e.args[0] if e.args else 0)
        return "reserved" if winerr in (13, 10013) else "busy"
    finally:
        try:
            s.close()
        except OSError:
            pass


def _port_in_use(host: str, port: int) -> bool:
    return _port_state(host, port) != "free"


def _pick_port(host: str, start: int, span: int = 200) -> int | None:
    """从 start 往后找一个真能用的端口。找不到返回 None。

    span 给 200 而不是 10：Windows 的保留段是成片的（本机 8720-8819 就有
    100 个），起点落在段里时小跨度的窗口会整段命中，白白报「没空端口」。
    """
    for p in range(max(1, start), min(start + span, 65536)):
        if _port_state(host, p) == "free":
            return p
    return None


def _is_our_jobkit(host: str, port: int) -> bool:
    """占用端口的是不是 jobkit 自己（避免把别人的服务当成自己）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
            body = r.read(800)          # 只读一次：连续两次 read 会接着往下读，判断不准
        return json.loads(body).get("service") == "jobkit"
    except Exception:
        return False


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--port", type=int, default=18770,
                     help="默认 18770（刻意避开 Windows 保留段 8720-8819 等）")
    ap_.add_argument("--host", default="127.0.0.1")
    ap_.add_argument("--force", action="store_true", help="旧参数已停用；请手动关闭原实例")
    ap_.add_argument("--cleanup", action="store_true",
                     help="旧参数已停用；不按文件名清理进程")
    args = ap_.parse_args()
    if args.force or args.cleanup:
        ap_.error("旧强制清理入口已停用。请关闭原应用窗口或服务终端后重启，不会终止其他进程。")
    if not 1 <= args.port <= 65535:
        ap_.error("端口必须在 1 到 65535 之间")
    from local_access import LOOPBACK_HOSTS
    if args.host not in LOOPBACK_HOSTS:
        ap_.error("本地桌面服务只允许监听回环地址")

    # ── 单实例守卫 ────────────────────────────────────────────
    if _port_in_use(args.host, args.port):
        if _is_our_jobkit(args.host, args.port):
            print(f"jobkit 已在运行 → http://{args.host}:{args.port}")
            print("本次启动取消。需要重启时，请关闭原应用窗口或服务终端。")
            return 3
        else:
            # 端口不能用但不是 jobkit：不把「换端口」这件事丢给用户。
            # reserved（系统保留段）连 netsh 都要查才知道，busy 可能是别的软件，
            # 两种情况用户都只能干瞪眼 —— 自动往后找，找到就用。
            state = _port_state(args.host, args.port)
            why = "被 Windows 保留" if state == "reserved" else "被其它程序占用"
            alt = _pick_port(args.host, args.port + 1)
            if alt is None:
                print(f"❌ 端口 {args.port} {why}，且往后 40 个端口也没空的")
                return 4
            print(f"⚠️ 端口 {args.port} {why} → 自动改用 {alt}")
            args.port = alt

    import uvicorn
    print(f"jobkit 已启动 →  http://{args.host}:{args.port}  (pid {os.getpid()})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.modules["app"] = sys.modules[__name__]
    from main import cli
    sys.exit(cli())
