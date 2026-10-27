# 草稿复制 / 修改履历接口契约

面向工作台前端与本机维护命令。两个接口都是**只读**：`effect=none`，不写平台、不写剪贴板、不改草稿。

调用约定（与 `docs/api/applicant-send-loop.md` 一致）：

- 所有 HTTP 接口使用会话头 `X-Jobkit-Context` 与当前工作区；`409` 表示身份/工作区/会话/前置状态不符，`400` 表示参数非法，`404` 表示记录不存在。
- 工具级错误返回固定错误码，前端按码分支，不要匹配文案。
- 两个路由都要求任务属于当前 `scope_id`；别的 scope 的任务按不存在处理。

---

## 一、`runtime.task.copy.get`

`GET /api/runtime/tasks/{task_id}/copy` — 复制前的服务端检查，返回可复制文本与版本，本身不写剪贴板。

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | path | 是 | 草稿任务 ID（`kind=work`、`tool=draft.reply`、`status=completed`） |
| `conversation_key` | query | 否 | 草稿所属会话键。**不传**保持升级前的行为：只按 `task_id` 核对，同一 scope 内即可读（老页面就是这种调用方式）。**传了**就必须与本任务的会话键完全一致，否则 `409 CONVERSATION_KEY_MISMATCH`。 |

返回：

```json
{"task_id": "...", "text": "一个月内可以到岗。", "manual_revision": 0, "document_revision": 1}
```

`manual_revision > 0` 时 `text` 是人工改过的版本。`document_revision` 与当前资料不一致会返回 `STALE_FACTS`，前端应当重新生成草稿而不是继续复制。

错误码：`COPY_NOT_AVAILABLE`（还没完成的草稿）、`TASK_SUPERSEDED`（旧稿已被替代）、`STALE_FACTS`（资料已更新）、`INVALID_CONVERSATION_KEY`（键形状非法，400）、`CONVERSATION_KEY_MISMATCH`（键属于另一个会话，409）。

## 二、`runtime.task.edits.get`

`GET /api/runtime/tasks/{task_id}/edits` — 读取这个草稿的人工修改履历（最近 20 条，倒序）。

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | path | 是 | 草稿任务 ID（`kind=work`、`tool=draft.reply`） |
| `conversation_key` | query | 否 | 同上：不传保持旧行为，传了必须与本任务的会话键一致 |

返回：

```json
{"task_id": "...", "total": 2, "edits": [{"revision": 2, "document_revision": 1, "text": "…", "created_at": 1793104188.0}]}
```

`total` 是全量条数，`edits` 只给最近 20 条。

错误码：`EDIT_NOT_AVAILABLE`（不是文字草稿）、`INVALID_CONVERSATION_KEY`（400）、`CONVERSATION_KEY_MISMATCH`（409）。

---

## 三、`conversation_key` 的形状

与会话窗口、预检、人工修改共用同一个校验（`applicant_reply_windows.checked_key`）：

- 1..128 个字符，不含任何空白（空格、制表符、换行都不行）；
- 不能用 HR 显示名（同名 HR 会串会话）；
- 键为空串时**不当作「没传」**：HTTP 层会把空串交给工具层，得到 `400 INVALID_CONVERSATION_KEY`，而不是静默退回旧行为。老页面不发这个参数，所以升级不受影响。

草稿的会话键来自 `runtime.applicant.reply.prepare`（不传键时是默认会话 `applicant_reply.DEFAULT_CONVERSATION_KEY`），前端从 `runtime.applicant.reply.get` / 任务列表拿到后原样回传即可。
