# 求职者回复闭环接口契约（第三十四–三十八批）

面向工作台前端与本机维护命令。**全部接口都不向 BOSS 写入任何数据**：`sending_enabled` 恒为 `false`，`delivery_confirmed` 恒为 `false`，`delivery_not_confirmed` 恒为真。

调用约定：

- 所有 HTTP 接口使用会话头与当前工作区上下文；`409` 表示身份/工作区/前置状态不符，`400` 表示参数非法，`404` 表示记录不存在。
- 工具级错误统一返回固定错误码（如 `SEND_DAILY_LIMIT_REACHED`），前端按码分支，不要匹配文案。
- 带幂等的接口重复调用返回同一结果并置 `created=false`。
- 标记 `maintenance_only` 的工具**没有 HTTP 路由**，只能由本机维护命令调用。

---

## 一、观察与准备

### runtime.applicant.reply.window.start

`POST /api/runtime/applicant/reply-window/start` — 建立消息观察起点。

| 字段 | 说明 |
| --- | --- |
| `message_observation_task_id` | 一次只读消息观察的任务 ID |
| `expected_revision` | 乐观并发；新建传当前 `revision`（`GET /api/runtime/applicant/reply-window` 取） |
| `expected_profile_revision` | 资料修订 |
| `expected_binding_revision` | 浏览器绑定修订 |

返回 `revision`，后续所有 `expected_window_revision` 用这个值。**重建起点会让已有预占失效**（见 `WINDOW_REBUILT`）。

### runtime.applicant.reply.window.observe

`POST /api/runtime/applicant/reply-window/observe` — 同字段，消息到达后推进观察窗口（`expected_revision` 传上次的 `revision`）。推进不等于重建：起点不变，已有预占照常可用。

### runtime.applicant.reply.window.get

`GET /api/runtime/applicant/reply-window` — 只读，返回当前窗口 `revision` 与起点任务标识。

### runtime.applicant.reply.prepare

`POST /api/runtime/applicant/replies/prepare`

| 字段 | 说明 |
| --- | --- |
| `message_observation_task_id` | 新的观察任务 |
| `message_ids` | 本次要回复的消息标识数组 |
| `expected_profile_revision` / `expected_binding_revision` | 乐观并发 |

返回 `intent`（`intent_id`）与 `task`（草稿任务，任务标识字段是 **`task.task_id`**）。注意：**规则没认出来的问句不会出草稿**，任务 `result.state` 为 `needs_review`、正文为空，此时不能进入发送流程，需要人工处理。

---

## 二、绑定发送对象

### runtime.applicant.send.recipient.confirm

`POST /api/runtime/applicant/send/recipient/confirm`

| 字段 | 说明 |
| --- | --- |
| `intent_id` | 回复意图 |
| `expected_manual_revision` | 草稿人工修订号，未改过传 0 |
| `expected_profile_revision` / `expected_binding_revision` | 乐观并发 |
| `expected_window_revision` | 当前观察窗口修订 |
| `confirmed` | 必须为 `true` |

返回 `recipient`，其中 `recipient_fingerprint` 是后续预占必须原样回传的凭据。绑定不可变：再次确认时目标若已变化返回 `SEND_RECIPIENT_CHANGED`。

### runtime.applicant.send.recipient.get

`GET /api/runtime/applicant/send/recipient/{intent_id}` — 读取已建立的绑定，只读。

---

## 三、预占额度

### runtime.applicant.send.reserve

`POST /api/runtime/applicant/send/reserve`

| 字段 | 说明 |
| --- | --- |
| `intent_id` | 回复意图 |
| `recipient_binding_id` | 上一步的 `recipient_fingerprint` |
| `expected_manual_revision` / `expected_profile_revision` / `expected_binding_revision` / `expected_window_revision` | 乐观并发 |
| `confirmed` | 必须为 `true` |

返回 `attempt`（`attempt_id`、`message_sha256`、`state`）与 `budget`。

- 同一意图重复预占返回同一条记录（`created=false`），不会重复占用额度。
- 额度是本机账本（默认 20/日，`JOBKIT_SEND_DAILY_LIMIT` 可调 1–200），用尽返回 `SEND_DAILY_LIMIT_REACHED`。
- 消息超过两分钟未重新观察返回 `MESSAGE_AGE_EXCEEDED`；观察中断超过两分钟返回 `WINDOW_GAP_EXCEEDED`。

