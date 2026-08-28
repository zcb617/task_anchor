# Task Anchor

Task Anchor 将用户显式开始的任务持久化到当前会话和项目边界内，并在上下文压缩后恢复当前仍处于活动状态的原始任务指令。

本仓库同时发布两个独立插件包：

- Codex：[`plugins/task-anchor`](plugins/task-anchor)
- Claude Code：[`plugins/task-anchor-claude`](plugins/task-anchor-claude)

两个包共享相同的任务语义，但各自使用宿主原生的 marketplace、Hook、技能和 MCP 协议。安装包是自包含的；不要跨插件目录引用脚本或状态。

## 核心语义

- 显式开始任务会生成新的 `task_id`，并将同一会话内已有活动任务标记为 `superseded_by_new_task`。
- 任务记录绑定会话哈希和工作区哈希；跨会话、跨项目、损坏或校验失败的记录不会被恢复。
- 每次命中 `PostCompact` Hook 都注入对应 CLI 的规则重读提醒（Codex 为 `AGENTS.md`，Claude Code 为 `CLAUDE.md`）；原始任务指令仍只在当前 `status = 1` 且原有校验通过时恢复。普通交付、工具调用完成、最终回复、`Stop` 和 `SessionEnd` 都不是可靠的语义完成信号。
- 显式结束任务会记录 `manually_ended`，此后压缩不再恢复该任务。
- 只读门控按“会话 + 工作区”保存，拒绝修改型工具和任意命令工具；它不替代宿主自身的权限、沙箱或审批机制。
- 受管进程按“会话 + 工作区”登记。默认 `stop_policy: "cleanup"` 会在 `Stop`/`SessionEnd` 清理进程树；只有明确使用 `keep` 的服务会保留到显式停止。

## 前置条件

- Node.js：启动 `managed_exec` MCP 服务和跨平台 Hook 启动器。
- Python 3.10+：运行 Hook、状态机和受管进程服务。可使用 `TASK_ANCHOR_PYTHON` 指定解释器。
- Claude Code 使用受管命令的人工确认标记需要 v2.1.199 或更新版本。

## Claude Code

Claude Code 插件包位于 [`plugins/task-anchor-claude`](plugins/task-anchor-claude)。市场清单位于 [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json)。

### 安装

在 Claude Code 中依次执行：

```text
/plugin marketplace add .
```

```text
/plugin install task-anchor@task-anchor-local
```

安装或更新后运行 `/reload-plugins`，并审查/允许插件 Hook。开发时可严格验证插件布局：

```bash
claude plugin validate plugins/task-anchor-claude --strict
```

### 使用

| 操作 | Claude Code 命令 |
| --- | --- |
| 开始任务 | `/task-anchor:task-anchor <完整任务指令>` |
| 手工结束任务 | `/task-anchor:task-anchor-end` |
| 开启只读门控 | `/task-anchor:task-anchor-readonly` |
| 解除只读门控 | `/task-anchor:task-anchor-write` |

`/task-anchor:task-anchor` 只保存命令参数作为原始任务指令；普通对话里出现的 `$task-anchor` 文本不会触发任务创建。

Claude 版受管命令工具名为：

```text
mcp__plugin_task-anchor_task-anchor__managed_exec
```

Hook 会把该工具调用绑定到当前可信 `session_id` 和 `cwd`，不会接受调用内容提供的会话/工作区。每次受管命令都要求用户确认。优先提供 `program` 和 `args`；仅在需要管道、重定向或复合命令时使用 `shell: true` 与 `command`。保留服务必须设置 `stop_policy: "keep"` 和 `name`，并在不需要时调用该工具的 `operation: "stop"`。

插件状态保存在 `${CLAUDE_PLUGIN_DATA}`，受管进程运行时数据保存在其 `runtime/` 子目录；插件更新不会抹掉这些数据。

## Codex

Codex 插件包位于 [`plugins/task-anchor`](plugins/task-anchor)，市场清单位于 [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json)。

### 安装

```bash
codex plugin marketplace add .
```

```bash
codex plugin add task-anchor@task-anchor-local
```

在 Codex 中启用 Task Anchor，并允许/信任其 `UserPromptSubmit`、`PostCompact`、`PreToolUse` 和 `Stop` Hook。Hook 文件更新后需要重新审查和信任。

### 使用

```text
$task-anchor <完整任务指令>
$task-anchor-end
$task-anchor-readonly
$task-anchor-write
```

Codex 版的受管命令工具名是 `mcp__task_anchor__managed_exec`。其行为与 Claude Code 版的任务边界、只读门控和资源清理语义相同，但命令、Hook 数据包和工具名均为 Codex 专用。

## 测试

运行 Codex Python 测试：

```bash
python -m unittest discover -s plugins/task-anchor/tests -p "test_*.py"
```

运行 Claude Code Python 测试：

```bash
python -m unittest discover -s plugins/task-anchor-claude/tests -p "test_*.py"
```

运行 Node 启动器测试：

```bash
node --test plugins/task-anchor/tests/test_managed_exec_launcher.cjs
```

建议额外以 `--plugin-dir plugins/task-anchor-claude` 启动 Claude Code，验证四个技能、自动与手动压缩恢复、只读拒绝、每次 `managed_exec` 的人工确认，以及 cleanup/keep 资源在 `Stop` 与 `SessionEnd` 下的行为。
