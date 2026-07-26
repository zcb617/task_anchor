---
name: task-anchor-end
description: "仅在用户显式调用 $task-anchor-end 时使用。手工结束当前会话、当前项目绑定下的 Task Anchor 任务，使其不再在上下文压缩后恢复。"
---

# Task Anchor End

用户已明确要求结束当前 Task Anchor 任务。`UserPromptSubmit` Hook 会在本消息提交时执行状态变更。

## 结束边界

- 只结束当前会话、当前项目绑定下的当前任务；不删除任务记录，也不影响其他会话。
- 结束后任务状态为 `status = 0`，后续 `PostCompact` 不再注入最初任务指令。
- 不把这项状态变更解释为中止已经开始的工具调用，也不创建新的任务。
- 若 Hook 报告没有当前任务、项目边界不一致或任务切换尚未完成，以 Hook 的提示为准，不要声称任务已经结束。
