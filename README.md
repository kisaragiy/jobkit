# jobkit

一个**单内核**的求职工作台：把「找岗位 → 看匹配理由 → 出草稿 → 人工核对 → 记录结果」做成可追踪的本地流程，
所有动作都经过同一套执行链，状态只写一个 SQLite。

设计目标不是"无人值守投简历"，而是**每一步都能被审计、每个动作都有证据、不确定的时候宁可不发**。

---

## 为什么不是又一个爬虫 / 自动投递脚本

求职自动化的难点不是点按钮，是**判断边界**：

- 这个岗位到底是不是我要的？（标题命中 ≠ 职责命中）
- 这条消息是不是**这次**触发的？（历史同文消息不能算）
- 刚才那一下到底发出去没有？（超时 ≠ 没发，也 ≠ 发了）
- 已经联系过的人会不会被再发一次？（换账号、换工作区、隔天都不该绕过）

jobkit 把这些都变成**显式的状态与凭据**，而不是散落在脚本里的 `if`。

---

## 核心设计

### 1. 单一执行链

```
Web / CLI
  → main.dispatch
    → ToolRouter（唯一注册表）
      → ExecutionEngine（唯一执行器：任务、回执、恢复）
        → sandbox（作用域 / 能力 / 外部动作授权）
          → 领域工具
            → SQLite（唯一事实来源）
```

没有第二个执行入口，没有旁路脚本，没有独立发送队列。

### 2. 证据门，不是开关

缺少稳定对象标识时，自动发送不会执行，返回：

```
SEND_STABLE_TARGET_REQUIRED
这个对象还没有可证明的稳定标识（平台会话 ID / 岗位 ID），按方案只提供草稿，请人工在平台上发送。
```

这是**证据门**：将来能稳定读到对象标识，同一个门自动放行，不需要改配置、不需要人来开闸。

### 3. 发送台账：点过就必须留痕

| 状态 | 拦后续发送 | 算已联系 |
| --- | --- | --- |
| `submitting` | 是 | 否 |
| `submitted_unverified` | 是 | 否 |
| `outcome_unknown` | 是 | 否 |
| `conflict` | 是 | 否 |
| `observed_reply` | 否 | 是 |
| `resolved_delivered` | 否 | 是 |
| `resolved_not_delivered` | 否 | 否 |

- 跨天不自动放行，跨工作区（换账号）也不绕过
- 有未闭环记录时，真实通道的**任何**外部写入都在 sandbox 层被拒（不只拦 handler）
- 只有人工核对能给结论，核对必须留核对人
- 已联系证据永不被容量淘汰；超上限只报警，需要显式归档

### 4. 可恢复：崩溃不产生"没发生过"

每个带副作用的控制都有专属的 `committed_result` 校验：恢复时**从库重读**比对操作、scope、版本、
正文摘要与不可变关联；核对不过一律 `INVALID_COMMIT_RECEIPT`，绝不重放提交。

### 5. 运行版本可证明

`GET /version` 返回进程启动时的核心文件指纹：

```json
{"app_version":"0.1.0","build_fingerprint":"c7a030…","git_commit":"a4ee4eb…",
 "files":{"runtime_tools.py":"aa94be7e19ea","sandbox.py":"0d2ec6439a58"}}
```

配套 `scripts/check-version.py` 把「运行中的进程」与「磁盘上的代码」逐文件对比：
`0` 一致 / `2` 运行版本落后 / `1` 不可达。**磁盘测试通过 ≠ 运行版本正确**。

---

## 能力

| 能力 | 状态 |
| --- | --- |
| 岗位检索与匹配（标题 + 职责、城市、薪资、学历/经验、排除词） | ✅ 每条带来源 / 观察时间 / 证据片段 / 理由码 |
| 岗位详情 JD 读取（固定只读脚本 + 双重对象核对） | ✅ 缺 JD 时进人工核对，不硬判匹配 |
| 规则草稿生成（未知事实转人工，不编造履历） | ✅ |
| 人工核对 → 复制发送 → 回读结果 | ✅ |
| 发送台账 / 结果闸门 / 跨工作区拦截 / 额度 | ✅ |
| 暂停与接管（只停自动回复，给人工让位） | ✅ |
| 归档已联系证据（活跃 ∪ 归档仍拦） | ✅ |
| 运行版本指纹与依赖自检 | ✅ |
| 无人值守自动发送 | ❌ 证据门关闭（缺稳定对象标识） |

---

## 快速开始

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
pip install -r requirements-ocr.txt                  # 薪资 OCR（可选）

python main.py serve --host 127.0.0.1 --port 18770
# 打开 http://127.0.0.1:18770/workspace
```

检查环境：

```bash
python scripts/check-deps.py       # 依赖与 OCR 就绪情况（0 正常 / 2 有缺失 / 1 出错）
python scripts/check-version.py    # 运行版本 vs 磁盘（0 一致 / 2 落后 / 1 不可达）
```

跑测试：

```powershell
# Windows PowerShell，约 8 分钟（1300+ 项 + JS 检查）
.\scripts\check-kernel.ps1
```

全部测试使用临时数据库与虚构数据，不连平台、不碰真实用户库。

---

## 目录

```
main.py                  CLI / HTTP 入口（dispatch → router → engine）
tool_router.py           唯一工具注册表
execution_engine.py      执行、任务、回执与崩溃恢复
sandbox.py               作用域、能力、外部动作授权
runtime_tools.py         领域工具编排
store.py                 原 SQLite 与运行时文档
applicant_browser.py     只读 DOM 适配（固定脚本，不接受任意 JS/URL）
applicant_jobs.py        岗位记录、校验、匹配理由
applicant_job_match.py   标题 + JD 匹配（纯函数）
applicant_send_ledger.py 发送台账（纯逻辑，无 IO）
run_state.py             暂停状态（纯逻辑）
web/workspace.html       工作台（单页，无构建步骤）
scripts/check-kernel.ps1 全量门禁
```

---

## 边界与合规

这个项目**明确不做**以下事情，代码里也有对应的拒绝路径：

- 不破解验证码、不做指纹伪装、不通过换号/换入口规避平台限制
- 不导出 Cookie、不恢复旧的旁路发送脚本
- 不把「页面回显有新增」说成「平台已送达」或「对方已读」
- 不编造工作年限、薪资、面试承诺等未知事实
- 不把本地预占当成平台额度

平台交互以**只读观察优先**；任何写操作都必须先有明确授权、即时核对与可核对的结果记录。
平台规则与可用性可能变化，本项目不承诺零风控，也不保证任何平台功能长期可用。

## 隐私

- 运行时数据（SQLite、浏览器 profile、日志、导出）全部在 `.gitignore` 内，不进版本库
- 仓库内不含任何真实简历、用户名、手机号、邮箱、聊天正文
- 测试一律使用虚构主体（示例公司 / 示例招聘方 / 张三）

## License

MIT
