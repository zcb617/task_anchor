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

## 前置条件

- Python 3.10+：运行 Hook 和状态机。可使用 `TASK_ANCHOR_PYTHON` 指定解释器。
- 当前插件不接管原生命令执行，也暂不提供命令超时或自动停止；原生命令由宿主按原方式执行。

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

在 Codex 中启用 Task Anchor，并允许/信任其 `UserPromptSubmit`、`PostCompact` 和 `Stop` Hook。Hook 文件更新后需要重新审查和信任。

### 使用

```text
$task-anchor <完整任务指令>
$task-anchor-end
$task-anchor-readonly
$task-anchor-write
```

Codex 版不接管原生命令执行；所有命令按 Codex 原生工具方式执行。当前版本不通过 Task Anchor 提供命令超时或自动停止。

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

建议额外以 `--plugin-dir plugins/task-anchor-claude` 启动 Claude Code，验证四个技能、自动与手动压缩恢复和只读状态切换。
