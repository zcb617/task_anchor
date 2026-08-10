# Task Anchor：任务指令跨压缩传递方案

版本：5.4
日期：2026-07-16
目标 Codex CLI：0.144.1  
目标目录：`D:\work\task_anchor`

> **2026-08-10 实现增补**：本文原版本只描述任务锚定与压缩恢复。当前实现已在不改变任务状态语义的前提下增加受管进程执行能力：通过 `.mcp.json` 暴露 `mcp__task_anchor__managed_exec`，由资源登记器记录 PID/工作目录/停止策略，`PreToolUse` 拒绝直接 Shell 调用，`Stop` 清理默认资源；`stop_policy = keep` 的资源保留到显式停止。本文中“插件不使用 MCP/Stop Hook”及仅列出两个 Hook 的旧描述以当前代码、README 和 Skill 为准。

## 1. 唯一目标

本插件只解决一个问题：

> 同一个任务无论经历多少次上下文压缩，压缩后都重新获得同一份最初任务指令，不依赖上一轮压缩摘要，不发生逐轮转述和方向漂移。

所有设计都必须直接服务于这个目标。不能直接帮助任务指令持续传递的机制，不加入插件。

## 2. 核心原理

任务开始时，将最初任务指令保存到上下文之外。

以后每次压缩完成，都直接读取这份原始数据并重新注入：

```text
第一次压缩 ─┐
第二次压缩 ─┼── 始终读取同一份原始任务指令
第 N 次压缩 ─┘
```

禁止使用“上一次压缩后的任务摘要”生成下一次任务指令。这样压缩次数不会造成累计偏差。

## 3. 插件组成

```text
task-anchor/
├── .codex-plugin/
│   └── plugin.json
├── skills/
│   ├── task-anchor/
│   │   ├── SKILL.md
│   │   └── agents/
│   │       └── openai.yaml
│   └── task-anchor-end/
│       ├── SKILL.md
│       └── agents/
│           └── openai.yaml
├── hooks/
│   └── hooks.json
├── scripts/
│   └── hook_entry.py
└── tests/
```

插件只包含：

1. 用于显式开始的 `$task-anchor` Skill。
2. 用于显式结束的 `$task-anchor-end` Skill。
3. `UserPromptSubmit` Hook：保存新任务或结束当前任务。
4. `PostCompact` Hook：仅在任务仍处于活动状态时恢复最初任务指令。
5. 一个负责保存、结束、校验和读取任务指令的 Python 脚本。

明确不包含：

- MCP。
- 小模型或子代理。
- 自定义 TOLIST。
- `PostToolUse`。
- `PreCompact`。
- `SessionStart(source=compact)`。
- `Stop`。
- 自定义删除或副作用命令审批。

## 4. 如何确定任务开始

任务不会由 Hook 猜测。

用户显式调用 `$task-anchor`，该条用户消息就是本 Codex 任务的初始任务指令和任务边界。

示例：

```text
$task-anchor 按已经讨论确定的方案开始实现。
```

`UserPromptSubmit` 在技术上会对每条用户消息触发。由于该事件不支持 matcher，Python 处理器必须自行判断：

1. 当前提示词既不包含显式 `$task-anchor`，也不包含显式 `$task-anchor-end`：立即退出，不保存、不注入、不改变状态。
2. 首次显式调用 `$task-anchor`：保存 Hook 收到的完整提示词原文，并将锚点设为活动状态。
3. 活动锚点存在时再次调用 `$task-anchor`：不覆盖最初任务指令。
4. 显式调用 `$task-anchor-end`：将当前锚点设为已结束。
5. 锚点已结束后再次显式调用 `$task-anchor`：用新的完整提示词替代已结束记录，开始下一项独立任务。

插件不自动猜测任务何时开始或结束；两个状态转换都必须由用户显式调用相应 Skill 触发。

## 5. 最初任务指令的保存

使用 Codex Hook 提供的 `session_id` 作为当前 Codex 任务标识，不再自建语义 task_id。

