# Task Anchor：任务指令跨压缩传递方案

版本：5.3  
日期：2026-07-15  
目标 Codex CLI：0.144.1  
目标目录：`D:\work\task_anchor`

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
│   └── task-anchor/
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

1. `$task-anchor` Skill。
2. `UserPromptSubmit` Hook。
3. `SessionStart(source=compact)` Hook。
4. 一个负责保存和读取最初任务指令的 Python 脚本。

明确不包含：

- MCP。
- 小模型或子代理。
- 自定义 TOLIST。
- `PostToolUse`。
- `PreCompact`。
- `PostCompact`。
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

1. 当前提示词不包含显式 `$task-anchor`：立即退出，不保存、不注入、不改变状态。
2. 当前提示词首次显式调用 `$task-anchor`：保存 Hook 收到的完整提示词原文。
3. 同一 Codex 任务再次调用 `$task-anchor`：只重新启用 Skill，不覆盖最初任务指令。

这套规则不需要自动判断任务何时开始，也不要求用户发送结束口令。

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

`metadata.json` 只保存：

```json
{
  "session_id": "...",
  "sha256": "...",
  "created_at": "...",
  "active": true
}
```

规则：

- 初始指令只写入一次。
- 已存在时禁止覆盖。
- 每次读取都校验 SHA-256。
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

上下文压缩完成后，Codex 会以 `source=compact` 触发 `SessionStart`。

Hook 根据当前 `session_id` 读取并校验最初任务指令，通过 `additionalContext` 注入：

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

## 11. 同一任务再次调用 Skill

同一 `session_id` 内再次显式调用 `$task-anchor`：

- 不创建新任务。
- 不替换初始指令。
- 不清空原生 TOLIST。
- 只要求模型重新加载并继续执行 Skill。

压缩后的 `SessionStart` 注入属于 developer context，不会触发 `UserPromptSubmit`，因此不会覆盖初始任务指令。

## 12. 新任务

第一版以一个 Codex 任务对应一个任务锚点为边界。

新建 Codex 任务会获得新的 `session_id`，因此自动获得独立的初始任务指令记录。

第一版不在同一个 Codex 任务中自动识别多个语义任务，也不判断旧任务何时结束。这避免重新引入不可靠的任务边界猜测。

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
SessionStart matcher=compact
```

`UserPromptSubmit` 的 matcher 不受支持，因此必须在 `hook_entry.py` 内执行显式条件判断。

`SessionStart` 使用 `matcher=compact`，只在上下文压缩后的会话恢复阶段运行。

### 14.1 Hook 信任是运行前提

安装或启用插件不等于 Hook 已启用。Task Anchor 的命令 Hook 属于非托管 Hook，Codex 必须先让用户审查其定义，并记录用户的允许和信任结果。

在 Hook 获得允许和信任之前，Codex 会直接跳过 Hook：

- `UserPromptSubmit` 不会保存最初任务指令。
- `SessionStart(source=compact)` 不会在压缩后注入任务指令。
- Skill 即使能够显示和调用，也不代表任务锚点已经建立。

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
compact_received
anchor_loaded
restore_emitted
```

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

### Skill

15. Skill 使用 Codex 原生 TOLIST。
16. Skill 不创建自定义 TOLIST。
17. 压缩后注入的 `$task-anchor` 指令能使主模型重新使用 Skill。

## 16. 完成标准

只有同时满足以下条件才能宣布插件完成：

- 安装一个插件即可安装 Skill 和全部 Hook。
- 用户已在 Codex Hook 设置中允许并信任 Task Anchor Hook；否则安装不算完成。
- 用户只有在任务开始时需要显式调用一次 `$task-anchor`。
- 最初任务指令按 Hook 收到的原文保存。
- 同一任务后续调用不会覆盖原文。
- 每次压缩后都从原始存储重新注入。
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
5. `SessionStart(source=compact)` 重新注入后是否能稳定触发 Skill，必须在 Codex CLI 0.144.1 中实测。

## 18. 最终工作链路

```text
用户显式调用 $task-anchor
        ↓
UserPromptSubmit 保存最初任务指令原文
        ↓
Skill 使用 Codex 原生 TOLIST 推进任务
        ↓
发生上下文压缩
        ↓
SessionStart(source=compact)
        ↓
从原始存储读取同一份任务指令
        ↓
注入“必须使用 Skill + 最初任务指令原文”
        ↓
模型沿原始任务方向继续工作
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
