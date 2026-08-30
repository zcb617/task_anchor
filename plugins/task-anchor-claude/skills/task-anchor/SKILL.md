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

## 原生命令执行边界

本插件不接管本地命令执行，所有命令按 Claude Code 原生工具方式执行；当前版本不通过 Task Anchor 提供命令超时或自动停止。

## 压缩后继续

收到 Task Anchor 注入的状态提醒时，重新读取其中的原始任务指令并继续未完成工作。该提醒不要求回复、不会建立新任务，也不会改变当前状态。