### runtime.applicant.send.release

`POST /api/runtime/applicant/send/release`

| 字段 | 说明 |
| --- | --- |
| `attempt_id` | 预占记录 |
| `reason` | `user_cancelled` / `preflight_drift` / `draft_changed` |
| `confirmed` | 必须为 `true` |

仅 `reserved → released`，只能一次。释放后额度恢复。已 `outcome_unknown` 的记录不能释放（返回 `INVALID_SEND_ATTEMPT_STATE`）。

### runtime.applicant.send.get

`GET /api/runtime/applicant/send/{attempt_id}` — 读取预占记录与当日额度，只读。

---

## 四、人工交接（当前唯一可用的"发送"方式）

### runtime.applicant.send.handoff

`POST /api/runtime/applicant/send/handoff`

| 字段 | 说明 |
| --- | --- |
| `attempt_id` | 预占记录（必须是 `reserved`） |
| `confirmed` | 必须为 `true` |

返回 `text`（待发正文，前端直接展示供复制）、`handoff`（对方名、锚点消息、消息标识、正文摘要、`text_expires_at`）、`expires_in_seconds`。

- 正文**不写入提交凭据**：崩溃恢复后需重新取卡（幂等，安全）。
- 超过时效返回 `HANDOFF_EXPIRED`，不提供宽容路径。
- 草稿在预占后被改动返回 `DRAFT_CHANGED_AFTER_RESERVATION`。

### runtime.applicant.send.handoff.get

`GET /api/runtime/applicant/send/handoff/{attempt_id}` — 返回 `handoff`、`claim`、`expired`，只读。

### runtime.applicant.send.queue.get

`GET /api/runtime/applicant/send/queue` — 只读，列出本工作区所有 `reserved` 预占及其可交接状态。

每项包含 `attempt_id`、`party_display_name`、`has_handoff`、`claimed`、`expires_at`、`expires_in_seconds`、`expired`、`ready`。**不可交接的项不会被隐藏**：`ready=false` 时带 `reason`（错误码）与 `detail`。触发 `ready=false` 的情形包括草稿被改（`DRAFT_CHANGED_AFTER_RESERVATION`）、对象绑定损坏（`SEND_RECIPIENT_REQUIRED`）、资料或会话变化（`STALE_SEND_FACTS`）、观察起点重建（`WINDOW_REBUILT`）、已自报或已回读（`SEND_ALREADY_USED`）。

`ready` 与真正取卡使用**同一套判据**（同一函数），不存在"队列说行、取卡说不行"的分歧。

`queue_limit` 给出单批上限（10）。

### runtime.applicant.send.queue.handoff

`POST /api/runtime/applicant/send/queue/handoff`

| 字段 | 说明 |
| --- | --- |
| `attempt_ids` | 1–10 个预占标识，自动去重 |
| `confirmed` | 必须为 `true` |

**批量交接不是原子的**，契约写死：

- `cards`：已真实写入的记录（含 `text` 正文），可以照常复制发送；
- `skipped`：失败项，带 `attempt_id`、`code`、`message`，**没有产生任何改动**；
- `partial=true` 表示部分失败；全部失败时整个调用抛错，错误码取第一条失败原因。

前端必须两条都渲染：成功的卡片可复制，失败的项要显式提示原因并引导重新观察会话。忽略 `skipped` 等于漏发。

### runtime.applicant.send.manual.claim

`POST /api/runtime/applicant/send/manual/claim`

| 字段 | 说明 |
| --- | --- |
| `attempt_id` | 预占记录 |
| `confirmed` | 必须为 `true` |

用户手动粘贴发送后调用。返回 `claim.self_reported=true`、`delivery_confirmed=false`。**这是自报，不是平台确认**，前端不得显示成"已送达"。未取交接卡返回 `SEND_HANDOFF_REQUIRED`。

---

## 五、回读证据

### runtime.applicant.send.readback

`POST /api/runtime/applicant/send/readback` — 只读探测，不落库。

返回 `party_match`、`readback_observed`、`evidence`（`reason` / `message_id` / `ambiguous` / `match_count`）。`readback_observed=true` 也**不等于**送达确认。