保存位置：

```text
PLUGIN_DATA/
└── sessions/
    └── <session_id>/
        ├── initial_instruction.txt
        └── metadata.json
```

`initial_instruction.txt` 保存 Hook 收到的完整用户提示词。

`metadata.json` 保存：

```json
{
  "session_id": "...",
  "sha256": "...",
  "workspace_sha256": "...",
  "created_at": "...",
  "active": true
}
```

结束后会将 `active` 写为 `false`，并增加 `ended_at`；原始指令保留用于审计和校验，但不再恢复。

规则：

- 活动锚点的初始指令只写入一次；活动期间重复调用禁止覆盖。
- 已结束锚点随后收到新的显式 `$task-anchor` 时，允许以新任务原文替代。
- 每次读取都校验 `session_id`、项目身份和 SHA-256。
- 不对指令做摘要、改写、清理或重新生成。
- 不把压缩恢复时注入的内容重新当成新的用户指令。

## 6. Skill 的职责

`$task-anchor` Skill 负责指导主模型：

1. 开始工作前使用 Codex 原生 TOLIST 建立计划。
2. 工作过程中持续更新 Codex 原生 TOLIST。
3. 用户改变目标、约束或执行方向时，立即更新原生 TOLIST。
4. 如果任务引用前文讨论或某个文件，在第一次执行时把具体工作步骤落实到原生 TOLIST。
5. 不创建额外的 TOLIST 文件、任务数据库或进度系统。

TOLIST 只是 Codex 自己的执行状态，不属于 Hook 的任务指令传递链路。

Hook 不读取、不保存、不复制、不注入 TOLIST。

## 7. 为什么不需要 PreCompact

最初任务指令在任务开始时已经保存到上下文之外，不需要等到压缩前再次处理。

`PreCompact` 无法改善原始指令的准确性，反而会增加一次无意义处理，所以不注册。

压缩前的任务进度由 Skill 持续更新到 Codex 原生 TOLIST，不由 Hook 临时总结。

## 8. 压缩后的恢复

上下文压缩成功后，Codex 触发 `PostCompact`。

Hook 先检查当前 `session_id` 的锚点是否仍为活动状态；只有活动锚点才会读取并校验最初任务指令，再通过 `additionalContext` 注入：

```text
必须使用 $task-anchor Skill 继续当前任务。

[最初任务指令]
<initial_instruction.txt 的原文>
```

只注入：

1. 要求重新使用 `$task-anchor` Skill。
2. 最初任务指令原文。

不注入：

- Skill 内部规则。
- TOLIST。
- 工具调用记录。
- 历史压缩摘要。
- 上一次恢复包。
- 全部对话记录。

## 9. 为什么上下文不会不断增长

每次压缩后注入的内容始终只有一份固定的最初任务指令。

恢复内容不采用追加式历史：

```text
恢复包大小 = 固定提示语 + 最初任务指令
```

恢复包大小与压缩次数无关。

## 10. 中途用户干预

插件不复制每一条用户消息。

任务执行期间，如果用户修改目标、约束或工作方向，正在运行的 Skill 要求主模型立即更新 Codex 原生 TOLIST。

普通讨论继续由 Codex 自身保存，Hook 不重复建立一套对话日志。

本版本由插件严格保证的是：最初任务指令始终原样恢复。中途干预依赖 Skill 对原生 TOLIST 的持续更新，不由 Hook另外保存。

## 11. 活动任务内再次调用 Skill

同一 `session_id` 内活动锚点存在时再次显式调用 `$task-anchor`：

- 不创建新任务。
- 不替换初始指令。
- 不清空原生 TOLIST。
- 只要求模型重新加载并继续执行 Skill。

`PostCompact` 注入属于 developer context，不会触发 `UserPromptSubmit`，因此不会覆盖初始任务指令。

## 12. 显式结束与同会话新任务

