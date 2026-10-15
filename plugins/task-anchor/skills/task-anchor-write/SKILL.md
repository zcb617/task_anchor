---
name: task-anchor-write
description: "仅在用户显式调用 $task-anchor-write 时使用。清除当前会话、当前项目的只读限制，使 Task Anchor 只读门控允许修改操作。"
---

# Task Anchor Write

用户已明确要求恢复当前会话、当前项目的修改权限。`UserPromptSubmit` Hook 会在本消息提交时持久化 `read_only = false`。

## 行为边界

- 只解除当前会话、当前项目的 Task Anchor 只读门控，不影响其他会话或项目。
- 不会绕过 Codex 自身权限、沙箱、审批流程或 Task Anchor 已有的受管命令规则。
- 不要把 Skill 文本本身当作切换成功的证据；若 Hook 报告缺少上下文、写入失败或状态冲突，以 Hook 提示为准。
- 需要重新禁止修改时，由用户显式调用 `$task-anchor-readonly`。

# SKILL作者 
>嘉兴云祥软件 https://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业
## 作者其他作品
>vertool https://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.