### runtime.applicant.send.readback.commit

`POST /api/runtime/applicant/send/readback/commit` — 自己执行一次实时读取并落库，不接受调用方传入结论。

- 同一结论重复提交幂等（`created=false`）；
- 未命中升级为命中会覆盖（新增证据）；
- 已命中却出现未命中返回 `READBACK_REGRESSION`，证据不允许被抹掉。

---

## 六、归因统计

### runtime.applicant.send.stats

`GET /api/runtime/applicant/send/stats` — 只读，无参数。

顶层字段：`totals`、`by_day`、`by_draft`（按草稿正文 sha256 分桶）、`items`（最近 50 条）、`truncated`（被截掉的更早条数）、`invalid_documents`（无法校验的文档 kind 列表）、`limits`、`delivery_confirmed`（恒 `false`）、`sending_enabled`（恒 `false`）。

`totals`：`attempts`、`by_state`（按预占状态计数）、`handoffs`、`claims`、`readback_hits`、`readback_ambiguous`、`handoff_to_readback_rate`、`claim_to_readback_rate`。

`items` 每项：`attempt_id`、`intent_id`、`state`、`budget_day`、`draft_text_sha256`、`handoff`、`claimed`、`readback_observed`、`readback_ambiguous`。

`limits`：`readback_rate_basis`（比率口径说明）、`not_platform_reply_rate`（恒 `true`）、`delivery_not_confirmed`（恒 `true`）、`sample_warning`（交接样本少于 10 条时的提示，否则 `null`）。

口径：分母是"有交接卡且有回读观察的预占"。这是**本机账本**，不是平台送达率或已读率；多命中回读单独计入 `readback_ambiguous`，不并入 `readback_hits`。

---

## 七、删除对话内容（仅本机维护命令）

### runtime.applicant.reply.forget.plan

`maintenance_only`，只读：给出将删除的文档数、将抹除的任务数、被阻断的意图与原因。

| 字段 | 说明 |
| --- | --- |
| `intent_id` **或** `party_display_name` | 二选一 |

### runtime.applicant.reply.forget

`maintenance_only`，不可逆。

| 字段 | 说明 |
| --- | --- |
| `intent_id` **或** `party_display_name` | 二选一 |
| `confirmed` | 必须为 `true` |

删除意图文档与逐消息映射，抹除草稿正文与对方显示名，保留任务骨架与提交凭据。只要存在预占记录或交接/自报/回读文档，整体拒绝（`FORGET_BLOCKED_BY_EVIDENCE`）。

---

## 八、状态机、时效与前端接线

### 预占状态（`runtime_send_attempts.state`）

| 状态 | 含义 | 可否释放 | 是否占额度 |
| --- | --- | --- | --- |
| `reserved` | 已预占、尚未使用 | 未自报且未回读时可释放 | 是 |
| `released` | 用户取消或核对漂移，主动退回 | 不可再释放 | 否 |
| `not_sent` | 明确未发送（保留位，当前无写入方） | 不可再释放 | 否 |
| `outcome_unknown` | 动作结果不明，需人工核对 | **不可**释放 | 是（保守占用） |

`state` 与"是否已使用"是两件事：`reserved` + 存在 `applicant_send_claim:*` 或 `applicant_send_readback:*` 文档 = 已使用，`release` 返回 `SEND_ALREADY_USED`。

### 三个时间窗

| 时效 | 值 | 触发错误 |
| --- | --- | --- |
| 消息新鲜度（读取到预占） | 2 分钟 | `MESSAGE_AGE_EXCEEDED` |
| 观察连续性（两次读取之间） | 2 分钟 | `WINDOW_GAP_EXCEEDED` |
| 观察起点只接受的真实读取 | 10 秒 | `FRESH_WINDOW_MESSAGES_REQUIRED` |
| 预检结果 | 10 秒（`expires_at = checked_at + 10`） | 过期需重新预检 |
| 交接卡可登记已发送 | 从消息首见起 2 分钟 | `HANDOFF_EXPIRED` |

### 不续期原则

预占在创建时固化 `window_start_task_id`（当时的观察起点）。`reply.window.observe` 只推进窗口、不改起点，旧预占照常可用；`reply.window.start` 重建起点后旧预占一律失效（`WINDOW_REBUILT`），**没有宽容路径、没有自动续期**。旧库升级时该列为空串，与当前起点必然不等，因此升级前的预占需要重新准备一次。