一个 Codex 对话（一个 `session_id`）可以依次承载多项独立任务，但任意时刻只有一个活动锚点。

用户完成或放弃当前任务时显式调用 `$task-anchor-end`。Hook 将元数据标为已结束；之后的普通对话即使发生压缩，也不会恢复旧指令。

要在同一对话开始下一项任务，用户必须先结束旧任务，再显式调用新的 `$task-anchor`。新调用会替代已结束记录。插件绝不根据一次回复、TOLIST 是否为空或 `Stop` 自动结束任务。

## 13. 权限边界

插件不处理：

- 删除文件确认。
- 副作用命令确认。
- 网络权限。
- 沙箱升级。
- 工具审批。

这些继续由 Codex 自身安全权限模块负责。

## 14. Hook 配置

`hooks/hooks.json` 只注册：

```text
UserPromptSubmit
PostCompact
```

`UserPromptSubmit` 的 matcher 不受支持，因此必须在 `hook_entry.py` 内执行显式开始和显式结束判断。

`PostCompact` 不配置 matcher；脚本只接受 `auto` 和 `manual` 两类压缩触发，并在锚点已结束时直接跳过恢复。

### 14.1 Hook 信任是运行前提

安装或启用插件不等于 Hook 已启用。Task Anchor 的命令 Hook 属于非托管 Hook，Codex 必须先让用户审查其定义，并记录用户的允许和信任结果。

在 Hook 获得允许和信任之前，Codex 会直接跳过 Hook：

- `UserPromptSubmit` 不会保存最初任务指令，也不会结束任务。
- `PostCompact` 不会在压缩后注入任务指令。
- Skill 即使能够显示和调用，也不代表任务锚点已经建立或已经结束。

因此，“Hook 已允许且已信任”属于安装完成条件，不是可选安全提示。Hook 文件变化后，其哈希也会变化，Codex 会要求重新审查和信任；重新信任之前仍会跳过新 Hook。

如果用户在 Hook 信任前已经调用过 `$task-anchor`，插件不会追溯或自动补存那条消息。信任后必须重新显式调用一次并附上要锚定的任务指令。

### 14.2 最小审计日志

为区分“Codex 没有触发 Hook”“锚点读取失败”和“已经生成恢复上下文”，插件在上下文之外写入：

```text
PLUGIN_DATA/audit/events.jsonl
```

日志只包含：

- UTC 时间。
- Hook 事件名和 `source`。
- `session_id` 的 SHA-256，不保存原始 `session_id`。
- 执行状态。
- 指令字节数和 SHA-256。

日志禁止保存任务指令正文，也不记录没有显式 `$task-anchor` 的普通用户消息。审计日志不会注入模型上下文。

一次成功压缩恢复应按顺序留下：

```text
post_compact_received
anchor_loaded
restore_emitted
```

一次显式结束应留下 `deactivation_received` 和 `deactivation_completed`；结束后的压缩应留下 `post_compact_received` 和 `restore_inactive`，而不应出现 `restore_emitted`。

## 15. 验收测试

### 激活

1. 插件已启用，但 Hook 未信任时，确认 Hook 被跳过且不会创建任务指令文件。
2. Hook 完成允许和信任后，普通用户消息不会创建任务指令文件。
3. 显式调用 `$task-anchor` 后保存完整提示词。
4. 保存内容与 Hook 输入字符串一致。
5. 初始指令 SHA-256 可重复计算。

### 防覆盖

5. 同一任务再次调用 Skill 不覆盖原始文件。
6. 普通后续消息不修改原始文件。
7. 压缩恢复注入不会被重新采集为任务指令。

### 压缩恢复

8. 手动压缩后触发 `SessionStart(source=compact)`。
9. 自动压缩后触发 `SessionStart(source=compact)`。
10. 恢复内容明确要求使用 `$task-anchor`。
11. 恢复内容包含完整最初任务指令。

### 多轮稳定性

