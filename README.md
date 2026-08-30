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
- 只读门控按“会话 + 工作区”保存；它不替代宿主自身的权限、沙箱或审批机制。
- managed_exec 资源按“会话 + 工作区”登记；默认 cleanup 资源在 Stop/SessionEnd 清理，keep 资源仅由显式 stop 关闭。

## 前置条件

- Node.js：直接运行 managed_exec MCP，负责本地进程启动、资源登记、超时和停止。
- Python 3.10+：运行 Hook 和任务状态机；可使用 `TASK_ANCHOR_PYTHON` 指定解释器。
- 普通资源到达 `timeout_ms` 会真正结束整个进程树；`wait=false` 仍然计时，`timeout_ms=null` 不自动超时。
- `stop_policy=keep` 必须设置 `name`，且不受 timeout_ms、Stop 或 SessionEnd 影响；不需要时必须显式调用 `operation=stop`。

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

Claude Code 的 `managed_exec` 通过 Node MCP 直接启动真实命令并绑定当前会话和项目。普通资源到达 `timeout_ms` 会结束整个进程树，`wait=false` 也会继续计时，`timeout_ms=null` 不自动超时；`stop_policy=keep` 必须设置 `name`，不受 timeout_ms、Stop/SessionEnd 影响，须显式调用 `operation=stop` 关闭。

插件状态保存在 `${CLAUDE_PLUGIN_DATA}`；插件更新不会抹掉这些数据。

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

Codex 的 `managed_exec` 通过 Node MCP 直接启动真实命令；普通资源到达 `timeout_ms` 会结束整个进程树，`wait=false` 也会继续计时，`timeout_ms=null` 不自动超时。`stop_policy=keep` 必须设置 `name`，不受 timeout_ms、Stop/SessionEnd 影响，须在不需要时显式调用 `operation=stop`。

### 使用

```text
$task-anchor <完整任务指令>
$task-anchor-end
$task-anchor-readonly
$task-anchor-write
```

Codex 版通过 Node MCP 的 `managed_exec` 直接启动并登记真实命令；普通资源支持 `timeout_ms` 超时结束整棵进程树，`wait=false` 仍会继续计时，`timeout_ms=null` 不自动超时。`stop_policy=keep` 资源必须设置 `name`，不受 Stop/SessionEnd 影响，须显式调用 `operation=stop` 停止。

## 测试

运行 Codex Python 测试：

```bash
python -m unittest discover -s plugins/task-anchor/tests -p "test_*.py"
```

运行 Claude Code Python 测试：

```bash
python -m unittest discover -s plugins/task-anchor-claude/tests -p "test_*.py"
```

运行 Codex Node 启动器、MCP 和资源管理测试：

```bash
node --test plugins/task-anchor/tests/*.cjs
```

运行 Claude Code Node 启动器、MCP 和资源管理测试：

```bash
node --test plugins/task-anchor-claude/tests/*.cjs
```

建议额外以 `--plugin-dir plugins/task-anchor-claude` 启动 Claude Code，验证四个技能、自动与手动压缩恢复、只读状态切换，以及 cleanup/keep 资源在 `Stop` 与 `SessionEnd` 下的行为。
