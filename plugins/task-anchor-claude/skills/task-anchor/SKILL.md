---
name: task-anchor
description: "开始需要跨上下文压缩持续推进的独立任务。"
disable-model-invocation: true
---

# Task Anchor

此技能只能由用户显式调用：`/task-anchor:task-anchor <任务指令>`。

`UserPromptExpansion` Hook 会在技能展开前将命令参数保存为新的独立任务，并将当前会话、当前项目的只读门控切换为可写。若 Hook 报告失败，以 Hook 提示为准，不要声称任务已经建立。

## 推进规则

1. 在开始实质工作前，使用 Claude Code 的计划能力建立并维护工作计划。
2. 用户明确改变目标、约束或方向时，以较新的明确指令为准。
3. 不要把普通交付、工具调用完成、计划清空、最终回复、`Stop` 或 `SessionEnd` 当作任务完成；任务只会被 `/task-anchor:task-anchor-end` 明确结束，或被下一个锚定任务取代。

## 受管命令执行

- 查询命令使用宿主原生工具。查询/只读命令（明确包括 `pwd`、`ls`、`git status`、`rg` 和读取文件）使用 Codex/Claude Code 宿主原生工具，不调用 `mcp__plugin_task-anchor_task-anchor__managed_exec`；Windows 下不要把 Unix 命令名作为 managed_exec 的 program，除非明确使用宿主支持的 shell 命令。\n- node/bun/npm/python/java 等进程命令使用 managed_exec。只有会启动本地进程、启动开发服务、测试服务或持续运行程序的命令（包括 Codex 现有关键词范围）才调用 `mcp__plugin_task-anchor_task-anchor__managed_exec`。\n- Node managed_exec 负责启动、登记、超时和停止本地进程；普通资源到达 `timeout_ms` 会结束整个进程树。
- 默认 `stop_policy` 为 `cleanup`，Stop/SessionEnd 会清理普通资源；`keep` 资源不受 timeout_ms 和 Stop/SessionEnd 影响，必须设置 `name`，并在不需要时显式调用 `operation: "stop"`。
- 优先使用 `program` 与 `args`；只有确实需要管道、重定向或复合命令时才使用 `shell: true` 与 `command`。

## 压缩后继续

收到 Task Anchor 注入的状态提醒时，重新读取其中的原始任务指令并继续未完成工作。该提醒不要求回复、不会建立新任务，也不会改变当前状态。