12. 连续多次压缩后，注入内容的指令 SHA-256 始终一致。
13. 恢复包大小不随压缩次数增加。
14. 恢复包不包含工具日志、旧摘要或历史恢复包。

### 显式结束与同会话新任务

15. 显式调用 `$task-anchor-end` 后，元数据变为 `active: false`，并写入结束时间。
16. 已结束后触发自动或手动压缩，不产生 `additionalContext`，审计记录 `restore_inactive`。
17. 已结束后在同一 `session_id` 显式调用新的 `$task-anchor`，原始指令被替换且重新变为活动状态。
18. `Stop`、一次回复完成或 TOLIST 暂空不会自动结束任务。

### Skill

19. Skill 使用 Codex 原生 TOLIST。
20. Skill 不创建自定义 TOLIST。
21. 压缩后注入的 `$task-anchor` 指令能使主模型重新使用 Skill。

## 16. 完成标准

只有同时满足以下条件才能宣布插件完成：

- 安装一个插件即可安装 Skill 和全部 Hook。
- 用户已在 Codex Hook 设置中允许并信任 Task Anchor Hook；否则安装不算完成。
- 用户在任务开始时显式调用 `$task-anchor`，在任务完成或放弃时显式调用 `$task-anchor-end`。
- 最初任务指令按 Hook 收到的原文保存。
- 活动任务的后续调用不会覆盖原文；结束后新的显式任务可以替代已结束记录。
- 只有活动锚点会在每次压缩后从原始存储重新注入；已结束锚点不会注入。
- 恢复不依赖上一轮压缩摘要。
- 多次压缩后指令哈希保持不变。
- Skill 使用 Codex 原生 TOLIST。
- Hook 完全不处理 TOLIST。
- 不使用 MCP、小模型或自定义任务管理系统。

## 17. 客观边界

1. 插件能原样保存的是 `UserPromptSubmit` Hook 实际收到的文本。
2. 如果任务指令只写成“按上面做”，具体方案必须在首次执行时由 Skill落实到 Codex 原生 TOLIST；插件不会把全部前文重复注入。
3. 如果任务依赖文件，初始指令中的文件引用会被保留，但文件本身仍由 Codex按需读取。
4. 本版本不保证将每一条中途用户消息原样永久注入；它只保证最初任务指令原样跨压缩传递。
5. `PostCompact` 重新注入后是否能稳定触发 Skill，必须在目标 Codex 版本中实测。

## 18. 最终工作链路

```text
用户显式调用 $task-anchor
        ↓
UserPromptSubmit 保存最初任务指令原文，并标为 active
        ↓
Skill 使用 Codex 原生 TOLIST 推进任务
        ↓
发生上下文压缩
        ↓
PostCompact
        ↓
锚点 active？──否──→ 不注入旧指令
        │
        是
        ↓
从原始存储读取同一份任务指令
        ↓
注入“必须使用 Skill + 最初任务指令原文”
        ↓
用户显式调用 $task-anchor-end 时写入 active: false；之后可在同一对话显式开始下一项任务
```

最终原则：

> 压缩可以改变工作上下文，但永远不能改写任务的起点。

## 19. 安装与分发

源码按可克隆的 Marketplace 仓库组织：

    .agents/plugins/marketplace.json
    plugins/task-anchor/.codex-plugin/plugin.json
    plugins/task-anchor/hooks/hooks.json
    plugins/task-anchor/scripts/hook_entry.py
    plugins/task-anchor/skills/task-anchor/SKILL.md
    plugins/task-anchor/skills/task-anchor-end/SKILL.md

克隆源码并进入仓库根目录后执行：

    codex plugin marketplace add .

Codex CLI 0.144.1 已实测只有 Marketplace 管理命令，没有 codex plugin install 或 codex plugin add 子命令。因此上述命令负责注册 Marketplace，随后必须：

