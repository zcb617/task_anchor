---
name: task-anchor-end
description: "手工结束当前会话和项目中的 Task Anchor 任务。"
disable-model-invocation: true
---

# Task Anchor End

此技能只能由用户显式调用：`/task-anchor:task-anchor-end`。

`UserPromptExpansion` Hook 会结束当前会话、当前项目绑定的活动任务。结束仅改变持久化任务状态：不会删除任务记录、不会中止已开始的工具调用，也不会影响其他会话或项目。

若 Hook 提示没有当前任务、项目边界不匹配或状态无法验证，以该提示为准，不要声称任务已经结束。