### 部分成功

`send.queue.handoff` 不是原子的。响应必须两条都渲染：

- `cards`：已真实写入的交接卡（含 `text`），可复制使用；
- `skipped`：失败项（`attempt_id` / `code` / `message`），**没有产生任何改动**；
- `partial=true` 表示部分失败；全部失败时整个调用抛错，错误码取第一条原因。

忽略 `skipped` 等于漏发，属于实现缺陷。

### 未知结果

`outcome_unknown` 只在动作结果无法核对时写入：保守占用额度、不允许 `release`、**不自动重试**。恢复靠独立只读核对（`send.handoff.get` / `send.readback` / `send.get`），不重放提交。当前人工交接链路不产生该状态（交接不触碰平台），保留给后续受限动作。

### 工作台面板 ↔ 接口映射

| 面板区块 | 调用 |
| --- | --- |
| ① 消息观察起点 | `GET reply-window`；`POST reply-window/start`、`reply-window/observe` |
| ② 核对与预占 | `POST replies/{id}/preflight` → `send/recipient/confirm` → `send/reserve` |
| ② 取卡与登记 | `POST send/handoff` → `send/manual/claim` → `send/readback`、`send/readback/commit`；`send/release` 退回 |
| ③ 交接队列 | `GET send/queue`；`POST send/queue/handoff` |
| ④ 结果归因 | `GET send/stats` |

普通模式只显示人话原因；错误码与 `trace_id` 只在开发者信息区展示。面板默认隐藏，只有求职者工作区载入后才出现。

## 九、真实通道发送台账（一次性授权 + 结果闸门）

真实 BOSS 对话页的写入走 `chat.target → chat.authorize → chat.fill → chat.submit → chat.observe`。其中 `chat.submit` 只发一次鼠标点击就返回 `submit_requested`（等响应会超时误判），**所以"点过"和"核对过"必须分开记账** —— 这本账就是 `applicant_send_ledger`（原库内一份文档，kind=`applicant_send_ledger`；容量策略见下文「容量与淘汰」）。

### 状态

| 状态 | 是否拦后续发送 | 是否算已联系 | 说明 |
| --- | --- | --- | --- |
| `submitting` | 是 | 否 | 正在点击（进程崩在这就按未核对处理） |
| `submitted_unverified` | 是 | 否 | 已点击，无回显证据 |
| `outcome_unknown` | 是 | 否 | 读超时/页面失联，禁止自动重试 |
| `conflict` | 是 | 否 | 回显计数倒退等自相矛盾证据 |
| `observed_reply` | 否 | 是 | 观察到新增回显（≠送达，≠已读） |
| `resolved_delivered` | 否 | 是 | 人工核对确认已发出 |
| `resolved_not_delivered` | 否 | 否 | 人工核对确认未发出，对象可重新使用 |

### 容量与淘汰

台账按「证据 / 流水」分成两类，容量只淘汰流水：

| 分类 | 状态 | 容量行为 |
| --- | --- | --- |
| 保护集 | `submitting` / `submitted_unverified` / `outcome_unknown` / `conflict`（拦后续发送）、`observed_reply` / `resolved_delivered`（已联系证据） | **永不因容量淘汰**；超过 `MAX_PROTECTED_KEPT`（5000）时一条不删，只在报告里给 `overflow=True` + `overflow_count`，等人人工归档 |
| 可淘汰集 | `resolved_not_delivered` | 按 `ts` 保留最新 `MAX_ITEMS_KEPT`（500）条；被淘汰的条目放在 `merge()` 返回值的 `evicted` 里备查 |

- 状态缺失、未知状态、非 dict 脏数据一律按保护集处理（fail-closed）：误留一条只多占空间，误删一条会直接放行重复招呼。
- `chat.outcome.get` 响应新增 `capacity` 字段：`{total, protected, evictable, blocking, contacted, limit, protected_limit, overflow, overflow_count, alarm}`；`overflow=true` 表示保护证据已超上限、需要人工归档，不是可以忽略的提示。
- 台账文档是 `{"items": [...], "meta": {...}}`，`meta` 保存最近一次 `merge()` 的容量报告（kept / protected / evictable / evicted / overflow / alarm），老读者只取 `items`，不受影响。
- 反例（已修）：旧实现 `merge` 用 `kept[-limit:]` 统一截断末尾，150 招呼/日下第 4 天就把最早的 `observed_reply` / `resolved_delivered` 挤出去，「同一对象永久不再发」实际只有约 500 条窗口，闸门静默失效。

