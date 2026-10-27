# AGENTS.md — jobkit

> 给接手这个仓库的人（或未来的我）看的现状说明书。改动前先读这一页。

## 一、这个项目是什么

BOSS 直聘求职自动化工具。核心设计原则：**任何对外动作必须是人工确认后的单次执行，且平台没确认的事不许说成完成。**

分代推进（每代都有全量门禁与验收文档）：

| 代 | 内容 | 测试基线 |
| --- | --- | --- |
| G1 | 人工回复闭环 | 1065 |
| G2 | 有界只读岗位搜索 | 1121 |
| G3 | 单次受限动作授权 + 六态结果协议 | 1161 |
| G4 | 招呼配额 / 去重 / 跨日核对（纯逻辑层） | 1211 |
| G4+ | 真实对话页只读通道 | 1231 |
| G4+ | 真实身份读取 `chat.target` | 1238 |
| G4+ | 真实会话严格授权层（B 方案） | 1266 |
| G4+ | 真实发送端到端打通（authorize→fill→submit→observe） | 1266 |
| G4+ | **修 delivered_marks 统计口径** | **1274** |

## 二、跑测试 / 门禁

```bash
# 单模块
/c/Users/<user>/AppData/Local/Programs/Python/Python311/python.exe -m unittest test_real_chat

# 多进程并发探针（额度占位在真实子进程竞争下是否超限/丢额度/锁冲突）
# 0 = 恰好占满上限且无锁冲突；2 = 有并发问题；1 = 探针自己没跑成
python scripts/concurrency_probe.py --workers 6 --per-worker 25 --limit 40

# 全量门禁（约 440-500 秒）
{ PATH="/c/Users/<user>/AppData/Local/Programs/Python/Python311:$PATH" \
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/check-kernel.ps1 \
  > tmp/commercial-audit/gate.log 2>&1; st=$?; echo "GATE_EXIT=$st" >> tmp/commercial-audit/gate.log; exit $st; }
```

**三个必须知道的坑**：

1. **必须显式加 Python311 到 PATH**，否则跑的不是目标解释器。
2. **门禁约 440-500 秒，超过 `terminal` 前台 420 秒上限** → 用 `background=true, notify=true`，再用 `sleep` 轮询日志；**不要用 `process wait`**（会 kill 进程）。
3. **不要用 `; echo "GATE_EXIT=$?"` 收尾**——末尾的 `echo` 让整个进程退出码恒为 0，后台通知永远假绿。用 `{ ...; exit $st; }`，然后读日志里的 `GATE_EXIT`。

新模块必须登记进 `scripts/check-kernel.ps1`，且**不要重复登记**（重复会让测试数虚高）。

## 三、两条硬不变量（测试锁死，改之前先看）

1. **维护工具不得有 HTTP 路由**——只有 `effect='none'` 的 `.get` 类工具可以暴露 HTTP。
2. **集合恒等**：`{kind=control 且 effect=state}` 必须等于 `execution_engine.RECEIPTED_CONTROLS`（`test_execution_receipts` 双向差集断言）。`local_ui` 类（如 `jobs.search.open`）**不得**进白名单。

## 四、真实平台 DOM（2026-10-27 实测，求职者登录态）

**全部来自真机快照，不是推断。** 详细证据见 `docs/reviews/boss-dom-probe-2026-10-27.md`。

| 用途 | 选择器 | 备注 |
| --- | --- | --- |
| 列表容器 | **`ul.rec-job-list`** | 原 `.job-list-box, ul.job-list` 已失效，保留为兜底 |
| 岗位标题 | `a.job-name` | ✅ 命中 |
| 详情链接 | `a.job-name[href*="/job_detail/"]` | href 里 `/job_detail/<加密串>.html`，该串即 securityId |
| 沟通入口 | `a.op-btn.op-btn-chat` | 列表页存在 |
| 输入框 | **`div.chat-input`** | **contenteditable 的 div，不是 textarea** |
| 发送按钮 | **`button.btn-send`** | 空内容时 class 尾缀 `disabled` |
| 会话列表 | `.user-list-content li` | 实测 44 条，364×78 |
| 可点区域 | `.friend-content` | 点**水平 35% 处**，中心/avatar 不响应 |
| 面板容器 | `.chat-conversation` / `.chat-no-data` | 后者 = 未选中占位 |
| 对话身份 | `.top-info-content` 内 `.name-text` / `.user-info span` / `.position-name` | 名字 / 公司 / 职位 |
| 送达标记 | `i.message-status.status-delivery` | 文本 `[送达]`；**注意：左侧会话列表 `.gray.last-msg` 里也有同名标记**，统计必须限定在右侧 `.message-content` 内 |
| 消息区容器 | `.chat-conversation .message-content` | 只有这里的标记才代表本会话消息状态 |
| 已读标记 | `i.message-status.status-read` | 文本 `[已读]` |

### 打开对话的唯一有效配方

```
mouseMoved → mousePressed(buttons=1) → mouseReleased(buttons=0)
坐标 = .friend-content 的 (x + w*0.35, y + h/2)
```

