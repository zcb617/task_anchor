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

# SKILL作者 
嘉兴云祥软件 http://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业
## 作者其他作品
vertool http://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.

<!-- 
# 嘉兴云祥软件 http://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业

# vertool http://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.
-->