### 闸门位置

> **最前一道是稳定对象门（方案第 131 行）**：授权目标里没有可证明的稳定标识（平台会话 ID / 真实岗位 ID）时，`chat.submit` 直接拒绝 `SEND_STABLE_TARGET_REQUIRED`，只出草稿、由人自己在平台发送。当前 `real_target()` 只读 name/company/position/salary/city 这些页面文本，读不到 `job_id`，所以**真实自动发送处于关闭状态**；等 DOM 能读到稳定标识就自动放开，不需要改配置或加开关。

1. **沙箱层**（`sandbox.require_channel_authorization`）：台账存在任一未闭环记录时，真实通道的**任何** `external_write` 直接拒绝 `SEND_OUTCOME_UNRESOLVED`。拦在沙箱而不是只拦 handler，是为了让以后新注册的外部写入无法绕过。
   - 这层检查排在工作区已有的受控通道授权（`channel_authorization`）之前。受控通道与真实通道是两条独立的腿，一份还没用掉的渠道授权不能替真实发送作保——否则重新登记一次授权就能带着未核对记录再发一次。
   - 按工具名前缀判定豁免：只有 `runtime.applicant.action.*`（受控页面的动作工具）跳过这层检查，其余一律先查（默认 fail-closed）。
2. **登记层**（`chat.authorize`）：同一目标指纹已有已联系证据 → `SEND_TARGET_ALREADY_CONTACTED`；有未闭环证据 → `SEND_TARGET_UNRESOLVED`。目标身份由 `name + company + position + job_id` 计算指纹，**换正文绕不过去**。
3. **提交层**（`chat.submit`）：领取授权后、点击之前，在同一写事务内再查一次同指纹证据。这条路不经过登记层，所以自己也要挡一遍（纵深防御，任何绕过 `chat.authorize` 直接写授权的脚本都发不出去）。
4. **额度层**：`action=greeting` 时在领取授权的同一 `BEGIN IMMEDIATE` 事务里占招呼额度（用户配置 150/日，`effective_limit` 与平台限制取小）；`action=reply` 不动招呼额度，只受每会话去重与冷却约束。

### 跨工作区不绕过（方案验收矩阵）

台账与额度分开存两份数据，但**判断时看全库，不按工作区过滤**：

- 未闭环阻塞（`sandbox.unresolved_send_blocker`）读所有 `applicant_send_ledger` 文档，任一未闭环就拦；
- 已联系证据与额度汇总走 `_ledger_read_all` / `_attempts_read_all`，换账号/换组织不能把同一真人变回「没联系过」，也不能刷新 150 日上限；
- 写入仍落在当前工作区的文档上（不跨 scope 写，避免权限与迁移问题）。

代价是不同工作区之间会互相阻塞（例如求职者工作区里有一条待核对，招聘者工作区也发不出去）。这个方向的误伤可以接受：宁可停下，也不能对同一个真人重复发送。

### 工作台入口

工作台的「人工回复闭环」面板里有 ⑤ 发送结果核对：

| 端口 | 方法 | 作用 |
| --- | --- | --- |
| `/api/runtime/applicant/chat/outcome` | GET | 读待核对记录 |
| `/api/runtime/applicant/chat/outcome/resolve` | POST | 登记人工结论（`ledger_id` / `resolution` / `confirmed_by`） |

面板只做两件事：列出待核对记录、登记「确认已发出 / 确认没发出」。它**不发送任何消息**，也不提供「已读」「已送达」这类判断——那需要平台侧证据，本机拿不到。核对人会取当前工作区的 `account_id` 一并落库。

`chat.outcome.verify`（读页面回显做只读核对）没有接进面板：它必须连真实浏览器，在浏览器不可用时只会报错，放在以鼠标操作为主的界面上会误导。

### 解除路径（唯一）