1. 重启 Codex 桌面版。
2. 打开 Plugins。
3. 选择 Task Anchor Local Marketplace。
4. 安装并启用 Task Anchor。
5. 打开 Codex 设置中的 Hooks 页面；CLI 也可以输入 `/hooks`。
6. 找到 Task Anchor Hook，将其设为允许，并完成信任。
7. 确认该 Hook 不再处于待审查、未信任或禁用状态。
8. 在新建的 Codex 任务中显式调用 `$task-anchor` 进行测试。

其中第 5～7 步是核心功能的强制前提。没有完成 Hook 允许和信任，即使插件出现在 Plugins 列表并显示已启用，也不会保存或恢复任何任务指令，不能视为安装成功。

GitHub 仓库也可以不克隆直接注册：

    codex plugin marketplace add owner/repository

ZIP 只是源码分发包，不是 Codex 专用安装格式。解压后仍需在包含 .agents 目录的根目录执行 Marketplace 注册命令。

## 20. 已实施：独立任务 ID 与被动状态提醒

> 状态：**已于 2026-07-16 获得批准并完成源码实施（22 项单元测试）。** 当前实现不自动判断任务完成；新版本必须重新安装并重新信任 Hook 后，才会在后续新会话生效。

### 20.1 要解决的问题

`session_id` 只代表一个持续的 Codex 对话，不代表其中的一个语义任务。一个对话可以先完成任务 A，再进行普通交流，之后再显式开始任务 B。因此不能用 `session_id` 直接作为任务 ID，也不能因为同一对话后续发生压缩而恢复任务 A 的指令。

本方案只保留一个面向用户的 Skill：`$task-anchor`。不增加 `$task-anchor-end` 或其他要求用户手动结束任务的 Skill。

### 20.2 目标行为

1. 用户每次显式调用 `$task-anchor <任务指令>`，插件在当前 `session_id` 中创建一个新的 `task_id`，并将该任务状态设为 `1`（进行中）。
2. `$task-anchor` Skill 使用 Codex 原生 TOLIST 推进该任务。
3. 已完成验证的当前基线为 `codex-cli 0.144.4`：宿主未提供可由插件 Hook 订阅、且可靠表示语义任务完成的内部动作或事件。因此本方案当前只能进入 20.6 定义的被动状态提醒兜底路径；只有未来重新验证出该能力后，才允许由宿主机制关闭当前 `task_id`，且不得依赖模型的文本、选择或最终回复。
4. 压缩后只读取当前对话的当前 `task_id`：状态为 `1` 才注入该任务的最初指令；状态为 `0` 则不注入。
5. 每一次显式调用 `$task-anchor` 都是一次原子切换：先把当前 `session_id` 下已有任务全部置为 `0`，并写入审计关闭原因；再创建新的 `task_id` 并置为 `1`。因此不会静默复用或覆盖旧任务。

### 20.3 状态模型

```text
session_id（对话容器）
  └── current_task_id
        └── task_id（一次显式 $task-anchor 调用）
              ├── status = 1：进行中，可在压缩后注入
              └── status = 0：已关闭，压缩后不注入
```

实现的持久化边界如下；真实 `session_id` 仍只以哈希形式保存：

```text
PLUGIN_DATA/
└── sessions/
    └── <session_id_sha256>/
        ├── current_task.json
        ├── transition.json（仅在显式新任务切换期间存在）
        └── tasks/
            └── <task_id>/
                ├── initial_instruction.txt
                └── metadata.json
```

`metadata.json` 至少包含：`task_id`、`session_id` 哈希或等价校验值、项目身份哈希、初始指令 SHA-256、创建时间、关闭时间、整数状态 `status` 和关闭原因 `closed_reason`。当前基线中，`status = 0` 的关闭原因只会是 `superseded_by_new_task`；普通交付不会被误记为已完成。`transition.json` 用于把“关闭旧任务、激活新任务、更新 current 指针”串成可恢复切换；压缩时若发现未完成切换，只提示且不改变状态。

### 20.4 任务边界