**实测无效的四种**（别再试）：`el.click()`、点 li 中心/avatar、Vue `handleOpenChat()`、缺 `mouseMoved` 的 press/release。

点击必然触发 SPA 导航 → CDP target 断（`WinError 10054`）。**断连不等于失败**，但必须重连后再读。

### 选中态无法从列表判断（重要）

**实测：会话列表项的 class 点击前后完全不变。** 选中态不用 class 标记，所以「当前对话是谁」只能从 `.top-info-content` 面板读。要发消息必须先手动打开目标对话。

### 登录态判定

**cookie 存在 ≠ 登录有效。** 必须看最终 URL 是否被 302 到 `/web/user/`，或页面有无「登录/注册」按钮。招聘方账号（`isOHPC=1`）进不去 `web/geek/*`，两套身份不互通。

## 五、CDP 注意事项

- Edge 148+ 以 `403 Forbidden` 拒绝带 `Origin` 的 WebSocket → 用 `websocket.create_connection(..., suppress_origin=True)`，**不需要**重启浏览器加 `--remote-allow-origins`。
- 连本机 Edge 一律带 `http_no_proxy=['127.0.0.1', 'localhost']`，别被环境代理劫持。
- 探针（`scripts/probe_boss_*.py`）只做 `Runtime.evaluate` 读 DOM；`probe_boss_composer.py` **适合一次性取证，不适合循环跑**（多次运行后 chat tab 会消失）。

## 六、编码约定

- **契约测试用组合不用继承**：`self.f = harness.SendTargetTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)`，否则 fixture 测试方法会重复执行。
- **SQLite 只读查询用 `file:...?mode=ro` 的 URI 连接**。`store.connect()` 每次都跑 `_migrate()`（`BEGIN IMMEDIATE` 取写锁），在已有事务里二次开库必然 `database is locked`。
- **绝不能 `except Exception: return None`**——会把「读不到」伪装成「没有」，是静默失败。只吞具体的 `sqlite3.Error`。
- **一次性凭证消费必须乐观锁**：`UPDATE ... WHERE revision=?` + `rowcount==1`，防止两个进程各执行一次。
- **配额占位与检查必须同一事务**：两个进程各自读到 `used=149` 会双双放行。
- **判据必须是目标元素非空**，不是「看起来不像空状态」。踩过：按 URL 长度挑 tab，`/web/geek/jobs` 排前面且 `stillEmpty=false`，被误判成命中。

## 七、当前未完成

| 项 | 阻塞原因 |
| --- | --- |
| ~~`real_fill` / `real_submit`~~ | ✅ 已实测端到端成功（2026-10-27），见 `docs/api/applicant-conversation.md` |
| 真实会话目标核对 | ✅ 已实现 `runtime.applicant.chat.target`（读 `.top-info-content`） |
| 详情页真实选择器 | 需登录态再抓一次 |
| `platform_limit` 自动探测 | 目前人工登记，带 24h 过期 |
| ~~`chat.authorize` 工具化~~ | ✅ 已做成内核工具（`effect='state'`，进 `RECEIPTED_CONTROLS`） |
| ~~`delivered_marks` 语义不准~~ | ✅ 已修：限定 `.chat-conversation .message-content` 作用域，作用域缺失返回 0；加回归护栏防止退回全局查询 |
| ~~真实会话授权口径~~ | ✅ B 方案：身份快照 + 90s 确认凭证 + 120s 授权 + 发送前逐字段核对 |
| G2 详情页 JD 读取 | ✅ 已实现 `runtime.applicant.jobs.detail`（固定 JS `applicant_job_detail_read.js` + 双重 job_id 核对）；**live 选择器未验证**（CDP 未开），首次真实搜索要抽核 `.job-sec-text` 是否命中 |
| 台账保护集溢出 | ✅ 后端 `capacity.overflow` + 工作台 `#capacityAlarm`；✅ 归档出口 `chat.outcome.archive`（活跃∪归档仍拦） |
| `scripts/check-version.py` | ✅ 运行版本 vs 磁盘三档（0 一致 / 2 落后 / 1 不可达）。注意本机 18770 是旧进程，现在跑会给 401 → 重启后才 0 |
| 台账归档文档无上限 | 有意为之（再套上限等于把删除挪个地方）；长期增长由人工决定 |
| 本人手动发言即停 | 仍空白：页面不提供发送者凭据，无法证明「本人」，硬做只能靠启发式误伤 |

## 八、工作区状态

**核心文件已纳入 git**（commit `a4ee4eb`，2026-10-27）：`main.py` / `tool_router.py` /
`execution_engine.py` / `sandbox.py` / `runtime_tools.py` / `store.py` / `web/workspace.html`
及全部 applicant 模块、测试、docs 均已跟踪，提交前工作区从 148 项未提交归零。

- 数据与运行时产物仍不入库：`.gitignore` 覆盖 `*.db / *.db-wal / *.log / tmp/ / .browser_profile/ / session.enc`。
- 后续改动走 worktree 分支（`git worktree add ../jobkit-p8x -b p8/x`），合并前跑门禁拿证据。
- 提交只在本机分支，不 push。改动前先 `git status` 确认干净，别在脏树上开 worktree。