`chat.outcome.get` 看还有几条没核对 → `chat.outcome.verify`（只读）用页面当前回显与点击前基线比对，只给三档结论（有新增回显 / 无新增回显 / 读不到）→ 仍不清楚时用 `chat.outcome.resolve` 人工给结论（`delivered` / `not_delivered` / `duplicate_confirmed`，必须带 `confirmed_by`）。

`verify` 与 `resolve` 都不重放提交。`not_delivered` 会把招呼额度占位改成 `not_executed`（不占额度）；`delivered` 会让该对象永久不再重复发送。

## 十、错误码速查

| 错误码 | 含义 |
| --- | --- |
| `SEND_RECIPIENT_REQUIRED` | 还没绑定发送对象 |
| `SEND_RECIPIENT_CHANGED` | 对象或绑定已变化，需重新核对 |
| `STALE_SEND_FACTS` | 资料/绑定/观察边界已过期 |
| `STALE_MANUAL_DRAFT` | 草稿人工修订已变化 |
| `DRAFT_NOT_READY` | 草稿未完成、被替换或规则未识别 |
| `DRAFT_CHANGED_AFTER_RESERVATION` | 预占后草稿被改 |
| `MESSAGE_AGE_EXCEEDED` | 消息超过两分钟未被重新观察 |
| `WINDOW_GAP_EXCEEDED` | 观察中断超过两分钟 |
| `SEND_DAILY_LIMIT_REACHED` | 当日额度已用完 |
| `SEND_ATTEMPT_CONFLICT` | 同一意图已有不同对象的预占 |
| `INVALID_SEND_ATTEMPT_STATE` | 状态机不允许该操作 |
| `HANDOFF_EXPIRED` | 交接卡过时效 |
| `SEND_HANDOFF_REQUIRED` | 未取卡就自报已发 |
| `INVALID_SEND_QUEUE` | 批量参数非法（数量、格式、未确认） |
| `SEND_ALREADY_USED` | 已有自报或回读证据；不退占且不再是待发项 |
| `WINDOW_REBUILT` | 观察起点已重建，旧预占失效 |
| `FORGET_SHARED_OBSERVATION` | 原始观察仍被其他意图引用，拒绝删除 |
| `READBACK_REGRESSION` | 已记录的送达证据被未命中覆盖 |
| `FORGET_BLOCKED_BY_EVIDENCE` | 有发送/交接记录，禁止删除 |
| `MAINTENANCE_REQUIRED` | 该操作只允许本机维护命令 |
| `SEND_OUTCOME_UNRESOLVED` | 上一条真实发送的结果还没核对，先核对再发（沙箱层拦） |
| `SEND_TARGET_ALREADY_CONTACTED` | 这个对象已有联系记录，不再重复发送 |
| `SEND_TARGET_UNRESOLVED` | 这个对象上次结果未核对，先人工核对 |
| `SEND_LEDGER_ENTRY_NOT_FOUND` | 没有这条发送台账记录 |
| `SEND_LEDGER_ALREADY_CLOSED` | 这条台账已经核对过，不能再改 |
| `SEND_RESOLUTION_INVALID` | 核对结论只能是 delivered / not_delivered / duplicate_confirmed |
| `SEND_RESOLUTION_BY_REQUIRED` | 人工核对必须留下核对人 |
| `SEND_LEDGER_IN_FLIGHT` | 这条正处在点击过程中（`submitting`），不能提前给结论 |
| `SEND_STABLE_TARGET_REQUIRED` | 对象没有可证明的稳定标识（平台会话 ID / 岗位 ID），按方案只出草稿，人工发送 |
| `SEND_VERIFY_TARGET_MISMATCH` | 当前打开的对话不是这条记录的对象，切回去再核对 |
| `DAILY_LIMIT_REACHED` | 当日招呼额度已满（真实通道提交前拦截） |

## 十一、运行开关（暂停自动回复）

`runtime_documents` 里一份 `kind='run_state'`（不新建表），跨工作区汇总判断：换账号不能绕过暂停。

| 工具 | effect | 作用 |
| --- | --- | --- |
| `runtime.run.get` | none | 读暂停集、待核对条数、招呼是否放行 |
| `runtime.run.pause` | state | 暂停（需 `confirmed` + `paused_by`，可选 `actions`） |
| `runtime.run.resume` | state | 恢复（可选 `action` 只恢复一种；`expected_generation` 做 CAS） |