- `task_id` 只能在显式 `$task-anchor` 启动时生成，不能从 `session_id` 派生，也不能由压缩恢复生成。
- `task_id` 是单个任务记录，不包含子任务；“把当前任务 ID 下的所有 task 置为 0”在数据结构上统一表述为：把当前 `session_id` 下已有的任务记录全部置为 `0`。
- 同一对话中同一时刻只能有一个状态为 `1` 的当前任务。每次新的显式 `$task-anchor` 都会先关闭该会话下已有任务，再创建新的活动任务；这个切换必须作为一笔可审计事务完成。
- 完成动作不是依据模型最终回复中的自然语言猜测，也不是把 `Stop` 当作完成信号；`Stop` 只表示一次助手回合结束。

### 20.5 同一个 Skill 内的完成约束

在唯一的 `$task-anchor` Skill 中加入以下规则：

1. 任务未完成时，持续维护原生 TOLIST，不能把未完成任务标为已完成。
2. 截至当前验证基线，没有可被插件 Hook 订阅的可靠完成机制；当前 `task_id` 的关闭不能由模型发起，也不能从模型文本推断。未来若该机制出现，关闭仍只能由宿主机制完成。
3. 没有可靠完成机制时，Skill 只能规范模型的工作与交付行为，不能把模型最终回复的自然语言当作持久化状态变更依据。
4. 这种兜底模式下，`PostCompact` 只能附加被动状态提醒；它不能提问、提供选项、等待用户回应、阻断正常任务或改变任务状态。
5. 普通中间回复、工具调用完成、一次 `Stop`、TOLIST 暂时为空，都不能触发完成动作。

### 20.6 已完成的技术验证与兜底规则

当前 `UserPromptSubmit` 和 `PostCompact` Hook 都不直接提供“模型即将交付最终结果”的可靠状态。已于 2026-07-16 在本机 `codex-cli 0.144.4` 上完成接口验证；以下结论只覆盖当前已公开且可由插件使用的接口。

#### 20.6.1 已验证的接口边界

- 当前 Hook 事件枚举只有 `preToolUse`、`permissionRequest`、`postToolUse`、`preCompact`、`postCompact`、`sessionStart`、`userPromptSubmit`、`subagentStart`、`subagentStop`、`stop`；没有 `TaskCompleted`、`TaskEnd` 或 `thread/goal/updated`。因此插件不能订阅语义任务完成事件。
- `Stop` 是 turn 范围事件；App Server 的 `turn/completed` 也只表示一次 turn 的状态为 `completed`、`interrupted` 或 `failed`，不能代表一个多回合语义任务已经完成。普通中间回复同样会产生这种 turn 结束。
- App Server 存在持久化的 `ThreadGoal`，状态包含 `active`、`paused`、`blocked`、`usageLimited`、`budgetLimited`、`complete`，并提供 `thread/goal/set` 与 `thread/goal/updated`。但它属于独立的 App Server 协议，不在插件 Hook 事件枚举中；当前公开接口也没有证明宿主会在模型最终交付时自动把 Goal 写为 `complete` 并回调插件。
- 当前代理侧的 `update_goal` 是一个需要代理显式调用的动作，不是插件可订阅的宿主完成回调，因此不满足“不能由模型触发”的完成条件。

#### 20.6.2 当前实施结论

- 当前没有可直接用于 Task Anchor 的可靠自动关闭机制。不得使用 `Stop`、`turn/completed`、TOLIST 清空、模型最终回复文本、`ThreadGoal` 状态或其他间接信号把 `status` 从 `1` 改为 `0`。
- 不为绕过该限制而让插件启动或常驻连接 App Server 客户端、守护进程或其他额外进程；这既不是现有 Hook 的直接能力，也会扩大插件运行边界。
- 因此现阶段只能用 `$task-anchor` Skill 规范模型的执行和交付行为。普通交付后，当前 `task_id` 仍保持 `1`；只有下一次显式 `$task-anchor` 启动新任务时，才以 `closed_reason = superseded_by_new_task` 将旧任务置为 `0`，再建立新的活动任务。
- 在该兜底模式下，`PostCompact` 注入只能被动提示当前 `task_id`、当前 `status` 与“自动完成状态无法验证”的事实；它不得提问、提供选择、等待回应、阻断任务或改变状态。用户可在看到提醒后自行决定后续操作，但该提醒本身不要求用户作答。

