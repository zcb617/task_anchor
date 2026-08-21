---
name: task-anchor-write
description: "解除当前会话和项目的 Task Anchor 只读门控。"
disable-model-invocation: true
---

# Task Anchor Write

此技能只能由用户显式调用：`/task-anchor:task-anchor-write`。

`UserPromptExpansion` Hook 会解除当前会话、当前项目的 Task Anchor 只读门控。它不会绕过 Claude Code 的权限、沙箱、审批流程或 Task Anchor 对受管命令的约束。

若 Hook 报告上下文缺失、写入失败或状态冲突，以 Hook 提示为准。需要重新禁止修改时，用户可调用 `/task-anchor:task-anchor-readonly`。