HTTP：`GET /api/runtime/run`、`POST /api/runtime/run/pause`、`POST /api/runtime/run/resume`。工作台「人工回复闭环」第 ⑥ 节。

### 边界（用户 2026-10-27 定）

- **只停自动回复**：默认 `actions=['reply']`；`greeting` 照发。
- **不拦人的手工操作**：`effect=local_ui`（把草稿填进输入框）在暂停期间照常允许——暂停要给人工接管让位。
- **跨工作区**：读全库，任一工作区说 `reply` 暂停就按暂停处理。
- **读坏了 fail-closed**：记录不合规或读不到时按「自动回复已暂停」处理。这与台账 blocker 的 fail-open 是**刻意的不对称**：台账读不到当成没记录（等一下就恢复），暂停读不到当成已停（刹车优先）。

### 闸门位置

在 `sandbox.require_channel_authorization` 的**真实会话授权分支**：读出授权里的 `action`，若在暂停集就抛 `RUN_PAUSED`，不点、不写台账。受控通道（`runtime.applicant.action.*`）不走这里，所以受控页验证不受影响。

暂停不解除任何未核对记录：`submitting` 仍拒绝给结论，`outcome_unknown` 仍只能靠人工核对解除。

### 错误码

| 码 | 含义 |
| --- | --- |
| `RUN_PAUSED` | 自动回复已暂停，先恢复；暂停不影响核对发送结果 |
| `RUN_STATE_INVALID` | 开关记录无法核对，按已暂停处理 |
| `RUN_STATE_UNREADABLE` | 读不到开关状态，按已暂停处理 |
| `RUN_ACTION_INVALID` | 暂停范围只能是 reply 或 greeting |
| `RUN_PAUSED_BY_REQUIRED` | 暂停/恢复必须留操作人 |
| `RUN_STALE_GENERATION` | 记录已被别的操作改过，重新读取再操作 |

| `SEND_LEDGER_ARCHIVE_INVALID` | 归档参数无效（缺操作人/既没给条目也没给目标） |

## 十二、台账归档（把已联系证据搬出活跃台账）

### 为什么需要它

台账的保护集（`submitting` / `submitted_unverified` / `outcome_unknown` / `conflict` /
`observed_reply` / `resolved_delivered`）**永不因容量淘汰**，超过 `MAX_PROTECTED_KEPT`
（5000）只报警。报警不能一直不管：150 招呼/日的气候下，长期运行一定会撞上限。
归档出口把**已联系证据**搬进同库的另一份文档，给活跃台账腾额度。

### 位置与形状

- 活跃：`runtime_documents`，`kind='applicant_send_ledger'`
- 归档：`runtime_documents`，`kind='applicant_send_ledger_archive'`（同库、同 scope、不新建表）
- 归档文档**没有容量上限**（再套一层上限只是把删除挪个地方），只报告条数

### 核心不变量：搬走 ≠ 忘掉

所有「这个对象联系过吗 / 还没闭环吗」的判断走同一个入口 `_ledger_read_all`，
它读**活跃 ∪ 归档**并按 `ledger_id` 去重（同 id 以活跃为准）。因此：

- `chat.authorize` / `chat.submit` 的两次同指纹检查照样报 `SEND_TARGET_ALREADY_CONTACTED`
- `outcome.get` / `verify` / `resolve` 照样看得到归档里的条目
- 沙箱层 `unresolved_send_blocker` 的 kind 查询是 `IN (active, archive)`，fail-closed

写入仍只落本 scope 的活跃文档，归档条目不会被"拉回"。

### 只允许归档已联系证据

| 状态 | 归档 | 错误码 |
| --- | --- | --- |
| `observed_reply` / `resolved_delivered` | ✅ 允许 | — |
| `submitting` | ❌ 那次点击还在路上 | `SEND_LEDGER_IN_FLIGHT` |
| `submitted_unverified` / `outcome_unknown` / `conflict` | ❌ 搬走等于关闸门 | `SEND_LEDGER_UNRESOLVED` |
| `resolved_not_delivered` / 未知状态 / 脏数据 | ❌ 本来就不是证据 | `SEND_LEDGER_NOT_ARCHIVABLE` |

