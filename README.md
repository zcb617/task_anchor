# Task Anchor

## 受管进程执行

插件提供 `mcp__task_anchor__managed_exec` 工具。包含进程型关键词的本地命令应通过该工具执行，插件会登记 PID、工作目录、命令和停止策略；短命查询命令可以直接执行。

- 默认 `stop_policy` 为 `cleanup`：Stop 时关闭该任务登记的进程树。
- `stop_policy: "keep"`：Stop 时保留服务；建议同时设置 `name`，不再需要时使用 `operation: "stop"` 关闭。
- `PreToolUse` Hook 会检查命令字符串；命中 `java`、`python`、`node`、`npm`、`mvn`、`gradle` 等进程型关键词时，必须通过 `managed_exec`，`rg`、`Get-Content` 等查询命令直接放行。
- `Stop` Hook 只清理受管且策略为 `cleanup` 的资源，不会按进程名扫描或误杀用户手工启动的程序。
- 受控管理器会自动识别 Windows、macOS 和 Linux：Windows 使用 `taskkill /T` 结束进程树；macOS/Linux 使用独立进程组和 `killpg` 结束进程组。
- 资源归属按“项目工作区 + 会话”隔离；同一会话内登记的资源由该会话统一管理，其他会话以及其他项目的资源都不会被当前 Stop 清理。
- `task_id` 只作为内部审计字段，不作为清理边界。没有明确会话上下文的资源不会进入自动清理范围；需要单独关闭某个资源时使用它的 `run_id` 或 `name`。

Task Anchor 是一个 Codex 插件：用户显式调用 `$task-anchor` 时创建一项独立任务；发生上下文压缩后，只恢复该会话当前仍为活动状态的任务指令。

> [!IMPORTANT]
> 安装并启用插件不等于 Hook 已启用。必须在 Codex 的 Hook 设置中允许并信任 Task Anchor Hook；否则最初任务不会保存，压缩后也不会恢复。

## 已验证环境与运行方式

- 已验证基线：`codex-cli 0.144.4`。
- 需要可执行的 `codex` 和 Python 3；Hook 在 Windows 使用 `python`，在 macOS/Linux 使用 `python3`，MCP 配置使用 `python` 作为启动入口。
- Hook 触发时运行一次 Python 脚本；`managed_exec` MCP 服务由 Codex 按 MCP 生命周期管理，受管业务进程由工具登记并按策略清理。

## 从源码安装

在仓库根目录注册 Marketplace 并安装插件：

    codex plugin marketplace add .
    codex plugin add task-anchor@task-anchor-local

然后在 Codex 的 Plugins 中确认 Task Anchor 已启用，并在 Hooks 设置（CLI 可用 `/hooks`）中允许、信任该插件的四个 Hook：

- `UserPromptSubmit`
- `PostCompact`
- `PreToolUse`
- `Stop`

Hook 文件变化后，Codex 会要求重新审查和信任；在重新信任前，更新后的 Hook 会被跳过。

## 更新本地插件

更新源码并通过缓存版本号生成器更新版本后，重新安装当前 Marketplace 中的插件：

    python C:\Users\zhang\.codex\skills\.system\plugin-creator\scripts\update_plugin_cachebuster.py plugins\task-anchor
    codex plugin add task-anchor@task-anchor-local

重新信任 Hook，并在新的 Codex 对话中验证。已经创建的对话不会自动重新加载修改后的 Skill 内容。

## 使用

开始任务 A：

    $task-anchor <任务 A 的完整指令>

同一对话中开始任务 B：

    $task-anchor <任务 B 的完整指令>

每一次显式调用都会生成新的 `task_id`。调用任务 B 时，插件会把该会话中已有任务标记为 `status = 0`，并以 `closed_reason = superseded_by_new_task` 记录关闭原因，再创建任务 B 为 `status = 1`。

手工结束当前任务：

    $task-anchor-end

结束命令只关闭当前会话、当前项目绑定下的任务：它会将该任务标为 `status = 0`，记录 `closed_reason = manually_ended`，并保留历史记录。结束后压缩不会再恢复该任务。它不会中止已经开始的工具调用。

不要通过最终回复、原生 TOLIST 清空、普通工具调用或 `Stop` 事件结束任务。

## 压缩后的状态

`PostCompact` 只读取当前 `task_id`：

- `status = 1`：重新注入原始任务指令，并附加 `task_id`、`status = 1` 和“自动完成状态无法验证”的被动提醒。
- `status = 0`：不注入任务指令。

当前 Codex 插件接口没有插件可订阅的可靠“语义任务已完成”事件。因此普通最终交付后，任务仍保持 `status = 1`；压缩时会继续恢复它。提醒不提问、不提供选项、不等待用户回应、不打断任务，也不会改变状态。用户可显式调用 `$task-anchor-end` 手工结束；下一次显式 `$task-anchor` 则会关闭旧任务并创建新任务。

## Hook 审计日志

审计日志位于 `PLUGIN_DATA/audit/events.jsonl`。它仅保存 Hook 事件、UTC 时间、会话哈希、项目哈希、任务 ID、状态及指令字节数/SHA-256；不保存任务正文、普通用户消息或真实 `session_id`。

常见事件包括：

    activation_received
    task_closed
    activation_completed
    post_compact_received
    restore_emitted
    restore_closed

## 仓库结构

    .agents/plugins/marketplace.json
    plugins/task-anchor/.codex-plugin/plugin.json
    plugins/task-anchor/.mcp.json
    plugins/task-anchor/hooks/hooks.json
    plugins/task-anchor/scripts/hook_entry.py
    plugins/task-anchor/scripts/resource_manager.py
    plugins/task-anchor/scripts/managed_exec_mcp.py
    plugins/task-anchor/skills/task-anchor/SKILL.md

插件不包含小模型、自定义 TOLIST、`PostToolUse` 或 `PreCompact`；MCP 服务只提供受管命令执行工具。
