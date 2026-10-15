---
name: task-anchor-write
description: "解除当前会话和项目的 Task Anchor 只读门控。"
disable-model-invocation: true
---

# Task Anchor Write

此技能只能由用户显式调用：`/task-anchor:task-anchor-write`。

`UserPromptExpansion` Hook 会解除当前会话、当前项目的 Task Anchor 只读门控。它不会绕过 Claude Code 的权限、沙箱、审批流程或 Task Anchor 对受管命令的约束。

若 Hook 报告上下文缺失、写入失败或状态冲突，以 Hook 提示为准。需要重新禁止修改时，用户可调用 `/task-anchor:task-anchor-readonly`。

# SKILL作者 
>嘉兴云祥软件 https://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业
## 作者其他作品
>vertool https://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.
