---
name: task-anchor-readonly
description: "仅在用户显式调用 $task-anchor-readonly 时使用。将当前会话、当前项目切换为只读模式，由 Task Anchor Hook 在修改型工具执行前拦截。"
---

# Task Anchor Readonly

用户已明确要求将当前会话、当前项目切换为只读模式。`UserPromptSubmit` Hook 会在本消息提交时持久化 `read_only = true`。

## 行为边界

- 只读标记仅绑定当前会话和当前项目，不影响其他会话或项目。
- 只读模式允许检查文件和分析现状，但会在执行前拒绝修改型工具以及可运行任意命令的工具。
- 不要把 Skill 文本本身当作切换成功的证据；若 Hook 报告缺少上下文、写入失败或状态冲突，以 Hook 提示为准。
- 需要恢复修改权限时，由用户显式调用 `$task-anchor-write`。

# SKILL 作者

嘉兴云祥软件 https://www.jxrjkf.cn