状态缺失或无法识别一律 fail-closed（不归档）。点名归档时只要有一条不合格，**整批拒绝**，
不做部分成功；整区归档只挑已联系证据，阻塞态原地留下。

### 怎么调用

工具：`runtime.applicant.chat.outcome.archive`（`effect='state'`，进 `RECEIPTED_CONTROLS`）

```json
{"ledger_ids": ["<20 位条目 id>"], "confirmed_by": "操作人"}
{"target_sha256": "<64 位目标指纹>", "confirmed_by": "操作人"}
{"confirmed_by": "操作人"}
```

CLI：

```bash
python main.py outcome archive --by 操作人 --ledger-id <id>
python main.py outcome archive --by 操作人 --target <64位指纹>
```

HTTP（工作台第⑤节「归档已联系证据」按钮走这条）：

`POST /api/runtime/applicant/chat/outcome/archive` — 请求体 `{"confirmed_by": "操作人"}` 整区归档已联系证据，或再加 `"ledger_ids": ["<20 位条目 id>"]` 点名归档。身份校验仍是 `X-Jobkit-Context` + loopback，和 `outcome.get` / `outcome.resolve` 一致；缺头或旧 token 一律 `STALE_CONTEXT`，不放宽。

返回 `archived` / `already_archived` / `active_count` / `archive_total` / `capacity` / `archive`。
重复归档同一 `ledger_id` 或同一 `target_sha256` 幂等（只有一条，第二次报 `already_archived`）。

### 可恢复

归档是带凭据控制：崩溃恢复时 `committed_result` 从**两个文档重读**核对——每条 `archived`
必须已不在活跃文档、在归档文档里且是已联系证据，`archived_by` 必须等于回执里的操作人。
核对不过一律 `INVALID_COMMIT_RECEIPT`，不重放。


### 2026-10-28：结果核验是纯读取

`runtime.applicant.chat.outcome.verify` 保持 `effect=none`，不修改发送台账、招呼占位或额度。响应 `state` 是读取时的原台账状态，`observed_state` 是本次回显对比的投影，`ledger_updated=false`。有新回显也保持 `needs_human=true`，不能据此解除未决阻断或称为平台送达。人工核对后仍通过原 `outcome.resolve` 显式记录结论。

读取失败保留原错误码，不把超时改装成对象缺失。内核仍保存只读任务的正常审计轨迹；“不写台账”不表示完全不产生审计记录。


### 2026-10-28：明确选择未决记录

存在多条未决记录时，`outcome.verify` 必须传入 `ledger_id`，否则返回 `SEND_VERIFY_SELECTION_REQUIRED`，不会先读取浏览器。仅有一条时仍可省略。CLI 用法：`python main.py outcome verify <ledger_id>`。无效标识返回 `INVALID_BROWSER_REQUEST`，不存在的记录返回 `SEND_LEDGER_ENTRY_NOT_FOUND`。

`outcome.get` 新增 `verification_evidence`，逐条给出 `ledger_id`、`baseline_present` 和 `target_fingerprint_present`。这些字段只说明本地证据是否存在，不表示身份或送达已验证。

核验读取后若记录被修改或解除，返回 `SEND_VERIFY_STATE_CHANGED`，不写台账，不自动重试。原 `state` / `observed_state` / `ledger_updated=false` 约定不变。


### 2026-10-28：搜索组合的城市覆盖

新建搜索会话按“优先级 → 关键词 → 城市”生成组合；默认先排广州 Agent、深圳 Agent，再排广州 Agentic、深圳 Agentic。最多四个组合、60 个岗位、600 秒等预算保持不变。排入组合不代表已访问，达到预算可能提前停止。

`jobs.search.get` 的 `search_coverage` 区分排入的城市、尝试过页面的城市、有新增岗位的城市和本批未排入的城市，并显示计划组合总数与本批组合数。`partial_schedule=true` 明确本批只覆盖部分配置；优先级高的城市尚未排完时，次选城市可能全部未排入，不能宣称六城都已搜索。

已有会话不改写、不重置，新顺序只用于明确新建的会话。旧会话仍可读取，覆盖视图会暴露其遗漏城市；若求职计划版本变化，覆盖仅返回 `status=plan_changed`，不把新计划与旧会话混算。持久化会话 schema 没有新增字段。
