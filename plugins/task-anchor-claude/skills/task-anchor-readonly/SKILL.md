---
name: task-anchor-readonly
description: "将当前会话和项目切换为由 Task Anchor 强制执行的只读模式。"
disable-model-invocation: true
---

# Task Anchor Readonly

此技能只能由用户显式调用：`/task-anchor:task-anchor-readonly`。

`UserPromptExpansion` Hook 会把当前会话、当前项目切换为只读。启用后，Task Anchor 会在修改型工具和任意命令执行前拒绝请求；只读检查工具仍可使用。

该门控不会影响其他会话或项目，也不会替代 Claude Code 本身的权限、沙箱或审批。若 Hook 报告上下文缺失、写入失败或状态冲突，以 Hook 提示为准。用户可用 `/task-anchor:task-anchor-write` 解除这一门控。