#### 20.6.3 重新评估的前提

只有 Codex 后续正式提供可被插件 Hook 订阅的任务或 Goal 完成事件，且事件携带可稳定关联当前会话与任务状态的标识时，才可以重新评估由宿主机制自动关闭 `task_id`；该变更仍需单独形成实施方案并获得批准。

### 20.7 压缩恢复规则

```text
按 session_id 找到 current_task_id
        ↓
读取该 task_id 的 status
        ↓
status == 1  → 校验并注入该 task 的最初任务指令及当前状态提示
status == 0  → 不注入，记录“任务已关闭而跳过恢复”
```

恢复包必须携带当前 `task_id`，以便 Skill 仅继续正确的那一个任务。压缩恢复不能创建任务、不能改变状态、不能复活已关闭任务。

在没有可靠内部完成动作时，状态提示至少应包含：当前 `task_id`、`status`，以及该状态尚未由自动完成机制验证。提示只用于让用户掌握状态；它不能提问、提供选择、等待回应、打断正在进行的任务或改变状态。用户可在看到提醒后自行决定后续操作，但该提醒本身不要求用户作答。

当前验证基线下，普通最终交付不会自动关闭任务：在下一次显式 `$task-anchor` 启动新任务前，原 `task_id` 仍为 `1`，后续压缩仍会按上述被动提醒规则恢复它。新的显式启动会先以 `closed_reason = superseded_by_new_task` 关闭旧记录；这表示“被新任务取代”，不能误记为“正常交付完成”。

### 20.8 验收测试

1. 同一 `session_id` 中连续两次显式调用 `$task-anchor`，两个 `task_id` 必须不同；第二次调用前该会话下已有任务全部变为 `0`，并具有可审计关闭原因。
2. 任务 A 已通过任一合法路径变为 `0` 后，插入任意普通对话再压缩，不得注入任务 A。
3. 任务 B 显式开始后压缩，只能注入任务 B，不能注入任务 A。
4. 任务进行中的多次自动和手动压缩，始终注入相同 `task_id` 的原始指令。
5. 存在内部完成动作时，完成动作失败必须保持 `1`，且模型不得报告任务已完成。
6. 没有内部完成动作时，`PostCompact` 注入必须显示被动状态提醒，不能提问、提供选项、等待回应或阻断任务，且不能把状态从 `1` 擅自改为 `0`。
7. `Stop`、普通回复、普通工具调用都不得把状态从 `1` 改为 `0`。
8. 所有状态转换、被新任务取代和跳过恢复都必须写入不含任务正文的审计日志。
9. 在验证基线 `codex-cli 0.144.4` 上，`Stop`、App Server `turn/completed`、`thread/goal/updated` 都不得作为关闭触发器；插件也不得为此启动或常驻连接额外进程。
10. 普通最终交付后、下一次显式 `$task-anchor` 前，任务必须保持 `status = 1`；若发生压缩，只能显示被动状态提醒，不能自动关闭。
11. 写入中断留下 `transition.json` 时，`PostCompact` 只能提示且不得改变任何状态；下一次显式启动必须在会话锁内先恢复该切换，再开始新的切换。

### 20.9 当前实现

当前工作区已实现本节的数据模型：每次显式 `$task-anchor` 创建 UUID `task_id`；新启动在会话锁内关闭既有活动记录、激活新记录并更新当前指针；切换记录可恢复且可审计。唯一保留的用户 Skill 是 `$task-anchor`，旧结束 Skill 已删除。插件只注册 `UserPromptSubmit` 和 `PostCompact`，不启动常驻外部进程。
